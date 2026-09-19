"""Provider-neutral measured model calls through operator-owned adapters.

Adapters, not models, return usage metadata. Legacy CLIs without this boundary
remain explicitly unmetered; result-file prose is never a billing source.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from orchestrator.delivery_store import DeliveryConflict
from orchestrator.reliability_store import ReliabilityStore
from orchestrator.worker_isolation import bounded_command


def call_model(cfg, attempt_id, adapter_name, request, *, timeout_seconds=600):
    records = ReliabilityStore(cfg)
    with records.delivery._db() as db:
        row = db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
    if not row or row["state"] != "running":
        raise DeliveryConflict("A model call needs an active controller-owned attempt")
    goal = records.delivery.get(row["goal_id"])
    if not records.delivery.execution_allowed(goal["id"], row["revision"]):
        raise DeliveryConflict("Model call authority has expired")
    from orchestrator.reliability import tenant_for, execution_gate, execution_monitor

    execution_gate(cfg, goal)
    adapter = cfg.get("model_adapters", {}).get(adapter_name, {})
    if tenant_for(cfg, goal) not in adapter.get("tenants", []):
        raise DeliveryConflict("Model adapter is not granted to this tenant")
    argv = adapter.get("argv")
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(x, str) for x in argv)
    ):
        raise ValueError("Model adapters require a fixed argv")
    env = {
        k: os.environ[k]
        for k in ("PATH", "LANG", *adapter.get("env_keys", []))
        if k in os.environ
    }
    encoded = json.dumps(
        {"attempt_id": attempt_id, "request": request}, allow_nan=False
    ).encode()
    if len(encoded) > 2 * 1024 * 1024:
        raise ValueError("Model request exceeds the controller limit")
    parent = "attempt:" + attempt_id
    if not any(s["id"] == parent for s in records.traces(goal["id"])):
        parent = None
    with records.span(
        goal,
        "model",
        parent=parent,
        attributes={"attempt_id": attempt_id, "provider": adapter_name},
    ) as key:
        span = next(s for s in records.traces(goal["id"]) if s["id"] == key)
        encoded = json.dumps(
            {
                "attempt_id": attempt_id,
                "request": request,
                "traceparent": f"00-{span['trace_id']}-{span['span_id']}-01",
            },
            allow_nan=False,
        ).encode()
        response = json.loads(
            bounded_command(
                argv,
                cwd=adapter.get("cwd", str(Path(argv[0]).parent)),
                timeout=min(timeout_seconds, adapter.get("timeout_seconds", 120)),
                env=env,
                input_data=encoded,
                limit=2 * 1024 * 1024,
                allowed=execution_monitor(
                    cfg, records.delivery, goal["id"], row["revision"]
                ),
            )
        )
        if not isinstance(response, dict) or set(response) != {"output", "usage"}:
            raise ValueError("Adapter response requires output and measured usage")
        receipt_key = records.usage(
            attempt_id, response["usage"], actor="adapter:" + adapter_name
        )
        if not records.delivery.execution_allowed(goal["id"], row["revision"]):
            raise DeliveryConflict(
                "Model result arrived after authority expired; usage retained"
            )
        return {"output": response["output"], "receipt_key": receipt_key}
