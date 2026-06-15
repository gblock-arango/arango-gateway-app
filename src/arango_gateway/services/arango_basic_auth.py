"""Resolve Arango HTTP basic-auth credentials for gateway I/O."""

from __future__ import annotations

import logging
import time
from typing import Any, Mapping

from arango_gateway.services.workflow_profile_store import get_active_profile_auth

logger = logging.getLogger(__name__)

# Short TTL avoids re-downloading the UC profiles JSON on every Arango proxy hop
# during extraction. Credentials still originate from UC (not the registry table).
# Worst case after Connect: stale auth for up to this many seconds.
_CACHE_TTL_SEC = 10.0
_cache: dict[str, Any] = {"at": 0.0, "key": "", "user": "", "password": "", "meta": {}}


def invalidate_arango_basic_auth_cache() -> None:
    """Force the next resolve to re-read UC (tests or ops)."""
    _cache["at"] = 0.0
    _cache["key"] = ""
    _cache["user"] = ""
    _cache["password"] = ""
    _cache["meta"] = {}


def _profile_auth_cache_key(user: str, password: str, active_profile: str) -> str:
    return f"{active_profile}|{user}|{len(password)}|{hash(password)}"


def resolve_arango_basic_auth(
    config: Mapping[str, Any],
    *,
    registry_row: dict[str, Any] | None = None,
) -> tuple[str, str, dict[str, Any]]:
    """
    Credentials for outbound Arango HTTP (proxy, embed, ping, bulk import).

    Used by **arango-gateway-app only** — not by the workflow Connection page
    (that app reads the same UC JSON via its own profile service).

    Primary source: ``workflow-data/settings/arango_connection_profiles.json``
    on the UC workflow volume (passwords never go in ``arango_connection_registry``).

    Optional legacy fallback: ``ARANGO_PING_BASIC_AUTH_*`` env (not set in ``app.yaml``).
    """
    profile_user, profile_password, active_profile = get_active_profile_auth()

    if profile_user:
        user = profile_user
        password = str(profile_password or "")
        meta = {
            "source": "uc_connection_profile",
            "active_profile": active_profile,
            "auth_user_present": True,
            "auth_password_present": bool(password),
        }
        cache_profile = active_profile
    else:
        user = (config.get("ARANGO_PING_BASIC_AUTH_USER") or "").strip()
        password_raw = config.get("ARANGO_PING_BASIC_AUTH_PASSWORD")
        password = str(password_raw) if password_raw is not None else ""
        meta_source = "env" if user or password else "missing"

        if not user and not password:
            from arango_gateway.deployment_profile import (
                is_local_dev,
                is_minikube_registry_row,
                read_minikube_root_password,
            )

            if is_local_dev() and is_minikube_registry_row(registry_row):
                mk_password = read_minikube_root_password()
                if mk_password:
                    user = "root"
                    password = mk_password
                    meta_source = "minikube_password_file"

        meta = {
            "source": meta_source,
            "active_profile": active_profile or "",
            "auth_user_present": bool(user),
            "auth_password_present": bool(password),
        }
        cache_profile = active_profile or ("minikube" if meta_source == "minikube_password_file" else "env")
        if meta_source == "missing":
            logger.info(
                "Arango basic auth not configured — set credentials in arango-workflow-app "
                "Connection page (/connection) and click Connect."
            )

    cache_key = _profile_auth_cache_key(user, password, cache_profile)
    now = time.monotonic()
    cache_at = float(_cache.get("at") or 0.0)
    if cache_at and now - cache_at < _CACHE_TTL_SEC and _cache.get("key") == cache_key:
        meta_cached = dict(_cache.get("meta") or {})
        return str(_cache.get("user") or ""), str(_cache.get("password") or ""), meta_cached

    _cache["at"] = now
    _cache["key"] = cache_key
    _cache["user"] = user
    _cache["password"] = password
    _cache["meta"] = meta
    return user, password, meta
