#!/usr/bin/env bash
set -euo pipefail

# Read-only monitoring remains available while execution is disabled.
export AGENT_OS_IGNORE_DISABLED=1
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common_env.sh"
cd "$ROOT"
exec "$ROOT/.venv/bin/python3" -m orchestrator.dashboard.server "$@"
