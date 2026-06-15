"""Local vs Databricks Apps deployment profile (driven by ``TEST_DEPLOYMENT_MODE``)."""

from __future__ import annotations

import os
from enum import Enum
from pathlib import Path
from urllib.parse import urlparse

LOCAL_GATEWAY_HOST = "127.0.0.1"
LOCAL_GATEWAY_PORT = 8001
LOCAL_MCP_PORT = 8002
LOCAL_WORKFLOW_PORT = 8010

MINIKUBE_ARANGO_HOST = "127.0.0.1"
MINIKUBE_ARANGO_PORT = 18529
MINIKUBE_CLUSTER_NAME = "local-minikube-dev"


class DeploymentMode(str, Enum):
    LOCAL_DEV = "local_dev"
    SELF_MANAGED_PLATFORM = "self_managed_platform"
    MANAGED_PLATFORM = "managed_platform"


_LOCAL_ALIASES = frozenset({"local_dev", "local_docker", "local"})


def _normalize_mode(raw: str) -> DeploymentMode:
    s = (raw or "").strip().lower()
    if s in _LOCAL_ALIASES or s == DeploymentMode.LOCAL_DEV.value:
        return DeploymentMode.LOCAL_DEV
    if s == DeploymentMode.MANAGED_PLATFORM.value:
        return DeploymentMode.MANAGED_PLATFORM
    if s:
        return DeploymentMode.SELF_MANAGED_PLATFORM
    return DeploymentMode.SELF_MANAGED_PLATFORM


def current_mode() -> DeploymentMode:
    return _normalize_mode(os.environ.get("TEST_DEPLOYMENT_MODE", ""))


def is_local_dev() -> bool:
    return current_mode() == DeploymentMode.LOCAL_DEV


def local_gateway_base_url() -> str:
    return f"http://{LOCAL_GATEWAY_HOST}:{LOCAL_GATEWAY_PORT}"


def local_mcp_base_url() -> str:
    return f"http://{LOCAL_GATEWAY_HOST}:{LOCAL_MCP_PORT}"


def local_workflow_base_url() -> str:
    return f"http://{LOCAL_GATEWAY_HOST}:{LOCAL_WORKFLOW_PORT}"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def local_workflow_data_root() -> Path:
    explicit = (os.environ.get("LOCAL_WORKFLOW_DATA_ROOT") or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    sibling = _repo_root().parent / "arango-workflow-app" / "local_dev" / "workflow-data"
    if sibling.is_dir() or is_local_dev():
        return sibling.resolve()
    return (_repo_root() / "local_dev" / "workflow-data").resolve()


def should_publish_peer_url_to_uc() -> bool:
    return not is_local_dev()


def should_use_uc_files_api_for_workflow_data() -> bool:
    if is_local_dev():
        return False
    mode = (os.environ.get("UC_WORKFLOW_DATA_IO_MODE") or "auto").strip().lower()
    if mode in ("files_api", "api"):
        return True
    if mode in ("local_mount", "local", "mount"):
        return False
    return True


def is_localhost_peer_url(url: str) -> bool:
    u = (url or "").strip()
    if not u:
        return False
    try:
        parsed = urlparse(u)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return host in ("127.0.0.1", "localhost", "::1")


def should_attach_outbound_bearer(url: str) -> bool:
    if is_local_dev() and is_localhost_peer_url(url):
        return False
    return True


def static_arango_registry_row() -> dict[str, object]:
    return {
        "cluster_name": MINIKUBE_CLUSTER_NAME,
        "ip_address": MINIKUBE_ARANGO_HOST,
        "port": MINIKUBE_ARANGO_PORT,
        "protocol": "https",
        "is_active": True,
    }


def is_minikube_registry_row(row: dict[str, object] | None) -> bool:
    """True when registry coordinates target the default local Minikube Arango."""
    if not row:
        return False
    cluster = str(row.get("cluster_name") or "").strip()
    if cluster == MINIKUBE_CLUSTER_NAME:
        return True
    ip = str(row.get("ip_address") or "").strip().lower()
    try:
        port = int(row.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    return ip in ("127.0.0.1", "localhost", "::1") and port == MINIKUBE_ARANGO_PORT


def arango_ping_tls_verify_default() -> bool:
    return not is_local_dev()


def minikube_root_password_file() -> Path | None:
    """Resolve Minikube root password file for local_dev (no Connection profile required)."""
    explicit = (os.environ.get("ARANGO_ROOT_PASSWORD_FILE") or "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_file() else None
    candidates = [
        _repo_root().parent.parent / "single-node-arango-on-minikube" / ".state" / "arango-root-password.txt",
        Path.home() / "gazebo_dev" / "ARANGO" / "single-node-arango-on-minikube" / ".state" / "arango-root-password.txt",
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


def read_minikube_root_password() -> str:
    path = minikube_root_password_file()
    if not path:
        return ""
    first = path.read_text(encoding="utf-8").strip().splitlines()
    return first[0].strip() if first else ""
