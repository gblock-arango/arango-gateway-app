"""ArangoDB named graph helpers (HTTP General Graph / gharial API)."""

from __future__ import annotations

import base64
import json
import ssl
from typing import Any
from urllib import error, request


def _normalize_base_url(base_url: str) -> str:
    return base_url.rstrip("/")


def create_named_graph(
    *,
    base_url: str,
    name: str,
    edge_definitions: list[dict[str, Any]],
    orphan_collections: list[str] | None = None,
    basic_auth_user: str | None = None,
    basic_auth_password: str | None = None,
    verify_tls: bool = True,
    timeout_seconds: float = 60.0,
) -> dict[str, Any]:
    """
    Create a named graph via POST /_api/gharial.

    ``edge_definitions`` items must match Arango's shape, e.g.::

        {"collection": "edges", "from": ["nodes"], "to": ["nodes"]}

    Returns a dict with ``ok`` (bool), ``status_code``, and ``body`` (parsed JSON).
    """
    url = f"{_normalize_base_url(base_url)}/_api/gharial"
    payload: dict[str, Any] = {"name": name, "edgeDefinitions": edge_definitions}
    if orphan_collections:
        payload["orphanCollections"] = orphan_collections

    data = json.dumps(payload).encode("utf-8")
    req = request.Request(url=url, method="POST", data=data)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    if basic_auth_user:
        password = (
            basic_auth_password if basic_auth_password is not None else ""
        )
        token = base64.b64encode(
            f"{basic_auth_user}:{password}".encode("utf-8")
        ).decode("ascii")
        req.add_header("Authorization", f"Basic {token}")

    ssl_ctx: ssl.SSLContext | None = None
    if url.lower().startswith("https:") and not verify_tls:
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

    open_kw: dict[str, Any] = {"timeout": timeout_seconds}
    if ssl_ctx is not None:
        open_kw["context"] = ssl_ctx

    try:
        with request.urlopen(req, **open_kw) as resp:
            body_text = resp.read().decode("utf-8", errors="replace")
            body: dict[str, Any] = (
                json.loads(body_text) if body_text.strip() else {}
            )
            return {
                "ok": body.get("error") is not True,
                "status_code": resp.getcode(),
                "body": body,
            }
    except error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(body_text) if body_text.strip() else {}
        except json.JSONDecodeError:
            body = {"raw": body_text}
        return {
            "ok": False,
            "status_code": exc.code,
            "body": body,
        }
    except Exception as exc:
        return {
            "ok": False,
            "status_code": None,
            "error": str(exc),
            "body": {},
        }


def _graph_already_exists(status_code: int | None, body: dict[str, Any]) -> bool:
    if status_code == 409:
        return True
    msg = str(body.get("errorMessage", "")).lower()
    if "graph" in msg and "exist" in msg:
        return True
    # Arango uses several numeric codes across versions for duplicate graph
    if body.get("errorNum") in (1925, 1928, 1929):
        return True
    return False


def ensure_named_graph(
    *,
    base_url: str,
    name: str,
    edge_definitions: list[dict[str, Any]],
    orphan_collections: list[str] | None = None,
    basic_auth_user: str | None = None,
    basic_auth_password: str | None = None,
    verify_tls: bool = True,
    timeout_seconds: float = 60.0,
) -> dict[str, Any]:
    """
    Like :func:`create_named_graph`, but treats an already-existing graph as success.
    """
    result = create_named_graph(
        base_url=base_url,
        name=name,
        edge_definitions=edge_definitions,
        orphan_collections=orphan_collections,
        basic_auth_user=basic_auth_user,
        basic_auth_password=basic_auth_password,
        verify_tls=verify_tls,
        timeout_seconds=timeout_seconds,
    )
    if result.get("ok"):
        return result
    body = result.get("body") or {}
    if _graph_already_exists(result.get("status_code"), body):
        return {
            "ok": True,
            "already_existed": True,
            "status_code": result.get("status_code"),
            "body": body,
        }
    return result


def ensure_simple_named_graph(
    *,
    base_url: str,
    name: str,
    edge_collection: str,
    from_collections: list[str],
    to_collections: list[str],
    basic_auth_user: str | None = None,
    basic_auth_password: str | None = None,
    verify_tls: bool = True,
    timeout_seconds: float = 60.0,
) -> dict[str, Any]:
    """Convenience wrapper for a single edge definition."""
    return ensure_named_graph(
        base_url=base_url,
        name=name,
        edge_definitions=[
            {
                "collection": edge_collection,
                "from": from_collections,
                "to": to_collections,
            }
        ],
        basic_auth_user=basic_auth_user,
        basic_auth_password=basic_auth_password,
        verify_tls=verify_tls,
        timeout_seconds=timeout_seconds,
    )
