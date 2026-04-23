#!/usr/bin/env bash
set -euo pipefail

AGENT="$1"
WORKDIR="$2"
PROMPT="$3"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODEX_BIN="${CODEX_BIN:-codex}"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
GEMINI_BIN="${GEMINI_BIN:-gemini}"
DEEPSEEK_RUNNER="${DEEPSEEK_RUNNER:-${SCRIPT_DIR}/run_deepseek.sh}"
GEMINI_MODEL="${GEMINI_MODEL:-gemini-2.5-flash}"

# DeepSeek configuration
export DEEPSEEK_OPENROUTER_CONFIG="${DEEPSEEK_OPENROUTER_CONFIG:-$HOME/.config/openrouter}"
export DEEPSEEK_OPENROUTER_MODEL="${DEEPSEEK_OPENROUTER_MODEL:-deepseek/deepseek-v3.2}"

cd "$WORKDIR"

if [ "$AGENT" = "codex" ]; then
    "$CODEX_BIN" exec --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check "$(cat "$PROMPT")"
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
