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


def test_resolve_minikube_password_file_in_local_dev(monkeypatch, tmp_path):
    invalidate_arango_basic_auth_cache()
    pw_file = tmp_path / "arango-root-password.txt"
    pw_file.write_text("test-root-pw\n", encoding="utf-8")
    monkeypatch.setenv("TEST_DEPLOYMENT_MODE", "local_dev")
    minikube_row = {
        "cluster_name": "local-minikube-dev",
        "ip_address": "127.0.0.1",
        "port": 18529,
        "protocol": "https",
    }
    with patch(
        "arango_gateway.services.arango_basic_auth.get_active_profile_auth",
        return_value=(None, None, ""),
    ), patch(
        "arango_gateway.deployment_profile.minikube_root_password_file",
        return_value=pw_file,
    ):
        user, password, meta = resolve_arango_basic_auth({}, registry_row=minikube_row)
    assert user == "root"
    assert password == "test-root-pw"
    assert meta["source"] == "minikube_password_file"


def test_resolve_skips_minikube_password_for_remote_row_in_local_dev(monkeypatch, tmp_path):
    invalidate_arango_basic_auth_cache()
    pw_file = tmp_path / "arango-root-password.txt"
    pw_file.write_text("test-root-pw\n", encoding="utf-8")
    monkeypatch.setenv("TEST_DEPLOYMENT_MODE", "local_dev")
    aws_row = {
        "cluster_name": "aws-arango",
        "ip_address": "gg8dcifd.rnd.pilot.arango.ai",
        "port": 443,
        "protocol": "https",
    }
    with patch(
        "arango_gateway.services.arango_basic_auth.get_active_profile_auth",
        return_value=(None, None, ""),
    ), patch(
        "arango_gateway.deployment_profile.minikube_root_password_file",
        return_value=pw_file,
    ):
        user, password, meta = resolve_arango_basic_auth({}, registry_row=aws_row)
    assert user == ""
    assert password == ""
    assert meta["source"] == "missing"


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
