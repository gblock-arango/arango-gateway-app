#!/usr/bin/env bash
# Local gateway on http://127.0.0.1:8001
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/load-app-yaml-env.sh
source "${ROOT}/scripts/load-app-yaml-env.sh"

export PYTHONPATH="${ROOT}/src${PYTHONPATH:+:${PYTHONPATH}}"
export PORT="${LOCAL_GATEWAY_PORT:-8001}"
export DATABRICKS_APP_PORT="${PORT}"

UV="${ROOT}/.venv/bin/python"
if [[ ! -x "${UV}" ]]; then
  echo "error: missing ${ROOT}/.venv — run: ./scripts/build-local.sh" >&2
  exit 1
fi

echo "==> arango-gateway-app http://127.0.0.1:${PORT}"
cd "${ROOT}"
exec "${UV}" app.py
