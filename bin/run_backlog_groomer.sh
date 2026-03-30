#!/usr/bin/env bash
# Weekly backlog groomer — reviews open issues, recent completions, CODEBASE.md
# Known Issues, and risk flags to create targeted improvement tasks.
#
# Suggested crontab entry (every Saturday at 20:00):
#   0 20 * * 6 /path/to/agent-os/bin/run_backlog_groomer.sh >> /path/to/agent-os/runtime/logs/backlog_groomer.log 2>&1
set -euo pipefail

# shellcheck source=bin/common_env.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common_env.sh"

log_cron_start "backlog_groomer"

cd "$ROOT"
"$ROOT/.venv/bin/python3" -m orchestrator.backlog_groomer
