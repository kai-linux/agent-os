#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=bin/common_env.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common_env.sh"

log_cron_start "deploy_watchdog"

cd "$ROOT"

"$ROOT/.venv/bin/python3" -m orchestrator.deploy_watchdog
