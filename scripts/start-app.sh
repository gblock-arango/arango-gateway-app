#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=scripts/load-app-yaml-env.sh
source "${ROOT}/scripts/load-app-yaml-env.sh"

mode="${TEST_DEPLOYMENT_MODE:-self_managed_platform}"
case "${mode}" in
  local_dev|local_docker|local)
    exec bash "${ROOT}/scripts/start-local-dev.sh"
    ;;
esac
exec bash "${ROOT}/scripts/start-databricks-app.sh"
