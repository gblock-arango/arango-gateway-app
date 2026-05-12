"""Runtime config for the Arango gateway Databricks App."""

import os
from dataclasses import dataclass, field

# Defaults align with ``app.yaml`` so local ``python app.py`` matches a typical first deploy.
_DEFAULT_DATABRICKS_SQL_WAREHOUSE_ID = "473d40703241ee4c"
_DEFAULT_ARANGO_REGISTRY_TABLE = "workspace.default.arango_connection_registry"
_DEFAULT_ARANGO_GATEWAY_REGISTRY_TABLE = "workspace.default.arango_gateway_registry"
_DEFAULT_ARANGO_PING_BASIC_AUTH_PASSWORD = (
    "8c1bc9344c886819859534a5ac951412c650870662228617cfbb69023489afd2"
)


def _uc_graph_volume_name_from_env() -> str:
    """Volume name segment under /Volumes/<catalog>/<schema>/ (see ``UC_GRAPH_VOLUME_NAME``)."""
    v = (os.environ.get("UC_GRAPH_VOLUME_NAME") or "arango_agent_volume").strip()
    return v if v else "arango_agent_volume"


def _uc_graph_snapshot_base() -> str:
    """
    UC Files path for gzip JSONL graph exports.

    If ``UC_GRAPH_SNAPSHOT_BASE`` is present in the environment, its value is used (empty
    means no default export path). Otherwise derives
    ``/Volumes/<catalog>/<schema>/<UC_GRAPH_VOLUME_NAME>/uc_graph_snapshots`` from
    ``ARANGO_REGISTRY_TABLE`` and ``UC_GRAPH_VOLUME_NAME``. If the table name is missing
    or not ``catalog.schema.table``, returns ``""``.

    Databricks Apps should set ``ARANGO_REGISTRY_TABLE`` and create the UC volume once
    (see ``app.yaml``). Override the full path with ``UC_GRAPH_SNAPSHOT_BASE`` if needed.
    """
    if "UC_GRAPH_SNAPSHOT_BASE" in os.environ:
        return os.environ.get("UC_GRAPH_SNAPSHOT_BASE", "").strip()
    table = (os.environ.get("ARANGO_REGISTRY_TABLE", "") or "").strip() or _DEFAULT_ARANGO_REGISTRY_TABLE
    parts = table.split(".")
    if len(parts) >= 3:
        catalog, schema = parts[0], parts[1]
        vol = _uc_graph_volume_name_from_env()
        return f"/Volumes/{catalog}/{schema}/{vol}/uc_graph_snapshots"
    return ""


@dataclass
class AppConfig:
    """Application settings loaded from environment variables at instantiation time."""

    DATABRICKS_SQL_WAREHOUSE_ID: str = field(
        default_factory=lambda: (
            (os.environ.get("DATABRICKS_SQL_WAREHOUSE_ID", "") or "").strip()
            or _DEFAULT_DATABRICKS_SQL_WAREHOUSE_ID
        )
    )
    ARANGO_REGISTRY_TABLE: str = field(
        default_factory=lambda: (
            (os.environ.get("ARANGO_REGISTRY_TABLE", "") or "").strip()
            or _DEFAULT_ARANGO_REGISTRY_TABLE
        )
    )
    ARANGO_REGISTRY_AUTO_CREATE: bool = field(
        default_factory=lambda: os.environ.get(
            "ARANGO_REGISTRY_AUTO_CREATE", "true"
        ).lower()
        == "true"
    )
    ARANGO_GATEWAY_REGISTRY_TABLE: str = field(
        default_factory=lambda: (
            (os.environ.get("ARANGO_GATEWAY_REGISTRY_TABLE", "") or "").strip()
            or _DEFAULT_ARANGO_GATEWAY_REGISTRY_TABLE
        )
    )
    ARANGO_GATEWAY_REGISTRY_AUTO_CREATE: bool = field(
        default_factory=lambda: os.environ.get(
            "ARANGO_GATEWAY_REGISTRY_AUTO_CREATE", "true"
        ).lower()
        == "true"
    )
    ARANGO_PING_TIMEOUT_SECONDS: float = field(
        default_factory=lambda: float(os.environ.get("ARANGO_PING_TIMEOUT_SECONDS", "5"))
    )
    ARANGO_PING_BASIC_AUTH_USER: str = field(
        default_factory=lambda: os.environ.get("ARANGO_PING_BASIC_AUTH_USER", "")
    )
    ARANGO_PING_BASIC_AUTH_PASSWORD: str = field(
        default_factory=lambda: (
            (os.environ.get("ARANGO_PING_BASIC_AUTH_PASSWORD", "") or "").strip()
            or _DEFAULT_ARANGO_PING_BASIC_AUTH_PASSWORD
        )
    )
    ARANGO_PING_TLS_VERIFY: bool = field(
        default_factory=lambda: os.environ.get("ARANGO_PING_TLS_VERIFY", "true").lower()
        == "true"
    )
    DEBUG_STARTUP_CHECKS: bool = field(
        default_factory=lambda: os.environ.get("DEBUG_STARTUP_CHECKS", "false").lower()
        == "true"
    )
    DEBUG_WEBHOOK_URL: str = field(
        default_factory=lambda: os.environ.get("DEBUG_WEBHOOK_URL", "")
    )
    ARANGO_UI_IFRAME_URL: str = field(
        default_factory=lambda: (os.environ.get("ARANGO_UI_IFRAME_URL", "") or "").strip()
    )
    ARANGO_EMBED_COOKIE_SAMESITE_NONE: bool = field(
        default_factory=lambda: os.environ.get(
            "ARANGO_EMBED_COOKIE_SAMESITE_NONE", "true"
        ).lower()
        == "true"
    )
    UC_GRAPH_VOLUME_NAME: str = field(
        default_factory=_uc_graph_volume_name_from_env
    )
    UC_GRAPH_SNAPSHOT_BASE: str = field(
        default_factory=_uc_graph_snapshot_base
    )
    ARANGO_DATABASE: str = field(
        default_factory=lambda: (os.environ.get("ARANGO_DATABASE", "_system") or "_system").strip()
    )
    ARANGO_UC_IMPORT_BATCH_SIZE: int = field(
        default_factory=lambda: max(
            1, int(os.environ.get("ARANGO_UC_IMPORT_BATCH_SIZE", "300") or "300")
        )
    )
    GENIE_SPACE_ID: str = field(
        default_factory=lambda: (os.environ.get("GENIE_SPACE_ID", "") or "").strip()
    )
    GENIE_SPACE_REGISTRY_TABLE: str = field(
        default_factory=lambda: (os.environ.get("GENIE_SPACE_REGISTRY_TABLE", "") or "").strip()
    )
    GENIE_SPACE_REGISTRY_AUTO_CREATE: bool = field(
        default_factory=lambda: os.environ.get(
            "GENIE_SPACE_REGISTRY_AUTO_CREATE", "true"
        ).lower()
        == "true"
    )
    GENIE_AUTO_PROVISION: bool = field(
        default_factory=lambda: os.environ.get("GENIE_AUTO_PROVISION", "true").lower()
        == "true"
    )
    GENIE_SERIALIZED_SPACE: str = field(
        default_factory=lambda: (os.environ.get("GENIE_SERIALIZED_SPACE", "") or "").strip()
    )
    GENIE_SERIALIZED_SPACE_FILE: str = field(
        default_factory=lambda: (os.environ.get("GENIE_SERIALIZED_SPACE_FILE", "") or "").strip()
    )
    GENIE_SPACE_TITLE: str = field(
        default_factory=lambda: (
            os.environ.get("GENIE_SPACE_TITLE") or "Genie (Arango agent)"
        ).strip()
    )
    GENIE_SPACE_DESCRIPTION: str = field(
        default_factory=lambda: (os.environ.get("GENIE_SPACE_DESCRIPTION", "") or "").strip()
    )
    GENIE_SPACE_PARENT_PATH: str = field(
        default_factory=lambda: (os.environ.get("GENIE_SPACE_PARENT_PATH", "") or "").strip()
    )
    GENIE_PROVISION_LOCK_PATH: str = field(
        default_factory=lambda: (os.environ.get("GENIE_PROVISION_LOCK_PATH", "") or "").strip()
    )
    GENIE_MESSAGE_TIMEOUT_SECONDS: float = field(
        default_factory=lambda: float(
            os.environ.get("GENIE_MESSAGE_TIMEOUT_SECONDS", "600") or "600"
        )
    )
    # Optional POST target for dashboard "Arango" mode (e.g. minikube ADA HTTP). Empty => stub.
    ARANGO_CONVERSATION_URL: str = field(
        default_factory=lambda: (os.environ.get("ARANGO_CONVERSATION_URL", "") or "").strip()
    )
    ARANGO_CONVERSATION_TIMEOUT_SECONDS: float = field(
        default_factory=lambda: float(
            os.environ.get("ARANGO_CONVERSATION_TIMEOUT_SECONDS", "120") or "120"
        )
    )
