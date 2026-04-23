"""Regression coverage for the orphan-branch PR recreation loop.

Incident 2026-04-23: liminalconsultants PR #44 was deliberately closed by
the operator as superseded. Within the same pr_monitor cycle,
`_create_prs_for_orphan_branches` saw the agent/* branch had commits
ahead of main and no open PR, and re-opened it as PR #57. The loop
would have continued indefinitely until the branch was manually
deleted. The check must skip branches whose recent PR was closed-not-merged.
"""
from __future__ import annotations

from unittest.mock import patch

from orchestrator.pr_monitor import _find_closed_pr_for_task


def test_find_closed_pr_returns_closed_unmerged_pr():
    fake_prs = [
        {"number": 44, "title": "Agent: task-1", "mergedAt": None, "headRefName": "agent/task-1", "url": "u1"},
    ]
    with patch("orchestrator.pr_monitor.gh_json", return_value=fake_prs):
        result = _find_closed_pr_for_task("owner/repo", "task-1")
    assert result is not None
    assert result["number"] == 44


def test_find_closed_pr_skips_merged_prs():
    fake_prs = [
        {"number": 44, "title": "Agent: task-1", "mergedAt": "2026-04-23T18:00:00Z", "headRefName": "agent/task-1", "url": "u1"},
    ]
    with patch("orchestrator.pr_monitor.gh_json", return_value=fake_prs):
        result = _find_closed_pr_for_task("owner/repo", "task-1")
    assert result is None


def test_find_closed_pr_returns_none_when_no_match():
    with patch("orchestrator.pr_monitor.gh_json", return_value=[]):
        result = _find_closed_pr_for_task("owner/repo", "task-1")
    assert result is None


def test_find_closed_pr_filters_by_exact_title():
    fake_prs = [
        {"number": 1, "title": "Agent: task-1-other", "mergedAt": None, "headRefName": "x", "url": "u"},
        {"number": 2, "title": "Agent: task-1", "mergedAt": None, "headRefName": "y", "url": "u"},
    ]
    with patch("orchestrator.pr_monitor.gh_json", return_value=fake_prs):
        result = _find_closed_pr_for_task("owner/repo", "task-1")
    assert result is not None
    assert result["number"] == 2
