#!/usr/bin/env bash
# Pre-create UC registry tables and grant SELECT/MODIFY to the local_dev identity (CLI user).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# shellcheck source=scripts/load-app-yaml-env.sh
source "${SCRIPT_DIR}/load-app-yaml-env.sh"

if [[ -x "${ROOT}/.venv/bin/python3" ]]; then
  PYTHON_BIN="${ROOT}/.venv/bin/python3"
elif [[ -n "${VIRTUAL_ENV:-}" && -x "${VIRTUAL_ENV}/bin/python3" ]]; then
  PYTHON_BIN="${VIRTUAL_ENV}/bin/python3"
else
  PYTHON_BIN="python3"
fi
export PYTHON_BIN

PROFILE="${DATABRICKS_CONFIG_PROFILE:-}"
if [[ -n "${PROFILE}" ]]; then
  export PROFILE
  PROFILE_ARGS=(--profile "${PROFILE}")
else
  PROFILE_ARGS=()
fi
export PROFILE_ARGS

WAREHOUSE_ID="${DATABRICKS_SQL_WAREHOUSE_ID:-}"
if [[ -z "${WAREHOUSE_ID// }" ]]; then
  echo "ERROR: DATABRICKS_SQL_WAREHOUSE_ID is required for local_dev UC grants." >&2
  exit 1
fi

# shellcheck source=scripts/_databricks_sql_lib.sh
source "${SCRIPT_DIR}/_databricks_sql_lib.sh"

REGISTRY_TABLE="${ARANGO_REGISTRY_TABLE:-workspace.default.arango_connection_registry}"
ARANGO_GATEWAY_REGISTRY_TABLE="${ARANGO_GATEWAY_REGISTRY_TABLE:-workspace.default.arango_gateway_registry}"

resolve_cli_user() {
  local name="" json=""
  if [[ -n "${LOCAL_DEV_UC_GRANTEE:-}" ]]; then
    echo "${LOCAL_DEV_UC_GRANTEE}"
    return 0
  fi
  name="$(
    PYTHONPATH="${ROOT}/src" "${PYTHON_BIN}" -c "
import sys
try:
    from databricks.sdk import WorkspaceClient
    me = WorkspaceClient().current_user.me()
    print((me.user_name or '').strip())
except Exception:
    sys.exit(1)
" 2>/dev/null
  )" || true
  if [[ -n "${name}" ]]; then
    echo "${name}"
    return 0
  fi
  json="$(databricks current-user me -o json "${PROFILE_ARGS[@]}" 2>/dev/null || echo '{}')"
  name="$("${PYTHON_BIN}" -c 'import json,sys; d=json.load(sys.stdin); print((d.get("userName") or d.get("user_name") or "").strip())' <<< "${json}" 2>/dev/null || true)"
  if [[ -n "${name}" ]]; then
    echo "${name}"
    return 0
  fi
  return 1
}

CLI_USER="$(resolve_cli_user)" || {
  echo "ERROR: could not resolve CLI user (run 'databricks auth login' or set LOCAL_DEV_UC_GRANTEE)." >&2
  exit 1
}
GRANTEE="\`${CLI_USER}\`"
echo "local_dev UC grants for user '${CLI_USER}' (warehouse ${WAREHOUSE_ID})"

_failures=0
_run_grant() {
  local desc="$1"
  local sql="$2"
  local required="${3:-optional}"
  echo "==> ${desc}"
  if run_sql_statement "${sql}"; then
    return 0
  fi
  echo "WARNING: ${desc} failed — ${sql}" >&2
  if [[ "${required}" == "required" ]]; then
    _failures=$((_failures + 1))
  fi
  return 1
}

REGISTRY_CATALOG_PRE="$(echo "${REGISTRY_TABLE}" | cut -d. -f1)"
REGISTRY_SCHEMA_PRE="$(echo "${REGISTRY_TABLE}" | cut -d. -f2)"
GATEWAY_REGISTRY_CATALOG_PRE="$(echo "${ARANGO_GATEWAY_REGISTRY_TABLE}" | cut -d. -f1)"
GATEWAY_REGISTRY_SCHEMA_PRE="$(echo "${ARANGO_GATEWAY_REGISTRY_TABLE}" | cut -d. -f2)"

echo "Pre-creating UC registry schemas/tables (CLI user becomes owner when new)..."
_run_grant "CREATE SCHEMA ${REGISTRY_CATALOG_PRE}.${REGISTRY_SCHEMA_PRE}" \
  "CREATE SCHEMA IF NOT EXISTS \`${REGISTRY_CATALOG_PRE}\`.\`${REGISTRY_SCHEMA_PRE}\`" required || true
_run_grant "CREATE TABLE ${REGISTRY_TABLE}" \
  "CREATE TABLE IF NOT EXISTS ${REGISTRY_TABLE} (cluster_name STRING NOT NULL, ip_address STRING NOT NULL, port INT NOT NULL, protocol STRING NOT NULL, is_active BOOLEAN NOT NULL, updated_at TIMESTAMP NOT NULL) USING DELTA" required || true

if [[ "${GATEWAY_REGISTRY_CATALOG_PRE}.${GATEWAY_REGISTRY_SCHEMA_PRE}" != "${REGISTRY_CATALOG_PRE}.${REGISTRY_SCHEMA_PRE}" ]]; then
  _run_grant "CREATE SCHEMA ${GATEWAY_REGISTRY_CATALOG_PRE}.${GATEWAY_REGISTRY_SCHEMA_PRE}" \
    "CREATE SCHEMA IF NOT EXISTS \`${GATEWAY_REGISTRY_CATALOG_PRE}\`.\`${GATEWAY_REGISTRY_SCHEMA_PRE}\`" required || true
fi
_run_grant "CREATE TABLE ${ARANGO_GATEWAY_REGISTRY_TABLE}" \
  "CREATE TABLE IF NOT EXISTS ${ARANGO_GATEWAY_REGISTRY_TABLE} (base_url STRING NOT NULL, app_name STRING NOT NULL, is_active BOOLEAN NOT NULL, updated_at TIMESTAMP NOT NULL) USING DELTA" required || true

_run_grant "USE CATALOG on ${REGISTRY_CATALOG_PRE}" \
  "GRANT USE CATALOG ON CATALOG ${REGISTRY_CATALOG_PRE} TO ${GRANTEE}" required || true
_run_grant "USE SCHEMA on ${REGISTRY_CATALOG_PRE}.${REGISTRY_SCHEMA_PRE}" \
  "GRANT USE SCHEMA ON SCHEMA ${REGISTRY_CATALOG_PRE}.${REGISTRY_SCHEMA_PRE} TO ${GRANTEE}" required || true

for tbl in "${REGISTRY_TABLE}" "${ARANGO_GATEWAY_REGISTRY_TABLE}"; do
  _run_grant "SELECT, MODIFY on ${tbl}" \
    "GRANT SELECT, MODIFY ON TABLE ${tbl} TO ${GRANTEE}" required || true
done

if [[ "${_failures}" -gt 0 ]]; then
  echo "ERROR: ${_failures} required local_dev UC grant(s) failed." >&2
  exit 1
fi

echo "local_dev UC grants complete."
