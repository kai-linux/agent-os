#!/bin/bash
# Weekly backlog groomer — reviews open issues, recent completions, CODEBASE.md
# Known Issues, and risk flags to create targeted improvement tasks.
#
# Suggested crontab entry (every Saturday at 20:00):
#   0 20 * * 6 /home/kai/agent-os/bin/run_backlog_groomer.sh >> /home/kai/agent-os/runtime/logs/backlog_groomer.log 2>&1
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$REPO_ROOT"
exec python -m orchestrator.backlog_groomer
