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
by technical builders.

## Objective Alignment

The repo objective file (`objectives/<repo>.yaml`) defines what this repo is
measured on. The planner and groomer MUST treat these metrics as primary
drivers, not secondary signals. If the objective includes external adoption
metrics (GitHub stars, user growth, activation rate), sprints must include
work that moves those metrics — not just internal infrastructure.

**Balance rule**: At least 40% of sprint capacity should target the primary
objective metric. A sprint filled entirely with internal plumbing when the
objective says "grow adoption" is a failed sprint.

## Selection Priorities

When choosing backlog items to promote, prefer work that:

1. Directly moves the tracked objective metrics (adoption, stars, activation,
   or whatever the objective file defines).
2. Increases trusted adoption or improves the system's ability to measure it.
3. Unblocks other important work or clears a repeated blocker.
4. Increases agent autonomy, reliability, or recovery capacity.
5. Improves evidence quality for planning: research, analytics, product
   inspection, user signals, or domain evaluation.
6. Improves activation and credibility for real operators: onboarding clarity,
   docs, proof of capability, demos, outcome visibility, or public trust.
7. Improves control-plane quality: backlog quality, planning quality,
   observability, routing, CI, or handoff robustness.
8. Compounds across repos instead of helping only a one-off local task.

## What To Avoid

Avoid promoting issues that:

- are stale, already superseded, or tied to a resolved blocker
- are blocked on external human action or missing credentials
- are vague epics without a clear, single-session success condition
- create churn without improving autonomy, evidence, product direction, or
  trusted adoption
- are entirely internal infrastructure when the objective demands external
  adoption work and the sprint has no adoption-facing tasks
- chase attention without improving activation, operator trust, or
  demonstrable shipped value

## Tie-Breakers

When several backlog items are all reasonable, prefer the issue that most
clearly does one of the following:

- improves trusted adoption or public proof that Agent OS works in practice
- resolves a current blocked dependency
- improves planner/groomer quality
- increases evidence-driven planning capability
- reduces repeated operational failure modes

## Domain Evaluation Rubric

If a repo contains `RUBRIC.md`, the planner and groomer should use its quality
dimensions and skill dimensions as additional evaluation criteria when shaping
work. The rubric defines what "good" looks like for this specific repo's domain
— it is not a generic checklist. Different repos may have entirely different
rubrics (a website repo cares about page speed and accessibility; an automation
harness cares about execution reliability and self-improvement loops).

When a rubric is present:

- Prefer work that closes gaps against rubric quality dimensions
- Use rubric skill dimensions to identify underdeveloped capabilities
- Reference rubric criteria in task success criteria where applicable
- Do not treat the rubric as a mandatory checklist — it informs priority, not
  gates completion

## Blocked Work

Blocked issues should not normally be promoted directly. Prefer the issue that
removes the blocker. If a blocked issue is actually retryable, that should be
made explicit by a follow-up or unblocker task rather than inferred.
