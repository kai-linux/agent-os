#!/usr/bin/env bash
set -euo pipefail

ROOT="${ORCH_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
export ORCH_ROOT="$ROOT"

export CODEX_BIN="/home/kai/.nvm/versions/node/v24.13.0/bin/codex"
export CLAUDE_BIN="/home/kai/.nvm/versions/node/v24.13.0/bin/claude"
export GEMINI_BIN="/home/kai/.nvm/versions/node/v24.13.0/bin/gemini"
export CLINE_BIN="/home/kai/.nvm/versions/node/v24.13.0/bin/cline"

export DEEPSEEK_RUNNER="$ROOT/bin/run_deepseek.sh"
export GEMINI_MODEL="gemini-2.5-flash"

cd "$ROOT"

flock -n /tmp/agent_os_queue.lock \
"$ROOT/.venv/bin/python3" -m orchestrator.queue