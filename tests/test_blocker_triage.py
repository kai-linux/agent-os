"""Unit tests for orchestrator.blocker_triage."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from orchestrator import blocker_triage
from orchestrator.blocker_triage import (
    FIXED_BY_COMMIT_BLOCKERS,
    MAX_AUTO_RETRIES,
    TRIAGE_COOLDOWN_HOURS,
    TriageDecision,
    _latest_orchestrator_blocker,
    _parse_blocker_code,
    decide,
    triage_repo,
)


# ---------------------------------------------------------------------------
# _parse_blocker_code
# ---------------------------------------------------------------------------


def test_parse_blocker_code_extracts_lowercase_code():
    body = (
        "## Orchestrator update\n\n**Status:** `blocked`\n\n"
        "### Blocker code\n`Runner_Failure`\n"
    )
    assert _parse_blocker_code(body) == "runner_failure"


def test_parse_blocker_code_returns_none_when_missing():
    body = "## Orchestrator update\n\n**Status:** `blocked`\nSome prose.\n"
    assert _parse_blocker_code(body) is None


# ---------------------------------------------------------------------------
# _latest_orchestrator_blocker
# ---------------------------------------------------------------------------


def _comment(body: str, created_at: str) -> dict:
    return {"body": body, "createdAt": created_at}


def test_latest_orchestrator_blocker_picks_most_recent_blocked():
    comments = [
        _comment("## Orchestrator update\n**Status:** `complete`\n", "2026-04-20T10:00:00Z"),
        _comment(
            "## Orchestrator update\n**Status:** `blocked`\n\n### Blocker code\n`timeout`\n",
            "2026-04-21T08:00:00Z",
        ),
        _comment("unrelated human comment", "2026-04-21T09:00:00Z"),
    ]
    code, ts = _latest_orchestrator_blocker(comments)
    assert code == "timeout"
    assert ts == "2026-04-21T08:00:00Z"


def test_latest_orchestrator_blocker_returns_none_when_no_blocked_update():
    comments = [_comment("## Orchestrator update\n**Status:** `complete`\n", "2026-04-20T10:00:00Z")]
    code, ts = _latest_orchestrator_blocker(comments)
    assert code is None
    assert ts is None


# ---------------------------------------------------------------------------
# decide — retry cap
# ---------------------------------------------------------------------------


def test_decide_leaves_issue_when_retry_cap_reached(tmp_path):
    decision = decide("timeout", "2026-04-21T00:00:00Z", MAX_AUTO_RETRIES, tmp_path)
    assert decision.action == "leave"
    assert "cap" in decision.reason.lower()


# ---------------------------------------------------------------------------
# decide — no blocker code
# ---------------------------------------------------------------------------


def test_decide_leaves_issue_when_blocker_code_missing(tmp_path):
    decision = decide(None, "2026-04-21T00:00:00Z", 0, tmp_path)
    assert decision.action == "leave"
    assert "no machine-readable blocker" in decision.reason.lower()


# ---------------------------------------------------------------------------
# decide — transient infra cool-down
# ---------------------------------------------------------------------------


def test_decide_leaves_transient_during_cooldown(tmp_path):
    now = datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc)
    block_iso = (now - timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
    decision = decide("timeout", block_iso, 0, tmp_path, now=now)
    assert decision.action == "leave"
    assert "cool-down" in decision.reason.lower()


def test_decide_unblocks_transient_after_cooldown(tmp_path):
    now = datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc)
    block_iso = (now - timedelta(hours=TRIAGE_COOLDOWN_HOURS + 1)).isoformat().replace("+00:00", "Z")
    decision = decide("runner_failure", block_iso, 0, tmp_path, now=now)
    assert decision.action == "unblock"
    assert decision.new_label == "ready"
    assert "runner_failure" in decision.reason


# ---------------------------------------------------------------------------
# decide — operator-only blockers
# ---------------------------------------------------------------------------


def test_decide_leaves_operator_only_blockers(tmp_path):
    now = datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc)
    block_iso = (now - timedelta(days=2)).isoformat().replace("+00:00", "Z")
    for code in ("missing_credentials", "manual_intervention_required", "invalid_result_contract"):
        decision = decide(code, block_iso, 0, tmp_path, now=now)
        assert decision.action == "leave", code
        assert "operator action" in decision.reason.lower()


# ---------------------------------------------------------------------------
# decide — fixed-by-commit
# ---------------------------------------------------------------------------


def test_decide_unblocks_prompt_too_large_when_fix_landed_after_block(tmp_path):
    now = datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc)
    block_iso = "2026-04-21T08:00:00Z"
    fix_iso = "2026-04-21T10:00:00Z"

    def fake_run(cmd, capture_output, text, timeout):
        class R:
            returncode = 0
            stdout = fix_iso
            stderr = ""
        return R()

    with patch.object(subprocess, "run", side_effect=fake_run):
        decision = decide("prompt_too_large", block_iso, 0, tmp_path, now=now)
    assert decision.action == "unblock"
    assert "prompt" in decision.reason.lower() or "e2big" in decision.reason.lower()


def test_decide_leaves_prompt_too_large_when_fix_is_older_than_block(tmp_path):
    now = datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc)
    block_iso = "2026-04-21T10:00:00Z"
    old_commit_iso = "2026-04-19T08:00:00Z"

    def fake_run(cmd, capture_output, text, timeout):
        class R:
            returncode = 0
            stdout = old_commit_iso
            stderr = ""
        return R()

    with patch.object(subprocess, "run", side_effect=fake_run):
        decision = decide("prompt_too_large", block_iso, 0, tmp_path, now=now)
    assert decision.action == "leave"


def test_decide_leaves_prompt_too_large_when_commit_not_found(tmp_path):
    now = datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc)
    block_iso = "2026-04-21T08:00:00Z"

    def fake_run(cmd, capture_output, text, timeout):
        class R:
            returncode = 128
            stdout = ""
            stderr = "unknown revision"
        return R()

    with patch.object(subprocess, "run", side_effect=fake_run):
        decision = decide("prompt_too_large", block_iso, 0, tmp_path, now=now)
    assert decision.action == "leave"


# ---------------------------------------------------------------------------
# FIXED_BY_COMMIT_BLOCKERS sanity
# ---------------------------------------------------------------------------


def test_prompt_too_large_mapped_to_concrete_commit():
    assert "prompt_too_large" in FIXED_BY_COMMIT_BLOCKERS
    sha, reason = FIXED_BY_COMMIT_BLOCKERS["prompt_too_large"]
    assert len(sha) >= 7
    assert reason


# ---------------------------------------------------------------------------
# triage_repo — integration-ish (mocks gh + state IO)
# ---------------------------------------------------------------------------


def test_triage_repo_unblocks_transient_past_cooldown_and_caps_retries(tmp_path):
    now = datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc)
    old_block = (now - timedelta(hours=TRIAGE_COOLDOWN_HOURS + 5)).isoformat().replace("+00:00", "Z")
    issues = [{
        "number": 249,
        "title": "stuck",
        "labels": [{"name": "blocked"}],
        "url": "https://example/249",
        "updatedAt": old_block,
        "comments": [
            _comment(
                "## Orchestrator update\n**Status:** `blocked`\n\n### Blocker code\n`timeout`\n",
                old_block,
            ),
        ],
    }]

    edit_calls: list[dict] = []
    comment_calls: list[dict] = []

    def fake_list(repo):
        return list(issues)

    def fake_edit(repo, number, add=None, remove=None):
        edit_calls.append({"repo": repo, "number": number, "add": add, "remove": remove})

    def fake_comment(repo, number, body):
        comment_calls.append({"repo": repo, "number": number, "body": body})

    def fake_decide(blocker_code, block_time_iso, retry_count, agent_os_root, now=None):
        return decide(blocker_code, block_time_iso, retry_count, agent_os_root, now=now_override)

    now_override = now

    with patch.object(blocker_triage, "_list_blocked_issues", side_effect=fake_list), \
            patch.object(blocker_triage, "edit_issue_labels", side_effect=fake_edit), \
            patch.object(blocker_triage, "add_issue_comment", side_effect=fake_comment), \
            patch.object(blocker_triage, "decide", side_effect=fake_decide):
        stats = triage_repo({}, "kai-linux/agent-os", tmp_path)

    assert stats == {"considered": 1, "unblocked": 1, "left": 0, "errors": 0}
    assert edit_calls == [{
        "repo": "kai-linux/agent-os", "number": 249,
        "add": ["ready"], "remove": ["blocked"],
    }]
    assert len(comment_calls) == 1
    assert "Blocker triage" in comment_calls[0]["body"]

    state_file = tmp_path / "runtime" / "state" / "blocker_triage.json"
    assert state_file.exists()
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["kai-linux/agent-os"]["249"]["retries"] == 1

    # Second pass with same issue still blocked: retries should stop at the cap.
    with patch.object(blocker_triage, "_list_blocked_issues", side_effect=fake_list), \
            patch.object(blocker_triage, "edit_issue_labels", side_effect=fake_edit), \
            patch.object(blocker_triage, "add_issue_comment", side_effect=fake_comment), \
            patch.object(blocker_triage, "decide", side_effect=fake_decide):
        triage_repo({}, "kai-linux/agent-os", tmp_path)
        triage_repo({}, "kai-linux/agent-os", tmp_path)

    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["kai-linux/agent-os"]["249"]["retries"] == MAX_AUTO_RETRIES
    # Third attempt must have been a leave — edit_calls length capped.
    assert len(edit_calls) == MAX_AUTO_RETRIES


def test_triage_repo_leaves_issue_with_operator_blocker(tmp_path):
    now = datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc)
    issues = [{
        "number": 300,
        "title": "needs creds",
        "labels": [{"name": "blocked"}],
        "url": "https://example/300",
        "updatedAt": now.isoformat(),
        "comments": [
            _comment(
                "## Orchestrator update\n**Status:** `blocked`\n\n### Blocker code\n`missing_credentials`\n",
                (now - timedelta(days=2)).isoformat().replace("+00:00", "Z"),
            ),
        ],
    }]

    edit_calls: list = []

    with patch.object(blocker_triage, "_list_blocked_issues", return_value=issues), \
            patch.object(blocker_triage, "edit_issue_labels", side_effect=lambda *a, **k: edit_calls.append((a, k))), \
            patch.object(blocker_triage, "add_issue_comment"):
        stats = triage_repo({}, "kai-linux/agent-os", tmp_path)

    assert stats["left"] == 1
    assert stats["unblocked"] == 0
    assert edit_calls == []
