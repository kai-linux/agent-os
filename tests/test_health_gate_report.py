import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.agent_scorer import GATE_DECISIONS_FILENAME, log_gate_decision
from orchestrator.health_gate_report import (
    _compute_baseline,
    _count_blocker_codes,
    _gate_decision_summary,
    _load_gate_decisions,
    generate_report,
)


def _ts(hours_ago: int) -> str:
    return (datetime.now(tz=timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def _make_record(agent: str, status: str, blocker_code: str = "", hours_ago: int = 1) -> dict:
    return {
        "timestamp": _ts(hours_ago),
        "agent": agent,
        "status": status,
        "blocker_code": blocker_code,
        "task_id": f"task-{hours_ago}",
        "repo": "/home/kai/agent-os",
        "attempt_count": 1,
        "duration_seconds": 100.0,
        "task_type": "implementation",
    }


def test_log_gate_decision_creates_jsonl(tmp_path):
    metrics_dir = tmp_path / "metrics"
    log_gate_decision(
        metrics_dir,
        gate="adaptive_7d_25pct",
        skipped={"deepseek": {"total": 5, "successes": 0, "rate": 0.0}},
        passed=["claude", "codex"],
        context="test",
    )
    log_path = metrics_dir / GATE_DECISIONS_FILENAME
    assert log_path.exists()
    records = [json.loads(l) for l in log_path.read_text().strip().splitlines()]
    assert len(records) == 1
    assert records[0]["gate"] == "adaptive_7d_25pct"
    assert "deepseek" in records[0]["skipped"]
    assert records[0]["passed"] == ["claude", "codex"]


def test_load_gate_decisions_filters_by_window(tmp_path):
    metrics_dir = tmp_path / "metrics"
    metrics_dir.mkdir(parents=True)
    log_path = metrics_dir / GATE_DECISIONS_FILENAME

    old_ts = (datetime.now(tz=timezone.utc) - timedelta(days=30)).isoformat()
    recent_ts = (datetime.now(tz=timezone.utc) - timedelta(hours=1)).isoformat()

    records = [
        {"timestamp": old_ts, "gate": "old", "skipped": {}, "passed": [], "context": ""},
        {"timestamp": recent_ts, "gate": "recent", "skipped": {}, "passed": [], "context": ""},
    ]
    log_path.write_text("\n".join(json.dumps(r) for r in records) + "\n")

    loaded = _load_gate_decisions(metrics_dir, window_days=7)
    assert len(loaded) == 1
    assert loaded[0]["gate"] == "recent"


def test_compute_baseline():
    records = [
        _make_record("claude", "complete"),
        _make_record("claude", "complete"),
        _make_record("codex", "complete"),
        _make_record("codex", "partial", "missing_context"),
        _make_record("deepseek", "blocked", "missing_credentials"),
    ]
    rates = _compute_baseline(records)
    assert rates["claude"]["rate"] == 1.0
    assert rates["codex"]["rate"] == 0.5
    assert rates["deepseek"]["rate"] == 0.0


def test_count_blocker_codes():
    records = [
        _make_record("codex", "blocked", "fallback_exhausted", hours_ago=1),
        _make_record("codex", "blocked", "fallback_exhausted", hours_ago=2),
        _make_record("codex", "blocked", "missing_context", hours_ago=3),
        _make_record("codex", "blocked", "fallback_exhausted", hours_ago=200),
    ]
    codes = _count_blocker_codes(records, window_days=7)
    assert codes["fallback_exhausted"] == 2
    assert codes["missing_context"] == 1


def test_gate_decision_summary():
    decisions = [
        {"skipped": {"deepseek": {"total": 5, "successes": 0, "rate": 0.0}}, "passed": ["claude"], "context": "dispatcher:resolve_agent"},
        {"skipped": {"deepseek": {"total": 5, "successes": 0, "rate": 0.0}}, "passed": ["claude"], "context": "queue:get_agent_chain"},
    ]
    summary = _gate_decision_summary(decisions)
    assert summary["total_invocations"] == 2
    assert summary["agents_skipped"]["deepseek"] == 2


def test_generate_report_with_metrics(tmp_path):
    # Create a minimal config and metrics
    metrics_dir = tmp_path / "runtime" / "metrics"
    metrics_dir.mkdir(parents=True)
    metrics_file = metrics_dir / "agent_stats.jsonl"

    records = [
        _make_record("claude", "complete", hours_ago=1),
        _make_record("claude", "complete", hours_ago=2),
        _make_record("codex", "complete", hours_ago=3),
        _make_record("codex", "partial", "missing_context", hours_ago=4),
        _make_record("deepseek", "blocked", "missing_credentials", hours_ago=5),
    ]
    metrics_file.write_text("\n".join(json.dumps(r) for r in records) + "\n")

    cfg = {"root_dir": str(tmp_path)}
    report = generate_report(cfg)

    assert "Health Gate Validation Report" in report
    assert "Baseline Metrics" in report
    assert "claude" in report
    assert "Gate Decisions" in report
    assert "Validation Status" in report
    assert "False Positive Analysis" in report


def test_generate_report_empty_metrics(tmp_path):
    metrics_dir = tmp_path / "runtime" / "metrics"
    metrics_dir.mkdir(parents=True)
    (metrics_dir / "agent_stats.jsonl").write_text("")

    cfg = {"root_dir": str(tmp_path)}
    report = generate_report(cfg)
    assert "Health Gate Validation Report" in report
