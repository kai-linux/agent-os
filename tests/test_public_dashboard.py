from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from orchestrator import public_dashboard as dashboard


def _record(ts: datetime, **overrides) -> dict:
    record = {
        "timestamp": ts.isoformat(),
        "task_id": f"task-{ts.timestamp()}",
        "repo": "/tmp/agent-os",
        "agent": "claude",
        "status": "complete",
        "blocker_code": "none",
        "attempt_count": 1,
        "duration_seconds": 3600,
        "task_type": "implementation",
    }
    record.update(overrides)
    return record


def test_build_dashboard_snapshot_prefers_metrics_and_computes_momentum(tmp_path):
    now = datetime.now(tz=timezone.utc)
    metrics_dir = tmp_path / "runtime" / "metrics"
    metrics_dir.mkdir(parents=True)
    records = [
        _record(now - timedelta(days=1), agent="claude", status="complete", duration_seconds=3600),
        _record(now - timedelta(days=2), agent="claude", status="complete", duration_seconds=7200),
        _record(now - timedelta(days=3), agent="codex", status="blocked", blocker_code="missing_context", duration_seconds=1800),
        _record(now - timedelta(days=9), agent="codex", status="complete", duration_seconds=5400),
        _record(now - timedelta(days=10), agent="codex", status="partial", blocker_code="test_failure", duration_seconds=2400),
    ]
    (metrics_dir / "agent_stats.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    (tmp_path / "PRODUCTION_FEEDBACK.md").write_text(
        "# Production Feedback\n"
        f"Auto-generated: {now.isoformat()}\n"
        "Window: last 14 days | Source: agent_stats.jsonl\n\n"
        "## Key Metrics\n"
        "- Overall success rate: 1% (1/99)\n"
        "- Escalation rate: 99% (99/100)\n"
        "- Mean completion time: 99.0h\n",
        encoding="utf-8",
    )

    snapshot = dashboard.build_dashboard_snapshot(tmp_path)

    assert snapshot["summary"]["task_success_rate"]["successes"] == 3
    assert snapshot["summary"]["task_success_rate"]["total"] == 5
    assert round(snapshot["summary"]["task_success_rate"]["rate"], 2) == 0.6
    assert round(snapshot["summary"]["escalation_rate"]["rate"], 2) == 0.2
    assert snapshot["top_blockers"][0]["code"] == "missing_context"
    assert snapshot["per_agent"][0]["agent"] == "claude"
    assert len(snapshot["rolling_14_day"]) == 14
    assert snapshot["momentum"]["current_week"]["total"] == 3
    assert snapshot["momentum"]["prior_week"]["total"] == 2


def test_build_dashboard_snapshot_falls_back_to_feedback_when_metrics_missing(tmp_path):
    (tmp_path / "PRODUCTION_FEEDBACK.md").write_text(
        "# Production Feedback\n"
        "Auto-generated: 2026-04-09T06:00:00+00:00\n"
        "Window: last 14 days | Source: agent_stats.jsonl\n\n"
        "## Key Metrics\n"
        "- Overall success rate: 68% (34/50)\n"
        "- Escalation rate: 8% (4/50)\n"
        "- Mean completion time: 2.5h\n\n"
        "## Per-Agent Performance\n"
        "- claude: 80% (8/10)\n\n"
        "## Top Blocker Codes\n"
        "- missing_context: 3\n",
        encoding="utf-8",
    )

    snapshot = dashboard.build_dashboard_snapshot(tmp_path)

    assert snapshot["summary"]["task_success_rate"]["rate"] == 0.68
    assert snapshot["summary"]["mean_completion_time_hours"] == 2.5
    assert snapshot["per_agent"][0]["agent"] == "claude"
    assert snapshot["top_blockers"][0]["code"] == "missing_context"


def test_write_dashboard_writes_markdown_html_and_json(tmp_path):
    output = dashboard.write_dashboard(tmp_path)

    assert output["window_days"] == 14
    assert (tmp_path / "docs" / "reliability" / "README.md").exists()
    assert (tmp_path / "docs" / "reliability" / "index.html").exists()
    assert (tmp_path / "docs" / "reliability" / "metrics.json").exists()
