"""Unit tests for pure functions in orchestrator/queue.py"""
import json
import os
import sys
import textwrap
import tempfile

import yaml
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Ensure orchestrator package is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator import github_dispatcher as gd
from orchestrator.repo_context import (
    gather_objective_alignment,
    gather_recent_git_state,
    read_evaluation_rubric,
)
from orchestrator.queue import (
    CommandExecutionError,
    _format_runner_failure,
    _parse_unblock_notes,
    _validate_workflow_files,
    agent_available,
    build_escalation_message,
    create_followup_task,
    downgrade_no_diff_complete,
    fallback_cooldown_remaining,
    get_agent_chain,
    handle_telegram_callback,
    handle_telegram_command,
    has_unpushed_commits,
    maybe_requeue_prompt_inspection_recovery,
    parse_agent_result,
    parse_bullets,
    record_metrics,
    recover_stalled_processing_tasks,
    rescue_git_progress,
    run_tests,
    save_telegram_action,
    should_attempt_git_rescue,
    split_section,
    start_fallback_cooldown,
    telegram_action_expired,
    parse_task,
    verify_pr_ci_debug_completion,
    write_unblock_notes_artifact,
    WorkflowValidationError,
    create_escalation_note,
    write_prompt,
    PromptTooLargeError,
    PROMPT_SIZE_LIMIT_BYTES,
    PERMANENT_INFRA_BLOCKERS,
    should_try_fallback,
)


# ---------------------------------------------------------------------------
# split_section
# ---------------------------------------------------------------------------

def test_split_section_basic():
    text = "STATUS: complete\n\nSUMMARY:\nDid the thing.\n\nNEXT_STEP:\nNone\n"
    assert split_section(text, "SUMMARY", ["NEXT_STEP"]) == "Did the thing."


def test_split_section_missing():
    assert split_section("STATUS: blocked\n", "SUMMARY", ["NEXT_STEP"]) == ""


def test_split_section_multiline():
    text = "DONE:\n- step one\n- step two\n\nBLOCKERS:\n- nothing\n"
    result = split_section(text, "DONE", ["BLOCKERS"])
    assert "step one" in result
    assert "step two" in result


# ---------------------------------------------------------------------------
# parse_bullets
# ---------------------------------------------------------------------------

def test_parse_bullets_normal():
    assert parse_bullets("- foo\n- bar") == ["- foo", "- bar"]


def test_parse_bullets_empty():
    assert parse_bullets("") == ["- None"]
    assert parse_bullets("   ") == ["- None"]


def test_parse_bullets_strips_whitespace():
    result = parse_bullets("  - foo  \n  - bar  ")
    assert result == ["- foo", "- bar"]


# ---------------------------------------------------------------------------
# get_agent_chain
# ---------------------------------------------------------------------------

def _cfg(fallbacks=None):
    # Isolate from the real runtime/metrics/agent_stats.jsonl so the health
    # gates don't strip agents based on whatever happened in production today.
    return {
        "default_agent": "auto",
        "default_task_type": "implementation",
        "root_dir": "/tmp/nonexistent-agent-os-test-root",
        "agent_fallbacks": fallbacks or {
            "implementation": ["codex", "claude", "gemini", "deepseek"],
        },
    }


def test_get_agent_chain_auto():
    from unittest.mock import patch
    with patch("orchestrator.queue.agent_available", return_value=(True, None)):
        chain = get_agent_chain({"task_type": "implementation"}, _cfg())
    assert chain == ["codex", "claude", "gemini", "deepseek"]


def test_get_agent_chain_requested_first():
    from unittest.mock import patch
    with patch("orchestrator.queue.agent_available", return_value=(True, None)):
        chain = get_agent_chain({"agent": "claude", "task_type": "implementation"}, _cfg())
    assert chain[0] == "claude"
    assert set(chain) == {"codex", "claude", "gemini", "deepseek"}


def test_get_agent_chain_unknown_type_falls_back_to_default():
    from unittest.mock import patch
    with patch("orchestrator.queue.agent_available", return_value=(True, None)):
        chain = get_agent_chain({"task_type": "unknown"}, _cfg())
    assert "codex" in chain


def test_get_agent_chain_rejects_invalid_requested_agent():
    from unittest.mock import patch
    with patch("orchestrator.queue.agent_available", return_value=(True, None)):
        chain = get_agent_chain({"agent": "bogus-agent", "task_type": "implementation"}, _cfg())
    assert chain == []


def test_get_agent_chain_skips_invalid_fallback_entries():
    cfg = _cfg({"implementation": ["bogus-agent", "codex", "auto", "claude"]})

    from unittest.mock import patch

    with patch("orchestrator.queue.agent_available", return_value=(True, None)):
        chain = get_agent_chain({"task_type": "implementation"}, cfg)
    assert chain == ["codex", "claude"]


def test_get_agent_chain_skips_unavailable_deepseek(monkeypatch):
    cfg = _cfg({"debugging": ["claude", "deepseek", "codex"]})

    def fake_available(agent):
        return (agent != "deepseek", None if agent != "deepseek" else "not configured")

    monkeypatch.setattr("orchestrator.queue.agent_available", fake_available)
    chain = get_agent_chain({"task_type": "debugging"}, cfg)
    assert chain == ["claude", "codex"]


def test_get_agent_chain_skips_deepseek_when_openrouter_credential_missing(monkeypatch, tmp_path):
    cfg = _cfg({"implementation": ["deepseek", "codex", "claude"]})
    openrouter_dir = tmp_path / "openrouter"
    openrouter_dir.mkdir()
    (openrouter_dir / "secrets.json").write_text("{}", encoding="utf-8")

    monkeypatch.setenv("CLINE_BIN", "cline")
    monkeypatch.setenv("DEEPSEEK_OPENROUTER_CONFIG", str(openrouter_dir))
    monkeypatch.delenv("DEEPSEEK_NANOGPT_CONFIG", raising=False)
    monkeypatch.delenv("DEEPSEEK_CHUTES_CONFIG", raising=False)
    monkeypatch.setattr("orchestrator.queue._command_available", lambda cmd: True)

    chain = get_agent_chain({"task_type": "implementation"}, cfg)
    assert chain == ["codex", "claude"]


def test_get_agent_chain_skips_agents_below_recent_health_threshold(tmp_path):
    metrics_dir = tmp_path / "runtime" / "metrics"
    metrics_dir.mkdir(parents=True)
    now = datetime.now(timezone.utc).isoformat()
    records = [
        {"timestamp": now, "agent": "codex", "status": "complete"},
        {"timestamp": now, "agent": "claude", "status": "complete"},
        {"timestamp": now, "agent": "claude", "status": "blocked"},
        {"timestamp": now, "agent": "claude", "status": "blocked"},
        {"timestamp": now, "agent": "claude", "status": "blocked"},
        {"timestamp": now, "agent": "claude", "status": "blocked"},
    ]
    (metrics_dir / "agent_stats.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )

    from unittest.mock import patch

    with patch("orchestrator.queue.agent_available", return_value=(True, None)):
        chain = get_agent_chain(
            {"task_type": "implementation"},
            {**_cfg({"implementation": ["claude", "codex"]}), "root_dir": str(tmp_path)},
        )

    assert chain == ["codex"]


def test_get_agent_chain_allows_agents_without_recent_metrics(tmp_path):
    metrics_dir = tmp_path / "runtime" / "metrics"
    metrics_dir.mkdir(parents=True)
    now = datetime.now(timezone.utc).isoformat()
    (metrics_dir / "agent_stats.jsonl").write_text(
        json.dumps({"timestamp": now, "agent": "claude", "status": "complete"}) + "\n",
        encoding="utf-8",
    )

    from unittest.mock import patch

    with patch("orchestrator.queue.agent_available", return_value=(True, None)):
        chain = get_agent_chain(
            {"task_type": "implementation"},
            {**_cfg({"implementation": ["gemini", "claude"]}), "root_dir": str(tmp_path)},
        )

    assert chain == ["gemini", "claude"]


def test_record_metrics_persists_model_attempt_details(tmp_path):
    logfile = tmp_path / "queue.log"
    summary_log = tmp_path / "summary.log"
    meta = {
        "task_id": "task-123",
        "repo": "/tmp/repo",
        "github_repo": "acme/repo",
        "task_type": "implementation",
        "model_attempt_details": [
            {
                "attempt": 1,
                "agent": "codex",
                "provider": "openai",
                "model": "codex",
                "input_chars": 400,
                "input_tokens_estimate": 100,
                "output_chars": 120,
                "output_tokens_estimate": 30,
                "status": "complete",
                "blocker_code": "none",
            }
        ],
    }

    record_metrics(
        {"root_dir": str(tmp_path)},
        meta,
        {"status": "complete", "blocker_code": "none"},
        "codex",
        ["codex"],
        datetime.now(timezone.utc) - timedelta(seconds=3),
        logfile,
        summary_log,
    )

    metrics_path = tmp_path / "runtime" / "metrics" / "agent_stats.jsonl"
    lines = metrics_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["github_repo"] == "acme/repo"
    assert record["model_attempt_details"][0]["model"] == "codex"
    assert record["model_attempt_details"][0]["input_tokens_estimate"] == 100


def test_recover_stalled_processing_task_requeues_crashed_worker(tmp_path, monkeypatch):
    from unittest.mock import ANY

    mailbox = tmp_path / "runtime" / "mailbox"
    processing = mailbox / "processing"
    inbox = mailbox / "inbox"
    blocked = mailbox / "blocked"
    for path in (processing, inbox, blocked):
        path.mkdir(parents=True, exist_ok=True)

    task_path = processing / "task-stalled.md"
    task_path.write_text(
        """---
task_id: task-stalled
repo: /tmp/repo
agent: codex
attempt: 1
max_attempts: 3
model_attempts:
  - codex
---

# Goal

Recover me
""",
        encoding="utf-8",
    )
    stale_time = datetime.now(timezone.utc) - timedelta(minutes=45)
    os.utime(task_path, (stale_time.timestamp(), stale_time.timestamp()))
    lock_path = processing / "task-stalled.md.lock.json"
    lock_path.write_text(json.dumps({"pid": 999999, "worker_id": "w0", "agent": "codex"}), encoding="utf-8")

    sent = []
    audits = []
    monkeypatch.setattr("orchestrator.queue.send_telegram", lambda cfg, text, *args, **kwargs: sent.append(text) or 1)
    monkeypatch.setattr("orchestrator.queue.append_audit_event", lambda cfg, event_type, payload: audits.append((event_type, payload)) or {})

    recovered = recover_stalled_processing_tasks(
        {"default_max_attempts": 4, "max_processing_minutes": 30},
        {"PROCESSING": processing, "INBOX": inbox, "BLOCKED": blocked},
        now=datetime.now(timezone.utc),
    )

    assert recovered and recovered[0]["action"] == "requeued"
    requeued = inbox / "task-stalled.md"
    assert requeued.exists()
    updated = requeued.read_text(encoding="utf-8")
    assert "attempt: 2" in updated
    assert "model_attempts: []" in updated
    assert "stalled_recovered_by: processing_stall_watchdog" in updated
    assert "stalled_duration_minutes:" in updated
    assert not task_path.exists()
    assert not lock_path.exists()
    assert sent and "Task: task-stalled" in sent[0] and "Agent: codex" in sent[0]
    assert audits == [("stalled_task_requeued", ANY)]

    assert recover_stalled_processing_tasks(
        {"default_max_attempts": 4, "max_processing_minutes": 30},
        {"PROCESSING": processing, "INBOX": inbox, "BLOCKED": blocked},
        now=datetime.now(timezone.utc),
    ) == []


def test_recover_stalled_processing_task_blocks_when_attempts_exhausted(tmp_path, monkeypatch):
    from unittest.mock import ANY

    mailbox = tmp_path / "runtime" / "mailbox"
    processing = mailbox / "processing"
    inbox = mailbox / "inbox"
    blocked = mailbox / "blocked"
    for path in (processing, inbox, blocked):
        path.mkdir(parents=True, exist_ok=True)

    task_path = processing / "task-exhausted.md"
    task_path.write_text(
        """---
task_id: task-exhausted
repo: /tmp/repo
agent: claude
attempt: 3
max_attempts: 3
---

# Goal

Recover me
""",
        encoding="utf-8",
    )
    stale_time = datetime.now(timezone.utc) - timedelta(minutes=50)
    os.utime(task_path, (stale_time.timestamp(), stale_time.timestamp()))
    lock_path = processing / "task-exhausted.md.lock.json"
    lock_path.write_text(json.dumps({"pid": 999999, "worker_id": "w9", "agent": "claude"}), encoding="utf-8")

    sent = []
    audits = []
    monkeypatch.setattr("orchestrator.queue.send_telegram", lambda cfg, text, *args, **kwargs: sent.append(text) or 1)
    monkeypatch.setattr("orchestrator.queue.append_audit_event", lambda cfg, event_type, payload: audits.append((event_type, payload)) or {})

    recovered = recover_stalled_processing_tasks(
        {"default_max_attempts": 4, "max_processing_minutes": 30},
        {"PROCESSING": processing, "INBOX": inbox, "BLOCKED": blocked},
        now=datetime.now(timezone.utc),
    )

    assert recovered and recovered[0]["action"] == "blocked"
    blocked_task = blocked / "task-exhausted.md"
    assert blocked_task.exists()
    blocked_text = blocked_task.read_text(encoding="utf-8")
    assert "blocker_code: worker_crash" in blocked_text
    assert "stalled_recovered_by: processing_stall_watchdog" in blocked_text
    assert not (inbox / "task-exhausted.md").exists()
    assert not lock_path.exists()
    assert sent and "worker_crash" in sent[0]
    assert audits == [("stalled_task_blocked", ANY)]


def test_recover_stalled_processing_task_skips_live_pid_lock(tmp_path, monkeypatch):
    mailbox = tmp_path / "runtime" / "mailbox"
    processing = mailbox / "processing"
    inbox = mailbox / "inbox"
    blocked = mailbox / "blocked"
    for path in (processing, inbox, blocked):
        path.mkdir(parents=True, exist_ok=True)

    task_path = processing / "task-live.md"
    task_path.write_text(
        """---
task_id: task-live
repo: /tmp/repo
agent: codex
attempt: 1
max_attempts: 3
---

# Goal

Do not touch me
""",
        encoding="utf-8",
    )
    stale_time = datetime.now(timezone.utc) - timedelta(minutes=60)
    os.utime(task_path, (stale_time.timestamp(), stale_time.timestamp()))
    lock_path = processing / "task-live.md.lock.json"
    lock_path.write_text(json.dumps({"pid": os.getpid(), "worker_id": "w1", "agent": "codex"}), encoding="utf-8")

    monkeypatch.setattr("orchestrator.queue.send_telegram", lambda *args, **kwargs: pytest.fail("should not alert"))
    monkeypatch.setattr("orchestrator.queue.append_audit_event", lambda *args, **kwargs: pytest.fail("should not audit"))

    recovered = recover_stalled_processing_tasks(
        {"default_max_attempts": 4, "max_processing_minutes": 30},
        {"PROCESSING": processing, "INBOX": inbox, "BLOCKED": blocked},
        now=datetime.now(timezone.utc),
    )

    assert recovered == []
    assert task_path.exists()
    assert lock_path.exists()
    assert not any(inbox.glob("*.md"))
    assert not any(blocked.glob("*.md"))


def test_get_agent_chain_skips_agents_below_adaptive_health_threshold(tmp_path):
    """Agents with <25% success over 7 days are skipped by the adaptive gate."""
    metrics_dir = tmp_path / "runtime" / "metrics"
    metrics_dir.mkdir(parents=True)
    now = datetime.now(timezone.utc).isoformat()
    # deepseek: 0% success (all blocked), claude: 100% success
    records = [
        {"timestamp": now, "agent": "deepseek", "status": "blocked"},
        {"timestamp": now, "agent": "deepseek", "status": "blocked"},
        {"timestamp": now, "agent": "deepseek", "status": "blocked"},
        {"timestamp": now, "agent": "claude", "status": "complete"},
        {"timestamp": now, "agent": "claude", "status": "complete"},
        {"timestamp": now, "agent": "claude", "status": "complete"},
    ]
    (metrics_dir / "agent_stats.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )

    from unittest.mock import patch

    with patch("orchestrator.queue.agent_available", return_value=(True, None)):
        chain = get_agent_chain(
            {"task_type": "implementation"},
            {**_cfg({"implementation": ["deepseek", "claude"]}), "root_dir": str(tmp_path)},
        )

    assert "deepseek" not in chain
    assert "claude" in chain


def test_get_agent_chain_uses_task_type_specific_health_when_sample_is_sufficient(tmp_path):
    metrics_dir = tmp_path / "runtime" / "metrics"
    metrics_dir.mkdir(parents=True)
    now = datetime.now(timezone.utc).isoformat()
    records = [
        {"timestamp": now, "agent": "codex", "status": "blocked", "task_type": "implementation"},
        {"timestamp": now, "agent": "codex", "status": "blocked", "task_type": "implementation"},
        {"timestamp": now, "agent": "codex", "status": "blocked", "task_type": "implementation"},
        {"timestamp": now, "agent": "codex", "status": "complete", "task_type": "debugging"},
        {"timestamp": now, "agent": "codex", "status": "complete", "task_type": "debugging"},
        {"timestamp": now, "agent": "codex", "status": "complete", "task_type": "debugging"},
        {"timestamp": now, "agent": "codex", "status": "complete", "task_type": "debugging"},
        {"timestamp": now, "agent": "codex", "status": "complete", "task_type": "debugging"},
        {"timestamp": now, "agent": "claude", "status": "complete", "task_type": "implementation"},
    ]
    (metrics_dir / "agent_stats.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )

    from unittest.mock import patch

    with patch("orchestrator.queue.agent_available", return_value=(True, None)):
        chain = get_agent_chain(
            {"task_type": "debugging"},
            {**_cfg({"debugging": ["codex", "claude"]}), "root_dir": str(tmp_path)},
        )

    assert chain == ["codex", "claude"]


def test_get_agent_chain_prefers_repo_specific_fallbacks():
    cfg = {
        **_cfg(
            {
                "implementation": ["claude", "codex", "gemini", "deepseek"],
                "debugging": ["claude", "codex", "gemini", "deepseek"],
            }
        ),
        "github_projects": {
            "agent-os": {
                "agent_fallbacks": {
                    "implementation": ["codex", "claude", "gemini", "deepseek"],
                    "debugging": ["codex", "claude", "gemini", "deepseek"],
                }
            }
        },
    }

    from unittest.mock import patch

    with patch("orchestrator.queue.agent_available", return_value=(True, None)):
        chain = get_agent_chain(
            {"task_type": "debugging", "github_project_key": "agent-os"},
            cfg,
        )

    assert chain == ["codex", "claude", "gemini", "deepseek"]


def test_write_prompt_includes_layered_repo_context(tmp_path):
    root = tmp_path / "root"
    worktree = tmp_path / "repo"
    root.mkdir()
    worktree.mkdir()
    (worktree / "README.md").write_text("## Goal\n\nShip autonomous improvements.\n", encoding="utf-8")
    (worktree / "NORTH_STAR.md").write_text("Closed-loop self-improvement.\n", encoding="utf-8")
    (worktree / "STRATEGY.md").write_text("## Product Vision\n\nClosed-loop optimization.\n", encoding="utf-8")
    (worktree / "PLANNING_PRINCIPLES.md").write_text("Prefer autonomy gains.\n", encoding="utf-8")
    (worktree / "CODEBASE.md").write_text("# Codebase Memory\n\nKnown gotcha.\n", encoding="utf-8")
    (worktree / "PRODUCTION_FEEDBACK.md").write_text("# Production Feedback\n\nSignal evidence.\n", encoding="utf-8")
    (worktree / "PLANNING_RESEARCH.md").write_text("# Planning Research\n\nResearch evidence.\n", encoding="utf-8")

    prompt_file = write_prompt(
        "task-1",
        {"task_type": "research"},
        "Investigate roadmap and analytics gaps.",
        "codex",
        [],
        root,
        worktree=worktree,
    )

    text = prompt_file.read_text(encoding="utf-8")
    assert "Product Goal (README.md)" in text
    assert "North Star (NORTH_STAR.md)" in text
    assert "Strategy Context (STRATEGY.md)" in text
    assert "Planning Principles (PLANNING_PRINCIPLES.md)" in text
    assert "Production Feedback (PRODUCTION_FEEDBACK.md)" in text
    assert "Planning Research (PLANNING_RESEARCH.md)" in text
    assert "Codebase Memory (read-only context)" in text
    snapshot = root / "runtime" / "prompts" / "task-1.txt"
    assert snapshot.read_text(encoding="utf-8") == text


def test_write_prompt_skips_research_for_plain_implementation(tmp_path):
    root = tmp_path / "root"
    worktree = tmp_path / "repo"
    root.mkdir()
    worktree.mkdir()
    (worktree / "README.md").write_text("## Goal\n\nShip autonomous improvements.\n", encoding="utf-8")
    (worktree / "NORTH_STAR.md").write_text("Closed-loop self-improvement.\n", encoding="utf-8")
    (worktree / "STRATEGY.md").write_text("## Product Vision\n\nClosed-loop optimization.\n", encoding="utf-8")
    (worktree / "PLANNING_PRINCIPLES.md").write_text("Prefer autonomy gains.\n", encoding="utf-8")
    (worktree / "CODEBASE.md").write_text("# Codebase Memory\n\nKnown gotcha.\n", encoding="utf-8")
    (worktree / "PLANNING_RESEARCH.md").write_text("# Planning Research\n\nResearch evidence.\n", encoding="utf-8")

    prompt_file = write_prompt(
        "task-2",
        {"task_type": "implementation"},
        "Fix the queue lock handling.",
        "codex",
        [],
        root,
        worktree=worktree,
    )

    text = prompt_file.read_text(encoding="utf-8")
    assert "Planning Research (PLANNING_RESEARCH.md)" not in text
    snapshot = root / "runtime" / "prompts" / "task-2.txt"
    assert snapshot.read_text(encoding="utf-8") == text


def test_goal_ancestry_flows_from_dispatcher_to_prompt_and_result(tmp_path, monkeypatch):
    root = tmp_path / "root"
    repo = tmp_path / "repo"
    root.mkdir()
    repo.mkdir()
    (repo / "README.md").write_text("## Goal\n\nShip autonomous improvements.\n", encoding="utf-8")
    (repo / "CODEBASE.md").write_text("# Codebase Memory\n\nKnown gotcha.\n", encoding="utf-8")
    (repo / "runtime").mkdir()
    (repo / "runtime" / "next_sprint_focus.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-04-21T09:00:00+00:00",
                "headline": "Carry objective and sprint context into dispatched work",
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
        "root_dir": str(root),
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
    monkeypatch.setattr("orchestrator.queue.load_config", lambda: cfg)

    task_id, task_text = gd.build_mailbox_task(cfg, "proj", repo_cfg, issue)
    task_path = tmp_path / f"{task_id}.md"
    task_path.write_text(task_text, encoding="utf-8")
    meta, body = parse_task(task_path)

    prompt_file = write_prompt(task_id, meta, body, "codex", [], root, worktree=repo)
    prompt_text = prompt_file.read_text(encoding="utf-8")
    assert "## Goal Ancestry" in prompt_text
    assert "Objective: `repo`" in prompt_text
    assert "Parent issue: [owner/repo#240](https://github.com/owner/repo/issues/240)" in prompt_text

    result = {
        "status": "blocked",
        "blocker_code": "missing_context",
        "summary": "Need to confirm ancestry appears in all escalation surfaces.",
        "done": ["- Added ancestry fields to task frontmatter"],
        "blockers": ["- Still validating escalation rendering"],
        "next_step": "Retry after checking escalation output.",
        "files_changed": ["- orchestrator/github_dispatcher.py"],
        "tests_run": ["- pytest tests/test_queue.py -q → passed"],
        "decisions": ["- Persist ancestry in frontmatter instead of recomputing later"],
        "risks": ["- Summary must stay bounded"],
        "attempted_approaches": ["- Verified prompt rendering from dispatcher output"],
        "unblock_notes": None,
    }
    logfile = tmp_path / "task.log"
    logfile.write_text("", encoding="utf-8")
    queue_summary_log = tmp_path / "queue-summary.log"
    queue_summary_log.write_text("", encoding="utf-8")
    escalated = tmp_path / "escalated"
    escalated.mkdir()

    esc_path = create_escalation_note(meta, body, result, logfile, ["codex"], escalated, queue_summary_log)
    esc_text = esc_path.read_text(encoding="utf-8")
    assert "## Goal Ancestry" in esc_text
    assert "Objective: `repo`" in esc_text
    assert "Issue owner/repo#240" in esc_text


def test_read_evaluation_rubric_returns_content(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "RUBRIC.md").write_text("### Quality\nShip reliable code.\n", encoding="utf-8")
    result = read_evaluation_rubric(repo)
    assert "Quality" in result
    assert "Ship reliable code." in result


def test_read_evaluation_rubric_returns_empty_when_missing(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    result = read_evaluation_rubric(repo)
    assert result == ""


def test_read_evaluation_rubric_truncates_at_max_chars(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "RUBRIC.md").write_text("x" * 5000, encoding="utf-8")
    result = read_evaluation_rubric(repo, max_chars=100)
    assert len(result) == 100


def test_write_prompt_includes_rubric_when_present(tmp_path):
    root = tmp_path / "root"
    worktree = tmp_path / "repo"
    root.mkdir()
    worktree.mkdir()
    (worktree / "README.md").write_text("## Goal\n\nShip stuff.\n", encoding="utf-8")
    (worktree / "RUBRIC.md").write_text("### Execution Reliability\nTasks complete reliably.\n", encoding="utf-8")

    prompt_file = write_prompt(
        "task-rubric",
        {"task_type": "architecture"},
        "Design domain rubric system.",
        "claude",
        [],
        root,
        worktree=worktree,
    )

    text = prompt_file.read_text(encoding="utf-8")
    assert "Domain Evaluation Rubric (RUBRIC.md)" in text
    assert "Execution Reliability" in text


def test_write_prompt_omits_rubric_when_absent(tmp_path):
    root = tmp_path / "root"
    worktree = tmp_path / "repo"
    root.mkdir()
    worktree.mkdir()
    (worktree / "README.md").write_text("## Goal\n\nShip stuff.\n", encoding="utf-8")

    prompt_file = write_prompt(
        "task-no-rubric",
        {"task_type": "architecture"},
        "Design something.",
        "claude",
        [],
        root,
        worktree=worktree,
    )

    text = prompt_file.read_text(encoding="utf-8")
    assert "Domain Evaluation Rubric (RUBRIC.md)" not in text


# ---------------------------------------------------------------------------
# parse_agent_result
# ---------------------------------------------------------------------------

def _write_result(tmp: Path, content: str) -> Path:
    f = tmp / ".agent_result.md"
    f.write_text(content)
    return tmp


def test_parse_agent_result_complete():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _write_result(tmp, textwrap.dedent("""\
            STATUS: complete

            BLOCKER_CODE:
            none

            SUMMARY:
            Implemented the feature.

            DONE:
            - wrote code

            BLOCKERS:
            - None

            NEXT_STEP:
            None

            FILES_CHANGED:
            - src/foo.py

            TESTS_RUN:
            - pytest

            DECISIONS:
            - chose approach A

            RISKS:
            - None

            ATTEMPTED_APPROACHES:
            - direct implementation

            MANUAL_STEPS:
            - None
        """))
        result = parse_agent_result(tmp)
        assert result["status"] == "complete"
        assert result["blocker_code"] == ""
        assert "Implemented the feature" in result["summary"]
        assert result["files_changed"] == ["- src/foo.py"]


def test_parse_agent_result_missing_file():
    with tempfile.TemporaryDirectory() as d:
        result = parse_agent_result(Path(d))
        assert result["status"] == "blocked"
        assert result["blocker_code"] == "invalid_result_contract"
        assert "No .agent_result.md" in result["summary"]


def test_parse_agent_result_invalid_status_normalised():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _write_result(tmp, "STATUS: weirdvalue\n\nSUMMARY:\nOops.\n")
        result = parse_agent_result(tmp)
        assert result["status"] == "blocked"
        assert result["blocker_code"] == "invalid_result_contract"


def test_parse_agent_result_blocked_requires_blocker_code():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _write_result(tmp, textwrap.dedent("""\
            STATUS: blocked

            SUMMARY:
            Missing spec details.

            DONE:
            - Investigated the repo

            BLOCKERS:
            - Missing API contract

            NEXT_STEP:
            Ask for the missing API contract.
        """))
        result = parse_agent_result(tmp)
        assert result["status"] == "blocked"
        assert result["blocker_code"] == "invalid_result_contract"
        assert "must include a valid BLOCKER_CODE" in result["blockers"][0]


def test_parse_agent_result_partial_accepts_valid_blocker_code():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _write_result(tmp, textwrap.dedent("""\
            STATUS: partial

            BLOCKER_CODE:
            missing_context

            SUMMARY:
            Made progress but the API contract is missing.

            DONE:
            - Implemented the scaffolding

            BLOCKERS:
            - API response schema is undocumented

            NEXT_STEP:
            Add the schema details and continue.

            UNBLOCK_NOTES:
            - blocking_cause: API response schema is undocumented
            - next_action: Request API schema from the team and continue implementation.
        """))
        result = parse_agent_result(tmp)
        assert result["status"] == "partial"
        assert result["blocker_code"] == "missing_context"
        assert result["unblock_notes"]["blocking_cause"] == "API response schema is undocumented"
        assert result["unblock_notes"]["next_action"] == "Request API schema from the team and continue implementation."


def test_parse_agent_result_partial_requires_unblock_notes():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _write_result(tmp, textwrap.dedent("""\
            STATUS: partial

            BLOCKER_CODE:
            missing_context

            SUMMARY:
            Made progress but the API contract is missing.

            DONE:
            - Implemented the scaffolding

            BLOCKERS:
            - API response schema is undocumented

            NEXT_STEP:
            Add the schema details and continue.
        """))
        result = parse_agent_result(tmp)
        assert result["status"] == "blocked"
        assert result["blocker_code"] == "invalid_result_contract"
        assert "UNBLOCK_NOTES" in result["blockers"][0]


def test_parse_agent_result_complete_does_not_require_unblock_notes():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _write_result(tmp, textwrap.dedent("""\
            STATUS: complete

            BLOCKER_CODE:
            none

            SUMMARY:
            Done.

            DONE:
            - Finished

            BLOCKERS:
            - None

            NEXT_STEP:
            None
        """))
        result = parse_agent_result(tmp)
        assert result["status"] == "complete"
        assert result.get("unblock_notes") == {}


def test_parse_unblock_notes_valid():
    raw = "- blocking_cause: API schema missing\n- next_action: Request schema from team"
    result = _parse_unblock_notes(raw)
    assert result == {"blocking_cause": "API schema missing", "next_action": "Request schema from team"}


def test_parse_unblock_notes_empty_returns_empty_dict():
    assert _parse_unblock_notes("") == {}
    assert _parse_unblock_notes("- blocking_cause: none\n- next_action: do something") == {}
    assert _parse_unblock_notes("- blocking_cause: real cause\n- next_action: none") == {}


def test_write_unblock_notes_artifact(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        monkeypatch.setenv("ORCH_ROOT", d)
        notes = {"blocking_cause": "Missing credentials", "next_action": "Add API key to secrets"}
        result = {"status": "blocked", "blocker_code": "missing_credentials"}
        path = write_unblock_notes_artifact("task-123", notes, result)
        assert path is not None
        assert path.exists()
        content = yaml.safe_load(path.read_text())
        assert content["task_id"] == "task-123"
        assert content["blocking_cause"] == "Missing credentials"
        assert content["next_action"] == "Add API key to secrets"
        assert content["blocker_code"] == "missing_credentials"


def test_write_unblock_notes_artifact_skips_empty():
    assert write_unblock_notes_artifact("task-123", {}, {}) is None


def test_verify_pr_ci_debug_completion_requires_post_attempt_rerun(monkeypatch):
    monkeypatch.setattr(
        "orchestrator.queue.gh_json",
        lambda args: {"workflow_runs": []},
    )

    result = verify_pr_ci_debug_completion(
        {
            "task_type": "debugging",
            "github_repo": "owner/repo",
            "github_issue_title": "Fix CI failure on PR #71",
            "branch": "agent/task-71",
        },
        textwrap.dedent("""\
            # Context

            - PR: https://github.com/owner/repo/pull/71
            - Failed checks:
            - **pytest**: `failure`
        """),
        {"status": "complete", "summary": "fixed", "blockers": ["- None"], "decisions": ["- Kept diff minimal."]},
        commit_hash="abc123",
        task_started_at=datetime(2026, 3, 20, 10, 0, tzinfo=timezone.utc),
    )

    assert result["status"] == "partial"
    assert result["blocker_code"] == "dependency_blocked"
    assert result["ci_rerun_reason"] == "missing_rerun"
    assert "No GitHub Actions workflow rerun was recorded" in result["summary"]


def test_verify_pr_ci_debug_completion_requires_passing_rerun_for_prior_failed_job(monkeypatch):
    def fake_gh_json(args):
        if args == ["api", "repos/owner/repo/actions/runs?branch=agent/task-71&per_page=20"]:
            return {
                "workflow_runs": [
                    {
                        "id": 201,
                        "head_branch": "agent/task-71",
                        "head_sha": "abc123",
                        "status": "completed",
                        "created_at": "2026-03-20T10:15:00Z",
                    }
                ]
            }
        if args == ["api", "repos/owner/repo/actions/runs/201/jobs?per_page=100"]:
            return {
                "jobs": [
                    {"name": "pytest", "conclusion": "failure"},
                ]
            }
        raise AssertionError(args)

    monkeypatch.setattr("orchestrator.queue.gh_json", fake_gh_json)

    result = verify_pr_ci_debug_completion(
        {
            "task_type": "debugging",
            "github_repo": "owner/repo",
            "github_issue_title": "Fix CI failure on PR #71",
            "branch": "agent/task-71",
        },
        textwrap.dedent("""\
            # Context

            - PR: https://github.com/owner/repo/pull/71
            - Failed checks:
            - **pytest**: `failure`
        """),
        {"status": "complete", "summary": "fixed", "blockers": ["- None"], "decisions": ["- Kept diff minimal."]},
        commit_hash="abc123",
        task_started_at=datetime(2026, 3, 20, 10, 0, tzinfo=timezone.utc),
    )

    assert result["status"] == "partial"
    assert result["blocker_code"] == "test_failure"
    assert result["ci_rerun_reason"] == "rerun_failed"
    assert "pytest" in result["summary"]


def test_verify_pr_ci_debug_completion_accepts_green_rerun_for_prior_failed_job(monkeypatch):
    def fake_gh_json(args):
        if args == ["api", "repos/owner/repo/actions/runs?branch=agent/task-71&per_page=20"]:
            return {
                "workflow_runs": [
                    {
                        "id": 202,
                        "head_branch": "agent/task-71",
                        "head_sha": "abc123",
                        "status": "completed",
                        "created_at": "2026-03-20T10:15:00Z",
                    }
                ]
            }
        if args == ["api", "repos/owner/repo/actions/runs/202/jobs?per_page=100"]:
            return {
                "jobs": [
                    {"name": "pytest", "conclusion": "success"},
                ]
            }
        raise AssertionError(args)

    monkeypatch.setattr("orchestrator.queue.gh_json", fake_gh_json)
    original = {
        "status": "complete",
        "summary": "fixed",
        "blockers": ["- None"],
        "decisions": ["- Kept diff minimal."],
    }

    result = verify_pr_ci_debug_completion(
        {
            "task_type": "debugging",
            "github_repo": "owner/repo",
            "github_issue_title": "Fix CI failure on PR #71",
            "branch": "agent/task-71",
        },
        textwrap.dedent("""\
            # Context

            - PR: https://github.com/owner/repo/pull/71
            - Failed checks:
            - **pytest**: `failure`
        """),
        original,
        commit_hash="abc123",
        task_started_at=datetime(2026, 3, 20, 10, 0, tzinfo=timezone.utc),
    )

    assert result is original


def test_verify_pr_ci_debug_completion_uses_meta_failed_checks_over_body(monkeypatch):
    """Structured failed_checks in meta prevents cascading failures when body
    markdown is reformatted and check names are lost (PR-98 RCA fix)."""
    def fake_gh_json(args):
        if "actions/runs?" in str(args):
            return {
                "workflow_runs": [
                    {
                        "id": 300,
                        "head_branch": "agent/task-98",
                        "head_sha": "def456",
                        "status": "completed",
                        "created_at": "2026-03-31T10:15:00Z",
                    }
                ]
            }
        if "actions/runs/300/jobs" in str(args):
            return {
                "jobs": [
                    {"name": "pytest", "conclusion": "success"},
                ]
            }
        raise AssertionError(args)

    monkeypatch.setattr("orchestrator.queue.gh_json", fake_gh_json)
    original = {
        "status": "complete",
        "summary": "fixed",
        "blockers": ["- None"],
        "decisions": ["- Kept diff minimal."],
    }

    # Body has NO failed check markdown — simulates follow-up reformatting.
    # Meta carries structured failed_checks from frontmatter.
    result = verify_pr_ci_debug_completion(
        {
            "task_type": "debugging",
            "github_repo": "owner/repo",
            "github_issue_title": "Fix CI failure on PR #98",
            "branch": "agent/task-98",
            "failed_checks": ["pytest"],
        },
        "Follow-up task body with no markdown check lines.",
        original,
        commit_hash="def456",
        task_started_at=datetime(2026, 3, 31, 10, 0, tzinfo=timezone.utc),
    )

    assert result is original, (
        "Should use meta.failed_checks and pass verification, not downgrade to partial"
    )


def test_parse_agent_result_manual_steps():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _write_result(tmp, textwrap.dedent("""\
            STATUS: complete

            BLOCKER_CODE:
            none

            SUMMARY:
            Done.

            DONE:
            - x

            BLOCKERS:
            - None

            NEXT_STEP:
            None

            FILES_CHANGED:
            - None

            TESTS_RUN:
            - None

            DECISIONS:
            - None

            RISKS:
            - None

            ATTEMPTED_APPROACHES:
            - None

            MANUAL_STEPS:
            - Add cron: 0 7 * * 1 /path/to/agent-os/bin/run_thing.sh
        """))
        result = parse_agent_result(tmp)
        assert "Add cron" in result["manual_steps"]


def test_run_tests_marks_complete_result_partial_with_test_failure_code(tmp_path):
    result_path = tmp_path / ".agent_result.md"
    result_path.write_text(textwrap.dedent("""\
        STATUS: complete

        BLOCKER_CODE:
        none

        SUMMARY:
        Done.

        DONE:
        - Implemented it

        BLOCKERS:
        - None

        NEXT_STEP:
        None
    """), encoding="utf-8")

    cfg = {"test_command": "pytest", "test_timeout_minutes": 1}
    run_tests(cfg, tmp_path, tmp_path, tmp_path / "queue.log", tmp_path / "summary.log")

    updated = result_path.read_text(encoding="utf-8")
    assert "STATUS: partial" in updated
    assert "BLOCKER_CODE:\ntest_failure" in updated
    assert "Tests failed: pytest" in updated


def test_prompt_inspection_success_requeues_blocked_task_once(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    blocked = tmp_path / "blocked"
    root = tmp_path / "root"
    for path in (inbox, blocked, root):
        path.mkdir()

    blocked_task = blocked / "task-original.md"
    blocked_task.write_text(textwrap.dedent("""\
        ---
        task_id: task-original
        repo: /tmp/repo
        agent: auto
        task_type: implementation
        attempt: 1
        max_attempts: 4
        prompt_snapshot_path: /tmp/prompts/task-original.txt
        ---

        # Goal

        Fix the broken worker prompt path.
    """), encoding="utf-8")

    completed_meta = {
        "task_id": "task-inspect",
        "recovery_trigger": "prompt_inspection",
        "recovery_target_task_id": "task-original",
        "recovery_target_blocker_code": "invalid_result_contract",
    }
    completed_body = textwrap.dedent("""\
        # Goal

        Inspect the worker prompt snapshot.
    """)
    result = {"status": "complete"}

    monkeypatch.setattr("orchestrator.queue.now_ts", lambda: "20260320-140000")

    rerun = maybe_requeue_prompt_inspection_recovery(
        {"INBOX": inbox, "BLOCKED": blocked, "ROOT": root},
        completed_meta,
        completed_body,
        result,
        tmp_path / "queue.log",
        tmp_path / "summary.log",
    )

    assert rerun == inbox / "task-20260320-140000-rerun-task-original.md"
    requeued_text = rerun.read_text(encoding="utf-8")
    assert "recovery_source_task_id: task-original" in requeued_text
    assert "recovery_trigger_task_id: task-inspect" in requeued_text
    assert "attempt: 2" in requeued_text
    assert "model_attempts: []" in requeued_text
    assert "prompt_snapshot_path: /tmp/prompts/task-20260320-140000-rerun-task-original.txt" in requeued_text

    blocked_text = blocked_task.read_text(encoding="utf-8")
    assert "prompt_inspection_requeued_by: task-inspect" in blocked_text
    assert "prompt_inspection_recovery_task_id: task-20260320-140000-rerun-task-original" in blocked_text

    second = maybe_requeue_prompt_inspection_recovery(
        {"INBOX": inbox, "BLOCKED": blocked, "ROOT": root},
        completed_meta,
        completed_body,
        result,
        tmp_path / "queue.log",
        tmp_path / "summary.log",
    )
    assert second is None
    assert len(list(inbox.glob("*.md"))) == 1


def test_prompt_inspection_recovery_skips_non_prompt_blocker_code(tmp_path):
    inbox = tmp_path / "inbox"
    blocked = tmp_path / "blocked"
    root = tmp_path / "root"
    for path in (inbox, blocked, root):
        path.mkdir()

    (blocked / "task-original.md").write_text(textwrap.dedent("""\
        ---
        task_id: task-original
        repo: /tmp/repo
        ---

        # Goal

        Fix the task.
    """), encoding="utf-8")

    rerun = maybe_requeue_prompt_inspection_recovery(
        {"INBOX": inbox, "BLOCKED": blocked, "ROOT": root},
        {
            "task_id": "task-inspect",
            "recovery_trigger": "prompt_inspection",
            "recovery_target_task_id": "task-original",
            "recovery_target_blocker_code": "environment_failure",
        },
        "# Goal\n\nInspect the worker prompt snapshot.\n",
        {"status": "complete"},
        tmp_path / "queue.log",
        tmp_path / "summary.log",
    )

    assert rerun is None
    assert list(inbox.glob("*.md")) == []


def test_build_escalation_message_contains_required_fields():
    meta = {
        "task_id": "task-123",
        "repo": "/tmp/demo",
        "github_issue_url": "https://github.com/acme/demo/issues/7",
    }
    result = {
        "summary": "Last agent summary.",
        "blockers": ["- Missing API token"],
        "files_changed": ["- orchestrator/queue.py"],
    }

    message = build_escalation_message(meta, result, Path("note.md"))
    assert "https://github.com/acme/demo/issues/7" in message
    assert "Task ID: task-123" in message
    assert "Last agent summary." in message
    assert "Missing API token" in message
    assert "orchestrator/queue.py" in message


def test_telegram_action_expired():
    action = {
        "expires_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
    }
    assert telegram_action_expired(action) is True


def test_format_runner_failure_classifies_usage_limit():
    exc = CommandExecutionError(
        ["/bin/agent_runner.sh", "claude"],
        1,
        "",
        "Claude error: usage limit reached for this billing period (429)",
    )
    summary, blockers, detail = _format_runner_failure(exc)
    assert "usage limit / rate limit" in summary
    assert "usage limit reached" in detail
    assert any("stderr tail" in item for item in blockers)


def test_validate_workflow_files_rejects_runner_context_in_job_env(tmp_path):
    workflow_dir = tmp_path / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    workflow = workflow_dir / "ci.yml"
    workflow.write_text(textwrap.dedent("""\
        name: CI
        on:
          push:
            branches: ["**"]
        jobs:
          test:
            runs-on: ubuntu-latest
            env:
              CI_ARTIFACT_DIR: ${{ runner.temp }}/ci-artifacts
            steps:
              - run: echo ok
    """), encoding="utf-8")

    import subprocess
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)

    with pytest.raises(WorkflowValidationError) as exc:
        _validate_workflow_files(tmp_path)

    assert "runner.*" in str(exc.value)
    assert "ci.yml" in str(exc.value)


def test_validate_workflow_files_allows_runner_temp_via_step_env(tmp_path):
    workflow_dir = tmp_path / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    workflow = workflow_dir / "ci.yml"
    workflow.write_text(textwrap.dedent("""\
        name: CI
        on:
          push:
            branches: ["**"]
        jobs:
          test:
            runs-on: ubuntu-latest
            steps:
              - name: Prepare artifact dir
                run: |
                  echo "CI_ARTIFACT_DIR=$RUNNER_TEMP/ci-artifacts" >> "$GITHUB_ENV"
                  mkdir -p "$RUNNER_TEMP/ci-artifacts"
    """), encoding="utf-8")

    import subprocess
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)

    _validate_workflow_files(tmp_path)


def test_agent_available_deepseek_requires_provider_config(monkeypatch):
    monkeypatch.setenv("CLINE_BIN", "cline")
    monkeypatch.delenv("DEEPSEEK_OPENROUTER_CONFIG", raising=False)
    monkeypatch.delenv("DEEPSEEK_NANOGPT_CONFIG", raising=False)
    monkeypatch.delenv("DEEPSEEK_CHUTES_CONFIG", raising=False)
    monkeypatch.setattr("orchestrator.queue._command_available", lambda cmd: True)
    monkeypatch.setattr("orchestrator.queue.Path.home", lambda: Path("/tmp/no-home-config"))

    available, reason = agent_available("deepseek")
    assert available is False
    assert "OpenRouter config dir missing" in reason


def test_agent_available_deepseek_requires_openrouter_api_key(monkeypatch, tmp_path):
    openrouter_dir = tmp_path / "openrouter"
    openrouter_dir.mkdir()
    (openrouter_dir / "secrets.json").write_text("{}", encoding="utf-8")

    monkeypatch.setenv("CLINE_BIN", "cline")
    monkeypatch.setenv("DEEPSEEK_OPENROUTER_CONFIG", str(openrouter_dir))
    monkeypatch.delenv("DEEPSEEK_NANOGPT_CONFIG", raising=False)
    monkeypatch.delenv("DEEPSEEK_CHUTES_CONFIG", raising=False)
    monkeypatch.setattr("orchestrator.queue._command_available", lambda cmd: True)

    available, reason = agent_available("deepseek")
    assert available is False
    assert "has no openRouterApiKey" in reason


def test_agent_available_deepseek_rejects_placeholder_openrouter_api_key(monkeypatch, tmp_path):
    openrouter_dir = tmp_path / "openrouter"
    openrouter_dir.mkdir()
    (openrouter_dir / "secrets.json").write_text(
        '{"openRouterApiKey": "YOUR_OPENROUTER_API_KEY"}',
        encoding="utf-8",
    )

    monkeypatch.setenv("CLINE_BIN", "cline")
    monkeypatch.setenv("DEEPSEEK_OPENROUTER_CONFIG", str(openrouter_dir))
    monkeypatch.delenv("DEEPSEEK_NANOGPT_CONFIG", raising=False)
    monkeypatch.delenv("DEEPSEEK_CHUTES_CONFIG", raising=False)
    monkeypatch.setattr("orchestrator.queue._command_available", lambda cmd: True)

    available, reason = agent_available("deepseek")
    assert available is False
    assert "placeholder text" in reason


def test_rescue_git_progress_marks_result_complete(tmp_path, monkeypatch):
    result = {
        "status": "blocked",
        "summary": "Need someone else to commit and push.",
        "done": ["- Updated files."],
        "decisions": ["- Could not push from agent environment."],
    }
    monkeypatch.setattr("orchestrator.queue.has_changes", lambda worktree: True)
    monkeypatch.setattr("orchestrator.queue.has_unpushed_commits", lambda worktree, branch: False)
    monkeypatch.setattr("orchestrator.queue.commit_and_push", lambda *args, **kwargs: True)

    rescued, pushed = rescue_git_progress({}, result, tmp_path, "agent/task-1", "task-1", True, tmp_path / "x.log", tmp_path / "y.log")
    assert rescued is not None
    assert pushed is True
    assert rescued["status"] == "complete"
    assert "rescued and pushed" in rescued["summary"]


def test_rescue_git_progress_withholds_push_when_validation_fails(tmp_path, monkeypatch):
    result = {
        "status": "blocked",
        "summary": "Need someone else to commit and push.",
        "done": ["- Updated files."],
        "decisions": ["- Could not push from agent environment."],
    }
    monkeypatch.setattr("orchestrator.queue.has_changes", lambda worktree: True)
    monkeypatch.setattr("orchestrator.queue.has_unpushed_commits", lambda worktree, branch: False)
    run_tests_calls = []
    monkeypatch.setattr(
        "orchestrator.queue.run_tests",
        lambda cfg, repo, worktree, logfile, queue_summary_log: run_tests_calls.append((cfg, worktree)),
    )
    monkeypatch.setattr(
        "orchestrator.queue.parse_agent_result",
        lambda worktree: {
            "status": "partial",
            "summary": "Tests failed during rescue validation.",
            "next_step": "Fix the failing tests.",
            "decisions": ["- Rescue validation found failing tests."],
        },
    )
    commit_calls = []
    monkeypatch.setattr("orchestrator.queue.commit_and_push", lambda *args, **kwargs: commit_calls.append(args) or True)

    rescued, pushed = rescue_git_progress(
        {"test_command": "pytest"},
        result,
        tmp_path,
        "agent/task-1",
        "task-1",
        True,
        tmp_path / "x.log",
        tmp_path / "y.log",
    )
    assert rescued is not None
    assert pushed is False
    assert rescued["status"] == "partial"
    assert "not pushed" in rescued["summary"]
    assert run_tests_calls
    assert commit_calls == []


def test_handle_telegram_callback_requeue(monkeypatch):
    with tempfile.TemporaryDirectory() as d:
        actions_dir = Path(d)
        action = {
            "action_id": "abcdef123456",
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "chat_id": "1",
            "message_id": 10,
            "task_id": "task-123",
            "github_project_key": "demo",
            "github_repo": "acme/demo",
            "github_issue_number": 7,
            "github_issue_url": "https://github.com/acme/demo/issues/7",
            "summary": "summary",
            "blockers": ["- blocker"],
            "files_changed": ["- file.py"],
            "escalation_note": "note.md",
        }
        save_telegram_action(actions_dir, action)

        monkeypatch.setattr(
            "orchestrator.queue.requeue_escalation",
            lambda cfg, saved_action, logfile=None, queue_summary_log=None: "https://github.com/acme/demo/issues/8",
        )

        outcome = handle_telegram_callback({}, actions_dir, "esc:abcdef123456:requeue")
        assert outcome["text"] == "Re-queued: https://github.com/acme/demo/issues/8"
        stored = actions_dir.joinpath("abcdef123456.json").read_text(encoding="utf-8")
        assert '"status": "done"' in stored


def test_handle_telegram_callback_expired():
    with tempfile.TemporaryDirectory() as d:
        actions_dir = Path(d)
        action = {
            "action_id": "abcdef123456",
            "status": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
            "chat_id": "1",
            "message_id": 10,
            "task_id": "task-123",
            "github_project_key": "demo",
            "github_repo": "acme/demo",
            "github_issue_number": 7,
            "github_issue_url": "https://github.com/acme/demo/issues/7",
            "summary": "summary",
            "blockers": ["- blocker"],
            "files_changed": ["- file.py"],
            "escalation_note": "note.md",
        }
        save_telegram_action(actions_dir, action)

        outcome = handle_telegram_callback({}, actions_dir, "esc:abcdef123456:close")
        assert outcome["show_alert"] is True
        assert "expired" in outcome["text"].lower()


def test_handle_telegram_callback_plan_approve():
    with tempfile.TemporaryDirectory() as d:
        actions_dir = Path(d)
        action = {
            "action_id": "abcdef123456",
            "type": "plan_approval",
            "status": "pending",
            "approval": "pending",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            "chat_id": "1",
            "message_id": 10,
            "repo": "owner/repo",
        }
        save_telegram_action(actions_dir, action)

        outcome = handle_telegram_callback({"root_dir": d}, actions_dir, "plan:abcdef123456:approve")
        assert "Approved sprint plan" in outcome["text"]
        stored = actions_dir.joinpath("abcdef123456.json").read_text(encoding="utf-8")
        assert '"approval": "approved"' in stored
        audit_lines = (Path(d) / "runtime" / "audit" / "audit.jsonl").read_text(encoding="utf-8").splitlines()
        assert any('"event_type":"telegram_callback"' in line for line in audit_lines)


def test_handle_telegram_callback_revert_approve(tmp_path, monkeypatch):
    actions_dir = tmp_path / "telegram_actions"
    actions_dir.mkdir()
    action = {
        "action_id": "abcdef123456",
        "type": "deploy_watchdog_revert",
        "status": "pending",
        "approval": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "chat_id": "1",
        "message_id": 10,
        "repo": "owner/repo",
        "source_pr_number": 77,
        "revert_pr_number": 88,
    }
    save_telegram_action(actions_dir, action)
    monkeypatch.setattr(
        "orchestrator.deploy_watchdog.handle_revert_callback",
        lambda cfg, saved_action, operation, logfile=None, queue_summary_log=None: f"{operation}:{saved_action['revert_pr_number']}",
    )

    outcome = handle_telegram_callback({}, actions_dir, "rvt:abcdef123456:approve")

    assert outcome["text"] == "approve:88"
    stored = actions_dir.joinpath("abcdef123456.json").read_text(encoding="utf-8")
    assert '"approval": "approved"' in stored


def test_handle_telegram_command_repo_mode_writes_audit_record(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        textwrap.dedent(
            """\
            github_projects:
              proj:
                automation_mode: full
                repos:
                  - key: demo
                    github_repo: owner/repo
            """
        ),
        encoding="utf-8",
    )
    cfg = {
        "root_dir": str(tmp_path),
        "github_projects": {
            "proj": {
                "automation_mode": "full",
                "repos": [{"key": "demo", "github_repo": "owner/repo"}],
            }
        },
    }

    reply = handle_telegram_command(cfg, {"ROOT": tmp_path, "CONFIG": cfg_path}, "/repo mode demo dispatcher")

    assert "automation_mode=dispatcher_only" in reply
    audit_lines = (tmp_path / "runtime" / "audit" / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert any('"event_type":"mode_change"' in line for line in audit_lines)


def test_handle_telegram_callback_blocked_task_retry(tmp_path, monkeypatch):
    actions_dir = tmp_path / "telegram_actions"
    actions_dir.mkdir()
    escalated = tmp_path / "mailbox" / "escalated"
    escalated.mkdir(parents=True)
    note_path = escalated / "task-root-escalation.md"
    note_path.write_text("# Escalation Note\n", encoding="utf-8")

    action = {
        "action_id": "abcdef123456",
        "type": "blocked_task_escalation",
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "chat_id": "1",
        "message_id": 10,
        "task_id": "task-123",
        "github_project_key": "demo",
        "github_repo": "acme/demo",
        "github_issue_number": 7,
        "github_issue_url": "https://github.com/acme/demo/issues/7",
        "escalation_note": "task-root-escalation.md",
    }
    save_telegram_action(actions_dir, action)

    monkeypatch.setattr("orchestrator.queue.runtime_paths", lambda cfg: {"ESCALATED": escalated})
    comments = []
    monkeypatch.setattr("orchestrator.queue.add_issue_comment", lambda repo, number, body: comments.append((repo, number, body)))

    outcome = handle_telegram_callback({}, actions_dir, "esc:abcdef123456:retry")
    assert "Recorded retry" in outcome["text"]
    note_text = note_path.read_text(encoding="utf-8")
    assert "## Retry Decision" in note_text
    assert "action: retry" in note_text
    assert comments and "`retry`" in comments[0][2]


def test_handle_telegram_callback_blocked_task_skip(tmp_path, monkeypatch):
    actions_dir = tmp_path / "telegram_actions"
    actions_dir.mkdir()
    escalated = tmp_path / "mailbox" / "escalated"
    escalated.mkdir(parents=True)
    note_path = escalated / "task-root-escalation.md"
    note_path.write_text("# Escalation Note\n", encoding="utf-8")

    action = {
        "action_id": "abcdef123456",
        "type": "blocked_task_escalation",
        "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "chat_id": "1",
        "message_id": 10,
        "task_id": "task-123",
        "github_project_key": "demo",
        "github_repo": "acme/demo",
        "github_issue_number": 7,
        "github_issue_url": "https://github.com/acme/demo/issues/7",
        "escalation_note": "task-root-escalation.md",
    }
    save_telegram_action(actions_dir, action)

    monkeypatch.setattr("orchestrator.queue.runtime_paths", lambda cfg: {"ESCALATED": escalated})
    monkeypatch.setattr("orchestrator.queue.add_issue_comment", lambda *args, **kwargs: None)

    outcome = handle_telegram_callback({}, actions_dir, "esc:abcdef123456:skip")
    assert "skipped" in outcome["text"].lower()
    note_text = note_path.read_text(encoding="utf-8")
    assert "action: skip" in note_text


def test_create_followup_task_inherits_resolved_agent(tmp_path):
    """Follow-up tasks inherit the resolved_agent from the parent task meta."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    meta = {
        "task_id": "task-orig",
        "repo": "/tmp/repo",
        "base_branch": "main",
        "branch": "agent/task-orig",
        "allow_push": True,
        "task_type": "implementation",
        "attempt": 1,
        "max_attempts": 3,
        "max_runtime_minutes": 40,
        "resolved_agent": "claude",
    }
    result = {
        "status": "partial",
        "next_step": "Fix the remaining test failure",
        "summary": "Partial progress",
    }
    logfile = tmp_path / "log.txt"
    logfile.touch()
    summary_log = tmp_path / "summary.log"
    summary_log.touch()

    path = create_followup_task(meta, "original body", result, logfile, 3, ["claude"], inbox, summary_log)
    assert path is not None
    content = path.read_text(encoding="utf-8")
    assert "agent: claude" in content


def test_create_followup_task_defaults_auto_without_resolved_agent(tmp_path):
    """Follow-up tasks default to auto when no resolved_agent is present."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    meta = {
        "task_id": "task-orig",
        "repo": "/tmp/repo",
        "base_branch": "main",
        "branch": "agent/task-orig",
        "allow_push": True,
        "task_type": "implementation",
        "attempt": 1,
        "max_attempts": 3,
        "max_runtime_minutes": 40,
    }
    result = {
        "status": "blocked",
        "next_step": "Investigate auth failure",
        "summary": "Blocked on auth",
    }
    logfile = tmp_path / "log.txt"
    logfile.touch()
    summary_log = tmp_path / "summary.log"
    summary_log.touch()

    path = create_followup_task(meta, "original body", result, logfile, 3, ["deepseek"], inbox, summary_log)
    assert path is not None
    content = path.read_text(encoding="utf-8")
    assert "agent: auto" in content


def test_create_followup_task_rejects_invalid_resolved_agent(tmp_path):
    """Follow-up tasks fall back to auto when resolved_agent is invalid."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    meta = {
        "task_id": "task-orig",
        "repo": "/tmp/repo",
        "base_branch": "main",
        "branch": "agent/task-orig",
        "allow_push": True,
        "task_type": "implementation",
        "attempt": 1,
        "max_attempts": 3,
        "max_runtime_minutes": 40,
        "resolved_agent": "none",
    }
    result = {
        "status": "partial",
        "next_step": "Continue work",
        "summary": "Partial",
    }
    logfile = tmp_path / "log.txt"
    logfile.touch()
    summary_log = tmp_path / "summary.log"
    summary_log.touch()

    path = create_followup_task(meta, "original body", result, logfile, 3, [], inbox, summary_log)
    assert path is not None
    content = path.read_text(encoding="utf-8")
    assert "agent: auto" in content


def test_create_followup_task_propagates_failed_checks(tmp_path):
    """Follow-up tasks propagate failed_checks from parent frontmatter (PR-98 fix)."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    meta = {
        "task_id": "task-orig",
        "repo": "/tmp/repo",
        "base_branch": "main",
        "branch": "agent/task-orig",
        "allow_push": True,
        "task_type": "debugging",
        "attempt": 1,
        "max_attempts": 3,
        "max_runtime_minutes": 40,
        "failed_checks": ["pytest", "lint"],
    }
    result = {
        "status": "partial",
        "next_step": "Rerun CI after fix",
        "summary": "Partial progress on CI fix",
    }
    logfile = tmp_path / "log.txt"
    logfile.touch()
    summary_log = tmp_path / "summary.log"
    summary_log.touch()

    path = create_followup_task(meta, "original body", result, logfile, 3, ["claude"], inbox, summary_log)
    assert path is not None
    content = path.read_text(encoding="utf-8")
    assert "failed_checks:" in content
    assert "pytest" in content
    assert "lint" in content


# --- Enhanced dispatch context tests ---


def test_gather_recent_git_state_returns_fallback_for_non_repo(tmp_path):
    result = gather_recent_git_state(tmp_path, "main")
    assert result == "(recent git state unavailable)"


def test_gather_recent_git_state_returns_string(tmp_path):
    import subprocess as _sp
    repo = tmp_path / "repo"
    repo.mkdir()
    _sp.run(["git", "init"], cwd=str(repo), capture_output=True)
    _sp.run(["git", "checkout", "-b", "main"], cwd=str(repo), capture_output=True)
    (repo / "f.txt").write_text("hello")
    _sp.run(["git", "add", "."], cwd=str(repo), capture_output=True)
    env = {**__import__("os").environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    _sp.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True, env=env)
    # No origin remote, so falls back gracefully
    result = gather_recent_git_state(repo, "main")
    assert isinstance(result, str)


def test_gather_objective_alignment_returns_empty_without_cfg():
    result = gather_objective_alignment(Path("/nonexistent"), cfg=None, github_slug="")
    assert result == ""


def test_gather_objective_alignment_returns_metrics(tmp_path, monkeypatch):
    obj_file = tmp_path / "objective.yaml"
    obj_file.write_text(
        'version: 1\nrepo: "owner/test-repo"\nprimary_outcome: "Adoption"\n'
        'metrics:\n  - id: stars\n    weight: 0.5\n    direction: increase\n',
        encoding="utf-8",
    )
    cfg = {"root_dir": str(tmp_path), "repo_objectives": {"owner/test-repo": str(obj_file)}}
    result = gather_objective_alignment(tmp_path, cfg=cfg, github_slug="owner/test-repo")
    assert "Primary outcome: Adoption" in result
    assert "stars" in result
    assert "weight=0.5" in result


def test_write_prompt_includes_enhanced_context_sections(tmp_path, monkeypatch):
    """write_prompt injects Dispatch Context when git state or objectives are available."""
    root = tmp_path / "root"
    worktree = tmp_path / "repo"
    root.mkdir()
    worktree.mkdir()
    (worktree / "README.md").write_text("## Goal\n\nShip things.\n", encoding="utf-8")
    (worktree / "CODEBASE.md").write_text("# Codebase Memory\n\nCtx.\n", encoding="utf-8")

    monkeypatch.setattr(
        "orchestrator.queue.gather_recent_git_state",
        lambda *a, **kw: "abc1234 feat: add widget\ndef5678 fix: repair thing",
    )
    monkeypatch.setattr(
        "orchestrator.queue.gather_objective_alignment",
        lambda *a, **kw: "Primary outcome: Adoption\nTracked metrics (id / weight / direction):\n  - stars: weight=0.5, direction=increase",
    )

    prompt_file = write_prompt(
        "task-ctx-1",
        {"task_type": "implementation", "base_branch": "main", "github_repo": "owner/repo"},
        "Implement the feature.",
        "claude",
        [],
        root,
        worktree=worktree,
    )

    text = prompt_file.read_text(encoding="utf-8")
    assert "Dispatch Context (structured)" in text
    assert "Recent Git State" in text
    assert "abc1234 feat: add widget" in text
    assert "Objective Alignment" in text
    assert "Primary outcome: Adoption" in text


def test_write_prompt_omits_enhanced_context_without_worktree(tmp_path):
    root = tmp_path / "root"
    root.mkdir()

    prompt_file = write_prompt(
        "task-ctx-2",
        {"task_type": "implementation"},
        "Do something.",
        "claude",
        [],
        root,
        worktree=None,
    )

    text = prompt_file.read_text(encoding="utf-8")
    assert "Dispatch Context (structured)" not in text


# ---------------------------------------------------------------------------
# create_escalation_note includes prompt snapshot path
# ---------------------------------------------------------------------------

def test_create_escalation_note_includes_prompt_snapshot_path(tmp_path):
    escalated = tmp_path / "escalated"
    escalated.mkdir()
    logfile = tmp_path / "task.log"
    logfile.write_text("", encoding="utf-8")
    queue_summary_log = tmp_path / "queue-summary.log"
    queue_summary_log.write_text("", encoding="utf-8")

    meta = {
        "task_id": "task-snap-test",
        "branch": "agent/task-snap-test",
        "repo": "/tmp/repo",
        "task_type": "implementation",
        "prompt_snapshot_path": "/home/kai/agent-os/runtime/prompts/task-snap-test.txt",
    }
    result = {
        "status": "blocked",
        "blocker_code": "missing_context",
        "summary": "Blocked for testing.",
        "done": ["- None"],
        "blockers": ["- Something missing"],
        "next_step": "Retry",
        "files_changed": ["- None"],
        "tests_run": ["- None"],
        "decisions": ["- None"],
        "risks": ["- None"],
        "attempted_approaches": ["- None"],
        "unblock_notes": None,
    }

    esc_path = create_escalation_note(meta, "Test body.", result, logfile, ["claude"], escalated, queue_summary_log)
    content = esc_path.read_text(encoding="utf-8")
    assert "## Prompt Snapshot" in content
    assert "runtime/prompts/task-snap-test.txt" in content


# ---------------------------------------------------------------------------
# has_unpushed_commits — the fresh-branch bug regression
# ---------------------------------------------------------------------------

def _init_origin_clone(tmp_path: Path) -> tuple[Path, Path]:
    """Create a bare origin repo with a single commit on main, and a clone."""
    import subprocess
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", "main", str(origin)],
                   check=True, capture_output=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-b", "main", str(seed)],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.email", "t@t.t"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.name", "t"],
                   check=True, capture_output=True)
    (seed / "README.md").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(seed), "add", "-A"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-m", "init"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(seed), "remote", "add", "origin", str(origin)],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(seed), "push", "origin", "main"],
                   check=True, capture_output=True)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(origin), str(clone)],
                   check=True, capture_output=True)
    return origin, clone


def test_has_unpushed_commits_false_on_fresh_agent_branch(tmp_path):
    """Regression: a task branch freshly checked out from origin/main with no
    agent work must not report unpushed commits. Earlier code wrongly pushed
    such branches and flipped fallback-exhausted tasks to status=complete."""
    import subprocess
    _, clone = _init_origin_clone(tmp_path)
    subprocess.run(["git", "-C", str(clone), "checkout", "-B", "agent/foo", "origin/main"],
                   check=True, capture_output=True)
    assert has_unpushed_commits(clone, "agent/foo") is False


def test_has_unpushed_commits_true_after_real_commit(tmp_path):
    import subprocess
    _, clone = _init_origin_clone(tmp_path)
    subprocess.run(["git", "-C", str(clone), "checkout", "-B", "agent/foo", "origin/main"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(clone), "config", "user.email", "t@t.t"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(clone), "config", "user.name", "t"],
                   check=True, capture_output=True)
    (clone / "NEW.md").write_text("y", encoding="utf-8")
    subprocess.run(["git", "-C", str(clone), "add", "-A"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(clone), "commit", "-m", "work"],
                   check=True, capture_output=True)
    assert has_unpushed_commits(clone, "agent/foo") is True


# ---------------------------------------------------------------------------
# should_attempt_git_rescue — skip fallback_exhausted
# ---------------------------------------------------------------------------

def test_should_attempt_git_rescue_skips_fallback_exhausted(tmp_path):
    """fallback_exhausted means no agent ran; rescue would only push an empty
    branch and wrongly flip the task to complete."""
    import subprocess
    _, clone = _init_origin_clone(tmp_path)
    subprocess.run(["git", "-C", str(clone), "checkout", "-B", "agent/foo", "origin/main"],
                   check=True, capture_output=True)
    result = {"status": "blocked", "blocker_code": "fallback_exhausted"}
    assert should_attempt_git_rescue(result, clone, "agent/foo") is False


def test_should_attempt_git_rescue_allows_other_blockers_with_work(tmp_path):
    import subprocess
    _, clone = _init_origin_clone(tmp_path)
    subprocess.run(["git", "-C", str(clone), "checkout", "-B", "agent/foo", "origin/main"],
                   check=True, capture_output=True)
    (clone / "dirty.md").write_text("z", encoding="utf-8")  # uncommitted change
    result = {"status": "blocked", "blocker_code": "timeout"}
    assert should_attempt_git_rescue(result, clone, "agent/foo") is True


# ---------------------------------------------------------------------------
# fallback cooldown
# ---------------------------------------------------------------------------

def test_fallback_cooldown_remaining_zero_when_absent(tmp_path):
    cfg = {"root_dir": str(tmp_path)}
    assert fallback_cooldown_remaining(cfg) == 0


def test_fallback_cooldown_arm_and_read(tmp_path):
    cfg = {"root_dir": str(tmp_path)}
    until = start_fallback_cooldown(cfg, minutes=5)
    remaining = fallback_cooldown_remaining(cfg)
    assert 4 * 60 < remaining <= 5 * 60
    assert until > datetime.now(timezone.utc)


def test_fallback_cooldown_expires(tmp_path):
    cfg = {"root_dir": str(tmp_path)}
    # Write an already-past timestamp directly.
    state_file = Path(tmp_path) / "runtime" / "state" / "fallback_cooldown_until.txt"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text((datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat(),
                          encoding="utf-8")
    assert fallback_cooldown_remaining(cfg) == 0


# ---------------------------------------------------------------------------
# downgrade_no_diff_complete
# ---------------------------------------------------------------------------

def _complete_result():
    return {
        "status": "complete",
        "summary": "Implemented the feature.",
        "done": ["- Wrote code."],
        "blockers": [],
        "next_step": "None",
        "files_changed": ["- foo.py"],
        "tests_run": ["- pytest"],
        "decisions": ["- Chose X over Y"],
        "risks": ["- None"],
        "attempted_approaches": ["- Direct"],
        "raw": "STATUS: complete\n",
    }


def test_downgrade_no_diff_complete_implementation_downgrades():
    meta = {"task_type": "implementation"}
    result = _complete_result()
    out = downgrade_no_diff_complete(meta, result, "gemini")
    assert out is not result
    assert out["status"] == "partial"
    assert out["blocker_code"] == "no_diff_produced"
    assert "gemini" in out["summary"]
    assert any("STATUS: complete" in b for b in out["blockers"])
    assert out["unblock_notes"]["blocking_cause"] == out["summary"]


def test_downgrade_no_diff_complete_research_passes_through():
    meta = {"task_type": "research"}
    result = _complete_result()
    out = downgrade_no_diff_complete(meta, result, "claude")
    assert out is result
    assert out["status"] == "complete"


def test_downgrade_no_diff_complete_already_partial_unchanged():
    meta = {"task_type": "debugging"}
    result = _complete_result()
    result["status"] = "partial"
    result["blocker_code"] = "test_failure"
    out = downgrade_no_diff_complete(meta, result, "codex")
    assert out is result


def test_downgrade_no_diff_complete_unknown_task_type_passes_through():
    meta = {"task_type": "exploration"}
    result = _complete_result()
    out = downgrade_no_diff_complete(meta, result, "claude")
    assert out is result


# ---------------------------------------------------------------------------
# Prompt size ceiling (E2BIG guard)
# ---------------------------------------------------------------------------

def test_write_prompt_raises_when_body_exceeds_argv_limit(tmp_path):
    root = tmp_path / "root"
    worktree = tmp_path / "repo"
    root.mkdir()
    worktree.mkdir()
    giant_body = "x" * (PROMPT_SIZE_LIMIT_BYTES + 10_000)
    with pytest.raises(PromptTooLargeError) as exc_info:
        write_prompt(
            "task-e2big",
            {"task_type": "implementation"},
            giant_body,
            "codex",
            [],
            root,
            worktree=worktree,
        )
    assert exc_info.value.size_bytes > PROMPT_SIZE_LIMIT_BYTES
    assert exc_info.value.limit_bytes == PROMPT_SIZE_LIMIT_BYTES


def test_should_try_fallback_short_circuits_on_permanent_infra_blocker():
    assert "prompt_too_large" in PERMANENT_INFRA_BLOCKERS
    blocked = {"status": "blocked", "blocker_code": "prompt_too_large"}
    assert should_try_fallback(blocked) is False
    recoverable = {"status": "blocked", "blocker_code": "timeout"}
    assert should_try_fallback(recoverable) is True


# ---------------------------------------------------------------------------
# Web-task detection + rubric injection + no_web_artifact downgrade
# ---------------------------------------------------------------------------

from orchestrator.queue import (
    _web_task_kind,
    _web_task_rubric_for,
    downgrade_web_no_artifact,
)


def test_web_task_kind_detects_homepage_in_body():
    meta = {"task_type": "implementation"}
    assert _web_task_kind(meta, "Build homepage with hero and testimonials") == "homepage"


def test_web_task_kind_detects_landing_page():
    meta = {"task_type": "content"}
    assert _web_task_kind(meta, "Draft a landing page for the new product") == "homepage"


def test_web_task_kind_detects_general_web_for_website_keyword():
    meta = {"task_type": "design"}
    assert _web_task_kind(meta, "Design a website for the consulting firm") == "web"


def test_web_task_kind_none_for_non_web_task():
    meta = {"task_type": "implementation"}
    assert _web_task_kind(meta, "Refactor the queue module") is None


def test_web_task_kind_none_for_research_task_even_if_web_keyword():
    meta = {"task_type": "research"}
    assert _web_task_kind(meta, "Investigate best practices for homepage design") is None


def test_web_task_rubric_for_homepage_contains_index_html_rule():
    text = _web_task_rubric_for("homepage")
    assert "index.html" in text
    assert "Markdown" in text or "markdown" in text


def test_write_prompt_injects_web_rubric_for_homepage_task(tmp_path):
    root = tmp_path / "root"
    worktree = tmp_path / "repo"
    root.mkdir()
    worktree.mkdir()
    prompt_file = write_prompt(
        "task-web-1",
        {"task_type": "implementation"},
        "Build the homepage with a hero section and call-to-action.",
        "codex",
        [],
        root,
        worktree=worktree,
    )
    text = prompt_file.read_text(encoding="utf-8")
    assert "Web deliverable rubric" in text
    assert "index.html" in text


def test_write_prompt_omits_web_rubric_for_non_web_task(tmp_path):
    root = tmp_path / "root"
    worktree = tmp_path / "repo"
    root.mkdir()
    worktree.mkdir()
    prompt_file = write_prompt(
        "task-web-2",
        {"task_type": "implementation"},
        "Refactor the queue loop for clarity.",
        "codex",
        [],
        root,
        worktree=worktree,
    )
    text = prompt_file.read_text(encoding="utf-8")
    assert "Web deliverable rubric" not in text


def test_downgrade_web_no_artifact_downgrades_when_index_html_missing(tmp_path):
    (tmp_path / "BVT_HOMEPAGE.md").write_text("# draft copy", encoding="utf-8")
    meta = {"task_type": "implementation"}
    body = "Build homepage with hero, value prop, and testimonials."
    result = {"status": "complete", "summary": "Shipped homepage spec."}
    out = downgrade_web_no_artifact(meta, body, result, "codex", tmp_path)
    assert out["status"] == "partial"
    assert out["blocker_code"] == "no_web_artifact"
    assert "index.html" in out["next_step"]


def test_downgrade_web_no_artifact_passes_through_when_index_html_present(tmp_path):
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
    meta = {"task_type": "implementation"}
    body = "Build homepage."
    result = {"status": "complete"}
    out = downgrade_web_no_artifact(meta, body, result, "codex", tmp_path)
    assert out is result


def test_downgrade_web_no_artifact_ignores_non_homepage_tasks(tmp_path):
    meta = {"task_type": "implementation"}
    body = "Refactor the queue module."
    result = {"status": "complete"}
    out = downgrade_web_no_artifact(meta, body, result, "codex", tmp_path)
    assert out is result


def test_downgrade_web_no_artifact_ignores_general_web_tasks_without_homepage_keyword(tmp_path):
    meta = {"task_type": "implementation"}
    body = "Add a Services page to the website."
    result = {"status": "complete"}
    out = downgrade_web_no_artifact(meta, body, result, "codex", tmp_path)
    assert out is result


def test_downgrade_web_no_artifact_ignores_non_complete_status(tmp_path):
    meta = {"task_type": "implementation"}
    body = "Build homepage."
    result = {"status": "partial", "blocker_code": "missing_context"}
    out = downgrade_web_no_artifact(meta, body, result, "codex", tmp_path)
    assert out is result
