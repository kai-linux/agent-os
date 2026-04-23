"""Coverage for pr_monitor's e2e PR health management.

The 2026-04-23 stuck-PR incident sat for hours because the cron loop kept
retrying the same failed remediation (auto-rebase that always reverted because
the post-rebase pytest failed on a JS repo). The user wants pr_monitor to
manage PR health end-to-end without operator intervention: when a PR is
wedged on the same root cause for too long, close it and let the dispatcher
re-spawn from main.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import orchestrator.pr_monitor as pm


def _hours_ago_iso(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()


def test_blocker_signature_detects_conflict():
    pr = {"mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY"}
    assert pm._pr_blocker_signature(pr, [], None) == "merge_conflict"


def test_blocker_signature_detects_ci_failure():
    pr = {"mergeable": "MERGEABLE", "mergeStateStatus": "BLOCKED"}
    checks = [{"name": "test", "conclusion": "FAILURE"}, {"name": "lint", "conclusion": "SUCCESS"}]
    assert pm._pr_blocker_signature(pr, checks, None) == "ci_failure:test"


def test_blocker_signature_detects_verifier_block():
    pr = {"mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN"}
    checks = [{"name": "test", "conclusion": "SUCCESS"}]
    assert pm._pr_blocker_signature(pr, checks, "scope_mismatch_v1") == "verifier_block:scope_mismatch_v1"


def test_blocker_signature_empty_when_clean_and_passing():
    pr = {"mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN"}
    checks = [{"name": "test", "conclusion": "SUCCESS"}]
    assert pm._pr_blocker_signature(pr, checks, None) == ""


def test_track_remediate_starts_clock_on_first_block(tmp_path, monkeypatch):
    pr = {"number": 42, "url": "u", "mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY"}
    pr_state = {}
    state = {"u": pr_state}
    paths = {"state_dir": tmp_path}
    monkeypatch.setattr(pm, "_save_state", lambda *a, **kw: None)

    took_action = pm._e2e_pr_health_track_and_remediate(
        {}, "owner/repo", pr, pr_state, paths, state, []
    )
    assert took_action is False
    assert pr_state["blocker_signature"] == "merge_conflict"
    assert "blocker_first_seen" in pr_state


def test_track_remediate_no_action_under_threshold(tmp_path, monkeypatch):
    pr = {"number": 42, "url": "u", "mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY"}
    pr_state = {
        "blocker_signature": "merge_conflict",
        "blocker_first_seen": _hours_ago_iso(1.0),
    }
    monkeypatch.setattr(pm, "_save_state", lambda *a, **kw: None)
    took_action = pm._e2e_pr_health_track_and_remediate(
        {}, "owner/repo", pr, pr_state, {}, {"u": pr_state}, []
    )
    assert took_action is False
    assert pr_state["blocker_signature"] == "merge_conflict"


def test_track_remediate_resets_clock_when_blocker_changes(tmp_path, monkeypatch):
    pr = {"number": 42, "url": "u", "mergeable": "MERGEABLE", "mergeStateStatus": "BLOCKED"}
    pr_state = {
        "blocker_signature": "merge_conflict",
        "blocker_first_seen": _hours_ago_iso(10.0),
    }
    monkeypatch.setattr(pm, "_save_state", lambda *a, **kw: None)
    checks = [{"name": "test", "conclusion": "FAILURE"}]
    took_action = pm._e2e_pr_health_track_and_remediate(
        {}, "owner/repo", pr, pr_state, {}, {"u": pr_state}, checks
    )
    assert took_action is False  # signature changed → clock reset, no terminal action
    assert pr_state["blocker_signature"] == "ci_failure:test"


def test_track_remediate_clears_state_when_unblocked(tmp_path, monkeypatch):
    pr = {"number": 42, "url": "u", "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN"}
    pr_state = {
        "blocker_signature": "merge_conflict",
        "blocker_first_seen": _hours_ago_iso(10.0),
    }
    monkeypatch.setattr(pm, "_save_state", lambda *a, **kw: None)
    pm._e2e_pr_health_track_and_remediate(
        {}, "owner/repo", pr, pr_state, {}, {"u": pr_state}, [{"name": "t", "conclusion": "SUCCESS"}]
    )
    assert "blocker_signature" not in pr_state


def test_track_remediate_terminal_close_after_threshold(tmp_path, monkeypatch):
    pr = {"number": 42, "url": "u", "mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY"}
    pr_state = {
        "blocker_signature": "merge_conflict",
        "blocker_first_seen": _hours_ago_iso(pm._STUCK_PR_TERMINAL_HOURS + 0.5),
    }
    monkeypatch.setattr(pm, "_save_state", lambda *a, **kw: None)
    monkeypatch.setattr(pm, "append_audit_event", lambda *a, **kw: None)

    gh_calls = []

    def fake_gh(args, check=False):
        gh_calls.append(args)
        return ""

    monkeypatch.setattr(pm, "gh", fake_gh)
    took_action = pm._e2e_pr_health_track_and_remediate(
        {}, "owner/repo", pr, pr_state, {}, {"u": pr_state}, []
    )
    assert took_action is True
    assert any("comment" in args for args in gh_calls)
    assert any("close" in args and "--delete-branch" in args for args in gh_calls)
    assert pr_state["e2e_terminal_close_count"] == 1
    assert "blocker_signature" not in pr_state
