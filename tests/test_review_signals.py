"""Tests for orchestrator/review_signals.py — review signal recording, querying, and follow-up generation."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.pr_risk_assessment import RiskAssessment, RiskSignal
from orchestrator import review_signals


def _make_cfg(tmp_path: Path) -> dict:
    return {"root_dir": str(tmp_path)}


def _make_risk(
    level: str = "low",
    signals: list[RiskSignal] | None = None,
    files_changed: int = 3,
    lines_changed: int = 50,
    has_test_changes: bool = True,
    has_source_changes: bool = True,
) -> RiskAssessment:
    return RiskAssessment(
        level=level,
        signals=signals or [],
        files_changed=files_changed,
        lines_changed=lines_changed,
        has_test_changes=has_test_changes,
        has_source_changes=has_source_changes,
    )


class TestRecordReviewSignal:
    def test_records_signal_to_jsonl(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        risk = _make_risk(level="medium", has_test_changes=False)
        record = review_signals.record_review_signal(
            cfg,
            repo="owner/repo",
            pr_number=42,
            task_id="task-123",
            issue_number=10,
            risk=risk,
            branch="agent/task-123",
        )

        assert record["repo"] == "owner/repo"
        assert record["pr_number"] == 42
        assert record["coverage_gap"] is True
        assert record["risk_level"] == "medium"

        log_path = tmp_path / "runtime" / "metrics" / review_signals.REVIEW_SIGNALS_FILENAME
        assert log_path.exists()
        lines = [json.loads(l) for l in log_path.read_text().strip().splitlines()]
        assert len(lines) == 1
        assert lines[0]["pr_number"] == 42

    def test_appends_multiple_signals(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        review_signals.record_review_signal(
            cfg, repo="owner/repo", pr_number=1, task_id="t1",
            issue_number=1, risk=_make_risk(),
        )
        review_signals.record_review_signal(
            cfg, repo="owner/repo", pr_number=2, task_id="t2",
            issue_number=2, risk=_make_risk(),
        )

        log_path = tmp_path / "runtime" / "metrics" / review_signals.REVIEW_SIGNALS_FILENAME
        lines = [json.loads(l) for l in log_path.read_text().strip().splitlines()]
        assert len(lines) == 2

    def test_records_risk_signals(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        signals = [RiskSignal(category="coverage_gap", severity="medium", detail="Source files changed without test changes")]
        risk = _make_risk(level="medium", signals=signals, has_test_changes=False)
        record = review_signals.record_review_signal(
            cfg, repo="r", pr_number=5, task_id="t", issue_number=1, risk=risk,
        )
        assert len(record["signals"]) == 1
        assert "coverage_gap" in record["signals"][0]


class TestLoadReviewSignals:
    def test_loads_within_window(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        review_signals.record_review_signal(
            cfg, repo="owner/repo", pr_number=1, task_id="t1",
            issue_number=1, risk=_make_risk(),
        )
        loaded = review_signals.load_review_signals(cfg, window_days=7)
        assert len(loaded) == 1

    def test_filters_by_repo(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        review_signals.record_review_signal(
            cfg, repo="owner/repo-a", pr_number=1, task_id="t1",
            issue_number=1, risk=_make_risk(),
        )
        review_signals.record_review_signal(
            cfg, repo="owner/repo-b", pr_number=2, task_id="t2",
            issue_number=2, risk=_make_risk(),
        )
        loaded = review_signals.load_review_signals(cfg, repo="owner/repo-a")
        assert len(loaded) == 1
        assert loaded[0]["repo"] == "owner/repo-a"

    def test_returns_empty_for_missing_file(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        loaded = review_signals.load_review_signals(cfg)
        assert loaded == []


class TestQueryFlaggedSignals:
    def test_flags_coverage_gap(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        review_signals.record_review_signal(
            cfg, repo="r", pr_number=1, task_id="t1",
            issue_number=1, risk=_make_risk(has_test_changes=False),
        )
        flagged = review_signals.query_flagged_signals(cfg)
        assert len(flagged) == 1
        assert "coverage_gap" in flagged[0]["followup_reasons"]

    def test_flags_high_risk(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        review_signals.record_review_signal(
            cfg, repo="r", pr_number=1, task_id="t1",
            issue_number=1, risk=_make_risk(level="high"),
        )
        flagged = review_signals.query_flagged_signals(cfg)
        assert len(flagged) == 1
        assert "high_risk" in flagged[0]["followup_reasons"]

    def test_skips_clean_signals(self, tmp_path):
        cfg = _make_cfg(tmp_path)
        review_signals.record_review_signal(
            cfg, repo="r", pr_number=1, task_id="t1",
            issue_number=1, risk=_make_risk(level="low", has_test_changes=True),
        )
        flagged = review_signals.query_flagged_signals(cfg)
        assert len(flagged) == 0


class TestGenerateFollowupIssues:
    def test_dry_run_creates_no_issues(self, tmp_path, monkeypatch):
        cfg = _make_cfg(tmp_path)
        review_signals.record_review_signal(
            cfg, repo="owner/repo", pr_number=10, task_id="t1",
            issue_number=5, risk=_make_risk(has_test_changes=False),
        )
        created = review_signals.generate_followup_issues(cfg, "owner/repo", dry_run=True)
        assert len(created) == 1
        assert created[0]["dry_run"] is True
        assert "coverage_gap" in created[0]["title"]

    def test_respects_max_followups(self, tmp_path, monkeypatch):
        cfg = _make_cfg(tmp_path)
        for i in range(5):
            review_signals.record_review_signal(
                cfg, repo="r", pr_number=i, task_id=f"t{i}",
                issue_number=i, risk=_make_risk(has_test_changes=False),
            )

        # Simulate 2 existing follow-ups already open
        monkeypatch.setattr(review_signals, "_count_existing_followups", lambda repo: 2)
        monkeypatch.setattr(review_signals, "_followup_exists", lambda repo, title: False)

        created = review_signals.generate_followup_issues(cfg, "r", dry_run=True)
        assert len(created) == review_signals.MAX_FOLLOWUPS_PER_SPRINT - 2

    def test_deduplicates_by_title(self, tmp_path, monkeypatch):
        cfg = _make_cfg(tmp_path)
        review_signals.record_review_signal(
            cfg, repo="r", pr_number=10, task_id="t1",
            issue_number=5, risk=_make_risk(has_test_changes=False),
        )

        monkeypatch.setattr(review_signals, "_count_existing_followups", lambda repo: 0)
        monkeypatch.setattr(review_signals, "_followup_exists", lambda repo, title: True)

        created = review_signals.generate_followup_issues(cfg, "r", dry_run=True)
        assert len(created) == 0

    def test_builds_coverage_gap_body(self):
        signal = {
            "pr_number": 42,
            "task_id": "task-abc",
            "issue_number": 10,
            "risk_level": "medium",
            "files_changed": 5,
            "lines_changed": 100,
            "coverage_gap": True,
            "signals": ["coverage_gap:medium:Source files changed without test changes"],
            "followup_reasons": ["coverage_gap"],
        }
        body = review_signals._build_followup_body(signal)
        assert "coverage" in body.lower()
        assert "PR #42" in body
        assert "task-abc" in body
        assert review_signals._FOLLOWUP_MARKER in body

    def test_builds_high_risk_body(self):
        signal = {
            "pr_number": 99,
            "task_id": "task-xyz",
            "issue_number": 20,
            "risk_level": "high",
            "files_changed": 20,
            "lines_changed": 800,
            "coverage_gap": False,
            "signals": ["risky_path:high:CI/CD workflow modified"],
            "followup_reasons": ["high_risk"],
        }
        body = review_signals._build_followup_body(signal)
        assert "high-risk" in body.lower() or "high risk" in body.lower()
        assert "PR #99" in body
