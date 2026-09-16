# Delivery Operations Rollout, 2026-09-16

Agent-OS PR358 adds persistent intent/program ownership, bounded execution,
independent acceptance, durable notifications and the live Proof dashboard.
Proof PRs 1-3 add operational observations separate from benchmark results and
keep historical reconciliation out of live delivery metrics and activity charts.

The deployment check found two operational gates, not worker failures:

- The host's immutable approved execution pin still selects `dc4559703af49126d2403132601992fff12dd4b1`.
  An initial fast-forward was reverted by the intentional autopull guard. Approval
  to advance the pin was requested; it was not silently overwritten or bypassed.
- The active GitHub OAuth login has repository access but not Project access.
  No environment token override was present. GitHub Project projection remains
  queued, rather than being reported as delivered.

The private read-only dashboard is installed as a user service on localhost:8765
from a separate immutable release directory with its own virtual environment.
No new cron, public listener, execution-mode change or worker was added. Monitor
health and execution health are separate: an inactive coordinator raises an alert.

Issue #355 was independently reconciled against merged PR356 and its actual README
diff. Its Telegram completion notice was acknowledged. It remains closed and is
marked historical, not counted as a new task or a near-instant delivery. The live
check exposed a historical-event chart inconsistency, corrected in Proof PR3 and
covered by both Proof and Agent-OS HTTP regression tests.

Validation of the foundation: 794 Agent-OS tests, 43 Proof tests, passing remote
Agent-OS CI, desktop/mobile browser checks, safe rendering of hostile titles,
outage/stale-snapshot behavior, and installed-wheel static assets. The follow-up
adds two Proof regression cases and an Agent-OS HTTP integration case. No claims
of real non-code publication, complete provider billing, OS sandboxing, or measured
business impact are inferred from those checks. See [delivery limits](../delivery.md).
