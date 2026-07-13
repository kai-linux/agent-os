#!/usr/bin/env bash
set -euo pipefail

AGENT="$1"
WORKDIR="$2"
PROMPT="$3"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODEX_BIN="${CODEX_BIN:-codex}"
CLAUDE_BIN="${CLAUDE_BIN:-claude}"
GEMINI_BIN="${GEMINI_BIN:-gemini}"
OMP_BIN="${OMP_BIN:-omp}"
OMP_MODEL="${OMP_MODEL:-openrouter/z-ai/glm-5.2}"
GEMINI_MODEL="${GEMINI_MODEL:-gemini-2.5-flash}"

# OMP (Oh My Pi) harness — GLM-5.2 via OpenRouter as the fast first-attempt agent.
# Override the binary or model via OMP_BIN / OMP_MODEL if needed.

cd "$WORKDIR"

if [ "$AGENT" = "codex" ]; then
    "$CODEX_BIN" exec --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check - < "$PROMPT"
elif [ "$AGENT" = "claude" ]; then
    "$CLAUDE_BIN" --dangerously-skip-permissions -p < "$PROMPT"
elif [ "$AGENT" = "gemini" ]; then
    "$GEMINI_BIN" -p "" -m "$GEMINI_MODEL" --output-format json < "$PROMPT"
elif [ "$AGENT" = "omp" ]; then
    "$OMP_BIN" -p --model "$OMP_MODEL" --approval-mode yolo --no-session --no-title "@$PROMPT"
else
    echo "Unknown agent: $AGENT"
    exit 1
fi
