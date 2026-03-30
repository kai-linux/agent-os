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
stronger evidence, tighter closed-loop improvement, and more credible adoption
by technical builders. Public attention is useful only insofar as it reflects
real product trust and usefulness.

## Selection Priorities

When choosing backlog items to promote, prefer work that:

1. Increases trusted adoption or improves the system's ability to measure it.
2. Unblocks other important work or clears a repeated blocker.
3. Increases agent autonomy, reliability, or recovery capacity.
4. Improves evidence quality for planning: research, analytics, product
   inspection, user signals, or domain evaluation.
5. Improves activation and credibility for real operators: onboarding clarity,
   docs, proof of capability, demos, outcome visibility, or public trust.
6. Improves control-plane quality: backlog quality, planning quality,
   observability, routing, CI, or handoff robustness.
7. Compounds across repos instead of helping only a one-off local task.

## What To Avoid

Avoid promoting issues that:

- are stale, already superseded, or tied to a resolved blocker
- are blocked on external human action or missing credentials
- are vague epics without a clear, single-session success condition
- create churn without improving autonomy, evidence, product direction, or
  trusted adoption
- chase stars or attention without improving activation, operator trust, or
  demonstrable shipped value

## Tie-Breakers

When several backlog items are all reasonable, prefer the issue that most
clearly does one of the following:

- improves trusted adoption or public proof that Agent OS works in practice
- resolves a current blocked dependency
- improves planner/groomer quality
- increases evidence-driven planning capability
- reduces repeated operational failure modes

## Blocked Work

Blocked issues should not normally be promoted directly. Prefer the issue that
removes the blocker. If a blocked issue is actually retryable, that should be
made explicit by a follow-up or unblocker task rather than inferred.
