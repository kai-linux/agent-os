#!/usr/bin/env bash
set -euo pipefail

AGENT="$1"
WORKDIR="$2"
PROMPT="$3"

CODEX_BIN="${CODEX_BIN:-/home/kai/.nvm/versions/node/v24.13.0/bin/codex}"
CLAUDE_BIN="${CLAUDE_BIN:-/home/kai/.nvm/versions/node/v24.13.0/bin/claude}"
GEMINI_BIN="${GEMINI_BIN:-/home/kai/.nvm/versions/node/v24.13.0/bin/gemini}"
DEEPSEEK_RUNNER="${DEEPSEEK_RUNNER:-/home/kai/agent-os/bin/run_deepseek.sh}"
GEMINI_MODEL="${GEMINI_MODEL:-gemini-2.5-flash}"

cd "$WORKDIR"

if [ "$AGENT" = "codex" ]; then
    "$CODEX_BIN" exec --full-auto --skip-git-repo-check "$(cat "$PROMPT")"
elif [ "$AGENT" = "claude" ]; then
    "$CLAUDE_BIN" --dangerously-skip-permissions -p "$(cat "$PROMPT")"
elif [ "$AGENT" = "gemini" ]; then
    "$GEMINI_BIN" -p "$(cat "$PROMPT")" -m "$GEMINI_MODEL" --output-format json
elif [ "$AGENT" = "deepseek" ]; then
    if [ -x "$DEEPSEEK_RUNNER" ]; then
        "$DEEPSEEK_RUNNER" "$WORKDIR" "$PROMPT"
    else
        echo "DeepSeek requested but runner is missing or not executable: $DEEPSEEK_RUNNER"
        exit 1
    fi
else
    echo "Unknown agent: $AGENT"
    exit 1
fi