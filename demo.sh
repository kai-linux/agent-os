#!/usr/bin/env bash
set -euo pipefail

# ─── Agent OS — 5-minute sandbox demo ───────────────────────────────────────
#
# This script bootstraps a minimal config, creates a test issue, dispatches it
# to an agent, and shows the result — all with just a GitHub token.
#
# Prerequisites: gh (authenticated), python3, claude CLI
# Usage:        ./demo.sh            (uses current repo)
#               ./demo.sh user/repo  (uses a specific repo)
# ────────────────────────────────────────────────────────────────────────────

DEMO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEMO_CONFIG="$DEMO_ROOT/demo.config.yaml"
DEMO_WORKTREES="/tmp/agent-os-demo-worktrees"
DEMO_RUNTIME="$DEMO_ROOT/runtime"

# ─── Colors ─────────────────────────────────────────────────────────────────
bold="\033[1m"
green="\033[32m"
yellow="\033[33m"
cyan="\033[36m"
red="\033[31m"
reset="\033[0m"

info()  { printf "${cyan}▸${reset} %s\n" "$*"; }
ok()    { printf "${green}✓${reset} %s\n" "$*"; }
warn()  { printf "${yellow}!${reset} %s\n" "$*"; }
fail()  { printf "${red}✗${reset} %s\n" "$*"; exit 1; }
header(){ printf "\n${bold}═══ %s ═══${reset}\n\n" "$*"; }

# ─── Preflight checks ──────────────────────────────────────────────────────
header "Agent OS — 5-minute demo"

info "Checking prerequisites..."

command -v gh >/dev/null 2>&1 || fail "gh CLI not found. Install: https://cli.github.com"
gh auth status >/dev/null 2>&1 || fail "gh not authenticated. Run: gh auth login"
command -v python3 >/dev/null 2>&1 || fail "python3 not found"
command -v claude >/dev/null 2>&1 || fail "claude CLI not found. Install: https://docs.anthropic.com/en/docs/claude-code"

ok "All prerequisites met"

# ─── Detect repo ────────────────────────────────────────────────────────────
if [[ -n "${1:-}" ]]; then
  DEMO_REPO="$1"
else
  DEMO_REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || true)"
  if [[ -z "$DEMO_REPO" ]]; then
    fail "Could not detect GitHub repo. Pass it as an argument: ./demo.sh user/repo"
  fi
fi

DEMO_OWNER="${DEMO_REPO%%/*}"
DEMO_LOCAL_REPO="$(pwd)"

ok "Target repo: $DEMO_REPO"

# ─── Set up Python environment ─────────────────────────────────────────────
if [[ ! -d "$DEMO_ROOT/.venv" ]]; then
  info "Creating Python virtual environment..."
  python3 -m venv "$DEMO_ROOT/.venv"
fi
# shellcheck disable=SC1091
source "$DEMO_ROOT/.venv/bin/activate"

info "Installing dependencies..."
pip install -q -r "$DEMO_ROOT/requirements.txt" 2>/dev/null
ok "Python environment ready"

# ─── Generate demo config ──────────────────────────────────────────────────
header "Step 1/4 — Generating config"

mkdir -p "$DEMO_WORKTREES" "$DEMO_RUNTIME/mailbox/inbox" "$DEMO_RUNTIME/logs"

cat > "$DEMO_CONFIG" <<YAML
root_dir: "$DEMO_ROOT"
mailbox_dir: "$DEMO_RUNTIME/mailbox"
logs_dir: "$DEMO_RUNTIME/logs"
worktrees_dir: "$DEMO_WORKTREES"

allowed_repos:
  - $DEMO_LOCAL_REPO

default_agent: claude
default_task_type: implementation
max_runtime_minutes: 10
default_base_branch: main
default_allow_push: false
default_max_attempts: 1
automation_mode: dispatcher_only
max_parallel_workers: 1

agent_fallbacks:
  implementation: [claude]

github_owner: "$DEMO_OWNER"

github_projects:
  demo:
    project_number: 0
    repos:
      - github_repo: "$DEMO_REPO"
        path: "$DEMO_LOCAL_REPO"
        local_repo: "$DEMO_LOCAL_REPO"
        automation_mode: dispatcher_only

trusted_authors:
  - "$DEMO_OWNER"
YAML

ok "Generated $DEMO_CONFIG"

# ─── Create a test issue ───────────────────────────────────────────────────
header "Step 2/4 — Creating test issue"

ISSUE_TITLE="[Demo] Add hello_demo() function to demo_output.py"
ISSUE_BODY="## Goal

Create a file called \`demo_output.py\` in the repository root with a single function:

\`\`\`python
def hello_demo():
    return \"Hello from Agent OS!\"

if __name__ == \"__main__\":
    print(hello_demo())
\`\`\`

## Success Criteria

- File \`demo_output.py\` exists in the repo root
- Contains the \`hello_demo()\` function that returns the greeting string
- File is executable with \`python3 demo_output.py\`

## Constraints

- Single file, no dependencies
- Prefer minimal diffs"

ISSUE_URL="$(gh issue create \
  --repo "$DEMO_REPO" \
  --title "$ISSUE_TITLE" \
  --body "$ISSUE_BODY" \
  --label "ready" 2>/dev/null || true)"

if [[ -z "$ISSUE_URL" ]]; then
  # Label might not exist — create without it
  ISSUE_URL="$(gh issue create \
    --repo "$DEMO_REPO" \
    --title "$ISSUE_TITLE" \
    --body "$ISSUE_BODY")"
fi

ISSUE_NUMBER="$(echo "$ISSUE_URL" | grep -oE '[0-9]+$')"
ok "Created issue #$ISSUE_NUMBER: $ISSUE_URL"

# ─── Build mailbox task directly (skip GitHub Projects requirement) ────────
header "Step 3/4 — Dispatching to agent"

TASK_TS="$(date '+%Y%m%d-%H%M%S')"
TASK_ID="task-${TASK_TS}-demo-hello"
TASK_BRANCH="agent/${TASK_ID}"
TASK_FILE="$DEMO_RUNTIME/mailbox/inbox/${TASK_ID}.md"

cat > "$TASK_FILE" <<TASK
---
task_id: $TASK_ID
repo: $DEMO_LOCAL_REPO
agent: claude
task_type: implementation
branch: $TASK_BRANCH
base_branch: main
allow_push: false
attempt: 1
max_attempts: 1
max_runtime_minutes: 10
model_attempts: []
priority: prio:high
github_project_key: demo
github_repo: $DEMO_REPO
github_issue_number: $ISSUE_NUMBER
github_issue_title: "$ISSUE_TITLE"
github_issue_url: $ISSUE_URL
prompt_snapshot_path: $DEMO_RUNTIME/prompts/${TASK_ID}.txt
outcome_check_ids: []
---

# Goal

Create a file called \`demo_output.py\` in the repository root with a single function:

\`\`\`python
def hello_demo():
    return "Hello from Agent OS!"

if __name__ == "__main__":
    print(hello_demo())
\`\`\`

# Success Criteria

- File \`demo_output.py\` exists in the repo root
- Contains the \`hello_demo()\` function that returns the greeting string
- File is executable with \`python3 demo_output.py\`

# Constraints

- Single file, no dependencies
- Prefer minimal diffs
- Work only inside the repo
TASK

ok "Dispatched task: $TASK_ID"
info "Mailbox task: $TASK_FILE"

# ─── Run the queue (executes the agent) ────────────────────────────────────
header "Step 4/4 — Running agent (claude)"

info "This is the real thing — claude will create a worktree, write code, and produce a result."
info "Watching the queue..."
echo ""

export AGENT_OS_CONFIG="$DEMO_CONFIG"
export ORCH_ROOT="$DEMO_ROOT"

# Run queue directly (single task, no supervisor needed)
"$DEMO_ROOT/.venv/bin/python3" -m orchestrator.queue 2>&1 | while IFS= read -r line; do
  printf "  ${cyan}│${reset} %s\n" "$line"
done
QUEUE_EXIT=${PIPESTATUS[0]}

echo ""

# ─── Show results ───────────────────────────────────────────────────────────
header "Results"

DONE_DIR="$DEMO_RUNTIME/mailbox/done"
FAILED_DIR="$DEMO_RUNTIME/mailbox/failed"
BLOCKED_DIR="$DEMO_RUNTIME/mailbox/blocked"

if ls "$DONE_DIR"/${TASK_ID}* >/dev/null 2>&1; then
  ok "Task completed successfully!"
  echo ""

  # Check if the agent created the file in the worktree
  WORKTREE_PATH="$DEMO_WORKTREES/agent-os/${TASK_ID}"
  if [[ -f "$WORKTREE_PATH/demo_output.py" ]]; then
    info "Agent created demo_output.py:"
    echo ""
    printf "  ${green}"
    cat "$WORKTREE_PATH/demo_output.py"
    printf "${reset}\n"
    echo ""
    info "Running it:"
    OUTPUT="$(cd "$WORKTREE_PATH" && python3 demo_output.py 2>/dev/null || echo "(could not run)")"
    printf "  ${bold}%s${reset}\n" "$OUTPUT"
  fi

  echo ""
  info "Agent result file:"
  RESULT_FILE="$WORKTREE_PATH/.agent_result.md"
  if [[ -f "$RESULT_FILE" ]]; then
    head -20 "$RESULT_FILE" | while IFS= read -r line; do
      printf "  ${cyan}│${reset} %s\n" "$line"
    done
  fi

elif ls "$FAILED_DIR"/${TASK_ID}* >/dev/null 2>&1; then
  warn "Task failed — check logs at: $DEMO_RUNTIME/logs/${TASK_ID}.log"
elif ls "$BLOCKED_DIR"/${TASK_ID}* >/dev/null 2>&1; then
  warn "Task blocked — check logs at: $DEMO_RUNTIME/logs/${TASK_ID}.log"
else
  warn "Task status unknown (exit code: $QUEUE_EXIT)"
  warn "Check logs at: $DEMO_RUNTIME/logs/"
fi

# ─── Cleanup hint ───────────────────────────────────────────────────────────
echo ""
header "Done"
info "Issue:     $ISSUE_URL"
info "Task log:  $DEMO_RUNTIME/logs/${TASK_ID}.log"
info "Worktree:  $DEMO_WORKTREES/agent-os/${TASK_ID}"
info "Config:    $DEMO_CONFIG"
echo ""
info "To clean up:"
echo "  gh issue close $ISSUE_NUMBER --repo $DEMO_REPO"
echo "  rm -rf $DEMO_WORKTREES/agent-os/${TASK_ID}"
echo "  rm $DEMO_CONFIG"
echo ""
ok "That's Agent OS. Clone → demo → see it work. All in under 5 minutes."
