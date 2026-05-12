"""Gunicorn entrypoint (lives under ``src/`` so ``PYTHONPATH=src`` does not depend on cwd)."""

from arango_gateway.app import create_app

app = create_app()
