#!/usr/bin/env bash
set -euo pipefail

# Deploy arango-gateway-app.
#
# Typical use: log in with the Databricks CLI, then from this repo:
#   ./deploy_app.sh
#
# Optional positional overrides: app-name, workspace source path, profile, tunnel URL, cluster,
#   registry table, warehouse id (see script body for $1..$7). Set ``DATABRICKS_SQL_WAREHOUSE_ID``
#   or pass warehouse as ``$7`` — no built-in default warehouse id.
#
# Local minikube: ``UPDATE_LOCAL_TUNNEL_REGISTRY=true ./deploy_app.sh`` starts cloudflared and
# upserts the tunnel host into ARANGO_REGISTRY_TABLE. Default is false so redeploy does not
# overwrite AWS/GCP endpoints set from arango-workflow-app Connection → Connect.
#
# On first run, if the Databricks App name does not exist yet, the script runs
# ``databricks apps create`` before ``databricks apps deploy``.
#
# If ``apps create`` was interrupted (Ctrl+C) or the app was stopped, ``apps deploy`` can fail with
# "not in RUNNING state". The script may run ``databricks apps start`` when compute is not ACTIVE.
#
# ``ensure_app_running_before_deploy`` skips ``apps start`` when compute is already ACTIVE (see
# ``../arango-workflow-app/scripts/_databricks_apps_deploy_lib.sh``). ``_deploy_app`` retries on
# deployment lock errors until idle or timeout.
#
# Genie lives on arango-dashboard-app; this script does not create Genie registry tables.
#
# Optional debug import: DEBUG_POST_DEPLOY_IMPORT=true ./deploy_app.sh ...
#   (requires debug_post_deploy_import.sh + arango-import/ in this repo.)
#   Arango must accept basic auth: set ARANGO_PING_BASIC_AUTH_PASSWORD to match your cluster root
#   password, or ARANGO_ROOT_PASSWORD_FILE=/path/to/arango-root-password.txt (first line).
# Large fixtures: DEBUG_IMPORT_VOLUME_DIR=dbfs:/Volumes/...

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=scripts/_app_yaml_env.sh — duplicated inline for gateway (no _app_yaml_env in repo yet)
_gateway_read_yaml() {
  local name="$1"
  local py="${PYTHON_BIN:-python3}"
  local script="${SCRIPT_DIR}/scripts/read_app_yaml_env.py"
  if [[ -f "${SCRIPT_DIR}/scripts/read_app_yaml_env.py" ]]; then
    "${py}" "${script}" "${name}" "${SCRIPT_DIR}/app.yaml" 2>/dev/null || true
  fi
}
_deploy_mode="$(_gateway_read_yaml TEST_DEPLOYMENT_MODE)"
if [[ "${_deploy_mode}" == "local_dev" || "${_deploy_mode}" == "local_docker" || "${_deploy_mode}" == "local" ]]; then
  echo "TEST_DEPLOYMENT_MODE=${_deploy_mode} — local run (skipping Databricks deploy)"
  bash "${SCRIPT_DIR}/scripts/build-local.sh"
  exec bash "${SCRIPT_DIR}/scripts/start-local-dev.sh"
fi

APP_NAME="${1:-arango-gateway-app}"
PROFILE="${3:-}"

_resolve_ws_user() {
  local args=() user_json user
  [[ -n "${PROFILE}" ]] && args=(--profile "${PROFILE}")
  user_json="$(databricks current-user me "${args[@]}" 2>/dev/null)" || return 1
  user="$(printf '%s' "${user_json}" | python3 -c 'import json,sys; d=json.load(sys.stdin); e=d.get("emails") or []; print(d.get("userName") or (e[0].get("value") if e else ""))' 2>/dev/null)" || return 1
  [[ -n "${user}" ]] || return 1
  printf '%s' "${user}"
}

if [[ -n "${2:-}" ]]; then
  SOURCE_CODE_PATH="$2"
else
  _ws_user="$(_resolve_ws_user)" || {
    echo "ERROR: could not resolve workspace user via 'databricks current-user me'." >&2
    echo "Pass an explicit source path: ./deploy_app.sh ${APP_NAME} /Workspace/Users/<you>/${APP_NAME}" >&2
    exit 1
  }
  SOURCE_CODE_PATH="/Workspace/Users/${_ws_user}/${APP_NAME}"
fi

LOCAL_ARANGO_URL="${4:-https://127.0.0.1:18529}"
CLUSTER_NAME="${5:-local-minikube-dev}"
REGISTRY_TABLE="${6:-workspace.default.arango_connection_registry}"
WAREHOUSE_ID="${DATABRICKS_SQL_WAREHOUSE_ID:-${7:-}}"
ARANGO_GATEWAY_REGISTRY_TABLE="${ARANGO_GATEWAY_REGISTRY_TABLE:-workspace.default.arango_gateway_registry}"
# Unity Catalog volume for gzip JSONL graph snapshots (/Volumes/<cat>/<schema>/<name>/uc_graph_snapshots).
UC_GRAPH_SNAPSHOT_VOLUME_NAME="${UC_GRAPH_SNAPSHOT_VOLUME_NAME:-${UC_GRAPH_VOLUME_NAME:-arango_agent_volume}}"
# Connection profiles + workflow-data (same env name as arango-workflow-app).
UC_WORKFLOW_VOLUME_NAME="${UC_WORKFLOW_VOLUME_NAME:-arango_workflow_volume}"
# Local minikube only: cloudflared tunnel + UC registry upsert (default off — preserves AWS/GCP Connect).
UPDATE_LOCAL_TUNNEL_REGISTRY="${UPDATE_LOCAL_TUNNEL_REGISTRY:-false}"
# Optional: only for DEBUG_POST_DEPLOY_IMPORT curl to local Arango (not stored in app.yaml).
ARANGO_PING_BASIC_AUTH_USER="${ARANGO_PING_BASIC_AUTH_USER:-root}"
ARANGO_PING_BASIC_AUTH_PASSWORD="${ARANGO_PING_BASIC_AUTH_PASSWORD:-}"
if [[ -n "${ARANGO_ROOT_PASSWORD_FILE:-}" && -f "${ARANGO_ROOT_PASSWORD_FILE}" ]]; then
  ARANGO_PING_BASIC_AUTH_PASSWORD="$(head -n 1 "${ARANGO_ROOT_PASSWORD_FILE}" | tr -d '\r\n')"
fi
# When true or 1, after deploy wait for RUNNING then: create UC tables from arango-import/*.jsonl,
# grant the app SP SELECT, and bulk-import into Arango (LOCAL_ARANGO_URL).
# UC load defaults to inlined SQL (no DBFS). Optional: DEBUG_IMPORT_VOLUME_DIR=dbfs:/Volumes/... for read_files + upload.
DEBUG_POST_DEPLOY_IMPORT="${DEBUG_POST_DEPLOY_IMPORT:-false}"
DEBUG_IMPORT_VOLUME_DIR="${DEBUG_IMPORT_VOLUME_DIR:-}"
DEBUG_IMPORT_GRAPH_NAME="${DEBUG_IMPORT_GRAPH_NAME:-debug_import_graph}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Prefer project venv: ``pip install -e .`` or monolith venv with Flask + databricks-sdk.
if [[ -x "${SCRIPT_DIR}/.venv/bin/python3" ]]; then
  PYTHON_BIN="${SCRIPT_DIR}/.venv/bin/python3"
elif [[ -n "${VIRTUAL_ENV:-}" && -x "${VIRTUAL_ENV}/bin/python3" ]]; then
  PYTHON_BIN="${VIRTUAL_ENV}/bin/python3"
else
  PYTHON_BIN="python3"
fi
export PYTHON_BIN
STATE_DIR=".state"
TUNNEL_LOG="${STATE_DIR}/cloudflared.log"
TUNNEL_PID_FILE="${STATE_DIR}/cloudflared.pid"

if [[ "${DEBUG_POST_DEPLOY_IMPORT}" == "true" || "${DEBUG_POST_DEPLOY_IMPORT}" == "1" ]] &&
  [[ -z "${ARANGO_PING_BASIC_AUTH_PASSWORD}" ]]; then
  echo "ERROR: DEBUG_POST_DEPLOY_IMPORT will call Arango at ${LOCAL_ARANGO_URL} with basic auth." >&2
  echo "Set ARANGO_ROOT_PASSWORD_FILE or ARANGO_PING_BASIC_AUTH_PASSWORD for the debug import only." >&2
  exit 1
fi

if [[ -n "${PROFILE}" ]]; then
  PROFILE_ARGS=(--profile "${PROFILE}")
else
  PROFILE_ARGS=()
fi

# shellcheck source=../arango-workflow-app/scripts/_databricks_apps_deploy_lib.sh
source "${SCRIPT_DIR}/../arango-workflow-app/scripts/_databricks_apps_deploy_lib.sh"

mkdir -p "${STATE_DIR}"

ensure_cloudflared() {
  if command -v cloudflared >/dev/null 2>&1; then
    return 0
  fi
  echo "cloudflared (Cloudflare Tunnel) not found; installing from official .deb..."
  local arch deb_arch
  arch="$(uname -m)"
  case "${arch}" in
    x86_64 | amd64) deb_arch=amd64 ;;
    aarch64 | arm64) deb_arch=arm64 ;;
    *)
      echo "ERROR: Automatic cloudflared install is not set up for architecture '${arch}'." >&2
      echo "Install manually: https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/installation/" >&2
      exit 1
      ;;
  esac
  local tmpdeb
  tmpdeb="$(mktemp /tmp/cloudflared.XXXXXX.deb)"
  if ! wget -q -O "${tmpdeb}" "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${deb_arch}.deb"; then
    rm -f "${tmpdeb}"
    echo "ERROR: Failed to download cloudflared-linux-${deb_arch}.deb" >&2
    exit 1
  fi
  if ! sudo dpkg -i "${tmpdeb}"; then
    rm -f "${tmpdeb}"
    echo "ERROR: cloudflared install failed (need sudo and dpkg). Install manually." >&2
    exit 1
  fi
  rm -f "${tmpdeb}"
  command -v cloudflared >/dev/null 2>&1 || {
    echo "ERROR: cloudflared still not on PATH after install." >&2
    exit 1
  }
}

ensure_cloudflared

echo "Arango credentials: configure in arango-workflow-app Connection page (/connection); gateway reads UC workflow-data."

echo "Syncing local project to '${SOURCE_CODE_PATH}'..."
databricks sync . "${SOURCE_CODE_PATH}" "${PROFILE_ARGS[@]}"

if ! databricks apps get "${APP_NAME}" "${PROFILE_ARGS[@]}" &>/dev/null; then
  echo "Creating Databricks App '${APP_NAME}' (not found in workspace; first-time setup)..."
  databricks apps create "${APP_NAME}" \
    --description "Arango gateway — UC registry, Arango embed proxy, access to remote Arango Cluster" \
    "${PROFILE_ARGS[@]}"
fi

ensure_app_running_before_deploy

echo "Deploying app '${APP_NAME}' from '${SOURCE_CODE_PATH}'..."
_deploy_app

wait_for_app_running() {
  local json app_state compute_state
  echo "Waiting for '${APP_NAME}' to reach RUNNING (compute ACTIVE) after deploy..."
  for _ in $(seq 1 90); do
    json="$(databricks apps get "${APP_NAME}" --output json "${PROFILE_ARGS[@]}" 2>/dev/null || true)"
    app_state="$(
      "${PYTHON_BIN}" -c 'import json,sys; d=json.load(sys.stdin); print((d.get("app_status") or {}).get("state",""))' <<< "${json}" 2>/dev/null || true
    )"
    compute_state="$(
      "${PYTHON_BIN}" -c 'import json,sys; d=json.load(sys.stdin); print((d.get("compute_status") or {}).get("state",""))' <<< "${json}" 2>/dev/null || true
    )"
    if [[ "${app_state}" == "RUNNING" && "${compute_state}" == "ACTIVE" ]]; then
      echo "App is RUNNING (compute ACTIVE)."
      return 0
    fi
    sleep 2
  done
  echo "WARNING: '${APP_NAME}' did not reach RUNNING/ACTIVE within ~3 minutes (app=${app_state:-unknown}, compute=${compute_state:-unknown})." >&2
  echo "         URLs below may hang until the app finishes starting — check Databricks Apps UI logs." >&2
  return 1
}

wait_for_app_running || true

echo "Fetching app metadata..."
APP_JSON="$(databricks apps get "${APP_NAME}" --output json "${PROFILE_ARGS[@]}")"

APP_URL="$(
  "${PYTHON_BIN}" -c 'import json,sys; print(json.load(sys.stdin).get("url",""))' <<< "${APP_JSON}"
)"
# Databricks puts a numeric suffix in the app hostname (e.g. ...-7474655777170309.aws.databricksapps.com).
# It is not configurable in this script; we parse it from the same ``url`` JSON field for convenience.
APP_URL_NUMERIC_SUFFIX="$(
  "${PYTHON_BIN}" -c '
import json, sys
from urllib.parse import urlparse

j = json.load(sys.stdin)
url = j.get("url", "") or ""
host = urlparse(url).hostname or ""
sub = host.split(".")[0] if host else ""
parts = sub.rsplit("-", 1)
suffix = parts[1] if len(parts) == 2 and parts[1].isdigit() else ""
print(suffix)
' <<< "${APP_JSON}"
)"
APP_RESOURCE_ID="$(
  "${PYTHON_BIN}" -c 'import json,sys; j=json.load(sys.stdin); print(j.get("id") or j.get("app_id") or "")' <<< "${APP_JSON}"
)"
APP_SERVICE_PRINCIPAL_CLIENT_ID="$(
  "${PYTHON_BIN}" -c 'import json,sys; print(json.load(sys.stdin).get("service_principal_client_id",""))' <<< "${APP_JSON}"
)"

if [[ -z "${APP_URL}" ]]; then
  echo "ERROR: Could not extract URL from Databricks app metadata." >&2
  exit 1
fi
if [[ -z "${APP_SERVICE_PRINCIPAL_CLIENT_ID}" ]]; then
  echo "ERROR: Could not extract app service principal client id." >&2
  exit 1
fi

APP_HEALTH_URL="${APP_URL}/health"
APP_HEALTH_EXTENDED_URL="${APP_URL}/api/debug/startup-status?refresh=true"
APP_EMBED_UI_URL="${APP_URL}/embedded-arango/_db/_system/_admin/aardvark/index.html#login"

if [[ -z "${WAREHOUSE_ID// }" ]]; then
  echo "ERROR: DATABRICKS_SQL_WAREHOUSE_ID is not set (export it, set in app.yaml, use arango-platform-bundle variables, or pass as 7th positional arg to deploy_app.sh)." >&2
  exit 1
fi

run_sql_statement() {
  local statement="$1"
  local payload
  payload="$(
    "${PYTHON_BIN}" -c 'import json,sys; print(json.dumps({"warehouse_id":sys.argv[1], "statement":sys.argv[2], "wait_timeout":"30s"}))' \
      "${WAREHOUSE_ID}" "${statement}"
  )"

  local response statement_id status
  response="$(databricks api post /api/2.0/sql/statements --json "${payload}" "${PROFILE_ARGS[@]}")"
  statement_id="$("${PYTHON_BIN}" -c 'import json,sys; print(json.load(sys.stdin).get("statement_id",""))' <<< "${response}")"
  status="$("${PYTHON_BIN}" -c 'import json,sys; print((json.load(sys.stdin).get("status") or {}).get("state",""))' <<< "${response}")"

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
    status="$("${PYTHON_BIN}" -c 'import json,sys; print((json.load(sys.stdin).get("status") or {}).get("state",""))' <<< "${response}")"
  done

  echo "ERROR: SQL statement ${statement_id} did not finish in time." >&2
  exit 1
}

if [[ -f "${SCRIPT_DIR}/debug_post_deploy_import.sh" ]]; then
  # shellcheck source=debug_post_deploy_import.sh
  source "${SCRIPT_DIR}/debug_post_deploy_import.sh"
else
  debug_post_deploy_import() {
    echo "NOTE: debug_post_deploy_import.sh not present; skip DEBUG_POST_DEPLOY_IMPORT." >&2
    return 1
  }
fi

REGISTRY_CATALOG_PRE="$(echo "${REGISTRY_TABLE}" | cut -d. -f1)"
REGISTRY_SCHEMA_PRE="$(echo "${REGISTRY_TABLE}" | cut -d. -f2)"
GATEWAY_REGISTRY_CATALOG_PRE="$(echo "${ARANGO_GATEWAY_REGISTRY_TABLE}" | cut -d. -f1)"
GATEWAY_REGISTRY_SCHEMA_PRE="$(echo "${ARANGO_GATEWAY_REGISTRY_TABLE}" | cut -d. -f2)"

# Pre-create both UC registry tables BEFORE granting on them. On a fresh install the
# app SP has not yet run startup_debug (app_status often UNAVAILABLE when this runs),
# so the connection registry does not exist yet and the GRANT below would otherwise
# fail with TABLE_DOES_NOT_EXIST. Creating here also makes the human (the SQL warehouse
# caller) the owner, so the subsequent GRANTs to the app SP succeed.
echo "Pre-creating UC registry schemas/tables so GRANTs succeed and human owns them..."
run_sql_statement "CREATE SCHEMA IF NOT EXISTS \`${REGISTRY_CATALOG_PRE}\`.\`${REGISTRY_SCHEMA_PRE}\`"
run_sql_statement "CREATE TABLE IF NOT EXISTS ${REGISTRY_TABLE} (cluster_name STRING NOT NULL, ip_address STRING NOT NULL, port INT NOT NULL, protocol STRING NOT NULL, is_active BOOLEAN NOT NULL, updated_at TIMESTAMP NOT NULL) USING DELTA"
if [[ "${GATEWAY_REGISTRY_CATALOG_PRE}.${GATEWAY_REGISTRY_SCHEMA_PRE}" != "${REGISTRY_CATALOG_PRE}.${REGISTRY_SCHEMA_PRE}" ]]; then
  run_sql_statement "CREATE SCHEMA IF NOT EXISTS \`${GATEWAY_REGISTRY_CATALOG_PRE}\`.\`${GATEWAY_REGISTRY_SCHEMA_PRE}\`"
fi
run_sql_statement "CREATE TABLE IF NOT EXISTS ${ARANGO_GATEWAY_REGISTRY_TABLE} (base_url STRING NOT NULL, app_name STRING NOT NULL, is_active BOOLEAN NOT NULL, updated_at TIMESTAMP NOT NULL) USING DELTA"

# Tolerant grant: ``account users`` may be disabled on some workspaces. Run in a
# subshell so ``run_sql_statement``'s ``exit 1`` does not kill the deploy script.
echo "Granting SELECT, MODIFY on registry tables to 'account users' (so humans can read/inspect)..."
( run_sql_statement "GRANT SELECT, MODIFY ON TABLE ${REGISTRY_TABLE} TO \`account users\`" ) || \
  echo "NOTE: GRANT account users on ${REGISTRY_TABLE} failed (probably ok; continuing)." >&2
( run_sql_statement "GRANT SELECT, MODIFY ON TABLE ${ARANGO_GATEWAY_REGISTRY_TABLE} TO \`account users\`" ) || \
  echo "NOTE: GRANT account users on ${ARANGO_GATEWAY_REGISTRY_TABLE} failed (probably ok; continuing)." >&2

echo "Granting UC privileges to app service principal client id '${APP_SERVICE_PRINCIPAL_CLIENT_ID}'..."
run_sql_statement "GRANT USE CATALOG ON CATALOG workspace TO \`${APP_SERVICE_PRINCIPAL_CLIENT_ID}\`"
run_sql_statement "GRANT USE SCHEMA ON SCHEMA workspace.default TO \`${APP_SERVICE_PRINCIPAL_CLIENT_ID}\`"
run_sql_statement "GRANT SELECT ON TABLE ${REGISTRY_TABLE} TO \`${APP_SERVICE_PRINCIPAL_CLIENT_ID}\`"
run_sql_statement "GRANT MODIFY ON TABLE ${REGISTRY_TABLE} TO \`${APP_SERVICE_PRINCIPAL_CLIENT_ID}\`"
run_sql_statement "GRANT SELECT ON TABLE ${ARANGO_GATEWAY_REGISTRY_TABLE} TO \`${APP_SERVICE_PRINCIPAL_CLIENT_ID}\`"
run_sql_statement "GRANT MODIFY ON TABLE ${ARANGO_GATEWAY_REGISTRY_TABLE} TO \`${APP_SERVICE_PRINCIPAL_CLIENT_ID}\`"

REGISTRY_CATALOG="$(echo "${REGISTRY_TABLE}" | cut -d. -f1)"
REGISTRY_SCHEMA="$(echo "${REGISTRY_TABLE}" | cut -d. -f2)"
echo "Ensuring UC graph snapshot volume ${REGISTRY_CATALOG}.${REGISTRY_SCHEMA}.${UC_GRAPH_SNAPSHOT_VOLUME_NAME} (JSONL export path under /Volumes/...)..."
run_sql_statement "CREATE VOLUME IF NOT EXISTS ${REGISTRY_CATALOG}.${REGISTRY_SCHEMA}.${UC_GRAPH_SNAPSHOT_VOLUME_NAME}"
run_sql_statement "GRANT READ VOLUME, WRITE VOLUME ON VOLUME ${REGISTRY_CATALOG}.${REGISTRY_SCHEMA}.${UC_GRAPH_SNAPSHOT_VOLUME_NAME} TO \`${APP_SERVICE_PRINCIPAL_CLIENT_ID}\`"

echo "Granting READ on workflow-data volume ${REGISTRY_CATALOG}.${REGISTRY_SCHEMA}.${UC_WORKFLOW_VOLUME_NAME} (Connection profiles from arango-workflow-app)..."
run_sql_statement "CREATE VOLUME IF NOT EXISTS ${REGISTRY_CATALOG}.${REGISTRY_SCHEMA}.${UC_WORKFLOW_VOLUME_NAME}"
run_sql_statement "GRANT READ VOLUME ON VOLUME ${REGISTRY_CATALOG}.${REGISTRY_SCHEMA}.${UC_WORKFLOW_VOLUME_NAME} TO \`${APP_SERVICE_PRINCIPAL_CLIENT_ID}\`"

echo
echo "Publishing gateway app URL to Unity Catalog (${ARANGO_GATEWAY_REGISTRY_TABLE})..."
_publish_gw_uc_ok=0
if [[ -n "${PROFILE}" ]]; then
  if ( "${SCRIPT_DIR}/update_arango_gateway_registry_uc.sh" \
    "${APP_URL}" "${APP_NAME}" "${ARANGO_GATEWAY_REGISTRY_TABLE}" "${WAREHOUSE_ID}" "${PROFILE}" \
    "${APP_SERVICE_PRINCIPAL_CLIENT_ID}" ); then
    _publish_gw_uc_ok=1
  fi
else
  if ( "${SCRIPT_DIR}/update_arango_gateway_registry_uc.sh" \
    "${APP_URL}" "${APP_NAME}" "${ARANGO_GATEWAY_REGISTRY_TABLE}" "${WAREHOUSE_ID}" "" \
    "${APP_SERVICE_PRINCIPAL_CLIENT_ID}" ); then
    _publish_gw_uc_ok=1
  fi
fi
if [[ "${_publish_gw_uc_ok}" -ne 1 ]]; then
  echo "NOTE: Gateway URL UC publish failed (often table owned by app SP without broad grants yet). Restart arango-gateway-app once, then re-run ./deploy_app.sh or run update_arango_gateway_registry_uc.sh manually." >&2
fi

if [[ "${UPDATE_LOCAL_TUNNEL_REGISTRY}" == "true" || "${UPDATE_LOCAL_TUNNEL_REGISTRY}" == "1" ]]; then
  if [[ -f "${TUNNEL_PID_FILE}" ]]; then
    OLD_PID="$(cat "${TUNNEL_PID_FILE}" 2>/dev/null || true)"
    if [[ -n "${OLD_PID}" ]] && kill -0 "${OLD_PID}" 2>/dev/null; then
      echo "Stopping existing cloudflared process (${OLD_PID})..."
      kill "${OLD_PID}" || true
      sleep 1
    fi
  fi

  echo "Starting cloudflared in background for ${LOCAL_ARANGO_URL}..."
  nohup cloudflared tunnel \
    --url "${LOCAL_ARANGO_URL}" \
    --no-tls-verify \
    > "${TUNNEL_LOG}" 2>&1 &
  TUNNEL_PID=$!
  echo "${TUNNEL_PID}" > "${TUNNEL_PID_FILE}"

  TUNNEL_URL=""
  for _ in $(seq 1 30); do
    if [[ -f "${TUNNEL_LOG}" ]]; then
      TUNNEL_URL="$("${PYTHON_BIN}" -c 'import re,sys; t=open(sys.argv[1], "r", encoding="utf-8", errors="ignore").read(); m=re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", t); print(m.group(0) if m else "")' "${TUNNEL_LOG}")"
    fi
    if [[ -n "${TUNNEL_URL}" ]]; then
      break
    fi
    sleep 1
  done

  if [[ -n "${TUNNEL_URL}" ]]; then
    echo
    echo "Updating Unity Catalog registry via Databricks SQL..."
    if [[ -n "${PROFILE}" ]]; then
      "${SCRIPT_DIR}/update_arango_registry_uc.sh" "${TUNNEL_URL}" "${CLUSTER_NAME}" "${REGISTRY_TABLE}" "${WAREHOUSE_ID}" "${PROFILE}"
    else
      "${SCRIPT_DIR}/update_arango_registry_uc.sh" "${TUNNEL_URL}" "${CLUSTER_NAME}" "${REGISTRY_TABLE}" "${WAREHOUSE_ID}"
    fi
  else
    echo "Skipping Unity Catalog registry update because tunnel URL was not detected."
  fi
else
  echo "Skipping cloudflared and local tunnel registry update (set UPDATE_LOCAL_TUNNEL_REGISTRY=true for minikube dev)."
  TUNNEL_URL=""
  TUNNEL_PID=""
fi

if [[ "${DEBUG_POST_DEPLOY_IMPORT}" == "true" || "${DEBUG_POST_DEPLOY_IMPORT}" == "1" ]]; then
  echo
  echo "DEBUG_POST_DEPLOY_IMPORT enabled: UC tables + Arango bulk load from arango-import/"
  debug_post_deploy_import
fi

echo
echo "DATABRICKS_APP_URL=${APP_URL}"
echo "DATABRICKS_APP_HEALTH_URL=${APP_HEALTH_URL}"
echo "DATABRICKS_APP_HEALTH_EXTENDED_URL=${APP_HEALTH_EXTENDED_URL}"
echo "DATABRICKS_APP_EMBED_UI_URL=${APP_EMBED_UI_URL}"
# OSC 8 hyperlink (iTerm2, VS Code terminal, GNOME Terminal 3.26+, etc.).
printf '  \033]8;;%s\033\\%s\033]8;;\033\\\n' "${APP_HEALTH_URL}" "→ Open gateway health (hyperlink)"
echo "NOTE: /health returns immediately once gunicorn is up; UC registry publish runs in background."
echo "      Browser: open while logged into Databricks (Apps URLs are not anonymous). Anonymous curl often gets 302/401, not JSON."
if command -v curl >/dev/null 2>&1; then
  http_code="$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 15 "${APP_HEALTH_URL}" || echo "000")"
  if [[ "${http_code}" == "200" ]]; then
    echo "Health probe: HTTP 200 (reachable)."
  elif [[ "${http_code}" == "000" ]]; then
    echo "Health probe: timed out — app may still be starting; retry in 30s or check Apps logs."
  else
    echo "Health probe: HTTP ${http_code} (expected 302/401 without Databricks session cookie — use browser while signed in)."
  fi
fi
if [[ -n "${APP_URL_NUMERIC_SUFFIX}" ]]; then
  echo "DATABRICKS_APP_URL_NUMERIC_SUFFIX=${APP_URL_NUMERIC_SUFFIX}  (from hostname in apps get url)"
else
  echo "DATABRICKS_APP_URL_NUMERIC_SUFFIX=  (could not parse; check hostname pattern)"
fi
if [[ -n "${APP_RESOURCE_ID}" ]]; then
  echo "DATABRICKS_APP_RESOURCE_ID=${APP_RESOURCE_ID}  (id field from apps get JSON, if present)"
fi
if [[ -n "${TUNNEL_URL}" ]]; then
  echo "ARANGO_TUNNEL_URL=${TUNNEL_URL}"
  echo "CLOUDFLARED_PID=${TUNNEL_PID}"
elif [[ "${UPDATE_LOCAL_TUNNEL_REGISTRY}" == "true" || "${UPDATE_LOCAL_TUNNEL_REGISTRY}" == "1" ]]; then
  echo "WARNING: tunnel URL not detected yet. Check ${TUNNEL_LOG}"
  echo "CLOUDFLARED_PID=${TUNNEL_PID}"
else
  echo "Local tunnel registry update skipped (UPDATE_LOCAL_TUNNEL_REGISTRY=false)."
fi
echo
echo "To export in your current shell:"
echo "export DATABRICKS_APP_URL=\"${APP_URL}\""
echo "# arango-dashboard-app reads gateway URL from UC (${ARANGO_GATEWAY_REGISTRY_TABLE}); override with:"
echo "# export ARANGO_GATEWAY_BASE_URL=\"${APP_URL}\""
if [[ -n "${APP_URL_NUMERIC_SUFFIX}" ]]; then
  echo "export DATABRICKS_APP_URL_NUMERIC_SUFFIX=\"${APP_URL_NUMERIC_SUFFIX}\""
fi
if [[ -n "${TUNNEL_URL}" ]]; then
  echo "export ARANGO_TUNNEL_URL=\"${TUNNEL_URL}\""
fi
echo
echo "cloudflared log: ${TUNNEL_LOG}"
echo "cloudflared pid file: ${TUNNEL_PID_FILE}"
echo "registry table: ${REGISTRY_TABLE}"
echo "uc graph snapshot volume: ${REGISTRY_CATALOG}.${REGISTRY_SCHEMA}.${UC_GRAPH_SNAPSHOT_VOLUME_NAME}"
echo "uc workflow volume: ${REGISTRY_CATALOG}.${REGISTRY_SCHEMA}.${UC_WORKFLOW_VOLUME_NAME} (READ — Connection profile auth)"
echo "warehouse id: ${WAREHOUSE_ID}"
echo "Gateway reads active profile credentials from workflow-data/settings/arango_connection_profiles.json (10s in-process cache; registry table has host/port only)."
echo "Registry row cached for \${ARANGO_REGISTRY_CACHE_TTL_SECONDS:-60}s per proxy worker (set ARANGO_REGISTRY_CACHE_TTL_SECONDS=0 to disable)."
