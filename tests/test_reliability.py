import json
import sqlite3
import subprocess
import sys
from argparse import Namespace

import pytest

from orchestrator.delivery import command, begin_worker, finish_worker
from orchestrator.delivery_store import DeliveryConflict
from orchestrator.model_gateway import call_model
from orchestrator.reliability import (
    SCENARIOS,
    approve_release,
    evaluate,
    execution_gate,
    fingerprint,
    readiness,
    snapshot,
)
from orchestrator.reliability_ops import backup, restore_copy, command as ops_command
from orchestrator.reliability_store import ReliabilityStore
from orchestrator.worker_isolation import bounded_command, sandbox_command


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    evaluator = tmp_path / "evaluate.py"
    evaluator.write_text(
        "import json\nprint("
        + repr(
            json.dumps(
                {
                    "quality": 1.0,
                    "samples": 6,
                    "scenarios": {s: True for s in SCENARIOS},
                }
            )
        )
        + ")\n"
    )
    runbook = tmp_path / "runbook.md"
    runbook.write_text(
        "Stop execution, preserve receipts, restore into quarantine, reconcile, approve."
    )
    monkeypatch.setattr("orchestrator.reliability.deployed_revision", lambda: "a" * 40)
    return {
        "root_dir": str(tmp_path),
        "model_adapters": {
            "worker": {"tenants": ["acme"], "argv": [sys.executable, "-c", "pass"]}
        },
        "reliability": {
            "mode": "observe",
            "tenants": {
                "acme": {
                    "repos": ["owner/app"],
                    "profiles": {"implementation": "coding"},
                },
                "other": {"repos": ["owner/other"], "profiles": {}},
            },
            "principals": {
                "alice": {
                    "tenants": ["acme"],
                    "roles": ["operator", "memory", "billing"],
                },
                "reviewer": {"tenants": ["acme"], "roles": ["approver"]},
            },
            "profiles": {
                "coding": {
                    "release": "a" * 40,
                    "python": sys.executable,
                    "model_adapter": "worker",
                    "evaluator": str(evaluator),
                    "rollback_plan": str(runbook),
                    "incident_playbook": str(runbook),
                    "min_samples": 6,
                    "min_quality": 0.9,
                    "max_quality_drop": 0.05,
                    "min_success_rate": 0.9,
                    "max_delivery_seconds": 3600,
                    "memory_ttl_seconds": 86400,
                    "evaluation_ttl_seconds": 3600,
                    "scenarios": sorted(SCENARIOS),
                    "sandbox": {"backend": "bubblewrap", "network": "none"},
                }
            },
        },
    }


def make_goal(cfg, source="source", repo="owner/app"):
    records = ReliabilityStore(cfg)
    goal = records.delivery.upsert(
        source,
        "Goal",
        "Human intent",
        metadata={
            "github_repo": repo,
            "task_type": "implementation",
            "workspace": cfg["root_dir"],
        },
    )
    return records, goal


def test_measured_worker_executes_isolated_tools_and_seals_usage(cfg, tmp_path):
    from orchestrator.queue import run_agent, parse_agent_result
    from orchestrator.delivery_checks import verify_goal

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    require_isolation(workspace)
    (workspace / ".git").write_text("gitdir: /private/controller/git")
    provider = tmp_path / "worker_adapter.py"
    tool = (
        "from pathlib import Path\nassert not Path('../runtime').exists()\n"
        "try:\n Path('.git').write_text('redirect')\nexcept OSError:\n pass\n"
        "else:\n raise AssertionError('Git metadata was writable')\n"
        "Path('artifact').write_text('verified result')"
    )
    provider.write_text(
        "import json,sys\n"
        "request=json.load(sys.stdin)\n"
        "messages=request['request']['messages']\n"
        "first=len(messages)==2\n"
        "output={'tool_calls':[{'argv':['/usr/bin/python3','-c',"
        + repr(tool)
        + "]}]} if first else {'final':{'status':'complete','summary':'Artifact ready','blocker_code':'none'}}\n"
        "usage=" + repr(receipt()) + "\n"
        "usage['request_id']=request['attempt_id']+str(len(messages))\n"
        "print(json.dumps({'output':output,'usage':usage}))\n"
    )
    cfg["model_adapters"]["worker"]["argv"] = [sys.executable, str(provider)]
    cfg["reliability"]["profiles"]["coding"]["artifacts"] = [str(provider)]
    cfg["reliability"]["mode"] = "enforce"
    run = evaluate(cfg, "coding", actor="alice")
    approve_release(
        cfg, "acme", "reviewer", "coding", run, "Independent scenario review"
    )
    records = ReliabilityStore(cfg)
    goal = records.delivery.upsert(
        "qualified",
        "Qualified worker",
        "Write an artifact",
        contract={
            "checks": [
                {
                    "id": "artifact",
                    "type": "file",
                    "path": "artifact",
                    "contains": ["verified result"],
                }
            ]
        },
        metadata={
            "github_repo": "owner/app",
            "workspace": str(workspace),
            "task_type": "implementation",
        },
    )
    meta = {
        "goal_id": goal["id"],
        "goal_revision": 1,
        "task_id": "qualified",
        "branch": "fixture",
    }
    attempt = begin_worker(cfg, meta, "worker", "fixture", 1, workspace)
    prompt = tmp_path / "prompt"
    prompt.write_text("Write artifact, then report completion.")
    run_agent(
        "no-legacy-cli",
        workspace,
        prompt,
        tmp_path / "log",
        1,
        tmp_path,
        tmp_path / "summary",
        delivery_meta=meta,
        delivery_cfg=cfg,
    )
    result = parse_agent_result(workspace)
    assert result["status"] == "complete"
    finish_worker(cfg, meta, attempt, result)
    assert verify_goal(records.delivery, goal["id"], cfg)
    assert records.delivery.snapshot()["attempts"][0]["cost_usd"] == pytest.approx(
        2400 / 1e9
    )
    spans = records.traces(goal["id"])
    assert sum(s["kind"] == "model" for s in spans) == 2
    assert sum(s["kind"] == "tool" for s in spans) == 1
    assert all(s["state"] == "ok" for s in spans)
    assert (workspace / ".git").read_text() == "gitdir: /private/controller/git"


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "oversized"])
def test_worker_handoff_cannot_access_host_files(tmp_path, kind):
    import os
    from orchestrator.queue import _write_result_contract, parse_agent_result

    outside = tmp_path / "host-private"
    outside.write_text("private sentinel")
    work = tmp_path / "worker"
    work.mkdir()
    result = work / ".agent_result.md"
    if kind == "symlink":
        result.symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, result)
    elif kind == "fifo":
        os.mkfifo(result)
    else:
        result.write_text("x" * (1024 * 1024 + 1))
    parsed = parse_agent_result(work)
    assert parsed["status"] == "blocked"
    assert "private sentinel" not in str(parsed)
    if kind != "oversized":
        with pytest.raises((OSError, ValueError)):
            _write_result_contract(
                work, {"status": "complete", "summary": "Not allowed"}
            )
    assert outside.read_text() == "private sentinel"


def test_evaluator_cannot_approve_own_release(cfg):
    cfg["reliability"]["principals"]["alice"]["roles"].append("approver")
    run = evaluate(cfg, "coding", actor="alice")
    with pytest.raises(DeliveryConflict, match="other than the evaluator"):
        approve_release(cfg, "acme", "alice", "coding", run, "Self approval")


def test_enforcement_disables_legacy_autonomy_and_checks_adapter_tenant(cfg):
    from orchestrator.repo_modes import repo_automation_mode

    cfg["reliability"]["mode"] = "enforce"
    cfg["automation_mode"] = "full"
    assert repo_automation_mode(cfg, "owner/app") == "dispatcher_only"
    cfg["model_adapters"]["worker"]["tenants"] = ["other"]
    assert "measured_worker_adapter_missing" in readiness(cfg, "coding")["reasons"]


def test_qualified_routing_does_not_require_or_mislabel_legacy_cli(cfg, monkeypatch):
    from orchestrator.queue import get_agent_chain, get_next_agent

    cfg["reliability"]["mode"] = "enforce"
    records, goal = make_goal(cfg)
    monkeypatch.setattr(
        "orchestrator.queue.agent_available",
        lambda _: pytest.fail("Legacy CLI probe was called"),
    )
    meta = {"goal_id": goal["id"], "agent": "codex"}
    assert get_agent_chain(meta, cfg) == ["worker"]
    assert get_next_agent(meta, cfg, ["worker"]) is None


def test_tenants_cannot_share_approval_profiles(cfg):
    from orchestrator.reliability import profile

    cfg["reliability"]["tenants"]["other"]["profiles"] = {"implementation": "coding"}
    with pytest.raises(ValueError, match="tenant-specific"):
        profile(cfg, "coding")


def test_unknown_prior_usage_blocks_new_work_until_reconciled(cfg):
    cfg["reliability"]["mode"] = "enforce"
    run = evaluate(cfg, "coding")
    approve_release(cfg, "acme", "reviewer", "coding", run, "Reviewed")
    records, goal = make_goal(cfg)
    records.delivery.begin_attempt(goal["id"], 1, "a", "fixture")
    records.delivery.finish_attempt("a", {"status": "blocked"})
    with pytest.raises(DeliveryConflict, match="usage needs reconciliation"):
        execution_gate(cfg, goal)
    key = records.usage("a", receipt(), actor="billing")
    records.seal_usage("a", [key], actor="billing")
    execution_gate(cfg, goal)


def test_service_targets_count_verified_outcomes_and_overdue_work(cfg):
    from orchestrator.reliability import service_levels

    records, goal = make_goal(cfg)
    assert (
        service_levels(cfg, "coding", records.delivery.snapshot())["status"]
        == "insufficient_data"
    )
    cfg["reliability"]["profiles"]["coding"]["slo_min_samples"] = 1
    records.delivery.record_evidence(
        goal["id"], 1, "human_acceptance", True, "reviewer", {}
    )
    assert records.delivery.verify(goal["id"])
    assert (
        service_levels(cfg, "coding", records.delivery.snapshot())["status"]
        == "meeting_target"
    )
    _, waiting = make_goal(cfg, "waiting")
    raw = records.delivery.snapshot()
    raw["generated_at"] += 4000
    report = service_levels(cfg, "coding", raw)
    assert report["overdue"] == 1 and report["status"] == "breach"


def test_service_breach_blocks_new_goals_without_deadlocking_existing_work(cfg):
    cfg["reliability"]["mode"] = "enforce"
    run = evaluate(cfg, "coding")
    approve_release(cfg, "acme", "reviewer", "coding", run, "Reviewed")
    records, goal = make_goal(cfg)
    records.delivery.begin_attempt(goal["id"], 1, "a", "fixture")
    with records.delivery._db() as db:
        db.execute(
            "UPDATE goals SET created_at=created_at-4000 WHERE id=?", (goal["id"],)
        )
    execution_gate(cfg, records.delivery.get(goal["id"]))
    _, new = make_goal(cfg, "new-work")
    with pytest.raises(DeliveryConflict, match="error budget"):
        execution_gate(cfg, new)
    assert snapshot(cfg)["ready"] is False


def test_cross_tenant_dependency_is_not_attached(cfg):
    from orchestrator.delivery_contract import register_issue

    cfg["reliability"]["mode"] = "enforce"
    records, foreign = make_goal(cfg, "other", "owner/other")
    issue = {
        "number": 1,
        "title": "Cross-tenant dependency",
        "labels": [],
        "body": "## Delivery Contract\n```yaml\ndepends_on: ["
        + foreign["id"]
        + "]\n```",
    }
    with pytest.raises(DeliveryConflict, match="another tenant"):
        register_issue(
            cfg,
            "project",
            {"github_repo": "owner/app", "local_repo": cfg["root_dir"]},
            issue,
            "implementation",
        )
    assert not records.delivery.snapshot()["dependencies"]


def test_continuous_monitor_records_and_routes_failed_evidence(cfg, monkeypatch):
    from orchestrator.reliability import monitor

    calls = []
    cfg["telegram_bot_token"] = "non-secret-test-placeholder"
    monkeypatch.setattr(
        "orchestrator.incident_router.escalate", lambda *a, **kw: calls.append(a)
    )
    monitor(cfg)
    monitor(cfg)
    assert len(calls) == 1
    health = ReliabilityStore(cfg).delivery.snapshot()["health"]
    assert any(
        h["component"] == "release_readiness" and "not_evaluated" in h["state"]
        for h in health
    )


def receipt(**changes):
    return {
        "provider": "test-provider",
        "account": "test-account",
        "request_id": "req-one",
        "model": "test-model",
        "input_tokens": 10,
        "output_tokens": 2,
        "cost_nano_usd": 1200,
        "final": True,
        **changes,
    }


def test_rbac_prevents_cross_tenant_and_self_acceptance(cfg):
    records, goal = make_goal(cfg)
    _, other = make_goal(cfg, "other", "owner/other")
    with pytest.raises(DeliveryConflict):
        command(cfg, ["accept", goal["id"], "Looks good"], actor="alice")
    with pytest.raises(DeliveryConflict):
        command(cfg, ["pause", other["id"]], actor="alice")
    assert other["id"] not in command(cfg, ["list"], actor="alice")
    command(cfg, ["accept", goal["id"], "Independently reviewed"], actor="reviewer")
    assert records.delivery.get(goal["id"])["state"] == "succeeded"


def test_memory_is_scoped_sourced_expiring_and_compare_and_swap(cfg):
    now = [10.0]
    records = ReliabilityStore(cfg, clock=lambda: now[0])
    records.put_memory(
        "acme",
        "fact",
        "Tenant fact",
        source="source-record",
        actor="alice",
        ttl_seconds=10,
        expected_revision=0,
    )
    assert records.memory("other", actor="bob") == []
    assert records.memory("acme", actor="alice")[0]["source"] == "source-record"
    with pytest.raises(DeliveryConflict):
        records.put_memory(
            "acme",
            "fact",
            "Overwrite",
            source="source-record",
            actor="alice",
            ttl_seconds=10,
            expected_revision=0,
        )
    now[0] = 21
    assert records.memory("acme", actor="alice") == []
    with records.delivery._db() as db:
        assert db.execute("SELECT COUNT(*) FROM reliability_memory").fetchone()[0] == 0


def test_memory_interface_requires_its_own_role(cfg):
    args = Namespace(action="memory-list", tenant="acme")
    with pytest.raises(DeliveryConflict):
        ops_command(cfg, args, "reviewer")
    assert ops_command(cfg, args, "alice") == []


def test_usage_is_idempotent_scoped_and_requires_complete_seal(cfg):
    records, goal = make_goal(cfg)
    records.delivery.begin_attempt(goal["id"], 1, "a", "test")
    records.delivery.finish_attempt("a", {"status": "complete"})
    key = records.usage("a", receipt(), actor="billing")
    assert records.usage("a", receipt(), actor="billing") == key
    assert records.delivery.snapshot()["attempts"][0]["cost_usd"] is None
    second = records.usage(
        "a",
        receipt(request_id="req-two", final=False, cost_nano_usd=None),
        actor="billing",
    )
    with pytest.raises(DeliveryConflict):
        records.seal_usage("a", [key], actor="billing")
    with pytest.raises(DeliveryConflict):
        records.seal_usage("a", [key, second], actor="billing")
    records.usage(
        "a", receipt(request_id="req-two", cost_nano_usd=3000), actor="billing"
    )
    records.seal_usage("a", [key, second], actor="billing")
    assert records.delivery.snapshot()["attempts"][0]["cost_usd"] == pytest.approx(
        4200 / 1e9
    )
    with pytest.raises(DeliveryConflict):
        records.usage("a", receipt(request_id="req-three"), actor="billing")
    with pytest.raises(DeliveryConflict):
        records.usage("a", receipt(cost_nano_usd=999), actor="billing")
    _, other = make_goal(cfg, "other", "owner/other")
    records.delivery.begin_attempt(other["id"], 1, "b", "test")
    with pytest.raises(DeliveryConflict):
        records.usage("b", receipt(), actor="billing")


@pytest.mark.parametrize("invalid", [-1, True, float("nan"), 1.5])
def test_usage_rejects_non_measured_values(cfg, invalid):
    records, goal = make_goal(cfg)
    records.delivery.begin_attempt(goal["id"], 1, "a", "test")
    with pytest.raises(ValueError):
        records.usage("a", receipt(cost_nano_usd=invalid), actor="billing")


def test_trace_privacy_parent_scope_and_error_completion(cfg):
    records, goal = make_goal(cfg)
    parent = records.start_span(goal["id"], 1, "worker")
    with pytest.raises(ValueError):
        records.start_span(goal["id"], 1, "model", attributes={"prompt": "private"})
    _, other = make_goal(cfg, "other", "owner/other")
    with pytest.raises(DeliveryConflict):
        records.start_span(other["id"], 1, "action", parent=parent)
    with pytest.raises(RuntimeError):
        with records.span(goal, "model", parent=parent):
            raise RuntimeError("private provider output")
    trace = records.traces(goal["id"])
    assert trace[-1]["state"] == "error"
    assert "private" not in json.dumps(trace)


def test_worker_path_emits_real_attempt_spans(cfg, tmp_path):
    records, goal = make_goal(cfg)
    meta = {
        "goal_id": goal["id"],
        "goal_revision": 1,
        "task_id": "task",
        "branch": "test",
    }
    key = begin_worker(cfg, meta, "worker", "test", 1, tmp_path)
    finish_worker(cfg, meta, key, {"status": "complete"})
    span = records.traces(goal["id"])[0]
    assert span["attributes"]["attempt_id"] == key
    assert span["finished_at"] >= span["started_at"]
    assert span["state"] == "ok"


def test_release_requires_current_success_approval_and_freshness(cfg):
    cfg["reliability"]["mode"] = "enforce"
    records, goal = make_goal(cfg)
    with pytest.raises(DeliveryConflict):
        execution_gate(cfg, goal)
    run = evaluate(cfg, "coding")
    with pytest.raises(DeliveryConflict):
        approve_release(cfg, "acme", "alice", "coding", run, "Self approval")
    approve_release(
        cfg, "acme", "reviewer", "coding", run, "Reviewed tests and recovery"
    )
    assert readiness(cfg, "coding")["ready"]
    execution_gate(cfg, goal)
    records.evaluation(
        "coding",
        fingerprint(cfg, "coding"),
        {"quality": 0.91, "samples": 6, "scenarios": {}},
        True,
    )
    assert "quality_drift" in readiness(cfg, "coding")["reasons"]
    records.evaluation(
        "coding", fingerprint(cfg, "coding"), {"error": "TimeoutExpired"}, False
    )
    assert "evaluation_failed" in readiness(cfg, "coding")["reasons"]
    with pytest.raises(DeliveryConflict):
        approve_release(
            cfg, "acme", "reviewer", "coding", run, "Use old passing result"
        )


def test_changed_artifact_invalidates_approval(cfg):
    run = evaluate(cfg, "coding")
    approve_release(cfg, "acme", "reviewer", "coding", run, "Reviewed")
    from pathlib import Path

    Path(cfg["reliability"]["profiles"]["coding"]["incident_playbook"]).write_text(
        "Changed recovery"
    )
    assert not readiness(cfg, "coding")["ready"]


def test_expired_evidence_and_wrong_execution_release_fail_closed(cfg, monkeypatch):
    run = evaluate(cfg, "coding")
    approve_release(cfg, "acme", "reviewer", "coding", run, "Reviewed")
    records = ReliabilityStore(cfg)
    with records.delivery._db() as db:
        db.execute("UPDATE reliability_evaluations SET observed_at=0")
    assert "evaluation_expired" in readiness(cfg, "coding")["reasons"]
    monkeypatch.setattr("orchestrator.reliability.deployed_revision", lambda: "b" * 40)
    assert "execution_release_mismatch" in readiness(cfg, "coding")["reasons"]


def test_restore_is_quarantined_and_cannot_overwrite(cfg, tmp_path):
    records, goal = make_goal(cfg)
    records.delivery.begin_attempt(goal["id"], 1, "active", "test")
    path = tmp_path / "backup.sqlite3"
    manifest = backup(cfg, path)
    restored = tmp_path / "restore.sqlite3"
    with pytest.raises(ValueError):
        restore_copy(path, restored, "wrong")
    assert not restored.exists()
    restore_copy(path, restored, manifest["sha256"])
    with sqlite3.connect(restored) as db:
        assert db.execute("SELECT state FROM goals").fetchone()[0] == "paused"
        assert db.execute("SELECT state FROM attempts").fetchone()[0] == "lost"
    assert records.delivery.get(goal["id"])["state"] == "running"
    assert restored.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        restore_copy(path, restored, manifest["sha256"])


def require_isolation(workspace):
    import shutil

    if not shutil.which("bwrap"):
        pytest.skip("bubblewrap must be installed for the OS boundary probe")
    argv, env = sandbox_command(["/bin/true"], workspace, {"backend": "bubblewrap"})
    result = subprocess.run(argv, env=env, capture_output=True, timeout=10)
    if result.returncode and (
        b"No permissions to create" in result.stderr
        or b"Operation not permitted" in result.stderr
    ):
        pytest.skip("Host kernel policy disables unprivileged user namespaces")
    assert result.returncode == 0, result.stderr.decode()


def test_os_isolation_hides_host_files_and_environment(cfg, tmp_path, monkeypatch):
    workspace = tmp_path / "work"
    workspace.mkdir()
    require_isolation(workspace)
    private = tmp_path / "private"
    private.write_text("must not be visible")
    monkeypatch.setenv("CONTROLLER_PRIVATE_VALUE", "must not be inherited")
    code = (
        "import os,pathlib; assert not pathlib.Path("
        + repr(str(private))
        + ").exists(); assert 'CONTROLLER_PRIVATE_VALUE' not in os.environ; pathlib.Path('artifact').write_text('ok')"
    )
    argv, env = sandbox_command(
        ["/usr/bin/python3", "-c", code], workspace, {"backend": "bubblewrap"}
    )
    result = subprocess.run(argv, env=env, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode()
    assert (workspace / "artifact").read_text() == "ok"


def test_sandbox_fails_closed_without_backend(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr("orchestrator.worker_isolation.shutil.which", lambda _: None)
    with pytest.raises(ValueError, match="fallback"):
        sandbox_command(["true"], tmp_path, {"backend": "bubblewrap"})


def test_model_gateway_records_usage_not_prompt_or_output(cfg, tmp_path):
    records, goal = make_goal(cfg)
    records.delivery.begin_attempt(goal["id"], 1, "a", "test")
    records.start_span(goal["id"], 1, "worker", key="attempt:a")
    adapter = tmp_path / "adapter.py"
    adapter.write_text(
        "import json\nprint("
        + repr(json.dumps({"output": "private response", "usage": receipt()}))
        + ")\n"
    )
    cfg["model_adapters"] = {
        "test": {"tenants": ["acme"], "argv": [sys.executable, str(adapter)]}
    }
    response = call_model(cfg, "a", "test", {"prompt": "private prompt"})
    assert response["output"] == "private response"
    assert "private" not in json.dumps(records.traces(goal["id"]))
    assert records.traces(goal["id"])[-1]["parent_id"] == "attempt:a"
    records.delivery.finish_attempt("a", {"status": "complete"})
    records.seal_usage("a", [response["receipt_key"]], actor="billing")
    assert snapshot(cfg)["sealed_attempts"] == 1


def test_bounded_command_kills_timeout_and_rejects_oversize(tmp_path):
    with pytest.raises(subprocess.TimeoutExpired):
        bounded_command(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            cwd=tmp_path,
            timeout=0.1,
        )
    with pytest.raises(ValueError):
        bounded_command(
            [sys.executable, "-c", "print('x'*1000)"], cwd=tmp_path, limit=10
        )
