# Planning Principles — agent-os

This document is a stable planner rubric for agent-os. Unlike `STRATEGY.md`,
which changes every sprint, these principles define what the strategic planner
should optimize for over time.

## North Star

Bootstrap agent-os toward Level 4 and beyond:

- Level 1: Reliable execution engine
- Level 2: Strategic planning
- Level 3: Evidence-driven planning
- Level 4: Closed-loop optimization

The planner should prefer work that compounds toward greater autonomy,
stronger evidence, and tighter closed-loop improvement.
The planner should optimize for better outcomes across managed repos, not for
vanity metrics on the agent-os repo itself.

## Selection Priorities

When choosing backlog items to promote, prefer work that:

1. Increases measured positive outcomes across managed repos or improves the system's ability to measure them.
2. Unblocks other important work or clears a repeated blocker.
3. Increases agent autonomy, reliability, or recovery capacity.
4. Improves evidence quality for planning: research, analytics, product
   inspection, user signals, or domain evaluation.
5. Improves control-plane quality: backlog quality, planning quality,
   observability, routing, CI, or handoff robustness.
6. Compounds across repos instead of helping only a one-off local task.

## What To Avoid

Avoid promoting issues that:

- are stale, already superseded, or tied to a resolved blocker
- are blocked on external human action or missing credentials
- are vague epics without a clear, single-session success condition
- create churn without improving autonomy, evidence, or product direction
- primarily chase stars, forks, docs traffic, or social attention without a
  plausible path to better managed-repo outcomes

## Tie-Breakers

When several backlog items are all reasonable, prefer the issue that most
clearly does one of the following:

- improves managed-repo business impact or outcome measurement quality
- resolves a current blocked dependency
- improves planner/groomer quality
- increases evidence-driven planning capability
- reduces repeated operational failure modes

## Blocked Work

Blocked issues should not normally be promoted directly. Prefer the issue that
removes the blocker. If a blocked issue is actually retryable, that should be
made explicit by a follow-up or unblocker task rather than inferred.
