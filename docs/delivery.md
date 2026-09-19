# Persistent Delivery And Operations

Agent-OS now has a durable delivery controller for newly dispatched GitHub work.
It owns the original human request across attempts, child work, waits and restarts.
GitHub Projects remains the planning interface; the private Proof dashboard shows
operational health and evidence-based KPIs. Neither a model's result file nor an
issue being closed is sufficient acceptance evidence.

## The Operating Contract

Move a trusted issue to **Ready** in a configured project as before. The dispatcher
retains its title/body verbatim and records any model interpretation separately.
New work receives a stable `g-...` identity and a revision in
`runtime/delivery/state.sqlite3`. Back up this directory with the private runtime,
including SQLite's WAL when active, or use SQLite's backup API. Do not commit it.

Simple coding issues default to an explicitly linked PR merged to the configured
base branch. This proves integration, not semantic correctness or deployment.
Add checks for the actual destination when a merge alone is insufficient.
Non-coding work without checks needs explicit human acceptance, not a fake diff.

Optional issue section:

````markdown
## Delivery Contract
```yaml
kind: program
scope: Deliver the launch, including the public recording and supporting site.
out_of_scope: Paid advertising and new account creation.
targets:
  - owner/workspace
  - owner/website
  - https://example.org/launch
max_attempts: 24
max_parallel: 2
budget_usd: 30
deadline: 2026-10-01T16:00:00Z
checks:
  - id: published-launch
    type: url
    url: https://example.org/launch
    status: 200
    contains: ["Watch the walkthrough"]
  - id: final-acceptance
    type: human
risks:
  - Publication needs delegated account access.
```
````

Budgeted goals require `delivery_attempt_reservation_usd` in operator config.
Attempts, concurrent slots and reservations count across the entire parent tree,
including retries and revisions; switching providers does not reset limits.
Unknown actual cost retains its reservation. This is an admission-control budget,
not a guarantee against a provider charging above a reservation. Use provider-side
spend caps for an absolute financial ceiling. Actual charges remain unknown until
observed; the dashboard does not invent them from text length.

## Programs And Projects

Use `kind: program`, `project` or `milestone`, or a corresponding `task:program`
label. The decomposer proposes 2-20 work packages per level, with stable keys and
dependencies. Larger programs need project-level decomposition rather than
silently dropping packages. Cross-repository work is limited to configured
workspaces explicitly named in the parent targets.

The controller persists the scope baseline before creating issues, recognizes
its own child markers after interruption, and dispatches dependency-ready work.
Each child retains a parent and parent revision. Completing every child still
does not satisfy the parent's own combined acceptance checks. No parent issue
is closed just because decomposition succeeded.

Declare existing dependencies with `depends_on: [owner/repo#123]` and an existing
parent with `parent: owner/repo#100` in the contract. Those goals must already be
registered. Cycles, including implicit parent/child completion dependencies, are
rejected. A parent's paused state or unmet prerequisite also blocks its children.

## Evidence And Non-Coding Actions

Available checks are `merged_pr`, `file`, `url`, `configured_command` and `human`.
File checks accept workspace-relative `path`, `min_bytes`, `contains` and `sha256`.
Verified small artifacts are archived privately before worktree cleanup. Paths
cannot escape the workspace. URL checks need an exact declared target, reject
private network addresses and redirects, and support status/content/hash checks.
Observations are limited to 2 MiB; use a dedicated verifier for video/media.

A `configured_command` check contains only `id`, `type` and an operator-configured
`name`. Config supplies a fixed `argv`, allowed `repos` and bounded timeout. Issue
bodies and model output cannot supply arbitrary verifier shell commands. Prefer
operator-owned verifier executables outside writable worker workspaces.

Installed capability is not authority to use an account. Optional action adapters
have fixed argv, exact allowed targets, bounded input fields and explicitly named
environment variables. The worker sees only their public invocation schemas and
can propose up to eight actions in the ignored `.agent_actions.json`:

```json
[{"capability":"publish-recording","target":"approved-channel","input":{"asset":"walkthrough.mp4"}}]
```

The goal also needs explicit delegation:

```yaml
allowed_actions:
  - capability: publish-recording
    target: approved-channel
    max_calls: 1
```

Child grants can narrow, never widen, parent authority. Limits apply across the
whole tree. Adapters receive structured JSON on stdin with a stable `action_id`;
they must return `{"receipt": {...}}`. Repeated identical proposals reuse a
confirmed receipt. Crashes/timeouts leave uncertainty and prevent automatic
repetition. Reconcile the real remote object before confirming a receipt. No
exactly-once guarantee is claimed for a provider without idempotency support.

There is no preinstalled video recording/upload adapter in this change. Workers
must inspect available capabilities, preserve intermediate work and ask a specific
access/approval question when needed. A script alone cannot pass a recording check.
Legacy CLI workers in observation mode still run as trusted host processes;
action gates alone are **not an OS sandbox**. The opt-in
[general reliability contract](reliability.md) adds a measured model gateway,
network-off isolated tools and verifiers, scoped operator roles, release
qualification, drift and service-target gates. Missing qualification fails closed.
Do not treat a merged implementation as an activated or proven production release.

## Controls And Recovery

Use authenticated Telegram commands or their local CLI equivalents:

```text
/goals
/goal status g-...
/goal pause g-... Reason
/goal resume g-... What changed
/goal answer g-... Specific answer
/goal cancel g-... Reason
/goal accept g-... Reviewed outcome and acceptance reason
/goal risk g-... Newly observed risk
/goal actions g-...
/goal receipt g-... ACTION_ID Verified external receipt reference
```

`accept` satisfies human checks only; it cannot bypass failed machine checks.
Answers, risks, prior attempts and decisions are sourced and retained under the
original goal, not turned into a new unrelated task. Editing an active issue's
title/body pauses it at source reconciliation; `/goal revise g-...` explicitly
adopts a new revision, retains the old contract and cancels unfinished old children.
Changing hierarchy requires a new linked goal. A closed issue without an associated
merged PR stops unverified work; it is never silently reopened or counted as a
verified success. Merge-triggered closure can still be awaiting deployment or other
checks. Use explicit cancellation (or GitHub's not-planned closure) to stop that work.

The existing queue/dispatcher cadence runs reconciliation. Pause/cancel/revision
stops a monitored worker process group; already performed external effects cannot
be undone. Expired leases wait for reconciliation rather than blindly retrying.
Timed quota/capacity waits wake without model calls. Failed local acceptance gets
bounded correction attempts with verifier feedback. Prepared PR delivery retries
the GitHub handoff without restarting a worker. Missing result files do not cause
a fallback if independent checks already prove delivery.

Lifecycle changes and pending GitHub/Telegram notices are one SQLite transaction.
Delivery retries separately with backoff. GitHub uses one stable status comment.
Telegram is at-least-once: a lost acknowledgment can yield a duplicate message,
but does not reset the task outcome. Dashboard alerts expose undelivered notices.

Waiting with a human condition gets `blocked` and `human-required`; verification
gets `verification-required`. The board uses In Review when available, otherwise
Blocked. Task-level In Progress requires a worker lease. Parent active status
means managing children, not a fictitious worker. New Ready issues are intake;
use goal controls for existing managed waits/revisions, not legacy Retry buttons.

## Dashboard

```bash
pip install -r requirements.txt
python -m orchestrator.dashboard.server --port 8765
```

Open `http://127.0.0.1:8765` on the host. The server is read-only and private by
default. For remote use, use an authenticated tunnel or the existing shared-secret
/ Tailscale auth configuration; do not expose an anonymous public listener.
Tailscale identity headers are accepted only from configured trusted proxies.
Set allowed hostnames when using a proxy. The systemd user-service template is in
`contrib/systemd/agent-os-dashboard.service`; monitoring can remain up while
execution is disabled. No GitHub Actions service is needed for this dashboard.

The Proof view refreshes every 10 seconds and provides hierarchy filters, goal
details, worker performance, risks, failures, deadlines, retries, actual cost
coverage and both attempt and intent-to-delivery duration. It distinguishes a
healthy HTTP connection from a coordinator heartbeat or worker lease. Completion
without current evidence raises an alert. Outages explicitly mark the retained
snapshot stale. `/api/observations` exports `proof.observations.v1` without raw
prompts, file contents, worker output or local workspace paths.

```bash
python -m orchestrator.delivery --snapshot
python -m orchestrator.delivery --tick
python -m orchestrator.delivery status g-...
```

Proof is pinned to an immutable reviewed commit. Its benchmark/simulated runs are
a separate product surface and cannot be ingested as live operational evidence.
Verification strength still depends on the chosen checks. Keyword presence is
not research quality, a merged PR is not a deployed service, and delivery is not
measured business impact.

## Migration And Limits

New dispatched work uses the controller. Existing unowned mailbox histories are
not mass-imported or replayed: their effects and costs may be unknown. Legacy
model-quality counts remain visible separately, not in the verified denominator.
To reconcile a selected historical issue without rerunning a worker:

```bash
python -m orchestrator.delivery adopt owner/repo#355 implementation
```

Closed issues are independently checked, then verified or retained as cancelled
without proof; they are not reopened. Open adopted issues start in Backlog and
require a deliberate resume. Do not adopt an issue while a legacy worker is active.
The SQLite database is local-host coordination, not distributed fleet consensus.

The default `planning_policy: scoped_delivery` disables speculative growth backlog
generation and has planner/groomer entrypoints reconcile accepted commitments.
`legacy_growth` is an explicit opt-in to the former policy. Dispatcher-only mode
still does not automatically review/merge PRs; preparation is not reported as
delivery while awaiting that review.

This is a bounded delivery foundation, not proof of an unrestricted autonomous
company. Remaining boundaries include OS-enforced tool isolation, a real media
capture/publication pilot, provider billing receipts, domain-specific quality and
business-impact evaluations, and distributed-host ownership. These are reported
as unproven, not inferred from the presence of modules or dashboard cards.

## Deployment Gates

A merged PR is not automatically a deployed runtime. `bin/run_autopull.sh` follows
the immutable commit in `runtime/deploy-approved-sha`, not the newest `main`.
Only advance this pin for an explicitly approved release after testing and checking
active workers. Updating the checkout alone is temporary: the next autopull will
restore the approved commit. Keep the guard, dispatcher-only setting and existing
schedules intact during a dashboard rollout.

Read-only monitoring can run independently of that execution release. Export a
reviewed commit with `git archive` into a private persistent release directory,
install its pinned dependencies into that directory's `.venv`, and set the user
service's `WorkingDirectory` and `ExecStart` to that release. Set
`Environment=ORCH_ROOT=%h/agent-os` so observations still read the real runtime.
Do not point a lasting service at a temporary development worktree. This starts
only `orchestrator.dashboard.server`, not the coordinator or queue. A stale
coordinator alert is expected until the execution release is actually enabled.

The GitHub identity used by the runtime needs Project read/write access in
addition to repository access. Reading issues and posting comments can work while
Project updates fail. Inspect scopes with `gh auth status` under the runtime
identity and check whether `GH_TOKEN` or `GITHUB_TOKEN` overrides the stored login;
never print token values. For an OAuth login, the account owner can grant the
missing access with `gh auth refresh --hostname github.com --scopes project`.
That requires an account authorization, not a code retry or GitHub Actions job.
Pending updates stay in the outbox and retry after access is restored.
