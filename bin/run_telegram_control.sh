#!/usr/bin/env bash
# Always-on Telegram control-tower poller.
#
# Polls Telegram for /on, /off, /status, /help commands and inline-button
# callbacks. Deliberately bypasses the kill-switch (AGENT_OS_IGNORE_DISABLED=1)
# so /on can reach the orchestrator while it is paused — otherwise the switch
# could only ever be flipped from the shell.

set -euo pipefail

export AGENT_OS_IGNORE_DISABLED=1

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$DIR/common_env.sh"

cd "$ORCH_ROOT"

log_cron_start run_telegram_control

exec python3 -c '
from orchestrator.paths import load_config, runtime_paths
from orchestrator.queue import process_telegram_callbacks

cfg = load_config()
paths = runtime_paths(cfg)
process_telegram_callbacks(cfg, paths)
'
