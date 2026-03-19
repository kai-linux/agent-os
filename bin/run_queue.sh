#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=bin/common_env.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common_env.sh"

# Agent binaries — override via env vars or ensure they're on PATH
export DEEPSEEK_RUNNER="$ROOT/bin/run_deepseek.sh"
export GEMINI_MODEL="gemini-2.5-flash"

cd "$ROOT"

flock -n /tmp/agent_os_queue.lock \
"$ROOT/.venv/bin/python3" -m orchestrator.supervisor
