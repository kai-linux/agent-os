#!/usr/bin/env bash
# Generate weekly adoption funnel report and send Telegram summary.
# Designed to run via cron every Monday at 07:30 UTC.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# shellcheck source=bin/common_env.sh
[ -f "$SCRIPT_DIR/common_env.sh" ] && source "$SCRIPT_DIR/common_env.sh"

cd "$REPO_ROOT"

echo "[$(date -Iseconds)] adoption_report start" >&2
python3 -m orchestrator.adoption_report

# Auto-commit report if changed
if git diff --quiet docs/adoption-reports/ 2>/dev/null; then
  echo "No adoption report changes to commit."
else
  git add docs/adoption-reports/
  git commit -m "chore: refresh weekly adoption report" --no-gpg-sign 2>/dev/null || true
  git push origin HEAD 2>/dev/null || echo "Push skipped (non-fatal)."
fi

echo "[$(date -Iseconds)] adoption_report done" >&2
