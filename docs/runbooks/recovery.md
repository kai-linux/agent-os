# Agent-OS Recovery And Rollback

Never restore over a live database or force-push a shared repository. Source-code
rollback cannot undo CRM writes, messages, purchases or published artifacts.

## Before A Release

Record the current immutable approved SHA, configuration fingerprint and a verified
backup hash. Preserve the compatible runtime environment and dependency pins.
Know the affected profiles, tenant owner and remote action-receipt locations.
Run the restore and isolation qualification scenarios on the deployment host.

```bash
python -m orchestrator.reliability_ops backup --destination /private/backups/delivery-UNIQUE.sqlite3
```

The command uses SQLite's online backup API, checks integrity, refuses overwrite
and creates a mode-0600 file. It prints a SHA-256 manifest. Retain the hash outside
the backup file, and set a private backup retention policy. Whole-store backup
requires the explicitly configured local recovery-operator identity.

## Restore Drill

```bash
python -m orchestrator.reliability_ops restore --file /private/backups/delivery-UNIQUE.sqlite3 --destination /private/restore/candidate.sqlite3 --sha256 RECORDED_HASH
```

This creates a **new** private candidate DB, never changes the live DB, verifies
the checksum/integrity, marks running attempts lost, pauses unfinished goals,
clears notification leases and removes release approvals. Uncertain external
actions stay uncertain. Historical delivery and receipts remain intact.

Inspect the candidate offline. Verify goals, revisions, dependencies, effects,
notifications and measured-cost receipts against their external sources. Record
what happened after the backup; those effects will not disappear when restoring.
Do not activate the candidate until that reconciliation is complete.

## Code Rollback Or State Activation

1. Pause affected goals and stop dispatch/worker processes through the authorized
   infrastructure operator. Verify no live writer or worker lease remains active.
2. Prefer a reviewed forward fix when an older release cannot understand the new
   schema. Never assume arbitrary downgrade compatibility.
3. For a compatible code rollback, obtain approval for the exact known-good SHA
   in `runtime/deploy-approved-sha`; preserve the deployment guard. Restore its
   pinned dependencies and verify the checkout, rather than following mutable main.
4. If state replacement is necessary, preserve the current DB, WAL and SHM as one
   recovery set while stopped. Activate the reconciled candidate only through the
   approved infrastructure procedure. Do not mix a restored DB with old WAL files.
5. Run fresh qualification, obtain independent release approval, then resume a
   bounded goal. Verify the outcome, channel delivery and actual cost coverage.

State activation and privileged service changes are deliberately not automated
by the backup command. There is no blind database rollback across external effects.
Compensation is a new explicitly delegated action with its own receipt and approval.
