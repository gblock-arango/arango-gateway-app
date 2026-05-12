"""Same-origin reverse proxy: Arango Web UI in an iframe with server-side Basic auth."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

import requests
from flask import Blueprint, Response, current_app, redirect, request

from arango_gateway.config import _DEFAULT_ARANGO_PING_BASIC_AUTH_PASSWORD
from arango_gateway.services.arango_registry import resolve_arango_http_origin

HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)

REDIRECT_STATI = frozenset({301, 302, 303, 307, 308})
EMBED_PREFIX = "/embedded-arango"

arango_embed_bp = Blueprint(
    "arango_embed",
    __name__,
    url_prefix=EMBED_PREFIX,
)


def _upstream_url_variants(origin: str) -> list[str]:
    out: list[str] = [origin.rstrip("/")]
    parts = urlsplit(origin)
    if parts.port is None and parts.scheme and parts.hostname:
        d = 443 if parts.scheme == "https" else 80
        out.append(f"{parts.scheme}://{parts.hostname}:{d}")
    elif parts.port is not None and (
        (parts.scheme == "https" and parts.port == 443)
        or (parts.scheme == "http" and parts.port == 80)
    ):
        out.append(f"{parts.scheme}://{parts.hostname}")
    seen: set[str] = set()
    uniq: list[str] = []
    for u in sorted(set(out), key=len, reverse=True):
        if u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def _is_rewritable_media(content_type: str) -> bool:
    base = (content_type or "").split(";")[0].strip().lower()
    if base.startswith("text/"):
        return True
    if base in (
        "application/javascript",
        "application/json",
        "application/x-javascript",
    ):
        return True
    if "javascript" in base or base.endswith("+json"):
        return True
    return False


def _fix_double_proxy_path_segments(text: str) -> str:
    """
    Undo mistaken double ``/embedded-arango`` segments from JS string concatenation.

    Aardvark often does ``base + "/_api/..."`` where ``base`` already includes the
    proxy prefix; rewriting bare ``"/_api/`` to ``"/embedded-arango/_api/`` then
    produces ``.../_system/embedded-arango/_api/`` and 404s.
    """
    # DB name must be a single path segment (no `/`, `"`, or `+`), otherwise
    # `[^/]+` can span `"+"/` in minified JS and strip a legitimate prefix.
    text = re.sub(
        r"(/_db/[^/\"+]+/)embedded-arango/(?=_(?:api|open|admin)/)",
        r"\1",
        text,
    )
    while "/embedded-arango/embedded-arango/" in text:
        text = text.replace("/embedded-arango/embedded-arango/", "/embedded-arango/")
    return text


def _normalize_proxy_subpath_duplication(path: str) -> str:
    """
    Collapse ``_db/<db>/embedded-arango/_api|_open|_admin/`` → ``_db/<db>/_api|...``.

    Aardvark often builds URLs as ``base + second`` at runtime; our rewriter adds
    ``/embedded-arango`` to the second fragment, which yields a bad path that never
    appears as one contiguous substring in the downloaded JS.
    """
    p = path
    while True:
        while "/embedded-arango/embedded-arango/" in p:
            p = p.replace("/embedded-arango/embedded-arango/", "/embedded-arango/")
        n, n_subs = re.subn(
            r"(_db/[^/]+/)embedded-arango/(_api/|_open/|_admin/)",
            r"\1\2",
            p,
        )
        if n_subs == 0:
            return n
        p = n


def _prefix_quoted_arango_paths(text: str, pref: str) -> str:
    """
    Prefix root-absolute Arango paths so XHR/fetch hit ``/embedded-arango/...``.

    Skip when the path is already right after ``pref``, or when ``/_api|_open|_admin``
    immediately follows ``_system/`` inside the same string (those segments were
    already reached via a prefixed ``/_db/...``).
    """

    def repl(m: re.Match[str]) -> str:
        q, path = m.group(1), m.group(2)
        i = m.start(2)
        if i >= len(pref) and text[i - len(pref) : i] == pref:
            return m.group(0)
        if path.startswith(("/_api/", "/_open/", "/_admin/")):
            if i >= 8 and text[i - 8 : i] == "_system/":
                return m.group(0)
        return q + pref + path

    text = re.sub(r'(["\`])(/_(?:db|api|open|admin)/)', repl, text)

    def repl_url(m: re.Match[str]) -> str:
        path = m.group(2)
        i = m.start(2)
        if i >= len(pref) and text[i - len(pref) : i] == pref:
            return m.group(0)
        if path.startswith(("/_api/", "/_open/", "/_admin/")):
            if i >= 8 and text[i - 8 : i] == "_system/":
                return m.group(0)
        return m.group(1) + pref + path

    text = re.sub(r"(url\(\s*)(/_(?:db|api|open|admin)/)", repl_url, text)
    return text


def _rewrite_text_body(body: bytes, content_type: str, origin: str, embed_prefix: str) -> bytes:
    if not _is_rewritable_media(content_type):
        return body
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return body
    pref = embed_prefix.rstrip("/")
    # Replace absolute upstream origin URLs with the same-origin proxy prefix.
    for variant in _upstream_url_variants(origin):
        text = text.replace(variant, pref)

    text = _prefix_quoted_arango_paths(text, pref)
    text = _fix_double_proxy_path_segments(text)
    return text.encode("utf-8")


def _rewrite_location(loc: str, origin: str) -> str:
    loc = (loc or "").strip()
    if not loc:
        return loc
    if loc.startswith("/"):
        return f"{EMBED_PREFIX}{loc}"
    for variant in _upstream_url_variants(origin):
        if loc.startswith(variant + "/") or loc == variant:
            tail = loc[len(variant) :].lstrip("/")
            return f"{EMBED_PREFIX}/{tail}" if tail else f"{EMBED_PREFIX}/"
    return loc


def _embed_basic_credentials() -> tuple[str, str]:
    user = (current_app.config.get("ARANGO_PING_BASIC_AUTH_USER") or "").strip() or "root"
    pw_raw = current_app.config.get("ARANGO_PING_BASIC_AUTH_PASSWORD")
    if pw_raw is None or str(pw_raw).strip() == "":
        password = _DEFAULT_ARANGO_PING_BASIC_AUTH_PASSWORD
    else:
        password = str(pw_raw)
    return user, password


def _proxy_upstream(subpath: str) -> Response:
    origin = resolve_arango_http_origin(
        env_override=current_app.config.get("ARANGO_UI_IFRAME_URL", ""),
        registry_table=current_app.config["ARANGO_REGISTRY_TABLE"],
        warehouse_id=current_app.config["DATABRICKS_SQL_WAREHOUSE_ID"],
        auto_create_registry=current_app.config.get("ARANGO_REGISTRY_AUTO_CREATE", True),
    )
    if not origin:
        return Response(
            "<!doctype html><title>Arango embed</title>"
            "<p>Arango upstream is not configured (no registry row or override).</p>",
            status=502,
            mimetype="text/html",
        )

    user, password = _embed_basic_credentials()
    verify_tls = bool(current_app.config.get("ARANGO_PING_TLS_VERIFY", True))

    path = _normalize_proxy_subpath_duplication(subpath.lstrip("/"))
    upstream = f"{origin.rstrip('/')}/{path}"
    if request.query_string:
        upstream = f"{upstream}?{request.query_string.decode()}"

    out_headers: dict[str, str] = {}
    for k, v in request.headers.items():
        kl = k.lower()
        if kl in HOP_BY_HOP or kl == "host":
            continue
        out_headers[k] = v

    parts = urlsplit(origin)
    out_headers["Host"] = parts.netloc.split("@")[-1]

    try:
        r = requests.request(
            method=request.method,
            url=upstream,
            headers=out_headers,
            data=request.get_data(),
            auth=(user, password),
            verify=verify_tls,
            timeout=(15, 180),
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        return Response(f"Upstream error: {exc}", status=502)

    content = r.content
    ct = r.headers.get("Content-Type", "")
    if r.status_code not in REDIRECT_STATI:
        content = _rewrite_text_body(content, ct, origin, EMBED_PREFIX)

    out = Response(content, status=r.status_code)
    if r.status_code not in REDIRECT_STATI and ct:
        out.headers["Content-Type"] = ct

    for k, v in r.headers.items():
        kl = k.lower()
        if kl in HOP_BY_HOP or kl in ("content-length", "content-encoding", "content-type"):
            continue
        if kl == "location":
            out.headers["Location"] = _rewrite_location(v, origin)
            continue
        if kl == "set-cookie":
            v2 = re.sub(r";\s*Domain=[^;]+", "", v, flags=re.I)
            out.headers.add("Set-Cookie", v2)
            continue
        out.headers[k] = v

    out.headers["Content-Length"] = str(len(content))
    return out


@arango_embed_bp.route("/", defaults={"subpath": ""})
@arango_embed_bp.route(
    "/<path:subpath>",
    methods=[
        "GET",
        "HEAD",
        "POST",
        "PUT",
        "DELETE",
        "PATCH",
        "OPTIONS",
    ],
)
def proxy(subpath: str) -> Response:
    if not subpath:
        return redirect(
            f"{EMBED_PREFIX}/_db/_system/_admin/aardvark/index.html#login",
            code=302,
        )
    return _proxy_upstream(subpath)
