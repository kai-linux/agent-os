# Architecture

```
agent-os/
├── orchestrator/
│   ├── github_dispatcher.py     # Backlog → mailbox, LLM task formatting
│   ├── queue.py                 # Execution engine, routing, retry, escalation
│   ├── supervisor.py            # Parallel worker management, per-repo locks
│   ├── github_sync.py           # Results → GitHub (comments, labels, status)
│   ├── pr_monitor.py            # CI gate, auto-merge, auto-rebase
│   ├── gh_project.py            # GitHub Projects v2 GraphQL + CLI wrapper
│   ├── codebase_memory.py       # CODEBASE.md read/write per repo
│   ├── task_formatter.py        # LLM-powered issue → structured task
│   ├── log_analyzer.py          # Weekly failure analysis → new issues
│   ├── agent_scorer.py          # Weekly execution + business scoring -> structured findings
│   ├── backlog_groomer.py       # Weekly backlog hygiene + task generation
│   ├── objectives.py            # External repo objective loading + scoring
│   └── paths.py                 # Config, path resolution
├── bin/                         # Shell entry points for cron
├── tests/                       # Pytest suite (CI runs on every push/PR)
├── .github/workflows/ci.yml     # GitHub Actions: lint + test
└── CODEBASE.md                  # Auto-maintained institutional memory
```

## The Team

Every startup needs roles. Agent OS fills them all:

| Role | Who | What they do | When |
|---|---|---|---|
| **Engineers** | Codex, Claude, Gemini, DeepSeek | Write code, fix bugs, implement features | Continuously |
| **Project Manager** | `github_dispatcher.py` | Triages the backlog, assigns work, reformats sloppy tickets | Every minute |
| **Tech Lead** | `queue.py` | Picks the right engineer for the job, manages retries and handoffs | Per task |
| **Code Reviewer** | `pr_monitor.py` | Watches CI, approves merges, resolves conflicts | Every 5 min |
| **Analyst** | `log_analyzer.py` | Synthesizes one remediation backlog from operational evidence and files issues | Monday 07:00 |
| **Performance Lead** | `agent_scorer.py` | Scores execution reliability and business-outcome movement from external objectives, then emits structured findings for the analyzer | Monday 07:00 |
| **Backlog Groomer** | `backlog_groomer.py` | Prunes stale work, surfaces risks, generates new tasks | Config-driven cadence |
| **Institutional Memory** | `CODEBASE.md` | Records what was done, why, and what broke — readable by all agents | After every task |

The backlog is GitHub Issues. The sprint board is GitHub Projects. The standup is Telegram. The office is a $5/month VPS.

## Design Principles

**GitHub is the entire control plane.** Issues = tasks. Project board = sprint. PRs = review gate. Issue comments = audit log. Telegram = alerts only. There is no second system.

**Markdown files, not message brokers.** The mailbox queue is `runtime/mailbox/inbox/*.md`. You can `ls` it. You can `cat` a task. You can manually move a file from `blocked/` back to `inbox/` to retry. No Redis. No Kafka. No state you can't see.

**Isolated worktrees, not branch gymnastics.** Every task runs in its own copy of the repo. Agents can't interfere with each other. The main checkout is never touched.

**One contract, many agents.** `.agent_result.md` is the only interface between the system and any agent. Swap Codex for a new model tomorrow — nothing else changes.

**Memory that compounds.** `CODEBASE.md` is committed to `main` after every completed task. Each agent reads it before starting. Over time, the team builds institutional knowledge — architecture decisions, known gotchas, file purposes. Like onboarding docs that write themselves.

## Agent Routing

```yaml
agent_fallbacks:
  implementation:     [codex, claude, gemini, deepseek]
  debugging:          [claude, codex, gemini, deepseek]
  architecture:       [claude, codex]
  research:           [claude, codex]
  docs:               [claude, codex]
  design:             [claude, codex]
  content:            [claude, codex]
  browser_automation: [claude, codex, gemini, deepseek]

planner_agents: [claude, codex]
```

Issues can specify a preferred agent. The dispatcher can auto-detect task type. Priority labels (`prio:high`, `prio:normal`, `prio:low`) influence scheduling order.

## Observability

| What happened | Where you see it |
|---|---|
| Task dispatched / completed / blocked | Telegram |
| Manual action required | Telegram + GitHub issue comment (secret-aware redaction) |
| CI failure on agent PR | GitHub issue comment + Telegram + `blocked` label |
| Agent underperforming | GitHub issue (filed weekly) |
| Failure pattern detected | GitHub issue (filed weekly) |
| Full execution trace | `runtime/logs/` |
| Structured metrics | `runtime/metrics/agent_stats.jsonl` |
| Accumulated repo knowledge | `CODEBASE.md` |

## Safety

| Mechanism | What it prevents |
|---|---|
| **Repo allowlist** | Agents can't touch repos not in config |
| **Repo-local config/objectives** | One visible source of truth for orchestration and planning inputs |
| **Isolated worktrees** | No shared mutable state between tasks |
| **Attempt ceilings** | `max_attempts=4` + per-model tracking stops loops |
| **Structured escalation** | System writes a note and stops — never thrashes |
| **CI gate** | Nothing merges without passing checks |
| **Force-push guard** | Rebases use `--force-with-lease` |
| **Per-repo locks** | Parallel workers can't corrupt the same repo |
