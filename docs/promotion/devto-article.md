---
title: "I Built an Autonomous Software Organization That Manages Its Own Development"
published: false
description: "Agent OS shipped 85 merged PRs, closed 110 issues, and produced 352 commits in 30 days — with zero human intervention per task. Here's how it works and what I learned."
tags: ai, automation, github, opensource
canonical_url: https://github.com/kai-linux/agent-os/blob/main/docs/case-study-agent-os.md
---

## The Experiment

What happens when you let AI agents manage an entire software project — not just write code, but triage issues, route tasks, review CI, merge PRs, analyze failures, and file their own fix tickets?

I built [Agent OS](https://github.com/kai-linux/agent-os) to find out. It bootstrapped itself from an empty repo to a fully autonomous software organization in 30 days.

**The results are public, auditable, and reproducible.**

## The Numbers

| Metric | Value |
|---|---|
| Issues closed | 110 of 119 (92% closure rate) |
| PRs merged | 85 of 90 (94% merge rate) |
| Total commits | 352 in 30 days (~12/day) |
| Agent tasks executed | 146+ (91% 14-day success rate) |
| Agents in pool | 4 (Claude, Codex, Gemini, DeepSeek) |
| Infrastructure cost | $5/month VPS |

Every number is verifiable from public GitHub data: [commits](https://github.com/kai-linux/agent-os/commits/main), [closed issues](https://github.com/kai-linux/agent-os/issues?q=is%3Aissue+is%3Aclosed), [merged PRs](https://github.com/kai-linux/agent-os/pulls?q=is%3Apr+is%3Amerged).

## How It Works

Agent OS is a coordination layer. It doesn't just call an LLM — it runs a full software team:

```
GitHub Issue (Backlog)
       │
  Dispatcher (every 60s) ─── LLM formats task, routes by type
       │
  Queue Engine ─── isolated worktree → agent → result → retry/escalate
       │
  PR Monitor (every 5m) ─── CI green → squash merge → close issue
       │
  Log Analyzer + Backlog Groomer ─── files fix tickets back into the backlog
```

The key insight: **that last arrow is the point.** The system files tickets about its own failures. Those tickets enter the backlog. The agents fix them. The fixes get merged. Next week, the system is better.

## The Recursive Loop

This is what separates Agent OS from a task runner:

- **Every Monday** — the log analyzer reads execution metrics, identifies failure patterns, and files fix tickets with evidence
- **Every Saturday** — the backlog groomer scans for stale issues, risk flags, and gaps, then generates improvement tasks
- **Every sprint** — the strategic planner evaluates business metrics and selects the next sprint from the backlog

These generated issues are indistinguishable from human-written ones. They enter the same queue, get dispatched to the same agents, go through the same CI pipeline. The system literally engineers itself.

## An Honest Failure Story

I'm not going to pretend everything worked perfectly. The first-attempt success rate was 60.8%. Here's what a real failure cascade looked like:

**PR #98** had a CI failure. Agent OS dispatched a debug task. The agent fixed the code, but the CI verification gate had a bug — it extracted failed job names from markdown prose in issue bodies. When follow-up tasks reformatted the body, the job names disappeared. The gate thought the fix failed, spawned a new debug task, which failed the same way, spawning another... 8 cascading debug tasks from one bug.

The system eventually fixed this too. The fix: persist `failed_checks` as structured metadata instead of parsing markdown prose. That fix was also shipped by an agent, through the same pipeline.

**The failure was real. The recovery was autonomous. Both are auditable.**

## What the Agents Built

Everything in the repo was implemented by dispatched agents:

- Parallel queue workers with per-repo file locking
- Priority-aware task dispatch with age-based scoring
- CI failure clustering by error signature to deduplicate debug tasks
- Automatic escalation with Telegram decision cards
- Post-merge outcome attribution linking PRs to business metrics
- Evidence-driven strategic planning

Each feature: GitHub Issue → agent dispatch → code → tests → PR → CI → auto-merge → issue closed.

## Key Design Choices

- **GitHub is the entire control plane** — no second system to maintain
- **Markdown files, not message brokers** — you can `ls` the queue
- **Isolated worktrees** — agents never collide
- **One contract, many agents** — `.agent_result.md` is the only interface
- **4-agent pool** with automatic fallback routing when one agent fails

## Try It

```bash
git clone https://github.com/kai-linux/agent-os && cd agent-os
gh auth login
./demo.sh
```

The demo creates a test issue, dispatches it to Claude, and shows the agent writing code. No config editing, no project setup.

**Requirements:** `gh` (authenticated), `python3`, `claude` CLI.

## What I Learned

1. **Coordination is harder than coding.** The agents can write code. The hard part is task state, routing, context preservation, failure recovery, and institutional memory.

2. **Honest metrics build trust.** A 55.7% success rate sounds bad until you see that 92% of issues got closed — retries and fallback routing work.

3. **Self-improvement compounds.** The log analyzer filing its own fix tickets creates a virtuous cycle that keeps getting better without human input.

4. **$5/month is enough.** The entire system runs on a cheap VPS with cron jobs. No Kubernetes, no message queues, no cloud functions.

5. **91% and climbing.** The 14-day rolling success rate improved from 61% to 91% through the self-improvement loop — the system literally engineered its own reliability.

---

[Full case study with architecture details](https://github.com/kai-linux/agent-os/blob/main/docs/case-study-agent-os.md) | [Live reliability dashboard](https://github.com/kai-linux/agent-os/blob/main/docs/reliability/README.md) | [Repository](https://github.com/kai-linux/agent-os)

I'd love to hear what you think — especially if you're building with AI agents or running solo. What would you want to see next?
