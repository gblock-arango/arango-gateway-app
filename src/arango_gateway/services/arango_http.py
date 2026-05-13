"""HTTP helpers for testing outbound connectivity to Arango and JSON REST calls."""

from __future__ import annotations

import base64
import json
import ssl
import time
from typing import Any
from urllib import error, request


def ping_arango_endpoint(
    protocol: str,
    ip_address: str,
    port: int,
    path: str = "/_api/version",
    timeout_seconds: float = 5.0,
    basic_auth_user: str | None = None,
    basic_auth_password: str | None = None,
    verify_tls: bool = True,
) -> dict:
    """Probe Arango endpoint and return latency + response details."""
    normalized_path = path if path.startswith("/") else f"/{path}"
    url = f"{protocol}://{ip_address}:{port}{normalized_path}"

    started = time.perf_counter()
    req = request.Request(url=url, method="GET")
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
    if protocol == "https" and not verify_tls:
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

    try:
        open_kw: dict = {"timeout": timeout_seconds}
        if ssl_ctx is not None:
            open_kw["context"] = ssl_ctx

        with request.urlopen(req, **open_kw) as resp:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            body_text = resp.read(2048).decode("utf-8", errors="replace")
            return {
                "reachable": True,
                "url": url,
                "status_code": resp.getcode(),
                "latency_ms": elapsed_ms,
                "response_preview": _preview_json_or_text(body_text),
            }
    except error.HTTPError as exc:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        body_text = exc.read(2048).decode("utf-8", errors="replace")
        return {
            "reachable": False,
            "url": url,
            "status_code": exc.code,
            "latency_ms": elapsed_ms,
            "error": f"HTTP error: {exc.reason}",
            "response_preview": _preview_json_or_text(body_text),
        }
    except Exception as exc:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return {
            "reachable": False,
            "url": url,
            "latency_ms": elapsed_ms,
            "error": str(exc),
        }


def _preview_json_or_text(raw_text: str) -> str:
    """Normalize preview to compact JSON string when possible."""
    text = raw_text.strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
        return json.dumps(parsed, separators=(",", ":"), sort_keys=True)[:512]
    except Exception:
        return text[:512]


def _normalize_arango_base_url(base_url: str) -> str:
    return (base_url or "").strip().rstrip("/")


def arango_json_request(
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
    """Perform an Arango REST call with JSON body/response (urllib).

    ``path`` must start with ``/`` (e.g. ``/_db/mydb/_api/collection``).
    Returns a dict with ``ok``, ``status_code``, and ``body`` (or ``error`` on transport failure).
    """
    normalized_path = path if path.startswith("/") else f"/{path}"
    url = f"{_normalize_arango_base_url(base_url)}{normalized_path}"
    m = method.upper().strip()
    data: bytes | None
    if payload is None or m == "GET":
        data = None
    else:
        data = json.dumps(payload).encode("utf-8")

    req = request.Request(url=url, method=m, data=data)
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
