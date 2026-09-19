# Agent-OS Incident Response

Owner: the operator configured for the affected tenant/profile. Maintain a real
contact and escalation schedule in private incident-router configuration before
production. Never include credentials in tickets or Telegram messages.

## Contain And Preserve

1. Acknowledge the routed incident; record its ID, affected goal/revision, attempts,
   release, profile fingerprint and last known good evaluation.
2. Pause the affected goal or parent with `/goal pause GOAL REASON`. For a host-wide
   compromise, an administrator uses `/off`. Preserve the private DB and logs.
3. Confirm the worker process group stopped. If it did not, treat containment as
   failed and stop the host service through the authorized infrastructure operator.
4. Take a private online backup using the recovery runbook. Do not delete pending
   outbox rows, action receipts or provider request IDs to make the dashboard green.

## Diagnose By Signal

| Signal | Required response |
|---|---|
| Evaluation failed, expired or drifted | Inspect the latest scenario results and exact release fingerprint. Fix or roll back; never reuse an older passing result to hide a newer failure. |
| Unknown usage or budget exhausted | Reconcile the provider request manifest and invoices. Do not treat missing charges as zero or switch providers to reset a goal budget. |
| Uncertain external effect | Query the actual remote object by its idempotency/request reference. Confirm its receipt, or perform an independently approved compensating action. Never blindly repeat it. |
| Cross-tenant or denied action | Keep the request blocked. Correct the scope/identity mapping only after approval; model instructions do not grant authority. |
| Source edit or stale revision | Review the changed human intent, then explicitly revise. Old attempts cannot approve new scope. |
| Stale worker/coordinator | Check process and lease state. Reconcile already-produced artifacts before launching another worker. A responding HTTP dashboard is not proof the controller is healthy. |
| Notification delay | Check the authenticated channel and Project scopes. Replay the persisted outbox, not the task. |
| Service target breach | Inspect both unsuccessful completed work and open overdue work; include human waiting time. Fix the cause before restoring admission. |

## Recover And Close

Use the recovery runbook to choose code rollback, forward fix, or quarantined
state restore. Re-run the required orchestration scenarios and relevant domain
evals on the actual candidate release. A separate approver authorizes the latest
passing evidence. Resume only affected goals with a recorded reason. Confirm
remote effects, worker ownership, cost coverage, board state and notifications.
Record the root cause and add a regression scenario before closing the incident.

Severity, human response/acknowledgment times and contact rotations must be set
by the deployment operator; this generic runbook does not invent staffed on-call
coverage or contractual response promises.
