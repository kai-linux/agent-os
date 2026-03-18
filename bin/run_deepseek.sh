#!/usr/bin/env bash
set -euo pipefail

WORKDIR="$1"
PROMPT_FILE="$2"

CLINE_BIN="${CLINE_BIN:-/home/kai/.nvm/versions/node/v24.13.0/bin/cline}"
CLINE_TIMEOUT_SECONDS="${CLINE_TIMEOUT_SECONDS:-1800}"

# Provider fallback order: openrouter -> nanogpt -> chutes
# Each provider needs its own Cline config dir with the correct API key/base URL set.
# Set these env vars to enable each provider:
#   DEEPSEEK_OPENROUTER_CONFIG  e.g. ~/.config/cline-openrouter
#   DEEPSEEK_OPENROUTER_MODEL   e.g. deepseek/deepseek-chat
#   DEEPSEEK_NANOGPT_CONFIG     e.g. ~/.config/cline-nanogpt
#   DEEPSEEK_NANOGPT_MODEL      e.g. deepseek-chat
#   DEEPSEEK_CHUTES_CONFIG      e.g. ~/.config/cline-chutes
#   DEEPSEEK_CHUTES_MODEL       e.g. deepseek-ai/DeepSeek-V3-0324

PROVIDER_NAMES=("openrouter" "nanogpt" "chutes")
PROVIDER_CONFIGS=(
  "${DEEPSEEK_OPENROUTER_CONFIG:-}"
  "${DEEPSEEK_NANOGPT_CONFIG:-}"
  "${DEEPSEEK_CHUTES_CONFIG:-}"
)
PROVIDER_MODELS=(
  "${DEEPSEEK_OPENROUTER_MODEL:-}"
  "${DEEPSEEK_NANOGPT_MODEL:-}"
  "${DEEPSEEK_CHUTES_MODEL:-}"
)

cd "$WORKDIR"

for i in "${!PROVIDER_NAMES[@]}"; do
  name="${PROVIDER_NAMES[$i]}"
  config="${PROVIDER_CONFIGS[$i]}"
  model="${PROVIDER_MODELS[$i]}"

  if [ -z "$config" ] || [ ! -d "$config" ]; then
    echo "DeepSeek provider '$name': config dir not set or missing, skipping."
    continue
  fi
  if [ -z "$model" ]; then
    echo "DeepSeek provider '$name': model not set, skipping."
    continue
  fi

  echo "DeepSeek: trying provider '$name' (model: $model)..."
  if "$CLINE_BIN" task \
      --yolo \
      --json \
      --cwd "$WORKDIR" \
      --config "$config" \
      --model "$model" \
      --timeout "$CLINE_TIMEOUT_SECONDS" \
      "$(cat "$PROMPT_FILE")"; then
    echo "DeepSeek: '$name' succeeded."
    exit 0
  fi
  echo "DeepSeek: '$name' failed, trying next provider..."
done

echo "DeepSeek: all providers (openrouter, nanogpt, chutes) failed or unconfigured."
exit 1
