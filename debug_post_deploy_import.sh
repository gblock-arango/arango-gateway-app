#!/usr/bin/env bash
# UC + Arango fixture load (arango-import/*.jsonl). Used in two ways:
#
# 1) Sourced by deploy_app.sh after run_sql_statement is defined.
#    Expects: APP_NAME, PROFILE_ARGS, REGISTRY_TABLE, SCRIPT_DIR, DEBUG_IMPORT_*,
#             LOCAL_ARANGO_URL, ARANGO_PING_BASIC_AUTH_*, APP_SERVICE_PRINCIPAL_CLIENT_ID,
#             run_sql_statement
#
# 2) Run directly once the Databricks app is online (same CLI auth as databricks sync):
#      ./debug_post_deploy_import.sh
#      ./debug_post_deploy_import.sh my-app DEFAULT https://127.0.0.1:18529 workspace.default.arango_connection_registry <warehouse-id>
#    Env overrides: PROFILE, DEBUG_IMPORT_VOLUME_DIR, DEBUG_IMPORT_GRAPH_NAME, REGISTRY_TABLE,
#    WAREHOUSE_ID or DATABRICKS_SQL_WAREHOUSE_ID, APP_SERVICE_PRINCIPAL_CLIENT_ID (optional;
#    fetched via databricks apps get if unset), ARANGO_PING_BASIC_AUTH_*.

wait_for_flask_app_online() {
  local state=""
  echo "Waiting for Databricks app '${APP_NAME}' to reach RUNNING..."
  for _ in $(seq 1 72); do
    state="$(
      databricks apps get "${APP_NAME}" --output json "${PROFILE_ARGS[@]}" 2>/dev/null \
        | python3 -c 'import json,sys; print((json.load(sys.stdin).get("app_status") or {}).get("state",""))' 2>/dev/null || true
    )"
    if [[ "${state}" == "RUNNING" ]]; then
      echo "App is RUNNING."
      return 0
    fi
    sleep 5
  done
  echo "ERROR: App '${APP_NAME}' did not reach RUNNING in time (last state: '${state}')." >&2
  return 1
}

# Build CREATE TABLE AS ... from a local JSONL file without DBFS (works when public DBFS root is disabled).
uc_sql_create_table_from_jsonl() {
  local table_fqn="$1"
  local jsonl_path="$2"
  python3 - "$table_fqn" "$jsonl_path" <<'PY'
import sys
from pathlib import Path

def esc(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "''") + "'"

table, path = sys.argv[1], sys.argv[2]
lines = [ln for ln in Path(path).read_text(encoding="utf-8").splitlines() if ln.strip()]
if not lines:
    print(
        f"CREATE OR REPLACE TABLE {table} AS\n"
        "SELECT CAST(NULL AS VARIANT) AS doc WHERE false"
    )
else:
    rows = ",\n  ".join(f"({esc(ln)})" for ln in lines)
    print(
        f"CREATE OR REPLACE TABLE {table} AS\n"
        "SELECT parse_json(s) AS doc\n"
        f"FROM (VALUES\n  {rows}\n) AS uploaded_lines(s)"
    )
PY
}

# Debug-only: UC tables from arango-import JSONL + Arango HTTP bulk import against LOCAL_ARANGO_URL.
debug_post_deploy_import() {
  local imp_catalog imp_schema nodes_tbl edges_tbl vol_nodes vol_edges curl_tls=()

  wait_for_flask_app_online || return 1

  if [[ ! -f "${SCRIPT_DIR}/arango-import/nodes.jsonl" || ! -f "${SCRIPT_DIR}/arango-import/edges.jsonl" ]]; then
    echo "ERROR: Expected ${SCRIPT_DIR}/arango-import/{nodes,edges}.jsonl for debug import." >&2
    return 1
  fi

  IFS='.' read -r imp_catalog imp_schema _imp_tbl_rest <<< "${REGISTRY_TABLE}"
  if [[ -z "${imp_catalog:-}" || -z "${imp_schema:-}" ]]; then
    echo "ERROR: REGISTRY_TABLE must be catalog.schema.table (got '${REGISTRY_TABLE}')." >&2
    return 1
  fi

  nodes_tbl="${imp_catalog}.${imp_schema}.arango_import_nodes"
  edges_tbl="${imp_catalog}.${imp_schema}.arango_import_edges"

  echo "[debug import] Creating Unity Catalog tables ${nodes_tbl} and ${edges_tbl}..."
  if [[ -n "${DEBUG_IMPORT_VOLUME_DIR}" ]]; then
    vol_nodes="${DEBUG_IMPORT_VOLUME_DIR%/}/nodes.jsonl"
    vol_edges="${DEBUG_IMPORT_VOLUME_DIR%/}/edges.jsonl"
    echo "[debug import] Uploading JSONL to '${DEBUG_IMPORT_VOLUME_DIR}' (UC volume path)..."
    databricks fs mkdir "${DEBUG_IMPORT_VOLUME_DIR}" "${PROFILE_ARGS[@]}"
    databricks fs cp "${SCRIPT_DIR}/arango-import/nodes.jsonl" "${vol_nodes}" --overwrite "${PROFILE_ARGS[@]}"
    databricks fs cp "${SCRIPT_DIR}/arango-import/edges.jsonl" "${vol_edges}" --overwrite "${PROFILE_ARGS[@]}"
    run_sql_statement "CREATE OR REPLACE TABLE ${nodes_tbl} AS
SELECT * FROM read_files('${vol_nodes}', format => 'json', inferSchema => true)"
    run_sql_statement "CREATE OR REPLACE TABLE ${edges_tbl} AS
SELECT * FROM read_files('${vol_edges}', format => 'json', inferSchema => true)"
  else
    echo "[debug import] Using inlined JSONL (no DBFS upload). For large files set DEBUG_IMPORT_VOLUME_DIR=dbfs:/Volumes/..."
    run_sql_statement "$(uc_sql_create_table_from_jsonl "${nodes_tbl}" "${SCRIPT_DIR}/arango-import/nodes.jsonl")"
    run_sql_statement "$(uc_sql_create_table_from_jsonl "${edges_tbl}" "${SCRIPT_DIR}/arango-import/edges.jsonl")"
  fi

  if [[ -n "${APP_SERVICE_PRINCIPAL_CLIENT_ID:-}" ]]; then
    echo "[debug import] Granting SELECT on import tables to app service principal..."
    run_sql_statement "GRANT SELECT ON TABLE ${nodes_tbl} TO \`${APP_SERVICE_PRINCIPAL_CLIENT_ID}\`"
    run_sql_statement "GRANT SELECT ON TABLE ${edges_tbl} TO \`${APP_SERVICE_PRINCIPAL_CLIENT_ID}\`"
  fi

  local base="${LOCAL_ARANGO_URL%/}"
  if [[ "${base}" == https://* ]]; then
    curl_tls=(-k)
  fi

  echo "[debug import] Loading collections 'nodes' and 'edges' into Arango at ${base} ..."

  arango_post_json() {
    local path="$1"
    local body="$2"
    curl -sS "${curl_tls[@]}" -u "${ARANGO_PING_BASIC_AUTH_USER}:${ARANGO_PING_BASIC_AUTH_PASSWORD}" \
      -H 'Content-Type: application/json' \
      -X POST "${base}${path}" \
      -d "${body}"
  }

  arango_put_empty() {
    local path="$1"
    curl -sS "${curl_tls[@]}" -u "${ARANGO_PING_BASIC_AUTH_USER}:${ARANGO_PING_BASIC_AUTH_PASSWORD}" \
      -X PUT "${base}${path}" -o /dev/null
  }

  arango_import_file() {
    local collection="$1"
    local file="$2"
    curl -sS "${curl_tls[@]}" -u "${ARANGO_PING_BASIC_AUTH_USER}:${ARANGO_PING_BASIC_AUTH_PASSWORD}" \
      -X POST "${base}/_api/import?collection=${collection}&type=documents&onDuplicate=replace" \
      --data-binary "@${file}"
  }

  local resp
  resp="$(arango_post_json "/_api/collection" '{"name":"nodes","type":2}')"
  if ! echo "${resp}" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("error") is not True or d.get("errorNum") in (1207,) else 1)' 2>/dev/null; then
    echo "ERROR: Arango create collection 'nodes' failed: ${resp}" >&2
    return 1
  fi

  resp="$(arango_post_json "/_api/collection" '{"name":"edges","type":3}')"
  if ! echo "${resp}" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("error") is not True or d.get("errorNum") in (1207,) else 1)' 2>/dev/null; then
    echo "ERROR: Arango create edge collection 'edges' failed: ${resp}" >&2
    return 1
  fi

  arango_put_empty "/_api/collection/nodes/truncate"
  arango_put_empty "/_api/collection/edges/truncate"

  resp="$(arango_import_file "nodes" "${SCRIPT_DIR}/arango-import/nodes.jsonl")"
  if ! echo "${resp}" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("error") is False and int(d.get("errors",0))==0 else 1)' 2>/dev/null; then
    echo "ERROR: Arango import into 'nodes' failed: ${resp}" >&2
    return 1
  fi

  resp="$(arango_import_file "edges" "${SCRIPT_DIR}/arango-import/edges.jsonl")"
  if ! echo "${resp}" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get("error") is False and int(d.get("errors",0))==0 else 1)' 2>/dev/null; then
    echo "ERROR: Arango import into 'edges' failed: ${resp}" >&2
    return 1
  fi

  local graph_verify_tls="true"
  if [[ "${base}" == https://* ]]; then
    graph_verify_tls="false"
  fi

  echo "[debug import] Ensuring named graph '${DEBUG_IMPORT_GRAPH_NAME}' (edges: nodes -> nodes)..."
  if ! python3 -c "
import importlib.util
import sys
from pathlib import Path

root = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location(
    \"arango_graph\", root / \"src\" / \"arango_gateway\" / \"services\" / \"arango_graph.py\"
)
mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(mod)

base_url, user, password, gname, vtls = sys.argv[2:7]
verify = vtls == \"true\"
r = mod.ensure_named_graph(
    base_url=base_url,
    name=gname,
    edge_definitions=[{
        \"collection\": \"edges\",
        \"from\": [\"nodes\"],
        \"to\": [\"nodes\"],
    }],
    basic_auth_user=user or None,
    basic_auth_password=password if user else None,
    verify_tls=verify,
)
if not r.get(\"ok\"):
    print(r, file=sys.stderr)
sys.exit(0 if r.get(\"ok\") else 1)
" "${SCRIPT_DIR}" "${base}" "${ARANGO_PING_BASIC_AUTH_USER}" "${ARANGO_PING_BASIC_AUTH_PASSWORD}" "${DEBUG_IMPORT_GRAPH_NAME}" "${graph_verify_tls}"; then
    echo "ERROR: Arango named graph '${DEBUG_IMPORT_GRAPH_NAME}' could not be created." >&2
    return 1
  fi

  echo "[debug import] Done. UC: ${nodes_tbl}, ${edges_tbl}. Arango: collections nodes, edges, graph ${DEBUG_IMPORT_GRAPH_NAME}."
}

# --- Standalone entrypoint (not used when this file is sourced) ---
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    cat <<'EOF'
Usage:
  ./debug_post_deploy_import.sh [APP_NAME] [PROFILE] [LOCAL_ARANGO_URL] [REGISTRY_TABLE] [WAREHOUSE_ID]

Defaults match deploy_app.sh. PROFILE may be empty (use DATABRICKS_HOST + token env).
WAREHOUSE_ID uses arg5, then WAREHOUSE_ID, then DATABRICKS_SQL_WAREHOUSE_ID (no built-in default).

Environment (optional):
  DEBUG_IMPORT_VOLUME_DIR, DEBUG_IMPORT_GRAPH_NAME, ARANGO_PING_BASIC_AUTH_USER,
  ARANGO_PING_BASIC_AUTH_PASSWORD, APP_SERVICE_PRINCIPAL_CLIENT_ID (fetched if unset)

Requires: databricks CLI, curl, python3; arango-import/nodes.jsonl and edges.jsonl next to this script.
EOF
    exit 0
  fi

  set -euo pipefail

  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  APP_NAME="${1:-${APP_NAME:-arango-gateway-app}}"
  PROFILE="${2:-${PROFILE:-}}"
  LOCAL_ARANGO_URL="${3:-${LOCAL_ARANGO_URL:-https://127.0.0.1:18529}}"
  REGISTRY_TABLE="${4:-${REGISTRY_TABLE:-workspace.default.arango_connection_registry}}"
  WAREHOUSE_ID="${5:-${WAREHOUSE_ID:-${DATABRICKS_SQL_WAREHOUSE_ID:-}}}"

  DEBUG_IMPORT_VOLUME_DIR="${DEBUG_IMPORT_VOLUME_DIR:-}"
  DEBUG_IMPORT_GRAPH_NAME="${DEBUG_IMPORT_GRAPH_NAME:-debug_import_graph}"
  ARANGO_PING_BASIC_AUTH_USER="${ARANGO_PING_BASIC_AUTH_USER:-root}"
  ARANGO_PING_BASIC_AUTH_PASSWORD="${ARANGO_PING_BASIC_AUTH_PASSWORD:-8c1bc9344c886819859534a5ac951412c650870662228617cfbb69023489afd2}"

  if [[ -n "${PROFILE}" ]]; then
    PROFILE_ARGS=(--profile "${PROFILE}")
  else
    PROFILE_ARGS=()
  fi

  if [[ -z "${WAREHOUSE_ID// }" ]]; then
    echo "ERROR: WAREHOUSE_ID is required (arg5, env WAREHOUSE_ID, or DATABRICKS_SQL_WAREHOUSE_ID)." >&2
    exit 1
  fi

  if [[ -z "${APP_SERVICE_PRINCIPAL_CLIENT_ID:-}" ]]; then
    echo "Fetching app service principal for '${APP_NAME}'..."
    APP_JSON="$(databricks apps get "${APP_NAME}" --output json "${PROFILE_ARGS[@]}")"
    APP_SERVICE_PRINCIPAL_CLIENT_ID="$(
      python3 -c 'import json,sys; print(json.load(sys.stdin).get("service_principal_client_id",""))' <<< "${APP_JSON}"
    )"
    if [[ -z "${APP_SERVICE_PRINCIPAL_CLIENT_ID}" ]]; then
      echo "ERROR: Could not read service_principal_client_id from databricks apps get." >&2
      exit 1
    fi
  fi

  run_sql_statement() {
    local statement="$1"
    local payload
    payload="$(
      python3 -c 'import json,sys; print(json.dumps({"warehouse_id":sys.argv[1], "statement":sys.argv[2], "wait_timeout":"30s"}))' \
        "${WAREHOUSE_ID}" "${statement}"
    )"

    local response statement_id status
    response="$(databricks api post /api/2.0/sql/statements --json "${payload}" "${PROFILE_ARGS[@]}")"
    statement_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("statement_id",""))' <<< "${response}")"
    status="$(python3 -c 'import json,sys; print((json.load(sys.stdin).get("status") or {}).get("state",""))' <<< "${response}")"

    if [[ -z "${statement_id}" ]]; then
      echo "ERROR: SQL statement did not return statement_id" >&2
      echo "${response}" >&2
      exit 1
    fi

    for _ in $(seq 1 30); do
      if [[ "${status}" == "SUCCEEDED" ]]; then
        return 0
      fi
      if [[ "${status}" == "FAILED" || "${status}" == "CANCELED" || "${status}" == "CLOSED" ]]; then
        echo "ERROR: SQL statement ${statement_id} status=${status}" >&2
        databricks api get "/api/2.0/sql/statements/${statement_id}" "${PROFILE_ARGS[@]}" >&2 || true
        exit 1
      fi
      sleep 1
      response="$(databricks api get "/api/2.0/sql/statements/${statement_id}" "${PROFILE_ARGS[@]}")"
      status="$(python3 -c 'import json,sys; print((json.load(sys.stdin).get("status") or {}).get("state",""))' <<< "${response}")"
    done

    echo "ERROR: SQL statement ${statement_id} did not finish in time." >&2
    exit 1
  }

  echo "Standalone debug import: app='${APP_NAME}', Arango='${LOCAL_ARANGO_URL}', warehouse='${WAREHOUSE_ID}'"
  debug_post_deploy_import
fi
