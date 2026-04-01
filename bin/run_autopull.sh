#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=bin/common_env.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common_env.sh"

log_cron_start "autopull"

cd "$ROOT"
git pull --rebase --autostash || true

# Push any local-only commits (e.g. CODEBASE.md updates from agents)
if [ "$(git rev-list --count @{u}..HEAD 2>/dev/null)" -gt 0 ] 2>/dev/null; then
  git push
fi
