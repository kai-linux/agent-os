"""Tests for orchestrator.pr_risk_assessment."""
from __future__ import annotations

from unittest.mock import patch, MagicMock
import json

from orchestrator.pr_risk_assessment import (
    assess_pr_risk,
    RiskAssessment,
    RiskSignal,
    _get_pr_diff_stat,
)


def _mock_diff_stat(lines: str):
    """Return a patch that makes _get_pr_diff_stat return parsed lines."""
    files = []
    for raw in lines.strip().splitlines():
        if "files changed" in raw:
            continue
        if "|" not in raw:
            continue
        path, count = raw.split("|", 1)
        files.append({"path": path.strip(), "additions": int(count.strip().split()[0]), "deletions": 0})
    result = MagicMock()
    result.returncode = 0
    result.stdout = json.dumps({"files": files})
    return patch("orchestrator.pr_risk_assessment.subprocess.run", return_value=result)


class TestGetPrDiffStat:
    def test_parses_pr_view_files_payload(self):
        result = MagicMock()
        result.returncode = 0
        result.stdout = json.dumps(
            {
                "files": [
                    {"path": "orchestrator/queue.py", "additions": 30, "deletions": 12},
                    {"path": "tests/test_queue.py", "additions": 18, "deletions": 0},
                ]
            }
        )
        with patch("orchestrator.pr_risk_assessment.subprocess.run", return_value=result) as run:
            files, total = _get_pr_diff_stat("owner/repo", 1)
        assert run.call_args.args[0] == ["gh", "pr", "view", "1", "-R", "owner/repo", "--json", "files"]
        assert files == ["orchestrator/queue.py", "tests/test_queue.py"]
        assert total == 60

    def test_handles_empty_output(self):
        result = MagicMock()
        result.returncode = 1
        result.stdout = ""
        with patch("orchestrator.pr_risk_assessment.subprocess.run", return_value=result):
            files, total = _get_pr_diff_stat("owner/repo", 1)
        assert files == []
        assert total == 0

    def test_handles_exception(self):
        with patch("orchestrator.pr_risk_assessment.subprocess.run", side_effect=Exception("fail")):
            files, total = _get_pr_diff_stat("owner/repo", 1)
        assert files == []
        assert total == 0


class TestAssessPrRisk:
    def test_low_risk_simple_change(self):
        stat = " orchestrator/backlog_groomer.py | 10 +++\n tests/test_backlog_groomer.py | 5 ++\n"
        with _mock_diff_stat(stat):
            risk = assess_pr_risk("owner/repo", 1)
        assert risk.level == "low"
        assert risk.signals == []
        assert risk.has_test_changes is True
        assert risk.has_source_changes is True

    def test_high_risk_workflow_change(self):
        stat = " .github/workflows/ci.yml | 20 +++---\n"
        with _mock_diff_stat(stat):
            risk = assess_pr_risk("owner/repo", 1)
        assert risk.level == "high"
        assert any(s.category == "risky_path" and s.severity == "high" for s in risk.signals)

    def test_high_risk_queue_change(self):
        stat = " orchestrator/queue.py | 30 +++---\n tests/test_queue.py | 15 +++\n"
        with _mock_diff_stat(stat):
            risk = assess_pr_risk("owner/repo", 1)
        assert risk.level == "high"
        assert any("core queue" in s.detail for s in risk.signals)

    def test_high_risk_secrets_path(self):
        stat = " secrets/prod.json | 5 +++\n"
        with _mock_diff_stat(stat):
            risk = assess_pr_risk("owner/repo", 1)
        assert risk.level == "high"

    def test_high_risk_bin_path(self):
        stat = " bin/run_queue.sh | 8 +++---\n"
        with _mock_diff_stat(stat):
            risk = assess_pr_risk("owner/repo", 1)
        assert risk.level == "high"

    def test_medium_risk_dispatcher_change(self):
        stat = " orchestrator/github_dispatcher.py | 25 +++---\n tests/test_github_dispatcher.py | 10 +++\n"
        with _mock_diff_stat(stat):
            risk = assess_pr_risk("owner/repo", 1)
        assert risk.level == "medium"
        assert any("dispatcher" in s.detail for s in risk.signals)

    def test_medium_risk_no_tests(self):
        stat = " orchestrator/backlog_groomer.py | 10 +++\n"
        with _mock_diff_stat(stat):
            risk = assess_pr_risk("owner/repo", 1)
        assert risk.level == "medium"
        assert any(s.category == "coverage_gap" for s in risk.signals)

    def test_medium_risk_large_diff_files(self):
        lines = "".join(f" file{i}.py | 5 +++\n" for i in range(16))
        lines += " tests/test_x.py | 5 +++\n"
        with _mock_diff_stat(lines):
            risk = assess_pr_risk("owner/repo", 1)
        assert any(s.category == "large_diff" for s in risk.signals)

    def test_medium_risk_large_diff_lines(self):
        stat = " orchestrator/new_feature.py | 600 +++\n tests/test_new_feature.py | 100 +++\n"
        with _mock_diff_stat(stat):
            risk = assess_pr_risk("owner/repo", 1)
        assert any(s.category == "large_diff" and "lines" in s.detail for s in risk.signals)

    def test_non_source_changes_no_coverage_gap(self):
        stat = " README.md | 10 +++\n docs/arch.md | 5 +++\n"
        with _mock_diff_stat(stat):
            risk = assess_pr_risk("owner/repo", 1)
        assert risk.level == "low"
        assert not any(s.category == "coverage_gap" for s in risk.signals)

    def test_dependency_file_medium_risk(self):
        stat = " requirements.txt | 3 +++\n"
        with _mock_diff_stat(stat):
            risk = assess_pr_risk("owner/repo", 1)
        assert risk.level == "medium"
        assert any("dependency" in s.detail for s in risk.signals)


class TestRiskAssessmentSummary:
    def test_low_risk_summary(self):
        r = RiskAssessment(files_changed=2, lines_changed=15)
        assert "Low risk" in r.summary
        assert "low" in r.short_summary

    def test_high_risk_summary(self):
        r = RiskAssessment(
            level="high",
            files_changed=3,
            lines_changed=42,
            signals=[RiskSignal("risky_path", "high", "CI/CD workflow modified: `.github/workflows/ci.yml`")],
        )
        assert "HIGH" in r.summary
        assert "CI/CD" in r.summary
        assert "high" in r.short_summary
