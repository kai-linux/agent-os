#!/usr/bin/env bash
# Weekly agent performance scorer — computes per-agent success rates and
# creates a GitHub issue if any agent drops below 60% over the past 7 days.
set -euo pipefail

ROOT="${ORCH_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
export ORCH_ROOT="$ROOT"

cd "$ROOT"
"$ROOT/.venv/bin/python3" -m orchestrator.agent_scorer
