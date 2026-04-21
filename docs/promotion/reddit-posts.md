# Reddit Posts

## r/programming

### Title
Show r/programming: I built an issue-to-PR automation system with public reliability metrics and supervised rollout guidance

### Body

I've been building [Agent OS](https://github.com/kai-linux/agent-os) — a coordination layer that lets AI agents manage large parts of a software project. Not just write code, but triage issues, route tasks, run CI, merge PRs, analyze failures, and file their own fix tickets.

**Historical results from 30 days managing its own repo:**

| Metric | Value |
|---|---|
| Issues closed | 110 of 119 (92%) |
| PRs merged | 85 of 90 (94%) |
| Commits | 352 (~12/day) |
| 14-day success rate | 91% (up from 61%) |
| Infrastructure | $5/month VPS |

The interesting part is the recursive self-improvement loop: the log analyzer reads execution metrics weekly and files fix tickets. Those tickets enter the same backlog, get dispatched to agents, go through CI, and get merged.

**I'm honest about failures.** PR-98 had a CI verification bug that spawned 8 cascading debug tasks before the system fixed its own verification logic — that story is documented in the [case study](https://github.com/kai-linux/agent-os/blob/main/docs/case-study-agent-os.md).

**Key design choices:**
- GitHub Issues is the entire control plane — no second system
- Markdown files instead of message brokers — you can `ls` the queue
- 4 agents (Claude, Codex, Gemini, DeepSeek) with automatic fallback routing
- `.agent_result.md` is the only interface contract

Everything is auditable from public GitHub data. The README, CI pipeline, and backlog groomer were all written by agents.

**Current framing:** new external repos should start in `dispatcher_only` mode, with 5 to 10 bounded issues and manual PR review before turning on the full loop.

**Try it:** `git clone https://github.com/kai-linux/agent-os && ./demo.sh`

[Full case study](https://github.com/kai-linux/agent-os/blob/main/docs/case-study-agent-os.md) | [Live reliability dashboard](https://github.com/kai-linux/agent-os/blob/main/docs/reliability/README.md)

Would love feedback — especially on failure recovery patterns and multi-agent routing.

---

## r/SideProject

### Title
Agent OS: My side project that manages its own development — and now recommends supervised rollout for external repos

### Body

I built [Agent OS](https://github.com/kai-linux/agent-os) as a side project to answer a question: can AI agents run an entire software project, not just write code?

After 30 days of autonomous operation managing its own repo:
- **110 issues closed** (92% closure rate)
- **85 PRs merged** (94% merge rate)
- **352 commits** (~12/day)
- Runs on a **$5/month VPS** with cron jobs

**How it works:** GitHub Issues are the backlog. A dispatcher triages and routes tasks to one of 4 AI agents (Claude, Codex, Gemini, DeepSeek). Each agent works in an isolated worktree, writes code, runs tests, opens a PR. A PR monitor checks CI and auto-merges on green. A log analyzer files fix tickets about failures weekly. Those tickets enter the same pipeline. The system improves itself.

**The honest version:** the self-managed repo case study is strong, but a fresh external repo should still start in `dispatcher_only` mode and expand only after a supervised pilot. The [case study](https://github.com/kai-linux/agent-os/blob/main/docs/case-study-agent-os.md) documents both the wins and the failure cascades.

**What's next:** Evidence-driven sprint planning, closed-loop optimization, and managing external repos.

Everything is open source and auditable: [github.com/kai-linux/agent-os](https://github.com/kai-linux/agent-os)

Would love to hear from other solo builders — what would you want an autonomous dev system to handle first?

---

## r/LocalLLaMA

### Title
Agent OS: Multi-agent orchestration system with public metrics, fallback routing, and supervised rollout guidance

### Body

I've been running [Agent OS](https://github.com/kai-linux/agent-os), a multi-agent orchestration layer that routes tasks to whichever LLM agent is best suited. It manages its own development autonomously.

**Multi-agent routing results after 30 days:**
- 4-agent pool: Claude (94.9%), DeepSeek (100% on small sample), Codex (75%)
- Automatic fallback routing when one agent fails
- Task-type-aware scoring — agents get routed to task types they succeed at
- Health gate auto-benches degraded agents

The system dispatches GitHub issues as tasks, each agent works in an isolated git worktree, and the PR monitor auto-merges on green CI. A log analyzer files fix tickets about failures weekly — those tickets enter the same backlog and get fixed by agents.

**Key learning on multi-agent routing:** Blended success rates across task types are misleading. Codex scored 56% when measured across all tasks but 100% on its target task type. We added task-type-scoped scoring and the routing quality improved immediately.

Current live metrics are in the repo's reliability dashboard. The 30-day numbers are retained as a historical case study, not as a promise that every new repo will perform the same on day one.

[Case study](https://github.com/kai-linux/agent-os/blob/main/docs/case-study-agent-os.md) | [Repo](https://github.com/kai-linux/agent-os) | [Reliability dashboard](https://github.com/kai-linux/agent-os/blob/main/docs/reliability/README.md)

---

## r/AutomateYourself / r/selfhosted

### Title
Self-hosted issue-to-PR automation on a $5/month VPS with public metrics and bounded escalation

### Body

Built [Agent OS](https://github.com/kai-linux/agent-os) to automate my entire dev workflow. It runs on a $5/month VPS with cron jobs — no Kubernetes, no cloud functions, no message queues.

**What it automates:**
- Issue triage and task dispatch (every 60s)
- Agent execution in isolated worktrees
- PR creation, CI monitoring, and auto-merge (every 5m)
- Failure analysis and fix ticket generation (weekly)
- Sprint planning and backlog grooming (weekly)

**30-day results managing its own repo:**
- 110 issues closed, 85 PRs merged, 352 commits
- 91% 14-day success rate
- All via cron: no daemon, no container orchestration

Setup: `git clone https://github.com/kai-linux/agent-os && ./demo.sh`

Requires: GitHub CLI (authenticated), Python 3, Claude CLI. [Deployment guide](https://github.com/kai-linux/agent-os/blob/main/docs/deployment-guide.md).
