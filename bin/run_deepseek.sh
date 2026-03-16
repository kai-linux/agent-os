#!/usr/bin/env bash
set -euo pipefail

WORKDIR="$1"
PROMPT_FILE="$2"

CLINE_BIN="${CLINE_BIN:-/home/kai/.nvm/versions/node/v24.13.0/bin/cline}"
DEEPSEEK_MODEL="${DEEPSEEK_MODEL:-}"
CLINE_TIMEOUT_SECONDS="${CLINE_TIMEOUT_SECONDS:-1800}"
CLINE_CONFIG_DIR="${CLINE_CONFIG_DIR:-$HOME/.config/cline}"

if [ -z "$DEEPSEEK_MODEL" ]; then
  echo "DEEPSEEK_MODEL is not set."
  echo "Set it to your exact Cline/OpenRouter model id, e.g. deepseek/..."
  exit 1
fi

cd "$WORKDIR"

"$CLINE_BIN" task \
  --yolo \
  --json \
  --cwd "$WORKDIR" \
  --config "$CLINE_CONFIG_DIR" \
  --model "$DEEPSEEK_MODEL" \
  --timeout "$CLINE_TIMEOUT_SECONDS" \
  "$(cat "$PROMPT_FILE")"
