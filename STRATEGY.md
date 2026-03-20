# Strategy — kai-linux/agent-os

> Auto-maintained by agent-os strategic planner. Updated each sprint cycle.

## Product Vision

# Agent OS

> What if you could hire an entire engineering team that works 24/7, never calls in sick, debugs its own failures, writes its own improvement tickets, and gets better every week — without you ever opening a laptop?

Agent OS is that team.

It's not a copilot. It's not a chatbot. It's a **fully autonomous software organization** — staffed by AI agents, managed by cron, coordinated through GitHub, and designed to run indefinitely without human input.

You give it a backlog. It ships pr

## Current Focus Areas


<!-- auto-focus-areas -->
- Strengthen the execution control plane with better failure gating, blocker handling, and recovery follow-through
- Improve observability and auditability of autonomous work through prompt snapshots, outcome attribution, and richer retrospectives
- Advance evidence-driven strategic planning with research, analytics, feedback inputs, and auto-derived planning signals
- Expand orchestration capabilities for autonomous task execution, including decomposition, dependency management, and bounded follow-up creation
- Improve cross-repo and delivery workflow reliability through dependency-aware planning, CI fixes, and safer git/publish operations

## Sprint History

### Sprint 2026-03-20

**Retrospective:**
Issues completed:
- #58: Gate git/publish tasks on writable remote capabilities [bug, prio:high, done] — COMPLETED

PRs merged:
- PR #61: Agent: task-20260320-081208-gate-git-publish-tasks-on-writable-remote-capabili (branch: agent/task-20260320-081208-gate-git-publish-tasks-on-writable-remote-capabili)

**Plan:**
- [prio:high] Require structured blocker codes on blocked task outcomes: This is the strongest next control-plane improvement because the strategy prioritizes turning failures into actionable loops, and structured blocker data is the prerequisite for reliable recovery automation.
- [prio:normal] Auto-file bounded follow-ups for partial debug outcomes: Last sprint exposed partial debug handoff gaps, and this promotion directly advances the North Star by converting incomplete recovery work into durable autonomous execution instead of operator-dependent cleanup.
- [prio:high] Persist worker prompt snapshots for each dispatched task: This matters this week because the strategy favors auditability and self-healing, and prompt snapshots remove a repeated debugging blind spot in the execution control plane.
- [prio:high] Add post-merge outcome attribution for issue, PR, and task IDs: This is a direct move from Level 3 planning toward Level 4 closed-loop optimization because the strategy explicitly favors measurable outcome loops over prompt-only reasoning.
- [prio:high] Build a normalized production feedback substrate for repos: This promotion compounds the recent planning-signals work and best matches the strategy's push toward evidence-driven planning with auditable, reusable product inputs.


### Sprint 2026-03-20

**Retrospective:**
(no activity in the last 0.1 days)

**Plan:**
- [prio:high] Gate git/publish tasks on writable remote capabilities: This is the highest-leverage task this week because it removes a repeated execution failure mode, improves control-plane reliability, and strengthens the North Star path from reliable execution toward closed-loop autonomy.


### Sprint 2026-03-20

**Plan:**



### Sprint 2026-03-19

**Retrospective:**
Issues completed:
- #39: Add pre-planning research inputs to strategic planning [enhancement, prio:normal, done] — COMPLETED
- #37: Diagnose and resolve CI failure blocking PR #34 merge [bug, prio:high] — COMPLETED
- #35: Fix CI failure on PR #34 [bug, prio:high, done, blocked] — COMPLETED
- #25: Strategic planner: configurable plan size and sprint cadence [enhancement, in-progress, agent-dispatched, done] — COMPLETED
- #24: Multi-repo strategic planning: cross-repo dependency awareness [enhancement, task:architecture, agent-dispatched, done, blocked, deepseek] — COMPLETED
- #23: STRATEGY.md: auto-update focus areas from sprint patterns [enhancement, in-progress, agent-dispatched, done, deepseek] — COMPLETED
- #22: Sprint retrospective quality: LLM-generated analysis [enhancement, in-progress, agent-dispatched, done, gemini] — COMPLETED
- #8: [task] Daily digest to Telegram [task:implementation, prio:normal, in-progress, agent-dispatched, done] — COMPLETED
- #7: [task] Structured Telegram escalation with reply buttons [task:implementation, prio:normal, in-progress, agent-dispatched, done] — COMPLETED
- #6: [task] Task dependency resolver [task:implementation, prio:normal, in-progress, agent-dispatched, done] — COMPLETED
- #2: [task] Task decomposer agent [task:implementation, prio:high, in-progress, agent-dispatched, done] — COMPLETED

PRs merged:
- PR #44: Agent: task-20260319-205609-add-pre-planning-research-inputs-to-strategic-plan (branch: agent/task-20260319-205609-add-pre-planning-research-inputs-to-strategic-plan)
- PR #36: Agent: task-20260319-155323-multi-repo-strategic-planning-cross-repo-dependenc (branch: agent/task-20260319-155323-multi-repo-strategic-planning-cross-repo-dependenc)
- PR #34: Agent: task-20260319-155323-multi-repo-strategic-planning-cross-repo-dependenc (branch: agent/task-20260319-155323-multi-repo-strategic-planning-cross-repo-dependenc)
- PR #33: Add configurable plan size and sprint cadence (#25) (branch: agent/task-20260319-133023-strategic-planner-configurable-plan-size-and-sprin)
- PR #32: Agent: task-20260319-103913-strategy-md-auto-update-focus-areas-from-sprint-pa (branch: agent/task-20260319-103913-strategy-md-auto-update-focus-areas-from-sprint-pa)
- PR #31: Agent: task-20260319-101806-task-task-decomposer-agent (branch: agent/task-20260319-101806-task-task-decomposer-agent)
- PR #30: Agent: task-20260319-073306-task-daily-digest-to-telegram (branch: agent/task-20260319-073306-task-daily-digest-to-telegram)
- PR #29: Agent: task-20260319-073206-task-structured-telegram-escalation-with-reply-but (branch: agent/task-20260319-073206-task-structured-telegram-escalation-with-reply-but)
- PR #28: Agent: task-20260319-073106-task-task-dependency-resolver (branch: agent/task-20260319-073106-task-task-dependency-resolver)

**Plan:**
- [prio:normal] Integrate analytics and user-signal inputs into planning: This is the strongest next step this week because it compounds directly on the new research-input foundation and moves Agent OS toward the planning rubric’s Level 3 evidence-driven planning goal.


### Sprint 2026-03-19

**Retrospective:**
Issues completed:
- #35: Fix CI failure on PR #34 [bug, prio:high, done, blocked] — COMPLETED
- #25: Strategic planner: configurable plan size and sprint cadence [enhancement, in-progress, agent-dispatched, done] — COMPLETED
- #24: Multi-repo strategic planning: cross-repo dependency awareness [enhancement, task:architecture, agent-dispatched, done, blocked, deepseek] — COMPLETED
- #23: STRATEGY.md: auto-update focus areas from sprint patterns [enhancement, in-progress, agent-dispatched, done, deepseek] — COMPLETED
- #22: Sprint retrospective quality: LLM-generated analysis [enhancement, in-progress, agent-dispatched, done, gemini] — COMPLETED
- #8: [task] Daily digest to Telegram [task:implementation, prio:normal, in-progress, agent-dispatched, done] — COMPLETED
- #7: [task] Structured Telegram escalation with reply buttons [task:implementation, prio:normal, in-progress, agent-dispatched, done] — COMPLETED
- #6: [task] Task dependency resolver [task:implementation, prio:normal, in-progress, agent-dispatched, done] — COMPLETED
- #2: [task] Task decomposer agent [task:implementation, prio:high, in-progress, agent-dispatched, done] — COMPLETED

PRs merged:
- PR #36: Agent: task-20260319-155323-multi-repo-strategic-planning-cross-repo-dependenc (branch: agent/task-20260319-155323-multi-repo-strategic-planning-cross-repo-dependenc)
- PR #34: Agent: task-20260319-155323-multi-repo-strategic-planning-cross-repo-dependenc (branch: agent/task-20260319-155323-multi-repo-strategic-planning-cross-repo-dependenc)
- PR #33: Add configurable plan size and sprint cadence (#25) (branch: agent/task-20260319-133023-strategic-planner-configurable-plan-size-and-sprin)
- PR #32: Agent: task-20260319-103913-strategy-md-auto-update-focus-areas-from-sprint-pa (branch: agent/task-20260319-103913-strategy-md-auto-update-focus-areas-from-sprint-pa)
- PR #31: Agent: task-20260319-101806-task-task-decomposer-agent (branch: agent/task-20260319-101806-task-task-decomposer-agent)
- PR #30: Agent: task-20260319-073306-task-daily-digest-to-telegram (branch: agent/task-20260319-073306-task-daily-digest-to-telegram)
- PR #29: Agent: task-20260319-073206-task-structured-telegram-escalation-with-reply-but (branch: agent/task-20260319-073206-task-structured-telegram-escalation-with-reply-but)
- PR #28: Agent: task-20260319-073106-task-task-dependency-resolver (branch: agent/task-20260319-073106-task-task-dependency-resolver)

**Plan:**
- [prio:normal] Add pre-planning research inputs to strategic planning: This is the best next step for the strategy this week because it extends the newly improved planner with outside evidence, moving Agent OS closer to autonomous, higher-quality prioritization instead of planning only from repo-internal context.


### Sprint 2026-03-19

**Retrospective:**
Issues completed:
- #35: Fix CI failure on PR #34 [bug, prio:high, done, blocked] — COMPLETED
- #25: Strategic planner: configurable plan size and sprint cadence [enhancement, in-progress, agent-dispatched, done] — COMPLETED
- #24: Multi-repo strategic planning: cross-repo dependency awareness [enhancement, task:architecture, agent-dispatched, done, blocked, deepseek] — COMPLETED
- #23: STRATEGY.md: auto-update focus areas from sprint patterns [enhancement, in-progress, agent-dispatched, done, deepseek] — COMPLETED
- #22: Sprint retrospective quality: LLM-generated analysis [enhancement, in-progress, agent-dispatched, done, gemini] — COMPLETED
- #8: [task] Daily digest to Telegram [task:implementation, prio:normal, in-progress, agent-dispatched, done] — COMPLETED
- #7: [task] Structured Telegram escalation with reply buttons [task:implementation, prio:normal, in-progress, agent-dispatched, done] — COMPLETED
- #6: [task] Task dependency resolver [task:implementation, prio:normal, in-progress, agent-dispatched, done] — COMPLETED
- #2: [task] Task decomposer agent [task:implementation, prio:high, in-progress, agent-dispatched, done] — COMPLETED

PRs merged:
- PR #36: Agent: task-20260319-155323-multi-repo-strategic-planning-cross-repo-dependenc (branch: agent/task-20260319-155323-multi-repo-strategic-planning-cross-repo-dependenc)
- PR #34: Agent: task-20260319-155323-multi-repo-strategic-planning-cross-repo-dependenc (branch: agent/task-20260319-155323-multi-repo-strategic-planning-cross-repo-dependenc)
- PR #33: Add configurable plan size and sprint cadence (#25) (branch: agent/task-20260319-133023-strategic-planner-configurable-plan-size-and-sprin)
- PR #32: Agent: task-20260319-103913-strategy-md-auto-update-focus-areas-from-sprint-pa (branch: agent/task-20260319-103913-strategy-md-auto-update-focus-areas-from-sprint-pa)
- PR #31: Agent: task-20260319-101806-task-task-decomposer-agent (branch: agent/task-20260319-101806-task-task-decomposer-agent)
- PR #30: Agent: task-20260319-073306-task-daily-digest-to-telegram (branch: agent/task-20260319-073306-task-daily-digest-to-telegram)
- PR #29: Agent: task-20260319-073206-task-structured-telegram-escalation-with-reply-but (branch: agent/task-20260319-073206-task-structured-telegram-escalation-with-reply-but)
- PR #28: Agent: task-20260319-073106-task-task-dependency-resolver (branch: agent/task-20260319-073106-task-task-dependency-resolver)

**Plan:**
- [prio:high] Bootstrap STRATEGY.md from repo state: This week should establish product foundations, and an auto-generated initial strategy closes the biggest planning gap by giving the strategic planner a durable source of direction instead of operating without a strategy document.


