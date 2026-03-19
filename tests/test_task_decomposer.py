"""Tests for orchestrator.task_decomposer."""
from __future__ import annotations

import json
from unittest import mock

import pytest

from orchestrator.task_decomposer import decompose_issue, create_sub_issues


# ---------------------------------------------------------------------------
# decompose_issue
# ---------------------------------------------------------------------------

def _mock_claude_run(stdout: str, returncode: int = 0):
    """Return a mock for subprocess.run that returns the given stdout."""
    m = mock.MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = ""
    return m


class TestDecomposeIssue:
    def test_atomic_result(self):
        with mock.patch("orchestrator.task_decomposer.subprocess.run") as run:
            run.return_value = _mock_claude_run(json.dumps({"type": "atomic"}))
            result = decompose_issue("Fix bug", "Some body")
        assert result == {"type": "atomic"}

    def test_epic_result(self):
        payload = {
            "type": "epic",
            "sub_issues": [
                {"title": "Part A", "body": "## Goal\n\nDo A"},
                {"title": "Part B", "body": "## Goal\n\nDo B"},
            ],
        }
        with mock.patch("orchestrator.task_decomposer.subprocess.run") as run:
            run.return_value = _mock_claude_run(json.dumps(payload))
            result = decompose_issue("Big feature", "Multiple things")
        assert result["type"] == "epic"
        assert len(result["sub_issues"]) == 2
        assert result["sub_issues"][0]["title"] == "Part A"

    def test_epic_capped_at_5(self):
        payload = {
            "type": "epic",
            "sub_issues": [
                {"title": f"Part {i}", "body": f"## Goal\n\nDo {i}"}
                for i in range(8)
            ],
        }
        with mock.patch("orchestrator.task_decomposer.subprocess.run") as run:
            run.return_value = _mock_claude_run(json.dumps(payload))
            result = decompose_issue("Huge epic", "Many things")
        assert result["type"] == "epic"
        assert len(result["sub_issues"]) == 5

    def test_single_sub_issue_treated_as_atomic(self):
        payload = {
            "type": "epic",
            "sub_issues": [{"title": "Only one", "body": "## Goal\n\nJust one"}],
        }
        with mock.patch("orchestrator.task_decomposer.subprocess.run") as run:
            run.return_value = _mock_claude_run(json.dumps(payload))
            result = decompose_issue("Not really epic", "One thing")
        assert result == {"type": "atomic"}

    def test_failure_returns_none(self):
        with mock.patch("orchestrator.task_decomposer.subprocess.run") as run:
            run.return_value = _mock_claude_run("", returncode=1)
            result = decompose_issue("Broken", "Body")
        assert result is None

    def test_invalid_json_returns_none(self):
        with mock.patch("orchestrator.task_decomposer.subprocess.run") as run:
            run.return_value = _mock_claude_run("not json at all")
            result = decompose_issue("Bad", "Body")
        assert result is None

    def test_timeout_returns_none(self):
        with mock.patch("orchestrator.task_decomposer.subprocess.run") as run:
            run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=60)
            result = decompose_issue("Slow", "Body")
        assert result is None

    def test_strips_markdown_fences(self):
        raw = '```json\n{"type": "atomic"}\n```'
        with mock.patch("orchestrator.task_decomposer.subprocess.run") as run:
            run.return_value = _mock_claude_run(raw)
            result = decompose_issue("Fenced", "Body")
        assert result == {"type": "atomic"}

    def test_missing_body_in_sub_issue_filtered(self):
        payload = {
            "type": "epic",
            "sub_issues": [
                {"title": "Good", "body": "## Goal\n\nOK"},
                {"title": "Bad", "body": ""},
                {"title": "Also good", "body": "## Goal\n\nOK2"},
            ],
        }
        with mock.patch("orchestrator.task_decomposer.subprocess.run") as run:
            run.return_value = _mock_claude_run(json.dumps(payload))
            result = decompose_issue("Mixed", "Body")
        assert result["type"] == "epic"
        assert len(result["sub_issues"]) == 2


# ---------------------------------------------------------------------------
# create_sub_issues
# ---------------------------------------------------------------------------

class TestCreateSubIssues:
    def test_creates_issues_with_parent_ref(self):
        with mock.patch("orchestrator.gh_project.gh") as mock_gh:
            mock_gh.return_value = "https://github.com/owner/repo/issues/42"
            created = create_sub_issues(
                "owner/repo", 10,
                [{"title": "Child 1", "body": "## Goal\n\nDo it"}],
            )
        assert len(created) == 1
        assert created[0]["number"] == 42
        # Verify body contains parent reference
        call_args = mock_gh.call_args[0][0]
        body_idx = call_args.index("--body") + 1
        assert "Part of #10" in call_args[body_idx]

    def test_passes_labels(self):
        with mock.patch("orchestrator.gh_project.gh") as mock_gh:
            mock_gh.return_value = "https://github.com/o/r/issues/5"
            create_sub_issues(
                "o/r", 1,
                [{"title": "T", "body": "B"}],
                labels=["prio:high"],
            )
        call_args = mock_gh.call_args[0][0]
        assert "--label" in call_args
        label_idx = call_args.index("--label") + 1
        assert call_args[label_idx] == "prio:high"

    def test_continues_on_failure(self):
        with mock.patch("orchestrator.gh_project.gh") as mock_gh:
            mock_gh.side_effect = [
                RuntimeError("boom"),
                "https://github.com/o/r/issues/7",
            ]
            created = create_sub_issues(
                "o/r", 1,
                [
                    {"title": "Fail", "body": "B1"},
                    {"title": "OK", "body": "B2"},
                ],
            )
        assert len(created) == 1
        assert created[0]["number"] == 7


import subprocess  # noqa: E402 — needed for TimeoutExpired in test
