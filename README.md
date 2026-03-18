# Agent OS

**A fully autonomous AI startup team — engineers, planners, reviewers, and analysts — running 24/7 without human intervention.**

Agent OS turns GitHub Projects into a living operations layer for AI coding agents. Drop a task into the backlog, set it to Ready, and walk away. The system dispatches it to the best available agent, executes it in isolation, opens a PR, runs CI, merges on green, and — if anything breaks — debugs, retries, escalates, and files new improvement tasks automatically.

The team improves itself. Every Monday it analyzes its own failure logs and creates tickets to fix its weaknesses. Every Saturday it grooms the backlog. Agents score each other. The system is the product.

---

## The Vision

Most AI coding tools answer questions. Agent OS runs a company.

The mental model is a startup engineering team, fully staffed by AI:

| Role | Component | Cadence |
|---|---|---|
| **Engineers** | Codex, Claude, Gemini, DeepSeek | Continuous (every minute) |
| **Dispatcher / PM** | `github_dispatcher.py` | Every minute |
| **Tech Lead** | `queue.py` — routing, fallback, retry | Per task |
| **Reviewer** | `pr_monitor.py` — CI gate, auto-merge | Every 5 minutes |
| **Analyst** | `log_analyzer.py` — failure pattern detection | Every Monday 07:00 |
| **Performance Lead** | `agent_scorer.py` — model success rates | Every Monday 07:00 |
| **Backlog Groomer** | `backlog_groomer.py` — stale issues, new tasks | Every Saturday 20:00 |
| **Memory** | `CODEBASE.md` per repo | After every task |

The backlog is GitHub. The sprint board is GitHub Projects. The standup is Telegram. Nobody needs to be there.

---

## How It Works

```
You write (or the system generates) a GitHub Issue
              ↓
    Set Status → Ready on the kanban board
              ↓
    github_dispatcher.py picks it up (every minute)
    - LLM-formats poorly-written notes into structured tasks
    - Routes by repo, task type, priority
    - Creates mailbox task, comments on issue, moves to In Progress
              ↓
    queue.py executes it
    - Spins up an isolated git worktree
    - Selects best agent (task-type-aware, priority-weighted)
    - Runs agent with full repo context + CODEBASE.md memory
    - Parses .agent_result.md handoff contract
    - Pushes branch, opens PR
              ↓
    pr_monitor.py watches CI (every 5 minutes)
    - All checks green → squash-merge, delete branch
    - Conflicts → auto-rebase, retry
    - CI failure → comment, label blocked, escalate after 3 attempts
              ↓
    Complete: issue closed, project → Done, Telegram alert
    Blocked:  follow-up task created, project → Blocked, Telegram alert
    Manual:   MANUAL_STEPS section posted to issue + Telegram
```

### Recursive self-improvement loop

Every week the system reads its own logs and creates tasks to fix what broke:

```
agent_stats.jsonl + queue-summary.log
              ↓
    log_analyzer.py (Monday 07:00)
    - Identifies top 3 failure patterns via Claude Haiku
    - Deduplicates against open issues
    - Files GitHub issues with structured task specs
              ↓
    Those issues enter the backlog → get dispatched → agents fix them
              ↓
    agent_scorer.py (Monday 07:00)
    - Computes per-model success rates (last 7 days)
    - Files issues for any model below 60% success
              ↓
    backlog_groomer.py (Saturday 20:00)
    - Surfaces stale issues, Known Issues without tickets, risk flags
    - Generates 3-5 new targeted improvement tasks via Claude Haiku
    - Semantic dedup (difflib, 0.75 threshold) prevents duplicates
```

The system improves itself on a weekly cadence without any human input.

---

## Architecture

```
agent-os/
├── orchestrator/
│   ├── github_dispatcher.py   # GitHub → mailbox, LLM task formatting
│   ├── queue.py               # Execution engine, routing, retry, escalation
│   ├── github_sync.py         # Write results back to GitHub
│   ├── pr_monitor.py          # CI polling, auto-merge, auto-rebase
│   ├── gh_project.py          # GitHub CLI / GraphQL wrapper
│   ├── codebase_memory.py     # Per-repo CODEBASE.md read/write
│   ├── task_formatter.py      # LLM issue formatting (Haiku)
│   ├── log_analyzer.py        # Weekly failure analysis → new issues
│   ├── agent_scorer.py        # Weekly model performance scoring
│   ├── backlog_groomer.py     # Weekly backlog hygiene + task generation
│   ├── supervisor.py          # Parallel worker management
│   └── paths.py               # Config loading, path resolution
├── bin/
│   ├── run_dispatcher.sh      # Cron entry: dispatcher
│   ├── run_queue.sh           # Cron entry: execution queue
│   ├── run_pr_monitor.sh      # Cron entry: PR auto-merge
│   ├── run_log_analyzer.sh    # Cron entry: weekly log analysis
│   ├── run_agent_scorer.sh    # Cron entry: model performance
│   ├── run_backlog_groomer.sh # Cron entry: backlog grooming
│   ├── agent_runner.sh        # Routes to correct agent binary
│   └── run_deepseek.sh        # DeepSeek with provider fallback chain
├── tests/                     # Pytest suite (runs in CI on every push)
├── .github/
│   ├── workflows/ci.yml       # GitHub Actions: lint + pytest
│   └── ISSUE_TEMPLATE/
│       └── agent-task.md      # Structured task template
├── example.config.yaml
└── CODEBASE.md                # Auto-maintained repo memory
```

### Key design decisions

**GitHub is the control plane.** Issues are tasks. The Project board is the sprint. PRs are the review boundary. Issue comments are the audit log. Nothing lives outside GitHub.

**Mailbox queue, not a message broker.** Tasks are markdown files on disk. Simple, inspectable, recoverable. No Redis, no RabbitMQ.

**Isolated git worktrees.** Every task runs in `/srv/worktrees/<repo>/<task-id>`. Branch collisions are impossible. The base repo is never touched.

**`.agent_result.md` is the agent contract.** Every agent must write this file. It powers recursion, fallbacks, escalation, and memory — without any agent needing to know about the others.

**CODEBASE.md is shared memory.** After every completed task, a summary is committed to `CODEBASE.md` on main. The next agent reads it before starting. Context accumulates over time.

---

## Agent Routing

Agents are selected by task type and tried in fallback order:

```yaml
agent_fallbacks:
  implementation:    [codex, claude, gemini, deepseek]
  debugging:         [claude, gemini, codex, deepseek]
  architecture:      [claude, gemini, codex, deepseek]
  research:          [claude, gemini, codex, deepseek]
  docs:              [claude, gemini, codex, deepseek]
  browser_automation:[claude, gemini, codex, deepseek]
```

DeepSeek itself has a provider fallback chain: `openrouter → nanogpt → chutes`.

If an agent returns `blocked`, the next agent in the chain gets the full context of what was tried. If all agents are exhausted, the task is escalated.

---

## The `.agent_result.md` Contract

Every agent must produce this file in the repo root before exiting:

```
STATUS: complete | partial | blocked

SUMMARY:
One paragraph.

DONE:
- what was accomplished

BLOCKERS:
- what prevented completion

NEXT_STEP:
What the follow-up agent should do. Write "None" if complete.

FILES_CHANGED:
- path/to/file.py

TESTS_RUN:
- pytest -q → 22 passed

DECISIONS:
- why approach X was chosen over Y

RISKS:
- anything that might break downstream

ATTEMPTED_APPROACHES:
- what was tried and why it didn't work

MANUAL_STEPS:
- cron entries, config changes, or secrets that need human action
- Write "None" if nothing required
```

`MANUAL_STEPS` are posted to the GitHub issue and sent as a separate Telegram alert so nothing requiring human action gets silently buried.

---

## Project Board Flow

```
Backlog → Ready → In Progress → Review → Done
                                       ↘ Blocked
```

Trigger dispatch: set Status to **Ready** on the kanban board (or add label `ready` — either works).

The system handles every transition from there.

---

## Cron Schedule

```cron
# Core loop
* * * * *  run_dispatcher.sh     # Dispatch ready issues
* * * * *  run_queue.sh          # Execute queued tasks
*/5 * * * * run_pr_monitor.sh    # CI check + auto-merge

# Weekly self-improvement
0 7 * * 1  run_log_analyzer.sh   # Analyze failures, file issues
0 7 * * 1  run_agent_scorer.sh   # Score model performance
0 20 * * 6 run_backlog_groomer.sh # Groom backlog, generate tasks
```

---

## Setup

### 1. Prerequisites

```bash
# System
sudo apt install -y git curl util-linux python3-venv

# CLI tools (must be authenticated)
gh auth login
gh auth refresh -s project   # Required for GraphQL project access
# Install: codex, claude, gemini (+ cline for DeepSeek)
```

### 2. Install

```bash
git clone https://github.com/yourname/agent-os
cd agent-os
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp example.config.yaml config.yaml
# Edit config.yaml — set github_owner, project numbers, repo paths, Telegram
```

### 3. GitHub Project

Create a GitHub Project with a **Status** single-select field:
`Backlog · Ready · In Progress · Review · Blocked · Done`

Set `project_number` in `config.yaml` to match (find it in the project URL: `/projects/N`).

### 4. Cron

```cron
PATH=/home/kai/.nvm/versions/node/v24.13.0/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

* * * * *  /home/kai/agent-os/bin/run_dispatcher.sh >> /home/kai/agent-os/runtime/logs/dispatcher.log 2>&1
* * * * *  /home/kai/agent-os/bin/run_queue.sh >> /home/kai/agent-os/runtime/logs/cron.log 2>&1
*/5 * * * * /home/kai/agent-os/bin/run_pr_monitor.sh >> /home/kai/agent-os/runtime/logs/pr_monitor.log 2>&1
0 7 * * 1  /home/kai/agent-os/bin/run_log_analyzer.sh >> /home/kai/agent-os/runtime/logs/log_analyzer.log 2>&1
0 7 * * 1  /home/kai/agent-os/bin/run_agent_scorer.sh >> /home/kai/agent-os/runtime/logs/agent_scorer.log 2>&1
0 20 * * 6 /home/kai/agent-os/bin/run_backlog_groomer.sh >> /home/kai/agent-os/runtime/logs/backlog_groomer.log 2>&1
```

### 5. Multi-repo setup

Each GitHub Project maps to one or more repos. All orchestrated from agent-os. Agents for different repos run in parallel without interfering — per-repo locking ensures clean worktree management.

---

## Safety

- **Allowlist**: queue refuses tasks outside configured `allowed_repos` paths
- **Isolated worktrees**: every task runs on its own branch, no shared state
- **Retry ceilings**: `max_attempts` (default 4) + per-model attempt tracking
- **Escalation**: when automation runs out of options, it writes a structured note and stops — it does not thrash
- **CI gate**: PRs are not merged until all checks pass
- **Force-push guard**: rebases use `--force-with-lease`

---

## Observability

| Signal | Where |
|---|---|
| Task start/complete/blocked | Telegram |
| Manual steps required | Telegram + GitHub issue comment |
| CI failure | GitHub issue comment + label |
| Agent performance degradation | GitHub issue (weekly) |
| Failure pattern analysis | GitHub issues (weekly) |
| Task audit trail | GitHub issue comments |
| Execution logs | `runtime/logs/` |
| Metrics | `runtime/metrics/agent_stats.jsonl` |
| Repo memory | `CODEBASE.md` (per repo, auto-updated) |

---

## What It Is Not (Yet)

- A browser-ops platform (planned)
- A multi-tenant SaaS
- A general-purpose agent framework

It is a focused, production-running autonomous engineering loop. The architecture is intentionally minimal — GitHub + cron + git worktrees + LLM APIs. No orchestration middleware, no vector databases, no proprietary runtimes.

The system is its own best customer. Every improvement gets filed as an issue, executed by an agent, and merged automatically.
