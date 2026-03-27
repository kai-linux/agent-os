# Agent OS

> What if you could hire an entire engineering team that works 24/7, never calls in sick, debugs its own failures, writes its own improvement tickets, and gets better every week — without you ever opening a laptop?

Agent OS is that team.

It's not a copilot. It's not a chatbot. It's a **fully autonomous software organization** — staffed by AI agents, managed by cron, coordinated through GitHub, and designed to run indefinitely without human input.

You give it a backlog. It ships product.

---

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

Nobody needs to be there.

---

## The Loop

```
                    ┌─────────────────────────────────────────┐
                    │                                         │
                    ▼                                         │
            GitHub Issue (Backlog)                            │
                    │                                         │
            Status → Ready                                    │
                    │                                         │
            ┌───────▼────────┐                                │
            │   Dispatcher   │  LLM-formats task              │
            │   (every min)  │  Routes by repo + type         │
            └───────┬────────┘                                │
                    │                                         │
            ┌───────▼────────┐                                │
            │  Queue Engine  │  Worktree → Agent → Result     │
            │                │  Retry / Fallback / Escalate   │
            └───────┬────────┘                                │
                    │                                         │
              Push branch, open PR                            │
                    │                                         │
            ┌───────▼────────┐                                │
            │  PR Monitor    │  CI green → merge              │
            │  (every 5 min) │  Conflict → rebase             │
            └───────┬────────┘  Failure → escalate            │
                    │                                         │
              Issue closed, board → Done                      │
                    │                                         │
        ┌───────────┴───────────┐                             │
        ▼                       ▼                             │
  Log Analyzer            Backlog Groomer                     │
  (Monday)                (Saturday)                          │
  Reads failure logs      Reads open issues                   │
  Files fix tickets ──────────────────────────────────────────┘
```

**That last arrow is the point.** The system files tickets about its own failures. Those tickets enter the backlog. The agents fix them. The fixes get merged. Next week, the system is better. Indefinitely.

---

## Recursive Self-Improvement

This is the part that makes Agent OS different from a task runner.

Every Monday at 07:00, two things happen automatically:

1. **`agent_scorer.py`** computes execution degradation signals plus weighted business-outcome findings from external repo objectives and writes them to `runtime/analysis/agent_scorer_findings.json`. It does not open issues directly.

2. **`log_analyzer.py`** reads the last 7 days of execution metrics, the queue summary log, and the scorer findings artifact. It synthesizes exactly one remediation backlog from that evidence, deduplicates overlapping signals, and files GitHub issues with explicit `## Evidence` and `## Reasoning` sections.

Every Saturday at 20:00:

3. **`backlog_groomer.py`** scans every repo for stale issues (>30 days), Known Issues in `CODEBASE.md` that don't have linked tickets, and risk flags from recent agent results. It generates 3-5 new improvement tasks. Semantic deduplication (0.75 similarity threshold) prevents duplicate issues from piling up.

The planner and groomer are safe to invoke frequently from cron. Each repo has its own cadence in config (`sprint_cadence_days`, `groomer_cadence_days`), fractional days are supported, and `0` means dormant. The configured cadence is a minimum interval, not a scheduler by itself: the job only runs when cron invokes it, so cron must run at least as often as your shortest desired cadence.

Strategic planning also supports an explicit early-refresh policy. `planner_allow_early_refresh: true` lets a repo refresh its sprint before the next cadence tick when the current sprint has gone idle; `false` enforces strict cadence. The setting can be defined globally, per project, or per repo, with repo overrides winning.

Repos can also opt into a manual execution lane with `automation_mode: dispatcher_only`. In that mode, Agent OS still accepts human-created issues, dispatches work when you move an issue to `Ready`, and opens PRs on completion, but it skips the planner, backlog groomer, weekly analyzer/scorer issue generation, PR auto-merge/CI remediation, and queue-generated self-healing follow-up tasks for that repo.

The `bin/` entrypoints bootstrap common user-local CLI install paths themselves, so cron usually does not need per-provider `PATH` or `CLAUDE_BIN` overrides.

Configuration is intentionally split from repo-controlled code. In production, Agent OS should load `~/.config/agent-os/config.yaml` rather than a tracked `config.yaml` inside the repository. Per-repo business objectives live alongside it in `~/.config/agent-os/objectives/<repo>.yaml`, while raw analytics and other external evidence should live in a separate external directory such as `~/.local/share/agent-os/evidence/`. That keeps the true reward surface and private metric sources outside the surfaces agents can push to GitHub.

## Objective Loop

Use this setup for a business-driven repo such as a private web app.

### 1. What To Create

You need 3 things:

1. `~/.config/agent-os/config.yaml`
2. `~/.config/agent-os/objectives/<repo>.yaml`
3. external evidence files in `~/.local/share/agent-os/evidence/<repo>/`

### 2. What Each File Does

`config.yaml`
- tells agent-os where repos are
- enables planning, production feedback, and outcome attribution
- points to the objectives directory and evidence directory

`objectives/<repo>.yaml`
- defines the business objective
- defines which metrics matter
- defines their weights
- defines where the evidence for those metrics lives
- defines how post-merge outcomes are judged

`evidence/<repo>/*.yaml`
- contains metric snapshots from GA4 or other external systems
- is written by your external exporter, not by agent-os

### 3. How To Define The Objective

In `~/.config/agent-os/objectives/<repo>.yaml`, define:

- `primary_outcome`
- `evaluation_window_days`
- `metrics`

Each metric should have:

- `id`
- `name`
- `weight`
- `direction`
- `source`
- `outcome_check`

For a web app, start with exactly these 4 metrics:

- `traffic`
- `conversion`
- `retention`
- `arpu`

### 4. How To Define Evidence

For each metric, create:

- one `*_latest.yaml` file for planning
- one `*_post_merge.yaml` file for post-merge outcome measurement

Example minimal set:

- `ga4_traffic_latest.yaml`
- `ga4_conversion_latest.yaml`
- `ga4_retention_latest.yaml`
- `ga4_arpu_latest.yaml`

Add these when you want closed-loop post-merge scoring:

- `ga4_traffic_post_merge.yaml`
- `ga4_conversion_post_merge.yaml`
- `ga4_retention_post_merge.yaml`
- `ga4_arpu_post_merge.yaml`

### 5. How Reward Works

Reward is the weighted business score implied by the objective file.

Example:

- traffic weight: `0.20`
- conversion weight: `0.35`
- retention weight: `0.25`
- arpu weight: `0.20`

If outcomes improve, the score goes positive. If outcomes stay flat, it stays near zero. If outcomes regress, it goes negative.

### 6. How Penalty Works

Penalty is not loss of autonomy. Penalty is negative value in the score.

Current interpretation mapping:

- `improved: 1.0`
- `unchanged: 0.0`
- `regressed: -1.0`
- `inconclusive: -0.35`

So penalty means:

- work shipped
- measured business outcomes did not improve, or got worse
- future planning and scoring should treat that direction as less valuable

### 7. Minimal First Version

Start with only:

- [example.config.yaml](/Users/kai/agent-os/example.config.yaml)
- [example.objective.yaml](/Users/kai/agent-os/example.objective.yaml)
- 4 GA4 evidence files for `traffic`, `conversion`, `retention`, and `arpu`

Ignore extra signals until the core loop is working.

Sprint selection is guided by three layers of context:
- `README.md` for the public product goal
- `STRATEGY.md` for sprint-to-sprint strategy memory
- `PLANNING_PRINCIPLES.md` for the stable north-star rubric the planner should optimize toward over time

Execution uses the same context model with different depth:
- high-level context: `README.md`, `STRATEGY.md`, `PLANNING_PRINCIPLES.md`
- evidence layers: `PRODUCTION_FEEDBACK.md` and `PLANNING_RESEARCH.md` when the task is strategic or evidence-driven
- low-level context: `CODEBASE.md`

That keeps context dynamic without collapsing high-level product direction, sprint-local strategy, bounded research, and code-level memory into a single bloated document.

These generated issues are indistinguishable from human-written ones. They enter the same queue, get dispatched to the same agents, go through the same CI → merge pipeline. The system literally engineers itself.

Ownership is intentionally split:
- `agent_scorer.py` measures execution degradation plus business-outcome movement and emits structured findings only.
- `log_analyzer.py` owns evidence synthesis, prioritization, deduplication, and remediation issue creation.
- `backlog_groomer.py` owns backlog hygiene and repo-context-driven task generation, not operational incident synthesis.

---

## How Tasks Execute

### 1. Dispatch

A human (or the system) creates a GitHub Issue. It can be a polished spec or a one-line note — the dispatcher's LLM formatter will restructure it into a proper task with goal, success criteria, constraints, and agent preference.

Set the Project status to **Ready** (or add the `ready` label — either triggers dispatch).

If you want only this manual `Ready -> dispatch -> PR` flow for a repo, set that repo's `automation_mode` to `dispatcher_only` in config.

### 2. Execution

The queue engine:
- Creates an **isolated git worktree** — each task gets its own branch in `/srv/worktrees/<repo>/<task-id>`, so agents never collide
- Selects the best agent based on **task type and priority** (implementation → Codex first, debugging → Claude first)
- Injects **CODEBASE.md** — the repo's accumulated memory from all prior agent work
- Runs the agent with a structured prompt including prior attempt history (if this is a retry)
- Parses the **`.agent_result.md`** handoff contract

### 3. Handoff

Every agent must write `.agent_result.md` before exiting — the universal contract:

```
STATUS: complete | partial | blocked
BLOCKER_CODE: none | missing_context | missing_credentials | environment_failure | dependency_blocked | quota_limited | runner_failure | timeout | test_failure | manual_intervention_required | fallback_exhausted | invalid_result_contract
SUMMARY: ...
DONE: ...
BLOCKERS: ...
NEXT_STEP: ...
FILES_CHANGED: ...
TESTS_RUN: ...
DECISIONS: ...
RISKS: ...
ATTEMPTED_APPROACHES: ...
MANUAL_STEPS: ...
```

This file is what makes multi-agent collaboration work. When an agent is blocked, the next agent in the fallback chain receives everything the first one tried, what failed, and what to do next. No context is lost between handoffs.

`BLOCKER_CODE` is required whenever `STATUS` is `partial` or `blocked`; `complete` outcomes should use `none`.

`MANUAL_STEPS` (cron entries, config changes, secrets) are surfaced back to GitHub for agent continuity, with secret values redacted.

### 4. Review & Merge

`pr_monitor.py` polls every 5 minutes:
- **CI green** → squash-merge, delete branch, close issue, move board to Done
- **Merge conflict** → auto-rebase onto main, force-push with lease, retry next poll
- **CI failure** → comment on issue with failed checks, redacting any detected secret material, label as blocked, retry up to 3 times, then escalate

### 5. Retry & Escalation

If a task returns `partial` or `blocked`:
- A **follow-up task** is created automatically with full prior context
- The next agent in the **fallback chain** takes over (e.g., Codex failed → Claude tries)
- After `max_attempts` (default 4), the system **escalates** — writes a structured note and stops

The system never thrashes. It tries, it hands off, it escalates. Like a real team.

---

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

planning_research:
  enabled: true
  max_age_hours: 72
  max_sources: 4
  max_source_chars: 4000
  allowed_domains: [docs.example.com, competitor.example.com]
  artifact_file: PLANNING_RESEARCH.md
  sources:
    - name: Official docs
      type: web
      kind: official_docs
      url: https://docs.example.com/changelog
    - name: Competitor pricing
      type: web
      kind: competitor
      url: https://competitor.example.com/pricing
    - name: Launch notes
      type: file
      kind: repo_reference
      path: ../shared/launch-notes.md

production_feedback:
  enabled: true
  max_age_hours: 24
  stale_after_hours: 72
  minimum_trust_level: medium
  allowed_privacy_levels: [public, internal]
  max_inputs: 6
  max_source_chars: 4000
  allowed_domains: [analytics.example.com, community.example.com, status.example.com, app.example.com]
  artifact_file: PRODUCTION_FEEDBACK.md
  inputs:
    - name: Weekly activation funnel
      type: web
      signal_class: analytics
      url: https://analytics.example.com/public/weekly-funnel
      observed_at: 2026-03-19T08:00:00Z
      provenance: Public dashboard snapshot
      trust_level: high
      trust_note: Aggregated product analytics exported for planning
      privacy: public
      privacy_note: Aggregated public-safe metrics only
    - name: Community export requests
      type: file
      signal_class: user_feedback
      path: ../shared/community-feedback.md
      observed_at: 2026-03-18T17:00:00Z
      provenance: Maintainer-curated summary of public issue comments
      trust_level: medium
      trust_note: Qualitative sample; treat volume as directional
      privacy: public
      privacy_note: Summary only; no raw user identifiers
    - name: Signup flow inspection
      type: web
      signal_class: product_inspection
      url: https://app.example.com/signup
      observed_at: 2026-03-19T09:30:00Z
      provenance: Maintainer walkthrough of public product surface
      trust_level: medium
      trust_note: Manual inspection; validate before large workflow changes
      privacy: public
      privacy_note: Public product surface only
    - name: Weekly availability summary
      type: file
      signal_class: incident_slo
      path: ../shared/status-summary.md
      observed_at: 2026-03-19T06:00:00Z
      provenance: Bounded incident summary exported from status tooling
      trust_level: high
      trust_note: Operational summary reviewed by maintainer
      privacy: public
      privacy_note: Service-level summary only; no customer-identifying logs
      input_type: market_signal
      url: https://competitor.example.com/pricing
      observed_at: 2026-03-18T10:00:00Z
      provenance: Public pricing page
      trust_note: Public market signal; validate before major roadmap shifts
      privacy_note: Public web content

outcome_attribution:
  enabled: true
  max_source_chars: 4000
  allowed_domains: [analytics.example.com]
  checks:
    - id: activation_rate
      name: Activation rate
      type: web
      url: https://analytics.example.com/public/activation-after-release
      measurement_window_days: 7
      comparison_window: Compare 7 days after merge vs 7 days before merge
    - id: community_feedback
      name: Community feedback pulse
      type: file
      path: ../shared/community-feedback-after-release.md
      measurement_window_days: 3
      comparison_window: Compare the post-release comment volume against the prior 3 days
```

## Self-improvement

What Each File Does

config.yaml

tells agent-os where repos are
enables planning, feedback, and outcome attribution
points to the objectives directory and evidence directory
objectives/<repo>.yaml

defines the business objective
defines which metrics matter
defines their weights
defines where the evidence for those metrics lives
defines how post-merge outcomes are judged
evidence/<repo>/*.yaml

contains the actual metric snapshots from GA4 or other systems
is written by your external exporter, not by agent-os
How To Define The Objective

In ~/.config/agent-os/objectives/repo1.yaml, define:

primary_outcome
Example: Grow profitable user acquisition and monetization

evaluation_window_days
Example: 28

metrics
Each metric needs:

id
name
weight
direction
source
outcome_check
For your web app, start with exactly these 4:

traffic
conversion
retention
arpu
How To Define Evidence

For each metric, create one “latest” evidence file and optionally one “post-merge” evidence file.

Examples:

~/.local/share/agent-os/evidence/repo1/ga4_traffic_latest.yaml
~/.local/share/agent-os/evidence/repo1/ga4_conversion_latest.yaml
~/.local/share/agent-os/evidence/repo1/ga4_retention_latest.yaml
~/.local/share/agent-os/evidence/repo1/ga4_arpu_latest.yaml
These are for planning.

Then:

~/.local/share/agent-os/evidence/repo1/ga4_traffic_post_merge.yaml
~/.local/share/agent-os/evidence/repo1/ga4_conversion_post_merge.yaml
~/.local/share/agent-os/evidence/repo1/ga4_retention_post_merge.yaml
~/.local/share/agent-os/evidence/repo1/ga4_arpu_post_merge.yaml
These are for outcome attribution after changes ship.

How Reward Works

Reward is not a separate file.
Reward is the weighted score implied by the objective.

Example:

traffic weight: 0.20
conversion weight: 0.35
retention weight: 0.25
arpu weight: 0.20
If outcomes improve:

positive score
If outcomes stay flat:

near zero
If outcomes regress:

negative score
So reward = weighted movement in business metrics.

How Penalty Works

Penalty is also not a separate file.
Penalty comes from negative interpretations in the objective scoring.

Current interpretation mapping is:

improved: 1.0
unchanged: 0.0
regressed: -1.0
inconclusive: -0.35
So penalty means:

work shipped
measured business outcomes did not improve, or got worse
planner/scorer should treat that direction as less valuable
This is the correct kind of punishment here:
not “remove autonomy”
but “assign negative value to unproductive work”.

What You Actually Do Step By Step

Copy example.config.yaml to ~/.config/agent-os/config.yaml
Copy example.objective.yaml to ~/.config/agent-os/objectives/repo1.yaml
Edit the objective so the 4 metrics match your business
Write a small exporter that dumps GA4 snapshots into ~/.local/share/agent-os/evidence/repo1/
Enable production_feedback
Enable outcome_attribution
Let planner use *_latest.yaml
Let scorer use *_post_merge.yaml

## Fallbacks

DeepSeek has its own provider fallback: `openrouter → nanogpt → chutes`. It is kept last in the chain by default because it depends on extra provider configuration and should not consume retries when those providers are unavailable.

Strategic planning uses its own narrow fallback chain (`planner_agents`) so the control plane does not stall on a single Claude quota event and does not spray planning work across every model.

Repos can opt into bounded pre-planning research with `planning_research`. Before sprint selection, the planner refreshes `PLANNING_RESEARCH.md` only when it is older than `max_age_hours`; otherwise it reuses the existing artifact. Research is intentionally constrained to explicitly configured `https` URLs on allowed domains plus relative repo or repo-adjacent files. There is no search step and no open-ended browsing path.

Repos can also opt into bounded `production_feedback`. The first version supports explicit signal classes for `analytics`, `user_feedback`, `product_inspection`, and `incident_slo` (with legacy `planning_signals` config still accepted for backward compatibility). Before sprint selection, the planner refreshes `PRODUCTION_FEEDBACK.md` from configured web or file sources, plus any objective-derived business metrics, normalizes each entry with source, observed time, freshness, provenance, trust, privacy, and planning implications, and injects that artifact into strategic planning, backlog grooming, and evidence-heavy execution prompts.

Guardrails are explicit and inspectable. Each feedback entry carries `trust_level`, `privacy`, `trust_note`, and `privacy_note`, while repo config sets `minimum_trust_level`, `allowed_privacy_levels`, and `stale_after_hours`. Entries that are stale, too low-trust, or too privacy-sensitive remain visible in the artifact but are marked `Planning Use: guarded`, so they do not silently influence prioritization.

Repos can opt into bounded post-merge measurement with `outcome_attribution`. Issues attach one or more configured check IDs in an `## Outcome Checks` section, for example `- activation_rate`. When the task PR is opened and later merged, Agent OS records the task, issue, PR, and check IDs in `runtime/metrics/outcome_attribution.jsonl`. During planning and retrospectives, the planner refreshes due snapshots from the configured file or public-safe web sources, records a timestamped interpretation (`improved`, `unchanged`, `regressed`, or `inconclusive`), and surfaces that evidence alongside shipped work. External objective files can inject these checks automatically so merged work is evaluated against business metrics instead of repo-local completion proxies. If no measurable external metric is attached, the merge is still tracked explicitly as `inconclusive`.

Issues can specify a preferred agent. The dispatcher can auto-detect task type. Priority labels (`prio:high`, `prio:normal`, `prio:low`) influence scheduling order.

---

## Architecture

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

### Design Principles

**GitHub is the entire control plane.** Issues = tasks. Project board = sprint. PRs = review gate. Issue comments = audit log. Telegram = alerts only. There is no second system.

**Markdown files, not message brokers.** The mailbox queue is `runtime/mailbox/inbox/*.md`. You can `ls` it. You can `cat` a task. You can manually move a file from `blocked/` back to `inbox/` to retry. No Redis. No Kafka. No state you can't see.

**Isolated worktrees, not branch gymnastics.** Every task runs in its own copy of the repo. Agents can't interfere with each other. The main checkout is never touched.

**One contract, many agents.** `.agent_result.md` is the only interface between the system and any agent. Swap Codex for a new model tomorrow — nothing else changes.

**Memory that compounds.** `CODEBASE.md` is committed to `main` after every completed task. Each agent reads it before starting. Over time, the team builds institutional knowledge — architecture decisions, known gotchas, file purposes. Like onboarding docs that write themselves.

---

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

---

## Safety

| Mechanism | What it prevents |
|---|---|
| **Repo allowlist** | Agents can't touch repos not in config |
| **External config/objectives** | Reward surface and private evidence sources stay outside pushable repo state |
| **Isolated worktrees** | No shared mutable state between tasks |
| **Attempt ceilings** | `max_attempts=4` + per-model tracking stops loops |
| **Structured escalation** | System writes a note and stops — never thrashes |
| **CI gate** | Nothing merges without passing checks |
| **Force-push guard** | Rebases use `--force-with-lease` |
| **Per-repo locks** | Parallel workers can't corrupt the same repo |

---

## Quick Start

```bash
# 1. Clone and install
git clone https://github.com/yourname/agent-os && cd agent-os
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure outside the repo
mkdir -p ~/.config/agent-os/objectives ~/.local/share/agent-os/evidence
cp example.config.yaml ~/.config/agent-os/config.yaml
cp example.objective.yaml ~/.config/agent-os/objectives/repo1.yaml
# Edit: github_owner, project_number, repo paths, Telegram token, and the 4 objective metrics

# 3. Authenticate
gh auth login && gh auth refresh -s project
# Ensure codex, claude, gemini CLIs are installed and authenticated

# 4. Create GitHub Project
# Add Status field: Backlog · Ready · In Progress · Blocked · Done
# Note the project number from the URL (/projects/N) → put in ~/.config/agent-os/config.yaml

# 5. Set up cron
crontab -e
```

```cron
* * * * *   /path/to/agent-os/bin/run_dispatcher.sh  >> runtime/logs/dispatcher.log 2>&1
* * * * *   /path/to/agent-os/bin/run_queue.sh        >> runtime/logs/cron.log 2>&1
*/5 * * * * /path/to/agent-os/bin/run_pr_monitor.sh   >> runtime/logs/pr_monitor.log 2>&1
0 * * * *   /path/to/agent-os/bin/run_strategic_planner.sh >> runtime/logs/strategic_planner.log 2>&1
0 * * * *   /path/to/agent-os/bin/run_backlog_groomer.sh >> runtime/logs/backlog_groomer.log 2>&1
0 7 * * 1   /path/to/agent-os/bin/run_log_analyzer.sh >> runtime/logs/log_analyzer.log 2>&1
0 7 * * 1   /path/to/agent-os/bin/run_agent_scorer.sh >> runtime/logs/agent_scorer.log 2>&1
```

Create your first issue. Set it to Ready. Watch the system work.

---

## The Philosophy

Most AI tools make individual developers faster. Agent OS asks a different question: **what if the developers were optional?**

Not because humans aren't valuable — but because most engineering work is structured, bounded, and repetitive enough that a well-orchestrated team of AI agents can handle it autonomously. The hard part was never the coding. It was the coordination: task state, routing, context preservation, failure recovery, quality gates, and institutional memory.

Agent OS solves coordination. The agents do the rest.

The system is its own first customer. This README was one of its tasks. The CI pipeline it runs on was built by an agent. The backlog groomer that generates its improvement tickets was written by an agent dispatched from a ticket that was generated by the log analyzer. It's turtles all the way down.

That's the vision: not a tool you use, but a team you deploy.

## Roadmap

Agent OS is not meant to stop at task execution. Its job is to bootstrap itself from a reliable execution engine into an evidence-driven, closed-loop operator that can grow products with decreasing human supervision.

That means the repository should be evolved intentionally toward Level 4 and beyond. New orchestration, planning, research, memory, and feedback systems should be judged by whether they move Agent OS up this ladder.

Level 1: Reliable execution engine
- dispatch, queue, retries, CI, merge, memory

Level 2: Strategic planning
- persistent strategy
- retrospectives
- backlog shaping
- sprint selection

Level 3: Evidence-driven planning
- live product inspection
- external research
- analytics/user feedback input
- domain-specific evaluation

Level 4: Closed-loop optimization
- hypothesis generation
- experiments
- outcome measurement
- autonomous iteration

Level 5+: Self-directed growth
- Agent OS expands its own capabilities, operating surface, and quality bar
- it identifies missing subsystems, builds them, validates them, and folds them back into the loop
- human input becomes governance, constraint-setting, and occasional intervention, not day-to-day direction

Current position: approximately Level 2. The system already has reliable execution, persistent memory, backlog grooming, strategic planning, retrospectives, and self-healing CI remediation. The next bottleneck is evidence: richer product inspection, research, analytics, and measurable outcomes.
