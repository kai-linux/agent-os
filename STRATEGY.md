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
- Harden execution reliability by reducing CI, publish, and control-plane failure modes before tasks run or close
- Close the loop on blocked and partial work with structured recovery, reruns, quarantines, and bounded follow-ups
- Strengthen evidence-driven planning through automated research, production-feedback, and sprint-derived strategy artifacts
- Improve observability and auditability with prompt snapshots, outcome attribution, blocker codes, and failure artifact capture
- Advance agent autonomy through better task decomposition, dependency handling, adaptive routing, and multi-repo planning

## Sprint History

### Sprint 2026-03-20

**Retrospective:**
Issues completed:
- #78: Auto-rerun blocked tasks after prompt inspection completes [bug, enhancement, prio:high, done] — COMPLETED
- #76: Auto-generate PRODUCTION_FEEDBACK.md each sprint cycle [enhancement, prio:high, done] — COMPLETED
- #73: Fix CI failure on PR #71 [bug, prio:high, done] — COMPLETED
- #69: Reproduce and fix the CI failure behind PR #34 [bug, prio:high] — COMPLETED
- #65: Replace static fallback chains with adaptive agent routing [enhancement, prio:high, done] — COMPLETED
- #64: Add post-merge outcome attribution for issue, PR, and task IDs [enhancement, prio:high, done] — COMPLETED
- #63: Collapse self-improvement generators behind one evidence synthesizer [enhancement, prio:high, done] — COMPLETED
- #62: Build a normalized production feedback substrate for repos [enhancement, prio:high, done] — COMPLETED
- #60: Auto-file bounded follow-ups for partial debug outcomes [enhancement, prio:normal, done] — COMPLETED
- #59: Quarantine tasks that block repeatedly within one day [enhancement, prio:high, done] — COMPLETED
- #58: Gate git/publish tasks on writable remote capabilities [bug, prio:high, done] — COMPLETED
- #56: Auto-generate PLANNING_RESEARCH.md each sprint cycle [enhancement, prio:normal, done] — COMPLETED
- #55: Add regression test for PR CI failure recovery flow [bug, prio:high, done, tech-debt] — COMPLETED
- #54: Require structured blocker codes on blocked task outcomes [bug, prio:high, done] — COMPLETED
- #53: Consume escalation-note retry decisions in task dispatch [enhancement, prio:high, done] — COMPLETED
- #51: Persist worker prompt snapshots for each dispatched task [enhancement, prio:high, done] — COMPLETED
- #50: Preflight git push readiness before dispatching publish tasks [bug, prio:high, done] — COMPLETED
- #45: Add CI artifact capture for failing PR jobs [bug, enhancement, prio:high, done] — COMPLETED

PRs merged:
- PR #84: Agent: task-20260320-134213-auto-rerun-blocked-tasks-after-prompt-inspection-c (branch: agent/task-20260320-134213-auto-rerun-blocked-tasks-after-prompt-inspection-c)
- PR #83: Agent: task-20260320-134110-auto-generate-production-feedback-md-each-sprint-c (branch: agent/task-20260320-134110-auto-generate-production-feedback-md-each-sprint-c)
- PR #82: Agent: task-20260320-134012-collapse-self-improvement-generators-behind-one-ev (branch: agent/task-20260320-134012-collapse-self-improvement-generators-behind-one-ev)
- PR #81: Agent: task-20260320-133911-preflight-git-push-readiness-before-dispatching-pu (branch: agent/task-20260320-133911-preflight-git-push-readiness-before-dispatching-pu)
- PR #80: Agent: task-20260320-133809-add-ci-artifact-capture-for-failing-pr-jobs (branch: agent/task-20260320-133809-add-ci-artifact-capture-for-failing-pr-jobs)
- PR #75: Agent: task-20260320-120911-add-regression-test-for-pr-ci-failure-recovery-flo (branch: agent/task-20260320-120911-add-regression-test-for-pr-ci-failure-recovery-flo)
- PR #74: Agent: task-20260320-120812-consume-escalation-note-retry-decisions-in-task-di (branch: agent/task-20260320-120812-consume-escalation-note-retry-decisions-in-task-di)
- PR #72: Agent: task-20260320-101212-auto-file-bounded-follow-ups-for-partial-debug-out (branch: agent/task-20260320-101212-auto-file-bounded-follow-ups-for-partial-debug-out)
- PR #71: Agent: task-20260320-101116-add-post-merge-outcome-attribution-for-issue-pr-an (branch: agent/task-20260320-101116-add-post-merge-outcome-attribution-for-issue-pr-an)
- PR #68: Agent: task-20260320-101013-build-a-normalized-production-feedback-substrate-f (branch: agent/task-20260320-101013-build-a-normalized-production-feedback-substrate-f)
- PR #67: Agent: task-20260320-100911-require-structured-blocker-codes-on-blocked-task-o (branch: agent/task-20260320-100911-require-structured-blocker-codes-on-blocked-task-o)
- PR #66: Agent: task-20260320-100812-persist-worker-prompt-snapshots-for-each-dispatche (branch: agent/task-20260320-100812-persist-worker-prompt-snapshots-for-each-dispatche)
- PR #61: Agent: task-20260320-081208-gate-git-publish-tasks-on-writable-remote-capabili (branch: agent/task-20260320-081208-gate-git-publish-tasks-on-writable-remote-capabili)

**Plan:**
- [prio:high] Gate CI debug task closure on a verified green rerun: This is the strongest immediate reliability promotion because the strategy prioritizes removing repeated operational failure modes and tightening closed-loop recovery with verified outcomes.
- [prio:normal] Record explicit unblock decision for blocked escalation reviews: This directly extends the new blocked-task recovery path and matches the strategy’s emphasis on turning failures and escalations into actionable, auditable loops.
- [prio:normal] Backfill the current sprint production feedback artifact: This best compounds the recently completed production-feedback infrastructure and aligns with the strategy’s push toward evidence-driven planning over narrative-only inputs.
- [prio:normal] Backfill first planning research artifact for current sprint: This repairs a planning-evidence gap immediately after research automation landed, which fits the strategy’s priority on improving evidence quality for planning decisions.
- [prio:normal] Make agent_scorer drive closed-loop remediation: This is the best long-range promotion in the current backlog because it advances the North Star from observation toward self-directed closed-loop optimization that can compound across repos.


### Sprint 2026-03-20

**Retrospective:**
Issues completed:
- #73: Fix CI failure on PR #71 [bug, prio:high, done] — COMPLETED
- #69: Reproduce and fix the CI failure behind PR #34 [bug, prio:high] — COMPLETED
- #65: Replace static fallback chains with adaptive agent routing [enhancement, prio:high, done] — COMPLETED
- #64: Add post-merge outcome attribution for issue, PR, and task IDs [enhancement, prio:high, done] — COMPLETED
- #62: Build a normalized production feedback substrate for repos [enhancement, prio:high, done] — COMPLETED
- #60: Auto-file bounded follow-ups for partial debug outcomes [enhancement, prio:normal, done] — COMPLETED
- #59: Quarantine tasks that block repeatedly within one day [enhancement, prio:high, done] — COMPLETED
- #58: Gate git/publish tasks on writable remote capabilities [bug, prio:high, done] — COMPLETED
- #56: Auto-generate PLANNING_RESEARCH.md each sprint cycle [enhancement, prio:normal, done] — COMPLETED
- #55: Add regression test for PR CI failure recovery flow [bug, prio:high, done, tech-debt] — COMPLETED
- #54: Require structured blocker codes on blocked task outcomes [bug, prio:high, done] — COMPLETED
- #53: Consume escalation-note retry decisions in task dispatch [enhancement, prio:high, done] — COMPLETED
- #51: Persist worker prompt snapshots for each dispatched task [enhancement, prio:high, done] — COMPLETED

PRs merged:
- PR #75: Agent: task-20260320-120911-add-regression-test-for-pr-ci-failure-recovery-flo (branch: agent/task-20260320-120911-add-regression-test-for-pr-ci-failure-recovery-flo)
- PR #74: Agent: task-20260320-120812-consume-escalation-note-retry-decisions-in-task-di (branch: agent/task-20260320-120812-consume-escalation-note-retry-decisions-in-task-di)
- PR #72: Agent: task-20260320-101212-auto-file-bounded-follow-ups-for-partial-debug-out (branch: agent/task-20260320-101212-auto-file-bounded-follow-ups-for-partial-debug-out)
- PR #71: Agent: task-20260320-101116-add-post-merge-outcome-attribution-for-issue-pr-an (branch: agent/task-20260320-101116-add-post-merge-outcome-attribution-for-issue-pr-an)
- PR #68: Agent: task-20260320-101013-build-a-normalized-production-feedback-substrate-f (branch: agent/task-20260320-101013-build-a-normalized-production-feedback-substrate-f)
- PR #67: Agent: task-20260320-100911-require-structured-blocker-codes-on-blocked-task-o (branch: agent/task-20260320-100911-require-structured-blocker-codes-on-blocked-task-o)
- PR #66: Agent: task-20260320-100812-persist-worker-prompt-snapshots-for-each-dispatche (branch: agent/task-20260320-100812-persist-worker-prompt-snapshots-for-each-dispatche)
- PR #61: Agent: task-20260320-081208-gate-git-publish-tasks-on-writable-remote-capabili (branch: agent/task-20260320-081208-gate-git-publish-tasks-on-writable-remote-capabili)

**Plan:**
- [prio:high] Auto-rerun blocked tasks after prompt inspection completes: This is the strongest immediate follow-through on the new blocker-code, prompt-snapshot, and bounded-follow-up work because the strategy prioritizes turning blocked work into reliable closed-loop recovery.
- [prio:high] Auto-generate PRODUCTION_FEEDBACK.md each sprint cycle: This best compounds the recently completed feedback and attribution foundation by advancing the strategy’s evidence-driven planning track with a durable planning input.
- [prio:high] Preflight git push readiness before dispatching publish tasks: This directly removes a repeated control-plane failure mode and matches the planning principles’ preference for unblockers and reliability improvements over local churn.
- [prio:high] Add CI artifact capture for failing PR jobs: Recent CI recovery work makes this the right hardening task now because the strategy favors preventing repeated execution failures and improving observability for faster recovery loops.
- [prio:high] Collapse self-improvement generators behind one evidence synthesizer: This pushes beyond maintenance toward the long-term vision by tightening closed-loop self-improvement and reducing control-plane noise in a way that can compound across repos.


### Sprint 2026-03-20

**Retrospective:**
Issues completed:
- #73: Fix CI failure on PR #71 [bug, prio:high, done] — COMPLETED
- #64: Add post-merge outcome attribution for issue, PR, and task IDs [enhancement, prio:high, done] — COMPLETED
- #62: Build a normalized production feedback substrate for repos [enhancement, prio:high, done] — COMPLETED
- #60: Auto-file bounded follow-ups for partial debug outcomes [enhancement, prio:normal, done] — COMPLETED
- #58: Gate git/publish tasks on writable remote capabilities [bug, prio:high, done] — COMPLETED
- #54: Require structured blocker codes on blocked task outcomes [bug, prio:high, done] — COMPLETED
- #51: Persist worker prompt snapshots for each dispatched task [enhancement, prio:high, done] — COMPLETED

PRs merged:
- PR #72: Agent: task-20260320-101212-auto-file-bounded-follow-ups-for-partial-debug-out (branch: agent/task-20260320-101212-auto-file-bounded-follow-ups-for-partial-debug-out)
- PR #71: Agent: task-20260320-101116-add-post-merge-outcome-attribution-for-issue-pr-an (branch: agent/task-20260320-101116-add-post-merge-outcome-attribution-for-issue-pr-an)
- PR #68: Agent: task-20260320-101013-build-a-normalized-production-feedback-substrate-f (branch: agent/task-20260320-101013-build-a-normalized-production-feedback-substrate-f)
- PR #67: Agent: task-20260320-100911-require-structured-blocker-codes-on-blocked-task-o (branch: agent/task-20260320-100911-require-structured-blocker-codes-on-blocked-task-o)
- PR #66: Agent: task-20260320-100812-persist-worker-prompt-snapshots-for-each-dispatche (branch: agent/task-20260320-100812-persist-worker-prompt-snapshots-for-each-dispatche)
- PR #61: Agent: task-20260320-081208-gate-git-publish-tasks-on-writable-remote-capabili (branch: agent/task-20260320-081208-gate-git-publish-tasks-on-writable-remote-capabili)

**Plan:**
- [prio:high] Consume escalation-note retry decisions in task dispatch: This is the strongest immediate follow-through on the new blocker-code and bounded-follow-up work because the strategy prioritizes turning failures into actionable, closed-loop recovery.
- [prio:high] Quarantine tasks that block repeatedly within one day: This directly removes a repeated operational failure mode, which the planning principles rank above local feature churn and aligns with the current focus on stronger execution control and blocker handling.
- [prio:high] Replace static fallback chains with adaptive agent routing: This pushes beyond maintenance toward the long-term North Star by compounding execution data into better autonomous routing and recovery decisions.
- [prio:normal] Auto-generate PLANNING_RESEARCH.md each sprint cycle: This best advances the strategy’s evidence-driven planning track by converting a currently missing input into a repeatable planning artifact.
- [prio:high] Add regression test for PR CI failure recovery flow: Recent CI recovery incidents make this the right reliability hardening task this week because the strategy favors preventing repeated control-plane regressions after a fix lands.


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


