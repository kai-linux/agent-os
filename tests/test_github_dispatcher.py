"""Unit tests for dependency handling in orchestrator/github_dispatcher.py"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator import github_dispatcher as gd


def test_parse_issue_dependencies_supports_both_keywords():
    body = """
Depends on #12
some text
blocked by #34
Depends on #12
"""
    assert gd.parse_issue_dependencies(body) == [12, 34]


def test_resolve_issue_dependencies_blocks_on_open_dependency():
    issue = {"number": 10, "body": "Depends on #11"}
    lookup = {
        ("owner/repo", 11): {"number": 11, "body": "", "state": "OPEN"},
    }

    result = gd._resolve_issue_dependencies("owner/repo", issue, lookup, {})

    assert result == {"status": "blocked", "dependency": 11}


def test_resolve_issue_dependencies_detects_circular_chain():
    issue = {"number": 10, "body": "Depends on #11"}
    lookup = {
        ("owner/repo", 11): {"number": 11, "body": "Blocked by #10", "state": "CLOSED"},
    }

    result = gd._resolve_issue_dependencies("owner/repo", issue, lookup, {})

    assert result["status"] == "circular"
    assert result["trail"] == [10, 11, 10]


def test_resolve_issue_dependencies_fetches_missing_issue_once(monkeypatch):
    issue = {"number": 10, "body": "Depends on #11"}
    calls = []

    def fake_fetch(repo_full, number):
        calls.append((repo_full, number))
        return {"number": 11, "body": "", "state": "CLOSED"}

    monkeypatch.setattr(gd, "_fetch_issue_dependency", fake_fetch)

    result = gd._resolve_issue_dependencies("owner/repo", issue, {}, {})

    assert result == {"status": "clear"}
    assert calls == [("owner/repo", 11)]


def test_requeue_unblocked_items_sets_status_ready(monkeypatch):
    queried = {
        1: ({
            "status_field_id": "status-field",
            "status_options": {"Ready": "ready-option"},
            "project_id": "project-1",
            "items": [{
                "item_id": "item-1",
                "number": 10,
                "body": "Depends on #11",
                "state": "OPEN",
                "status": "Blocked",
                "repo": "owner/repo",
            }],
        }, []),
    }
    repo_to_project = {"owner/repo": ("proj", {"blocked_value": "Blocked", "ready_value": "Ready"}, {})}
    issue_lookup = {
        ("owner/repo", 10): queried[1][0]["items"][0],
        ("owner/repo", 11): {"number": 11, "body": "", "state": "CLOSED", "repo": "owner/repo"},
    }
    calls = []

    def fake_set_status(project_id, item_id, field_id, option_id):
        calls.append((project_id, item_id, field_id, option_id))

    monkeypatch.setattr(gd, "set_item_status", fake_set_status)

    gd._requeue_unblocked_items(queried, repo_to_project, issue_lookup)

    assert calls == [("project-1", "item-1", "status-field", "ready-option")]


def test_parse_issue_body_extracts_branch_fields():
    body = """
## Goal
Fix CI

## Task Type
debugging

## Base Branch
agent/task-123

## Branch
agent/task-123
"""
    parsed = gd.parse_issue_body(body)
    assert parsed["task_type"] == "debugging"
    assert parsed["base_branch"] == "agent/task-123"
    assert parsed["branch"] == "agent/task-123"


def test_build_mailbox_task_preserves_custom_branch(monkeypatch):
    cfg = {
        "default_agent": "auto",
        "default_task_type": "implementation",
        "default_base_branch": "main",
        "default_allow_push": True,
        "default_max_attempts": 4,
        "max_runtime_minutes": 40,
        "formatter_model": None,
    }
    repo_cfg = {"local_repo": "/tmp/repo", "github_repo": "owner/repo"}
    issue = {
        "number": 42,
        "title": "Fix CI failure on PR #34",
        "url": "https://github.com/owner/repo/issues/42",
        "labels": [{"name": "prio:high"}],
        "body": """
## Goal
Repair CI.

## Task Type
debugging

## Base Branch
agent/task-123

## Branch
agent/task-123
""",
    }

    monkeypatch.setattr(gd, "format_task", lambda title, body, model=None: None)
    task_id, task_md = gd.build_mailbox_task(cfg, "proj", repo_cfg, issue)
    assert "base_branch: agent/task-123" in task_md
    assert "branch: agent/task-123" in task_md
    assert f"prompt_snapshot_path: {Path.cwd() / 'runtime' / 'prompts' / f'{task_id}.txt'}" in task_md


def test_dispatch_item_blocks_publish_task_without_push_capability(tmp_path, monkeypatch):
    cfg = {
        "default_allow_push": False,
        "github_owner": "owner",
    }
    paths = {"INBOX": tmp_path}
    info = {
        "status_field_id": "status-field",
        "status_options": {"Blocked": "blocked-option"},
        "project_id": "project-1",
    }
    ready_items = [{
        "item_id": "item-1",
        "number": 42,
        "title": "Publish branch for release fix",
        "body": "Please git commit the fix, git push the branch, and open a PR.",
        "url": "https://github.com/owner/repo/issues/42",
        "labels": {"ready", "prio:high"},
        "repo": "owner/repo",
        "author": "trusted-user",
        "state": "OPEN",
    }]
    repo_to_project = {
        "owner/repo": (
            "proj",
            {"blocked_value": "Blocked"},
            {"local_repo": "/tmp/repo", "github_repo": "owner/repo"},
        ),
    }
    comments = []
    label_edits = []
    status_updates = []

    monkeypatch.setattr(gd, "is_trusted", lambda author, _cfg: True)
    monkeypatch.setattr(gd, "_resolve_issue_dependencies", lambda *args, **kwargs: {"status": "clear"})
    monkeypatch.setattr(gd, "add_issue_comment", lambda repo, number, body: comments.append((repo, number, body)))
    monkeypatch.setattr(
        gd,
        "edit_issue_labels",
        lambda repo, number, add=None, remove=None: label_edits.append((repo, number, add, remove)),
    )
    monkeypatch.setattr(
        gd,
        "set_item_status",
        lambda project_id, item_id, field_id, option_id: status_updates.append((project_id, item_id, field_id, option_id)),
    )

    dispatched = gd._dispatch_item(cfg, paths, "owner", repo_to_project, info, ready_items, {})

    assert dispatched is False
    assert not list(tmp_path.iterdir())
    assert status_updates == [("project-1", "item-1", "status-field", "blocked-option")]
    assert label_edits == [(
        "owner/repo",
        42,
        ["blocked", gd.MISSING_PUBLISH_CAPABILITY_LABEL],
        ["ready", "in-progress", "agent-dispatched"],
    )]
    assert len(comments) == 1
    assert gd.MISSING_PUBLISH_CAPABILITY_CODE in comments[0][2]
    payload = comments[0][2].splitlines()[1]
    assert json.loads(payload) == {
        "code": gd.MISSING_PUBLISH_CAPABILITY_CODE,
        "requirements": ["git_commit", "git_push", "push_branch", "open_pr", "publish_changes"],
        "runtime_allow_push": False,
    }
