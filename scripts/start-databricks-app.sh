#!/usr/bin/env bash
# Databricks Apps: gunicorn on platform port.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${DATABRICKS_APP_PORT:-8000}"
export PYTHONPATH="${ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"
exec gunicorn wsgi:app --bind "0.0.0.0:${PORT}" --workers 4 --timeout 660
