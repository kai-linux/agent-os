"""Unit tests for helpers in orchestrator/pr_monitor.py and gh_project.py."""
import json
import sys
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator import gh_project
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
