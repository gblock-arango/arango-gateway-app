"""Allowlist for ``POST /api/arango/http`` forwarded paths."""

from __future__ import annotations


def arango_http_proxy_path_allowed(path: str, *, allow_admin: bool) -> bool:
    """Return True if ``path`` may be forwarded to Arango (query string ignored)."""
    if not path or ".." in path or "\x00" in path:
        return False
    base = path.split("?", 1)[0]
    if not base.startswith("/"):
        base = f"/{base}"
    parts = [p for p in base.split("/") if p]
    # /_db/<db>/_api/... or /_db/<db>/_admin/...
    if len(parts) >= 3 and parts[0] == "_db" and parts[2] in ("_api", "_admin"):
        if parts[2] == "_admin":
            return allow_admin
        return True
    if parts and parts[0] == "_api":
        return True
    # Server-level admin used by cluster / hot backup (narrow prefixes).
    if base.startswith("/_admin/cluster") or base.startswith("/_admin/server/"):
        return True
    if base.startswith("/_admin/backup"):
        return True
    if parts and parts[0] == "_admin":
        return allow_admin
    return False
