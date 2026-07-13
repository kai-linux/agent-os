#!/usr/bin/env bash
set -euo pipefail

# shellcheck source=bin/common_env.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common_env.sh"

log_cron_start "autopull"

cd "$ROOT"

# Runtime deploys are immutable and operator-approved. Write a full commit SHA
# to runtime/deploy-approved-sha during an explicit deployment. This checkout
# never pushes and never follows a mutable branch tip.
APPROVED_SHA_FILE="${AGENTOS_APPROVED_SHA_FILE:-$ROOT/runtime/deploy-approved-sha}"
if [[ ! -f "$APPROVED_SHA_FILE" ]]; then
  echo "No approved deploy SHA at $APPROVED_SHA_FILE; leaving runtime unchanged."
  exit 0
fi

APPROVED_SHA="$(tr -d '[:space:]' < "$APPROVED_SHA_FILE")"
if [[ ! "$APPROVED_SHA" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Invalid approved deploy SHA; expected 40 lowercase hexadecimal characters." >&2
  exit 1
fi

git fetch --quiet origin main
if ! git merge-base --is-ancestor "$APPROVED_SHA" origin/main; then
  echo "Approved SHA is not an ancestor of origin/main; refusing deployment." >&2
  exit 1
fi

if [[ "$(git rev-parse HEAD)" != "$APPROVED_SHA" ]]; then
  git checkout --detach "$APPROVED_SHA"
fi
