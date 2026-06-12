"""Tests for gateway-side Arango HTTP batch execution."""

from __future__ import annotations

from unittest.mock import patch

from arango_gateway.services.arango_http_batch import execute_arango_http_batch


def test_execute_batch_empty():
    result = execute_arango_http_batch(
        base_url="https://host:8529",
        requests=[],
        basic_auth_user="root",
        basic_auth_password="pw",
        verify_tls=True,
        timeout_seconds=30.0,
        allow_admin=False,
    )
    assert result["ok"] is True
    assert result["count"] == 0
    assert result["results"] == []


def test_execute_batch_parallel_counts_failures():
    specs = [
        {"method": "POST", "path": "/_db/x/_api/collection", "body": {"name": "a", "type": 2}},
        {"method": "POST", "path": "/_db/x/_api/collection", "body": {"name": "b", "type": 2}},
    ]

    def fake_request(**kwargs):
        payload = kwargs.get("payload") or {}
        if payload.get("name") == "b":
            return {"ok": False, "status_code": 500, "body": {"error": True, "errorNum": 1}}
        return {"ok": True, "status_code": 200, "body": {"name": "a"}}

    with patch(
        "arango_gateway.services.arango_http_batch.arango_json_request",
        side_effect=fake_request,
    ):
        result = execute_arango_http_batch(
            base_url="https://host:8529",
            requests=specs,
            basic_auth_user="root",
            basic_auth_password="pw",
            verify_tls=True,
            timeout_seconds=30.0,
            allow_admin=False,
            parallel=True,
            max_workers=2,
        )

    assert result["ok"] is False
    assert result["failed"] == 1
    assert len(result["results"]) == 2
    assert result["results"][0]["ok"] is True
    assert result["results"][1]["ok"] is False


def test_duplicate_collection_treated_as_ok():
    with patch(
        "arango_gateway.services.arango_http_batch.arango_json_request",
        return_value={
            "ok": False,
            "status_code": 409,
            "body": {"error": True, "errorNum": 1207, "errorMessage": "duplicate name"},
        },
    ):
        result = execute_arango_http_batch(
            base_url="https://host:8529",
            requests=[
                {"method": "POST", "path": "/_db/x/_api/collection", "body": {"name": "a"}},
            ],
            basic_auth_user="root",
            basic_auth_password="pw",
            verify_tls=True,
            timeout_seconds=30.0,
            allow_admin=False,
            parallel=False,
        )
    assert result["ok"] is True
    assert result["results"][0]["ok"] is True
