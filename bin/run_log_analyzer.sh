#!/usr/bin/env bash
# Weekly log analyzer — reads last 7 days of metrics, queue logs, and scorer
# findings, synthesizes the top remediation issues, and creates GitHub issues.
#
# Suggested crontab entry (every Monday at 07:00):
#   0 7 * * 1 /path/to/agent-os/bin/run_log_analyzer.sh >> /path/to/agent-os/runtime/logs/log_analyzer.log 2>&1
set -euo pipefail

# shellcheck source=bin/common_env.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common_env.sh"

log_cron_start "log_analyzer"

cd "$ROOT"
"$ROOT/.venv/bin/python3" -m orchestrator.log_analyzer
