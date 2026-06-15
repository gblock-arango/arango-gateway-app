"""Flask application factory for the Arango gateway Databricks App."""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from flask import Flask, jsonify
from werkzeug.middleware.proxy_fix import ProxyFix

from arango_gateway.config import AppConfig
from arango_gateway.routes.api import api_blueprint
from arango_gateway.routes.embed import arango_embed_bp
from arango_gateway.services.gateway_url_registry import publish_self_gateway_url_to_uc_if_configured
from arango_gateway.services.startup_debug import run_startup_debug_check

log = logging.getLogger(__name__)
_STARTUP_LOCK = Path("/tmp/arango-gateway-startup.lock")


def _run_background_startup(app: Flask) -> None:
    """UC publish + optional Arango probe — must not block ``/health`` or gunicorn boot."""
    acquired = False
    try:
        fd = os.open(_STARTUP_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.close(fd)
        acquired = True
    except FileExistsError:
        log.info("gateway background startup skipped (another worker holds lock)")
        app.extensions["startup_debug_status"] = {
            "status": "skipped",
            "message": "Startup diagnostics ran on another worker.",
        }
        return

    try:
        publish_self_gateway_url_to_uc_if_configured(app)
        if app.config.get("DEBUG_STARTUP_CHECKS", False):
            app.extensions["startup_debug_status"] = run_startup_debug_check(app)
    except Exception:
        log.exception("gateway background startup failed")
        app.extensions["startup_debug_status"] = {
            "status": "error",
            "message": "Background startup failed — see app logs.",
        }
    finally:
        if acquired:
            _STARTUP_LOCK.unlink(missing_ok=True)


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.from_object(AppConfig())

    from arango_gateway.deployment_profile import arango_ping_tls_verify_default, is_local_dev

    if is_local_dev():
        app.config["ARANGO_PING_TLS_VERIFY"] = arango_ping_tls_verify_default()

    @app.route("/health")
    def health_root():
        return jsonify({"status": "ok"})

    @app.after_request
    def add_security_headers(response):
        # ``X-Frame-Options: ALLOWALL`` is not a valid directive; rely on CSP for embedders.
        response.headers.pop("X-Frame-Options", None)
        response.headers["Content-Security-Policy"] = "frame-ancestors *"
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return response

    app.register_blueprint(arango_embed_bp)
    app.register_blueprint(api_blueprint, url_prefix="/api")

    app.extensions["startup_debug_status"] = {
        "status": "pending",
        "message": "Startup diagnostics running in background…",
    }
    threading.Thread(
        target=_run_background_startup,
        args=(app,),
        name="gateway-background-startup",
        daemon=True,
    ).start()

    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=1,
        x_proto=1,
        x_host=1,
        x_port=1,
        x_prefix=1,
    )
    return app
