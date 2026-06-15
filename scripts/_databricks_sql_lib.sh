# Shared SQL Statement Execution helpers for deploy / UC registry scripts.
# Source from other scripts:  source "$(dirname "$0")/_databricks_sql_lib.sh"

_databricks_cli() {
  # Global flags (e.g. --profile) must follow ``databricks``, not trail ``api post PATH``.
  local -a cmd=(databricks)
  if [[ -n "${PROFILE:-}" ]]; then
    cmd+=(--profile "${PROFILE}")
  elif [[ -n "${PROFILE_ARGS+set}" && ${#PROFILE_ARGS[@]} -gt 0 ]]; then
    cmd+=("${PROFILE_ARGS[@]}")
  fi
  cmd+=("$@")
  "${cmd[@]}"
}

run_sql_statement() {
  local statement="$1"
  local payload
  payload="$(
    "${PYTHON_BIN:-python3}" -c 'import json,sys; print(json.dumps({"warehouse_id":sys.argv[1], "statement":sys.argv[2], "wait_timeout":"30s"}))' \
      "${WAREHOUSE_ID}" "${statement}"
  )"

  local response statement_id status
  response="$(_databricks_cli api post /api/2.0/sql/statements --json "${payload}")"
  statement_id="$("${PYTHON_BIN:-python3}" -c 'import json,sys; print(json.load(sys.stdin).get("statement_id",""))' <<< "${response}")"
  status="$("${PYTHON_BIN:-python3}" -c 'import json,sys; print((json.load(sys.stdin).get("status") or {}).get("state",""))' <<< "${response}")"

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
      _databricks_cli api get "/api/2.0/sql/statements/${statement_id}" >&2 || true
      return 1
    fi
    sleep 1
    response="$(_databricks_cli api get "/api/2.0/sql/statements/${statement_id}")"
    status="$("${PYTHON_BIN:-python3}" -c 'import json,sys; print((json.load(sys.stdin).get("status") or {}).get("state",""))' <<< "${response}")"
  done

  echo "ERROR: SQL statement ${statement_id} did not finish in time." >&2
  return 1
}

run_sql_query() {
  # Prints result rows as JSON array on stdout.
  local statement="$1"
  local payload
  payload="$(
    "${PYTHON_BIN:-python3}" -c 'import json,sys; print(json.dumps({"warehouse_id":sys.argv[1], "statement":sys.argv[2], "wait_timeout":"30s", "format":"JSON_ARRAY"}))' \
      "${WAREHOUSE_ID}" "${statement}"
  )"
  local response statement_id status
  response="$(_databricks_cli api post /api/2.0/sql/statements --json "${payload}")"
  statement_id="$("${PYTHON_BIN:-python3}" -c 'import json,sys; print(json.load(sys.stdin).get("statement_id",""))' <<< "${response}")"
  status="$("${PYTHON_BIN:-python3}" -c 'import json,sys; print((json.load(sys.stdin).get("status") or {}).get("state",""))' <<< "${response}")"
  if [[ -z "${statement_id}" ]]; then
    echo "[]"
    return 1
  fi
  for _ in $(seq 1 30); do
    if [[ "${status}" == "SUCCEEDED" ]]; then
      "${PYTHON_BIN:-python3}" -c '
import json, sys
d = json.load(sys.stdin)
chunks = d.get("result") or {}
data = chunks.get("data_array") or []
print(json.dumps(data))
' <<< "${response}"
      return 0
    fi
    if [[ "${status}" == "FAILED" || "${status}" == "CANCELED" ]]; then
      echo "[]"
      return 1
    fi
    sleep 1
    response="$(_databricks_cli api get "/api/2.0/sql/statements/${statement_id}")"
    status="$("${PYTHON_BIN:-python3}" -c 'import json,sys; print((json.load(sys.stdin).get("status") or {}).get("state",""))' <<< "${response}")"
  done
  echo "[]"
  return 1
}
