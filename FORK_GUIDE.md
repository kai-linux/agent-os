# Fork Guide — Customizing Agent OS

This guide is for solo builders who have forked Agent OS and want to adapt it
to a different product, a different agent lineup, or a different workflow. It
documents the **high-leverage customization entry points** with working
examples. If you are only trying to run Agent OS on your own repo with the
default setup, read the README quickstart instead — this guide is for bending
the system, not booting it.

Everything below is a "change this file, redispatch a task, done" pattern. No
refactoring required.

## Table of contents

1. [Fork checklist](#fork-checklist)
2. [Agent routing](#1-agent-routing--which-agent-runs-which-task)
3. [Adding a new agent](#2-adding-a-new-agent)
4. [Task dispatch](#3-task-dispatch--issue--task-spec)
5. [Prompt configuration](#4-prompt-configuration--what-the-agent-sees)
6. [Objectives & scoring](#5-objectives--scoring--what-success-means)
7. [Per-repo overrides](#6-per-repo-overrides)
8. [Pre-commit & secret guard](#7-pre-commit--secret-guard)
9. [What NOT to fork](#what-not-to-fork)

---

## Fork checklist

Before you customize anything, do these once:

```bash
# 1. Fork on GitHub, then clone your fork
git clone https://github.com/<you>/agent-os && cd agent-os
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Create your own config — config.yaml is .gitignored
cp example.config.yaml config.yaml
$EDITOR config.yaml

# 3. Enable the secret guard so you never commit config.yaml or a bot token
git config core.hooksPath hooks

# 4. Create an objective file for your repo
cp objectives/example.yaml objectives/myrepo.yaml    # local-only; gitignored
$EDITOR objectives/myrepo.yaml
```

Files that stay **local-only** on your fork (already in `.gitignore`):
`config.yaml`, `objectives/<repo>.yaml` (except `example.yaml`),
`runtime/`, `.agent_result.md`.

---

## 1. Agent routing — which agent runs which task

**Where:** `config.yaml` → `agent_fallbacks` (read by `queue.py:get_agent_chain` at `orchestrator/queue.py:2352`).

The queue picks an agent by `task_type`, falls back down the list on failure,
and skips agents that the health gate has benched.

```yaml
# config.yaml
default_agent: auto
default_task_type: implementation

agent_fallbacks:
  implementation: [codex, claude, gemini, deepseek]
  debugging:      [claude, codex, gemini, deepseek]   # claude first for bugs
  architecture:   [claude, codex]                     # planning needs context
  research:       [claude, codex]
  docs:           [claude, codex]
  design:         [claude, codex]
  content:        [claude, codex]
  browser_automation: [claude, codex, gemini, deepseek]

# The strategic planner has its own narrow list (it is control-plane, not task-plane):
planner_agents: [claude, codex]
```

Valid agents today: `claude`, `codex`, `gemini`, `deepseek`
(`VALID_ASSIGNABLE_AGENTS` in `orchestrator/queue.py:2349`).

**Per-agent timeout:**
```yaml
agent_timeout_minutes:
  codex: 40
  claude: 45
  gemini: 35
  deepseek: 30
```

**Pinning one task to one agent.** Put this in the GitHub issue body:
```markdown
## Agent Preference
claude
```
`parse_issue_body` (`orchestrator/github_dispatcher.py:88`) lifts it into
`meta["agent"]`; `get_agent_chain` then prepends it to the fallback list.

---

## 2. Adding a new agent

To wire a brand-new CLI agent (e.g. `llama`) into the pool:

**Step A — add a runner branch** in `bin/agent_runner.sh`:
```bash
elif [ "$AGENT" = "llama" ]; then
    "${LLAMA_BIN:-llama}" --prompt-file "$PROMPT" --auto
```

**Step B — register it** in `orchestrator/queue.py:2349`:
```python
VALID_ASSIGNABLE_AGENTS = {"auto", "claude", "codex", "gemini", "deepseek", "llama"}
```

**Step C — add to a fallback chain** in `config.yaml`:
```yaml
agent_fallbacks:
  implementation: [llama, codex, claude]
agent_timeout_minutes:
  llama: 30
```

**Step D — give it pricing** so cost tracking works (`cost_tracking.agent_models` in `config.yaml`):
```yaml
cost_tracking:
  agent_models:
    llama: "meta/llama-3.1-70b"
  model_overrides:
    llama:
      input_per_million_tokens: 0.5
      output_per_million_tokens: 0.75
```

Any agent you add must obey the `.agent_result.md` contract (see README → Key
Design Choices). If your CLI does not write that file, wrap it in a shell
script that extracts its output and emits the contract. Everything downstream
— scorer, PR monitor, log analyzer — keys off that file.

---

## 3. Task dispatch — issue → task spec

**Where:** `orchestrator/github_dispatcher.py:parse_issue_body` (line 88),
`orchestrator/task_formatter.py:FORMAT_PROMPT` (line 11).

Agent OS reads GitHub issue bodies for these sections:

```markdown
## Goal
<what you want done>

## Success Criteria
- <measurable outcome>

## Task Type
implementation

## Agent Preference
auto

## Constraints
- Prefer minimal diffs

## Context
<optional background>

## Base Branch
main

## Branch
feature/custom-name       # optional; auto-generated if omitted
```

If the body is free-form, `task_formatter.format_task` calls Claude Haiku
(configurable via `FORMATTER_MODEL` env var) to rewrite it into this shape
before dispatch.

### Adding a new task_type

Task types are just strings. The only place they are gated is the routing
table. To introduce `cost_optimization`:

1. Add it to the `FORMAT_PROMPT` whitelist in `orchestrator/task_formatter.py:29`:
   ```python
   task_type: one of implementation, debugging, architecture, research,
     docs, browser_automation, design, content, cost_optimization.
   ```
2. Add a fallback chain in `config.yaml`:
   ```yaml
   agent_fallbacks:
     cost_optimization: [claude, codex]
   ```
3. Write issues with `## Task Type\ncost_optimization`.

That is it. No schema migrations, no registration step — task_type is metadata
that flows through the queue unchanged.

### Labels → priority

```yaml
priority_weights:
  prio:high: 30
  prio:normal: 10
  prio:low: 0
```
The dispatcher reads GitHub labels like `prio:high` and adds the matching
score when ranking the Ready queue.

---

## 4. Prompt configuration — what the agent sees

**Where:** `orchestrator/queue.py:write_prompt` (line 1936) is the single
assembly point. It layers:

1. Task metadata (YAML front-matter)
2. Task body (from the issue)
3. **Repository Context** — built by `repo_context.build_execution_context`
   (`orchestrator/repo_context.py:271`). Reads, in order:
   - `README.md` (the `## Goal` section)
   - `NORTH_STAR.md`
   - `STRATEGY.md`
   - `PLANNING_PRINCIPLES.md`
   - `RUBRIC.md` (optional)
   - For research/debugging tasks: `PRODUCTION_FEEDBACK.md`, `PRODUCT_INSPECTION.md`, `PLANNING_RESEARCH.md`
4. `CODEBASE.md` — auto-maintained memory of recent work
5. **Dispatch Context** — recent git state, objective alignment, sprint directives

### Easiest customization: edit the markdown files

No code change required. Drop your product vision into `NORTH_STAR.md`,
your sprint rules into `STRATEGY.md`, your quality bar into `RUBRIC.md`.
Every agent invocation picks them up on the next run:

```markdown
# NORTH_STAR.md (yours)
We optimize for:
- Weekly active users (target: 10k by Q3)
- Free→paid conversion (baseline 2.1%, target 3.5%)
- Core web vitals: LCP < 2s on p75
Never: ship features that increase LCP by more than 100ms.
```

### Injecting a custom context file

To pipe a file the system does not know about (e.g. `SECURITY_BASELINE.md`)
into every prompt, add a reader in `orchestrator/repo_context.py` and list it
in `build_execution_context`:

```python
# orchestrator/repo_context.py
def read_security_baseline(repo_path: Path) -> str:
    return _read_file_section(repo_path / "SECURITY_BASELINE.md", max_chars=2000)

def build_execution_context(repo_path, task_type, body):
    sections = [
        ("Product Goal (README.md)", read_readme_goal(repo_path)),
        ("North Star (NORTH_STAR.md)", read_north_star(repo_path)),
        ...
        ("Security Baseline (SECURITY_BASELINE.md)", read_security_baseline(repo_path)),
    ]
    ...
```

Budget it — every section eats into the agent's context window. Keep custom
sections under ~2 KB and show only the parts that change behavior.

### Changing the result contract

`write_prompt` also embeds the required output schema (the `STATUS: / DONE: /
NEXT_STEP:` block you see at the bottom of every prompt). Downstream code
(`queue.parse_agent_result`, `pr_monitor`, `log_analyzer`) parses these
fields. If you change the schema, you also change the parsers — generally
**don't**, unless you are adding a new field alongside the existing ones.

---

## 5. Objectives & scoring — what success means

**Where:** `objectives/<repo>.yaml`, loaded by
`orchestrator/objectives.py:load_repo_objective`. One file per managed repo.

This is the hinge for evidence-driven planning. The strategic planner, the
backlog groomer, and the outcome attribution loop all read the objective file
to decide what "shipped something useful" means.

```yaml
# objectives/myrepo.yaml  (local-only, never committed)
version: 1
repo: "myorg/myrepo"
product_name: "My SaaS"
primary_outcome: "Grow profitable activations"
evaluation_window_days: 28

interpretation_scores:
  improved: 1.0
  unchanged: 0.0
  regressed: -1.0
  inconclusive: -0.35

metrics:
  - id: "activations"
    name: "Weekly activations"
    weight: 0.5
    direction: "increase"
    source:
      type: "file"
      path: "~/.local/share/agent-os/evidence/myrepo/activations_latest.yaml"
      provenance: "Mixpanel export written nightly by cron"
      trust_level: "high"
    outcome_check:
      type: "file"
      path: "~/.local/share/agent-os/evidence/myrepo/activations_post_merge.yaml"
      measurement_window_days: 7
      comparison_window: "7 days after merge vs 7 days before"

  - id: "infra_cost"
    name: "Cost per activation (USD)"
    weight: 0.5
    direction: "decrease"
    source:
      type: "file"
      path: "~/.local/share/agent-os/evidence/myrepo/cost_latest.yaml"
      provenance: "GCP billing export"
      trust_level: "high"
    outcome_check:
      type: "file"
      path: "~/.local/share/agent-os/evidence/myrepo/cost_post_merge.yaml"
      measurement_window_days: 14
      comparison_window: "14-day cost ratio post-merge vs prior 14 days"
```

**Rules of thumb:**
- Weights should sum to ~1.0. They control how the scorer blends signals.
- Keep evidence **outside** the repo (`~/.local/share/agent-os/evidence/...`)
  so metric snapshots never enter git.
- If the objective file is missing, the planner and scorer fall back to raw
  execution metrics (task success rate, completion time). The system keeps
  running; it just gets less product-aware.

---

## 6. Per-repo overrides

**Where:** `config.yaml` → `github_projects.<key>.repos[]`.

Everything global can be overridden per repo. This is how you run Agent OS
across multiple repos with different cadences, different agents, different
objectives:

```yaml
github_projects:
  my-project:
    repos:
      - github_repo: "myorg/frontend"
        path: "/srv/repos/frontend"
        automation_mode: full
        plan_size: 5
        sprint_cadence_days: 7
        agent_fallbacks:
          implementation: [claude, codex]     # frontend → claude-first
        quality_harness:
          enabled: true
          suites: [unit, browser_e2e]
          suite_commands:
            unit: "npm test"
            browser_e2e: "npx playwright test"

      - github_repo: "myorg/backend"
        path: "/srv/repos/backend"
        automation_mode: dispatcher_only      # manual Ready → dispatch → PR only
        agent_fallbacks:
          implementation: [codex, claude]     # backend → codex-first
```

`automation_mode`:
- `full` — planner, groomer, log analyzer, PR monitor all active
- `dispatcher_only` — you manually move issues to Ready; everything else is off

Flip modes at runtime from Telegram: `/repo mode frontend dispatcher`
(see README → Pause & Resume).

---

## 7. Pre-commit & secret guard

**Where:** `hooks/pre-commit`. Enable with `git config core.hooksPath hooks`.

The hook blocks:
- `config.yaml` (holds your bot token)
- `objectives/*.yaml` except `objectives/example.yaml`
- `.agent_result.md` (worktree artifact, never part of a commit)
- Any staged diff line matching the Telegram bot-token shape (`\d{9,10}:[A-Za-z0-9_-]{35}`)

To extend it (e.g. block `.env`), add a case arm to `hooks/pre-commit:22`:
```bash
.env|.env.local)
  red "✖ refusing to commit $f — environment secrets."
  fail=1
  ;;
```

Bypass only for a verified false positive: `git commit --no-verify`.

---

## What NOT to fork

Things that look customizable but usually aren't worth touching:

- **`.agent_result.md` contract** — every downstream module parses it.
  Changing it means patching the scorer, PR monitor, log analyzer, and
  supervisor. Add fields, don't rename.
- **`CODEBASE.md` format** — written by `orchestrator/codebase_memory.py` after
  every merge. Rewriting its shape breaks the planner's "recent changes"
  prompt section.
- **Mailbox directory layout** — `runtime/mailbox/<task_id>/{task.md,result/}`
  is assumed by the queue and the supervisor. Move it with
  `mailbox_dir` in `config.yaml`, not by renaming folders.
- **GitHub Project status field names** — `Ready`, `In Progress`, `Blocked`,
  `Done` are configurable via `github_project_*_value` in `config.yaml`. Keep
  four states; the dispatcher, PR monitor, and groomer all encode this
  transition model.

If you find yourself wanting to change one of these, open an issue describing
the use case before you patch — there is usually a less invasive entry point.

---

## Further reading

| Topic | File |
|---|---|
| Architecture, components, data flow | [docs/architecture.md](docs/architecture.md) |
| Task execution, handoff contract, retry logic | [docs/execution.md](docs/execution.md) |
| Full configuration reference | [docs/configuration.md](docs/configuration.md) |
| Deployment on a $5 VPS | [docs/deployment-guide.md](docs/deployment-guide.md) |
| Cost tracking model | [docs/cost-tracking.md](docs/cost-tracking.md) |
| Cron entrypoints | [CRON.md](CRON.md) |

Questions or patterns worth adding to this guide?
[Open a discussion](https://github.com/kai-linux/agent-os/discussions).
