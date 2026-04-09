#!/usr/bin/env bash
# Health gate validation report — generates weekly gate efficacy report.
#
# Suggested crontab entry (weekly on Mondays at 09:00):
#   0 9 * * 1 /home/kai/agent-os/bin/run_health_gate_report.sh >> /home/kai/agent-os/runtime/logs/health_gate_report.log 2>&1
set -euo pipefail

# shellcheck source=bin/common_env.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common_env.sh"

log_cron_start "health_gate_report"

cd "$ROOT"
"$ROOT/.venv/bin/python3" -m orchestrator.health_gate_report
