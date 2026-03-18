#!/bin/bash
# Weekly log analyzer — reads last 7 days of metrics and queue logs,
# uses Claude Haiku to identify the top 3 issues, and creates GitHub issues.
#
# Suggested crontab entry (every Monday at 07:00):
#   0 7 * * 1 /home/kai/agent-os/bin/run_log_analyzer.sh >> /home/kai/agent-os/runtime/logs/log_analyzer.log 2>&1
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$REPO_ROOT"
exec python -m orchestrator.log_analyzer
