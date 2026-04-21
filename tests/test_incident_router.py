from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator import incident_router as ir
from orchestrator.queue import handle_telegram_command


def _cfg(tmp_path: Path) -> dict:
    return {
        "root_dir": str(tmp_path),
        "mailbox_dir": str(tmp_path / "runtime" / "mailbox"),
        "logs_dir": str(tmp_path / "runtime" / "logs"),
        "incident_router": {
            "business_timezone": "UTC",
            "business_hours": {"start_hour": 9, "end_hour": 17},
            "digest_hour": 9,
            "tiers": {
                "sev1": {
                    "delivery": "immediate",
                    "dedup_window_minutes": 0,
                    "bypass_kill_switch": True,
                    "snooze_minutes": 0,
                    "handlers": [{"type": "telegram_chat", "snooze_minutes": 0}],
                },
                "sev2": {
                    "delivery": "next_business_hour",
                    "dedup_window_minutes": 60,
                    "bypass_kill_switch": False,
                    "snooze_minutes": 60,
                    "handlers": [{"type": "telegram_chat", "snooze_minutes": 60}],
                },
                "sev3": {
                    "delivery": "regular_digest",
                    "dedup_window_minutes": 240,
                    "bypass_kill_switch": False,
                    "snooze_minutes": 240,
                    "handlers": [{"type": "telegram_chat", "snooze_minutes": 240}],
                },
            },
            "sources": {},
        },
    }


def test_sev1_routes_immediately_without_dedup(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    sent: list[tuple[str, dict | None, str | None, bool]] = []
    monkeypatch.setattr(
        "orchestrator.queue.send_telegram",
        lambda cfg, text, logfile=None, queue_summary_log=None, reply_markup=None, chat_id=None, bypass_kill_switch=False: sent.append((text, reply_markup, chat_id, bypass_kill_switch)) or 101,
    )
    now = datetime(2026, 4, 21, 10, 0, tzinfo=timezone.utc)
    event = {
        "source": "queue",
        "type": "task_failed",
        "repo": "agent-os",
        "task_id": "task-123",
        "summary": "Critical execution path failed.",
        "runbook_url": "https://example.com/runbook",
        "dedup_key": "queue:task-123:critical",
    }

    first = ir.escalate("sev1", event, cfg=cfg, now=now)
    second = ir.escalate("sev1", event, cfg=cfg, now=now + timedelta(minutes=1))

    assert len(sent) == 2
    assert first["message_id"] == 101
    assert second["message_id"] == 101
    assert "Runbook:" in sent[0][0]
    assert sent[0][2] is None
    assert sent[0][3] is True
    incidents = ir.list_incidents(cfg)
    assert len(incidents) == 2
    assert incidents[0].get("deduped_to") is None
    assert incidents[1].get("deduped_to") is None


def test_router_uses_per_tier_handler_chat_override(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    cfg["incident_router"]["tiers"]["sev2"]["handlers"] = [
        {"type": "telegram_chat", "chat_id": "ops-sev2", "snooze_minutes": 60}
    ]
    sent: list[tuple[str | None, bool]] = []
    monkeypatch.setattr(
        "orchestrator.queue.send_telegram",
        lambda cfg, text, logfile=None, queue_summary_log=None, reply_markup=None, chat_id=None, bypass_kill_switch=False: sent.append((chat_id, bypass_kill_switch)) or 202,
    )

    ir.escalate(
        "sev2",
        {
            "source": "pr_monitor",
            "type": "high_risk_pr",
            "repo": "owner/repo",
            "pr_number": 17,
            "summary": "Risk escalation",
            "dedup_key": "risk:17",
        },
        cfg=cfg,
        now=datetime(2026, 4, 21, 18, 0, tzinfo=timezone.utc),
    )
    ir.flush_pending(cfg, now=datetime(2026, 4, 22, 9, 0, tzinfo=timezone.utc))

    assert sent == [("ops-sev2", False)]


def test_sev3_dedups_within_configured_window(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    sent: list[str] = []
    monkeypatch.setattr(
        "orchestrator.queue.send_telegram",
        lambda cfg, text, logfile=None, queue_summary_log=None, reply_markup=None: sent.append(text) or 201,
    )
    now = datetime(2026, 4, 21, 10, 0, tzinfo=timezone.utc)
    event = {
        "source": "agent_scorer",
        "type": "agent_remediation",
        "repo": "owner/repo",
        "summary": "Codex degraded below threshold.",
        "dedup_key": "agent:codex:owner/repo",
    }

    first = ir.escalate("sev3", event, cfg=cfg, now=now)
    second = ir.escalate("sev3", event, cfg=cfg, now=now + timedelta(minutes=5))

    assert sent == []
    incidents = ir.list_incidents(cfg)
    assert len(incidents) == 2
    assert incidents[0]["id"] == first["id"]
    assert incidents[1]["deduped_to"] == first["id"]
    assert second["deduped_to"] == first["id"]


def test_ack_and_resolve_commands_update_persisted_incident(tmp_path):
    cfg = _cfg(tmp_path)
    incident = ir.escalate(
        "sev3",
        {
            "source": "agent_scorer",
            "type": "agent_remediation",
            "repo": "owner/repo",
            "summary": "Codex degraded below threshold.",
            "dedup_key": "agent:codex:owner/repo",
        },
        cfg=cfg,
        now=datetime(2026, 4, 21, 10, 0, tzinfo=timezone.utc),
    )
    paths = {"ROOT": tmp_path, "CONFIG": tmp_path / "config.yaml"}

    ack_reply = handle_telegram_command(cfg, paths, f"/ack {incident['id']}")
    resolved_reply = handle_telegram_command(cfg, paths, f"/resolve {incident['id']}")

    updated = ir.list_incidents(cfg)[0]
    assert "acknowledged" in ack_reply
    assert "resolved" in resolved_reply
    assert updated["ack_at"]
    assert updated["resolved_at"]
