# From Tasks to Persistent Intent

Implementation follow-up: [Persistent Delivery And Operations](delivery.md)
describes the new goal controller, program hierarchy, evidence gates, bounded
actions, durable notifications and Proof dashboard. The findings below remain
the historical audit baseline; the follow-up documents implemented behavior,
tests and residual boundaries rather than declaring every capability proven.

## Scope and Verdict

Audit date: 2026-09-16. Source baseline: `94fd9b3` on `origin/main`.
Evidence: source, relevant existing tests, retained local runtime records, and
GitHub issue #355 / PR #356. Historical logs are samples, not a live fleet audit.
The proposed capabilities and acceptance scenarios below are not implemented by
this document. No workers, schedules, credentials, or live objectives were changed.

The operator's target is a persistent, autonomous system that translates human
intent into useful outcomes, including work outside software development. GitHub
can remain its workspace and project-management interface.

Agent-OS has useful execution infrastructure, but its central contract still
follows a repository task through an agent result and GitHub updates. It does not
consistently preserve, own, and verify the human outcome across that whole path.
Adding more agents or task types alone will leave this gap intact.

Success should mean: the requested outcome has supporting evidence at the right
destination, within delegated authority and budget. If it cannot be achieved yet,
the system retains ownership, reports the specific dependency, and knows when to
resume. Persistence includes waiting without spending model calls.

## Findings

### 1. The original intent can change during intake

[`task_formatter.py`](../orchestrator/task_formatter.py) instructs the model to
infer two to four success criteria and always prefer minimal diffs. Its output
does not distinguish explicit requirements from assumptions. Context injection
adds useful objective and sprint material, but does not establish that the
interpretation matches what the person meant.

A retained footer task illustrates this: a complaint about different links
apparently leading to the same content became a criterion about links sharing
the same domain. These require different fixes. The original intent can survive
as prose while the executable success criterion has already drifted.

**Required:** retain the original request and revisions, identify assumptions,
record target and acceptance criteria with their sources, and ask a focused
question when ambiguity would materially change the result. Low-risk work can
proceed with disclosed assumptions; every request need not become an interview.

### 2. No durable owner consistently carries a goal through its children

In [`github_dispatcher.py`](../orchestrator/github_dispatcher.py),
`_try_decompose()` closes the parent epic after creating children, promotes the
first child to Ready, and sends the rest to Backlog. Splitting work therefore
closes the original issue before its aggregate outcome is verified.
`create_followup_task()` in [`queue.py`](../orchestrator/queue.py) creates a new
task with the previous next step as its goal. Ancestry and prior context exist,
but these are not an enforced parent-outcome completion rule.

**Required:** a stable goal identity across plans, child tasks, retries, waits,
and restarts. Child completion must not close the goal. Someone can cancel or
revise a goal without leaving old children free to act under superseded intent.
Recurring duties also need a next-check condition rather than a single terminal
task or a continuously running worker.

### 3. The workspace is too tightly coupled to the execution target

`build_mailbox_task()` selects the configured local repository, and
`write_prompt()` tells every worker it is a coding worker confined to the current
repository. The formatter does recognize research, content, design, and browser
automation, but their execution still passes through this repository-centric
contract. `_maybe_transfer_public_website_issue()` adds a useful companion-site
heuristic; it is not general target discovery.

[`tool_registry.py`](../orchestrator/tool_registry.py) validates configured tool
records and formats allowed tools into the prompt. The
[`runner`](../bin/agent_runner.sh) launches CLIs with approval bypasses. A tool
description in a prompt does not enforce per-action authorization. In particular,
`write_prompt()` treats available credentials as sufficient reason to publish
external posts. Credentials prove access, not permission for a particular action.

**Required:** separate the workspace from the object being changed: deployed
site, document, account, recording, service, or several repositories. Discover
and test relevant capabilities before declaring work human-only. Enforce allowed
actions, data boundaries, and spending limits at execution, including any shell
escape path. Reuse existing approvals; bind approval to the actual action and
current goal revision. Missing authority or physical access can legitimately
require a human handoff.

The existing trusted-author filter is useful for issue intake. Broader tool use
also needs to treat retrieved pages, messages, and files as evidence rather than
instructions that can grant authority. Test that boundary when adding tools.

### 4. Completion has several conflicting meanings

The queue combines `.agent_result.md`, tests, Git changes, and task-specific
checks. These are useful evidence sources, but neither a model claim nor a new
diff universally proves the requested outcome. An already delivered change may
produce no new diff; a locally created page may never reach the requested site.

In [`github_sync.py`](../orchestrator/github_sync.py), `sync_result()` can put an
issue in Done while its PR is still open. Conversely, its terminal-issue guard
can preserve GitHub's closed state without correcting the queue's partial result.
[`pr_monitor.py`](../orchestrator/pr_monitor.py) has additional review and merge
gates, but skips dispatcher-only repositories. Mode selection changes which
stages run; the meaning of a user-visible completion needs to remain explicit.

Issue [#355](https://github.com/kai-linux/agent-os/issues/355) is a concrete
contradiction. [PR #356](https://github.com/kai-linux/agent-os/pull/356) merged the
requested README title change on 2026-09-01. The retained worker log shows a
complete result downgraded to `partial / no_diff_produced` and a blocked queue
state. The metrics retain that partial result. The completion-notification path
requires `complete`, explaining the absent Complete message in this case.

**Required:** a canonical lifecycle reconciled against actual outcome evidence.
Distinguish implementation prepared, reviewed, merged, deployed, and verified
when the goal requires those stages. For a document or video, use appropriate
artifact and destination checks. A script is a valid intermediate deliverable
for a video, but cannot satisfy a request for a published recording.

### 5. Recovery and communication are not one durable transaction

The queue has locks, stalled-process recovery, retry limits, provider cooldowns,
and structured blockers. These address real failures. The sampled April 23
reception-viewer retry log records an immediate provider quota failure, not an
hour of productive work. Later cooldown protections exist; that historical
example does not establish that today's exact behavior is unchanged.

Recovery still primarily operates at task/process boundaries. It cannot generally
tell whether an external action succeeded before a worker died, or resume from
a durable action receipt. Completion metrics, GitHub updates, mailbox moves, and
Telegram sends are separate operations. `send_telegram()` can fail without a
durable completion-message retry. The
[`incident router`](../orchestrator/incident_router.py) already persists pending
incident notifications, so that useful pattern should be extended rather than
ignored.

**Required:** durable checkpoints and action receipts, explicit wait/wake
conditions, and budgets across an entire goal lineage. A retry needs a reason
it could now succeed. Persist lifecycle changes and pending notifications
atomically; reconcile GitHub and Telegram from those changes. Distinguish a live
process from recent meaningful progress. Never imply exactly-once external
effects when a provider offers no idempotency or reconciliation mechanism.

### 6. Memory is mostly about repository work

[`codebase_memory.py`](../orchestrator/codebase_memory.py) injects repository
summaries and recent changes, then appends information from complete results.
Objective files, sprint history, approval records, and prior attempts retain
additional context. These are useful foundations, but do not form a durable,
queryable record of a person's intent, preferences, decisions, and current facts.
For Telegram, ordinary free text is handled in the pending QA-reply path rather
than as a general goal-steering conversation.

**Required:** distinguish user decisions, observed facts, model hypotheses,
action receipts, and reusable procedures. Retain source, time, scope, and
supersession information; check freshness when it matters. Retrieve relevant
context per action. A generated completion summary must not silently become a
verified fact. Human answers must resume the waiting goal instead of requiring
a fresh issue that loses the previous work. Apply retention and access rules so
personal context is not indiscriminately copied into public issues or prompts.

### 7. Self-assessment rewards component presence and activity

[`system_architect.py`](../orchestrator/system_architect.py) discovers roles from
Python filenames, jobs from function-name text, agents from configuration, and
sensors partly from metric filenames. `evaluate_system_architect()` compares
those names with the target model. Even a no-op module can satisfy presence;
the existing tests use no-op jobs to exercise this inventory. It cannot establish
that the end-to-end capability actually works.

The tracked [strategy](../STRATEGY.md) also pursues an autonomous software
organization and GitHub adoption, allocating at least 40% of sprint capacity to
adoption/credibility work. That is a different mandate from the operator's new
goal. Adding modules under the old mandate can generate more activity without
making the system a dependable translator of intent.

**Required:** behavioral evaluations drawn from real failed goals, with fresh
evidence, user corrections, intervention count, elapsed time, and total cost.
Self-improvement must address a reproduced failure and demonstrate improvement
on both that case and held-out cases. Missing measurement is its own gap, not
evidence that another feature is needed. Replace the old planning objective
before enabling autonomous planning for this new direction.

## What the Retained Metrics Establish

Read from the local `runtime/metrics/` files on the audit date:

| Dataset | Coverage in retained file | Observation |
| --- | --- | --- |
| `agent_stats.jsonl` | 281 records, March 18 to September 1, 2026 | 206 complete, 46 partial, 29 blocked |
| `outcome_attribution.jsonl` | 70 snapshots, March 31 to April 20, 2026 | 65 inconclusive, 4 improved, 1 unchanged |

These are different populations and time windows. Outcome snapshots are not
unique human goals, and recorded improvement is not independently established
causation. `record_metrics()` intentionally excludes some infrastructure/quota
failures for model-quality scoring. Consequently, these files do not supply an
end-to-end user-goal success rate. A missing recent snapshot also does not prove
that every live service is inactive.

Report execution health separately from goal fulfillment and downstream impact.
A delivered research brief can be verified now while its business impact remains
unknown. Neither a merged PR nor an inconclusive adoption metric should decide
all three questions.

## Minimum Operating Model

Use a durable goal record as the owner of work. Keep its original request,
revision, interpreted outcome, assumptions, targets, constraints, delegated
authority, budget, and evidence requirements together. Attach plans, child
tasks, action receipts, evidence, questions, and wake conditions to that identity.
Simple tasks should take a short path through the same model.

The control loop is: understand, inspect current reality, plan, act, observe,
verify, then complete, revise, or wait. Model workers propose plans and actions;
deterministic lifecycle rules enforce authority, limits, and evidence gates.
Before repeating an uncertain external action, reconcile what actually happened.

Choose evidence checks before execution. Separate directly observed results from
model judgments. Subjective work needs a rubric grounded in the person's
preferences or examples, with uncertainty and review needs made explicit; a
second model's approval is not by itself proof of usefulness.

Use one durable lifecycle store with atomic transition and notification records.
GitHub and Telegram are interfaces to it; authenticated board moves and messages
become commands, and accepted state changes project back to those interfaces.
Synchronization must not interpret its own status updates as fresh user intent.
Reconcile missed updates after outages. Do not build a second competing status
system alongside the mailbox without a migration/compatibility boundary.

Suggested states are Backlog, Clarifying, Ready, Running, Waiting, Verifying,
Succeeded, Failed, and Cancelled. Task-level In Progress requires a current worker
lease and observable progress; a parent can remain active while its child waits,
with that reason visible. Existing board columns can represent these through
explicit status/label mappings; extra columns alone do not fix ownership.

A human handoff preserves completed work, names the exact decision or access
needed, and states how an answer will resume execution. It is a waiting state,
not success or an invitation to run the same failed attempt indefinitely.
Missing capabilities can be acquired or delegated within the same authority and
budget; newly generated skills need tests before reuse. Arbitrary requests do
not imply unrestricted permissions or a guarantee that all work is possible.

Retain the useful worktree/CLI execution path for coding, cost controls,
approvals, blocker taxonomy, independent PR verifier, quality harness, and
incident delivery. Adapt these behind the goal lifecycle rather than replacing
the whole system or adding another layer of agent roles.

## Delivery Order and Acceptance Gates

1. **Make one goal truthful end to end.** Establish stable identity, explicit
   outcome evidence, lifecycle transitions, and reliable status delivery. Replay
   #355 and a false-completion case. Include dispatcher-only mode. Do not expand
   the autonomous workload until these cases agree across worker, queue, board,
   and notifications.
2. **Prove persistence across different kinds of work.** Carry a small research
   deliverable and a real recording workflow through tool discovery, multiple
   steps, restart, verification, and a real wait/resume. Keep the parent open
   until its outcome is satisfied. For publication without delegated access,
   report a tested handoff, not a completed publication. This exercise determines
   which general-purpose interfaces are actually needed.
3. **Enable bounded learning and expansion.** Align planning with the new
   mandate, migrate existing task state explicitly, and gate capability claims
   on behavioral evaluations. Run recovery and authority tests before increasing
   concurrency or permitting autonomous tool acquisition. Evaluate cost and
   user corrections alongside completion, including unsuccessful attempts.

The following are proposed acceptance scenarios, not an existing passing suite:

| Scenario | Required observation |
| --- | --- |
| A site request is filed in its backend workspace | Inspect ownership and the actual route; act on the right target or clarify, without inventing a local substitute. |
| Different links appear to show the same content | Verify destinations/content; do not substitute a same-domain rule. |
| An agent exits without its result file after delivering work | Inspect durable receipts and actual output; verify or report uncertainty before retrying. |
| The requested change is already merged (#355) | Verify the requested content and target revision; reconcile to success without creating a redundant diff. A merely closed issue is insufficient. |
| A worker claims complete but only produced a plan | Keep the goal unsatisfied when the required artifact or external effect is absent. |
| A goal splits into several tasks | Parent remains owned until aggregate criteria pass; unstarted children remain visible. |
| A worker or host dies after an upload | Reconcile the action receipt or remote object before retrying; do not blindly duplicate the upload. |
| Access or a human decision is needed | Persist a specific question and human-wait state, stop model retries, then resume the same goal with retained artifacts. |
| Provider quota is exhausted | Wait until a relevant condition changes, within global and per-goal budgets; no stream of misleading starts. |
| Telegram or GitHub rejects an update | Retain the pending transition, retry delivery safely, and show delivery uncertainty without changing the goal outcome. |
| A person changes or cancels the goal | Fence queued actions under the old revision, reconcile in-flight effects, and show any irreversible work already performed. |
| A goal must wait 24 hours | Survive a restart, wake when due, refresh stale facts, and continue without repeated model calls during the wait. |
| A tool is installed but unavailable or unauthorized | Report tested readiness separately from installation; refuse unauthorized effects even if credentials exist. |
| A new capability is declared supported | Pass representative outcome and injected-failure tests, not just an inventory check or self-review. |

Verification performed for this audit: `python3 -m pytest tests/ -q` passed all
741 existing tests, including the targeted queue, GitHub sync, incident routing,
outcome attribution, and system-architect tests. Those results validate existing
test expectations; they do not establish that the proposed end-to-end acceptance
scenarios pass. Local document links and diff whitespace were checked. No live
non-coding pilot was run as part of this audit.
