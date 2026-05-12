"""HTTP helpers for testing outbound connectivity to Arango."""

from __future__ import annotations

import base64
import json
import ssl
import time
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
