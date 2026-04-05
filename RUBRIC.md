# Domain Evaluation Rubric — agent-os

> Repo-specific quality criteria for planning and grooming. Planners and
> groomers use these dimensions to evaluate work beyond generic context.
> Each repo declares its own rubric; not every repo shares the same criteria.

## Domain: Autonomous Software Organization

This repo is an automation harness that dispatches, executes, reviews, and
self-improves AI agent work across managed repositories. "Good" means the
system reliably ships useful work, earns operator trust, and compounds
improvements from its own operational evidence.

## Quality Dimensions

### Execution Reliability
- Tasks complete without manual intervention at a high rate
- Failures are caught, classified, and recovered automatically
- CI remains green; regressions are detected and fixed within one sprint

### Planning Quality
- Sprint plans are evidence-driven, not just prompt-driven
- Backlog reflects actual gaps, not speculative churn
- Objectives and production feedback visibly influence task selection

### Operator Trust
- Every automated action is auditable (logs, artifacts, commit messages)
- Escalation paths exist and work; humans are not surprised by agent behavior
- Configuration is explicit and inspectable; no hidden prompt heuristics

### Adoption & Credibility
- README, demos, and quickstart are compelling and up to date
- A new technical user can understand what the system does in under 60 seconds
- Public proof of capability (real PRs, real issues, real CI runs) is visible

### Self-Improvement Loop
- The system generates actionable improvement issues from its own metrics
- Remediation issues close real gaps, not theoretical ones
- Evidence quality improves over time (fresher data, tighter feedback loops)

## Skill Dimensions

These are the capabilities the system should strengthen over time. Planners
should prefer work that advances underdeveloped skills.

| Skill                  | Description                                                        |
|------------------------|--------------------------------------------------------------------|
| Task dispatch          | Route work to the right agent with correct priority and context    |
| Failure recovery       | Detect, classify, and recover from blocked or partial outcomes     |
| Strategic planning     | Select sprint work that moves objectives, not just clears backlog  |
| Domain evaluation      | Assess work quality against repo-specific criteria, not just CI    |
| Evidence integration   | Feed production metrics, user signals, and outcomes into planning  |
| Cross-repo coordination| Sequence work across dependent repos without manual intervention   |
| Public credibility     | Maintain visible, up-to-date proof that the system works           |
