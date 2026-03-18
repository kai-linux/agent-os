#!/usr/bin/env bash
# Weekly log analyzer — reads last 7 days of metrics and queue logs,
# uses Claude Haiku to identify the top 3 issues, and creates GitHub issues.
#
# Suggested crontab entry (every Monday at 07:00):
#   0 7 * * 1 /path/to/agent-os/bin/run_log_analyzer.sh >> /path/to/agent-os/runtime/logs/log_analyzer.log 2>&1
set -euo pipefail

ROOT="${ORCH_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
export ORCH_ROOT="$ROOT"

cd "$ROOT"
"$ROOT/.venv/bin/python3" -m orchestrator.log_analyzer
