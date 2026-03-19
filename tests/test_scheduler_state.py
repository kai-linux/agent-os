"""Tests for scheduler state and due gating."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator import scheduler_state as ss


def _cfg(tmp_path: Path) -> dict:
    return {
        "root_dir": str(tmp_path),
        "mailbox_dir": str(tmp_path / "runtime" / "mailbox"),
        "logs_dir": str(tmp_path / "runtime" / "logs"),
    }


def test_is_due_when_never_run(tmp_path):
    due, reason = ss.is_due(_cfg(tmp_path), "planner", "owner/repo", cadence_hours=24)
    assert due is True
    assert reason == "never-run"


def test_is_due_false_before_cadence_elapsed(tmp_path):
    cfg = _cfg(tmp_path)
    now = datetime.now(timezone.utc)
    ss.record_run(cfg, "planner", "owner/repo", now=now)

    due, reason = ss.is_due(
        cfg,
        "planner",
        "owner/repo",
        cadence_hours=24,
        now=now + timedelta(hours=6),
    )
    assert due is False
    assert reason.startswith("next due in ")


def test_is_due_true_after_cadence_elapsed(tmp_path):
    cfg = _cfg(tmp_path)
    now = datetime.now(timezone.utc)
    ss.record_run(cfg, "planner", "owner/repo", now=now)

    due, reason = ss.is_due(
        cfg,
        "planner",
        "owner/repo",
        cadence_hours=12,
        now=now + timedelta(hours=12, minutes=1),
    )
    assert due is True
    assert reason == "due"


def test_is_due_dormant_when_cadence_zero(tmp_path):
    due, reason = ss.is_due(_cfg(tmp_path), "planner", "owner/repo", cadence_hours=0)
    assert due is False
    assert reason == "dormant"
