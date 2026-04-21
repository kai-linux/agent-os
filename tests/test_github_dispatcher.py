"""Unit tests for dependency handling in orchestrator/github_dispatcher.py"""
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

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


def test_escalate_unassigned_blocked_tasks_marks_first_cycle(tmp_path):
    blocked = tmp_path / "blocked"
    escalated = tmp_path / "escalated"
    blocked.mkdir()
    escalated.mkdir()

    task_path = blocked / "task-current.md"
    task_path.write_text("""---
task_id: task-current
repo: /tmp/repo
agent: none
task_type: implementation
branch: agent/task-current
---

# Goal

Fix the task.
""", encoding="utf-8")

    changed = gd._escalate_unassigned_blocked_tasks({}, {"BLOCKED": blocked, "ESCALATED": escalated, "DONE": tmp_path / "done"})

    assert changed is True
    updated = task_path.read_text(encoding="utf-8")
    assert "agent: none" in updated
    assert f"{gd.UNASSIGNED_BLOCKED_SEEN_AT}:" in updated
    assert list(escalated.iterdir()) == []


def test_escalate_unassigned_blocked_tasks_moves_to_escalated_on_next_cycle(tmp_path):
    blocked = tmp_path / "blocked"
    escalated = tmp_path / "escalated"
    blocked.mkdir()
    escalated.mkdir()

    task_path = blocked / "task-current.md"
    task_path.write_text("""---
task_id: task-current
parent_task_id: task-root
repo: /tmp/repo
agent: none
task_type: implementation
branch: agent/task-current
unassigned_blocked_seen_at: 2026-03-31T10:55:20
---

# Goal

Fix the task.
""", encoding="utf-8")

    changed = gd._escalate_unassigned_blocked_tasks({}, {"BLOCKED": blocked, "ESCALATED": escalated, "DONE": tmp_path / "done"})

    assert changed is True
    moved_task = escalated / "task-current.md"
    note_path = escalated / "task-root-escalation.md"
    assert moved_task.exists()
    assert note_path.exists()
    assert not task_path.exists()
    moved_text = moved_task.read_text(encoding="utf-8")
    assert "escalation_note: task-root-escalation.md" in moved_text
    assert "escalated_at:" in moved_text
    note_text = note_path.read_text(encoding="utf-8")
    assert "## Parent Task ID" in note_text
    assert "task-root" in note_text
    assert "Blocked task has no assigned agent after one scheduler cycle." in note_text
    assert "agent=none" in note_text


def test_escalate_unassigned_blocked_tasks_skips_assigned_tasks(tmp_path):
    blocked = tmp_path / "blocked"
    escalated = tmp_path / "escalated"
    blocked.mkdir()
    escalated.mkdir()

    task_path = blocked / "task-current.md"
    original = """---
task_id: task-current
repo: /tmp/repo
agent: codex
unassigned_blocked_seen_at: 2026-03-31T10:55:20
---

# Goal

Fix the task.
"""
    task_path.write_text(original, encoding="utf-8")

    changed = gd._escalate_unassigned_blocked_tasks({}, {"BLOCKED": blocked, "ESCALATED": escalated, "DONE": tmp_path / "done"})

    assert changed is False
    assert task_path.read_text(encoding="utf-8") == original
    assert list(escalated.iterdir()) == []


def test_escalate_over_retried_blocked_task_posts_comment_and_telegram(tmp_path, monkeypatch):
    blocked = tmp_path / "blocked"
    escalated = tmp_path / "escalated"
    logs = tmp_path / "logs"
    actions = tmp_path / "telegram_actions"
    inbox = tmp_path / "inbox"
    processing = tmp_path / "processing"
    done = tmp_path / "done"
    failed = tmp_path / "failed"
    for path in (blocked, escalated, logs, actions, inbox, processing, done, failed):
        path.mkdir()

    task_path = blocked / "task-20260401-120602-debug-pr98.md"
    task_path.write_text("""---
task_id: task-20260401-120602-debug-pr98
parent_task_id: task-pr98-root
repo: /tmp/repo
github_project_key: proj
github_repo: owner/repo
github_issue_number: 98
github_issue_url: https://github.com/owner/repo/issues/98
branch: agent/task-20260401-120602-debug-pr98
attempt: 3
prompt_snapshot_path: /home/kai/agent-os/runtime/prompts/task-20260401-120602-debug-pr98.txt
---

# Goal

Repair PR #98.
""", encoding="utf-8")
    (logs / "task-20260401-120602-debug-pr98.log").write_text("""Worker status from codex: blocked
Worker result file:
STATUS: blocked

BLOCKER_CODE:
test_failure

SUMMARY:
CI rerun is still red for PR #98.

DONE:
- Investigated the failing branch.

BLOCKERS:
- The `pytest` job still fails on rerun.

NEXT_STEP:
Retry after reviewing the rerun logs.
""", encoding="utf-8")
    older = blocked / "task-20260401-120502-debug-pr98.md"
    older.write_text("""---
task_id: task-20260401-120502-debug-pr98
parent_task_id: task-pr98-root
repo: /tmp/repo
attempt: 2
---

# Goal

Previous retry.
""", encoding="utf-8")
    (logs / "task-20260401-120502-debug-pr98.log").write_text("""Worker status from claude: blocked
Worker result file:
STATUS: blocked

BLOCKER_CODE:
missing_context

SUMMARY:
The failing CI job name was not preserved.

DONE:
- Reviewed the CI metadata.

BLOCKERS:
- The failing CI job name was not preserved.

NEXT_STEP:
Recover the failing job context.
""", encoding="utf-8")

    comments = []
    saved_actions = []
    sent = []
    monkeypatch.setattr(gd, "add_issue_comment", lambda repo, number, body: comments.append((repo, number, body)))
    monkeypatch.setattr(gd, "save_telegram_action", lambda actions_dir, action: saved_actions.append(dict(action)))
    monkeypatch.setattr(gd, "send_telegram", lambda cfg, text, reply_markup=None: sent.append((text, reply_markup)) or 77)

    changed = gd._escalate_over_retried_blocked_tasks(
        {"telegram_chat_id": "123", "blocked_escalation_attempt_threshold": 3, "blocked_escalation_age_hours": 24},
        {
            "INBOX": inbox,
            "PROCESSING": processing,
            "DONE": done,
            "FAILED": failed,
            "BLOCKED": blocked,
            "ESCALATED": escalated,
            "LOGS": logs,
            "TELEGRAM_ACTIONS": actions,
        },
    )

    assert changed is True
    note_path = escalated / "task-pr98-root-escalation.md"
    assert note_path.exists()
    note_text = note_path.read_text(encoding="utf-8")
    assert "Blocked task reached retry attempt 3" in note_text
    assert "test_failure" in note_text
    assert "missing_context" in note_text
    assert "task `task-20260401-120602-debug-pr98`" in note_text
    # Prompt snapshot reference is embedded in escalation note, comment, telegram, and action
    assert "runtime/prompts/task-20260401-120602-debug-pr98.txt" in note_text
    assert comments == [("owner/repo", 98, comments[0][2])]
    assert "## Blocked task escalation" in comments[0][2]
    assert "`test_failure`" in comments[0][2]
    assert "Attempt 3 | task `task-20260401-120602-debug-pr98`" in comments[0][2]
    assert "runtime/prompts/task-20260401-120602-debug-pr98.txt" in comments[0][2]
    assert len(saved_actions) == 2
    assert saved_actions[-1]["message_id"] == 77
    assert saved_actions[-1].get("prompt_snapshot_path") == "/home/kai/agent-os/runtime/prompts/task-20260401-120602-debug-pr98.txt"
    assert sent and sent[0][1]["inline_keyboard"][0][0]["text"] == "Retry"
    assert "runtime/prompts/task-20260401-120602-debug-pr98.txt" in sent[0][0]


def test_escalate_over_retried_blocked_task_uses_age_threshold_and_dedupes(tmp_path, monkeypatch):
    blocked = tmp_path / "blocked"
    escalated = tmp_path / "escalated"
    logs = tmp_path / "logs"
    actions = tmp_path / "telegram_actions"
    inbox = tmp_path / "inbox"
    processing = tmp_path / "processing"
    done = tmp_path / "done"
    failed = tmp_path / "failed"
    for path in (blocked, escalated, logs, actions, inbox, processing, done, failed):
        path.mkdir()

    task_path = blocked / "task-20260401-120807-debug-pr98.md"
    task_path.write_text("""---
task_id: task-20260401-120807-debug-pr98
parent_task_id: task-20260401-120807-debug-pr98
repo: /tmp/repo
github_project_key: proj
github_repo: owner/repo
github_issue_number: 98
github_issue_url: https://github.com/owner/repo/issues/98
branch: agent/task-20260401-120807-debug-pr98
attempt: 1
---

# Goal

Repair PR #98.
""", encoding="utf-8")
    stale = datetime.now().timestamp() - timedelta(hours=49).total_seconds()
    os.utime(task_path, (stale, stale))
    (logs / "task-20260401-120807-debug-pr98.log").write_text("""Worker status from codex: blocked
Worker result file:
STATUS: blocked

BLOCKER_CODE:
test_failure

SUMMARY:
PR #98 remains blocked.

DONE:
- Reproduced the failure.

BLOCKERS:
- PR #98 remains blocked.

NEXT_STEP:
Escalate to human review.
""", encoding="utf-8")

    comments = []
    monkeypatch.setattr(gd, "add_issue_comment", lambda repo, number, body: comments.append((repo, number, body)))
    monkeypatch.setattr(gd, "save_telegram_action", lambda *args, **kwargs: None)
    monkeypatch.setattr(gd, "send_telegram", lambda *args, **kwargs: 88)

    paths = {
        "INBOX": inbox,
        "PROCESSING": processing,
        "DONE": done,
        "FAILED": failed,
        "BLOCKED": blocked,
        "ESCALATED": escalated,
        "LOGS": logs,
        "TELEGRAM_ACTIONS": actions,
    }
    cfg = {"telegram_chat_id": "123", "blocked_escalation_attempt_threshold": 3, "blocked_escalation_age_hours": 24}

    assert gd._escalate_over_retried_blocked_tasks(cfg, paths) is True
    assert "remained unowned" in comments[0][2]
    assert gd._escalate_over_retried_blocked_tasks(cfg, paths) is False


def test_escalate_over_retried_blocked_task_handles_pr98_examples(tmp_path, monkeypatch):
    blocked = tmp_path / "blocked"
    escalated = tmp_path / "escalated"
    logs = tmp_path / "logs"
    actions = tmp_path / "telegram_actions"
    inbox = tmp_path / "inbox"
    processing = tmp_path / "processing"
    done = tmp_path / "done"
    failed = tmp_path / "failed"
    for path in (blocked, escalated, logs, actions, inbox, processing, done, failed):
        path.mkdir()

    for task_id in (
        "task-20260401-120602-debug-pr98",
        "task-20260401-120802-debug-pr98",
        "task-20260401-120807-debug-pr98",
    ):
        (blocked / f"{task_id}.md").write_text(f"""---
task_id: {task_id}
parent_task_id: {task_id}
repo: /tmp/repo
github_project_key: proj
github_repo: owner/repo
github_issue_number: 98
github_issue_url: https://github.com/owner/repo/issues/98
attempt: 3
---

# Goal

Repair PR #98.
""", encoding="utf-8")
        (logs / f"{task_id}.log").write_text("""Worker status from codex: blocked
Worker result file:
STATUS: blocked

BLOCKER_CODE:
test_failure

SUMMARY:
Repeated CI failure.

DONE:
- Reproduced the failing PR job.

BLOCKERS:
- Repeated CI failure.

NEXT_STEP:
Escalate.
""", encoding="utf-8")

    monkeypatch.setattr(gd, "add_issue_comment", lambda *args, **kwargs: None)
    monkeypatch.setattr(gd, "save_telegram_action", lambda *args, **kwargs: None)
    monkeypatch.setattr(gd, "send_telegram", lambda *args, **kwargs: 55)

    paths = {
        "INBOX": inbox,
        "PROCESSING": processing,
        "DONE": done,
        "FAILED": failed,
        "BLOCKED": blocked,
        "ESCALATED": escalated,
        "LOGS": logs,
        "TELEGRAM_ACTIONS": actions,
    }
    cfg = {"telegram_chat_id": "123", "blocked_escalation_attempt_threshold": 3}

    assert gd._escalate_over_retried_blocked_tasks(cfg, paths) is True
    assert gd._escalate_over_retried_blocked_tasks(cfg, paths) is True
    assert gd._escalate_over_retried_blocked_tasks(cfg, paths) is True
    assert sorted(path.name for path in escalated.glob("*-escalation.md")) == [
        "task-20260401-120602-debug-pr98-escalation.md",
        "task-20260401-120802-debug-pr98-escalation.md",
        "task-20260401-120807-debug-pr98-escalation.md",
    ]


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


def test_build_mailbox_task_preserves_failed_ci_check_context_when_formatter_summarizes(monkeypatch):
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
        "title": "Fix CI failure on PR #71",
        "url": "https://github.com/owner/repo/issues/42",
        "labels": [],
        "body": """
## Goal
Repair CI.

## Task Type
debugging

## Branch
agent/task-71

## Context
- PR: https://github.com/owner/repo/pull/71
- Failed checks:
- **pytest**: `failure`
""",
    }

    monkeypatch.setattr(
        gd,
        "format_task",
        lambda title, body, model=None: {
            "goal": "Repair CI.",
            "success_criteria": "- Make CI green",
            "task_type": "debugging",
            "agent_preference": "auto",
            "outcome_checks": [],
            "constraints": "- Prefer minimal diffs",
            "context": "- Investigate the failing workflow rerun.",
            "base_branch": "",
            "branch": "agent/task-71",
        },
    )

    _task_id, mailbox = gd.build_mailbox_task(cfg, "proj", repo_cfg, issue)

    assert "- Investigate the failing workflow rerun." in mailbox
    assert "- PR: https://github.com/owner/repo/pull/71" in mailbox
    assert "- Failed checks:" in mailbox
    assert "- **pytest**: `failure`" in mailbox


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


def test_build_mailbox_task_resolves_goal_ancestry_from_objective_and_sprint(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    runtime_dir = repo / "runtime"
    runtime_dir.mkdir()
    (runtime_dir / "next_sprint_focus.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-04-21T09:00:00+00:00",
                "headline": "Eliminate missing_context as the top task failure mode",
                "next_sprint_focus": ["Keep goal-trace context attached to dispatched work"],
            }
        ),
        encoding="utf-8",
    )
    objectives_dir = tmp_path / "objectives"
    objectives_dir.mkdir()
    (objectives_dir / "repo.yaml").write_text(
        "primary_outcome: Trusted adoption by technical builders\n",
        encoding="utf-8",
    )
    cfg = {
        "root_dir": str(tmp_path),
        "objectives_dir": str(objectives_dir),
        "default_agent": "auto",
        "default_task_type": "implementation",
        "default_base_branch": "main",
        "default_allow_push": True,
        "default_max_attempts": 4,
        "max_runtime_minutes": 40,
        "formatter_model": None,
    }
    repo_cfg = {"local_repo": str(repo), "github_repo": "owner/repo"}
    issue = {
        "number": 240,
        "title": "Attach goal ancestry to dispatched tasks",
        "url": "https://github.com/owner/repo/issues/240",
        "labels": [],
        "body": "## Goal\nShip ancestry.\n",
    }

    monkeypatch.setattr(gd, "format_task", lambda title, body, model=None: None)

    _task_id, task_md = gd.build_mailbox_task(cfg, "proj", repo_cfg, issue)

    assert "objective_id: repo" in task_md
    assert "sprint_id: sprint-2026-04-21" in task_md
    assert "parent_issue: owner/repo#240" in task_md
    assert "parent_goal_summary:" in task_md
    assert "## Goal Ancestry" in task_md
    assert "Objective: `repo`" in task_md
    assert "Parent issue: [owner/repo#240](https://github.com/owner/repo/issues/240)" in task_md


def test_build_mailbox_task_rejects_invalid_agent_preference(monkeypatch):
    cfg = {
        "default_agent": "auto",
        "default_task_type": "implementation",
        "default_base_branch": "main",
        "default_allow_push": True,
        "default_max_attempts": 4,
        "max_runtime_minutes": 40,
        "formatter_model": None,
        "agent_fallbacks": {"implementation": ["codex", "claude"]},
    }
    repo_cfg = {"local_repo": "/tmp/repo", "github_repo": "owner/repo"}
    issue = {
        "number": 42,
        "title": "Improve dispatch validation",
        "url": "https://github.com/owner/repo/issues/42",
        "labels": [{"name": "prio:high"}],
        "body": """
## Goal
Improve dispatch validation.

## Agent Preference
none
""",
    }

    monkeypatch.setattr(gd, "format_task", lambda title, body, model=None: None)

    try:
        gd.build_mailbox_task(cfg, "proj", repo_cfg, issue)
        assert False, "expected build_mailbox_task() to reject agent preference 'none'"
    except ValueError as exc:
        assert str(exc) == "Unsupported agent preference: none. Expected one of: auto, claude, codex, deepseek, gemini."


def test_build_mailbox_task_rejects_when_no_healthy_agent_available(tmp_path, monkeypatch):
    metrics_dir = tmp_path / "runtime" / "metrics"
    metrics_dir.mkdir(parents=True)
    now = gd.datetime.now().isoformat()
    # Each agent: 2 complete + 10 blocked = 16.7% over 12 tasks. Crosses the
    # genuinely-broken threshold (>=10 samples at <=50%) so Fix B's fail-open
    # does NOT apply — dispatcher must raise.
    records = (
        [{"timestamp": now, "agent": "codex", "status": "complete"}] * 2 +
        [{"timestamp": now, "agent": "codex", "status": "blocked"}] * 10 +
        [{"timestamp": now, "agent": "claude", "status": "complete"}] * 2 +
        [{"timestamp": now, "agent": "claude", "status": "blocked"}] * 10
    )
    (metrics_dir / "agent_stats.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )

    cfg = {
        "root_dir": str(tmp_path),
        "default_agent": "auto",
        "default_task_type": "implementation",
        "default_base_branch": "main",
        "default_allow_push": True,
        "default_max_attempts": 4,
        "max_runtime_minutes": 40,
        "formatter_model": None,
        "agent_fallbacks": {"implementation": ["codex", "claude"]},
    }
    repo_cfg = {"local_repo": "/tmp/repo", "github_repo": "owner/repo"}
    issue = {
        "number": 42,
        "title": "Improve dispatch validation",
        "url": "https://github.com/owner/repo/issues/42",
        "labels": [{"name": "prio:high"}],
        "body": """
## Goal
Improve dispatch validation.
""",
    }

    monkeypatch.setattr(gd, "format_task", lambda title, body, model=None: None)

    with pytest.raises(ValueError, match="No healthy agents available"):
        gd.build_mailbox_task(cfg, "proj", repo_cfg, issue)


def test_build_mailbox_task_skips_agent_below_adaptive_threshold(tmp_path, monkeypatch):
    """Agents with <25% success over 7 days are skipped by the adaptive gate."""
    metrics_dir = tmp_path / "runtime" / "metrics"
    metrics_dir.mkdir(parents=True)
    now = gd.datetime.now().isoformat()
    # deepseek: 0% (all blocked), claude: 100%
    records = [
        {"timestamp": now, "agent": "deepseek", "status": "blocked"},
        {"timestamp": now, "agent": "deepseek", "status": "blocked"},
        {"timestamp": now, "agent": "claude", "status": "complete"},
        {"timestamp": now, "agent": "claude", "status": "complete"},
    ]
    (metrics_dir / "agent_stats.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )

    cfg = {
        "root_dir": str(tmp_path),
        "default_agent": "auto",
        "default_task_type": "implementation",
        "default_base_branch": "main",
        "default_allow_push": True,
        "default_max_attempts": 4,
        "max_runtime_minutes": 40,
        "formatter_model": None,
        "agent_fallbacks": {"implementation": ["deepseek", "claude"]},
    }
    repo_cfg = {"local_repo": "/tmp/repo", "github_repo": "owner/repo"}
    issue = {
        "number": 99,
        "title": "Test adaptive gate",
        "url": "https://github.com/owner/repo/issues/99",
        "labels": [],
        "body": "## Goal\nTest.\n",
    }

    monkeypatch.setattr(gd, "format_task", lambda title, body, model=None: None)

    task_id, task_md = gd.build_mailbox_task(cfg, "proj", repo_cfg, issue)
    # deepseek should be skipped, claude assigned
    assert "agent: claude" in task_md or "agent: auto" in task_md


def test_build_mailbox_task_uses_task_type_specific_health_when_debugging(tmp_path, monkeypatch):
    metrics_dir = tmp_path / "runtime" / "metrics"
    metrics_dir.mkdir(parents=True)
    now = gd.datetime.now().isoformat()
    records = [
        {"timestamp": now, "agent": "codex", "status": "blocked", "task_type": "implementation"},
        {"timestamp": now, "agent": "codex", "status": "blocked", "task_type": "implementation"},
        {"timestamp": now, "agent": "codex", "status": "blocked", "task_type": "implementation"},
        {"timestamp": now, "agent": "codex", "status": "complete", "task_type": "debugging"},
        {"timestamp": now, "agent": "codex", "status": "complete", "task_type": "debugging"},
        {"timestamp": now, "agent": "codex", "status": "complete", "task_type": "debugging"},
    ]
    (metrics_dir / "agent_stats.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )

    cfg = {
        "root_dir": str(tmp_path),
        "default_agent": "auto",
        "default_task_type": "implementation",
        "default_base_branch": "main",
        "default_allow_push": True,
        "default_max_attempts": 4,
        "max_runtime_minutes": 40,
        "formatter_model": None,
        "agent_fallbacks": {"debugging": ["codex", "claude"]},
    }
    repo_cfg = {"local_repo": "/tmp/repo", "github_repo": "owner/repo"}
    issue = {
        "number": 100,
        "title": "Fix CI failure on PR #100",
        "url": "https://github.com/owner/repo/issues/100",
        "labels": [{"name": "codex"}],
        "body": """
## Goal
Fix CI.

## Task Type
debugging
""",
    }

    monkeypatch.setattr(gd, "format_task", lambda title, body, model=None: None)

    _task_id, task_md = gd.build_mailbox_task(cfg, "proj", repo_cfg, issue)
    assert "agent: codex" in task_md


def test_check_push_readiness_reports_missing_origin_remote(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()

    cfg = {"default_allow_push": True}
    repo_cfg = {"local_repo": str(repo)}

    calls = []

    class Result:
        def __init__(self, returncode, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(repo_path, *args):
        calls.append((repo_path, args))
        if args == ("rev-parse", "--is-inside-work-tree"):
            return Result(0, "true\n")
        if args == ("rev-parse", "--git-common-dir"):
            return Result(0, ".git\n")
        if args == ("remote", "get-url", "origin"):
            return Result(2, "", "error: No such remote 'origin'\n")
        raise AssertionError(args)

    monkeypatch.setattr(gd.shutil, "which", lambda cmd: "/usr/bin/git")
    monkeypatch.setattr(gd, "_run_git_readiness", fake_run)
    monkeypatch.setattr(gd.os, "access", lambda path, mode: True)

    readiness = gd._check_push_readiness(cfg, repo_cfg)

    assert readiness["ready"] is False
    assert readiness["failures"] == [{
        "code": "missing_origin_remote",
        "detail": "error: No such remote 'origin'",
    }]
    assert calls == [
        (repo, ("rev-parse", "--is-inside-work-tree")),
        (repo, ("rev-parse", "--git-common-dir")),
        (repo, ("remote", "get-url", "origin")),
    ]


def test_dispatch_item_blocks_publish_task_when_push_not_ready(tmp_path, monkeypatch):
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
    monkeypatch.setattr(
        gd,
        "_check_push_readiness",
        lambda cfg, repo_cfg: {
            "ready": False,
            "failures": [{"code": "allow_push_disabled", "detail": "default_allow_push is false"}],
        },
    )
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
        ["blocked", gd.PUSH_NOT_READY_LABEL],
        ["ready", "in-progress", "agent-dispatched"],
    )]
    assert len(comments) == 1
    assert gd.PUSH_NOT_READY_CODE in comments[0][2]
    payload = comments[0][2].splitlines()[1]
    assert json.loads(payload) == {
        "code": gd.PUSH_NOT_READY_CODE,
        "push_readiness": [{"code": "allow_push_disabled", "detail": "default_allow_push is false"}],
        "requirements": ["git_commit", "git_push", "push_branch", "open_pr", "publish_changes"],
        "runtime_allow_push": False,
    }


def test_skip_push_not_ready_persists_unblock_artifact(tmp_path, monkeypatch):
    """Push readiness block writes a structured unblock-notes artifact."""
    monkeypatch.setenv("ORCH_ROOT", str(tmp_path))
    cfg = {"default_allow_push": False, "github_owner": "owner"}
    paths = {"INBOX": tmp_path / "inbox"}
    paths["INBOX"].mkdir()
    info = {
        "status_field_id": "status-field",
        "status_options": {"Blocked": "blocked-option"},
        "project_id": "project-1",
    }
    ready_items = [{
        "item_id": "item-1",
        "number": 99,
        "title": "Push hotfix branch",
        "body": "Please git push the fix.",
        "url": "https://github.com/owner/repo/issues/99",
        "labels": {"ready"},
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

    monkeypatch.setattr(gd, "is_trusted", lambda author, _cfg: True)
    monkeypatch.setattr(gd, "_resolve_issue_dependencies", lambda *args, **kwargs: {"status": "clear"})
    monkeypatch.setattr(
        gd,
        "_check_push_readiness",
        lambda cfg, repo_cfg: {
            "ready": False,
            "failures": [{"code": "allow_push_disabled", "detail": "default_allow_push is false"}],
        },
    )
    monkeypatch.setattr(gd, "add_issue_comment", lambda *a, **kw: None)
    monkeypatch.setattr(gd, "edit_issue_labels", lambda *a, **kw: None)
    monkeypatch.setattr(gd, "set_item_status", lambda *a, **kw: None)

    gd._dispatch_item(cfg, paths, "owner", repo_to_project, info, ready_items, {})

    artifact_dir = tmp_path / "runtime" / "unblock_notes"
    artifacts = list(artifact_dir.glob("dispatch-owner-repo-99.yaml"))
    assert len(artifacts) == 1
    data = yaml.safe_load(artifacts[0].read_text())
    assert data["blocker_code"] == "push_not_ready"
    assert data["status"] == "blocked"
    assert "allow_push_disabled" in data["blocking_cause"]
    assert data["task_id"] == "dispatch-owner-repo-99"


def test_dispatch_item_blocks_task_when_agent_fallbacks_are_invalid(tmp_path, monkeypatch):
    cfg = {
        "default_agent": "auto",
        "default_task_type": "implementation",
        "default_allow_push": True,
        "github_owner": "owner",
        "agent_fallbacks": {"implementation": ["bogus-agent"]},
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
        "title": "Dispatch task with no runnable agent",
        "body": "Keep the diff minimal.",
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
    monkeypatch.setattr(gd, "_try_decompose", lambda *args, **kwargs: None)
    monkeypatch.setattr(gd, "format_task", lambda title, body, model=None: None)
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
        ["blocked", gd.AGENT_UNAVAILABLE_LABEL],
        ["ready", "in-progress", "agent-dispatched"],
    )]
    assert len(comments) == 1
    payload = comments[0][2].splitlines()[1]
    assert json.loads(payload) == {
        "code": gd.AGENT_UNAVAILABLE_CODE,
        "detail": (
            "Unsupported agent fallback(s) for task_type='implementation': "
            "bogus-agent. Expected only: claude, codex, deepseek, gemini."
        ),
    }


def test_dispatch_item_escalates_to_human_review_when_all_agents_unhealthy(tmp_path, monkeypatch):
    metrics_dir = tmp_path / "runtime" / "metrics"
    metrics_dir.mkdir(parents=True)
    now = gd.datetime.now().isoformat()
    # 12 samples per agent at ~17% success — crosses the >=10-sample genuinely-
    # broken threshold so the dispatcher still escalates instead of failing open.
    records = (
        [{"timestamp": now, "agent": "codex", "status": "complete"}] * 2 +
        [{"timestamp": now, "agent": "codex", "status": "blocked"}] * 10 +
        [{"timestamp": now, "agent": "claude", "status": "complete"}] * 2 +
        [{"timestamp": now, "agent": "claude", "status": "blocked"}] * 10
    )
    (metrics_dir / "agent_stats.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )

    cfg = {
        "root_dir": str(tmp_path),
        "default_agent": "auto",
        "default_task_type": "implementation",
        "default_allow_push": True,
        "github_owner": "owner",
        "agent_fallbacks": {"implementation": ["codex", "claude"]},
    }
    paths = {"INBOX": tmp_path / "inbox"}
    paths["INBOX"].mkdir()
    info = {
        "status_field_id": "status-field",
        "status_options": {"Blocked": "blocked-option"},
        "project_id": "project-1",
    }
    ready_items = [{
        "item_id": "item-1",
        "number": 42,
        "title": "Dispatch task with unhealthy agents",
        "body": "Keep the diff minimal.",
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

    monkeypatch.setattr(gd, "is_trusted", lambda author, _cfg: True)
    monkeypatch.setattr(gd, "_resolve_issue_dependencies", lambda *args, **kwargs: {"status": "clear"})
    monkeypatch.setattr(gd, "_try_decompose", lambda *args, **kwargs: None)
    monkeypatch.setattr(gd, "format_task", lambda title, body, model=None: None)
    monkeypatch.setattr(gd, "add_issue_comment", lambda repo, number, body: comments.append((repo, number, body)))
    monkeypatch.setattr(gd, "edit_issue_labels", lambda *args, **kwargs: None)
    monkeypatch.setattr(gd, "set_item_status", lambda *args, **kwargs: None)

    dispatched = gd._dispatch_item(cfg, paths, "owner", repo_to_project, info, ready_items, {})

    assert dispatched is False
    assert len(comments) == 1
    assert "Escalated for human review." in comments[0][2]
    payload = comments[0][2].splitlines()[1]
    assert "No healthy agents available" in json.loads(payload)["detail"]


def test_cluster_duplicate_debug_issues_blocks_matching_signature(monkeypatch):
    info = {
        "project_id": "project-1",
        "status_field_id": "status-field",
        "status_options": {"Blocked": "blocked-option"},
    }
    project_cfg = {"blocked_value": "Blocked"}
    primary = {
        "item_id": "item-1",
        "number": 101,
        "title": "Follow up partial debug for root issue #99",
        "body": """
## Task Type
debugging

## CI Failure Signature
checks=pytest | error=ImportError | location=src/app.py:17 | frame=load_config
""",
        "repo": "owner/repo",
    }
    duplicate = {
        "item_id": "item-2",
        "number": 102,
        "title": "Follow up partial debug for root issue #100",
        "body": """
## Task Type
debugging

## CI Failure Signature
checks=pytest | error=ImportError | location=src/app.py:17 | frame=load_config
""",
        "repo": "owner/repo",
    }
    unrelated = {
        "item_id": "item-3",
        "number": 103,
        "title": "Follow up partial debug for root issue #101",
        "body": """
## Task Type
debugging

## CI Failure Signature
checks=pytest | error=ValueError | location=src/app.py:22 | frame=parse_payload
""",
        "repo": "owner/repo",
    }
    gh_calls = []
    label_calls = []
    comments = []
    status_updates = []

    monkeypatch.setattr(gd, "gh", lambda args, check=False: gh_calls.append(args) or "")
    monkeypatch.setattr(gd, "edit_issue_labels", lambda repo, number, add=None, remove=None: label_calls.append((repo, number, add, remove)))
    monkeypatch.setattr(gd, "add_issue_comment", lambda repo, number, body: comments.append((repo, number, body)))
    monkeypatch.setattr(gd, "set_item_status", lambda project_id, item_id, field_id, option_id: status_updates.append((project_id, item_id, field_id, option_id)))

    gd._cluster_duplicate_debug_issues({}, "owner/repo", primary, info, project_cfg, [primary, duplicate, unrelated])

    assert gh_calls == [[
        "api",
        "repos/owner/repo/issues/102",
        "-X", "PATCH",
        "-f", "body=\n## Task Type\ndebugging\n\n## CI Failure Signature\nchecks=pytest | error=ImportError | location=src/app.py:17 | frame=load_config\n\nDepends on #101\n\n## Duplicate CI Signature Parent\n#101\n",
    ]]
    assert label_calls == [("owner/repo", 102, ["blocked"], ["ready", "in-progress", "agent-dispatched"])]
    assert len(comments) == 1
    assert "#101" in comments[0][2]
    assert "ImportError" in comments[0][2]
    assert status_updates == [("project-1", "item-2", "status-field", "blocked-option")]


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


# --- Task context validation tests ---


def test_validate_task_context_all_present():
    parsed = {"goal": "Do something", "success_criteria": "It works"}
    issue = {"url": "https://github.com/owner/repo/issues/1"}
    repo_cfg = {"github_repo": "owner/repo"}
    result = gd.validate_task_context(parsed, issue, repo_cfg)
    assert result["complete"] is True
    assert result["missing"] == []
    assert set(result["present"]) == {"issue_link", "repo", "task_description", "acceptance_criteria"}


def test_validate_task_context_missing_goal():
    parsed = {"goal": "", "success_criteria": "It works"}
    issue = {"url": "https://github.com/owner/repo/issues/1"}
    repo_cfg = {"github_repo": "owner/repo"}
    result = gd.validate_task_context(parsed, issue, repo_cfg)
    assert result["complete"] is False
    assert "task_description" in result["missing"]


def test_validate_task_context_missing_criteria():
    parsed = {"goal": "Do something", "success_criteria": ""}
    issue = {"url": "https://github.com/owner/repo/issues/1"}
    repo_cfg = {"github_repo": "owner/repo"}
    result = gd.validate_task_context(parsed, issue, repo_cfg)
    assert result["complete"] is False
    assert "acceptance_criteria" in result["missing"]


def test_validate_task_context_missing_issue_link():
    parsed = {"goal": "Do something", "success_criteria": "It works"}
    issue = {"url": ""}
    repo_cfg = {"github_repo": "owner/repo"}
    result = gd.validate_task_context(parsed, issue, repo_cfg)
    assert result["complete"] is False
    assert "issue_link" in result["missing"]


def test_validate_task_context_missing_repo():
    parsed = {"goal": "Do something", "success_criteria": "It works"}
    issue = {"url": "https://github.com/owner/repo/issues/1"}
    repo_cfg = {"github_repo": ""}
    result = gd.validate_task_context(parsed, issue, repo_cfg)
    assert result["complete"] is False
    assert "repo" in result["missing"]


def test_build_mailbox_task_records_context_complete(monkeypatch, tmp_path):
    cfg = {
        "default_agent": "auto",
        "default_task_type": "implementation",
        "default_base_branch": "main",
        "default_allow_push": True,
        "default_max_attempts": 4,
        "max_runtime_minutes": 40,
        "formatter_model": None,
        "root_dir": str(tmp_path),
    }
    repo_cfg = {"local_repo": "/tmp/repo", "github_repo": "owner/repo"}
    issue = {
        "number": 99,
        "title": "Well-structured task",
        "url": "https://github.com/owner/repo/issues/99",
        "labels": [],
        "body": "## Goal\nDo something\n\n## Success Criteria\n- It works\n",
    }
    monkeypatch.setattr(gd, "format_task", lambda title, body, model=None: None)
    _task_id, task_md = gd.build_mailbox_task(cfg, "proj", repo_cfg, issue)
    assert "context_complete: true" in task_md
    assert "context_missing" not in task_md

    log_file = tmp_path / "runtime" / "metrics" / "context_completeness.jsonl"
    assert log_file.exists()
    record = json.loads(log_file.read_text().strip())
    assert record["complete"] is True
    assert record["missing"] == []


def test_build_mailbox_task_records_context_incomplete(monkeypatch, tmp_path):
    cfg = {
        "default_agent": "auto",
        "default_task_type": "implementation",
        "default_base_branch": "main",
        "default_allow_push": True,
        "default_max_attempts": 4,
        "max_runtime_minutes": 40,
        "formatter_model": None,
        "root_dir": str(tmp_path),
    }
    repo_cfg = {"local_repo": "/tmp/repo", "github_repo": "owner/repo"}
    issue = {
        "number": 100,
        "title": "Sparse task",
        "url": "https://github.com/owner/repo/issues/100",
        "labels": [],
        "body": "Fix the thing",
    }
    monkeypatch.setattr(gd, "format_task", lambda title, body, model=None: None)
    _task_id, task_md = gd.build_mailbox_task(cfg, "proj", repo_cfg, issue)
    assert "context_complete: false" in task_md
    assert "context_missing:" in task_md

    log_file = tmp_path / "runtime" / "metrics" / "context_completeness.jsonl"
    assert log_file.exists()
    record = json.loads(log_file.read_text().strip())
    assert record["complete"] is False
    assert "acceptance_criteria" in record["missing"]
