#!/bin/bash
# Weekly agent performance scorer — computes per-agent success rates and
# creates a GitHub issue if any agent drops below 60% over the past 7 days.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$REPO_ROOT"
exec python -m orchestrator.agent_scorer
