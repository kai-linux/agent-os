#!/usr/bin/env bash
# Shared environment bootstrap for cron-safe agent-os entrypoints.

if [[ -n "${AGENT_OS_COMMON_ENV_LOADED:-}" ]]; then
  return 0
fi
export AGENT_OS_COMMON_ENV_LOADED=1

ROOT="${ORCH_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
export ORCH_ROOT="$ROOT"

append_path() {
  local dir="$1"
  [[ -d "$dir" ]] || return 0
  case ":$PATH:" in
    *":$dir:"*) ;;
    *) PATH="$dir:$PATH" ;;
  esac
}

# Common user-local CLI install locations.
append_path "$HOME/.local/bin"
append_path "$HOME/bin"
append_path "/opt/homebrew/bin"
append_path "/usr/local/bin"
append_path "/usr/bin"
append_path "/bin"

# Node-based CLIs often live under NVM-managed bins.
if [[ -z "${NVM_DIR:-}" ]]; then
  export NVM_DIR="$HOME/.nvm"
fi
if [[ -s "$NVM_DIR/nvm.sh" ]]; then
  # shellcheck disable=SC1090
  . "$NVM_DIR/nvm.sh" >/dev/null 2>&1 || true
fi
if [[ -d "$NVM_DIR/versions/node" ]]; then
  while IFS= read -r -d '' node_bin; do
    append_path "$node_bin"
  done < <(find "$NVM_DIR/versions/node" -maxdepth 2 -type d -name bin -print0 2>/dev/null | sort -rz)
fi

pick_bin() {
  local preferred="$1"
  local fallback="$2"
  if [[ -n "$preferred" ]] && command -v "$preferred" >/dev/null 2>&1; then
    command -v "$preferred"
    return 0
  fi
  if command -v "$fallback" >/dev/null 2>&1; then
    command -v "$fallback"
    return 0
  fi
  printf '%s' "$fallback"
}

export PATH
export CODEX_BIN="${CODEX_BIN:-$(pick_bin "${CODEX_BIN:-}" codex)}"
export CLAUDE_BIN="${CLAUDE_BIN:-$(pick_bin "${CLAUDE_BIN:-}" claude)}"
export GEMINI_BIN="${GEMINI_BIN:-$(pick_bin "${GEMINI_BIN:-}" gemini)}"
export CLINE_BIN="${CLINE_BIN:-$(pick_bin "${CLINE_BIN:-}" cline)}"

log_cron_start() {
  local job_name="${1:-$(basename "$0")}"
  printf '[%s] %s start\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$job_name" >&2
}
