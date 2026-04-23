"""Tests for per-agent monthly budget tracking and hard-stop enforcement."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator import budgets

def _cfg(tmp_path: Path, **overrides) -> dict:
    cfg: dict = {
        "root_dir": str(tmp_path),
        "cost_tracking": {"default_price_multiplier": 1.0},
        "budgets": {
            "default": {"soft_warn_usd": 1.0, "hard_stop_usd": 2.0},
            "per_agent": {
                "codex": {"soft_warn_usd": 1.0, "hard_stop_usd": 2.0},
                "claude": {"soft_warn_usd": 50.0, "hard_stop_usd": 100.0},
            },
        },
    }
    cfg.update(overrides)
    return cfg

def _seed_cost_events(metrics_dir: Path, entries: list[dict]) -> Path:
    metrics_dir.mkdir(parents=True, exist_ok=True)
    path = metrics_dir / budgets.COST_EVENTS_FILENAME
    with path.open("a", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
    return path

def test_current_month_key_returns_utc_ym():
    from datetime import datetime, timezone
    key = budgets.current_month_key(datetime(2026, 4, 23, 12, 0, tzinfo=timezone.utc))
    assert key == "2026-04"

def test_record_cost_events_writes_required_fields(tmp_path):
    cfg = _cfg(tmp_path)
    path = budgets.record_cost_events(
        cfg,
        task_id="task-1",
        timestamp="2026-04-23T10:00:00+00:00",
        github_repo="acme/api",
        model_attempt_details=[
            {
                "attempt": 1,
                "agent": "codex",
                "provider": "openai",
                "model": "codex",
                "input_tokens_estimate": 1000,
                "output_tokens_estimate": 500,
                "status": "complete",
                "blocker_code": "none",
            }
        ],
    )
    assert path is not None and path.exists()
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    assert len(rows) == 1
    row = rows[0]
    for key in ("agent", "task_id", "input_tokens", "output_tokens", "usd_estimate", "timestamp", "month_key"):
        assert key in row, f"cost_events.jsonl must include {key}"
    assert row["agent"] == "codex"
    assert row["task_id"] == "task-1"
    assert row["month_key"] == "2026-04"
    assert row["usd_estimate"] > 0

def test_monthly_spend_by_agent_sums_only_requested_month(tmp_path):
    cfg = _cfg(tmp_path)
    metrics_dir = Path(cfg["root_dir"]) / "runtime" / "metrics"
    _seed_cost_events(metrics_dir, [
        {
            "timestamp": "2026-04-01T00:00:00+00:00",
            "month_key": "2026-04",
            "task_id": "t1",
            "agent": "codex",
            "usd_estimate": 3.0,
        },
        {
            "timestamp": "2026-04-15T00:00:00+00:00",
            "month_key": "2026-04",
            "task_id": "t2",
            "agent": "codex",
            "usd_estimate": 1.25,
        },
        {
            "timestamp": "2026-03-15T00:00:00+00:00",
            "month_key": "2026-03",
            "task_id": "t3",
            "agent": "codex",
            "usd_estimate": 99.0,
        },
        {
            "timestamp": "2026-04-02T00:00:00+00:00",
            "month_key": "2026-04",
            "task_id": "t4",
            "agent": "claude",
            "usd_estimate": 7.5,
        },
    ])
    totals = budgets.monthly_spend_by_agent(cfg, month_key="2026-04")
    assert totals == {"codex": 4.25, "claude": 7.5}

def test_budget_for_agent_falls_back_to_default(tmp_path):
    cfg = _cfg(tmp_path)
    entry = budgets.budget_for_agent(cfg, "gemini")
    assert entry == {"soft_warn_usd": 1.0, "hard_stop_usd": 2.0}

def test_filter_budget_compliant_applies_default_hard_stop(tmp_path):
    cfg = _cfg(tmp_path)
    metrics_dir = Path(cfg["root_dir"]) / "runtime" / "metrics"
    month_key = budgets.current_month_key()
    _seed_cost_events(metrics_dir, [
        {
            "timestamp": f"{month_key}-20T00:00:00+00:00",
            "month_key": month_key,
            "task_id": "t1",
            "agent": "gemini",
            "usd_estimate": 2.5,
        },
    ])
    passing, skipped = budgets.filter_budget_compliant_agents(["gemini", "claude"], cfg)
    assert passing == ["claude"]
    assert skipped["gemini"]["hard_stop_usd"] == 2.0
    assert skipped["gemini"]["hard_stopped"] is True

def test_budget_for_agent_returns_none_when_missing():
    assert budgets.budget_for_agent({}, "codex") is None
    assert budgets.budget_for_agent({"budgets": {}}, "codex") is None

def test_filter_budget_compliant_returns_input_when_no_config(tmp_path):
    cfg = {"root_dir": str(tmp_path)}
    passing, skipped = budgets.filter_budget_compliant_agents(["codex", "claude"], cfg)
    assert passing == ["codex", "claude"]
    assert skipped == {}

def test_filter_budget_compliant_removes_hard_stopped_agent(tmp_path):
    cfg = _cfg(tmp_path)
    metrics_dir = Path(cfg["root_dir"]) / "runtime" / "metrics"
    month_key = budgets.current_month_key()
    _seed_cost_events(metrics_dir, [
        {
            "timestamp": "2026-04-20T00:00:00+00:00",
            "month_key": month_key,
            "task_id": "t1",
            "agent": "codex",
            "usd_estimate": 5.0,
        },
    ])
    passing, skipped = budgets.filter_budget_compliant_agents(["codex", "claude"], cfg)
    assert "codex" not in passing
    assert "codex" in skipped
    assert skipped["codex"]["hard_stopped"] is True
    assert skipped["codex"]["spend_usd"] == 5.0
    assert passing == ["claude"]

def test_filter_budget_compliant_passes_agent_below_hard_stop(tmp_path):
    cfg = _cfg(tmp_path)
    metrics_dir = Path(cfg["root_dir"]) / "runtime" / "metrics"
    month_key = budgets.current_month_key()
    _seed_cost_events(metrics_dir, [
        {
            "timestamp": "2026-04-20T00:00:00+00:00",
            "month_key": month_key,
            "task_id": "t1",
            "agent": "codex",
            "usd_estimate": 1.5,
        },
    ])
    passing, skipped = budgets.filter_budget_compliant_agents(["codex"], cfg)
    assert passing == ["codex"]
    assert skipped == {}

def test_hard_stopped_agent_excluded_even_as_sole_candidate(tmp_path, monkeypatch):
    """Regression: a hard-stopped agent must not be routed to, even when it is
    the only candidate. This protects the budget invariant against the
    health-gate safety net and against the sole-candidate fallback path."""
    cfg = _cfg(tmp_path)
    metrics_dir = Path(cfg["root_dir"]) / "runtime" / "metrics"
    month_key = budgets.current_month_key()
    _seed_cost_events(metrics_dir, [
        {
            "timestamp": "2026-04-20T00:00:00+00:00",
            "month_key": month_key,
            "task_id": "t1",
            "agent": "codex",
            "usd_estimate": 9999.0,
        },
    ])
    passing, skipped = budgets.filter_budget_compliant_agents(["codex"], cfg)
    assert passing == [], "sole over-budget candidate must be removed"
    assert "codex" in skipped

def test_check_budget_alerts_fires_once_per_threshold(tmp_path):
    cfg = _cfg(tmp_path)
    metrics_dir = Path(cfg["root_dir"]) / "runtime" / "metrics"
    month_key = budgets.current_month_key()
    _seed_cost_events(metrics_dir, [
        {
            "timestamp": "2026-04-20T00:00:00+00:00",
            "month_key": month_key,
            "task_id": "t1",
            "agent": "codex",
            "usd_estimate": 5.0,
        },
    ])
    sent: list[str] = []

    def fake_send(_cfg, text):
        sent.append(text)

    fired_first = budgets.check_budget_alerts(cfg, fake_send)
    fired_second = budgets.check_budget_alerts(cfg, fake_send)
    thresholds = {item["threshold"] for item in fired_first}
    assert thresholds == {"soft_warn", "hard_stop"}
    assert fired_second == []
    assert len(sent) == 2

def test_soft_warn_alert_does_not_filter_agent(tmp_path):
    cfg = _cfg(tmp_path)
    metrics_dir = Path(cfg["root_dir"]) / "runtime" / "metrics"
    month_key = budgets.current_month_key()
    _seed_cost_events(metrics_dir, [
        {
            "timestamp": "2026-04-20T00:00:00+00:00",
            "month_key": month_key,
            "task_id": "t1",
            "agent": "codex",
            "usd_estimate": 1.25,
        },
    ])
    sent: list[str] = []
    fired = budgets.check_budget_alerts(cfg, lambda _c, text: sent.append(text))
    thresholds = {item["threshold"] for item in fired}
    assert thresholds == {"soft_warn"}
    passing, _ = budgets.filter_budget_compliant_agents(["codex"], cfg)
    assert passing == ["codex"]

def test_warn_if_budgets_missing_logs_once(tmp_path):
    budgets.reset_missing_warning_for_tests()
    messages: list[str] = []
    budgets.warn_if_budgets_missing({"root_dir": str(tmp_path)}, logger=messages.append)
    budgets.warn_if_budgets_missing({"root_dir": str(tmp_path)}, logger=messages.append)
    assert len(messages) == 1
    assert "Budget enforcement disabled" in messages[0]
    budgets.reset_missing_warning_for_tests()

def test_warn_skipped_when_budgets_configured(tmp_path):
    budgets.reset_missing_warning_for_tests()
    messages: list[str] = []
    budgets.warn_if_budgets_missing(_cfg(tmp_path), logger=messages.append)
    assert messages == []
    budgets.reset_missing_warning_for_tests()

def test_budget_snapshot_includes_per_agent_remaining(tmp_path):
    cfg = _cfg(tmp_path)
    metrics_dir = Path(cfg["root_dir"]) / "runtime" / "metrics"
    month_key = budgets.current_month_key()
    _seed_cost_events(metrics_dir, [
        {
            "timestamp": "2026-04-10T00:00:00+00:00",
            "month_key": month_key,
            "task_id": "t1",
            "agent": "codex",
            "usd_estimate": 1.5,
        },
    ])
    snapshot = budgets.budget_snapshot(cfg)
    assert snapshot["enabled"] is True
    assert snapshot["month_key"] == month_key
    entries = {item["agent"]: item for item in snapshot["per_agent"]}
    assert entries["codex"]["spend_usd"] == 1.5
    assert entries["codex"]["remaining_usd"] == 0.5
    assert entries["codex"]["hard_stopped"] is False

def test_record_cost_events_and_monthly_spend_round_trip(tmp_path):
    """End-to-end: write events via record_cost_events, read via monthly_spend_by_agent."""
    cfg = _cfg(tmp_path)
    budgets.record_cost_events(
        cfg,
        task_id="task-a",
        timestamp="2026-04-10T09:00:00+00:00",
        github_repo="acme/api",
        model_attempt_details=[
            {
                "attempt": 1,
                "agent": "codex",
                "provider": "openai",
                "model": "codex",
                "input_tokens_estimate": 1_000_000,
                "output_tokens_estimate": 500_000,
                "status": "complete",
                "blocker_code": "none",
            }
        ],
    )
    spend = budgets.monthly_spend_by_agent(cfg, month_key="2026-04")
    # codex pricing: $15/M input + $60/M output → $15 + $30 = $45
    assert spend["codex"] == pytest.approx(45.0, abs=1e-3)

def test_get_agent_chain_excludes_hard_stopped_sole_candidate(tmp_path, monkeypatch):
    """Integration regression: the queue's dispatch chain must drop a
    hard-stopped agent even when it is the sole candidate for the task type.

    Protects against the health-gate safety net silently resurrecting an
    over-budget agent."""
    from orchestrator import queue

    cfg = _cfg(
        tmp_path,
        default_task_type="implementation",
        default_agent="auto",
        agent_fallbacks={"implementation": ["codex"]},
    )
    metrics_dir = Path(cfg["root_dir"]) / "runtime" / "metrics"
    month_key = budgets.current_month_key()
    _seed_cost_events(metrics_dir, [
        {
            "timestamp": f"{month_key}-15T00:00:00+00:00",
            "month_key": month_key,
            "task_id": "t1",
            "agent": "codex",
            "usd_estimate": 9999.0,
        },
    ])

    monkeypatch.setattr(queue, "agent_available", lambda agent: (True, ""))

    chain = queue.get_agent_chain({"task_type": "implementation", "agent": "auto"}, cfg)
    assert chain == [], "hard-stopped sole candidate must be removed from dispatch chain"
    assert queue.get_next_agent({"task_type": "implementation", "agent": "auto"}, cfg, []) is None

def test_get_agent_chain_skips_hard_stopped_requested_agent_and_uses_fallback(tmp_path, monkeypatch):
    """Regression: an explicit agent preference must not bypass the monthly
    hard-stop when a compliant fallback is available."""

    from orchestrator import queue

    cfg = _cfg(
        tmp_path,
        default_task_type="implementation",
        default_agent="auto",
        agent_fallbacks={"implementation": ["codex", "claude"]},
    )
    metrics_dir = Path(cfg["root_dir"]) / "runtime" / "metrics"
    month_key = budgets.current_month_key()
    _seed_cost_events(metrics_dir, [
        {
            "timestamp": f"{month_key}-15T00:00:00+00:00",
            "month_key": month_key,
            "task_id": "t1",
            "agent": "codex",
            "usd_estimate": 9999.0,
        },
    ])

    monkeypatch.setattr(queue, "agent_available", lambda agent: (True, ""))

    chain = queue.get_agent_chain({"task_type": "implementation", "agent": "codex"}, cfg)
    assert chain == ["claude"], "requested over-budget agent must be removed while fallback remains"
    assert queue.get_next_agent({"task_type": "implementation", "agent": "codex"}, cfg, []) == "claude"

    meta = {"task_type": "implementation", "agent": "codex"}
    chain = queue.get_agent_chain(meta, cfg)
    assert chain == ["claude"], "over-budget requested agent must not be selected"
    assert queue.get_next_agent(meta, cfg, []) == "claude"
