"""Flask application factory for the Arango gateway Databricks App."""

from __future__ import annotations

from flask import Flask, jsonify
from werkzeug.middleware.proxy_fix import ProxyFix

from arango_gateway.config import AppConfig
from arango_gateway.routes.api import api_blueprint
from arango_gateway.routes.embed import arango_embed_bp
from arango_gateway.services.startup_debug import run_startup_debug_check


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.from_object(AppConfig())

    @app.route("/health")
    def health_root():
        return jsonify({"status": "ok"})

    @app.after_request
    def add_security_headers(response):
        response.headers["X-Frame-Options"] = "ALLOWALL"
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return response

    app.register_blueprint(arango_embed_bp)
    app.register_blueprint(api_blueprint, url_prefix="/api")

    app.extensions["startup_debug_status"] = {
        "status": "not_run",
        "message": "Set DEBUG_STARTUP_CHECKS=true to run startup diagnostics.",
    }
    if app.config.get("DEBUG_STARTUP_CHECKS", False):
        app.extensions["startup_debug_status"] = run_startup_debug_check(app)

    app.wsgi_app = ProxyFix(
        app.wsgi_app,
        x_for=1,
        x_proto=1,
        x_host=1,
        x_port=1,
        x_prefix=1,
    )
    return app
