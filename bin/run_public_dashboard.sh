#!/usr/bin/env bash
# Daily public reliability dashboard export from existing repo metrics artifacts.
set -euo pipefail

# shellcheck source=bin/common_env.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common_env.sh"

log_cron_start "public_dashboard"

cd "$ROOT"
"$ROOT/.venv/bin/python3" -m orchestrator.public_dashboard
