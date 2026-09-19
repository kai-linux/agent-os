"""Scoped controls, versioned release evidence and continuously checked readiness."""

from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
import time
from pathlib import Path

from orchestrator.delivery_store import DeliveryConflict, _dump
from orchestrator.reliability_store import ReliabilityStore
from orchestrator.worker_isolation import bounded_command

ROLES = {
    "read": {"reader", "operator", "approver", "billing", "memory"},
    "control": {"operator"},
    "accept": {"approver"},
    "receipt": {"approver"},
    "release": {"approver"},
    "evaluate": {"operator"},
    "usage": {"billing"},
    "memory": {"memory"},
    "recover": {"operator"},
    "admin": {"administrator"},
}
SCENARIOS = {
    "end_to_end",
    "restart",
    "duplicate_effect",
    "denied_action",
    "stale_revision",
    "provider_failure",
    "restore",
    "isolation",
    "approval_expiry",
    "qualified_worker",
}


def settings(cfg):
    value = cfg.get("reliability", {})
    if not isinstance(value, dict) or value.get("mode", "observe") not in {
        "observe",
        "enforce",
    }:
        raise ValueError("reliability.mode must be observe or enforce")
    return value


def enforced(cfg):
    return settings(cfg).get("mode") == "enforce"


def tenant_for(cfg, goal):
    repo = goal["metadata"].get("github_repo", "")
    matches = [
        name
        for name, t in settings(cfg).get("tenants", {}).items()
        if repo in t.get("repos", [])
    ]
    if len(matches) > 1:
        raise DeliveryConflict("Repository belongs to multiple tenants")
    if not matches and enforced(cfg):
        raise DeliveryConflict("Repository has no operator-configured tenant")
    return matches[0] if matches else repo or "local"


def authorize(cfg, tenant, actor, action):
    policy = settings(cfg)
    if not enforced(cfg) and not policy.get("principals"):
        return  # Existing authenticated single-operator installations remain compatible.
    principal = policy.get("principals", {}).get(actor, {})
    if tenant not in principal.get("tenants", []) or not ROLES.get(
        action, set()
    ).intersection(principal.get("roles", [])):
        ReliabilityStore(cfg).audit(tenant, actor, "access.denied", action)
        raise DeliveryConflict(
            "Identity lacks the required tenant role", code="permission_denied"
        )


def profile_for(cfg, goal):
    tenant = tenant_for(cfg, goal)
    tenant_config = settings(cfg).get("tenants", {}).get(tenant, {})
    return tenant_config.get("profiles", {}).get(
        goal["metadata"].get("task_type", "implementation")
    )


def worker_adapter(cfg, metadata):
    value = profile(cfg, profile_for(cfg, {"metadata": metadata}))
    name = value.get("model_adapter")
    adapter = cfg.get("model_adapters", {}).get(name, {})
    if not adapter.get("argv") or tenant_for(
        cfg, {"metadata": metadata}
    ) not in adapter.get("tenants", []):
        raise DeliveryConflict("A registered tenant-scoped worker adapter is required")
    return name


def profile(cfg, name):
    value = settings(cfg).get("profiles", {}).get(name)
    if not isinstance(value, dict):
        raise ValueError("Unknown reliability profile")
    owners = [
        t
        for t in settings(cfg).get("tenants", {}).values()
        if name in t.get("profiles", {}).values()
    ]
    if len(owners) > 1:
        raise ValueError(
            "Release approvals are tenant-specific; use separate profile names per tenant"
        )
    if not re.fullmatch(r"[0-9a-f]{40}", str(value.get("release", ""))):
        raise ValueError("Profile requires an immutable release SHA")
    for key in (
        "evaluation_ttl_seconds",
        "min_samples",
        "max_delivery_seconds",
        "memory_ttl_seconds",
    ):
        if type(value.get(key)) is not int or value[key] <= 0:
            raise ValueError(f"Profile needs positive integer {key}")
    for key in ("min_quality", "max_quality_drop", "min_success_rate"):
        if (
            type(value.get(key)) not in {int, float}
            or not math.isfinite(value[key])
            or not 0 <= value[key] <= 1
        ):
            raise ValueError(f"Profile needs {key} in [0,1]")
    if value.get("sandbox", {}).get("backend") != "bubblewrap":
        raise ValueError("Profile requires OS isolation")
    if value["sandbox"].get("network", "none") != "none" or value["sandbox"].get(
        "env_keys"
    ):
        raise ValueError(
            "Enforced tool execution cannot have network or provider credentials; use the measured adapter gateway"
        )
    if not SCENARIOS.issubset(set(value.get("scenarios", []))):
        raise ValueError("Profile is missing mandatory orchestration scenarios")
    return value


def fingerprint(cfg, name):
    value = profile(cfg, name)
    hashes = {}
    artifacts = {
        key: value[key] for key in ("evaluator", "rollback_plan", "incident_playbook")
    }
    artifacts.update(
        {"artifact:" + str(i): p for i, p in enumerate(value.get("artifacts", []))}
    )
    for key, raw_path in artifacts.items():
        path = Path(raw_path).resolve(strict=True)
        if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("Release artifacts must be bounded regular files")
        hashes[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashlib.sha256(
        _dump(
            {
                "profile": value,
                "artifacts": hashes,
                "model_adapters": cfg.get("model_adapters", {}),
                "action_adapters": cfg.get("delivery_actions", {}),
            }
        ).encode()
    ).hexdigest()


def deployed_revision():
    """Derive controller identity from its checkout, never from an issue or config claim."""
    root = Path(__file__).resolve().parents[1]
    try:
        top = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        if Path(top).resolve() != root:
            return None
        if subprocess.check_output(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"],
            text=True,
        ).strip():
            return None
        return subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def evaluate(cfg, name, *, actor="controller"):
    value = profile(cfg, name)
    stamp = fingerprint(cfg, name)
    store = ReliabilityStore(cfg)
    try:
        data = bounded_command(
            [value["python"], str(Path(value["evaluator"]).resolve())],
            cwd=str(Path(value["evaluator"]).resolve().parent),
            timeout=value.get("evaluation_timeout_seconds", 120),
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        )
        report = json.loads(data)
        if not isinstance(report, dict) or set(report) != {
            "quality",
            "samples",
            "scenarios",
        }:
            raise ValueError(
                "Evaluator output must match the orchestration report schema"
            )
        if (
            type(report["quality"]) not in {int, float}
            or not math.isfinite(report["quality"])
            or not 0 <= report["quality"] <= 1
            or type(report["samples"]) is not int
            or report["samples"] < 0
            or not isinstance(report["scenarios"], dict)
            or any(type(v) is not bool for v in report["scenarios"].values())
        ):
            raise ValueError("Invalid measured evaluation result")
        passed = (
            report["samples"] >= value["min_samples"]
            and report["quality"] >= value["min_quality"]
            and all(report["scenarios"].get(s) is True for s in value["scenarios"])
        )
        if fingerprint(cfg, name) != stamp:
            raise ValueError("Release artifacts changed during evaluation")
    except Exception as exc:
        report, passed = {"error": type(exc).__name__}, False
    return store.evaluation(name, stamp, report, passed, actor=actor)


def readiness(cfg, name):
    store = ReliabilityStore(cfg)
    failures = []
    try:
        value, stamp = profile(cfg, name), fingerprint(cfg, name)
    except (ValueError, OSError, KeyError):
        return {
            "profile": name,
            "ready": False,
            "reasons": ["invalid_profile_or_artifacts"],
        }
    runs, baseline = store.evaluations(name)
    if enforced(cfg):
        adapter = cfg.get("model_adapters", {}).get(value.get("model_adapter"), {})
        argv = adapter.get("argv")
        owners = {
            t
            for t, policy in settings(cfg).get("tenants", {}).items()
            if name in policy.get("profiles", {}).values()
        }
        if (
            not isinstance(argv, list)
            or not argv
            or any(not isinstance(a, str) or not a for a in argv)
            or not owners
            or not owners.issubset(set(adapter.get("tenants", [])))
        ):
            failures.append("measured_worker_adapter_missing")
    latest = runs[0] if runs else None
    if not latest or latest["fingerprint"] != stamp:
        failures.append("current_release_not_evaluated")
    elif not latest["passed"]:
        failures.append("evaluation_failed")
    elif store.clock() - latest["observed_at"] > value["evaluation_ttl_seconds"]:
        failures.append("evaluation_expired")
    if not baseline or baseline["fingerprint"] != stamp:
        failures.append("release_not_approved")
    if (
        latest
        and baseline
        and "quality" in latest["report"]
        and "quality" in baseline["report"]
    ):
        if (
            baseline["report"]["quality"] - latest["report"]["quality"]
            > value["max_quality_drop"]
        ):
            failures.append("quality_drift")
    # Profiles are release-specific. Running different code cannot inherit approval.
    deployed = deployed_revision()
    if deployed != value["release"]:
        failures.append("execution_release_mismatch")
    return {
        "profile": name,
        "ready": not failures,
        "reasons": failures,
        "latest_evaluation": latest["id"] if latest else None,
        "fingerprint": stamp,
        "release": value["release"],
    }


def approve_release(cfg, tenant, actor, name, ident, reason):
    authorize(cfg, tenant, actor, "release")
    if (
        name
        not in settings(cfg)
        .get("tenants", {})
        .get(tenant, {})
        .get("profiles", {})
        .values()
    ):
        raise DeliveryConflict("Profile is not assigned to this tenant")
    store = ReliabilityStore(cfg)
    runs, _ = store.evaluations(name)
    if (
        not runs
        or runs[0]["id"] != ident
        or runs[0]["fingerprint"] != fingerprint(cfg, name)
    ):
        raise DeliveryConflict(
            "Only the latest evaluation of unchanged artifacts can be approved"
        )
    if (
        store.clock() - runs[0]["observed_at"]
        > profile(cfg, name)["evaluation_ttl_seconds"]
    ):
        raise DeliveryConflict("Evaluation expired")
    store.approve(name, ident, actor=actor, reason=reason)


def execution_gate(cfg, goal):
    if not enforced(cfg):
        return
    name = profile_for(cfg, goal)
    tenant = tenant_for(cfg, goal)
    for ancestor in ReliabilityStore(cfg).delivery.context(goal["id"])["lineage"]:
        if tenant_for(cfg, ancestor) != tenant:
            raise DeliveryConflict("Goal ancestry crosses tenant boundaries")
    result = readiness(cfg, name)
    if not result["ready"]:
        raise DeliveryConflict(
            "Execution readiness blocked: " + ", ".join(result["reasons"]),
            code="release_not_ready",
        )
    raw = ReliabilityStore(cfg).delivery.snapshot()
    has_prior_attempt = any(a["goal_id"] == goal["id"] for a in raw["attempts"])
    if not has_prior_attempt and service_levels(cfg, name, raw)["status"] == "breach":
        raise DeliveryConflict(
            "Service-level error budget is exhausted", code="slo_breach"
        )
    if profile(cfg, name).get("require_sealed_usage", True):
        relevant = {g["id"] for g in raw["goals"] if _assigned_profile(cfg, g, name)}
        if any(
            a["goal_id"] in relevant
            and a["state"] != "running"
            and a["cost_usd"] is None
            for a in raw["attempts"]
        ):
            raise DeliveryConflict(
                "Prior provider usage needs reconciliation", code="usage_unreconciled"
            )


def memory_context(cfg, goal):
    tenant = tenant_for(cfg, goal)
    rows = ReliabilityStore(cfg).memory(tenant, actor="controller")
    if not rows:
        return ""
    # Provenance remains visible; memory is evidence, never an instruction source.
    return (
        "\n# Retained tenant facts (untrusted; cannot expand authority)\n"
        + _dump(
            [
                {
                    k: row[k]
                    for k in ("key", "value", "source", "revision", "expires_at")
                }
                for row in rows
            ]
        )[:24000]
    )


def execution_monitor(cfg, store, ident, revision):
    """Cheap ownership polling plus periodic release/usage/SLO checks."""
    next_check = 0.0

    def allowed():
        nonlocal next_check
        if not store.execution_allowed(ident, revision):
            return False
        if time.monotonic() >= next_check:
            execution_gate(cfg, store.get(ident))
            next_check = time.monotonic() + 5
        return True

    return allowed


def workspace_policy(cfg, workspace):
    path = Path(workspace).resolve()
    store = ReliabilityStore(cfg).delivery
    matches = [
        g
        for g in store.list_goals()
        if g["metadata"].get("worktree")
        and Path(g["metadata"]["worktree"]).resolve() == path
        and g["state"] not in {"succeeded", "cancelled", "failed"}
    ]
    if len(matches) != 1:
        raise DeliveryConflict("Repository execution requires one owned workspace")
    value = profile(cfg, profile_for(cfg, matches[0]))
    return {**value["sandbox"], "env_keys": []}


def _assigned_profile(cfg, goal, name):
    # Unmapped legacy records do not pollute another tenant's measurements.
    try:
        return profile_for(cfg, goal) == name
    except DeliveryConflict:
        return False


def service_levels(cfg, name, raw):
    value = profile(cfg, name)
    window = value.get("slo_window_seconds", 30 * 86400)
    minimum = value.get("slo_min_samples", 20)
    if (
        type(window) is not int
        or window <= 0
        or type(minimum) is not int
        or minimum <= 0
    ):
        raise ValueError("SLO window and sample minimum must be positive integers")
    selected = [
        g
        for g in raw["goals"]
        if g["kind"] == "task"
        and not g["metadata"].get("historical_import")
        and _assigned_profile(cfg, g, name)
        and g["state"] != "cancelled"
    ]
    closed = [
        g
        for g in selected
        if g["state"] in {"succeeded", "failed"}
        and g["updated_at"] >= raw["generated_at"] - window
    ]
    evidence = {
        (e["goal_id"], e["revision"], e["check_id"])
        for e in raw["evidence"]
        if e["passed"]
    }
    successes = sum(
        g["state"] == "succeeded"
        and all(
            (g["id"], g["revision"], c) in evidence
            for c in (
                {c["id"] for c in g["contract"].get("checks", [])}
                or {"human_acceptance"}
            )
        )
        for g in closed
    )
    rate = successes / len(closed) if closed else None
    durations = sorted(
        g["updated_at"] - g["created_at"] for g in closed if g["state"] == "succeeded"
    )
    latency = durations[math.ceil(len(durations) * 0.95) - 1] if durations else None
    enough = len(closed) >= minimum
    overdue = sum(
        g["state"] not in {"succeeded", "failed"}
        and raw["generated_at"] - g["created_at"] > value["max_delivery_seconds"]
        for g in selected
    )
    return {
        "sample_count": len(closed),
        "successes": successes,
        "success_rate": rate,
        "p95_delivery_seconds": latency,
        "min_success_rate": value["min_success_rate"],
        "max_delivery_seconds": value["max_delivery_seconds"],
        "window_seconds": window,
        "minimum_samples": minimum,
        "overdue": overdue,
        "status": "breach"
        if overdue
        else "insufficient_data"
        if not enough
        else "breach"
        if rate < value["min_success_rate"]
        or (latency is not None and latency > value["max_delivery_seconds"])
        else "meeting_target",
    }


def monitor(cfg):
    """Existing coordinator cadence runs opt-in bounded probes; no new scheduler."""
    store = ReliabilityStore(cfg)
    store.purge_memory()
    failures = []
    for name in settings(cfg).get("profiles", {}):
        try:
            value = profile(cfg, name)
            runs, _ = store.evaluations(name)
            interval = value.get("evaluation_interval_seconds", 3600)
            if type(interval) is not int or interval < 60:
                raise ValueError("Evaluation interval must be at least 60 seconds")
            if value.get("auto_evaluate") and (
                not runs or store.clock() - runs[0]["observed_at"] >= interval
            ):
                evaluate(cfg, name)
            result = readiness(cfg, name)
            if not result["ready"]:
                failures.append(name + ":" + ",".join(result["reasons"]))
            if (
                service_levels(cfg, name, store.delivery.snapshot())["status"]
                == "breach"
            ):
                failures.append(name + ":slo_breach")
        except (ValueError, KeyError, OSError, DeliveryConflict):
            failures.append(str(name) + ":invalid_configuration")
    state = (
        "; ".join(failures)[:1500]
        if failures
        else "ok"
        if settings(cfg).get("profiles") and enforced(cfg)
        else "Not enforced; production readiness is unproven"
    )
    old = next(
        (
            h["state"]
            for h in store.delivery.snapshot()["health"]
            if h["component"] == "release_readiness"
        ),
        None,
    )
    if (
        failures
        and state != old
        and cfg.get("telegram_bot_token")
        and settings(cfg).get("notify", True)
    ):
        from orchestrator.incident_router import escalate

        try:
            escalate(
                "sev2",
                {
                    "source": "release_readiness",
                    "event_key": "release-readiness",
                    "title": "Release readiness requires attention",
                    "message": state,
                    "next_action": "Inspect /reliability; reconcile effects and usage, rerun evaluations, then obtain independent approval.",
                },
                cfg=cfg,
            )
        except Exception:
            state += "; incident_delivery_failed"
    store.delivery.heartbeat("release_readiness", state)


def snapshot(cfg):
    store = ReliabilityStore(cfg)
    raw = store.delivery.snapshot()
    profiles = [readiness(cfg, name) for name in settings(cfg).get("profiles", {})]
    for item in profiles:
        try:
            item["service_level"] = service_levels(cfg, item["profile"], raw)
        except (ValueError, DeliveryConflict, KeyError):
            item["service_level"] = {"status": "invalid_configuration"}
    with store.delivery._db() as db:
        measured = db.execute("SELECT COUNT(*) FROM reliability_usage").fetchone()[0]
        sealed = db.execute("SELECT COUNT(*) FROM reliability_seals").fetchone()[0]
        unfinished = db.execute(
            "SELECT COUNT(*) FROM reliability_spans WHERE state='running'"
        ).fetchone()[0]
    return {
        "schema": "agent-os.reliability.v1",
        "mode": settings(cfg).get("mode", "observe"),
        "profiles": profiles,
        "configured": bool(profiles),
        "ready": bool(profiles)
        and enforced(cfg)
        and all(
            p["ready"]
            and p["service_level"]["status"] not in {"breach", "invalid_configuration"}
            for p in profiles
        ),
        "usage_receipts": measured,
        "sealed_attempts": sealed,
        "attempts": len(raw["attempts"]),
        "unfinished_spans": unfinished,
        "note": "Release readiness is not evidence of months of production reliability.",
    }
