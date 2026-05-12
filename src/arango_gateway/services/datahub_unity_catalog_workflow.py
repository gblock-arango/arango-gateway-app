"""
Unity Catalog metadata extraction shaped for DataHub-style graphs.

Uses the Databricks Unity Catalog REST APIs via :class:`databricks.sdk.WorkspaceClient`,
the same surface area described in DataHub's ``databricks`` / ``unity-catalog`` source
(`https://docs.datahub.com/docs/generated/ingestion/sources/databricks/`).

This module does **not** emit DataHub MCP events or run ``datahub ingest``; it builds a
portable JSON graph (nodes + edges + URNs) that you can persist (e.g. Unity Catalog table
or volume) and load into ArangoDB. For full parity with DataHub profiling, lineage, and
usage statistics, install ``acryl-datahub[unity-catalog]`` and run a recipe with a sink.
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.catalog import ColumnInfo, TableInfo

_DEFAULT_MAX_TABLE_SCAN_BUDGET = 100_000


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _enum_str(value: Any) -> str | None:
    if value is None:
        return None
    v = getattr(value, "value", value)
    return str(v)


def _stable_key(*parts: str) -> str:
    raw = "\x1f".join(parts)
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", raw)
    if len(safe) <= 248:
        return safe
    return "k_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def _dataset_urn(catalog: str, schema: str, table: str, env: str) -> str:
    name = f"{catalog}.{schema}.{table}"
    return f"urn:li:dataset:(urn:li:dataPlatform:databricks,{name},{env})"


def _container_urn_catalog(catalog: str, env: str) -> str:
    return f"urn:li:container:(urn:li:dataPlatform:databricks,{catalog},{env})"


def _container_urn_schema(catalog: str, schema: str, env: str) -> str:
    return f"urn:li:container:(urn:li:dataPlatform:databricks,{catalog}.{schema},{env})"


def _schema_field_urn(dataset_urn: str, field_path: str) -> str:
    return f"urn:li:schemaField:({dataset_urn},{field_path})"


def _serialize_column(col: ColumnInfo) -> dict[str, Any]:
    return {
        "name": col.name,
        "type_text": col.type_text,
        "type_name": _enum_str(col.type_name),
        "type_json": col.type_json,
        "nullable": col.nullable,
        "comment": col.comment,
        "position": col.position,
        "partition_index": col.partition_index,
        "type_precision": col.type_precision,
        "type_scale": col.type_scale,
    }


def _serialize_table_metrics(t: TableInfo) -> dict[str, Any]:
    return {
        "table_type": _enum_str(t.table_type),
        "data_source_format": _enum_str(t.data_source_format),
        "owner": t.owner,
        "comment": t.comment,
        "created_at": t.created_at,
        "updated_at": t.updated_at,
        "created_by": t.created_by,
        "updated_by": t.updated_by,
        "storage_location": t.storage_location,
        "table_id": t.table_id,
    }


def _schema_name_matched(patterns: Iterable[str], catalog: str, schema: str) -> bool:
    fq = f"{catalog}.{schema}"
    for p in patterns:
        if fnmatch.fnmatch(schema, p) or fnmatch.fnmatch(fq, p):
            return True
    return False


def _normalize_table_id(raw: Any) -> str:
    if raw is None:
        return ""
    return str(raw).strip().lower()


def _normalize_full_name(raw: str) -> str:
    return str(raw or "").strip().lower()


@dataclass
class UnityCatalogGraphOptions:
    """Options mirroring common DataHub recipe knobs (subset)."""

    catalogs_allowlist: list[str] | None = None
    schema_pattern_deny: list[str] = field(
        default_factory=lambda: ["information_schema"]
    )
    platform_instance_env: str = "PROD"
    max_catalogs: int | None = None
    max_schemas_per_catalog: int | None = None
    max_tables_total: int | None = 5000
    """Max tables to **include** in the graph when no table allowlist is set."""
    max_table_scan_budget: int | None = None
    """Max table rows to **visit** when a table allowlist is set (bounds UC walks)."""
    table_ids_allowlist: frozenset[str] | None = None
    """Normalized lowercase table_id values; union with ``table_full_names_allowlist``."""
    table_full_names_allowlist: frozenset[str] | None = None
    """Normalized lowercase ``catalog.schema.table`` full names; union with ids."""
    include_columns: bool = True
    include_delta_metadata: bool = False
    delta_metadata_max_tables: int = 100


def _table_allowlists_active(opts: UnityCatalogGraphOptions) -> bool:
    ids = opts.table_ids_allowlist
    names = opts.table_full_names_allowlist
    return bool(ids) or bool(names)


def _table_matches_allowlist(
    tbl: TableInfo, full_name: str, opts: UnityCatalogGraphOptions
) -> bool:
    if not _table_allowlists_active(opts):
        return True
    tid = _normalize_table_id(tbl.table_id)
    fn = _normalize_full_name(full_name)
    ids = opts.table_ids_allowlist
    names = opts.table_full_names_allowlist
    if ids and tid and tid in ids:
        return True
    if names and fn and fn in names:
        return True
    return False


def extract_unity_catalog_graph(
    *,
    options: UnityCatalogGraphOptions | None = None,
    workspace_client: WorkspaceClient | None = None,
) -> dict[str, Any]:
    """
    Walk Unity Catalog and return a JSON-serializable graph plus DataHub-style URNs.

    Nodes use ``datahub_entity_type`` of ``container`` (catalog/schema), ``dataset``
    (table/view), or ``schemaField`` (column), aligned with DataHub's concept mapping.
    """
    opts = options or UnityCatalogGraphOptions()
    w = workspace_client or WorkspaceClient()
    host = getattr(w.config, "host", None) or ""

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    warnings: list[str] = []

    tables_processed = 0
    delta_fetches = 0
    allowlist_on = _table_allowlists_active(opts)
    scan_budget: int | None = None
    if allowlist_on:
        scan_budget = opts.max_table_scan_budget
        if scan_budget is None:
            scan_budget = _DEFAULT_MAX_TABLE_SCAN_BUDGET
    tables_visited = 0
    matched_ids: set[str] = set()
    matched_names: set[str] = set()
    scan_exhausted = False

    allow = opts.catalogs_allowlist
    if allow is not None:
        allow = [c.strip() for c in allow if c and str(c).strip()]
        if not allow:
            allow = None

    catalog_count = 0
    for cat in w.catalogs.list(max_results=0):
        if scan_exhausted:
            break
        cname = cat.name
        if not cname:
            continue
        if allow is not None and cname not in allow:
            continue
        if opts.max_catalogs is not None and catalog_count >= opts.max_catalogs:
            warnings.append(
                f"Stopped after max_catalogs={opts.max_catalogs}; remaining catalogs skipped."
            )
            break
        catalog_count += 1

        env = opts.platform_instance_env
        cat_id = _stable_key("cat", env, cname)
        cat_urn = _container_urn_catalog(cname, env)
        nodes.append(
            {
                "id": cat_id,
                "datahub_entity_type": "container",
                "datahub_urn": cat_urn,
                "labels": ["Catalog", "Container"],
                "properties": {
                    "name": cname,
                    "comment": cat.comment,
                    "owner": cat.owner,
                    "catalog_type": _enum_str(cat.catalog_type),
                    "created_at": cat.created_at,
                    "updated_at": cat.updated_at,
                },
            }
        )

        schema_num = 0
        for sch in w.schemas.list(catalog_name=cname, max_results=0):
            if scan_exhausted:
                break
            sname = sch.name
            if not sname:
                continue
            if opts.schema_pattern_deny and _schema_name_matched(
                opts.schema_pattern_deny, cname, sname
            ):
                continue
            if (
                opts.max_schemas_per_catalog is not None
                and schema_num >= opts.max_schemas_per_catalog
            ):
                warnings.append(
                    f"Catalog {cname!r}: stopped after max_schemas_per_catalog="
                    f"{opts.max_schemas_per_catalog}."
                )
                break
            schema_num += 1

            sch_id = _stable_key("sch", env, cname, sname)
            sch_urn = _container_urn_schema(cname, sname, env)
            nodes.append(
                {
                    "id": sch_id,
                    "datahub_entity_type": "container",
                    "datahub_urn": sch_urn,
                    "labels": ["Schema", "Container"],
                    "properties": {
                        "catalog": cname,
                        "name": sname,
                        "full_name": f"{cname}.{sname}",
                        "comment": sch.comment,
                        "owner": sch.owner,
                        "created_at": sch.created_at,
                        "updated_at": sch.updated_at,
                    },
                }
            )
            edges.append(
                {
                    "id": _stable_key("e", cat_id, sch_id, "SUBCONTAINER"),
                    "relationship_type": "SUBCONTAINER",
                    "from_id": cat_id,
                    "to_id": sch_id,
                    "properties": {"datahub_lineage_note": "catalog contains schema"},
                }
            )

            for tbl in w.tables.list(
                catalog_name=cname,
                schema_name=sname,
                max_results=0,
                omit_columns=not opts.include_columns,
                omit_properties=False,
            ):
                if allowlist_on:
                    tables_visited += 1
                    if scan_budget is not None and tables_visited > scan_budget:
                        warnings.append(
                            f"Stopped: max_table_scan_budget={scan_budget} UC table rows visited; "
                            "allowlist may be incomplete."
                        )
                        scan_exhausted = True
                        break
                elif (
                    opts.max_tables_total is not None
                    and tables_processed >= opts.max_tables_total
                ):
                    warnings.append(
                        f"Stopped: max_tables_total={opts.max_tables_total} reached."
                    )
                    break

                tname = tbl.name
                if not tname:
                    continue
                full_name = tbl.full_name or f"{cname}.{sname}.{tname}"
                if not _table_matches_allowlist(tbl, full_name, opts):
                    continue

                ds_urn = _dataset_urn(cname, sname, tname, env)
                tbl_id = _stable_key("tbl", env, full_name)

                table_info: TableInfo = tbl
                if opts.include_delta_metadata and (
                    delta_fetches < opts.delta_metadata_max_tables
                ):
                    fmt = _enum_str(tbl.data_source_format)
                    if fmt and "DELTA" in fmt.upper():
                        try:
                            table_info = w.tables.get(
                                full_name,
                                include_delta_metadata=True,
                            )
                            delta_fetches += 1
                        except Exception as exc:
                            warnings.append(
                                f"Delta metadata fetch failed for {full_name}: {exc}"
                            )

                metrics = _serialize_table_metrics(table_info)
                nodes.append(
                    {
                        "id": tbl_id,
                        "datahub_entity_type": "dataset",
                        "datahub_urn": ds_urn,
                        "labels": ["Table", "Dataset"],
                        "properties": {
                            "name": tname,
                            "full_name": full_name,
                            **metrics,
                        },
                    }
                )
                edges.append(
                    {
                        "id": _stable_key("e", sch_id, tbl_id, "HAS_DATASET"),
                        "relationship_type": "HAS_DATASET",
                        "from_id": sch_id,
                        "to_id": tbl_id,
                        "properties": {"note": "schema contains dataset (table/view)"},
                    }
                )

                if opts.include_columns and table_info.columns:
                    for col in table_info.columns:
                        cname_col = col.name
                        if not cname_col:
                            continue
                        field_urn = _schema_field_urn(ds_urn, cname_col)
                        col_id = _stable_key("col", env, full_name, cname_col)
                        nodes.append(
                            {
                                "id": col_id,
                                "datahub_entity_type": "schemaField",
                                "datahub_urn": field_urn,
                                "labels": ["Column", "SchemaField"],
                                "properties": _serialize_column(col),
                            }
                        )
                        edges.append(
                            {
                                "id": _stable_key("e", tbl_id, col_id, "HAS_SCHEMA_FIELD"),
                                "relationship_type": "HAS_SCHEMA_FIELD",
                                "from_id": tbl_id,
                                "to_id": col_id,
                                "properties": {},
                            }
                        )

                tables_processed += 1
                tid_m = _normalize_table_id(tbl.table_id)
                if tid_m:
                    matched_ids.add(tid_m)
                matched_names.add(_normalize_full_name(full_name))

                if (
                    not allowlist_on
                    and opts.max_tables_total is not None
                    and tables_processed >= opts.max_tables_total
                ):
                    warnings.append(
                        f"Stopped: max_tables_total={opts.max_tables_total} reached."
                    )
                    break

            if scan_exhausted:
                break
            if (
                not allowlist_on
                and opts.max_tables_total is not None
                and tables_processed >= opts.max_tables_total
            ):
                break

        if scan_exhausted:
            break
        if (
            not allowlist_on
            and opts.max_tables_total is not None
            and tables_processed >= opts.max_tables_total
        ):
            break

    if opts.table_ids_allowlist:
        missing_ids = sorted(opts.table_ids_allowlist - matched_ids)
        if missing_ids:
            preview = ", ".join(missing_ids[:15])
            more = f" (+{len(missing_ids) - 15} more)" if len(missing_ids) > 15 else ""
            warnings.append(
                f"table_ids not found after scan: {preview}{more}"
            )
    if opts.table_full_names_allowlist:
        missing_n = sorted(opts.table_full_names_allowlist - matched_names)
        if missing_n:
            preview = ", ".join(missing_n[:15])
            more = f" (+{len(missing_n) - 15} more)" if len(missing_n) > 15 else ""
            warnings.append(
                f"table_full_names not found after scan: {preview}{more}"
            )

    summary = {
        "node_count": len(nodes),
        "edge_count": len(edges),
        "tables_scanned": tables_processed,
        "tables_visited": tables_visited if allowlist_on else None,
        "delta_metadata_fetches": delta_fetches,
        "catalogs_iterated": catalog_count,
    }

    return {
        "status": "ok",
        "source": "unity-catalog",
        "datahub_source_doc": "https://docs.datahub.com/docs/generated/ingestion/sources/databricks/",
        "generated_at": _utc_iso(),
        "workspace_host": host,
        "platform": "databricks",
        "platform_instance_env": opts.platform_instance_env,
        "allowlist_active": allowlist_on,
        "options": {
            "catalogs_allowlist": opts.catalogs_allowlist,
            "schema_pattern_deny": opts.schema_pattern_deny,
            "max_catalogs": opts.max_catalogs,
            "max_schemas_per_catalog": opts.max_schemas_per_catalog,
            "max_tables_total": opts.max_tables_total,
            "max_table_scan_budget": opts.max_table_scan_budget,
            "table_ids_allowlist": sorted(opts.table_ids_allowlist)
            if opts.table_ids_allowlist
            else None,
            "table_full_names_allowlist": sorted(opts.table_full_names_allowlist)
            if opts.table_full_names_allowlist
            else None,
            "include_columns": opts.include_columns,
            "include_delta_metadata": opts.include_delta_metadata,
            "delta_metadata_max_tables": opts.delta_metadata_max_tables,
        },
        "summary": summary,
        "warnings": warnings,
        "graph": {
            "nodes": nodes,
            "edges": edges,
        },
    }


def options_from_request_payload(payload: dict[str, Any]) -> UnityCatalogGraphOptions:
    """Build options from POST JSON (e.g. Flask ``request.get_json()``)."""

    def _get_str(key: str, default: str | None = None) -> str | None:
        v = payload.get(key)
        if v is None:
            return default
        s = str(v).strip()
        return s if s else default

    catalogs_raw = payload.get("catalogs")
    allow: list[str] | None = None
    if isinstance(catalogs_raw, list):
        allow = [str(x).strip() for x in catalogs_raw if str(x).strip()]
        if not allow:
            allow = None

    deny = payload.get("schema_pattern_deny")
    if deny is None:
        deny_list = ["information_schema"]
    elif isinstance(deny, list):
        deny_list = [str(x) for x in deny]
    else:
        deny_list = ["information_schema"]

    max_cat = payload.get("max_catalogs")
    max_sch = payload.get("max_schemas_per_catalog")
    max_tbl = payload.get("max_tables_total")
    env = _get_str("platform_instance_env", "PROD") or "PROD"

    max_tables_parsed: int | None
    if max_tbl is None:
        max_tables_parsed = 5000
    else:
        try:
            mt = int(max_tbl)
        except (TypeError, ValueError):
            max_tables_parsed = 5000
        else:
            max_tables_parsed = None if mt < 0 else mt

    msb_raw = payload.get("max_table_scan_budget")
    max_scan: int | None
    if msb_raw is None:
        max_scan = None
    else:
        try:
            mv = int(msb_raw)
        except (TypeError, ValueError):
            max_scan = None
        else:
            max_scan = None if mv < 0 else mv

    tid_raw = payload.get("table_ids")
    table_ids_allow: frozenset[str] | None = None
    if isinstance(tid_raw, list):
        _ids = {_normalize_table_id(x) for x in tid_raw if _normalize_table_id(x)}
        table_ids_allow = frozenset(_ids) if _ids else None

    tfn_raw = payload.get("table_full_names")
    table_names_allow: frozenset[str] | None = None
    if isinstance(tfn_raw, list):
        _names = {_normalize_full_name(str(x)) for x in tfn_raw if str(x).strip()}
        table_names_allow = frozenset(_names) if _names else None

    return UnityCatalogGraphOptions(
        catalogs_allowlist=allow,
        schema_pattern_deny=deny_list,
        platform_instance_env=env,
        max_catalogs=int(max_cat) if max_cat is not None else None,
        max_schemas_per_catalog=int(max_sch) if max_sch is not None else None,
        max_tables_total=max_tables_parsed,
        max_table_scan_budget=max_scan,
        table_ids_allowlist=table_ids_allow,
        table_full_names_allowlist=table_names_allow,
        include_columns=bool(payload.get("include_columns", True)),
        include_delta_metadata=bool(payload.get("include_delta_metadata", False)),
        delta_metadata_max_tables=int(
            payload.get("delta_metadata_max_tables", 100) or 100
        ),
    )


def discover_uc_tables(
    *,
    options: UnityCatalogGraphOptions | None = None,
    workspace_client: WorkspaceClient | None = None,
) -> dict[str, Any]:
    """
    List UC tables (table_id + full_name + type) for UI pickers.

    Respects ``catalogs_allowlist``, ``schema_pattern_deny``, and ``max_tables_total``
    as a cap on how many tables to return. Ignores ``table_ids`` / ``table_full_names``.
    """
    opts = options or UnityCatalogGraphOptions()
    w = workspace_client or WorkspaceClient()
    host = getattr(w.config, "host", None) or ""

    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    count = 0
    list_cap_done = False

    allow = opts.catalogs_allowlist
    if allow is not None:
        allow = [c.strip() for c in allow if c and str(c).strip()]
        if not allow:
            allow = None

    catalog_count = 0
    for cat in w.catalogs.list(max_results=0):
        if list_cap_done:
            break
        cname = cat.name
        if not cname:
            continue
        if allow is not None and cname not in allow:
            continue
        if opts.max_catalogs is not None and catalog_count >= opts.max_catalogs:
            warnings.append(
                f"Stopped after max_catalogs={opts.max_catalogs}; remaining catalogs skipped."
            )
            break
        catalog_count += 1

        schema_num = 0
        for sch in w.schemas.list(catalog_name=cname, max_results=0):
            if list_cap_done:
                break
            sname = sch.name
            if not sname:
                continue
            if opts.schema_pattern_deny and _schema_name_matched(
                opts.schema_pattern_deny, cname, sname
            ):
                continue
            if (
                opts.max_schemas_per_catalog is not None
                and schema_num >= opts.max_schemas_per_catalog
            ):
                warnings.append(
                    f"Catalog {cname!r}: stopped after max_schemas_per_catalog="
                    f"{opts.max_schemas_per_catalog}."
                )
                break
            schema_num += 1

            for tbl in w.tables.list(
                catalog_name=cname,
                schema_name=sname,
                max_results=0,
                omit_columns=True,
                omit_properties=False,
            ):
                if opts.max_tables_total is not None and count >= opts.max_tables_total:
                    warnings.append(
                        f"Stopped: max_tables_total={opts.max_tables_total} listing cap."
                    )
                    list_cap_done = True
                    break

                tname = tbl.name
                if not tname:
                    continue
                full_name = tbl.full_name or f"{cname}.{sname}.{tname}"
                rows.append(
                    {
                        "table_id": tbl.table_id,
                        "full_name": full_name,
                        "catalog": cname,
                        "schema": sname,
                        "name": tname,
                        "table_type": _enum_str(tbl.table_type),
                        "data_source_format": _enum_str(tbl.data_source_format),
                    }
                )
                count += 1

            if list_cap_done:
                break

        if list_cap_done:
            break

    return {
        "status": "ok",
        "source": "unity-catalog-discovery",
        "generated_at": _utc_iso(),
        "workspace_host": host,
        "summary": {
            "table_count": len(rows),
            "catalogs_iterated": catalog_count,
        },
        "warnings": warnings,
        "tables": rows,
    }


def discovery_options_from_request_payload(payload: dict[str, Any]) -> UnityCatalogGraphOptions:
    """Options for :func:`discover_uc_tables` (strips table allowlists)."""
    filtered = {k: v for k, v in payload.items() if k not in ("table_ids", "table_full_names")}
    if filtered.get("max_tables_total") is None:
        filtered = {**filtered, "max_tables_total": 10_000}
    return options_from_request_payload(filtered)
