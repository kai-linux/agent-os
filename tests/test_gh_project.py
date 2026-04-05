"""Unit tests for helpers in orchestrator/pr_monitor.py and gh_project.py."""
import json
import sys
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator import gh_project
from orchestrator import pr_monitor as pm
from orchestrator.pr_monitor import (
    _checks_all_passed,
    _checks_any_failed,
    _extract_issue_number,
    _get_conflicted_files,
    _try_union_resolve,
)


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

    from orchestrator.ci_artifact_validator import ArtifactValidation
    monkeypatch.setattr(pm, "validate_ci_artifacts", lambda repo, checks: ArtifactValidation(
        valid=True, run_id=555, artifacts=[{"name": "pr-ci-failure-test-1", "size_in_bytes": 5000}],
        total_bytes=5000,
    ))

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
    descendant_calls = []
    monkeypatch.setattr(
        pm,
        "_mark_issue_done",
        lambda cfg, repo, issue_number, close_issue, comment=None: done_calls.append((repo, issue_number, close_issue, comment)),
    )
    monkeypatch.setattr(
        pm,
        "_cleanup_descendant_followup_issues",
        lambda cfg, repo, remediation_issue_number, pr_number, branch: descendant_calls.append((repo, remediation_issue_number, pr_number, branch)),
    )

    pm._cleanup_merged_pr_issues({}, "owner/repo", {"number": 34, "body": "Fixes #24", "headRefName": "agent/task-24"})

    assert done_calls[0][1] == 24
    assert done_calls[0][2] is True
    assert "PR #34 merged successfully" in done_calls[0][3]
    assert done_calls[1][1] == 35
    assert done_calls[1][2] is True
    assert "Resolved automatically after PR #34 merged" in done_calls[1][3]
    assert descendant_calls == [("owner/repo", 35, 34, "agent/task-24")]


def test_cleanup_stale_ci_remediation_issues_for_merged_pr(monkeypatch):
    monkeypatch.setattr(
        pm,
        "_list_open_ci_remediation_issues",
        lambda repo: [{"number": 35, "title": "Fix CI failure on PR #34", "url": "https://github.com/owner/repo/issues/35"}],
    )
    monkeypatch.setattr(
        pm,
        "_get_pr",
        lambda repo, pr_number: {
            "number": pr_number,
            "url": "https://github.com/owner/repo/pull/34",
            "body": "Fixes #24",
            "state": "MERGED",
            "mergedAt": "2026-03-19T18:00:00Z",
        },
    )
    cleaned = []
    monkeypatch.setattr(pm, "_cleanup_merged_pr_issues", lambda cfg, repo, pr: cleaned.append((repo, pr["number"])))

    state = {"https://github.com/owner/repo/pull/34": {"attempts": 3}}
    changed = pm._cleanup_stale_ci_remediation_issues({}, "owner/repo", state)

    assert changed is True
    assert cleaned == [("owner/repo", 34)]
    assert state == {}


def test_reconcile_open_pr_state_keeps_source_issue_in_progress(monkeypatch):
    calls = []
    monkeypatch.setattr(pm, "_extract_issue_number", lambda body: 64)
    monkeypatch.setattr(
        pm,
        "_mark_issue_in_progress",
        lambda cfg, repo, issue_number, reopen_issue, comment=None: calls.append((repo, issue_number, reopen_issue)),
    )
    monkeypatch.setattr(pm, "_find_issue_by_title", lambda repo, title, state="open": None)

    changed = pm._reconcile_open_pr_state(
        {},
        "owner/repo",
        {"number": 71, "url": "https://github.com/owner/repo/pull/71", "body": "Fixes #64"},
        [{"name": "test", "state": "SUCCESS", "bucket": "pass"}],
        {},
    )

    assert changed is False
    assert calls == [("owner/repo", 64, True)]


def test_reconcile_open_pr_state_reopens_closed_remediation_and_clears_attempts(monkeypatch):
    source_calls = []
    ready_calls = []
    monkeypatch.setattr(pm, "_extract_issue_number", lambda body: 64)
    monkeypatch.setattr(
        pm,
        "_mark_issue_in_progress",
        lambda cfg, repo, issue_number, reopen_issue, comment=None: source_calls.append((repo, issue_number, reopen_issue)),
    )
    monkeypatch.setattr(
        pm,
        "_find_issue_by_title",
        lambda repo, title, state="open": {
            "number": 73,
            "title": title,
            "state": "CLOSED",
        } if state == "all" else None,
    )
    monkeypatch.setattr(
        pm,
        "_mark_issue_ready",
        lambda cfg, repo, issue_number, reopen_issue, comment=None: ready_calls.append((repo, issue_number, reopen_issue, comment)),
    )
    state = {"https://github.com/owner/repo/pull/71": {"attempts": 3}}

    changed = pm._reconcile_open_pr_state(
        {},
        "owner/repo",
        {"number": 71, "url": "https://github.com/owner/repo/pull/71", "body": "Fixes #64"},
        [{"name": "test", "state": "FAILURE", "bucket": "fail"}],
        state,
    )

    assert changed is True
    assert source_calls == [("owner/repo", 64, True)]
    assert ready_calls and ready_calls[0][1] == 73
    assert "PR #71 is still failing CI" in ready_calls[0][3]
    assert state == {}


def test_cleanup_descendant_followup_issues_closes_matching_branch_chain(monkeypatch):
    issues = [
        {
            "number": 102,
            "title": "Follow up partial debug for root issue #99",
            "body": "## Root Issue Number\n99\n\n## Root PR Number\n98\n\n## Root Branch\nagent/task-99\n",
        },
        {
            "number": 103,
            "title": "Follow up partial debug for root issue #50",
            "body": "## Root Issue Number\n50\n\n## RootBranch\nagent/other\n",
        },
        {
            "number": 104,
            "title": "Follow up partial debug for root issue #77",
            "body": "## Branch\nagent/task-99\n",
        },
    ]
    monkeypatch.setattr(pm, "_list_followup_debug_issues", lambda repo, state="open": issues)
    done_calls = []
    monkeypatch.setattr(
        pm,
        "_mark_issue_done",
        lambda cfg, repo, issue_number, close_issue, comment=None: done_calls.append((issue_number, close_issue, comment)),
    )

    closed = pm._cleanup_descendant_followup_issues({}, "owner/repo", remediation_issue_number=99, pr_number=98, branch="agent/task-99")

    assert closed == 2
    assert done_calls[0][0] == 102
    assert done_calls[1][0] == 104
    assert all(call[1] is True for call in done_calls)


def test_close_stale_redundant_agent_prs_closes_second_pr_after_merge(monkeypatch):
    monkeypatch.setattr(
        pm,
        "_list_agent_prs",
        lambda repo: [
            {"number": 107, "title": "Agent: task-123", "headRefName": "agent/task-123"},
            {"number": 108, "title": "Agent: task-999", "headRefName": "agent/task-999"},
        ],
    )
    monkeypatch.setattr(
        pm,
        "_find_merged_pr_for_task",
        lambda repo, task_id: {"number": 98} if task_id == "task-123" else None,
    )
    gh_calls = []
    monkeypatch.setattr(pm, "gh", lambda args, check=False: gh_calls.append(args) or "")

    closed = pm._close_stale_redundant_agent_prs("owner/repo")

    assert closed == 1
    assert gh_calls == [[
        "pr", "close", "107", "-R", "owner/repo",
        "--comment", "Closed automatically as stale automation drift; PR #98 already merged for task `task-123`.",
    ]]


def test_reconcile_issue_board_state_normalizes_closed_blocked_issue(monkeypatch):
    done_calls = []
    monkeypatch.setattr(
        pm,
        "_mark_issue_done",
        lambda cfg, repo, issue_number, close_issue, comment=None: done_calls.append((issue_number, close_issue, comment)),
    )

    pm._reconcile_issue_board_state(
        {},
        "owner/repo",
        {"number": 99, "state": "CLOSED", "labels": [{"name": "blocked"}, {"name": "done"}]},
    )

    assert done_calls == [(99, False, None)]


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


def test_get_issue_does_not_fetch_comments_by_default(monkeypatch):
    calls = []

    def fake_gh_json(cmd):
        calls.append(cmd)
        return {"number": 42}

    monkeypatch.setattr(gh_project, "gh_json", fake_gh_json)

    issue = gh_project.get_issue("kai-linux/agent-os", 42)

    assert issue == {"number": 42}
    assert calls == [[
        "issue", "view", "42", "-R", "kai-linux/agent-os",
        "--json", "number,title,body,labels,url,updatedAt",
    ]]


def test_get_issue_can_fetch_comments_explicitly(monkeypatch):
    calls = []

    def fake_gh_json(cmd):
        calls.append(cmd)
        return {"number": 42, "comments": []}

    monkeypatch.setattr(gh_project, "gh_json", fake_gh_json)

    issue = gh_project.get_issue("kai-linux/agent-os", 42, include_comments=True)

    assert issue == {"number": 42, "comments": []}
    assert calls == [[
        "issue", "view", "42", "-R", "kai-linux/agent-os",
        "--json", "number,title,body,labels,url,updatedAt,comments",
    ]]


# ---------------------------------------------------------------------------
# _get_conflicted_files
# ---------------------------------------------------------------------------


def test_get_conflicted_files(tmp_path, monkeypatch):
    """Returns conflicted file list, excluding CODEBASE.md and .agent_result.md."""
    import subprocess as _sp
    orig_run = _sp.run

    def mock_run(cmd, **kw):
        if "diff" in cmd and "--diff-filter=U" in cmd:
            r = Mock()
            r.returncode = 0
            r.stdout = "CODEBASE.md\nfoo.py\n.agent_result.md\nbar.py\n"
            return r
        return orig_run(cmd, **kw)

    monkeypatch.setattr(_sp, "run", mock_run)
    result = _get_conflicted_files(tmp_path)
    assert result == ["foo.py", "bar.py"]


def test_get_conflicted_files_none(tmp_path, monkeypatch):
    """Returns empty list when no conflicts."""
    import subprocess as _sp

    def mock_run(cmd, **kw):
        r = Mock()
        r.returncode = 0
        r.stdout = ""
        return r

    monkeypatch.setattr(_sp, "run", mock_run)
    assert _get_conflicted_files(tmp_path) == []


# ---------------------------------------------------------------------------
# _try_union_resolve
# ---------------------------------------------------------------------------


def test_try_union_resolve_strips_markers(tmp_path, monkeypatch):
    """Union resolve keeps both sides and strips conflict markers."""
    import subprocess as _sp
    git_add_calls = []
    orig_run = _sp.run

    def mock_run(cmd, **kw):
        if "add" in cmd:
            git_add_calls.append(cmd)
            r = Mock()
            r.returncode = 0
            return r
        return orig_run(cmd, **kw)

    monkeypatch.setattr(_sp, "run", mock_run)

    conflicted = tmp_path / "foo.py"
    conflicted.write_text(
        "import os\n"
        "<<<<<<< HEAD\n"
        "def hello():\n"
        "    pass\n"
        "=======\n"
        "def world():\n"
        "    pass\n"
        ">>>>>>> feature\n"
        "# end\n"
    )

    result = _try_union_resolve(tmp_path, ["foo.py"])
    assert result is True

    resolved = conflicted.read_text()
    assert "<<<<<<" not in resolved
    assert "=======" not in resolved
    assert ">>>>>>>" not in resolved
    assert "def hello():" in resolved
    assert "def world():" in resolved
    assert "# end" in resolved


def test_try_union_resolve_missing_file(tmp_path, monkeypatch):
    """Returns False when a conflicted file doesn't exist."""
    result = _try_union_resolve(tmp_path, ["nonexistent.py"])
    assert result is False


def test_try_union_resolve_no_markers(tmp_path, monkeypatch):
    """Files without markers are skipped (considered already resolved)."""
    import subprocess as _sp
    monkeypatch.setattr(_sp, "run", lambda cmd, **kw: Mock(returncode=0))

    clean = tmp_path / "clean.py"
    clean.write_text("def foo():\n    pass\n")

    result = _try_union_resolve(tmp_path, ["clean.py"])
    assert result is True
    # Content unchanged
    assert clean.read_text() == "def foo():\n    pass\n"
