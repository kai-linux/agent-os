#!/usr/bin/env bash
# Weekly agent performance scorer — computes per-agent success rates and
# writes structured degradation findings for log_analyzer synthesis.
set -euo pipefail

# shellcheck source=bin/common_env.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common_env.sh"

log_cron_start "agent_scorer"

cd "$ROOT"
"$ROOT/.venv/bin/python3" -m orchestrator.agent_scorer
