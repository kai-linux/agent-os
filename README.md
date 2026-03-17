# Agent OS

GitHub-native autonomous engineering loop for AI coding agents.

This system turns GitHub Issues + GitHub Project into the control plane, and uses a mailbox queue plus isolated git worktrees to let Codex, Claude, Gemini, and DeepSeek execute tasks recursively with fallbacks, escalation, and Telegram alerts.

---

## What this does

- Uses **GitHub Issues** as task objects
- Uses a **GitHub Project** as the kanban board / source of truth
- Dispatches `ready` issues into a local mailbox queue
- Runs AI coding agents in isolated **git worktrees**
- Supports **multi-model routing and fallback**
- Writes structured `.agent_result.md` handoff files
- Creates recursive follow-up tasks when needed
- Escalates gracefully instead of looping forever
- Sends status updates to Telegram
- Comments results back to GitHub issues and updates project status

---

## High-level architecture

```text
GitHub Issue
   ↓
GitHub Project (Status = Ready)
   ↓
github_dispatcher.py
   ↓
runtime/mailbox/inbox/*.md
   ↓
queue.py
   ↓
git worktree
   ↓
AI agent (Codex / Claude / Gemini / DeepSeek)
   ↓
.agent_result.md
   ↓
GitHub comment + project status sync + optional PR
   ↓
Done / Blocked / Escalated / Follow-up
```

Core concepts
1. GitHub is the control plane

GitHub replaces OpenClaw as the main orchestration layer.

Use:

Issues for tasks

Project for board state

PRs for review boundary

Issue comments for memory / progress logs

Telegram is only for:

alerts

summaries

manual intervention

2. The mailbox is the execution queue

The dispatcher converts GitHub issues into markdown task files.

Those tasks are written into:

runtime/mailbox/inbox/

The queue picks them up, executes them, and moves them into:

done/

blocked/

failed/

escalated/

3. Every task runs in an isolated git worktree

Each task is executed in its own temporary worktree:

/srv/worktrees/<repo>/<task-id>

This prevents branch collisions and keeps the base repo clean.

4. Agents must write .agent_result.md

Every agent run must produce a structured result file in the repo root:

.agent_result.md

This is the handoff contract that powers recursion, fallbacks, and escalation.
```
Repo structure
agent-os/
├── README.md
├── requirements.txt
├── config.yaml.example
├── .github/
│   └── ISSUE_TEMPLATE/
│       └── agent-task.md
├── bin/
│   ├── run_dispatcher.sh
│   ├── run_queue.sh
│   ├── agent_runner.sh
│   └── run_deepseek.sh
└── orchestrator/
    ├── __init__.py
    ├── paths.py
    ├── gh_project.py
    ├── github_dispatcher.py
    ├── github_sync.py
    └── queue.py
```
Components
orchestrator/github_dispatcher.py

Scans GitHub repos for ready issues, converts one into a queue task, writes it into the mailbox, comments on the issue, and moves the project item to In Progress.

orchestrator/queue.py

Main execution engine.

Responsibilities:

read mailbox task

create worktree

resolve agent / fallback chain

run the agent

parse .agent_result.md

commit / push branch

sync result back to GitHub

create recursive follow-up task if needed

escalate if retries are exhausted

orchestrator/github_sync.py

Pushes execution results back into GitHub:

adds issue comments

updates labels

moves project item status

optionally creates a PR

orchestrator/gh_project.py

Thin wrapper around gh CLI for:

issue listing

issue comments

labels

project item lookup

project field updates

PR creation

orchestrator/paths.py

Loads config and resolves all runtime paths relative to the repo.

Issue-driven workflow
1. Create an issue

Use the provided issue template.

The issue should contain:

Goal

Success Criteria

Repo

Task Type

Agent Preference

Constraints

Context

2. Mark it ready

Add the ready label and move the project status to Ready.

3. Dispatcher picks it up

The dispatcher writes a normalized task into:

runtime/mailbox/inbox/
4. Queue executes it

The queue:

creates a branch

creates a worktree

picks the best model

runs the task

commits and pushes changes

5. Result is synced

Depending on the outcome:

complete → comment on issue, optionally create PR, move to Review

partial / blocked → comment progress, move to Blocked, create follow-up task

escalated → write escalation note, comment it, move to Blocked / Escalated

Supported agents

Codex

Claude

Gemini

DeepSeek via Cline/OpenRouter wrapper

Routing is task-type aware and supports fallback chains.

Example config:
```
agent_fallbacks:
  implementation: [codex, claude, gemini, deepseek]
  debugging: [claude, gemini, codex, deepseek]
  architecture: [claude, gemini, codex, deepseek]
  research: [claude, gemini, codex, deepseek]
  docs: [claude, gemini, codex, deepseek]
  browser_automation: [claude, gemini, codex, deepseek]
  ```
Task types

Supported task types:

implementation

debugging

architecture

research

docs

browser_automation

These affect routing and fallback order.

.agent_result.md contract

Every agent must write:

STATUS: complete|partial|blocked

SUMMARY:
One short paragraph.

DONE:
- bullet
- bullet

BLOCKERS:
- bullet
- bullet

NEXT_STEP:
One short paragraph. If complete, write: None

FILES_CHANGED:
- path
- path

TESTS_RUN:
- command + result
- command + result

DECISIONS:
- bullet
- bullet

RISKS:
- bullet
- bullet

ATTEMPTED_APPROACHES:
- bullet
- bullet

This enables:

recursive continuation

anti-repeat behavior

escalation notes

better handoffs across models

Recursion and follow-ups

If a task returns:

STATUS: partial

STATUS: blocked

the queue can generate a new follow-up mailbox task automatically.

Follow-up tasks preserve:

original task

prior summary

blockers

files changed

tests run

risks

attempted approaches

model attempts already tried

This allows the system to continue work without losing context.

Anti-loop protection

Each task has:

attempt: 1
max_attempts: 4
model_attempts: []

Protection exists at two levels:

Task-level attempts

Prevents endless recursive follow-up loops.

Model-level attempts

Prevents trying the same fallback chain forever.

GitHub Project status flow

Recommended statuses:

Backlog

Ready

In Progress

Review

Blocked

Done

Recommended labels:

ready

in-progress

review

blocked

task:implementation

task:debugging

task:architecture

task:docs

task:browser

prio:high

prio:normal

prio:low


Installation
1. Clone the repo
git clone <your-agent-os-repo>
cd agent-os
2. Create virtualenv
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
3. Copy config
cp config.yaml.example config.yaml

Edit config.yaml.

4. Install system dependencies

Ubuntu example:

sudo apt update
sudo apt install -y git curl util-linux cron python3-venv
5. Install / authenticate CLIs

Required:

gh

codex

claude

gemini

cline (if using DeepSeek fallback)

Also make sure:

gh auth status
gh auth refresh -s project
6. Ensure allowed repos exist

GitHub setup
1. Create a project

Create a GitHub Project and add a single-select field named:

Status

With values:

Ready

In Progress

Review

Blocked

Done

2. Enable built-in project workflows

Recommended:

auto-add items from repos

mark closed/merged work as Done

3. Add the issue template

Place the provided template under:

.github/ISSUE_TEMPLATE/agent-task.md
Runtime scripts
bin/run_dispatcher.sh

Runs the GitHub issue → mailbox dispatcher.

bin/run_queue.sh

Runs the main execution queue.

bin/agent_runner.sh

Routes execution to Codex / Claude / Gemini / DeepSeek wrapper.

bin/run_deepseek.sh

Wrapper for DeepSeek via Cline/OpenRouter.

Cron

Example cron setup:

* * * * * PATH=/home/kai/.nvm/versions/node/v24.13.0/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin /home/kai/agent-os/bin/run_dispatcher.sh >> /home/kai/agent-os/runtime/logs/dispatcher.log 2>&1
* * * * * PATH=/home/kai/.nvm/versions/node/v24.13.0/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin /home/kai/agent-os/bin/run_queue.sh >> /home/kai/agent-os/runtime/logs/cron.log 2>&1
Telegram alerts

Telegram is optional but recommended for:

start notifications

fallback notifications

blocked notifications

completion

escalation

fatal errors

It is not used for dispatch.

Safety model
Allowed repos

The queue refuses to execute tasks outside configured repo paths.

Isolated worktrees

Each task runs in its own branch/worktree.

Retry ceilings

Both recursive attempts and model fallbacks are bounded.

Escalation notes

When automation runs out of productive options, it writes a structured escalation note instead of thrashing.

Current status

This system is good for:

recursive coding workflows

GitHub-native task execution

model routing / fallback

issue-driven automation

bounded software work

task memory via issue comments + result files

It is not yet:

a parallel swarm scheduler

a browser-ops platform

a full growth/content/trading operating system

But it is the right core for building those.

Planned upgrades

parallel worker slots

per-repo locks

reviewer loop

planner loop

GitHub issue auto-creation from summaries / metrics

browser automation lane

KPI / executive summary loop

automatic PR merge policies

Philosophy

This system treats AI agents as stateless workers and adds the missing pieces that make them operational:

task state

routing

recursion

anti-repeat memory

escalation

observability

GitHub-native coordination

The result is not a chatbot.

It is an engineering operating loop.