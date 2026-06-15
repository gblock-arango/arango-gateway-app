"""Unity Catalog-backed registry for Arango cluster connectivity."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit, urlunsplit

from .databricks_sql import execute_sql

logger = logging.getLogger(__name__)

# Cache active registry row — arango_http_proxy previously ran UC SQL on every hop.
_REGISTRY_CACHE_TTL_SEC = float(os.environ.get("ARANGO_REGISTRY_CACHE_TTL_SECONDS", "60"))
_registry_cache: dict[str, object] = {"at": 0.0, "key": "", "row": None}


def invalidate_active_registry_cache() -> None:
    """Force the next lookup to re-query UC (Connect / registry upsert)."""
    _registry_cache["at"] = 0.0
    _registry_cache["key"] = ""
    _registry_cache["row"] = None

# Web UI entry (Aardvark). Iframe must target this path, not the server root.
ARANGO_AARDVARK_PATH = "/_db/_system/_admin/aardvark/index.html"
ARANGO_AARDVARK_FRAGMENT = "login"


@dataclass
class RegistryTableRef:
    catalog: str
    schema: str
    table: str

    @property
    def fqn(self) -> str:
        return f"`{self.catalog}`.`{self.schema}`.`{self.table}`"


def parse_registry_table_name(table_name: str) -> RegistryTableRef:
    """
    Parse <catalog>.<schema>.<table> table name from config.

    Keeping strict 3-part naming avoids accidental writes to wrong objects.
    """
    parts = table_name.split(".")
    if len(parts) != 3 or any(not p.strip() for p in parts):
        raise ValueError(
            "ARANGO_REGISTRY_TABLE must be fully qualified as catalog.schema.table"
        )
    return RegistryTableRef(catalog=parts[0], schema=parts[1], table=parts[2])


def try_grant_account_users_registry_dml(ref: RegistryTableRef, warehouse_id: str) -> None:
    """
    Allow non-owner identities (e.g. the human running ``deploy_app.sh``, or anyone
    inspecting the registry from the Databricks UI) to SELECT/UPDATE the registry.

    The gateway app commonly creates this table first (DEBUG_STARTUP_CHECKS), making the
    app service principal the owner; without a follow-on grant, humans get
    ``Requires permission SELECT on table ...`` in the workspace UI.

    Uses the account-level ``account users`` principal. Tighten in production if your
    security model disallows it.
    """
    try:
        execute_sql(
            statement=f"GRANT SELECT, MODIFY ON TABLE {ref.fqn} TO `account users`",
            warehouse_id=warehouse_id,
        )
    except Exception as exc:
        logger.info(
            "Could not GRANT %s to `account users` (may be disabled or not owner): %s",
            ref.fqn,
            exc,
        )


def ensure_registry_table(table_name: str, warehouse_id: str) -> None:
    """Create schema/table for registry if they do not exist."""
    ref = parse_registry_table_name(table_name)

    execute_sql(
        statement=f"CREATE SCHEMA IF NOT EXISTS `{ref.catalog}`.`{ref.schema}`",
        warehouse_id=warehouse_id,
    )

    execute_sql(
        statement=f"""
            CREATE TABLE IF NOT EXISTS {ref.fqn} (
                cluster_name STRING NOT NULL,
                ip_address STRING NOT NULL,
                port INT NOT NULL,
                protocol STRING NOT NULL,
                is_active BOOLEAN NOT NULL,
                updated_at TIMESTAMP NOT NULL
            )
            USING DELTA
        """,
        warehouse_id=warehouse_id,
    )
    try_grant_account_users_registry_dml(ref, warehouse_id)


def get_active_registry_entry(table_name: str, warehouse_id: str) -> dict:
    """
    Read the newest registry row that is marked active.

    Only rows with ``is_active`` true are returned; inactive/historical rows are ignored.
    """
    ref = parse_registry_table_name(table_name)
    return execute_sql(
        statement=f"""
            SELECT cluster_name, ip_address, port, protocol, is_active, updated_at
            FROM {ref.fqn}
            WHERE is_active IS TRUE
            ORDER BY updated_at DESC
            LIMIT 1
        """,
        warehouse_id=warehouse_id,
    )


def build_arango_web_ui_base_url(protocol: str, ip_address: str, port: int | str) -> str:
    """
    Build the origin URL for the Arango web UI from registry-style fields.

    Omits the port in the URL when it matches the scheme default (80 / 443).
    """
    p = (protocol or "https").strip().lower()
    if p not in ("http", "https"):
        p = "https"
    host = (ip_address or "").strip()
    if not host:
        return ""
    try:
        port_n = int(port)
    except (TypeError, ValueError):
        return ""
    if port_n < 1 or port_n > 65535:
        return ""
    default_port = 443 if p == "https" else 80
    if port_n == default_port:
        return f"{p}://{host}"
    return f"{p}://{host}:{port_n}"


def append_arango_aardvark_entry(url: str) -> str:
    """
    Ensure the URL opens the Aardvark web UI (not ``/``).

    If the URL already targets Aardvark, it is returned unchanged.
    """
    s = (url or "").strip()
    if not s:
        return ""
    parts = urlsplit(s)
    path = parts.path or ""
    if "_admin/aardvark" in path:
        return s
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            ARANGO_AARDVARK_PATH,
            parts.query,
            ARANGO_AARDVARK_FRAGMENT,
        )
    )


def normalize_override_to_http_origin(override: str) -> str:
    """
    Turn ``ARANGO_UI_IFRAME_URL`` (full UI URL or origin) into ``scheme://host[:port]``.
    """
    s = (override or "").strip()
    if not s:
        return ""
    parts = urlsplit(s)
    if not parts.scheme or not parts.hostname:
        return ""
    scheme = (parts.scheme or "https").lower()
    host = parts.hostname
    port = parts.port
    default = 443 if scheme == "https" else 80
    if port is None:
        port = default
    if port == default:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def resolve_arango_http_origin(
    *,
    env_override: str,
    registry_table: str,
    warehouse_id: str,
    auto_create_registry: bool,
) -> str:
    """
    Active cluster HTTP(S) origin (no path), for outbound proxying to Arango.

    Uses ``ARANGO_UI_IFRAME_URL`` when set (origin only), else the active UC registry row.
    """
    o = normalize_override_to_http_origin(env_override)
    if o:
        return o
    if not warehouse_id or not registry_table:
        return ""
    try:
        if auto_create_registry:
            ensure_registry_table(table_name=registry_table, warehouse_id=warehouse_id)
        row = get_active_registry_row(
            table_name=registry_table, warehouse_id=warehouse_id
        )
        if not row:
            return ""
        return build_arango_web_ui_base_url(
            str(row.get("protocol", "")),
            str(row.get("ip_address", "")),
            row.get("port", ""),
        )
    except Exception:
        return ""


def inject_url_basic_auth(url: str, user: str, password: str) -> str:
    """
    Add ``https://user:password@host/...`` userinfo so the browser sends Basic auth.

    Do **not** use this for ``iframe src`` — Chromium blocks credential URLs in
    embedded frames (blank / black iframe). Use for top-level ``target=_blank``
    links only. Credentials come from arango-workflow-app Connection profiles on UC volume.
    """
    user = (user or "").strip()
    if not user:
        return url
    parts = urlsplit(url)
    netloc = parts.netloc
    if "@" in netloc:
        return url
    hostname = parts.hostname
    if not hostname:
        return url
    port = parts.port
    auth = f"{quote(user, safe='')}:{quote(password or '', safe='')}"
    if port is not None and not (
        (parts.scheme == "https" and port == 443)
        or (parts.scheme == "http" and port == 80)
    ):
        hostpart = f"{hostname}:{port}"
    else:
        hostpart = hostname
    new_netloc = f"{auth}@{hostpart}"
    return urlunsplit(
        (parts.scheme, new_netloc, parts.path or "", parts.query, parts.fragment)
    )


def _registry_row_is_active(row: dict) -> bool:
    """Normalize API boolean/string shapes; inactive rows must never be used."""
    v = row.get("is_active")
    if v is True:
        return True
    if v is False or v is None:
        return False
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "t", "yes")
    if isinstance(v, (int, float)):
        return int(v) == 1
    return False


def get_active_registry_row(table_name: str, warehouse_id: str) -> dict | None:
    """
    Return the newest row with ``is_active`` true, or None if there is no active row.

    Relies on ``get_active_registry_entry`` (SQL ``WHERE is_active IS TRUE``) and
    double-checks the payload so an inactive row is never returned.

    Results are cached briefly (``ARANGO_REGISTRY_CACHE_TTL_SECONDS``, default 60s)
    so high-volume ``/api/arango/http`` proxy traffic does not re-query the SQL
    warehouse on every Arango REST hop.
    """
    from arango_gateway.deployment_profile import is_local_dev, static_arango_registry_row

    if is_local_dev():
        return dict(static_arango_registry_row())

    cache_key = f"{table_name}|{warehouse_id}"
    now = time.monotonic()
    cache_at = float(_registry_cache.get("at") or 0.0)
    if (
        cache_at
        and now - cache_at < _REGISTRY_CACHE_TTL_SEC
        and _registry_cache.get("key") == cache_key
    ):
        cached = _registry_cache.get("row")
        if isinstance(cached, dict):
            return dict(cached)

    result = get_active_registry_entry(table_name=table_name, warehouse_id=warehouse_id)
    rows = result.get("rows", [])
    if not rows:
        _registry_cache["at"] = now
        _registry_cache["key"] = cache_key
        _registry_cache["row"] = None
        return None
    row = rows[0]
    if not _registry_row_is_active(row):
        _registry_cache["at"] = now
        _registry_cache["key"] = cache_key
        _registry_cache["row"] = None
        return None
    _registry_cache["at"] = now
    _registry_cache["key"] = cache_key
    _registry_cache["row"] = dict(row)
    return dict(row)


def upsert_registry_entry(
    table_name: str,
    warehouse_id: str,
    cluster_name: str,
    ip_address: str,
    port: int,
    protocol: str,
) -> None:
    """
    Mark prior entries inactive and insert a new active connection row.

    This keeps history and avoids fragile UPDATE+LIMIT behavior.
    """
    ref = parse_registry_table_name(table_name)

    execute_sql(
        statement=f"UPDATE {ref.fqn} SET is_active = FALSE WHERE is_active = TRUE",
        warehouse_id=warehouse_id,
    )

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    safe_cluster = cluster_name.replace("'", "''")
    safe_ip = ip_address.replace("'", "''")
    safe_protocol = protocol.replace("'", "''")

    execute_sql(
        statement=f"""
            INSERT INTO {ref.fqn}
                (cluster_name, ip_address, port, protocol, is_active, updated_at)
            VALUES
                ('{safe_cluster}', '{safe_ip}', {port}, '{safe_protocol}', TRUE, TIMESTAMP('{ts}'))
        """,
        warehouse_id=warehouse_id,
    )
    invalidate_active_registry_cache()
