#!/usr/bin/env bash
set -euo pipefail

ROOT="${ORCH_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
export ORCH_ROOT="$ROOT"

cd "$ROOT"

"$ROOT/.venv/bin/python3" -m orchestrator.pr_monitor
