"""Read arango-workflow-app Connection settings from the UC workflow-data volume."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

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
    catalog, schema = _registry_catalog_schema()
    vol = uc_workflow_volume_name()
    return Path(f"/Volumes/{catalog}/{schema}/{vol}") / _workflow_data_subdir()


def workflow_data_root_uc_path() -> str:
    return str(workflow_data_root()).rstrip("/")


def local_mount_available() -> bool:
    return workflow_data_root().is_dir()


def use_files_api_for_io() -> bool:
    mode = (os.environ.get("UC_WORKFLOW_DATA_IO_MODE") or "auto").strip().lower()
    if mode in ("files_api", "api"):
        return True
    if mode in ("local_mount", "local", "mount"):
        return False
    if not local_mount_available():
        return True
    deploy = (os.environ.get("TEST_DEPLOYMENT_MODE") or "").strip().lower()
    if deploy and deploy not in ("local_docker", "local"):
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
