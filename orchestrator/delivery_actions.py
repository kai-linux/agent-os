"""Run registered skills with explicit delegation and durable effect receipts.

The agent supplies structured data, never shell text or executable paths. Skills
are operator-installed adapters and must return an externally reconcilable receipt.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from orchestrator.delivery_store import DeliveryConflict, DeliveryStore, store_path


def execute_action(cfg, ident, revision, proposal):
    from orchestrator.reliability_store import ReliabilityStore
    from orchestrator.reliability import execution_gate
    records = ReliabilityStore(cfg)
    goal = records.delivery.get(ident)
    execution_gate(cfg, goal)
    with records.span(goal, "action", parent=records.worker_parent(goal)):
        return _execute_action(cfg, ident, revision, proposal)


def _execute_action(cfg, ident, revision, proposal):
    capability, target = proposal.get("capability"), proposal.get("target")
    adapter = (cfg.get("delivery_actions") or {}).get(capability)
    if not isinstance(adapter, dict):
        raise DeliveryConflict(
            f"Capability {capability!r} is not configured; investigate or install a scoped adapter"
        )
    argv = adapter.get("argv")
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(s, str) for s in argv)
    ):
        raise DeliveryConflict("The registered adapter needs a fixed argument list")
    if target not in adapter.get("targets", []):
        raise DeliveryConflict("Target is outside the adapter's configured authority")
    if not shutil.which(argv[0]):
        raise DeliveryConflict("The configured capability executable is unavailable")
    request = proposal.get("input", {})
    fields = adapter.get("input_fields", {})
    if (
        not isinstance(request, dict)
        or not isinstance(fields, dict)
        or set(request) != set(fields)
    ):
        raise DeliveryConflict(
            "Action inputs must exactly match the configured adapter fields"
        )
    for name, spec in fields.items():
        value = request[name]
        if (
            not isinstance(spec, dict)
            or not isinstance(value, str)
            or len(value) > int(spec.get("max_length", 2000))
        ):
            raise DeliveryConflict("Action input does not match its configured bounds")
        if "enum" in spec and value not in spec["enum"]:
            raise DeliveryConflict("Action input is outside its approved choices")
    store = DeliveryStore(store_path(cfg))
    goal = store.get(ident)
    payload = json.dumps(
        {"capability": capability, "target": target, "input": request}, sort_keys=True
    )
    key = hashlib.sha256(f"{ident}:{revision}:{payload}".encode()).hexdigest()
    action = store.prepare_action(ident, revision, key, capability, target, request)
    if action["state"] == "confirmed":
        return json.loads(action["receipt"])
    if action["state"] == "uncertain":
        raise DeliveryConflict(
            f"Action {key} may already have happened; reconcile its remote receipt before retrying"
        )
    env = {
        key: os.environ[key]
        for key in ("PATH", "LANG", *adapter.get("env_keys", []))
        if key in os.environ
    }
    payload = json.dumps(
        {
            "action_id": key,
            "goal_id": ident,
            "revision": revision,
            "target": target,
            "input": request,
        }
    ).encode()
    with tempfile.TemporaryFile() as input_file, tempfile.TemporaryFile() as output:
        input_file.write(payload)
        input_file.seek(0)
        proc = subprocess.Popen(
            argv,
            stdin=input_file,
            stdout=output,
            stderr=subprocess.DEVNULL,
            cwd=goal["metadata"].get("worktree") or goal["metadata"]["workspace"],
            env=env,
            start_new_session=True,
        )
        deadline = time.monotonic() + min(300, int(adapter.get("timeout_seconds", 120)))
        try:
            while proc.poll() is None:
                if (
                    not store.execution_allowed(ident, revision)
                    or time.monotonic() >= deadline
                    or output.tell() > 65536
                ):
                    raise DeliveryConflict(
                        f"Action {key} stopped with an uncertain result; reconcile before retrying"
                    )
                time.sleep(0.1)
            output.seek(0)
            result = output.read(65537)
            if proc.returncode or len(result) > 65536:
                raise DeliveryConflict(
                    f"Action {key} has an uncertain result; do not repeat without reconciliation"
                )
            response = json.loads(result)
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    receipt = response.get("receipt") if isinstance(response, dict) else None
    if not isinstance(receipt, dict) or not receipt:
        raise DeliveryConflict(f"Action {key} returned no durable receipt")
    store.confirm_action(key, receipt)
    return receipt


def run_proposals(cfg, meta, worktree):
    path = Path(worktree) / ".agent_actions.json"
    if not path.exists():
        return []
    if path.is_symlink() or path.stat().st_size > 65536:
        raise DeliveryConflict("Invalid action proposal file")
    proposals = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(proposals, list) or len(proposals) > 8:
        raise DeliveryConflict(
            "An execution step may propose at most eight registered actions"
        )
    return [
        execute_action(cfg, meta["goal_id"], meta["goal_revision"], proposal)
        for proposal in proposals
    ]


def capability_catalog(cfg):
    """Expose invocation schemas, not executable paths, credentials or environment."""
    return {
        name: {
            key: adapter[key]
            for key in ("description", "targets", "input_fields")
            if key in adapter
        }
        for name, adapter in (cfg.get("delivery_actions") or {}).items()
        if isinstance(adapter, dict)
    }
