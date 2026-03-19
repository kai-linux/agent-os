"""Unit tests for helpers in orchestrator/pr_monitor.py and gh_project.py."""
import json
import sys
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator import gh_project
from orchestrator import pr_monitor as pm
from orchestrator.pr_monitor import _checks_all_passed, _checks_any_failed, _extract_issue_number


# ---------------------------------------------------------------------------
# _checks_all_passed
# ---------------------------------------------------------------------------

def test_checks_all_passed_empty():
    assert _checks_all_passed([]) is False


def test_checks_all_passed_success():
    checks = [{"state": "SUCCESS", "bucket": "pass"}]
    assert _checks_all_passed(checks) is True


def test_checks_all_passed_pending():
    checks = [
        {"state": "SUCCESS", "bucket": "pass"},
        {"state": "PENDING", "bucket": ""},
    ]
    assert _checks_all_passed(checks) is False


def test_checks_all_passed_skipped_neutral():
    checks = [
        {"state": "SKIPPED", "bucket": "pass"},
        {"state": "NEUTRAL", "bucket": "pass"},
    ]
    assert _checks_all_passed(checks) is True


def test_checks_all_passed_failure():
    checks = [{"state": "FAILURE", "bucket": "fail"}]
    assert _checks_all_passed(checks) is False


# ---------------------------------------------------------------------------
# _checks_any_failed
# ---------------------------------------------------------------------------

def test_checks_any_failed_none():
    assert _checks_any_failed([]) is False
    assert _checks_any_failed([{"state": "SUCCESS", "bucket": "pass"}]) is False


def test_checks_any_failed_one_failure():
    checks = [
        {"state": "SUCCESS", "bucket": "pass"},
        {"state": "FAILURE", "bucket": "fail"},
    ]
    assert _checks_any_failed(checks) is True


def test_checks_any_failed_bucket_fail():
    assert _checks_any_failed([{"state": "ERROR", "bucket": "fail"}]) is True


def test_format_failed_checks_uses_state_and_link():
    checks = [
        {"name": "test", "state": "FAILURE", "bucket": "fail", "link": "https://example.com/run/1"},
        {"name": "lint", "state": "SUCCESS", "bucket": "pass"},
    ]
    lines = pm._format_failed_checks(checks)
    assert "**test**" in lines
    assert "`failure`" in lines
    assert "https://example.com/run/1" in lines


def test_ensure_ci_remediation_issue_creates_ready_debugging_issue(monkeypatch):
    created = {}

    def fake_find(repo, title):
        return None

    def fake_create(repo, title, body, labels):
        created["repo"] = repo
        created["title"] = title
        created["body"] = body
        created["labels"] = labels
        return "https://github.com/owner/repo/issues/99"

    ready_calls = []

    monkeypatch.setattr(pm, "_find_open_issue_by_title", fake_find)
    monkeypatch.setattr(pm, "_create_issue", fake_create)
    monkeypatch.setattr(pm, "_set_issue_ready", lambda cfg, repo, url: ready_calls.append((repo, url)))

    cfg = {
        "github_owner": "owner",
        "github_projects": {
            "proj": {
                "project_number": 1,
                "ready_value": "Ready",
                "repos": [{"github_repo": "owner/repo", "local_repo": "/tmp/repo"}],
            }
        },
    }
    pr = {"number": 34, "url": "https://github.com/owner/repo/pull/34", "headRefName": "agent/task-123"}
    checks = [{"name": "test", "state": "FAILURE", "bucket": "fail"}]

    url, created_new = pm._ensure_ci_remediation_issue(cfg, "owner/repo", pr, checks, 12)

    assert created_new is True
    assert url == "https://github.com/owner/repo/issues/99"
    assert created["title"] == "Fix CI failure on PR #34"
    assert "## Task Type\ndebugging" in created["body"]
    assert "## Base Branch\nagent/task-123" in created["body"]
    assert "## Branch\nagent/task-123" in created["body"]
    assert ready_calls == [("owner/repo", "https://github.com/owner/repo/issues/99")]


def test_cleanup_merged_pr_issues_marks_original_and_remediation_done(monkeypatch):
    done_calls = []

    monkeypatch.setattr(pm, "_extract_issue_number", lambda body: 24)
    monkeypatch.setattr(pm, "_find_open_issue_by_title", lambda repo, title: {"number": 35, "title": title})
    monkeypatch.setattr(
        pm,
        "_mark_issue_done",
        lambda cfg, repo, issue_number, close_issue, comment=None: done_calls.append((repo, issue_number, close_issue, comment)),
    )

    pm._cleanup_merged_pr_issues({}, "owner/repo", {"number": 34, "body": "Fixes #24"})

    assert done_calls[0][1] == 24
    assert done_calls[0][2] is True
    assert "PR #34 merged successfully" in done_calls[0][3]
    assert done_calls[1][1] == 35
    assert done_calls[1][2] is True
    assert "Resolved automatically after PR #34 merged" in done_calls[1][3]


# ---------------------------------------------------------------------------
# _extract_issue_number
# ---------------------------------------------------------------------------

def test_extract_issue_number():
    assert _extract_issue_number("Automated changes for issue #42") == 42
    assert _extract_issue_number("no number here") is None
    assert _extract_issue_number("") is None
    assert _extract_issue_number("closes #7 and #9") == 7


def test_create_pr_for_branch_lists_created_pr(monkeypatch):
    calls = []

    def fake_gh(cmd, *, check=True):
        calls.append(cmd)
        if cmd[:3] == ["pr", "create", "-R"]:
            return "created"
        if cmd[:3] == ["pr", "list", "-R"]:
            return json.dumps([{"url": "https://github.com/kai-linux/agent-os/pull/123"}])
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(gh_project, "gh", fake_gh)
    monkeypatch.setattr(gh_project, "gh_json", lambda cmd: json.loads(fake_gh(cmd)))

    pr_url = gh_project.create_pr_for_branch(
        "kai-linux/agent-os",
        "agent/task-123",
        "Agent: task-123",
        "Automated changes.",
    )

    assert pr_url == "https://github.com/kai-linux/agent-os/pull/123"
    assert calls[0] == [
        "pr", "create", "-R", "kai-linux/agent-os",
        "--head", "agent/task-123",
        "--title", "Agent: task-123",
        "--body", "Automated changes.",
    ]


def test_create_pr_for_branch_recovers_when_pr_already_exists(monkeypatch):
    create_error = RuntimeError("a pull request for branch already exists")
    gh_mock = Mock(side_effect=create_error)
    gh_json_mock = Mock(return_value=[{"url": "https://github.com/kai-linux/agent-os/pull/456"}])

    monkeypatch.setattr(gh_project, "gh", gh_mock)
    monkeypatch.setattr(gh_project, "gh_json", gh_json_mock)

    pr_url = gh_project.create_pr_for_branch(
        "kai-linux/agent-os",
        "agent/task-456",
        "Agent: task-456",
        "Automated changes.",
    )

    assert pr_url == "https://github.com/kai-linux/agent-os/pull/456"
    gh_json_mock.assert_called_once_with([
        "pr", "list", "-R", "kai-linux/agent-os",
        "--head", "agent/task-456",
        "--state", "open",
        "--json", "url",
    ])
