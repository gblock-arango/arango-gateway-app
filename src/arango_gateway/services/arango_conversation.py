"""
Arango ADA / cluster-backed conversation (HTTP boundary for the dashboard).

When ``ARANGO_CONVERSATION_URL`` is unset, returns a stub so the UI and route can be
exercised without a cluster. When set, POSTs JSON to that URL and normalizes the JSON
body for the same response shape as :func:`genie_conversation.ask_genie_conversation`.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Mapping
from urllib import error, request

logger = logging.getLogger(__name__)


def _message_dict_from_remote(msg: Any) -> dict[str, Any]:
    if msg is None:
        return {"content": ""}
    if isinstance(msg, str):
        return {"content": msg}
    if isinstance(msg, dict):
        return msg
    return {"content": str(msg)}


def _post_remote_conversation(
    url: str,
    payload: dict[str, Any],
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(url=url.strip(), data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    try:
        with request.urlopen(req, timeout=max(1.0, timeout_seconds)) as resp:
            raw = resp.read(65536).decode("utf-8", errors="replace")
    except error.HTTPError as exc:
        detail = exc.read(4096).decode("utf-8", errors="replace")
        logger.warning("Arango conversation HTTP %s: %s", exc.code, detail[:500])
        return {
            "ok": False,
            "error": f"HTTP {exc.code}: {exc.reason or 'error'} — {detail[:800]}",
        }
    except Exception as exc:
        logger.warning("Arango conversation request failed: %s", exc)
        return {"ok": False, "error": str(exc)}

    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"Invalid JSON from Arango conversation URL: {exc}"}

    if not isinstance(data, dict):
        return {"ok": False, "error": "Remote response must be a JSON object"}

    if data.get("ok") is False:
        return {"ok": False, "error": str(data.get("error") or "error")}

    msg_raw = data.get("message")
    if msg_raw is None and "reply" in data:
        msg_raw = data.get("reply")
    if msg_raw is None:
        if data.get("error"):
            return {"ok": False, "error": str(data["error"])}
        return {"ok": False, "error": "Remote response missing message/reply"}

    msg = _message_dict_from_remote(msg_raw)
    cid = data.get("conversation_id")
    cid = str(cid).strip() if cid is not None else ""
    mid = data.get("message_id")
    mid = str(mid).strip() if mid is not None else ""
    return {
        "ok": True,
        "conversation_id": cid or None,
        "message_id": mid or None,
        "message": msg,
    }


def ask_arango_conversation(
    *,
    content: str,
    conversation_id: str | None,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    """
    Send ``content`` to the configured Arango conversation endpoint (or stub).

    Mirrors the Genie chat JSON shape: ``ok``, ``conversation_id``, ``message`` (dict),
    optional ``error``.
    """
    text = (content or "").strip()
    if not text:
        return {"ok": False, "error": "content is empty"}

    url = str(config.get("ARANGO_CONVERSATION_URL") or "").strip()
    timeout = float(config.get("ARANGO_CONVERSATION_TIMEOUT_SECONDS") or 120.0)

    payload: dict[str, Any] = {"content": text}
    if conversation_id:
        payload["conversation_id"] = str(conversation_id).strip()

    if url:
        result = _post_remote_conversation(url, payload, timeout_seconds=timeout)
        if result.get("ok") and not result.get("conversation_id"):
            result["conversation_id"] = str(uuid.uuid4())
        return result

    # Local development stub until minikube / ADA is wired.
    cid = (conversation_id or "").strip() or str(uuid.uuid4())
    stub = (
        "Arango AI (stub): received your message. Set **ARANGO_CONVERSATION_URL** to your cluster "
        "ADA chat HTTPS URL. The dashboard usually proxies chat via **arango-mcp-app** "
        "(configure the URL there); this gateway route is for direct callers.\n\n"
        f"---\n\n{text[:2000]}"
    )
    return {
        "ok": True,
        "conversation_id": cid,
        "message_id": "arango-stub",
        "message": {"content": stub, "status": "SUCCEEDED"},
    }
