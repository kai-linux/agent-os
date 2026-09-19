"""Orchestration acceptance scenarios with local fixture providers, not business evals."""

import json
import subprocess
import sys

import pytest

from orchestrator.delivery import begin_worker, finish_worker
from orchestrator.delivery_actions import execute_action
from orchestrator.delivery_checks import verify_goal
from orchestrator.delivery_store import DeliveryConflict
from orchestrator.model_gateway import call_model
from orchestrator.reliability_store import ReliabilityStore
from tests.test_reliability import cfg as cfg


def setup(tmp_path):
    cfg = {"root_dir": str(tmp_path)}
    records = ReliabilityStore(cfg)
    return cfg, records, records.delivery


def test_end_to_end(tmp_path):
    cfg, records, store = setup(tmp_path)
    program = store.upsert("program", "Delivery", "Deliver and review", kind="program")
    checks = {
        "checks": [
            {
                "id": "artifact",
                "type": "file",
                "path": "artifact",
                "contains": ["verified result"],
            }
        ]
    }
    first = store.upsert(
        "research",
        "Research",
        "Write result",
        parent_id=program["id"],
        contract=checks,
        metadata={"workspace": str(tmp_path)},
    )
    second = store.upsert(
        "review",
        "Review",
        "Check result",
        parent_id=program["id"],
        metadata={"workspace": str(tmp_path)},
    )
    store.depend(second["id"], first["id"])
    with pytest.raises(DeliveryConflict):
        store.begin_attempt(second["id"], 1, "too-early", "reviewer")
    meta = {
        "goal_id": first["id"],
        "goal_revision": 1,
        "task_id": "research",
        "branch": "fixture",
    }
    attempt = begin_worker(cfg, meta, "researcher", "fixture", 1, tmp_path)
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; Path('artifact').write_text('verified result')",
        ],
        cwd=tmp_path,
        check=True,
    )
    finish_worker(cfg, meta, attempt, {"status": "complete"})
    assert verify_goal(store, first["id"], cfg)
    store.begin_attempt(second["id"], 1, "review", "reviewer")
    store.finish_attempt("review", {"status": "complete"})
    store.record_evidence(
        second["id"], 1, "human_acceptance", True, "reviewer", {"artifact": "checked"}
    )
    assert store.verify(second["id"])
    assert not store.verify(program["id"])
    store.record_evidence(
        program["id"], 1, "human_acceptance", True, "operator", {"outcome": "accepted"}
    )
    assert store.verify(program["id"])
    assert {s["kind"] for s in records.traces(first["id"])} == {
        "worker",
        "verification",
    }


def test_restart(tmp_path):
    cfg, records, store = setup(tmp_path)
    goal = store.upsert(
        "recover",
        "Recover",
        "Observe before retrying",
        contract={
            "checks": [
                {
                    "id": "artifact",
                    "type": "file",
                    "path": "artifact",
                    "contains": ["real artifact"],
                }
            ]
        },
        metadata={"workspace": str(tmp_path)},
    )
    store.begin_attempt(goal["id"], 1, "old", "fixture", lease_seconds=1)
    (tmp_path / "artifact").write_text("real artifact")
    reopened = ReliabilityStore(cfg, clock=lambda: store.clock() + 10).delivery
    reopened.tick()
    assert reopened.get(goal["id"])["state"] == "waiting"
    assert verify_goal(reopened, goal["id"], cfg)
    assert len(reopened.snapshot()["attempts"]) == 1


def action_fixture(tmp_path, cfg, store, *, allowed=True):
    adapter = tmp_path / "action.py"
    adapter.write_text(
        "import json,pathlib\np=pathlib.Path('effects'); p.write_text(p.read_text()+'1' if p.exists() else '1')\nprint(json.dumps({'receipt':{'id':'fixture-effect'}}))"
    )
    cfg["delivery_actions"] = {
        "fixture": {
            "argv": [sys.executable, str(adapter)],
            "targets": ["local"],
            "input_fields": {},
        }
    }
    goal = store.upsert(
        "action",
        "Action",
        "Approved local fixture",
        contract={
            "allowed_actions": [
                {"capability": "fixture", "target": "local", "max_calls": 1}
            ]
        }
        if allowed
        else {},
        metadata={"workspace": str(tmp_path)},
    )
    return goal, {"capability": "fixture", "target": "local", "input": {}}


def test_duplicate_effect(tmp_path):
    cfg, records, store = setup(tmp_path)
    goal, proposal = action_fixture(tmp_path, cfg, store)
    first = execute_action(cfg, goal["id"], 1, proposal)
    second = execute_action(cfg, goal["id"], 1, proposal)
    assert first == second
    assert (tmp_path / "effects").read_text() == "1"
    assert len(store.list_actions(goal["id"])) == 1


def test_denied_action(tmp_path):
    cfg, records, store = setup(tmp_path)
    goal, proposal = action_fixture(tmp_path, cfg, store, allowed=False)
    with pytest.raises(DeliveryConflict):
        execute_action(cfg, goal["id"], 1, proposal)
    assert not (tmp_path / "effects").exists()
    assert records.traces(goal["id"])[-1]["state"] == "error"


def test_stale_revision(tmp_path):
    cfg, records, store = setup(tmp_path)
    goal, proposal = action_fixture(tmp_path, cfg, store)
    store.control(
        goal["id"],
        "revise",
        actor="operator",
        note="Changed request",
        original="New intent",
        contract={},
    )
    with pytest.raises(DeliveryConflict):
        execute_action(cfg, goal["id"], 1, proposal)
    assert not (tmp_path / "effects").exists()
    assert store.get(goal["id"])["revision"] == 2


def test_provider_failure(tmp_path):
    cfg, records, store = setup(tmp_path)
    goal = store.upsert(
        "fallback",
        "Fallback",
        "Retain identity across providers",
        contract={"max_attempts": 2},
    )
    bad = tmp_path / "bad.py"
    bad.write_text("raise SystemExit(1)")
    good = tmp_path / "good.py"
    payload = {
        "output": "fixture",
        "usage": {
            "provider": "fixture",
            "account": "fixture",
            "request_id": "r",
            "model": "fixture",
            "input_tokens": 1,
            "output_tokens": 1,
            "cost_nano_usd": 1,
            "final": True,
        },
    }
    good.write_text("print(" + repr(json.dumps(payload)) + ")")
    cfg["model_adapters"] = {
        "bad": {"argv": [sys.executable, str(bad)], "tenants": ["local"]},
        "good": {"argv": [sys.executable, str(good)], "tenants": ["local"]},
    }
    meta = {
        "goal_id": goal["id"],
        "goal_revision": 1,
        "task_id": "fallback",
        "branch": "fixture",
    }
    first = begin_worker(cfg, meta, "worker", "bad", 1, tmp_path)
    with pytest.raises(subprocess.CalledProcessError):
        call_model(cfg, first, "bad", {})
    finish_worker(
        cfg,
        meta,
        first,
        {"status": "blocked", "blocker_code": "provider_failure"},
        retry=True,
    )
    second = begin_worker(cfg, meta, "worker", "good", 1, tmp_path)
    assert call_model(cfg, second, "good", {})["output"] == "fixture"
    finish_worker(cfg, meta, second, {"status": "complete"})
    assert len(store.snapshot()["attempts"]) == 2
    assert all(s["goal_id"] == goal["id"] for s in records.traces(goal["id"]))
    assert store.snapshot()["attempts"][0]["cost_usd"] is None


def test_restore(cfg, tmp_path):
    from tests.test_reliability import test_restore_is_quarantined_and_cannot_overwrite

    test_restore_is_quarantined_and_cannot_overwrite(cfg, tmp_path)


def test_isolation(cfg, tmp_path, monkeypatch):
    from tests.test_reliability import (
        test_os_isolation_hides_host_files_and_environment,
    )

    test_os_isolation_hides_host_files_and_environment(cfg, tmp_path, monkeypatch)


def test_approval_expiry(cfg, monkeypatch):
    from tests.test_reliability import (
        test_expired_evidence_and_wrong_execution_release_fail_closed,
    )

    test_expired_evidence_and_wrong_execution_release_fail_closed(cfg, monkeypatch)


def test_qualified_worker(cfg, tmp_path):
    from tests.test_reliability import (
        test_measured_worker_executes_isolated_tools_and_seals_usage,
    )

    test_measured_worker_executes_isolated_tools_and_seals_usage(cfg, tmp_path)
