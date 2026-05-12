"""Unity Catalog registry for the deployed arango-gateway-app public base URL."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from databricks.sdk import WorkspaceClient

from arango_gateway.services.arango_registry import parse_registry_table_name
from arango_gateway.services.databricks_sql import execute_sql

logger = logging.getLogger(__name__)


def ensure_gateway_registry_table(table_name: str, warehouse_id: str) -> None:
    """Create schema/table for gateway URL registry if they do not exist."""
    ref = parse_registry_table_name(table_name)
    execute_sql(
        statement=f"CREATE SCHEMA IF NOT EXISTS `{ref.catalog}`.`{ref.schema}`",
        warehouse_id=warehouse_id,
    )
    execute_sql(
        statement=f"""
            CREATE TABLE IF NOT EXISTS {ref.fqn} (
                base_url STRING NOT NULL,
                app_name STRING NOT NULL,
                is_active BOOLEAN NOT NULL,
                updated_at TIMESTAMP NOT NULL
            )
            USING DELTA
        """,
        warehouse_id=warehouse_id,
    )
    try_grant_account_users_gateway_registry_dml(ref, warehouse_id)


def try_grant_account_users_gateway_registry_dml(ref, warehouse_id: str) -> None:
    """
    Allow non-owner identities (e.g. laptop ``deploy_app.sh``) to UPDATE/INSERT the URL row.

    The gateway app often creates this table first (owner = app SP); without a follow-on
    grant, human-driven SQL upserts fail with ``INSUFFICIENT_PERMISSIONS`` / missing SELECT.

    Uses the account-level ``account users`` principal (see Databricks UC GRANT docs). Tighten
    in production if your security model disallows this.
    """
    try:
        execute_sql(
            statement=(
                f"GRANT SELECT, MODIFY ON TABLE {ref.fqn} TO `account users`"
            ),
            warehouse_id=warehouse_id,
        )
    except Exception as exc:
        logger.info(
            "Could not GRANT gateway URL registry to `account users` (may be disabled or not owner): %s",
            exc,
        )


def publish_gateway_base_url(
    *,
    table_name: str,
    warehouse_id: str,
    base_url: str,
    app_name: str,
) -> None:
    """
    Mark prior rows inactive and insert a new active row (same pattern as Arango tunnel registry).
    """
    ref = parse_registry_table_name(table_name)
    url = (base_url or "").strip().rstrip("/")
    name = (app_name or "").strip()
    if not url or not name:
        return

    try_grant_account_users_gateway_registry_dml(ref, warehouse_id)
    execute_sql(
        statement=f"UPDATE {ref.fqn} SET is_active = FALSE WHERE is_active = TRUE",
        warehouse_id=warehouse_id,
    )
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    safe_url = url.replace("'", "''")
    safe_name = name.replace("'", "''")
    execute_sql(
        statement=f"""
            INSERT INTO {ref.fqn}
                (base_url, app_name, is_active, updated_at)
            VALUES
                ('{safe_url}', '{safe_name}', TRUE, TIMESTAMP('{ts}'))
        """,
        warehouse_id=warehouse_id,
    )
    try_grant_account_users_gateway_registry_dml(ref, warehouse_id)


def resolve_self_app_base_url() -> str | None:
    """
    Return this Databricks App's public ``https://…`` base URL, or None.

    Uses ``DATABRICKS_APP_NAME`` and the Apps API (no ``DATABRICKS_APP_URL`` env is provided by the platform).
    """
    name = (os.environ.get("DATABRICKS_APP_NAME") or "").strip()
    if not name:
        return None
    try:
        app = WorkspaceClient().apps.get(name)
        u = (getattr(app, "url", None) or "").strip().rstrip("/")
        return u or None
    except Exception as exc:
        logger.warning("Could not resolve Databricks App URL for %r: %s", name, exc)
        return None


def publish_self_gateway_url_to_uc_if_configured(app) -> None:
    """On gateway startup, upsert our public URL into UC for consumers (e.g. dashboard)."""
    if not app.config.get("ARANGO_GATEWAY_REGISTRY_AUTO_CREATE", True):
        return
    table = str(app.config.get("ARANGO_GATEWAY_REGISTRY_TABLE") or "").strip()
    warehouse = str(app.config.get("DATABRICKS_SQL_WAREHOUSE_ID") or "").strip()
    if not table or not warehouse:
        return
    url = resolve_self_app_base_url()
    if not url:
        return
    app_name = (os.environ.get("DATABRICKS_APP_NAME") or "").strip() or "unknown"
    try:
        ensure_gateway_registry_table(table_name=table, warehouse_id=warehouse)
        publish_gateway_base_url(
            table_name=table,
            warehouse_id=warehouse,
            base_url=url,
            app_name=app_name,
        )
        logger.info("Published gateway base URL to UC table %s", table)
    except Exception as exc:
        logger.warning("Could not publish gateway URL to UC (%s): %s", table, exc)
