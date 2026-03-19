#!/usr/bin/env bash
# Daily digest — summarizes the last 24 hours of agent activity to Telegram.
#
# Suggested crontab entry (daily at 08:00):
#   0 8 * * * /path/to/agent-os/bin/run_daily_digest.sh >> /path/to/agent-os/runtime/logs/daily_digest.log 2>&1
set -euo pipefail

# shellcheck source=bin/common_env.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common_env.sh"

cd "$ROOT"
"$ROOT/.venv/bin/python3" -m orchestrator.daily_digest
