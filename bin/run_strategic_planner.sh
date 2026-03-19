#!/usr/bin/env bash
# Weekly strategic planner — generates a prioritized sprint plan,
# posts to Telegram for approval, then creates and dispatches issues.
#
# Suggested crontab entry (every Sunday at 20:00):
#   0 20 * * 0 /path/to/agent-os/bin/run_strategic_planner.sh >> /path/to/agent-os/runtime/logs/strategic_planner.log 2>&1
set -euo pipefail

# shellcheck source=bin/common_env.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common_env.sh"

cd "$ROOT"
"$ROOT/.venv/bin/python3" -m orchestrator.strategic_planner
