"""Startup diagnostics for Unity Catalog registry and Arango reachability (gateway only)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib import error, request

from arango_gateway.services.arango_http import ping_arango_endpoint
from arango_gateway.services.arango_basic_auth import resolve_arango_basic_auth
from arango_gateway.services.arango_registry import (
    ensure_registry_table,
    get_active_registry_row,
)


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _post_debug_webhook(url: str, payload: dict) -> None:
    if not url:
        return
    try:
        req = request.Request(url=url, method="POST")
        req.add_header("Content-Type", "application/json")
        body = json.dumps(payload).encode("utf-8")
        with request.urlopen(req, data=body, timeout=3):
            pass
    except (error.URLError, TimeoutError, ValueError):
        pass


def run_startup_debug_check(app) -> dict:
    """Run diagnostics and return sanitized status payload (no Genie block)."""
    table_name = app.config["ARANGO_REGISTRY_TABLE"]
    warehouse_id = app.config["DATABRICKS_SQL_WAREHOUSE_ID"]
    timeout_seconds = float(app.config.get("ARANGO_PING_TIMEOUT_SECONDS", 5.0))
    verify_tls = bool(app.config.get("ARANGO_PING_TLS_VERIFY", True))

    status = {
        "checked_at": _now_utc(),
        "registry_table": table_name,
        "warehouse_id_present": bool(warehouse_id),
        "secrets": {"source": "pending"},
        "registry": {"status": "unknown"},
        "probe": {"status": "skipped"},
    }

    try:
        if app.config.get("ARANGO_REGISTRY_AUTO_CREATE", True):
            ensure_registry_table(table_name=table_name, warehouse_id=warehouse_id)

        row = get_active_registry_row(table_name=table_name, warehouse_id=warehouse_id)
        auth_user, auth_password, auth_meta = resolve_arango_basic_auth(
            app.config,
            registry_row=row,
        )
        status["secrets"] = auth_meta

        if not row:
            status["registry"] = {"status": "empty", "message": "No active row found"}
        else:
            status["registry"] = {
                "status": "ok",
                "cluster_name": row.get("cluster_name"),
                "ip_address": row.get("ip_address"),
                "port": row.get("port"),
                "protocol": row.get("protocol"),
            }
            probe = ping_arango_endpoint(
                protocol=str(row["protocol"]).lower(),
                ip_address=str(row["ip_address"]),
                port=int(row["port"]),
                path="/_api/version",
                timeout_seconds=timeout_seconds,
                basic_auth_user=auth_user or None,
                basic_auth_password=auth_password if auth_user else None,
                verify_tls=verify_tls,
            )
            status["probe"] = {
                "status": "ok" if probe.get("reachable") else "unreachable",
                "details": probe,
            }
    except Exception as exc:
        status["registry"] = {"status": "error", "error": str(exc)}
        status["probe"] = {"status": "error", "error": str(exc)}

    _post_debug_webhook(app.config.get("DEBUG_WEBHOOK_URL", ""), status)
    return status
