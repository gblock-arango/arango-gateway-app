"""Tests for UC Connection profile auth resolution in arango-gateway-app."""

from __future__ import annotations

from unittest.mock import patch

from arango_gateway.services.arango_basic_auth import (
    invalidate_arango_basic_auth_cache,
    resolve_arango_basic_auth,
)


def test_resolve_from_uc_connection_profile():
    invalidate_arango_basic_auth_cache()
    with patch(
        "arango_gateway.services.arango_basic_auth.get_active_profile_auth",
        return_value=("root", "secret", "aws"),
    ):
        user, password, meta = resolve_arango_basic_auth({})
    assert user == "root"
    assert password == "secret"
    assert meta["source"] == "uc_connection_profile"
    assert meta["active_profile"] == "aws"


def test_resolve_missing_when_no_profile_or_env():
    invalidate_arango_basic_auth_cache()
    with patch(
        "arango_gateway.services.arango_basic_auth.get_active_profile_auth",
        return_value=(None, None, ""),
    ):
        user, password, meta = resolve_arango_basic_auth({})
    assert user == ""
    assert password == ""
    assert meta["source"] == "missing"


def test_resolve_env_fallback_when_profile_empty():
    invalidate_arango_basic_auth_cache()
    with patch(
        "arango_gateway.services.arango_basic_auth.get_active_profile_auth",
        return_value=(None, None, ""),
    ):
        user, password, meta = resolve_arango_basic_auth(
            {
                "ARANGO_PING_BASIC_AUTH_USER": "root",
                "ARANGO_PING_BASIC_AUTH_PASSWORD": "legacy",
            }
        )
    assert user == "root"
    assert password == "legacy"
    assert meta["source"] == "env"
