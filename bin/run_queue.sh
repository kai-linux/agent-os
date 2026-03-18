#!/usr/bin/env bash
set -euo pipefail

ROOT="${ORCH_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
export ORCH_ROOT="$ROOT"

# Agent binaries — override via env vars or ensure they're on PATH
export CODEX_BIN="${CODEX_BIN:-codex}"
export CLAUDE_BIN="${CLAUDE_BIN:-claude}"
export GEMINI_BIN="${GEMINI_BIN:-gemini}"
export CLINE_BIN="${CLINE_BIN:-cline}"

export DEEPSEEK_RUNNER="$ROOT/bin/run_deepseek.sh"
export GEMINI_MODEL="gemini-2.5-flash"

cd "$ROOT"

flock -n /tmp/agent_os_queue.lock \
"$ROOT/.venv/bin/python3" -m orchestrator.supervisor