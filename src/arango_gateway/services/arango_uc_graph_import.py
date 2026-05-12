"""
Bulk-load UC metadata graph nodes/edges into ArangoDB via the HTTP API.

Vertex collection holds DataHub-style node dicts (``id`` matches ``_key``).
Edge collection uses ``_from`` / ``_to`` pointing at that vertex collection.
"""

from __future__ import annotations

import base64
import json
import ssl
from typing import Any, Callable, Iterator
from urllib import error, request

ProgressCallback = Callable[[int, int], None]


def _normalize_base_url(base_url: str) -> str:
    return (base_url or "").strip().rstrip("/")


def _arango_json_call(
    *,
    method: str,
    base_url: str,
    path: str,
    payload: Any | None,
    basic_auth_user: str | None,
    basic_auth_password: str | None,
    verify_tls: bool,
    timeout_seconds: float,
) -> dict[str, Any]:
    """HTTP JSON request; path must start with / (e.g. /_db/mydb/_api/collection)."""
    url = f"{_normalize_base_url(base_url)}{path}"
    data: bytes | None
    if payload is None:
        data = None
    else:
        data = json.dumps(payload).encode("utf-8")

    req = request.Request(url=url, method=method, data=data)
    if data is not None:
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
            body: Any = json.loads(body_text) if body_text.strip() else {}
            return {
                "ok": True,
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


def _collection_exists_error(body: dict[str, Any], status_code: int | None) -> bool:
    if status_code == 409:
        return True
    # duplicate name / arango errors vary by version
    if body.get("errorNum") in (1207, 1924, 1925):
        return True
    msg = str(body.get("errorMessage", "")).lower()
    return "duplicate" in msg and "name" in msg


def ensure_arango_collection(
    *,
    base_url: str,
    database: str,
    name: str,
    collection_type: int,
    basic_auth_user: str | None,
    basic_auth_password: str | None,
    verify_tls: bool,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Create collection if missing (type 2=document, 3=edge)."""
    db = database.strip() or "_system"
    path = f"/_db/{db}/_api/collection"
    result = _arango_json_call(
        method="POST",
        base_url=base_url,
        path=path,
        payload={"name": name, "type": collection_type},
        basic_auth_user=basic_auth_user,
        basic_auth_password=basic_auth_password,
        verify_tls=verify_tls,
        timeout_seconds=timeout_seconds,
    )
    if result.get("ok"):
        return result
    body = result.get("body") or {}
    if _collection_exists_error(body, result.get("status_code")):
        return {
            "ok": True,
            "already_existed": True,
            "status_code": result.get("status_code"),
            "body": body,
        }
    return result


def _batch_insert_documents(
    *,
    base_url: str,
    database: str,
    collection: str,
    documents: list[dict[str, Any]],
    basic_auth_user: str | None,
    basic_auth_password: str | None,
    verify_tls: bool,
    timeout_seconds: float,
) -> dict[str, Any]:
    if not documents:
        return {"ok": True, "status_code": 202, "body": []}
    db = database.strip() or "_system"
    coll = collection.strip()
    qs = "overwriteMode=replace&waitForSync=false&returnNew=false"
    path = f"/_db/{db}/_api/document/{coll}?{qs}"
    result = _arango_json_call(
        method="POST",
        base_url=base_url,
        path=path,
        payload=documents,
        basic_auth_user=basic_auth_user,
        basic_auth_password=basic_auth_password,
        verify_tls=verify_tls,
        timeout_seconds=timeout_seconds,
    )
    if not result.get("ok"):
        return result
    body = result.get("body")
    if isinstance(body, list):
        for item in body:
            if isinstance(item, dict) and item.get("error") is True:
                return {
                    "ok": False,
                    "status_code": result.get("status_code"),
                    "body": item,
                    "error": item.get("errorMessage", "batch item error"),
                }
    elif isinstance(body, dict) and body.get("error") is True:
        return {
            "ok": False,
            "status_code": result.get("status_code"),
            "body": body,
        }
    return result


def _node_document(node: dict[str, Any]) -> dict[str, Any]:
    nid = node.get("id")
    if not nid:
        raise ValueError("node missing id")
    doc = dict(node)
    doc["_key"] = str(nid)
    return doc


def _edge_document(edge: dict[str, Any], vertex_collection: str) -> dict[str, Any]:
    eid = edge.get("id")
    fid = edge.get("from_id")
    tid = edge.get("to_id")
    if not eid or fid is None or tid is None:
        raise ValueError("edge missing id/from_id/to_id")
    vc = vertex_collection.strip()
    return {
        "_key": str(eid),
        "_from": f"{vc}/{fid}",
        "_to": f"{vc}/{tid}",
        "id": eid,
        "relationship_type": edge.get("relationship_type"),
        "properties": edge.get("properties"),
    }


def iter_uc_graph_import_events(
    *,
    base_url: str,
    database: str,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    vertex_collection: str = "uc_graph_nodes",
    edge_collection: str = "uc_graph_edges",
    batch_size: int = 300,
    basic_auth_user: str | None = None,
    basic_auth_password: str | None = None,
    verify_tls: bool = True,
    timeout_seconds: float = 120.0,
) -> Iterator[dict[str, Any]]:
    """
    Yields ``{"kind": "progress", "posted": int, "total": int}`` after each batch, then
    ``{"kind": "complete", "result": dict}``. On setup/insert failure yields
    ``{"kind": "error", "step": str, "detail": dict}`` and stops.
    """
    if batch_size < 1:
        batch_size = 1
    vc = vertex_collection.strip() or "uc_graph_nodes"
    ec = edge_collection.strip() or "uc_graph_edges"
    total = len(nodes) + len(edges)
    posted = 0

    cr_v = ensure_arango_collection(
        base_url=base_url,
        database=database,
        name=vc,
        collection_type=2,
        basic_auth_user=basic_auth_user,
        basic_auth_password=basic_auth_password,
        verify_tls=verify_tls,
        timeout_seconds=timeout_seconds,
    )
    if not cr_v.get("ok"):
        yield {
            "kind": "error",
            "step": "ensure_vertex_collection",
            "detail": cr_v,
        }
        return

    cr_e = ensure_arango_collection(
        base_url=base_url,
        database=database,
        name=ec,
        collection_type=3,
        basic_auth_user=basic_auth_user,
        basic_auth_password=basic_auth_password,
        verify_tls=verify_tls,
        timeout_seconds=timeout_seconds,
    )
    if not cr_e.get("ok"):
        yield {
            "kind": "error",
            "step": "ensure_edge_collection",
            "detail": cr_e,
        }
        return

    for i in range(0, len(nodes), batch_size):
        chunk = nodes[i : i + batch_size]
        docs = [_node_document(n) for n in chunk]
        ins = _batch_insert_documents(
            base_url=base_url,
            database=database,
            collection=vc,
            documents=docs,
            basic_auth_user=basic_auth_user,
            basic_auth_password=basic_auth_password,
            verify_tls=verify_tls,
            timeout_seconds=timeout_seconds,
        )
        if not ins.get("ok"):
            yield {"kind": "error", "step": "insert_nodes", "detail": ins}
            return
        posted += len(chunk)
        yield {"kind": "progress", "posted": posted, "total": total}

    for i in range(0, len(edges), batch_size):
        chunk = edges[i : i + batch_size]
        docs = [_edge_document(e, vc) for e in chunk]
        ins = _batch_insert_documents(
            base_url=base_url,
            database=database,
            collection=ec,
            documents=docs,
            basic_auth_user=basic_auth_user,
            basic_auth_password=basic_auth_password,
            verify_tls=verify_tls,
            timeout_seconds=timeout_seconds,
        )
        if not ins.get("ok"):
            yield {"kind": "error", "step": "insert_edges", "detail": ins}
            return
        posted += len(chunk)
        yield {"kind": "progress", "posted": posted, "total": total}

    yield {
        "kind": "complete",
        "result": {
            "ok": True,
            "vertex_collection": vc,
            "edge_collection": ec,
            "nodes_written": len(nodes),
            "edges_written": len(edges),
            "database": database.strip() or "_system",
        },
    }


def import_uc_graph_to_arango(
    *,
    base_url: str,
    database: str,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    vertex_collection: str = "uc_graph_nodes",
    edge_collection: str = "uc_graph_edges",
    batch_size: int = 300,
    basic_auth_user: str | None = None,
    basic_auth_password: str | None = None,
    verify_tls: bool = True,
    timeout_seconds: float = 120.0,
    on_progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """
    Insert all vertices then edges. ``on_progress(posted, total)`` fires after each batch
    (posted = cumulative documents written, total = len(nodes)+len(edges)).
    """
    final: dict[str, Any] | None = None
    for ev in iter_uc_graph_import_events(
        base_url=base_url,
        database=database,
        nodes=nodes,
        edges=edges,
        vertex_collection=vertex_collection,
        edge_collection=edge_collection,
        batch_size=batch_size,
        basic_auth_user=basic_auth_user,
        basic_auth_password=basic_auth_password,
        verify_tls=verify_tls,
        timeout_seconds=timeout_seconds,
    ):
        k = ev.get("kind")
        if k == "progress" and on_progress is not None:
            on_progress(int(ev["posted"]), int(ev["total"]))
        elif k == "complete":
            final = ev.get("result") or {}
        elif k == "error":
            return {
                "ok": False,
                "step": ev.get("step", "unknown"),
                "detail": ev.get("detail"),
            }
    return final or {"ok": False, "step": "internal", "detail": {}}
