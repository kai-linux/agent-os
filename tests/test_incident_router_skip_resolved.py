"""Regression coverage for the 2026-04-24 stale-digest-alert incident.

Five sev3 incidents landed in the Telegram digest at 09:00 UTC; four were
about PRs that had already merged the previous afternoon:
- pr_monitor stuck_pr_merge on agent-os#330 (created 15:55, PR merged 16:00)
- work_verifier blocks on liminalconsultants#53/54/55 (created 18:15-25,
  PRs merged 18:36)

Only the fifth (agent-os#349) reflected a still-live condition.
`flush_pending` never re-checked the underlying PR state before delivery,
so any incident raised before the digest hour shipped regardless of
whether its cause had since resolved. The fix: liveness probe via
`gh pr view` right before delivery; if the PR is merged/closed, mark the
incident resolved and skip.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone

from orchestrator import incident_router as ir


def _make_incident(event: dict, created_at: datetime, sev: str = "sev3") -> dict:
    return {
        "id": "deadbeef",
        "sev": sev,
        "source": str(event.get("source") or ""),
        "event": event,
        "event_key": "test",
        "created_at": created_at.isoformat(),
        "resolved_at": None,
        "deduped_to": None,
        "notified_at": None,
    }


def test_stale_probe_marks_incident_resolved_when_pr_merged(monkeypatch):
    event = {
        "source": "pr_monitor",
        "type": "stuck_pr_merge",
        "repo": "kai-linux/agent-os",
        "pr_number": 330,
    }
    def fake_gh(cmd, capture_output=True, text=True, timeout=20, check=False):
        return subprocess.CompletedProcess(
            cmd, 0,
            stdout=json.dumps({"state": "MERGED", "mergedAt": "2026-04-23T16:00:33Z", "mergeStateStatus": ""}),
            stderr="",
        )
    monkeypatch.setattr(ir.subprocess, "run", fake_gh)

    incident = _make_incident(event, datetime(2026, 4, 23, 15, 55, tzinfo=timezone.utc))
    stale, reason = ir._is_incident_stale(incident)
    assert stale is True
    assert "merged" in reason.lower()


def test_stale_probe_leaves_open_pr_alerts_untouched(monkeypatch):
    event = {
        "source": "work_verifier",
        "type": "work_verifier_block",
        "repo": "kai-linux/agent-os",
        "pr_number": 349,
    }
    def fake_gh(cmd, capture_output=True, text=True, timeout=20, check=False):
        return subprocess.CompletedProcess(
            cmd, 0,
            stdout=json.dumps({"state": "OPEN", "mergedAt": None, "mergeStateStatus": "BLOCKED"}),
            stderr="",
        )
    monkeypatch.setattr(ir.subprocess, "run", fake_gh)

    incident = _make_incident(event, datetime(2026, 4, 24, 8, 50, tzinfo=timezone.utc))
    stale, _ = ir._is_incident_stale(incident)
    assert stale is False


def test_stale_probe_clears_stuck_merge_when_pr_now_clean(monkeypatch):
    """A stuck_pr_merge alert fires when pr_monitor can't merge a CLEAN PR.
    If a subsequent poll makes it CLEAN again, the alert is no longer useful."""
    event = {
        "source": "pr_monitor",
        "type": "stuck_pr_merge",
        "repo": "kai-linux/agent-os",
        "pr_number": 999,
    }
    def fake_gh(cmd, capture_output=True, text=True, timeout=20, check=False):
        return subprocess.CompletedProcess(
            cmd, 0,
            stdout=json.dumps({"state": "OPEN", "mergedAt": None, "mergeStateStatus": "CLEAN"}),
            stderr="",
        )
    monkeypatch.setattr(ir.subprocess, "run", fake_gh)

    incident = _make_incident(event, datetime(2026, 4, 24, 8, 0, tzinfo=timezone.utc))
    stale, reason = ir._is_incident_stale(incident)
    assert stale is True
    assert "CLEAN" in reason


def test_stale_probe_fails_open_on_gh_error(monkeypatch):
    """GitHub API failure must not mark an incident resolved — we err on
    the side of delivering rather than silently swallowing."""
    event = {"source": "pr_monitor", "type": "stuck_pr_merge", "repo": "x/y", "pr_number": 1}
    def boom(cmd, capture_output=True, text=True, timeout=20, check=False):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="network down")
    monkeypatch.setattr(ir.subprocess, "run", boom)

    stale, reason = ir._is_incident_stale(_make_incident(event, datetime.now(timezone.utc)))
    assert stale is False
    assert reason == ""


def test_stale_probe_skips_non_pr_incidents(monkeypatch):
    """Incidents without a PR number (e.g. queue-level task_blocked) must
    not trigger gh lookups — the probe only applies to PR-typed incidents."""
    called = []
    monkeypatch.setattr(ir.subprocess, "run", lambda *a, **kw: called.append(1))
    incident = _make_incident({"source": "queue", "type": "task_blocked"}, datetime.now(timezone.utc))
    stale, _ = ir._is_incident_stale(incident)
    assert stale is False
    assert called == []  # no subprocess call made


def test_flush_pending_auto_resolves_stale_incidents(tmp_path, monkeypatch):
    """End-to-end: a queued digest-tier incident about a merged PR is
    auto-resolved when flush_pending runs — no Telegram send attempted."""
    cfg = {
        "root_dir": str(tmp_path),
        "incident_router": {
            "business_timezone": "UTC",
            "business_hours": {"start_hour": 9, "end_hour": 17},
            "digest_hour": 9,
            "tiers": {
                "sev3": {"delivery": "regular_digest", "dedup_window_minutes": 0,
                         "bypass_kill_switch": False, "snooze_minutes": 0,
                         "handlers": [{"type": "telegram_chat"}]},
            },
            "sources": {},
        },
    }
    (tmp_path / "runtime" / "incidents").mkdir(parents=True)
    path = tmp_path / "runtime" / "incidents" / "incidents.jsonl"
    incident = _make_incident(
        {"source": "pr_monitor", "type": "stuck_pr_merge", "repo": "kai-linux/agent-os", "pr_number": 330},
        datetime(2026, 4, 23, 15, 55, tzinfo=timezone.utc),
    )
    path.write_text(json.dumps(incident) + "\n")

    def fake_gh(cmd, capture_output=True, text=True, timeout=20, check=False):
        return subprocess.CompletedProcess(
            cmd, 0,
            stdout=json.dumps({"state": "MERGED", "mergedAt": "2026-04-23T16:00:33Z", "mergeStateStatus": ""}),
            stderr="",
        )
    monkeypatch.setattr(ir.subprocess, "run", fake_gh)
    send_calls = []
    monkeypatch.setattr(ir, "_send_incident",
                        lambda *args, **kw: send_calls.append(args) or True)

    sent = ir.flush_pending(cfg, now=datetime(2026, 4, 24, 9, 0, tzinfo=timezone.utc))

    assert sent == 0, "stale incident must not trigger a send"
    assert send_calls == []
    # State persisted: incident now resolved with auto-resolve reason.
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    assert rows[0]["resolved_at"] is not None
    assert "merged" in rows[0]["auto_resolved_reason"].lower()
