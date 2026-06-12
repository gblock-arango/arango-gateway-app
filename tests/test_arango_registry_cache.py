"""Tests for UC registry row caching in arango-gateway-app."""

from __future__ import annotations

from unittest.mock import patch

from arango_gateway.services import arango_registry as reg


def test_get_active_registry_row_uses_cache():
    reg.invalidate_active_registry_cache()
    row = {
        "cluster_name": "aws",
        "ip_address": "host.example",
        "port": 443,
        "protocol": "https",
        "is_active": True,
    }
    with patch.object(
        reg,
        "get_active_registry_entry",
        return_value={"rows": [row]},
    ) as mock_entry:
        first = reg.get_active_registry_row("workspace.default.arango_connection_registry", "wh1")
        second = reg.get_active_registry_row("workspace.default.arango_connection_registry", "wh1")
    assert first == row
    assert second == row
    mock_entry.assert_called_once()


def test_upsert_invalidates_registry_cache():
    reg.invalidate_active_registry_cache()
    with (
        patch.object(reg, "execute_sql"),
        patch.object(
            reg,
            "get_active_registry_entry",
            return_value={"rows": [{"is_active": True, "ip_address": "x", "port": 443, "protocol": "https"}]},
        ) as mock_entry,
    ):
        reg.get_active_registry_row("workspace.default.arango_connection_registry", "wh1")
        reg.upsert_registry_entry(
            "workspace.default.arango_connection_registry",
            "wh1",
            "aws",
            "host.example",
            443,
            "https",
        )
        reg.get_active_registry_row("workspace.default.arango_connection_registry", "wh1")
    assert mock_entry.call_count == 2
