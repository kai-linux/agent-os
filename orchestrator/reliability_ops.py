"""Local operator controls and offline recovery. Never starts queue workers."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from orchestrator.delivery_store import DeliveryConflict
from orchestrator.reliability import (
    approve_release,
    authorize,
    evaluate,
    settings,
    snapshot,
    tenant_for,
)
from orchestrator.reliability_store import ReliabilityStore


def backup(cfg, destination):
    store = ReliabilityStore(cfg)
    destination = Path(destination)
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        with (
            closing(sqlite3.connect(store.delivery.path)) as src,
            closing(sqlite3.connect(destination)) as dst,
        ):
            src.backup(dst)
            if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Backup integrity check failed")
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return {
        "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
        "bytes": destination.stat().st_size,
    }


def restore_copy(source, destination, expected_sha256):
    """Restore to a NEW private DB for inspection; never overwrite a running controller."""
    source, destination = Path(source), Path(destination)
    if hashlib.sha256(source.read_bytes()).hexdigest() != expected_sha256:
        raise ValueError("Backup checksum mismatch")
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        with (
            closing(
                sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True)
            ) as src,
            closing(sqlite3.connect(destination)) as dst,
        ):
            if src.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Backup integrity check failed")
            src.backup(dst)
            # External effects cannot be rolled back with the database. Quarantine
            # all unfinished work until an operator compares it to remote receipts.
            dst.execute("UPDATE attempts SET state='lost' WHERE state='running'")
            dst.execute(
                "UPDATE goals SET state='paused',reason='Restored copy: reconcile external effects before resuming' WHERE state NOT IN ('succeeded','failed','cancelled')"
            )
            dst.execute("UPDATE outbox SET lease_until=0")
            dst.execute(
                "UPDATE reliability_spans SET state='interrupted' WHERE state='running'"
            )
            dst.execute("DELETE FROM reliability_approvals")
            dst.commit()
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return {"restored": True, "quarantined": True, "execution_enabled": False}


def command(cfg, args, actor):
    store = ReliabilityStore(cfg)
    tenant = args.tenant
    action = args.action
    roles = {
        "snapshot": "read",
        "traces": "read",
        "memory-list": "memory",
        "memory-put": "memory",
        "memory-delete": "memory",
        "evaluate": "evaluate",
        "approve": "release",
        "usage": "usage",
        "seal": "usage",
    }
    authorize(cfg, tenant, actor, roles[action])
    if action in {"evaluate", "approve"}:
        if (
            args.profile
            not in settings(cfg)
            .get("tenants", {})
            .get(tenant, {})
            .get("profiles", {})
            .values()
        ):
            raise DeliveryConflict("Profile does not belong to this tenant")
        if action == "evaluate":
            return {"evaluation_id": evaluate(cfg, args.profile, actor=actor)}
        approve_release(cfg, tenant, actor, args.profile, args.evaluation, args.reason)
        return {"approved": True}
    if action == "snapshot":
        result = snapshot(cfg)
        names = set(
            settings(cfg)
            .get("tenants", {})
            .get(tenant, {})
            .get("profiles", {})
            .values()
        )
        # Per-tenant controls must not return aggregate data from other tenants.
        return {
            "mode": result["mode"],
            "profiles": [p for p in result["profiles"] if p["profile"] in names],
        }
    if action.startswith("memory-"):
        if action == "memory-list":
            return store.memory(tenant, actor=actor)
        if action == "memory-delete":
            store.delete_memory(tenant, args.key, actor=actor)
            return {"deleted": True}
        names = (
            settings(cfg)
            .get("tenants", {})
            .get(tenant, {})
            .get("profiles", {})
            .values()
        )
        from orchestrator.reliability import profile

        limits = [profile(cfg, name)["memory_ttl_seconds"] for name in names]
        if limits and args.ttl > min(limits):
            raise ValueError("Memory retention exceeds the tenant profile limit")
        return {
            "revision": store.put_memory(
                tenant,
                args.key,
                args.value,
                source=args.source,
                actor=actor,
                ttl_seconds=args.ttl,
                expected_revision=args.revision,
            )
        }
    if action == "traces":
        goal = store.delivery.get(args.goal)
        if tenant_for(cfg, goal) != tenant:
            raise DeliveryConflict("Goal belongs to another tenant")
        return store.traces(args.goal)
    with store.delivery._db() as db:
        row = db.execute(
            "SELECT goal_id FROM attempts WHERE id=?", (args.attempt,)
        ).fetchone()
    if not row or tenant_for(cfg, store.delivery.get(row[0])) != tenant:
        raise DeliveryConflict("Attempt does not belong to this tenant")
    with Path(args.file).open("rb") as stream:
        payload = stream.read(65537)
    if len(payload) > 65536:
        raise ValueError("Usage import too large")
    data = json.loads(payload)
    if action == "usage":
        return {"receipt_key": store.usage(args.attempt, data, actor=actor)}
    store.seal_usage(args.attempt, data, actor=actor)
    return {"sealed": True}


def main():
    from orchestrator.paths import load_config

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=[
            "snapshot",
            "traces",
            "memory-list",
            "memory-put",
            "memory-delete",
            "evaluate",
            "approve",
            "usage",
            "seal",
            "backup",
            "restore",
        ],
    )
    for key in (
        "tenant",
        "profile",
        "evaluation",
        "reason",
        "goal",
        "key",
        "value",
        "source",
        "attempt",
        "file",
        "destination",
        "sha256",
    ):
        parser.add_argument("--" + key)
    parser.add_argument("--ttl", type=int, default=86400)
    parser.add_argument("--revision", type=int, default=0)
    args = parser.parse_args()
    required = {
        "traces": ["goal"],
        "memory-put": ["key", "value", "source"],
        "memory-delete": ["key"],
        "evaluate": ["profile"],
        "approve": ["profile", "evaluation", "reason"],
        "usage": ["attempt", "file"],
        "seal": ["attempt", "file"],
        "backup": ["destination"],
        "restore": ["file", "destination", "sha256"],
    }
    for flag in required.get(args.action, []):
        if not getattr(args, flag):
            parser.error("--" + flag + " is required for " + args.action)
    cfg, actor = load_config(), "local:" + str(os.getuid())
    if args.action in {"backup", "restore"}:
        # Whole-store recovery is an OS operator operation, never tenant delegated.
        if actor not in settings(cfg).get("recovery_operators", []):
            raise DeliveryConflict(
                "Local identity is not a configured recovery operator"
            )
        result = (
            backup(cfg, args.destination)
            if args.action == "backup"
            else restore_copy(Path(args.file).resolve(), args.destination, args.sha256)
        )
        ReliabilityStore(cfg).audit(
            "controller", actor, "recovery." + args.action, args.destination
        )
    else:
        if not args.tenant:
            parser.error("--tenant is required")
        result = command(cfg, args, actor)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
