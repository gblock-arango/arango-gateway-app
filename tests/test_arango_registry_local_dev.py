"""Tests for multi-target registry resolution in local_dev."""

from __future__ import annotations

from unittest.mock import patch

from arango_gateway.services import arango_registry as reg


def test_local_dev_prefers_active_profile_over_uc(monkeypatch):
    reg.invalidate_active_registry_cache()
    monkeypatch.setenv("TEST_DEPLOYMENT_MODE", "local_dev")
    profile_row = {
        "cluster_name": "aws-arango",
        "ip_address": "aws.example.com",
        "port": 443,
        "protocol": "https",
        "is_active": True,
    }
    uc_row = {
        "cluster_name": "local-minikube-dev",
        "ip_address": "127.0.0.1",
        "port": 18529,
        "protocol": "https",
        "is_active": True,
    }
    with (
        patch(
            "arango_gateway.services.workflow_profile_store.registry_row_from_active_profile",
            return_value=profile_row,
        ),
        patch.object(reg, "get_active_registry_entry", return_value={"rows": [uc_row]}) as mock_uc,
    ):
        out = reg.get_active_registry_row("workspace.default.arango_connection_registry", "wh1")
    assert out == profile_row
    mock_uc.assert_not_called()


def test_local_dev_falls_back_to_minikube_when_uc_empty(monkeypatch):
    reg.invalidate_active_registry_cache()
    monkeypatch.setenv("TEST_DEPLOYMENT_MODE", "local_dev")
    with (
        patch(
            "arango_gateway.services.workflow_profile_store.registry_row_from_active_profile",
            return_value=None,
        ),
        patch.object(reg, "get_active_registry_entry", return_value={"rows": []}),
    ):
        out = reg.get_active_registry_row("workspace.default.arango_connection_registry", "wh1")
    assert out is not None
    assert out["cluster_name"] == "local-minikube-dev"
    assert out["ip_address"] == "127.0.0.1"
    assert out["port"] == 18529
