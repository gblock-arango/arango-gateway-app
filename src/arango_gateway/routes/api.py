"""API blueprint definitions."""

import json
import os
import tempfile

from databricks.sdk import WorkspaceClient
from flask import Blueprint, Response, current_app, jsonify, request, stream_with_context
from werkzeug.utils import secure_filename

from arango_gateway.services.arango_registry import (
    build_arango_web_ui_base_url,
    ensure_registry_table,
    get_active_registry_entry,
    get_active_registry_row,
    upsert_registry_entry,
)
from arango_gateway.services.arango_http import arango_json_request, ping_arango_endpoint
from arango_gateway.services.arango_http_batch import execute_arango_http_batch
from arango_gateway.services.arango_proxy_path import arango_http_proxy_path_allowed
from arango_gateway.services.arango_uc_graph_import import (
    import_uc_graph_to_arango,
    iter_uc_graph_import_events,
)
from arango_gateway.services.datahub_unity_catalog_workflow import (
    discover_uc_tables,
    discovery_options_from_request_payload,
    extract_unity_catalog_graph,
    options_from_request_payload,
)
from arango_gateway.services.startup_debug import run_startup_debug_check
from arango_gateway.services.uc_graph_jsonl_bundle import (
    build_local_gzip_jsonl_bundle,
    cleanup_local_bundle,
    parse_jsonl_export_config,
    publish_local_bundle_to_uc_volume,
    resolve_run_id,
)
from arango_gateway.services.arango_conversation import ask_arango_conversation
from arango_gateway.services.arango_basic_auth import resolve_arango_basic_auth

api_blueprint = Blueprint("api", __name__)

_ARANGO_HTTP_PROXY_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})

# Temp uploads for "Add Your Documents" until UC volume / ML pipeline wiring exists.
_DATABRICKS_GRAPH_UPLOAD_SUBDIR = "arango_dashboard_uploads"


def _arango_basic_auth(*, registry_row: dict | None = None) -> tuple[str, str]:
    user, password, _meta = resolve_arango_basic_auth(
        current_app.config,
        registry_row=registry_row,
    )
    return user, password


def _ensure_registry_if_configured(table_name: str, warehouse_id: str) -> None:
    if current_app.config.get("ARANGO_REGISTRY_AUTO_CREATE", True):
        ensure_registry_table(table_name=table_name, warehouse_id=warehouse_id)


def _looks_like_missing_uc_volume(message: str) -> bool:
    """Heuristic for Databricks UC errors when the volume path is wrong or missing."""
    m = (message or "").lower()
    if "does not exist" not in m:
        return False
    return "volume" in m or "volumes" in m


def _uc_volume_create_sql_hint() -> str:
    """One-line SQL to create the configured UC volume (catalog.schema from registry table)."""
    table = str(current_app.config.get("ARANGO_REGISTRY_TABLE") or "").strip()
    vol = str(
        current_app.config.get("UC_GRAPH_SNAPSHOT_VOLUME_NAME")
        or current_app.config.get("UC_GRAPH_VOLUME_NAME")
        or ""
    ).strip()
    if not vol:
        vol = "arango_agent_volume"
    parts = table.split(".")
    if len(parts) >= 3:
        return f"CREATE VOLUME IF NOT EXISTS {parts[0]}.{parts[1]}.{vol};"
    return (
        f"CREATE VOLUME IF NOT EXISTS <catalog>.<schema>.{vol}; "
        "(set ARANGO_REGISTRY_TABLE to catalog.schema.table)"
    )


def _registry_arango_base_url() -> tuple[str | None, dict | None]:
    """HTTP origin for Arango API from active UC registry row, or (None, None)."""
    table_name = current_app.config["ARANGO_REGISTRY_TABLE"]
    warehouse_id = current_app.config["DATABRICKS_SQL_WAREHOUSE_ID"]
    if not table_name or not warehouse_id:
        return None, None
    try:
        _ensure_registry_if_configured(table_name=table_name, warehouse_id=warehouse_id)
        row = get_active_registry_row(table_name=table_name, warehouse_id=warehouse_id)
        if not row:
            return None, None
        base = build_arango_web_ui_base_url(
            str(row.get("protocol", "")),
            str(row.get("ip_address", "")),
            row.get("port", ""),
        )
        if not base:
            return None, row
        return base, row
    except Exception:
        return None, None


def _apply_jsonl_export(payload: dict, result: dict) -> None:
    """Mutates ``result`` with ``jsonl_export`` and may set ``graph`` to None."""
    export_cfg = parse_jsonl_export_config(
        payload,
        default_volume_base=str(
            current_app.config.get("UC_GRAPH_SNAPSHOT_BASE") or ""
        ),
    )
    if not export_cfg:
        return
    run_id = resolve_run_id(
        str(export_cfg["run_id"]).strip() if export_cfg.get("run_id") else None
    )
    bundle = build_local_gzip_jsonl_bundle(
        graph_result=result,
        run_id=run_id,
        workspace_host=str(result.get("workspace_host") or ""),
    )
    try:
        try:
            export_result = publish_local_bundle_to_uc_volume(
                workspace_client=WorkspaceClient(),
                volume_base_path=str(export_cfg["volume_base_path"]),
                run_id=run_id,
                bundle=bundle,
                use_staging_directory=bool(
                    export_cfg.get("use_staging_directory", False)
                ),
            )
        except Exception as exc:
            msg = str(exc)
            if _looks_like_missing_uc_volume(msg):
                hint_sql = _uc_volume_create_sql_hint()
                warn = (
                    "JSONL export to Unity Catalog volume failed (volume missing or no access): "
                    f"{msg}. Create the volume and grant the app identity access, e.g. {hint_sql}"
                )
                w = result.get("warnings")
                if isinstance(w, list):
                    w.append(warn)
                else:
                    result["warnings"] = [warn]
                result["jsonl_export"] = {
                    "skipped": True,
                    "reason": "volume_missing_or_inaccessible",
                    "error": msg,
                    "create_volume_sql_hint": hint_sql,
                    "attempted_volume_base_path": str(export_cfg["volume_base_path"]),
                }
                return
            raise
    finally:
        cleanup_local_bundle(bundle)
    result["jsonl_export"] = export_result
    if not export_cfg.get("include_graph_in_response", False):
        result["graph"] = None
        result["graph_omitted"] = True
        result["graph_omitted_reason"] = (
            "Graph is only in UC volume JSONL; set jsonl_export.include_graph_in_response "
            "to true to include inline graph (not recommended for large catalogs)."
        )


def _snapshot_graph_documents(result: dict) -> tuple[list[dict], list[dict]]:
    """Copy nodes/edges before ``graph`` may be cleared by export."""
    graph = result.get("graph") or {}
    nodes = list(graph.get("nodes") or [])
    edges = list(graph.get("edges") or [])
    return nodes, edges


def _run_arango_import(
    payload: dict, nodes: list[dict], edges: list[dict]
) -> dict[str, object]:
    if payload.get("arango_import") is False:
        return {"skipped": True, "reason": "disabled_by_request"}
    base, row = _registry_arango_base_url()
    if not base:
        return {
            "skipped": True,
            "reason": "no_active_registry_row_or_invalid_endpoint",
        }
    auth_user, auth_password = _arango_basic_auth(registry_row=row)
    verify_tls = bool(current_app.config.get("ARANGO_PING_TLS_VERIFY", True))
    db = str(current_app.config.get("ARANGO_DATABASE") or "_system").strip() or "_system"
    batch = int(current_app.config.get("ARANGO_UC_IMPORT_BATCH_SIZE") or 300)
    summary = import_uc_graph_to_arango(
        base_url=base,
        database=db,
        nodes=nodes,
        edges=edges,
        batch_size=batch,
        basic_auth_user=auth_user or None,
        basic_auth_password=auth_password if auth_user else None,
        verify_tls=verify_tls,
        timeout_seconds=120.0,
    )
    if row and isinstance(summary, dict):
        summary = dict(summary)
        summary["registry_cluster_name"] = row.get("cluster_name")
    return summary


def _extract_schema_execute(payload: dict) -> dict:
    """Full extract: UC graph, optional JSONL export, Arango bulk load."""
    opts = options_from_request_payload(payload)
    result = extract_unity_catalog_graph(options=opts)
    nodes, edges = _snapshot_graph_documents(result)
    _apply_jsonl_export(payload, result)
    result["arango_import"] = _run_arango_import(payload, nodes, edges)
    return result


def _iter_extract_schema_ndjson(payload: dict):
    """NDJSON lines with manifest + Arango progress for the dashboard."""
    opts = options_from_request_payload(payload)
    result = extract_unity_catalog_graph(options=opts)
    nodes, edges = _snapshot_graph_documents(result)
    _apply_jsonl_export(payload, result)
    total_docs = len(nodes) + len(edges)
    yield json.dumps(
        {
            "event": "manifest_ready",
            "node_count": len(nodes),
            "edge_count": len(edges),
            "total_documents": total_docs,
        }
    )

    if payload.get("arango_import") is False:
        result["arango_import"] = {
            "skipped": True,
            "reason": "disabled_by_request",
        }
        yield json.dumps({"event": "done", "result": result})
        return

    base, row = _registry_arango_base_url()
    if not base:
        result["arango_import"] = {
            "skipped": True,
            "reason": "no_active_registry_row_or_invalid_endpoint",
        }
        yield json.dumps({"event": "done", "result": result})
        return

    auth_user, auth_password = _arango_basic_auth(registry_row=row)
    verify_tls = bool(current_app.config.get("ARANGO_PING_TLS_VERIFY", True))
    db = str(current_app.config.get("ARANGO_DATABASE") or "_system").strip() or "_system"
    batch = int(current_app.config.get("ARANGO_UC_IMPORT_BATCH_SIZE") or 300)

    arango_summary: dict[str, object] | None = None
    for ev in iter_uc_graph_import_events(
        base_url=base,
        database=db,
        nodes=nodes,
        edges=edges,
        batch_size=batch,
        basic_auth_user=auth_user or None,
        basic_auth_password=auth_password if auth_user else None,
        verify_tls=verify_tls,
        timeout_seconds=120.0,
    ):
        k = ev.get("kind")
        if k == "progress":
            posted = int(ev["posted"])
            total = int(ev["total"])
            if total > 0:
                pct = 20.0 + (80.0 * posted) / total
            else:
                pct = 100.0
            yield json.dumps(
                {
                    "event": "arango_progress",
                    "posted": posted,
                    "total": total,
                    "pct": round(pct, 1),
                }
            )
        elif k == "error":
            err_body = ev.get("detail") or {}
            if isinstance(err_body, dict):
                msg = err_body.get("body", {})
                if isinstance(msg, dict) and msg.get("errorMessage"):
                    err_text = str(msg.get("errorMessage"))
                else:
                    err_text = str(err_body.get("error") or err_body)
            else:
                err_text = str(err_body)
            result["arango_import"] = {
                "ok": False,
                "step": ev.get("step"),
                "detail": ev.get("detail"),
            }
            yield json.dumps({"event": "error", "error": err_text, "result": result})
            return
        elif k == "complete":
            arango_summary = ev.get("result") or {}
            if row and isinstance(arango_summary, dict):
                arango_summary = dict(arango_summary)
                arango_summary["registry_cluster_name"] = row.get("cluster_name")

    if arango_summary is not None:
        result["arango_import"] = arango_summary
    yield json.dumps({"event": "done", "result": result})


@api_blueprint.get("/health")
def health():
    return jsonify({"status": "ok"})


@api_blueprint.post("/arango/chat")
def arango_chat():
    """
    Dashboard Arango mode: same JSON body as Genie chat (``content`` / ``message``,
    optional ``conversation_id``). Delegates to :mod:`arango_gateway.services.arango_conversation`.
    """
    payload = request.get_json(silent=True) or {}
    content = str(
        payload.get("content") or payload.get("message") or ""
    ).strip()
    if not content:
        return jsonify({"ok": False, "error": "content or message is required"}), 400

    conversation_id = payload.get("conversation_id")
    if conversation_id is not None:
        conversation_id = str(conversation_id).strip() or None

    result = ask_arango_conversation(
        content=content,
        conversation_id=conversation_id,
        config=current_app.config,
    )
    status = 200 if result.get("ok") else 502
    return jsonify(result), status


@api_blueprint.get("/arango/registry")
def get_arango_registry_config():
    """Reads latest active connectivity registry row from Unity Catalog table."""
    table_name = current_app.config["ARANGO_REGISTRY_TABLE"]
    warehouse_id = current_app.config["DATABRICKS_SQL_WAREHOUSE_ID"]

    try:
        _ensure_registry_if_configured(table_name=table_name, warehouse_id=warehouse_id)
        rows = get_active_registry_entry(table_name=table_name, warehouse_id=warehouse_id)
        return jsonify(rows)
    except Exception as exc:
        return jsonify(
            {
                "columns": [],
                "rows": [],
                "warning": f"Unable to read registry table '{table_name}': {exc}",
            }
        ), 200


@api_blueprint.post("/arango/registry/init")
def init_arango_registry_table():
    """Initialize registry schema/table if missing."""
    table_name = current_app.config["ARANGO_REGISTRY_TABLE"]
    warehouse_id = current_app.config["DATABRICKS_SQL_WAREHOUSE_ID"]

    try:
        ensure_registry_table(table_name=table_name, warehouse_id=warehouse_id)
        return jsonify(
            {
                "status": "ok",
                "message": "Registry table is ready",
                "table": table_name,
            }
        )
    except Exception as exc:
        return jsonify({"status": "error", "table": table_name, "error": str(exc)}), 500


@api_blueprint.post("/arango/registry")
def put_arango_registry_config():
    """Upsert active Arango endpoint in Unity Catalog registry table."""
    payload = request.get_json(silent=True) or {}

    cluster_name = str(payload.get("cluster_name", "default")).strip()
    ip_address = str(payload.get("ip_address", "")).strip()
    protocol = str(payload.get("protocol", "http")).strip().lower()
    port = payload.get("port")

    if not ip_address:
        return jsonify({"status": "error", "error": "ip_address is required"}), 400
    if protocol not in {"http", "https"}:
        return jsonify({"status": "error", "error": "protocol must be http or https"}), 400
    try:
        port = int(port)
    except (TypeError, ValueError):
        return jsonify({"status": "error", "error": "port must be an integer"}), 400
    if port < 1 or port > 65535:
        return jsonify({"status": "error", "error": "port must be 1-65535"}), 400

    table_name = current_app.config["ARANGO_REGISTRY_TABLE"]
    warehouse_id = current_app.config["DATABRICKS_SQL_WAREHOUSE_ID"]

    try:
        _ensure_registry_if_configured(table_name=table_name, warehouse_id=warehouse_id)
        upsert_registry_entry(
            table_name=table_name,
            warehouse_id=warehouse_id,
            cluster_name=cluster_name,
            ip_address=ip_address,
            port=port,
            protocol=protocol,
        )
        return jsonify({"status": "ok", "table": table_name})
    except Exception as exc:
        return jsonify({"status": "error", "table": table_name, "error": str(exc)}), 500


@api_blueprint.post("/arango/ping")
def ping_arango_from_registry():
    """Probe outbound network reachability to active Arango endpoint."""
    payload = request.get_json(silent=True) or {}
    path = str(payload.get("path", "/_api/version")).strip() or "/_api/version"

    table_name = current_app.config["ARANGO_REGISTRY_TABLE"]
    warehouse_id = current_app.config["DATABRICKS_SQL_WAREHOUSE_ID"]
    timeout_seconds = float(current_app.config.get("ARANGO_PING_TIMEOUT_SECONDS", 5.0))

    try:
        _ensure_registry_if_configured(table_name=table_name, warehouse_id=warehouse_id)
        row = get_active_registry_row(table_name=table_name, warehouse_id=warehouse_id)
        if not row:
            return jsonify(
                {
                    "status": "error",
                    "error": "No active Arango registry row found. Set one with POST /api/arango/registry first.",
                    "table": table_name,
                }
            ), 404

        auth_user, auth_password = _arango_basic_auth(registry_row=row)
        verify_tls = bool(current_app.config.get("ARANGO_PING_TLS_VERIFY", True))

        probe = ping_arango_endpoint(
            protocol=str(row["protocol"]).lower(),
            ip_address=str(row["ip_address"]),
            port=int(row["port"]),
            path=path,
            timeout_seconds=timeout_seconds,
            basic_auth_user=auth_user or None,
            basic_auth_password=auth_password if auth_user else None,
            verify_tls=verify_tls,
        )

        return jsonify(
            {
                "status": "ok" if probe.get("reachable") else "unreachable",
                "cluster_name": row.get("cluster_name"),
                "registry": {
                    "ip_address": row.get("ip_address"),
                    "port": row.get("port"),
                    "protocol": row.get("protocol"),
                },
                "probe": probe,
                "ping_auth": "basic" if auth_user else "none",
            }
        )
    except Exception as exc:
        return jsonify({"status": "error", "table": table_name, "error": str(exc)}), 500


@api_blueprint.post("/arango/http")
def arango_http_proxy():
    """Forward JSON REST to Arango (active UC registry + gateway Basic auth).

    Body: ``{"method": "GET|POST|...", "path": "/_db/.../_api/...", "body": optional}``.
    Intended for the Arango MCP server in ``arango-mcp-app`` and other workspace agents that cannot
    reach Arango directly.
    """
    payload = request.get_json(silent=True) or {}
    method = str(payload.get("method", "GET")).upper().strip()
    raw_path = str(payload.get("path", "")).strip()
    body = payload.get("body")
    if body is None and "json" in payload:
        body = payload.get("json")

    if method not in _ARANGO_HTTP_PROXY_METHODS:
        return jsonify({"ok": False, "error": f"unsupported method: {method}"}), 400

    path = raw_path if raw_path.startswith("/") else f"/{raw_path}"
    allow_admin = bool(current_app.config.get("ARANGO_HTTP_PROXY_ALLOW_ADMIN", False))
    if not arango_http_proxy_path_allowed(path, allow_admin=allow_admin):
        return jsonify(
            {
                "ok": False,
                "error": "path not allowed for Arango HTTP proxy",
                "path": path,
            }
        ), 403

    table_name = current_app.config["ARANGO_REGISTRY_TABLE"]
    warehouse_id = current_app.config["DATABRICKS_SQL_WAREHOUSE_ID"]
    timeout_seconds = float(
        current_app.config.get("ARANGO_HTTP_PROXY_TIMEOUT_SECONDS", 120.0)
    )

    try:
        _ensure_registry_if_configured(table_name=table_name, warehouse_id=warehouse_id)
        row = get_active_registry_row(table_name=table_name, warehouse_id=warehouse_id)
        if not row:
            return jsonify(
                {
                    "ok": False,
                    "error": "No active Arango registry row found.",
                    "table": table_name,
                }
            ), 404

        base = build_arango_web_ui_base_url(
            str(row.get("protocol", "")),
            str(row.get("ip_address", "")),
            row.get("port", ""),
        )
        if not base:
            return jsonify({"ok": False, "error": "invalid registry coordinates"}), 500

        auth_user, auth_password = _arango_basic_auth(registry_row=row)
        verify_tls = bool(current_app.config.get("ARANGO_PING_TLS_VERIFY", True))

        result = arango_json_request(
            method=method,
            base_url=base,
            path=path,
            payload=None if method == "GET" else body,
            basic_auth_user=auth_user or None,
            basic_auth_password=auth_password if auth_user else None,
            verify_tls=verify_tls,
            timeout_seconds=timeout_seconds,
        )
        return jsonify(result)
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "body": {}}), 500


@api_blueprint.post("/arango/http/batch")
def arango_http_proxy_batch():
    """Run many Arango REST calls with one UC registry lookup (schema bootstrap).

    Body::

        {
          "requests": [
            {"method": "POST", "path": "/_db/mydb/_api/collection", "body": {"name": "documents", "type": 2}},
            ...
          ],
          "parallel": true,
          "max_workers": 8,
          "stop_on_error": false
        }
    """
    payload = request.get_json(silent=True) or {}
    raw_requests = payload.get("requests")
    if not isinstance(raw_requests, list):
        return jsonify({"ok": False, "error": "requests must be a JSON array"}), 400

    table_name = current_app.config["ARANGO_REGISTRY_TABLE"]
    warehouse_id = current_app.config["DATABRICKS_SQL_WAREHOUSE_ID"]
    timeout_seconds = float(
        current_app.config.get("ARANGO_HTTP_PROXY_TIMEOUT_SECONDS", 120.0)
    )
    allow_admin = bool(current_app.config.get("ARANGO_HTTP_PROXY_ALLOW_ADMIN", False))
    parallel = str(payload.get("parallel", True)).strip().lower() not in ("0", "false", "no")
    try:
        max_workers = int(payload.get("max_workers", 8))
    except (TypeError, ValueError):
        max_workers = 8
    stop_on_error = str(payload.get("stop_on_error", False)).strip().lower() in (
        "1",
        "true",
        "yes",
    )

    try:
        _ensure_registry_if_configured(table_name=table_name, warehouse_id=warehouse_id)
        row = get_active_registry_row(table_name=table_name, warehouse_id=warehouse_id)
        if not row:
            return jsonify(
                {
                    "ok": False,
                    "error": "No active Arango registry row found.",
                    "table": table_name,
                }
            ), 404

        base = build_arango_web_ui_base_url(
            str(row.get("protocol", "")),
            str(row.get("ip_address", "")),
            row.get("port", ""),
        )
        if not base:
            return jsonify({"ok": False, "error": "invalid registry coordinates"}), 500

        auth_user, auth_password = _arango_basic_auth(registry_row=row)
        verify_tls = bool(current_app.config.get("ARANGO_PING_TLS_VERIFY", True))

        result = execute_arango_http_batch(
            base_url=base,
            requests=raw_requests,
            basic_auth_user=auth_user or None,
            basic_auth_password=auth_password if auth_user else None,
            verify_tls=verify_tls,
            timeout_seconds=timeout_seconds,
            allow_admin=allow_admin,
            parallel=parallel,
            max_workers=max_workers,
            stop_on_error=stop_on_error,
        )
        status = 200 if result.get("ok") else 502
        return jsonify(result), status
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc), "body": {}}), 500


@api_blueprint.get("/debug/startup-status")
def startup_status():
    """Return latest startup diagnostics; optionally refresh on demand."""
    refresh = str(request.args.get("refresh", "false")).lower() == "true"
    if refresh:
        current_app.extensions["startup_debug_status"] = run_startup_debug_check(
            current_app
        )
    return jsonify(current_app.extensions.get("startup_debug_status", {}))


@api_blueprint.post("/databricks-graph/uc-tables")
def databricks_graph_uc_tables():
    """
    List Unity Catalog tables (table_id, full_name, type) for dashboard multiselect.

    JSON body uses the same discovery options as extract-schema except
    ``table_ids`` / ``table_full_names`` are ignored. Default ``max_tables_total``
    for this route is 10_000 unless overridden.
    """
    payload = request.get_json(silent=True) or {}
    try:
        opts = discovery_options_from_request_payload(payload)
        return jsonify(discover_uc_tables(options=opts))
    except Exception as exc:
        return jsonify({"status": "error", "error": str(exc)}), 500


@api_blueprint.post("/databricks-graph/extract-schema")
def databricks_graph_extract_schema():
    """
    Extract Unity Catalog metadata into a DataHub-aligned graph (nodes/edges/URNs).

    Optional JSON body (see ``options_from_request_payload``): catalogs allowlist,
    schema deny patterns, caps, ``include_delta_metadata``,
    ``table_ids`` and/or ``table_full_names`` (union allowlist; normalized lowercase),
    ``max_table_scan_budget`` when allowlisting, etc.

    Optional ``jsonl_export``: write gzip JSONL + ``manifest.json`` under
    ``<volume_base>/runs/<run_id>/snapshot/`` on a Unity Catalog volume (see
    ``uc_graph_jsonl_bundle``). Set env ``UC_GRAPH_SNAPSHOT_BASE`` as default
    volume path. Unless ``jsonl_export.include_graph_in_response`` is true, the
    inline ``graph`` is omitted when export succeeds (large payloads / network).

    With ``stream_progress: true``, responds with ``application/x-ndjson``: first
    ``manifest_ready`` (use ~20% in UI), then ``arango_progress`` with ``pct``
    from 20–100 as documents are written, then ``done`` with the same payload as
    the non-streaming JSON response. Set ``arango_import: false`` to skip loading
    into the cluster from the active registry row.
    """
    payload = request.get_json(silent=True) or {}
    if payload.get("stream_progress"):
        @stream_with_context
        def ndjson_stream():
            try:
                for line in _iter_extract_schema_ndjson(payload):
                    yield line + "\n"
            except Exception as exc:
                yield json.dumps(
                    {
                        "event": "error",
                        "error": str(exc),
                        "hint": "Ensure the app identity can USE_CATALOG / USE_SCHEMA / read table "
                        "metadata (see DataHub Databricks source prerequisites).",
                    }
                ) + "\n"

        return Response(
            ndjson_stream(),
            mimetype="application/x-ndjson",
            headers={"X-Accel-Buffering": "no"},
        )

    try:
        result = _extract_schema_execute(payload)
        return jsonify(result)
    except Exception as exc:
        return jsonify(
            {
                "status": "error",
                "error": str(exc),
                "hint": "Ensure the app identity can USE_CATALOG / USE_SCHEMA / read table "
                "metadata (see DataHub Databricks source prerequisites).",
                "datahub_source_doc": "https://docs.datahub.com/docs/generated/ingestion/sources/databricks/",
            }
        ), 500


@api_blueprint.post("/databricks-graph/build-corpus-graphs")
def databricks_graph_build_corpus_graphs():
    """Placeholder: Arango AutoGraph automated extractor service (next)."""
    payload = request.get_json(silent=True) or {}
    return jsonify(
        {
            "status": "accepted",
            "message": "Build corpus graphs: Arango AutoGraph extractor service will be wired next.",
            "echo": payload,
        }
    )


@api_blueprint.post("/databricks-graph/documents")
def databricks_graph_upload_documents():
    """
    Accept PDF, TXT, or MD uploads into a process temp directory.

    Next: persist to UC volumes and run Databricks ML on extracted text.
    """
    allowed_ext = {".pdf", ".txt", ".md"}
    files = request.files.getlist("files")
    if not files or all(not f or f.filename in ("", None) for f in files):
        return jsonify({"status": "error", "error": "No files received"}), 400

    upload_root = os.path.join(
        tempfile.gettempdir(), _DATABRICKS_GRAPH_UPLOAD_SUBDIR
    )
    os.makedirs(upload_root, exist_ok=True)

    saved: list[dict[str, str]] = []
    for f in files:
        if not f or not f.filename:
            continue
        ext = os.path.splitext(f.filename)[1].lower()
        if ext not in allowed_ext:
            return jsonify(
                {
                    "status": "error",
                    "error": f"Unsupported file type {ext!r}; allowed: {sorted(allowed_ext)}",
                }
            ), 400
        safe_name = secure_filename(f.filename)
        if not safe_name:
            return jsonify({"status": "error", "error": "Invalid filename"}), 400
        dest = os.path.join(upload_root, safe_name)
        f.save(dest)
        saved.append({"name": safe_name, "path": dest})

    if not saved:
        return jsonify({"status": "error", "error": "No files saved"}), 400

    return jsonify(
        {
            "status": "ok",
            "saved": saved,
            "upload_dir": upload_root,
            "message": "Files stored in server temp workspace; ML pipeline wiring is next.",
        }
    )
