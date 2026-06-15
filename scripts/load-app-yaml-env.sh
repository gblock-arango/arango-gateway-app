#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_YAML="${ROOT}/app.yaml"
PY="${PYTHON_BIN:-python3}"
SCRIPT="${ROOT}/scripts/read_app_yaml_env.py"

if [[ -f "${APP_YAML}" ]]; then
  while IFS= read -r line; do
    name="${line#export }"
    key="${name%%=*}"
    if [[ -z "${!key:-}" ]]; then
      eval "${line}"
    fi
  done < <("${PY}" "${SCRIPT}" --export-all "${APP_YAML}")
fi

if [[ -z "${LOCAL_WORKFLOW_DATA_ROOT:-}" ]]; then
  sibling="${ROOT}/../arango-workflow-app/local_dev/workflow-data"
  if [[ -d "${sibling}" ]]; then
    export LOCAL_WORKFLOW_DATA_ROOT="${sibling}"
  else
    export LOCAL_WORKFLOW_DATA_ROOT="${ROOT}/local_dev/workflow-data"
  fi
fi
mkdir -p "${LOCAL_WORKFLOW_DATA_ROOT}"

# Minikube Arango root password (local_dev gateway ping without Connection UI).
if [[ -z "${ARANGO_ROOT_PASSWORD_FILE:-}" ]]; then
  _mk_pw="${ROOT}/../single-node-arango-on-minikube/.state/arango-root-password.txt"
  if [[ -f "${_mk_pw}" ]]; then
    export ARANGO_ROOT_PASSWORD_FILE="${_mk_pw}"
  fi
fi
if [[ -n "${ARANGO_ROOT_PASSWORD_FILE:-}" && -f "${ARANGO_ROOT_PASSWORD_FILE}" ]]; then
  export ARANGO_PING_BASIC_AUTH_USER="${ARANGO_PING_BASIC_AUTH_USER:-root}"
  if [[ -z "${ARANGO_PING_BASIC_AUTH_PASSWORD:-}" ]]; then
    export ARANGO_PING_BASIC_AUTH_PASSWORD="$(head -n 1 "${ARANGO_ROOT_PASSWORD_FILE}" | tr -d '\r\n')"
  fi
fi
