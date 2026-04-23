"""Regression coverage for `_branch_has_commits_ahead_of_main` honoring
the actual default branch reported by GitHub.

Discovered while diagnosing the 2026-04-23 stuck-PR incident. The
function previously hardcoded `base="main"`, so on repos that default
to `master` (e.g. eigendark) the GitHub compare endpoint returns 404 →
exception caught → False → orphan check treats the agent branch as
"fully merged" and **deletes a valid branch**. The lookup now consults
GitHub for the actual default branch.
"""
from __future__ import annotations

from unittest.mock import patch

import orchestrator.pr_monitor as pr_monitor


def test_remote_default_branch_returns_master_for_master_repo():
    pr_monitor._remote_default_branch.cache_clear()
    with patch("orchestrator.pr_monitor.gh", return_value="master"):
        assert pr_monitor._remote_default_branch("kai-linux/eigendark") == "master"


def test_remote_default_branch_falls_back_to_main_on_failure():
    pr_monitor._remote_default_branch.cache_clear()
    with patch("orchestrator.pr_monitor.gh", side_effect=RuntimeError("api down")):
        assert pr_monitor._remote_default_branch("kai-linux/anything") == "main"


def test_branch_has_commits_uses_actual_default_branch():
    pr_monitor._remote_default_branch.cache_clear()
    captured: list[list[str]] = []

    def fake_gh(args, check=False):
        captured.append(args)
        if "/compare/" in args[1]:
            return "1"
        if args[1].startswith("repos/") and "default_branch" in (args[-1] if args else ""):
            return "master"
        return "master"

    with patch("orchestrator.pr_monitor.gh", side_effect=fake_gh):
        assert pr_monitor._branch_has_commits_ahead_of_main("kai-linux/eigendark", "agent/foo") is True

    compare_calls = [a for a in captured if "/compare/" in a[1]]
    assert compare_calls, "expected a compare API call"
    assert "master...agent/foo" in compare_calls[0][1]
