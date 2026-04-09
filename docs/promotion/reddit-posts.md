# Reddit Posts

## r/programming

### Title
Show r/programming: I built an autonomous software org that shipped 59 PRs and 275 commits in 23 days with zero human intervention per task

### Body

I've been building [Agent OS](https://github.com/kai-linux/agent-os) — a coordination layer that lets AI agents manage an entire software project. Not just write code, but triage issues, route tasks, run CI, merge PRs, analyze failures, and file their own fix tickets.

**Results from 23 days managing its own repo:**

| Metric | Value |
|---|---|
| Issues closed | 79 of 86 (92%) |
| PRs merged | 59 of 65 (91%) |
| Commits | 275 (~12/day) |
| First-attempt success | 55.7% |
| Infrastructure | $5/month VPS |

The interesting part is the recursive self-improvement loop: the log analyzer reads execution metrics weekly and files fix tickets. Those tickets enter the same backlog, get dispatched to agents, go through CI, and get merged. The system literally fixes its own bugs.

**I'm honest about failures.** PR-98 had a CI verification bug that spawned 8 cascading debug tasks before the system fixed its own verification logic — that story is documented in the [case study](https://github.com/kai-linux/agent-os/blob/main/docs/case-study-agent-os.md).

**Key design choices:**
- GitHub Issues is the entire control plane — no second system
- Markdown files instead of message brokers — you can `ls` the queue
- 4 agents (Claude, Codex, Gemini, DeepSeek) with automatic fallback routing
- `.agent_result.md` is the only interface contract

Everything is auditable from public GitHub data. The README, CI pipeline, and backlog groomer were all written by agents.

**Try it:** `git clone https://github.com/kai-linux/agent-os && ./demo.sh`

[Full case study](https://github.com/kai-linux/agent-os/blob/main/docs/case-study-agent-os.md) | [GitHub Discussion](https://github.com/kai-linux/agent-os/discussions/167)

Would love feedback — especially on failure recovery patterns and multi-agent routing.

---

## r/SideProject

### Title
Agent OS: My side project that manages its own development — 59 PRs merged, 79 issues closed, zero human intervention

### Body

I built [Agent OS](https://github.com/kai-linux/agent-os) as a side project to answer a question: can AI agents run an entire software project, not just write code?

After 23 days of autonomous operation managing its own repo:
- **79 issues closed** (92% closure rate)
- **59 PRs merged** (91% merge rate)  
- **275 commits** (~12/day)
- Runs on a **$5/month VPS** with cron jobs

**How it works:** GitHub Issues are the backlog. A dispatcher triages and routes tasks to one of 4 AI agents (Claude, Codex, Gemini, DeepSeek). Each agent works in an isolated worktree, writes code, runs tests, opens a PR. A PR monitor checks CI and auto-merges on green. A log analyzer files fix tickets about failures weekly. Those tickets enter the same pipeline. The system improves itself.

**The honest version:** First-attempt success rate is 55.7%. But with retries and fallback routing, 92% of issues eventually get closed. The [case study](https://github.com/kai-linux/agent-os/blob/main/docs/case-study-agent-os.md) documents both the wins and the failure cascades.

**What's next:** Evidence-driven sprint planning, closed-loop optimization, and managing external repos.

Everything is open source and auditable: [github.com/kai-linux/agent-os](https://github.com/kai-linux/agent-os)

Would love to hear from other solo builders — what would you want an autonomous dev system to handle first?
