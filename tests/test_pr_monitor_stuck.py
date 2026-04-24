"""Regression coverage for the stuck-PR self-heal path.

Incident 2026-04-21: work_verifier.py crashed mid-poll on a ``.format``
KeyError. Each pr_monitor run incremented ``attempts`` before dying, so
liminalconsultants#32 hit ``MAX_MERGE_ATTEMPTS`` without ever reaching
the merge call. The agent reported "Complete" on Telegram, but the PR
was silently stuck open and the 9 image assets never landed on main.

``_handle_stuck_merge_attempts`` is the recovery primitive: when the
counter caps but the PR is still cleanly mergeable with no active
remediation issue, surface one Telegram alert and auto-reset the
counter once. On a second cap after the auto-reset, alert again but
leave it for the operator.
"""
from __future__ import annotations

from copy import deepcopy
from unittest.mock import MagicMock

from orchestrator import pr_monitor
from orchestrator.pr_risk_assessment import RiskAssessment, RiskSignal


def _make_pr(mergeable: str = "MERGEABLE", merge_state: str = "CLEAN") -> dict:
    return {
        "number": 32,
        "url": "https://github.com/kai-linux/liminalconsultants/pull/32",
        "mergeable": mergeable,
        "mergeStateStatus": merge_state,
    }


def test_first_stuck_cycle_alerts_and_auto_resets(monkeypatch):
    routed: list = []
    saved_states: list = []

    monkeypatch.setattr(pr_monitor, "_find_open_issue_by_title", lambda *a, **kw: None)
    monkeypatch.setattr(pr_monitor, "classify_severity", lambda *a, **kw: "warning")
    monkeypatch.setattr(pr_monitor, "route_incident", lambda sev, ev, cfg=None: routed.append((sev, ev)))
    monkeypatch.setattr(pr_monitor, "_save_state", lambda paths, state: saved_states.append(dict(state)))

    pr = _make_pr()
    pr_state = {"attempts": 3}
    full_state = {pr["url"]: pr_state}

    pr_monitor._handle_stuck_merge_attempts(
        cfg={},
        repo="kai-linux/liminalconsultants",
        pr=pr,
        pr_state=pr_state,
        paths={},
        state=full_state,
    )

    assert pr_state["attempts"] == 0
    assert pr_state["auto_reset_used"] is True
    assert len(routed) == 1
    _, event = routed[0]
    assert event["type"] == "stuck_pr_merge"
    assert event["stage"] == "self_heal"
    assert event["pr_number"] == 32
    assert "auto-resetting" in event["summary"]
    assert saved_states, "state should be persisted after reset"


def test_second_stuck_cycle_alerts_without_resetting(monkeypatch):
    routed: list = []
    saves: list = []

    monkeypatch.setattr(pr_monitor, "_find_open_issue_by_title", lambda *a, **kw: None)
    monkeypatch.setattr(pr_monitor, "classify_severity", lambda *a, **kw: "warning")
    monkeypatch.setattr(pr_monitor, "route_incident", lambda sev, ev, cfg=None: routed.append(ev))
    monkeypatch.setattr(pr_monitor, "_save_state", lambda paths, state: saves.append(1))

    pr = _make_pr()
    pr_state = {"attempts": 3, "auto_reset_used": True}

    pr_monitor._handle_stuck_merge_attempts(
        cfg={},
        repo="kai-linux/liminalconsultants",
        pr=pr,
        pr_state=pr_state,
        paths={},
        state={pr["url"]: pr_state},
    )

    assert pr_state["attempts"] == 3  # unchanged — operator must intervene
    assert pr_state["auto_reset_used"] is True
    assert len(routed) == 1
    assert routed[0]["stage"] == "escalate"
    assert "operator review required" in routed[0]["summary"]
    assert not saves, "no auto-reset means no state save needed"


def test_stuck_pr_with_active_remediation_issue_is_not_self_healed(monkeypatch):
    routed: list = []
    monkeypatch.setattr(
        pr_monitor, "_find_open_issue_by_title",
        lambda *a, **kw: {"number": 99, "title": "Fix CI failure on PR #32"},
    )
    monkeypatch.setattr(pr_monitor, "route_incident", lambda *a, **kw: routed.append(1))

    pr = _make_pr()
    pr_state = {"attempts": 3}
    pr_monitor._handle_stuck_merge_attempts(
        cfg={}, repo="kai-linux/liminalconsultants", pr=pr,
        pr_state=pr_state, paths={}, state={pr["url"]: pr_state},
    )

    # A remediation issue already owns this PR — don't spam, don't reset.
    assert pr_state["attempts"] == 3
    assert "auto_reset_used" not in pr_state
    assert not routed


def test_stuck_pr_that_is_dirty_is_left_alone(monkeypatch):
    routed: list = []
    monkeypatch.setattr(pr_monitor, "_find_open_issue_by_title", lambda *a, **kw: None)
    monkeypatch.setattr(pr_monitor, "route_incident", lambda *a, **kw: routed.append(1))

    pr = _make_pr(mergeable="CONFLICTING", merge_state="DIRTY")
    pr_state = {"attempts": 3}
    pr_monitor._handle_stuck_merge_attempts(
        cfg={}, repo="repo/x", pr=pr, pr_state=pr_state, paths={},
        state={pr["url"]: pr_state},
    )

    # DIRTY PRs are a real problem — stuck attempts are the right signal.
    # Conflict rebase / escalation is handled elsewhere; don't self-heal here.
    assert pr_state["attempts"] == 3
    assert "auto_reset_used" not in pr_state
    assert not routed


def test_stuck_pr_alert_is_deduped_per_stage():
    # The dedup_key includes the stage so self_heal and escalate alerts are
    # separate entries but repeated stuck cycles at the same stage collapse.
    routed = []

    class _Cfg:
        def get(self, *a, **kw):
            return None

    ev1 = {
        "source": "pr_monitor",
        "type": "stuck_pr_merge",
        "repo": "r/x",
        "pr_number": 32,
        "stage": "self_heal",
        "dedup_key": "stuck-pr:r/x:32:self_heal",
    }
    ev2 = {
        "source": "pr_monitor",
        "type": "stuck_pr_merge",
        "repo": "r/x",
        "pr_number": 32,
        "stage": "escalate",
        "dedup_key": "stuck-pr:r/x:32:escalate",
    }
    assert ev1["dedup_key"] != ev2["dedup_key"]


def test_monitor_prs_work_verifier_block_clears_poisoned_attempts(monkeypatch, tmp_path):
    pr = {
        "number": 32,
        "url": "https://github.com/owner/repo/pull/32",
        "title": "Agent: task-123",
        "body": "Automated changes from agent branch `agent/task-123`.\n\n## Original Task ID\ntask-123\n",
        "headRefName": "agent/task-123",
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "isDraft": False,
    }
    state = {pr["url"]: {"attempts": 3}}
    saves: list[dict] = []
    stuck_calls: list[int] = []
    merge_calls: list[int] = []

    monkeypatch.setattr(
        pr_monitor,
        "load_config",
        lambda: {
            "github_projects": {
                "demo": {
                    "repos": [{"github_repo": "owner/repo", "key": "demo"}],
                }
            }
        },
    )
    monkeypatch.setattr(pr_monitor, "runtime_paths", lambda cfg: {"ROOT": tmp_path, "LOGS": tmp_path})
    monkeypatch.setattr(pr_monitor, "_load_state", lambda paths: state)
    monkeypatch.setattr(pr_monitor, "_save_state", lambda paths, current: saves.append(deepcopy(current)))
    monkeypatch.setattr(pr_monitor, "_close_fork_prs", lambda repos: None)
    monkeypatch.setattr(pr_monitor, "_close_stale_redundant_agent_prs", lambda repo: False)
    monkeypatch.setattr(pr_monitor, "_create_prs_for_orphan_branches", lambda cfg, repos: None)
    monkeypatch.setattr(pr_monitor, "_cleanup_stale_ci_remediation_issues", lambda cfg, repo, current: False)
    monkeypatch.setattr(pr_monitor, "_list_agent_prs", lambda repo: [pr])
    monkeypatch.setattr(pr_monitor, "_repo_has_active_workflows", lambda repo: True)
    monkeypatch.setattr(pr_monitor, "_get_pr_checks", lambda repo, pr_number: [{"name": "test", "state": "SUCCESS", "bucket": "pass"}])
    monkeypatch.setattr(pr_monitor, "_reconcile_open_pr_state", lambda cfg, repo, current_pr, checks, current: False)
    monkeypatch.setattr(
        pr_monitor,
        "assess_pr_risk",
        lambda repo, pr_number: RiskAssessment(
            level="low",
            files_changed=2,
            lines_changed=8,
            has_source_changes=True,
            has_test_changes=True,
        ),
    )
    monkeypatch.setattr(pr_monitor, "_post_risk_comment", lambda *args, **kwargs: None)
    monkeypatch.setattr(pr_monitor, "_send_risk_telegram", lambda *args, **kwargs: None)
    monkeypatch.setattr(pr_monitor, "_quality_harness_gate", lambda *args, **kwargs: (True, "passed"))
    monkeypatch.setattr(pr_monitor, "_work_verifier_gate", lambda *args, **kwargs: (False, "missing linked issue"))
    monkeypatch.setattr(pr_monitor, "_handle_stuck_merge_attempts", lambda *args, **kwargs: stuck_calls.append(1))
    monkeypatch.setattr(pr_monitor, "_try_merge", lambda *args, **kwargs: merge_calls.append(1) or True)
    monkeypatch.setattr(pr_monitor, "generate_followup_issues", lambda cfg, repo: [])
    monkeypatch.setattr(pr_monitor, "_prompt_labeled_field_failures", lambda cfg, repo: None)
    monkeypatch.setattr("orchestrator.control_state.is_repo_disabled", lambda *args, **kwargs: False)


def test_send_risk_telegram_creates_high_risk_approval(tmp_path, monkeypatch):
    sent = []

    monkeypatch.setattr("orchestrator.queue.send_telegram", lambda cfg, text: sent.append(text) or 321)
    cfg = {"root_dir": str(tmp_path), "telegram_chat_id": "-100123"}
    risk = RiskAssessment(
        level="high",
        signals=[RiskSignal(category="large_diff", severity="high", detail="large risky diff")],
        files_changed=9,
        lines_changed=900,
        has_source_changes=True,
        has_test_changes=False,
    )

    pr_monitor._send_risk_telegram(cfg, "owner/repo", 77, risk)

    assert sent
    approval_files = list((tmp_path / "runtime" / "approvals").glob("approval-*.md"))
    assert len(approval_files) == 1
    text = approval_files[0].read_text(encoding="utf-8")
    assert "kind: high_risk_pr" in text
    assert "pr_number: 77" in text
