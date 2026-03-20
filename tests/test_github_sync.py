from __future__ import annotations

"""Unit tests for follow-up creation and issue lifecycle in orchestrator/github_sync.py."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator import github_sync


def test_sync_result_partial_debug_creates_single_deduped_followup(monkeypatch):
    created = []
    comments = []
    searches = []

    monkeypatch.setattr(github_sync, "load_config", lambda: {
        "github_owner": "owner",
        "github_projects": {
            "proj": {
                "project_number": 1,
                "ready_value": "Ready",
                "blocked_value": "Blocked",
                "done_value": "Done",
                "repos": [{"github_repo": "owner/repo", "local_repo": "/tmp/repo"}],
            }
        },
    })
    monkeypatch.setattr(github_sync, "edit_issue_labels", lambda *args, **kwargs: None)
    monkeypatch.setattr(github_sync, "create_pr_for_branch", lambda *args, **kwargs: None)
    monkeypatch.setattr(github_sync, "query_project", lambda *args, **kwargs: {
        "project_id": "proj-id",
        "status_field_id": "status-field",
        "status_options": {"Blocked": "blocked-option", "Ready": "ready-option"},
        "items": [{"url": "https://github.com/owner/repo/issues/7", "item_id": "item-7"}],
    })
    monkeypatch.setattr(github_sync, "set_item_status", lambda *args, **kwargs: None)
    monkeypatch.setattr(github_sync, "gh", lambda *args, **kwargs: '{"id":"item-99"}')
    monkeypatch.setattr(github_sync, "add_issue_comment", lambda repo, number, body: comments.append((repo, number, body)))

    def fake_gh_json(cmd):
        searches.append(cmd)
        if len(searches) == 1:
            return []
        return [{
            "number": 88,
            "title": "Follow up partial debug for task-123",
            "url": "https://github.com/owner/repo/issues/88",
            "body": "## Original Task ID\ntask-123\n",
        }]

    monkeypatch.setattr(github_sync, "gh_json", fake_gh_json)

    def fake_create_issue(repo, title, body, labels):
        created.append((repo, title, body, labels))
        return "https://github.com/owner/repo/issues/88"

    monkeypatch.setattr(github_sync, "_create_issue", fake_create_issue)

    meta = {
        "task_id": "task-123",
        "task_type": "debugging",
        "branch": "agent/task-123",
        "base_branch": "main",
        "priority": "prio:high",
        "github_project_key": "proj",
        "github_repo": "owner/repo",
        "github_issue_number": 7,
        "github_issue_url": "https://github.com/owner/repo/issues/7",
    }
    result = {
        "status": "partial",
        "summary": "Still failing in the parser when fixture B is enabled.",
        "next_step": "Reproduce the parser failure with fixture B enabled and isolate which normalization step corrupts the token stream.",
        "blocker_code": "test_failure",
        "done": ["- Added debug logging around token normalization."],
        "blockers": ["- Failure only reproduces with fixture B."],
        "files_changed": ["- orchestrator/queue.py"],
        "tests_run": ["- pytest tests/test_queue.py -k parser -> failed"],
        "attempted_approaches": ["- Retried with broader logging only; did not isolate the corrupting step."],
        "manual_steps": "- None",
    }

    first = github_sync.sync_result(meta, result, None)
    second = github_sync.sync_result(meta, result, None)

    assert first["followup_issue_url"] == "https://github.com/owner/repo/issues/88"
    assert second["followup_issue_url"] == "https://github.com/owner/repo/issues/88"
    assert len(created) == 1
    assert created[0][1] == "Follow up partial debug for task-123"
    assert "## Original Task ID\ntask-123" in created[0][2]
    assert "## Remaining Failure\nStill failing in the parser" in created[0][2]
    assert "## Goal\nReproduce the parser failure with fixture B enabled" in created[0][2]
    assert created[0][3] == ["ready", "prio:high"]
    assert any("Follow-up issue" in body for _, _, body in comments)


def test_sync_result_partial_pr_ci_recovery_keeps_branch_handoff_and_ready_followup(monkeypatch):
    created = []
    comments = []
    label_calls = []
    project_status_calls = []
    gh_calls = []

    monkeypatch.setattr(github_sync, "load_config", lambda: {
        "github_owner": "owner",
        "github_projects": {
            "proj": {
                "project_number": 1,
                "ready_value": "Ready",
                "blocked_value": "Blocked",
                "done_value": "Done",
                "repos": [{"github_repo": "owner/repo", "local_repo": "/tmp/repo"}],
            }
        },
    })
    monkeypatch.setattr(
        github_sync,
        "edit_issue_labels",
        lambda repo, issue, add=None, remove=None: label_calls.append(
            {"repo": repo, "issue": issue, "add": add, "remove": remove}
        ),
    )
    monkeypatch.setattr(github_sync, "create_pr_for_branch", lambda *args, **kwargs: None)
    monkeypatch.setattr(github_sync, "add_issue_comment", lambda repo, number, body: comments.append((repo, number, body)))
    monkeypatch.setattr(
        github_sync,
        "query_project",
        lambda *args, **kwargs: {
            "project_id": "proj-id",
            "status_field_id": "status-field",
            "status_options": {"Blocked": "blocked-option", "Ready": "ready-option", "Done": "done-option"},
            "items": [{"url": "https://github.com/owner/repo/issues/73", "item_id": "item-73"}],
        },
    )
    monkeypatch.setattr(
        github_sync,
        "set_item_status",
        lambda project_id, item_id, field_id, option_id: project_status_calls.append((item_id, option_id)),
    )
    monkeypatch.setattr(
        github_sync,
        "gh",
        lambda args, check=True: gh_calls.append(args) or '{"id":"item-99"}',
    )
    monkeypatch.setattr(github_sync, "gh_json", lambda cmd: [])

    def fake_create_issue(repo, title, body, labels):
        created.append((repo, title, body, labels))
        return "https://github.com/owner/repo/issues/88"

    monkeypatch.setattr(github_sync, "_create_issue", fake_create_issue)

    meta = {
        "task_id": "task-20260320-105114-fix-ci-failure-on-pr-71",
        "task_type": "debugging",
        "branch": "agent/task-20260320-101116-add-post-merge-outcome-attribution-for-issue-pr-an",
        "base_branch": "agent/task-20260320-101116-add-post-merge-outcome-attribution-for-issue-pr-an",
        "priority": "prio:high",
        "github_project_key": "proj",
        "github_repo": "owner/repo",
        "github_issue_number": 73,
        "github_issue_url": "https://github.com/owner/repo/issues/73",
    }
    result = {
        "status": "partial",
        "summary": "Merge conflict markers remain in the PR branch and collection still fails.",
        "next_step": "Resolve the committed merge-conflict markers on the existing PR branch and rerun the focused pytest coverage.",
        "blocker_code": "test_failure",
        "done": ["- Identified unresolved conflict markers in the active PR branch."],
        "blockers": ["- Pytest collection fails until the branch is repaired."],
        "files_changed": ["- orchestrator/github_dispatcher.py", "- tests/test_github_dispatcher.py"],
        "tests_run": ["- pytest tests/test_github_dispatcher.py -> failed during collection"],
        "attempted_approaches": ["- Inspected the failing branch state without changing the branch handoff target."],
        "manual_steps": "- None",
    }

    sync = github_sync.sync_result(meta, result, None)

    assert sync["followup_issue_url"] == "https://github.com/owner/repo/issues/88"
    assert len(created) == 1
    assert created[0][1] == "Follow up partial debug for task-20260320-105114-fix-ci-failure-on-pr-71"
    assert "## Goal\nResolve the committed merge-conflict markers on the existing PR branch" in created[0][2]
    assert "## Base Branch\nagent/task-20260320-101116-add-post-merge-outcome-attribution-for-issue-pr-an" in created[0][2]
    assert "## Branch\nagent/task-20260320-101116-add-post-merge-outcome-attribution-for-issue-pr-an" in created[0][2]
    assert "Original issue: #73" in created[0][2]
    assert created[0][3] == ["ready", "prio:high"]
    assert label_calls == [
        {
            "repo": "owner/repo",
            "issue": 73,
            "add": ["blocked"],
            "remove": ["in-progress", "ready", "agent-dispatched"],
        }
    ]
    assert project_status_calls == [("item-99", "ready-option"), ("item-73", "blocked-option")]
    assert gh_calls == [["project", "item-add", "1", "--owner", "owner", "--url", "https://github.com/owner/repo/issues/88", "--format", "json"]]
    assert comments and "### Follow-up issue\nhttps://github.com/owner/repo/issues/88" in comments[0][2]


def _cfg() -> dict:
    return {
        "github_owner": "kai-linux",
        "github_projects": {
            "agent-os": {
                "project_number": 6,
                "in_progress_value": "In Progress",
                "blocked_value": "Blocked",
                "done_value": "Done",
            }
        },
    }


def _meta() -> dict:
    return {
        "github_project_key": "agent-os",
        "github_repo": "kai-linux/agent-os",
        "github_issue_number": 64,
        "github_issue_url": "https://github.com/kai-linux/agent-os/issues/64",
        "branch": "agent/test-branch",
        "task_id": "task-123",
    }


def test_sync_result_complete_with_pr_keeps_issue_in_progress(monkeypatch):
    monkeypatch.setattr("orchestrator.github_sync.load_config", lambda: _cfg())
    comments = []
    monkeypatch.setattr("orchestrator.github_sync.add_issue_comment", lambda repo, issue, body: comments.append(body))
    label_calls = []
    monkeypatch.setattr(
        "orchestrator.github_sync.edit_issue_labels",
        lambda repo, issue, add=None, remove=None: label_calls.append({"add": add, "remove": remove}),
    )
    monkeypatch.setattr(
        "orchestrator.github_sync.create_pr_for_branch",
        lambda repo, branch, title, body: "https://github.com/kai-linux/agent-os/pull/71",
    )
    gh_calls = []
    monkeypatch.setattr("orchestrator.github_sync.gh", lambda args: gh_calls.append(args))
    monkeypatch.setattr(
        "orchestrator.github_sync.query_project",
        lambda project_number, owner: {
            "project_id": "proj",
            "status_field_id": "field",
            "status_options": {"In Progress": "opt-in-progress", "Done": "opt-done", "Blocked": "opt-blocked"},
            "items": [{"url": "https://github.com/kai-linux/agent-os/issues/64", "item_id": "item-64"}],
        },
    )
    project_status_calls = []
    monkeypatch.setattr(
        "orchestrator.github_sync.set_item_status",
        lambda project_id, item_id, field_id, option_id: project_status_calls.append(option_id),
    )

    github_sync.sync_result(_meta(), {"status": "complete", "summary": "ok", "next_step": "none"}, "abc123")

    assert comments and "https://github.com/kai-linux/agent-os/pull/71" in comments[0]
    assert label_calls == [
        {
            "add": ["in-progress", "agent-dispatched"],
            "remove": ["ready", "blocked", "done"],
        }
    ]
    assert gh_calls == []
    assert project_status_calls == ["opt-in-progress"]


def test_sync_result_complete_without_pr_closes_issue(monkeypatch):
    monkeypatch.setattr("orchestrator.github_sync.load_config", lambda: _cfg())
    monkeypatch.setattr("orchestrator.github_sync.add_issue_comment", lambda repo, issue, body: None)
    label_calls = []
    monkeypatch.setattr(
        "orchestrator.github_sync.edit_issue_labels",
        lambda repo, issue, add=None, remove=None: label_calls.append({"add": add, "remove": remove}),
    )
    monkeypatch.setattr("orchestrator.github_sync.create_pr_for_branch", lambda repo, branch, title, body: None)
    gh_calls = []
    monkeypatch.setattr("orchestrator.github_sync.gh", lambda args: gh_calls.append(args))
    monkeypatch.setattr(
        "orchestrator.github_sync.query_project",
        lambda project_number, owner: {
            "project_id": "proj",
            "status_field_id": "field",
            "status_options": {"In Progress": "opt-in-progress", "Done": "opt-done", "Blocked": "opt-blocked"},
            "items": [{"url": "https://github.com/kai-linux/agent-os/issues/64", "item_id": "item-64"}],
        },
    )
    project_status_calls = []
    monkeypatch.setattr(
        "orchestrator.github_sync.set_item_status",
        lambda project_id, item_id, field_id, option_id: project_status_calls.append(option_id),
    )

    github_sync.sync_result(_meta(), {"status": "complete", "summary": "ok", "next_step": "none"}, "abc123")

    assert label_calls == [
        {
            "add": ["done"],
            "remove": ["in-progress", "ready", "blocked", "agent-dispatched"],
        }
    ]
    assert gh_calls == [["issue", "close", "64", "-R", "kai-linux/agent-os"]]
    assert project_status_calls == ["opt-done"]
