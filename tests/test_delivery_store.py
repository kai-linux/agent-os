import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from orchestrator.delivery_store import DeliveryConflict, DeliveryStore


@pytest.fixture
def store(tmp_path):
    return DeliveryStore(tmp_path / "delivery.sqlite3")


def goal(store, name="task", **kwargs):
    return store.upsert(name, name, "Original intent " + name, **kwargs)


def accept(store, item):
    store.record_evidence(
        item["id"],
        item["revision"],
        "human_acceptance",
        True,
        "human:operator",
        {"reason": "Reviewed artifact"},
    )
    return store.verify(item["id"])


def test_goal_identity_and_original_survive_restart(store):
    first = goal(store)
    other = DeliveryStore(store.path)
    assert goal(other)["id"] == first["id"]
    assert other.get(first["id"])["original"] == "Original intent task"
    with pytest.raises(DeliveryConflict, match="revision"):
        other.upsert("task", "task", "Changed meaning")


def test_decomposition_never_completes_parent(store):
    program = goal(store, "program", kind="program")
    project = goal(store, "project", kind="project", parent_id=program["id"])
    task = goal(store, "leaf", parent_id=project["id"])
    assert not accept(store, program)
    assert not accept(store, project)
    assert accept(store, task)
    assert store.verify(project["id"])
    assert store.verify(program["id"])


def test_children_alone_do_not_prove_integrated_delivery(store):
    parent = goal(store, "program", kind="program")
    child = goal(store, "child", parent_id=parent["id"])
    accept(store, child)
    assert not store.verify(parent["id"])
    assert store.get(parent["id"])["state"] == "waiting"


def test_dependencies_and_cycles(store):
    a, b, c = [goal(store, n) for n in "abc"]
    store.depend(b["id"], a["id"])
    store.depend(c["id"], b["id"])
    with pytest.raises(DeliveryConflict, match="cycle"):
        store.depend(a["id"], c["id"])
    with pytest.raises(DeliveryConflict, match="Dependencies"):
        store.begin_attempt(b["id"], 1, "blocked", "w0")
    accept(store, a)
    store.begin_attempt(b["id"], 1, "allowed", "w0")


def test_dependency_cycle_including_parent_child_edges_is_rejected(store):
    a = goal(store, "a", kind="project")
    child = goal(store, "child", parent_id=a["id"])
    b = goal(store, "b")
    store.depend(child["id"], b["id"])
    with pytest.raises(DeliveryConflict, match="cycle"):
        store.depend(b["id"], a["id"])


def test_child_cannot_bypass_parent_dependency(store):
    first = goal(store, "first")
    project = goal(store, "project", kind="project")
    child = goal(store, "child", parent_id=project["id"])
    store.depend(project["id"], first["id"])
    with pytest.raises(DeliveryConflict, match="prerequisites"):
        store.begin_attempt(child["id"], 1, "attempt", "w")


def test_child_inherits_bounded_action_grant_and_cannot_widen_it(store):
    parent = goal(
        store,
        "program",
        kind="program",
        contract={
            "allowed_actions": [
                {"capability": "publish", "target": "one", "max_calls": 1}
            ]
        },
    )
    child = goal(store, "child", parent_id=parent["id"])
    assert (
        store.prepare_action(child["id"], 1, "a", "publish", "one", {})["state"]
        == "reserved"
    )
    other = goal(store, "other", parent_id=parent["id"])
    with pytest.raises(DeliveryConflict, match="limit"):
        store.prepare_action(other["id"], 1, "b", "publish", "one", {})
    with pytest.raises(DeliveryConflict, match="delegation"):
        store.prepare_action(other["id"], 1, "c", "publish", "another", {})


def test_revision_preserves_old_intent_and_clears_old_execution(store):
    item = goal(store)
    store.bind_execution(
        item["id"], 1, {"mailbox_payload": "old task", "plan_materialized": True}
    )
    with pytest.raises(ValueError):
        store.control(
            item["id"],
            "revise",
            actor="operator",
            original="bad",
            contract={"max_attempts": -1},
        )
    assert store.get(item["id"])["revision"] == 1
    store.control(
        item["id"], "revise", actor="operator", original="revised", contract={}
    )
    current = store.get(item["id"])
    assert "mailbox_payload" not in current["metadata"]
    assert current["metadata"]["prior_execution"]["mailbox_payload"] == "old task"
    with store._db() as db:
        original = db.execute(
            "SELECT original FROM revisions WHERE goal_id=? AND revision=1",
            (item["id"],),
        ).fetchone()[0]
    assert original == "Original intent task"


def test_atomic_claim_prevents_two_workers(store):
    item = goal(store)

    def claim(key):
        try:
            store.begin_attempt(item["id"], 1, key, key)
            return True
        except DeliveryConflict:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(claim, ["w1", "w2"])) == [False, True]


def test_parent_budget_applies_to_all_children(store):
    parent = goal(
        store, "program", kind="program", contract={"budget_usd": 2, "max_attempts": 2}
    )
    a, b = [goal(store, n, parent_id=parent["id"]) for n in "ab"]
    store.begin_attempt(a["id"], 1, "first", "w", reserve_usd=1.5)
    with pytest.raises(DeliveryConflict, match="Cost budget"):
        store.begin_attempt(b["id"], 1, "second", "w", reserve_usd=1)
    store.finish_attempt("first", {"status": "partial"})
    with pytest.raises(DeliveryConflict, match="Cost budget"):
        store.begin_attempt(b["id"], 1, "third", "w", reserve_usd=1)
    assert store.snapshot()["attempts"][0]["cost_usd"] is None


def test_model_rotation_does_not_reset_attempt_budget(store):
    item = goal(store, contract={"max_attempts": 1})
    store.begin_attempt(item["id"], 1, "first", "claude")
    store.finish_attempt("first", {"status": "blocked"})
    store.retry(item["id"], 1, "Try a different provider")
    with pytest.raises(DeliveryConflict, match="Attempt budget"):
        store.begin_attempt(item["id"], 1, "second", "codex")


def test_pause_cancellation_and_revision_fence_descendants(store):
    parent = goal(store, "program", kind="program")
    child = goal(store, "child", parent_id=parent["id"])
    store.begin_attempt(child["id"], 1, "worker", "w")
    store.control(parent["id"], "pause", actor="operator")
    assert not store.execution_allowed(child["id"], 1)
    store.control(
        parent["id"], "resume", actor="operator", note="Continue agreed scope"
    )
    assert store.execution_allowed(child["id"], 1)
    store.control(
        parent["id"], "revise", actor="operator", original="New scope", contract={}
    )
    assert store.get(child["id"])["state"] == "cancelled"
    assert not store.execution_allowed(child["id"], 1)
    with pytest.raises(DeliveryConflict, match="revision"):
        store.execution_allowed(parent["id"], 1)
    store.finish_attempt("worker", {"status": "complete"})
    assert store.get(child["id"])["state"] == "cancelled"


def test_wait_survives_restart_and_wakes_without_model(tmp_path):
    clock = [1000.0]
    store = DeliveryStore(tmp_path / "db", clock=lambda: clock[0])
    item = goal(store)
    store.wait(item["id"], 1, "Next daily observation", wake_at=87400)
    store = DeliveryStore(store.path, clock=lambda: clock[0])
    store.tick()
    assert store.get(item["id"])["state"] == "waiting"
    clock[0] = 87400
    store.tick()
    assert store.get(item["id"])["state"] == "ready"
    assert not store.snapshot()["attempts"]


def test_expired_worker_does_not_blindly_repeat_external_effect(tmp_path):
    clock = [1.0]
    store = DeliveryStore(tmp_path / "db", clock=lambda: clock[0])
    item = goal(store)
    store.begin_attempt(item["id"], 1, "attempt", "w", lease_seconds=5)
    clock[0] = 10
    store.tick()
    assert store.get(item["id"])["state"] == "waiting"
    assert store.snapshot()["attempts"][0]["state"] == "lost"


def test_action_receipts_prevent_duplicate_or_unauthorized_effect(store):
    item = goal(
        store,
        contract={"allowed_actions": [{"capability": "publish", "target": "approved"}]},
    )
    with pytest.raises(DeliveryConflict, match="delegation"):
        store.prepare_action(item["id"], 1, "bad", "publish", "unapproved", {})
    first = store.prepare_action(
        item["id"], 1, "publish-1", "publish", "approved", {"asset": "recording"}
    )
    assert first["state"] == "reserved"
    again = store.prepare_action(
        item["id"], 1, "publish-1", "publish", "approved", {"asset": "recording"}
    )
    assert again["state"] == "uncertain"
    assert not accept(store, item)
    store.confirm_action("publish-1", {"url": "https://example.test/video/1"})
    assert store.verify(item["id"])
    with pytest.raises(DeliveryConflict):
        store.prepare_action(
            item["id"], 1, "publish-1", "publish", "approved", {"asset": "other"}
        )


def test_completion_and_outbox_share_transaction(store):
    item = goal(store)
    assert accept(store, item)
    pending = store.claim_outbox()
    success = [r for r in pending if '"succeeded"' in r["payload"]]
    assert {r["channel"] for r in success} == {"github", "telegram"}
    assert store.claim_outbox() == []
    for row in pending:
        store.finish_delivery(row["event_id"], row["channel"], receipt="ack")
    assert store.snapshot()["pending_notifications"] == 0


def test_worker_claim_is_never_acceptance_evidence(store):
    item = goal(store)
    store.begin_attempt(item["id"], 1, "worker", "w")
    store.finish_attempt("worker", {"status": "complete"})
    assert not store.verify(item["id"])
    assert store.get(item["id"])["state"] == "waiting"


@pytest.mark.parametrize("value", [-1, 0, float("nan"), float("inf"), True])
def test_invalid_budget_rejected(store, value):
    with pytest.raises(ValueError):
        goal(store, contract={"budget_usd": value})
