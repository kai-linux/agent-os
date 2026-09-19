# General Reliability Contract

This layer makes general engineering requirements executable. It does not
certify business impact, an individual customer's quality bar, or months of
successful operation. Release eligibility and production service evidence are
different claims. The live view is `/reliability`, linked from the Proof dashboard;
machine consumers use `/api/reliability` and `/api/traces/<goal-id>`.

## Adoption And Authority

Start from [the policy template](reliability-policy.example.yaml). It is invalid
until the operator assigns real repositories, identities, absolute artifact paths
and an immutable reviewed controller release. No configuration is silently
installed into the live host. The existing execution deployment pin remains an
independent approval. Enforced mode forces dispatcher-only operation, even if a
legacy project override requests full automation. Self-directed review/deployment
jobs are not part of the qualified worker boundary.

`observe` preserves the authenticated legacy single-operator installation and
reports that enforcement is absent. `enforce` denies unmanaged mailbox work,
unmapped repositories, unqualified releases, unavailable isolation, exhausted
service targets and unreconciled prior usage. It does not fall back to trusted
host execution. Legacy model formatting is disabled in enforced intake; original
human intent is retained. Programs use a registered, measured planning adapter,
not an unmetered host CLI fallback.

Repository-to-tenant and task-type-to-profile mappings come only from operator
config, never issue bodies or model output. Parent/child work cannot cross tenant
boundaries in enforced mode; neither can declared dependencies. Reuse YAML templates for profiles, but give each
tenant its own profile names and release approvals. Customer-specific external
tool targets still need exact grants in the goal ancestry.

Principals have explicit tenant scopes and roles: reader, operator, approver,
billing, memory, administrator. Unknown identities fail closed when principals
are configured, even in observe mode. CLI identity is the actual `local:<uid>`;
Telegram uses its authenticated immutable `telegram:<user-id>`, not a mutable
username or shared group ID. Legacy global Telegram commands and callback buttons
require the controller administrator role. Goal commands remain tenant scoped.
The shared private dashboard is an **operator-wide view**, not a customer portal.
Do not give its bearer credential or host access to tenant-only users.

## Traces And Measured Usage

Worker attempts, model-gateway calls, isolated tools, registered actions and acceptance verification
have persisted spans with common goal/revision trace IDs and parent span IDs.
Worker restarts do not lose those records. Errors retain their type, not raw
provider messages. Allowed attributes exclude prompts, tool inputs, credentials
and model outputs. A running span after a crash stays unfinished, not successful.
The model adapter receives a W3C-format `traceparent` for downstream propagation.
These are local traces, not an installed distributed tracing collector.

`model_gateway.call_model` accepts a controller-owned active attempt and invokes
an operator-owned adapter using fixed argv and structured JSON stdin. The adapter
receives `{attempt_id, traceparent, request}` and returns `{output, usage}`.
Only named environment keys reach it. Place adapter code outside worker-writable
paths; Python adapters should use `-I` to exclude workspace import paths. Trusted
adapters must return provider metadata, never ask a model to estimate its cost.

Usage schema:

```json
{"provider":"provider-name","account":"nonsecret-account-reference","request_id":"provider-request-id","model":"model-version","input_tokens":123,"output_tokens":45,"cost_nano_usd":123456,"final":true}
```

Costs use integer billionths of USD in receipts. Provisional receipts may have
null cost. Exact replays are idempotent, final receipts are immutable, and a
provider/account/request identity cannot be charged to two attempts. Token counts
are measured, not estimated from characters. Include caching or other billed
categories in the actual total charge; the two token counters are not a price
calculator. Account references must never be credentials.

A finished attempt's cost remains unknown until a trusted collector seals the
**complete list** of receipt keys. One observed call is not full coverage. Sealing
updates the existing delivery cost and budget accounting atomically. Additional
requests cannot be appended after sealing. Incorrect final receipts require an
audited reconciliation, not silent overwriting. Existing CLI agents that do not
expose a complete measured request manifest remain unmetered; use an instrumented
adapter or an authorized billing collector. The implementation does not invent
provider billing access or invoice data.

```bash
python -m orchestrator.reliability_ops usage --tenant example --attempt ATTEMPT --file receipt.json
python -m orchestrator.reliability_ops seal --tenant example --attempt ATTEMPT --file receipt-keys.json
python -m orchestrator.reliability_ops traces --tenant example --goal GOAL
```

Adapter calls have a bounded timeout and output size; descendants are killed on
exit or timeout. A timed-out effect is uncertain and cannot be blindly repeated.
There is no claim of provider-side exactly-once delivery. Runtime reservations
are admission controls, not absolute provider spending caps.

## Memory And Isolation

Tenant facts have provenance, writer identity, revision and expiry. Updates use
compare-and-swap; stale writers cannot overwrite newer facts. Reads are tenant
scoped and expired facts are removed. The coordinator also purges expired facts.
Only a memory-role principal may change retained facts. Prompt injection labels
memory as untrusted evidence, never as authority. Intent and decision history
remain separately owned by the goal controller.

```bash
python -m orchestrator.reliability_ops memory-put --tenant example --key fact --value 'Reviewed fact' --source 'source-reference' --revision 0 --ttl 86400
python -m orchestrator.reliability_ops memory-list --tenant example
python -m orchestrator.reliability_ops memory-delete --tenant example --key fact
```

Memory retention cannot exceed the tenant profiles' configured limit. Live row
deletion does not erase previous backups; backup retention is a separate operator
responsibility. Secrets must not be stored as facts. Redaction is defense in depth,
not a guarantee that arbitrary sensitive personal information is detected.

Enforced workers run in Bubblewrap user/mount/PID namespaces, with capabilities
dropped, a new home and temporary directory, and only their worktree writable.
Host home, controller DB, other workspaces and inherited credentials are absent.
Git metadata is masked so workers cannot redirect later controller Git commands.
Repository tests and configured-command verifiers use the same boundary. Git
publication disables hooks/fsmonitor and refuses executable filters/included
config at any Git configuration level. Handoff files reject symlinks, hard links,
special files and oversized content before privileged reads or writes. A missing
or kernel-disabled sandbox is a failure, not a fallback.

Enforced profiles require network-off tools with no provider credentials. A
controller-owned agent loop calls the measured model adapter outside the tool
sandbox, then executes only structured `argv` tool proposals inside it. The model
cannot run a privileged host shell or connect to the operator dashboard. Tool
turns, request/output sizes and total attempt time are bounded; pause/cancel also
stops an active model adapter or tool process group. Complete observed usage
manifests are sealed on attempt completion; crashes remain unreconciled.

Adapter `output` for workers must be exactly `{"tool_calls":[{"argv":[...]}]}`
or `{"final":{"status":"complete","summary":"...","blocker_code":"none"}}`.
Planning adapters return a structured work-package plan instead. Provider-specific
adapters translate this request/response protocol; raw legacy CLI agents are not
quietly substituted in enforced mode. Install executable dependencies through
explicit read-only mounts. Keep provider credentials only in the trusted adapter's
named private environment, never argv. This is not a sandbox against a compromised host
kernel, malicious host administrator or malicious operator-owned adapter. Do not
run legacy unsandboxed review/deployment automation over untrusted artifacts;
dispatcher-only mode keeps those separate from this managed execution boundary.

Queue intake and execution select the profile's adapter, not whichever legacy
CLI happens to be installed. The adapter's name is reported as the worker
identity. Failures do not silently switch to an unqualified CLI. Reconcile any
uncertain billed request before changing providers or retrying.

## Evaluation And Release Gates

Every profile binds a controller Git SHA, evaluator, test artifacts, rollback
plan and incident playbook. Their hashes and the adapter registries form the
evidence fingerprint. Changing them invalidates approval. The executing checkout
must match the SHA and have no tracked modifications. A config string cannot
pretend to be the actual deployed release.

The evaluator is a bounded operator-owned executable. It returns numeric quality,
sample count and boolean results for named scenarios. Required scenarios include
end-to-end delivery, restart recovery, duplicate effects, denied actions, stale
revisions, provider failure, restore, isolation, approval expiry and the complete
measured-model/isolated-tool/independent-verification flow. Missing,
failed, timed-out or skipped scenarios cannot pass. `bin/evaluate_orchestration.py`
runs actual controller tests and emits an **engineering** score, not a business
quality score. Its providers are local fixtures and never enter live metrics.
Ordinary test runs may skip unavailable host isolation; the qualification report
maps that skip to a failed mandatory scenario, never an eligible release.
Add task-specific scenarios and reference datasets through the evaluator/artifact
contract when qualifying a business workflow.

```bash
python -m orchestrator.reliability_ops evaluate --tenant example --profile example-code
python -m orchestrator.reliability_ops approve --tenant example --profile example-code --evaluation RUN_ID --reason 'Reviewed evidence and recovery plan'
```

The approver must differ from the evaluation initiator. Only the latest passing,
fresh evaluation of unchanged artifacts can be approved. New failed evaluations
override older passes for eligibility. Quality loss from the approved baseline
blocks admission even when the absolute minimum still passes. Evidence expires.
Optional periodic evaluation uses the existing coordinator cadence and runs only
when explicitly configured; no cron is installed by this change. Running managed
workers recheck their release gate every five seconds.

## Service Commitments

The template gives separate code, research and external-action profiles explicit
success-rate and end-to-end latency targets, a rolling window, sample minimum and
memory retention. These values are configurable operating targets, not a signed
customer SLA. Define business quality and contractual obligations with the client.

Success uses independently accepted terminal tasks, not model completion claims.
Cancelled tasks and historical imports are excluded; the denominator and sample
count are visible. Latency includes waits. Open overdue work is an immediate
breach. Too few completed samples means insufficient data, not 100% reliability.
Service breaches stop admission of new goals. Already-started goals can finish or
recover within their original authority and budgets, rather than being trapped
forever by their own overdue status. Readiness transitions route through the
existing persistent incident router and its acknowledgment/escalation policy.

See [incident response](runbooks/incidents.md) and [recovery](runbooks/recovery.md).
Distributed fleet consensus, customer connectors/evals, host-specific egress
controls and sustained production evidence are not supplied by a generic library.
