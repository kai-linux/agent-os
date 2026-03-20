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


def test_parse_issue_body_extracts_outcome_checks():
    body = """
## Goal
Measure adoption

## Outcome Checks
- activation_rate
- signup_completion
"""
    parsed = gd.parse_issue_body(body)
    assert parsed["outcome_checks"] == ["activation_rate", "signup_completion"]


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


def test_build_mailbox_task_preserves_branch_when_formatter_omits_it(monkeypatch):
    cfg = {
        "default_agent": "auto",
        "default_task_type": "implementation",
        "default_base_branch": "main",
        "default_allow_push": True,
        "default_max_attempts": 4,
        "max_runtime_minutes": 40,
        "formatter_model": "haiku",
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

## Success Criteria
- Tests pass

## Task Type
debugging

## Base Branch
agent/task-123

## Branch
agent/task-123
## Outcome Checks
- activation_rate
""",
    }

    monkeypatch.setattr(
        gd,
        "format_task",
        lambda title, body, model=None: {
            "goal": "Repair CI.",
            "success_criteria": "- Tests pass",
            "task_type": "debugging",
            "agent_preference": "auto",
            "constraints": "- Prefer minimal diffs",
            "context": "None",
            "base_branch": "",
            "branch": "",
            "outcome_checks": [],
        },
    )
    _task_id, task_md = gd.build_mailbox_task(cfg, "proj", repo_cfg, issue)
    assert "base_branch: agent/task-123" in task_md
    assert "branch: agent/task-123" in task_md
    assert "outcome_check_ids:" in task_md
    assert "- activation_rate" in task_md


def test_build_mailbox_task_includes_outcome_check_ids(monkeypatch):
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
        "title": "Improve activation",
        "url": "https://github.com/owner/repo/issues/42",
        "labels": [{"name": "prio:high"}],
        "body": """
## Goal
Improve activation.

## Outcome Checks
- activation_rate
""",
    }

    monkeypatch.setattr(gd, "format_task", lambda title, body, model=None: None)
    _task_id, task_md = gd.build_mailbox_task(cfg, "proj", repo_cfg, issue)
    assert "outcome_check_ids:" in task_md
    assert "- activation_rate" in task_md


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


def test_parse_retry_decision_supports_yaml_section():
    note = """
# Escalation Note

## Retry Decision
action: reroute
agent: codex
reason: Fresh Codex retry with the preserved branch is safe.
"""
    assert gd.parse_retry_decision(note) == {
        "action": "reroute",
        "agent": "codex",
        "reason": "Fresh Codex retry with the preserved branch is safe.",
    }


def test_consume_retry_decision_moves_task_back_to_inbox(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    blocked = tmp_path / "blocked"
    escalated = tmp_path / "escalated"
    failed = tmp_path / "failed"
    for path in (inbox, blocked, escalated, failed):
        path.mkdir()

    task_path = escalated / "task-current.md"
    task_path.write_text("""---
task_id: task-current
parent_task_id: task-root
repo: /tmp/repo
github_project_key: proj
github_repo: owner/repo
github_issue_number: 42
github_issue_url: https://github.com/owner/repo/issues/42
agent: auto
---

# Goal

Retry this task
""", encoding="utf-8")

    note_path = escalated / "task-root-escalation.md"
    note_path.write_text("""
# Escalation Note

## Retry Decision
```yaml
action: retry
reason: Missing context was added to the escalation note.
```
""", encoding="utf-8")

    cfg = {
        "github_projects": {"proj": {"ready_value": "Ready"}},
    }
    paths = {"INBOX": inbox, "BLOCKED": blocked, "ESCALATED": escalated, "FAILED": failed}
    queried = {
        1: ({
            "project_id": "project-1",
            "status_field_id": "status-field",
            "status_options": {"Ready": "ready-option"},
            "items": [{
                "item_id": "item-1",
                "repo": "owner/repo",
                "number": 42,
            }],
        }, []),
    }

    comments = []
    label_edits = []
    status_updates = []
    monkeypatch.setattr(gd, "add_issue_comment", lambda repo, number, body: comments.append((repo, number, body)))
    monkeypatch.setattr(gd, "edit_issue_labels", lambda repo, number, add=None, remove=None: label_edits.append((repo, number, add, remove)))
    monkeypatch.setattr(gd, "set_item_status", lambda project_id, item_id, field_id, option_id: status_updates.append((project_id, item_id, field_id, option_id)))

    assert gd._consume_retry_decisions(cfg, paths, queried) is True

    requeued = inbox / "task-current.md"
    assert requeued.exists()
    updated = requeued.read_text(encoding="utf-8")
    assert "escalation_note: task-root-escalation.md" in updated
    assert "escalation_decision: retry" in updated
    assert "escalation_decision_reason: Missing context was added to the escalation note." in updated
    assert not task_path.exists()
    assert label_edits == [("owner/repo", 42, ["ready"], ["blocked", "in-progress", "agent-dispatched"])]
    assert status_updates == [("project-1", "item-1", "status-field", "ready-option")]
    assert "Dispatcher retry decision" in comments[0][2]
    note_text = note_path.read_text(encoding="utf-8")
    assert gd.RETRY_DECISION_APPLIED_MARKER in note_text


def test_consume_retry_decision_reroutes_agent(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    blocked = tmp_path / "blocked"
    escalated = tmp_path / "escalated"
    failed = tmp_path / "failed"
    for path in (inbox, blocked, escalated, failed):
        path.mkdir()

    (blocked / "task-current.md").write_text("""---
task_id: task-current
parent_task_id: task-root
repo: /tmp/repo
github_project_key: proj
github_repo: owner/repo
github_issue_number: 42
github_issue_url: https://github.com/owner/repo/issues/42
agent: auto
---

# Goal

Retry this task
""", encoding="utf-8")

    (escalated / "task-root-escalation.md").write_text("""
# Escalation Note

## Retry Decision
action: reroute
agent: claude
reason: Claude should take the next bounded attempt.
""", encoding="utf-8")

    cfg = {
        "github_projects": {"proj": {"ready_value": "Ready"}},
    }
    paths = {"INBOX": inbox, "BLOCKED": blocked, "ESCALATED": escalated, "FAILED": failed}
    queried = {
        1: ({
            "project_id": "project-1",
            "status_field_id": "status-field",
            "status_options": {"Ready": "ready-option"},
            "items": [{
                "item_id": "item-1",
                "repo": "owner/repo",
                "number": 42,
            }],
        }, []),
    }

    monkeypatch.setattr(gd, "add_issue_comment", lambda *args, **kwargs: None)
    monkeypatch.setattr(gd, "edit_issue_labels", lambda *args, **kwargs: None)
    monkeypatch.setattr(gd, "set_item_status", lambda *args, **kwargs: None)

    assert gd._consume_retry_decisions(cfg, paths, queried) is True
    updated = (inbox / "task-current.md").read_text(encoding="utf-8")
    assert "agent: claude" in updated
    assert "escalation_decision_target_agent: claude" in updated


def test_consume_retry_decision_stop_closes_task(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    blocked = tmp_path / "blocked"
    escalated = tmp_path / "escalated"
    failed = tmp_path / "failed"
    for path in (inbox, blocked, escalated, failed):
        path.mkdir()

    task_path = escalated / "task-current.md"
    task_path.write_text("""---
task_id: task-current
parent_task_id: task-root
repo: /tmp/repo
github_project_key: proj
github_repo: owner/repo
github_issue_number: 42
github_issue_url: https://github.com/owner/repo/issues/42
agent: auto
---

# Goal

Stop this task
""", encoding="utf-8")

    (escalated / "task-root-escalation.md").write_text("""
# Escalation Note

## Retry Decision
action: stop
reason: This line of work is no longer worth pursuing.
""", encoding="utf-8")

    cfg = {
        "github_projects": {"proj": {"done_value": "Done"}},
    }
    paths = {"INBOX": inbox, "BLOCKED": blocked, "ESCALATED": escalated, "FAILED": failed}
    queried = {
        1: ({
            "project_id": "project-1",
            "status_field_id": "status-field",
            "status_options": {"Done": "done-option"},
            "items": [{
                "item_id": "item-1",
                "repo": "owner/repo",
                "number": 42,
            }],
        }, []),
    }

    gh_calls = []
    label_edits = []
    status_updates = []
    monkeypatch.setattr(gd, "gh", lambda cmd, check=False: gh_calls.append((cmd, check)) or "")
    monkeypatch.setattr(gd, "add_issue_comment", lambda *args, **kwargs: None)
    monkeypatch.setattr(gd, "edit_issue_labels", lambda repo, number, add=None, remove=None: label_edits.append((repo, number, add, remove)))
    monkeypatch.setattr(gd, "set_item_status", lambda project_id, item_id, field_id, option_id: status_updates.append((project_id, item_id, field_id, option_id)))

    assert gd._consume_retry_decisions(cfg, paths, queried) is True

    stopped = failed / "task-current.md"
    assert stopped.exists()
    assert not task_path.exists()
    assert gh_calls and gh_calls[0][0][:4] == ["api", "repos/owner/repo/issues/42", "-X", "PATCH"]
    assert label_edits == [("owner/repo", 42, ["done"], ["blocked", "ready", "in-progress", "agent-dispatched"])]
    assert status_updates == [("project-1", "item-1", "status-field", "done-option")]
