#!/usr/bin/env bash
# Monthly curated library scout. Cron may invoke it daily; config cadence
# decides when each repo is actually scanned.
set -euo pipefail

# shellcheck source=bin/common_env.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common_env.sh"

log_cron_start "library_scout"

cd "$ROOT"
"$ROOT/.venv/bin/python3" -m orchestrator.library_scout

