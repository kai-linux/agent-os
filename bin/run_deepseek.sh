#!/usr/bin/env bash
set -euo pipefail

WORKDIR="$1"
PROMPT_FILE="$2"

CLINE_BIN="${CLINE_BIN:-cline}"
CLINE_TIMEOUT_SECONDS="${CLINE_TIMEOUT_SECONDS:-1800}"

# Provider fallback order: openrouter -> nanogpt -> chutes
# Each provider needs its own Cline config dir with the correct API key/base URL set.
# Set these env vars to enable each provider:
#   DEEPSEEK_OPENROUTER_CONFIG  e.g. /home/kai/.config/openrouter
#   DEEPSEEK_OPENROUTER_MODEL   e.g. deepseek/deepseek-chat
#   DEEPSEEK_NANOGPT_CONFIG     e.g. /home/kai/.config/cline-nanogpt
#   DEEPSEEK_NANOGPT_MODEL      e.g. deepseek-chat
#   DEEPSEEK_CHUTES_CONFIG      e.g. /home/kai/.config/cline-chutes
#   DEEPSEEK_CHUTES_MODEL       e.g. deepseek-ai/DeepSeek-V3-0324
#
# Example config dir layout for /home/kai/.config/openrouter:
#   /home/kai/.config/openrouter/
#   ├── globalState.json
#   ├── secrets.json
#   └── settings/
#       └── cline_mcp_settings.json
#
# Example /home/kai/.config/openrouter/globalState.json:
#   {
#     "actModeApiProvider": "openrouter",
#     "actModeOpenRouterModelId": "deepseek/deepseek-chat",
#     "planModeApiProvider": "openrouter",
#     "planModeOpenRouterModelId": "deepseek/deepseek-chat"
#   }
#
# Example /home/kai/.config/openrouter/secrets.json:
#   {
#     "openRouterApiKey": "YOUR_OPENROUTER_API_KEY"
#   }
#
# Example /home/kai/.config/openrouter/settings/cline_mcp_settings.json:
#   {
#     "mcpServers": {}
#   }
#
# The directory passed via --config should point at the folder containing these files.

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
PROVIDER_RESULTS=()

cd "$WORKDIR"

for i in "${!PROVIDER_NAMES[@]}"; do
  name="${PROVIDER_NAMES[$i]}"
  config="${PROVIDER_CONFIGS[$i]}"
  model="${PROVIDER_MODELS[$i]}"

  if [ -z "$config" ] || [ ! -d "$config" ]; then
    msg="DeepSeek provider '$name': config dir not set or missing, skipping."
    echo "$msg"
    PROVIDER_RESULTS+=("$msg")
    continue
  fi
  if [ -z "$model" ]; then
    msg="DeepSeek provider '$name': model not set, skipping."
    echo "$msg"
    PROVIDER_RESULTS+=("$msg")
    continue
  fi

  echo "DeepSeek: trying provider '$name' (model: $model)..."
  set +e
  "$CLINE_BIN" task \
    --yolo \
    --json \
    --cwd "$WORKDIR" \
    --config "$config" \
    --model "$model" \
    --timeout "$CLINE_TIMEOUT_SECONDS" \
    "$(cat "$PROMPT_FILE")"
  exit_code=$?
  set -e

  if [ "$exit_code" -eq 0 ]; then
    msg="DeepSeek: provider '$name' succeeded."
    echo "$msg"
    PROVIDER_RESULTS+=("$msg")
    exit 0
  fi
  msg="DeepSeek: provider '$name' failed with exit code $exit_code, trying next provider."
  echo "$msg"
  PROVIDER_RESULTS+=("$msg")
done

echo "DeepSeek: all providers (openrouter, nanogpt, chutes) failed or unconfigured."
for result in "${PROVIDER_RESULTS[@]}"; do
  echo "DeepSeek summary: $result"
done
exit 1
