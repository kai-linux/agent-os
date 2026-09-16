"""Privacy-filtered observation export for Proof and the operator dashboard."""

from __future__ import annotations

import json
from pathlib import Path

from orchestrator.delivery_store import DeliveryStore, store_path
from orchestrator.privacy import redact_text


def _legacy_summary(cfg):
    path = Path(cfg.get("root_dir", ".")) / "runtime" / "metrics" / "agent_stats.jsonl"
    records, invalid = [], 0
    if path.exists():
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                    if isinstance(record, dict) and not record.get("goal_id"):
                        records.append(record)
                except (ValueError, TypeError):
                    invalid += 1
    return {
        "records": len(records),
        "reported_complete": sum(r.get("status") == "complete" for r in records),
        "first_at": min((str(r.get("timestamp", "")) for r in records), default=None),
        "last_at": max((str(r.get("timestamp", "")) for r in records), default=None),
        "invalid_records": invalid,
        "verified_outcomes": None,
        "note": "Legacy model-quality records exclude some infrastructure failures and cannot establish outcome success.",
    }


def observations(cfg):
    raw = DeliveryStore(store_path(cfg)).snapshot()
    goals, attempts, events, risks = [], [], [], []
    for g in raw["goals"]:
        contract, meta = g["contract"], g["metadata"]
        goals.append(
            {
                key: g[key]
                for key in (
                    "id",
                    "parent_id",
                    "revision",
                    "kind",
                    "state",
                    "created_at",
                    "updated_at",
                )
            }
            | {
                "title": redact_text(g["title"])[:400],
                "reason": redact_text(g["reason"])[:800],
                "parent_revision": meta.get("parent_revision", 1),
                "historical_import": bool(meta.get("historical_import")),
                "issue_url": meta.get("github_issue_url"),
                "deadline": contract.get("deadline"),
                "budget_usd": contract.get("budget_usd"),
                "required_checks": [c["id"] for c in contract.get("checks", [])],
            }
        )
        for risk in contract.get("risks", []):
            risks.append(
                {
                    "goal_id": g["id"],
                    "note": redact_text(str(risk))[:800],
                    "source": "scope_baseline",
                }
            )
    for a in raw["attempts"]:
        result = json.loads(a["result"])
        attempts.append(
            {
                key: a[key]
                for key in (
                    "id",
                    "goal_id",
                    "revision",
                    "worker",
                    "state",
                    "started_at",
                    "finished_at",
                    "lease_until",
                    "reserved_usd",
                    "cost_usd",
                )
            }
            | {
                "result_status": result.get("status"),
                "blocker_code": result.get("blocker_code"),
            }
        )
    for e in raw["events"]:
        payload = json.loads(e["payload"])
        if e["kind"] == "state":
            events.append(
                {
                    "goal_id": e["goal_id"],
                    "revision": e["revision"],
                    "at": e["created_at"],
                    "state": payload["state"],
                }
            )
        if e["kind"] == "memory" and payload.get("category") == "risk":
            risks.append(
                {
                    "goal_id": e["goal_id"],
                    "note": redact_text(payload.get("note", ""))[:800],
                    "source": "reported",
                }
            )
    return {
        "schema": "proof.observations.v1",
        "observed_at": raw["generated_at"],
        "goals": goals,
        "attempts": attempts,
        "evidence": [
            {
                key: e[key]
                for key in (
                    "goal_id",
                    "revision",
                    "check_id",
                    "passed",
                    "evaluator",
                    "observed_at",
                )
            }
            for e in raw["evidence"]
        ],
        "dependencies": raw["dependencies"],
        "events": events,
        "risks": risks,
        "notifications": {
            "pending": raw["pending_notifications"],
            "oldest_pending_at": raw["oldest_pending_notification"],
        },
        "uncertain_actions": raw["uncertain_actions"],
        "legacy": _legacy_summary(cfg),
        "health": raw["health"],
    }


def operational_snapshot(cfg):
    from proof.operations import summarize_operations

    return summarize_operations(observations(cfg))
