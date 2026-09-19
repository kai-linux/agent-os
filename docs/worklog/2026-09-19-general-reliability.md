# General Reliability Implementation, 2026-09-19

Scope: close reusable engineering gaps beyond the persistent-delivery foundation
merged in PRs 358-359. Customer connectors, business-specific evaluations,
contractual service promises and elapsed production evidence are distinct work.

## Implemented Boundaries

- Tenant-scoped operator, approver, billing and memory roles, immutable Telegram
  identity, local OS identity, independent release approval and audited controls.
- Provenance-bearing, expiring, revision-checked tenant facts, separately from
  authoritative intent and scope. Dependencies and ancestry cannot cross tenants.
- Durable worker/model/tool/action/verification traces, W3C correlation and bounded
  attributes without prompt or response payloads.
- Provider-neutral measured-call gateway, idempotent provider receipts and exact
  finished-attempt manifests. Missing/provisional coverage stays unknown and can
  block admission rather than becoming a zero-cost claim.
- Qualified worker loop with network-off Bubblewrap tools, no inherited host
  credentials/controller state, masked Git metadata, bounded process groups and
  explicit adapter routing. Repository tests/verifiers use the same OS boundary.
  Handoff files cannot redirect controller reads/writes through links or FIFOs.
- Release/artifact fingerprints, fresh passing orchestration evidence, separate
  approval, drift detection, periodic probes and persistent incident routing.
- Per-type rolling success/latency targets and visible sample counts. Service
  breaches stop new goals while allowing existing work to finish or recover.
- Private readiness view and JSON/trace APIs alongside the existing Proof view.
- Tested online backup and quarantined restore, with rollback/incident playbooks.

## Verification

The qualification executable runs ten real local controller scenarios: delivery,
restart, duplicate effects, denied actions, revision fencing, provider failure,
restore, OS isolation, expired approval and measured-model/tool/verification
integration. All ten passed on this host. Fixture providers and artifacts are
isolated from production observations. A skipped OS test fails qualification.

The full unit/integration suite passed 839 tests on this host. Secret scanning
and PR checks accompany the implementation. The readiness page was inspected at 390px and 1440px widths using
an empty temporary database; no horizontal overflow and no false healthy state.

## Activation And Remaining Evidence

No execution deployment pin, live policy, credentials, cron or privileged service
was changed. The shared host remains on its explicitly approved release. The new
mode is opt-in and requires a reviewed immutable checkout, real tenant identities,
trusted measured provider adapters, artifact paths, host qualification and a
separate approver. Enforced mode forces dispatcher-only legacy behavior.

This closes the listed implementation gaps, not the deployment and proof gaps.
It does not install customer CRM/media providers, certify business correctness,
promise absolute provider spending caps or prove months of reliable operation.
Operator adapters, the kernel and host administrator remain trusted. A private
operator dashboard is not a tenant-isolated customer portal. Multi-host consensus
and separately staffed incident response are outside this single-host controller.
