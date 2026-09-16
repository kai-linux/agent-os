"""Behavioral delivery tests. All external accounts/providers are replaced by fixtures."""

import sys
import time
from pathlib import Path
from threading import Thread

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from orchestrator import delivery, paths, queue
from orchestrator.delivery_actions import execute_action
from orchestrator.delivery_contract import register_issue
from orchestrator.delivery_program import manage_decomposition
from orchestrator.delivery_store import DeliveryConflict, DeliveryStore, store_path


@pytest.mark.parametrize(
    "result_file,real_artifact,expected",
    [(True, True, "succeeded"), (False, True, "succeeded"), (True, False, "ready")],
)
def test_real_mailbox_path_requires_artifact_not_worker_claim(
    tmp_path, monkeypatch, result_file, real_artifact, expected
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cfg = {
        "root_dir": str(tmp_path),
        "mailbox_dir": str(tmp_path / "mailbox"),
        "logs_dir": str(tmp_path / "logs"),
        "worktrees_dir": str(tmp_path / "worktrees"),
        "allowed_repos": [str(workspace)],
        "default_agent": "codex",
        "default_task_type": "research",
        "default_base_branch": "main",
        "default_allow_push": False,
        "max_runtime_minutes": 1,
    }
    store = DeliveryStore(store_path(cfg))
    goal = store.upsert(
        "brief",
        "Deliver brief",
        "Produce a sourced brief, not a plan",
        contract={
            "checks": [
                {
                    "id": "brief",
                    "type": "file",
                    "path": "brief.md",
                    "contains": ["Verified fixture artifact"],
                }
            ]
        },
        metadata={"workspace": str(workspace)},
    )
    monkeypatch.setattr(paths, "ROOT", tmp_path)
    monkeypatch.setattr(queue, "load_config", lambda: cfg)
    monkeypatch.setattr("orchestrator.github_sync.load_config", lambda: cfg)
    runtime = paths.runtime_paths(cfg)
    meta = {
        "task_id": "fixture",
        "repo": str(workspace),
        "branch": "agent/fixture",
        "agent": "codex",
        "task_type": "research",
        "goal_id": goal["id"],
        "goal_revision": 1,
    }
    (runtime["INBOX"] / "fixture.md").write_text(
        queue.render_task(meta, "Produce the brief")
    )
    original_tick = delivery.tick
    monkeypatch.setattr(
        delivery, "tick", lambda config: original_tick(config, publish=False)
    )
    monkeypatch.setattr(queue, "flush_outbox", lambda config: None)
    monkeypatch.setattr(queue, "maybe_run_stall_watchdog", lambda *args, **kwargs: None)
    monkeypatch.setattr(queue, "fallback_cooldown_remaining", lambda cfg: 0)
    monkeypatch.setattr(
        queue,
        "get_next_agent",
        lambda meta, cfg, attempts: next(
            (a for a in ["codex", "claude"] if a not in attempts), None
        ),
    )
    monkeypatch.setattr(queue, "ensure_worktree", lambda *args: workspace)
    monkeypatch.setattr(queue, "run_tests", lambda *args: None)
    monkeypatch.setattr(queue, "commit_and_push", lambda *args: False)
    monkeypatch.setattr(queue, "rescue_git_progress", lambda *args: (None, False))
    monkeypatch.setattr(queue, "detect_default_branch", lambda *args: "main")
    monkeypatch.setattr(queue, "update_codebase_memory", lambda *args: None)
    monkeypatch.setattr(queue, "cleanup_worktree", lambda *args: None)
    monkeypatch.setattr(queue, "write_unblock_notes_artifact", lambda *args: None)
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("fixture provider invocation")
    monkeypatch.setattr(queue, "write_prompt", lambda *args, **kwargs: prompt)
    calls = []

    def worker(*args, **kwargs):
        calls.append(args)
        if real_artifact:
            (workspace / "brief.md").write_text("Verified fixture artifact")
        else:
            (workspace / "plan.md").write_text("Plan to write the brief later")
        if result_file:
            queue._write_result_contract(
                workspace,
                {
                    "status": "complete",
                    "blocker_code": "none",
                    "summary": "Fixture worker claims completion",
                },
            )

    monkeypatch.setattr(queue, "run_agent", worker)
    queue.main()
    assert len(calls) == 1
    assert store.get(goal["id"])["state"] == expected
    destination = "DONE" if expected == "succeeded" else "BLOCKED"
    assert (runtime[destination] / "fixture.md").exists()
    assert not list(runtime["PROCESSING"].glob("*.md"))
    assert len(store.snapshot()["attempts"]) == 1
    assert store.snapshot()["pending_notifications"] > 0


def test_monitored_worker_process_stops_when_parent_paused(tmp_path):
    store = DeliveryStore(tmp_path / "db")
    parent = store.upsert("program", "Program", "Program", kind="program")
    goal = store.upsert("work", "Work", "Work", parent_id=parent["id"])
    store.begin_attempt(goal["id"], 1, "attempt", "fixture")
    errors = []
    marker = tmp_path / "started"

    def worker():
        try:
            delivery.run_monitored(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; import time; Path('started').write_text('started'); time.sleep(30)",
                ],
                tmp_path,
                tmp_path / "log",
                timeout_seconds=10,
                store=store,
                ident=goal["id"],
                revision=1,
            )
        except DeliveryConflict as exc:
            errors.append(exc)

    thread = Thread(target=worker)
    thread.start()
    deadline = time.monotonic() + 5
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    store.control(parent["id"], "pause", actor="operator")
    thread.join(timeout=6)
    assert not thread.is_alive()
    assert errors


def test_registered_action_receipt_survives_retry_without_second_effect(tmp_path):
    cfg = {
        "root_dir": str(tmp_path),
        "delivery_actions": {
            "fixture": {
                "argv": [
                    sys.executable,
                    "-c",
                    "import json,sys; from pathlib import Path; p=json.load(sys.stdin); Path('effect').open('a').write('once'); print(json.dumps({'receipt': {'external_id':p['action_id']}}))",
                ],
                "targets": ["fixture-account"],
                "input_fields": {"asset": {"enum": ["fixture"]}},
            }
        },
    }
    store = DeliveryStore(store_path(cfg))
    goal = store.upsert(
        "action",
        "Action",
        "Action",
        metadata={"workspace": str(tmp_path)},
        contract={
            "allowed_actions": [{"capability": "fixture", "target": "fixture-account"}]
        },
    )
    proposal = {
        "capability": "fixture",
        "target": "fixture-account",
        "input": {"asset": "fixture"},
    }
    receipt = execute_action(cfg, goal["id"], 1, proposal)
    assert execute_action(cfg, goal["id"], 1, proposal) == receipt
    assert (tmp_path / "effect").read_text() == "once"
    assert store.snapshot()["uncertain_actions"] == 0
    with pytest.raises(DeliveryConflict):
        execute_action(cfg, goal["id"], 1, {**proposal, "target": "not-delegated"})


def test_source_edits_pause_and_require_explicit_revision(tmp_path, monkeypatch):
    cfg = {"root_dir": str(tmp_path)}
    repo = {"github_repo": "owner/repo", "local_repo": str(tmp_path)}
    issue = {
        "title": "Original",
        "number": 1,
        "body": "Original scope",
        "state": "OPEN",
    }
    goal = register_issue(cfg, "p", repo, issue, "research")
    monkeypatch.setattr(
        delivery, "gh_json", lambda *args: {**issue, "body": "Revised scope"}
    )
    monkeypatch.setattr(delivery, "flush_outbox", lambda *args: None)
    delivery.tick(cfg)
    store = DeliveryStore(store_path(cfg))
    assert store.get(goal["id"])["state"] == "paused"
    assert "Original scope" in store.get(goal["id"])["original"]
    delivery.command(cfg, ["revise", goal["id"]], actor="operator")
    revised = store.get(goal["id"])
    assert revised["revision"] == 2
    assert "Revised scope" in revised["original"]
    assert revised["state"] == "ready"


def test_externally_closed_issue_is_cancelled_not_reopened_or_completed(
    tmp_path, monkeypatch
):
    cfg = {"root_dir": str(tmp_path)}
    issue = {"title": "Original", "number": 1, "body": "Scope", "state": "OPEN"}
    goal = register_issue(
        cfg,
        "p",
        {"github_repo": "owner/repo", "local_repo": str(tmp_path)},
        issue,
        "research",
    )
    monkeypatch.setattr(delivery, "gh_json", lambda *args: {**issue, "state": "CLOSED"})
    monkeypatch.setattr(delivery, "flush_outbox", lambda *args: None)
    delivery.tick(cfg)
    assert DeliveryStore(store_path(cfg)).get(goal["id"])["state"] == "cancelled"


def test_merge_closure_does_not_cancel_outstanding_deployment_checks(
    tmp_path, monkeypatch
):
    cfg = {"root_dir": str(tmp_path)}
    store = DeliveryStore(store_path(cfg))
    goal = store.upsert(
        "deploy",
        "Deploy",
        "Deploy",
        contract={
            "checks": [{"id": "site", "type": "url", "url": "https://example.test"}]
        },
        metadata={
            "github_repo": "owner/repo",
            "github_issue_number": 1,
            "pr_url": "https://github.com/owner/repo/pull/2",
        },
    )
    store.await_verification(goal["id"], 1, "Awaiting deployment")
    monkeypatch.setattr(
        delivery,
        "gh_json",
        lambda *args: {"state": "CLOSED", "stateReason": "COMPLETED"},
    )
    monkeypatch.setattr(
        "orchestrator.delivery_checks.observe",
        lambda check, *args: (check["type"] == "merged_pr", {"reason": "fixture"}),
    )
    monkeypatch.setattr(delivery, "flush_outbox", lambda *args: None)
    delivery.tick(cfg)
    assert store.get(goal["id"])["state"] == "verifying"


def test_plan_is_owned_and_recoverable_across_materialization_retry(
    tmp_path, monkeypatch
):
    import orchestrator.delivery_program as program

    cfg = {
        "root_dir": str(tmp_path),
        "github_owner": "owner",
        "github_projects": {
            "p": {
                "project_number": 1,
                "repos": [{"github_repo": "owner/repo", "local_repo": str(tmp_path)}],
            }
        },
    }
    created = []

    def github(args):
        if args[:2] == ["issue", "create"]:
            issue = {
                "number": len(created) + 2,
                "title": args[args.index("--title") + 1],
                "body": args[args.index("--body") + 1],
            }
            issue["url"] = f"https://github.com/owner/repo/issues/{issue['number']}"
            created.append(issue)
            return issue["url"]
        return ""

    monkeypatch.setattr(program, "gh", github)
    monkeypatch.setattr(program, "gh_json", lambda *args: created)
    parent = {"title": "Program", "number": 1, "body": "Ship the combined outcome"}
    plan = {
        "kind": "program",
        "sub_issues": [
            {
                "key": "a",
                "title": "Research",
                "body": "Research first",
                "task_type": "research",
            },
            {
                "key": "b",
                "title": "Deliver",
                "body": "Then deliver",
                "task_type": "research",
                "depends_on": ["a"],
            },
        ],
    }
    assert len(manage_decomposition(cfg, "owner/repo", parent, plan, "p")) == 1
    assert len(manage_decomposition(cfg, "owner/repo", parent, plan, "p")) == 1
    assert len(created) == 2
    store = DeliveryStore(store_path(cfg))
    goals = store.list_goals()
    assert len(goals) == 3
    assert next(g for g in goals if g["kind"] == "program")["state"] != "succeeded"
    assert next(g for g in goals if g["title"] == "Deliver")["state"] == "waiting"
    assert len(store.snapshot()["dependencies"]) == 1
