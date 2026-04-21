#!/usr/bin/env bash
# Weekly dependency watcher — scans configured repos for outdated or vulnerable
# dependencies and opens bounded remediation work.
#
# Suggested crontab entry (daily cadence gate in config decides whether to act):
#   0 9 * * * /path/to/agent-os/bin/run_dependency_watcher.sh >> /path/to/agent-os/runtime/logs/dependency_watcher.log 2>&1
set -euo pipefail

# shellcheck source=bin/common_env.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common_env.sh"

log_cron_start "dependency_watcher"

cd "$ROOT"
"$ROOT/.venv/bin/python3" -m orchestrator.dependency_watcher
