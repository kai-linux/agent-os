import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from orchestrator.delivery import command, flush_outbox, settle_result, tick
from orchestrator.delivery_checks import observe, verify_goal
from orchestrator.delivery_contract import parse_contract, register_issue
from orchestrator.delivery_program import validate_plan
from orchestrator.delivery_store import DeliveryStore, store_path


@pytest.fixture
def setup(tmp_path):
    cfg = {"root_dir": str(tmp_path), "mailbox_dir": str(tmp_path / "mailbox")}
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = DeliveryStore(store_path(cfg))
    return cfg, store, workspace


def test_issue_355_merged_without_new_diff_is_verified(setup, monkeypatch):
    cfg, store, workspace = setup
    goal = register_issue(
        cfg,
        "p",
        {"github_repo": "owner/repo", "local_repo": str(workspace)},
        {
            "number": 355,
            "title": "Rename the README title",
            "body": "",
            "url": "https://github.com/owner/repo/issues/355",
        },
        "implementation",
    )
    monkeypatch.setattr(
        "orchestrator.delivery_checks.gh_json",
        lambda *a: [
            {
                "url": "https://github.com/owner/repo/pull/356",
                "mergedAt": "2026-09-01T12:49:20Z",
                "mergeCommit": {"oid": "abc"},
                "baseRefName": "main",
                "closingIssuesReferences": [{"number": 355}],
            }
        ],
    )
    result = settle_result(
        cfg,
        {"goal_id": goal["id"], "goal_revision": 1},
        {
            "status": "partial",
            "blocker_code": "no_diff_produced",
            "summary": "No new diff",
        },
    )
    assert result["status"] == "complete"
    assert result["delivery_state"] == "succeeded"
    assert store.get(goal["id"])["state"] == "succeeded"


def test_closed_issue_or_unrelated_pr_is_not_evidence(setup, monkeypatch):
    cfg, store, workspace = setup
    goal = register_issue(
        cfg,
        "p",
        {"github_repo": "owner/repo", "local_repo": str(workspace)},
        {"number": 1, "title": "Deliver", "body": "", "state": "CLOSED"},
        "implementation",
    )
    monkeypatch.setattr(
        "orchestrator.delivery_checks.gh_json",
        lambda *a: [
            {
                "mergedAt": "today",
                "mergeCommit": {"oid": "abc"},
                "baseRefName": "main",
                "closingIssuesReferences": [{"number": 2}],
            }
        ],
    )
    assert not verify_goal(store, goal["id"], cfg)


def test_research_deliverable_verified_and_preserved_independently(setup):
    cfg, store, workspace = setup
    check = {
        "id": "brief",
        "type": "file",
        "path": "brief.md",
        "contains": ["Sources", "Limitations"],
    }
    goal = store.upsert(
        "research",
        "Research brief",
        "Produce a brief",
        contract={"checks": [check]},
        metadata={"workspace": str(workspace)},
    )
    (workspace / "brief.md").write_text("A plan to do research later")
    assert not verify_goal(store, goal["id"], cfg)
    (workspace / "brief.md").write_text(
        "Sources\nTest fixture source\nLimitations\nTest fixture limitation"
    )
    assert verify_goal(store, goal["id"], cfg)
    evidence = json.loads(store.snapshot()["evidence"][0]["detail"])
    assert Path(evidence["artifact"]).read_text().startswith("Sources")


def test_file_verifier_rejects_escape_and_symlink(setup):
    cfg, store, workspace = setup
    outside = workspace.parent / "private"
    outside.write_text("must not be copied")
    (workspace / "link").symlink_to(outside)
    goal = store.upsert("file", "File", "File", metadata={"workspace": str(workspace)})
    for name in ("../private", "link"):
        assert observe({"type": "file", "path": name}, goal, cfg)[0] is False


def test_noncode_task_without_evidence_waits_for_acceptance(setup):
    cfg, store, workspace = setup
    goal = store.upsert(
        "media",
        "Recording",
        "Deliver recording",
        metadata={"workspace": str(workspace)},
    )
    result = settle_result(
        cfg,
        {"goal_id": goal["id"], "goal_revision": 1},
        {"status": "complete", "summary": "Wrote a script"},
    )
    assert result["delivery_state"] == "waiting"
    assert store.get(goal["id"])["state"] != "succeeded"


def test_human_answer_preserves_identity_and_requires_reason(setup):
    cfg, store, workspace = setup
    goal = store.upsert("intent", "Intent", "Original")
    store.wait(goal["id"], 1, "Which target account?")
    with pytest.raises(ValueError):
        command(cfg, ["resume", goal["id"]], actor="operator")
    command(
        cfg,
        ["answer", goal["id"], "Use the existing approved target"],
        actor="operator",
    )
    assert store.get(goal["id"])["state"] == "ready"
    assert store.get(goal["id"])["original"] == "Original"
    assert store.context(goal["id"])["decisions"]


def test_telegram_failure_retries_without_changing_outcome(setup, monkeypatch):
    cfg, store, _ = setup
    now = [100.0]
    store.clock = lambda: now[0]
    monkeypatch.setattr(
        "orchestrator.delivery.DeliveryStore", lambda *args, **kwargs: store
    )
    goal = store.upsert("notice", "Notice", "Original")
    store.record_evidence(goal["id"], 1, "human_acceptance", True, "human:operator", {})
    store.verify(goal["id"])
    cfg.update(telegram_bot_token="test-placeholder", telegram_chat_id="test-chat")
    flush_outbox(
        cfg, sender=lambda text: None, projector=lambda *args: {"updated": True}
    )
    assert store.snapshot()["pending_notifications"] > 0
    assert store.get(goal["id"])["state"] == "succeeded"
    now[0] += 61
    delivered = []
    flush_outbox(
        cfg,
        sender=lambda text: delivered.append(text) or {"message_id": 1},
        projector=lambda *args: {"updated": True},
    )
    assert store.snapshot()["pending_notifications"] == 0
    assert len(delivered) == 1


def test_invalid_plans_fail_before_creating_any_children():
    base = [
        {"key": "a", "title": "A", "body": "A", "depends_on": ["b"]},
        {"key": "b", "title": "B", "body": "B", "depends_on": ["a"]},
    ]
    with pytest.raises(ValueError, match="cycle"):
        validate_plan({"sub_issues": base}, {"owner/repo"}, "owner/repo")
    base[1]["depends_on"] = []
    base[0]["repo"] = "unapproved/repo"
    with pytest.raises(ValueError, match="workspace"):
        validate_plan({"sub_issues": base}, {"owner/repo"}, "owner/repo")


def test_issue_cannot_supply_arbitrary_verifier_command():
    body = "## Delivery Contract\n```yaml\nchecks:\n- id: attack\n  type: configured_command\n  argv: [sh, -c, forbidden]\n```"
    with pytest.raises(ValueError, match="name only"):
        parse_contract(body)


def test_dependency_wakes_after_verified_delivery(setup):
    cfg, store, _ = setup
    a = store.upsert("a", "A", "A")
    b = store.upsert("b", "B", "B")
    store.depend(b["id"], a["id"])
    store.wait(b["id"], 1, "dependency: waiting")
    store.record_evidence(a["id"], 1, "human_acceptance", True, "human:operator", {})
    store.verify(a["id"])
    tick(cfg, publish=False)
    assert store.get(b["id"])["state"] == "ready"


def test_parent_resume_wakes_interrupted_child(setup):
    cfg, store, _ = setup
    parent = store.upsert("program", "Program", "Program", kind="program")
    child = store.upsert("child", "Child", "Child", parent_id=parent["id"])
    store.control(parent["id"], "pause", actor="operator")
    store.wait(child["id"], 1, "ancestor_paused: worker stopped")
    tick(cfg, publish=False)
    assert store.get(child["id"])["state"] == "waiting"
    store.control(parent["id"], "resume", actor="operator", note="Continue")
    tick(cfg, publish=False)
    assert store.get(child["id"])["state"] == "ready"


def test_acceptance_recorded_while_parent_paused_reconciles_on_resume(setup):
    cfg, store, _ = setup
    parent = store.upsert("p", "Program", "Program", kind="program")
    child = store.upsert("c", "Child", "Child", parent_id=parent["id"])
    store.wait(child["id"], 1, "Human acceptance required")
    store.control(parent["id"], "pause", actor="operator")
    command(cfg, ["accept", child["id"], "Reviewed"], actor="operator")
    assert store.get(child["id"])["state"] == "waiting"
    store.control(parent["id"], "resume", actor="operator", note="Continue")
    tick(cfg, publish=False)
    assert store.get(child["id"])["state"] == "succeeded"


def test_false_completion_corrections_are_bounded(setup):
    cfg, store, workspace = setup
    goal = store.upsert(
        "missing",
        "Missing artifact",
        "Deliver the artifact",
        metadata={"workspace": str(workspace)},
        contract={
            "checks": [{"id": "artifact", "type": "file", "path": "missing.txt"}]
        },
    )
    for n in range(3):
        store.begin_attempt(goal["id"], 1, f"a{n}", "fixture")
        store.finish_attempt(f"a{n}", {"status": "complete"})
        result = settle_result(
            cfg, {"goal_id": goal["id"], "goal_revision": 1}, {"status": "complete"}
        )
        assert result["status"] == "partial"
    assert store.get(goal["id"])["state"] == "waiting"
    assert "two correction attempts" in store.get(goal["id"])["reason"]


def test_async_verification_reconciles_blocked_mailbox(setup):
    from orchestrator.queue import render_task

    cfg, store, _ = setup
    goal = store.upsert("async", "Async", "Deliver")
    folder = Path(cfg["mailbox_dir"]) / "blocked"
    folder.mkdir(parents=True)
    (folder / "task.md").write_text(
        render_task(
            {
                "task_id": "task",
                "repo": cfg["root_dir"],
                "goal_id": goal["id"],
                "goal_revision": 1,
            },
            "Original task",
        )
    )
    store.record_evidence(goal["id"], 1, "human_acceptance", True, "human:operator", {})
    store.verify(goal["id"])
    tick(cfg, publish=False)
    assert (folder.parent / "done" / "task.md").exists()
    assert not (folder / "task.md").exists()


def test_unknown_dependency_does_not_leave_dispatchable_goal(setup):
    from orchestrator.delivery_store import DeliveryConflict, goal_id

    cfg, store, workspace = setup
    issue = {
        "number": 7,
        "title": "Dependency",
        "body": "## Delivery Contract\n```yaml\ndepends_on: [owner/repo#999]\n```",
    }
    with pytest.raises(DeliveryConflict):
        register_issue(
            cfg,
            "p",
            {"github_repo": "owner/repo", "local_repo": str(workspace)},
            issue,
            "research",
        )
    assert store.get(goal_id("github:owner/repo#7"))["state"] == "waiting"
