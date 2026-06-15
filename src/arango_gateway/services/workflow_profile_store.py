"""Read arango-workflow-app Connection settings from the UC workflow-data volume."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

_PROFILES_REL = "settings/arango_connection_profiles.json"


def _workflow_data_subdir() -> str:
    return (os.environ.get("UC_WORKFLOW_DATA_SUBDIR") or "workflow-data").strip() or "workflow-data"


def _registry_catalog_schema() -> tuple[str, str]:
    table = (os.environ.get("ARANGO_REGISTRY_TABLE") or "workspace.default.arango_connection_registry").strip()
    parts = table.split(".")
    if len(parts) >= 3:
        return parts[0], parts[1]
    return "workspace", "default"


def uc_workflow_volume_name() -> str:
    """Volume segment for workflow-data (shared env name with arango-gateway-app)."""
    explicit = (os.environ.get("UC_WORKFLOW_VOLUME_NAME") or "").strip()
    if explicit:
        return explicit
    legacy = (os.environ.get("UC_GRAPH_VOLUME_NAME") or "").strip()
    if legacy and legacy != "arango_agent_volume":
        return legacy
    return "arango_workflow_volume"


def workflow_data_root() -> Path:
    from arango_gateway.deployment_profile import is_local_dev, local_workflow_data_root

    if is_local_dev():
        return local_workflow_data_root()
    catalog, schema = _registry_catalog_schema()
    vol = uc_workflow_volume_name()
    return Path(f"/Volumes/{catalog}/{schema}/{vol}") / _workflow_data_subdir()


def workflow_data_root_uc_path() -> str:
    return str(workflow_data_root()).rstrip("/")


def local_mount_available() -> bool:
    return workflow_data_root().is_dir()


def use_files_api_for_io() -> bool:
    from arango_gateway.deployment_profile import should_use_uc_files_api_for_workflow_data

    if not should_use_uc_files_api_for_workflow_data():
        return False
    mode = (os.environ.get("UC_WORKFLOW_DATA_IO_MODE") or "auto").strip().lower()
    if mode in ("files_api", "api"):
        return True
    if mode in ("local_mount", "local", "mount"):
        return False
    if not local_mount_available():
        return True
    deploy = (os.environ.get("TEST_DEPLOYMENT_MODE") or "").strip().lower()
    if deploy and deploy not in ("local_dev", "local_docker", "local"):
        return True
    if (os.environ.get("DATABRICKS_RUNTIME_VERSION") or "").strip():
        return True
    return False


def _read_via_files_api(rel: str) -> bytes:
    from databricks.sdk import WorkspaceClient

    abs_path = f"{workflow_data_root_uc_path()}/{rel.lstrip('/')}"
    resp = WorkspaceClient().files.download(abs_path)
    if not resp.contents:
        raise FileNotFoundError(rel)
    return resp.contents.read()


def read_bytes(relative_path: str) -> bytes:
    rel = relative_path.strip().replace("\\", "/").lstrip("/")
    if not rel or ".." in rel.split("/"):
        raise ValueError("Invalid volume path")
    if use_files_api_for_io():
        return _read_via_files_api(rel)
    target = workflow_data_root() / rel
    return target.read_bytes()


def load_connection_profiles_doc() -> dict[str, Any]:
    try:
        raw = read_bytes(_PROFILES_REL)
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        logger.warning("Could not read connection profiles from UC: %s", exc)
        return {}


def _parse_server_endpoint(endpoint: str) -> tuple[str, str, int]:
    raw = (endpoint or "").strip()
    if not raw:
        raise ValueError("server_endpoint is required")
    if "://" not in raw:
        raw = f"https://{raw}"
    parts = urlsplit(raw)
    host = (parts.hostname or "").strip()
    if not host:
        raise ValueError("server_endpoint must include a hostname")
    protocol = (parts.scheme or "https").strip().lower()
    port = parts.port
    if port is None:
        port = 443 if protocol == "https" else 80
    return host, protocol, int(port)


def _endpoint_has_explicit_port(server_endpoint: str) -> bool:
    raw = (server_endpoint or "").strip()
    if not raw:
        return False
    if "://" not in raw:
        raw = f"https://{raw}"
    return urlsplit(raw).port is not None


def _parse_stored_port(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    if port < 1 or port > 65535:
        return None
    return port


def _resolve_connection_target(profile: dict[str, Any]) -> tuple[str, str, int]:
    endpoint = str(profile.get("server_endpoint") or "").strip()
    if not endpoint:
        raise ValueError("server_endpoint is required")
    host, protocol, parsed_port = _parse_server_endpoint(endpoint)
    if _endpoint_has_explicit_port(endpoint):
        return host, protocol, parsed_port
    explicit = _parse_stored_port(profile.get("port"))
    if explicit is not None:
        return host, protocol, explicit
    return host, protocol, parsed_port


def registry_row_from_active_profile() -> dict[str, Any] | None:
    """
    Build a registry-shaped row from the active Connection profile (Tier A JSON).

    Used when ``local_dev`` should honor AWS/GCS/Local targets saved on the laptop
    before or without a UC active row.
    """
    doc = load_connection_profiles_doc()
    active = str(doc.get("active_profile") or "").strip().lower()
    profiles = doc.get("profiles")
    if not isinstance(profiles, dict) or not active:
        return None
    profile = profiles.get(active)
    if not isinstance(profile, dict):
        return None

    env = str(profile.get("environment") or active).strip().lower()
    endpoint = str(profile.get("server_endpoint") or "").strip()
    if not endpoint:
        if env == "local":
            from arango_gateway.deployment_profile import static_arango_registry_row

            return dict(static_arango_registry_row())
        return None

    host, protocol, port = _resolve_connection_target(profile)
    cluster_name = str(profile.get("cluster_name") or profile.get("display_name") or active).strip()
    return {
        "cluster_name": cluster_name,
        "ip_address": host,
        "port": port,
        "protocol": protocol,
        "is_active": True,
    }


def get_active_profile_auth() -> tuple[str | None, str | None, str]:
    """
    Return ``(username, password, active_profile_key)`` from the workflow Connection cache.

    Password may be empty when the profile exists but no secret was saved yet.
    """
    doc = load_connection_profiles_doc()
    active = str(doc.get("active_profile") or "").strip().lower()
    profiles = doc.get("profiles")
    if not isinstance(profiles, dict) or active not in profiles:
        return None, None, active if active else ""
    profile = profiles.get(active)
    if not isinstance(profile, dict):
        return None, None, active
    user = str(profile.get("username") or "").strip()
    password = profile.get("password")
    if user:
        return user, str(password) if password is not None else "", active
    return None, None, active
