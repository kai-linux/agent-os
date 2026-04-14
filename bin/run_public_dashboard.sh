#!/usr/bin/env bash
# Daily public reliability dashboard export from existing repo metrics artifacts.
# Regenerates docs/reliability/ from live agent_stats.jsonl + PRODUCTION_FEEDBACK.md
# and commits the result so GitHub always shows current metrics.
set -euo pipefail

# shellcheck source=bin/common_env.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common_env.sh"

log_cron_start "public_dashboard"

cd "$ROOT"
"$ROOT/.venv/bin/python3" -m orchestrator.public_dashboard

if git -C "$ROOT" diff --quiet docs/reliability/; then
  echo "Dashboard unchanged, skipping commit."
else
  git -C "$ROOT" add docs/reliability/README.md docs/reliability/metrics.json docs/reliability/index.html
  git -C "$ROOT" commit -m "chore: refresh reliability dashboard"
  git -C "$ROOT" push || echo "Push failed (non-fatal)"
fi
