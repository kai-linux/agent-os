# External Repo Pilot Playbook

Use this guide when evaluating Agent OS on a repo that did not build Agent OS itself.

The goal of the first pilot is not "prove full autonomy." The goal is to answer a narrower question:

> Can Agent OS turn a small set of bounded issues into reviewable PRs with acceptable operator overhead?

If the answer is yes, expand the scope. If not, fix the constraints before enabling more automation.

## Recommended Pilot Shape

Run the first pilot with:

- `automation_mode: dispatcher_only`
- manual PR review
- one repo
- 5 to 10 issues
- issues sized for 10 to 40 minutes of agent execution
- existing CI and a deterministic `test_command`

Do not turn on the full planner/groomer/self-improvement loop until this pilot is stable.

## Good First Tasks

- add or tighten tests around an existing function
- add a small backend endpoint or CLI flag
- improve docs tied to an existing feature
- fix a contained CI failure with a clear repro
- refactor a local module behind passing tests

## Bad First Tasks

- redesign the UI
- restructure app state across many packages
- "improve the product" or "clean up the codebase"
- anything blocked on missing secrets, hidden setup, or undocumented workflows
- tasks that require product strategy rather than implementation discipline

## Issue Template For Pilots

Use issues with explicit observable outcomes:

```markdown
## Goal
Add a `/health` endpoint that returns status, version, and uptime.

## Success Criteria
- `GET /health` returns HTTP 200
- response body includes `status`, `version`, and `uptime`
- automated tests cover success path
- no existing endpoint behavior changes

## Constraints
- keep the diff minimal
- do not introduce new dependencies without justification
- leave auth behavior unchanged

## Task Type
implementation

## Agent Preference
auto
```

The dispatcher can improve rough issues, but the first pilot should avoid underspecified work on purpose.

## Graduation Criteria

Promote the repo from supervised pilot to broader automation only if most of the following are true:

- PRs are usually reviewable without major rewrites
- escalation volume is low enough that the operator stays ahead of it
- the repo's test command catches real regressions
- issues can be written in a bounded, observable form
- the agent is not repeatedly blocked on credentials or environment drift

If those conditions are not met, stay in `dispatcher_only` mode and fix the bottleneck first.

## What To Measure

During the pilot, record:

- tasks attempted
- PRs opened
- PRs merged
- escalations
- median operator minutes per task
- repeated blocker categories

The pilot passes when the operator overhead is lower than doing the same class of work manually, not when the system looks impressive on a dashboard.

## Short Pitch For Evaluators

If you need a concise recommendation for a founder, maintainer, or another model:

> Start Agent OS on one repo in `dispatcher_only` mode. Give it 5 to 10 bounded issues with clear success criteria. Review the first PRs manually. Expand only after the measured operator overhead and merge quality are acceptable.
