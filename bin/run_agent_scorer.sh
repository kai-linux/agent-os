#!/usr/bin/env bash
# Weekly agent performance scorer — computes per-agent success rates and
# creates a GitHub issue if any agent drops below 60% over the past 7 days.
set -euo pipefail

# shellcheck source=bin/common_env.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common_env.sh"

cd "$ROOT"
"$ROOT/.venv/bin/python3" -m orchestrator.agent_scorer
