#!/usr/bin/env bash
set -euo pipefail

# Publish the arango-gateway-app public HTTPS URL to Unity Catalog (for arango-dashboard-app
# and other consumers). Run as a human with SQL warehouse access (same auth as
# update_arango_registry_uc.sh). Optionally grant MODIFY to the gateway app SP so the
# running app can upsert the same table on startup.
#
# Usage:
#   ./update_arango_gateway_registry_uc.sh [base-url] [app-name] [table] [warehouse-id] [profile] [gateway-sp-client-id]
#
# Optional env: GATEWAY_REGISTRY_UC_UPSERT_RETRIES (default 10) — retries when Delta reports
# concurrent writes (e.g. deploy script vs gateway app startup publishing the same table).
#
# Example (from deploy_app.sh after apps get):
#     "https://arango-gateway-app-123.aws.databricksapps.com" \
#     arango-gateway-app \
#     workspace.default.arango_gateway_registry \
#     473d40703241ee4c \
#     "" \
#     "8d019ad9-0038-453c-927a-bc5297cea12d"

BASE_URL_INPUT="${1:-${DATABRICKS_APP_URL:-}}"
APP_NAME_INPUT="${2:-${DATABRICKS_APP_NAME:-arango-gateway-app}}"
REGISTRY_TABLE="${3:-${ARANGO_GATEWAY_REGISTRY_TABLE:-workspace.default.arango_gateway_registry}}"
WAREHOUSE_ID="${4:-${DATABRICKS_SQL_WAREHOUSE_ID:-473d40703241ee4c}}"
PROFILE="${5:-}"
GATEWAY_SP_ID="${6:-${APP_SERVICE_PRINCIPAL_CLIENT_ID:-}}"

if [[ -z "${BASE_URL_INPUT}" ]]; then
  echo "ERROR: gateway base URL required (arg1 or DATABRICKS_APP_URL)." >&2
  exit 1
fi

BASE_URL="${BASE_URL_INPUT%/}"

if [[ -n "${PROFILE}" ]]; then
  PROFILE_ARGS=(--profile "${PROFILE}")
else
  PROFILE_ARGS=()
fi

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
    return 1
  fi

  for _ in $(seq 1 30); do
    if [[ "${status}" == "SUCCEEDED" ]]; then
      return 0
    fi
    if [[ "${status}" == "FAILED" || "${status}" == "CANCELED" || "${status}" == "CLOSED" ]]; then
      echo "ERROR: SQL statement ${statement_id} status=${status}" >&2
      databricks api get "/api/2.0/sql/statements/${statement_id}" "${PROFILE_ARGS[@]}" >&2 || true
      return 1
    fi
    sleep 1
    response="$(databricks api get "/api/2.0/sql/statements/${statement_id}" "${PROFILE_ARGS[@]}")"
    status="$(python3 -c 'import json,sys; print((json.load(sys.stdin).get("status") or {}).get("state",""))' <<< "${response}")"
  done

  echo "ERROR: SQL statement ${statement_id} did not finish in time." >&2
  return 1
}

ESC_URL="$(safe_sql_literal "${BASE_URL}")"
ESC_APP="$(safe_sql_literal "${APP_NAME_INPUT}")"
FQTBL="\`${CATALOG_NAME}\`.\`${SCHEMA_NAME}\`.\`${TABLE_NAME}\`"

echo "Ensuring gateway URL registry schema/table exists..."
run_sql "CREATE SCHEMA IF NOT EXISTS \`${CATALOG_NAME}\`.\`${SCHEMA_NAME}\`" || exit 1
run_sql "CREATE TABLE IF NOT EXISTS ${FQTBL} (base_url STRING NOT NULL, app_name STRING NOT NULL, is_active BOOLEAN NOT NULL, updated_at TIMESTAMP NOT NULL) USING DELTA" || exit 1

echo "Granting SELECT, MODIFY on ${REGISTRY_TABLE} to \`account users\` (so laptop deploy can upsert even if the app SP created the table first)..."
if ! ( run_sql "GRANT SELECT, MODIFY ON TABLE ${FQTBL} TO \`account users\`" ); then
  echo "NOTE: GRANT to \`account users\` failed (ignore if you are not table owner). If deploy upsert fails, restart the gateway app once to let it grant, or DROP the table as metastore admin and redeploy." >&2
fi

echo "Upserting active gateway base URL into ${REGISTRY_TABLE}..."
# Same pattern as the gateway app on startup (UPDATE inactive + INSERT). Two writers
# often race right after deploy → DELTA_CONCURRENT_APPEND / ROW_LEVEL_CHANGES; retry.
UPSERT_ATTEMPTS="${GATEWAY_REGISTRY_UC_UPSERT_RETRIES:-10}"
for attempt in $(seq 1 "${UPSERT_ATTEMPTS}"); do
  if run_sql "UPDATE ${FQTBL} SET is_active = FALSE WHERE is_active = TRUE" &&
    run_sql "INSERT INTO ${FQTBL} (base_url, app_name, is_active, updated_at) VALUES ('${ESC_URL}', '${ESC_APP}', TRUE, current_timestamp())"; then
    break
  fi
  if [[ "${attempt}" -ge "${UPSERT_ATTEMPTS}" ]]; then
    echo "ERROR: gateway registry upsert failed after ${UPSERT_ATTEMPTS} attempts (concurrent writers or permissions)." >&2
    exit 1
  fi
  echo "NOTE: UC upsert conflict (often concurrent gateway app publish); retrying (${attempt}/${UPSERT_ATTEMPTS})..." >&2
  sleep $((1 + attempt))
done

if [[ -n "${GATEWAY_SP_ID}" ]]; then
  echo "Granting SELECT, MODIFY on ${REGISTRY_TABLE} to gateway app SP ${GATEWAY_SP_ID}..."
  run_sql "GRANT SELECT, MODIFY ON TABLE ${FQTBL} TO \`${GATEWAY_SP_ID}\`"
fi

echo "Gateway URL registry updated:"
echo "  base_url=${BASE_URL}"
echo "  app_name=${APP_NAME_INPUT}"
echo "  table=${REGISTRY_TABLE}"
