#!/usr/bin/env bash
set -euo pipefail

# Upsert active Arango tunnel endpoint in Unity Catalog via Databricks SQL.
# Uses Databricks CLI auth (PAT via DATABRICKS_TOKEN or configured profile).
#
# Usage:
#   ./update_arango_registry_uc.sh [tunnel-url-or-host] [cluster-name] [table] [warehouse-id] [profile]
#
# Example:
#   ./update_arango_registry_uc.sh \
#     https://example.trycloudflare.com \
#     local-minikube-dev \
#     workspace.default.arango_connection_registry \
#     473d40703241ee4c

TUNNEL_INPUT="${1:-${ARANGO_TUNNEL_URL:-}}"
CLUSTER_NAME="${2:-local-minikube-dev}"
REGISTRY_TABLE="${3:-workspace.default.arango_connection_registry}"
WAREHOUSE_ID="${4:-${DATABRICKS_SQL_WAREHOUSE_ID:-473d40703241ee4c}}"
PROFILE="${5:-}"

if [[ -z "${TUNNEL_INPUT}" ]]; then
  echo "ERROR: tunnel URL/host is required (arg1 or ARANGO_TUNNEL_URL)." >&2
  exit 1
fi

if [[ -n "${PROFILE}" ]]; then
  PROFILE_ARGS=(--profile "${PROFILE}")
else
  PROFILE_ARGS=()
fi

if [[ "${TUNNEL_INPUT}" =~ ^https?:// ]]; then
  TUNNEL_HOST="${TUNNEL_INPUT#*://}"
else
  TUNNEL_HOST="${TUNNEL_INPUT}"
fi
TUNNEL_HOST="${TUNNEL_HOST%%/*}"

IFS='.' read -r CATALOG_NAME SCHEMA_NAME TABLE_NAME <<< "${REGISTRY_TABLE}"
if [[ -z "${CATALOG_NAME:-}" || -z "${SCHEMA_NAME:-}" || -z "${TABLE_NAME:-}" ]]; then
  echo "ERROR: REGISTRY_TABLE must be catalog.schema.table" >&2
  exit 1
fi

safe_sql_literal() {
  printf "%s" "$1" | sed "s/'/''/g"
}

run_sql() {
  local statement="$1"
  local payload
  payload="$(
    python3 -c 'import json,sys; print(json.dumps({"warehouse_id":sys.argv[1], "statement":sys.argv[2], "wait_timeout":"30s"}))' \
      "${WAREHOUSE_ID}" "${statement}"
  )"

  local response
  response="$(databricks api post /api/2.0/sql/statements --json "${payload}" "${PROFILE_ARGS[@]}")"

  local status statement_id
  status="$(python3 -c 'import json,sys; print((json.load(sys.stdin).get("status") or {}).get("state",""))' <<< "${response}")"
  statement_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("statement_id",""))' <<< "${response}")"

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

ESC_CLUSTER="$(safe_sql_literal "${CLUSTER_NAME}")"
ESC_HOST="$(safe_sql_literal "${TUNNEL_HOST}")"

echo "Ensuring registry schema/table exists..."
run_sql "CREATE SCHEMA IF NOT EXISTS \`${CATALOG_NAME}\`.\`${SCHEMA_NAME}\`"
run_sql "CREATE TABLE IF NOT EXISTS \`${CATALOG_NAME}\`.\`${SCHEMA_NAME}\`.\`${TABLE_NAME}\` (cluster_name STRING NOT NULL, ip_address STRING NOT NULL, port INT NOT NULL, protocol STRING NOT NULL, is_active BOOLEAN NOT NULL, updated_at TIMESTAMP NOT NULL) USING DELTA"

echo "Upserting active registry endpoint ${TUNNEL_HOST}:443..."
run_sql "UPDATE \`${CATALOG_NAME}\`.\`${SCHEMA_NAME}\`.\`${TABLE_NAME}\` SET is_active = FALSE WHERE is_active = TRUE"
run_sql "INSERT INTO \`${CATALOG_NAME}\`.\`${SCHEMA_NAME}\`.\`${TABLE_NAME}\` (cluster_name, ip_address, port, protocol, is_active, updated_at) VALUES ('${ESC_CLUSTER}', '${ESC_HOST}', 443, 'https', TRUE, current_timestamp())"

echo "Registry updated:"
echo "  cluster_name=${CLUSTER_NAME}"
echo "  ip_address=${TUNNEL_HOST}"
echo "  port=443"
echo "  protocol=https"
echo "  table=${REGISTRY_TABLE}"
