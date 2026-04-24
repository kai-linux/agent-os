from __future__ import annotations

from datetime import datetime, timedelta, timezone

from orchestrator import approvals


def test_request_then_resolve_moves_file_to_resolved(tmp_path):
    cfg = {"root_dir": str(tmp_path)}

    created = approvals.request(
        cfg,
        kind="sprint_plan",
        approval_id="abc123def456",
        context={"repo": "owner/repo", "plan": [{"title": "Ship it"}]},
        telegram_message_id=99,
        action_url="telegram://message?chat_id=1&message_id=99",
    )

    pending_path = tmp_path / "runtime" / "approvals" / "approval-abc123def456.md"
    resolved_path = tmp_path / "runtime" / "approvals" / "resolved" / "approval-abc123def456.md"

    assert created["status"] == "pending"
    assert pending_path.exists()

    resolved = approvals.resolve(cfg, "abc123def456", "approve", "Operator approved.")

    assert not pending_path.exists()
    assert resolved_path.exists()
    assert resolved["status"] == "resolved"
    assert resolved["decision"] == "approve"
    assert resolved["reason"] == "Operator approved."


def test_expire_pending_uses_kind_default_resolution(tmp_path):
    cfg = {"root_dir": str(tmp_path)}
    approvals.request(
        cfg,
        kind="high_risk_pr",
        approval_id="deadbeefcafe",
        expires_at=(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
        context={"repo": "owner/repo", "pr_number": 42},
    )

    expired = approvals.expire_pending(cfg)

    assert len(expired) == 1
    resolved = approvals.get(cfg, "deadbeefcafe")
    assert resolved is not None
    assert resolved["status"] == "resolved"
    assert resolved["decision"] == "hold"
    assert "defaulted to hold" in resolved["reason"]


def test_cli_resolve_reads_decision_from_frontmatter(tmp_path, monkeypatch):
    cfg = {"root_dir": str(tmp_path)}
    monkeypatch.setattr(approvals, "load_config", lambda: cfg)
    approvals.request(
        cfg,
        kind="repeat_blocker",
        approval_id="feedfaceb00c",
        context={"repo": "owner/repo", "blocker_code": "missing_context"},
    )
    pending_path = tmp_path / "runtime" / "approvals" / "approval-feedfaceb00c.md"
    text = pending_path.read_text(encoding="utf-8")
    text = text.replace("decision: pending", "decision: hold", 1)
    text = text.replace("reason: ''", "reason: Needs RCA before retry.", 1)
    text = text.replace('reason: ""', "reason: Needs RCA before retry.", 1)
    pending_path.write_text(text, encoding="utf-8")

    rc = approvals.main(["resolve", "feedfaceb00c"])

    assert rc == 0
    resolved = approvals.get(cfg, "feedfaceb00c")
    assert resolved is not None
    assert resolved["decision"] == "hold"
    assert resolved["reason"] == "Needs RCA before retry."
