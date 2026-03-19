#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=bin/common_env.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common_env.sh"

cd "$ROOT"
"$ROOT/.venv/bin/python3" -m orchestrator.github_dispatcher
