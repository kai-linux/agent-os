"""Unit tests for follow-up creation in orchestrator/github_sync.py."""
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
