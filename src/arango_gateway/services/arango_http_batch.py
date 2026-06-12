"""Execute multiple Arango REST calls with one registry lookup and optional parallelism."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from arango_gateway.services.arango_http import arango_json_request
from arango_gateway.services.arango_proxy_path import arango_http_proxy_path_allowed

logger = logging.getLogger(__name__)

_ARANGO_HTTP_PROXY_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
# Collection / graph already exists — treat as idempotent success during bootstrap.
_DUPLICATE_ERROR_NUMS = frozenset({1207, 1925, 1932, 1946, 1948})


def _result_ok(result: dict[str, Any]) -> bool:
    if result.get("ok"):
        return True
    body = result.get("body")
    if isinstance(body, dict):
        code = body.get("errorNum")
        if code in _DUPLICATE_ERROR_NUMS:
            return True
        msg = str(body.get("errorMessage") or "").lower()
        if "duplicate" in msg and "collection" in msg:
            return True
    status = result.get("status_code")
    return status in (409,)


def execute_arango_http_batch(
    *,
    base_url: str,
    requests: list[dict[str, Any]],
    basic_auth_user: str | None,
    basic_auth_password: str | None,
    verify_tls: bool,
    timeout_seconds: float,
    allow_admin: bool,
    parallel: bool = True,
    max_workers: int = 8,
    stop_on_error: bool = False,
) -> dict[str, Any]:
    """
    Run many Arango REST operations against one cluster origin.

    Each item: ``{"method": "POST", "path": "/_db/.../_api/collection", "body": {...}}``.
    """
    if not requests:
        return {"ok": True, "count": 0, "failed": 0, "results": []}

    normalized: list[dict[str, Any]] = []
    for i, item in enumerate(requests):
        method = str(item.get("method", "GET")).upper().strip()
        if method not in _ARANGO_HTTP_PROXY_METHODS:
            raise ValueError(f"requests[{i}].method unsupported: {method}")
        raw_path = str(item.get("path", "")).strip()
        path = raw_path if raw_path.startswith("/") else f"/{raw_path}"
        if not arango_http_proxy_path_allowed(path, allow_admin=allow_admin):
            raise ValueError(f"requests[{i}].path not allowed: {path}")
        body = item.get("body")
        if body is None and "json" in item:
            body = item.get("json")
        normalized.append({"method": method, "path": path, "body": body})

    workers = max(1, min(int(max_workers or 8), 16, len(normalized)))

    def _run_one(index: int, spec: dict[str, Any]) -> dict[str, Any]:
        result = arango_json_request(
            method=spec["method"],
            base_url=base_url,
            path=spec["path"],
            payload=spec.get("body"),
            basic_auth_user=basic_auth_user,
            basic_auth_password=basic_auth_password,
            verify_tls=verify_tls,
            timeout_seconds=timeout_seconds,
        )
        return {
            "index": index,
            "method": spec["method"],
            "path": spec["path"],
            "ok": _result_ok(result),
            "status_code": result.get("status_code"),
            "body": result.get("body"),
            "error": result.get("error"),
        }

    results: list[dict[str, Any] | None] = [None] * len(normalized)
    failed = 0

    if parallel and len(normalized) > 1 and workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_run_one, i, spec): i for i, spec in enumerate(normalized)
            }
            for fut in as_completed(futures):
                idx = futures[fut]
                row = fut.result()
                results[idx] = row
                if not row.get("ok"):
                    failed += 1
                    if stop_on_error:
                        break
    else:
        for i, spec in enumerate(normalized):
            row = _run_one(i, spec)
            results[i] = row
            if not row.get("ok"):
                failed += 1
                if stop_on_error:
                    break

    filled = [r for r in results if r is not None]
    ok = failed == 0
    if not ok:
        logger.warning(
            "arango http batch completed with %d/%d failures",
            failed,
            len(filled),
        )
    return {
        "ok": ok,
        "count": len(filled),
        "failed": failed,
        "parallel": parallel and len(normalized) > 1 and workers > 1,
        "max_workers": workers,
        "results": filled,
    }
