"""Unit tests for pure functions in orchestrator/queue.py"""
import sys
import textwrap
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Ensure orchestrator package is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.queue import (
    CommandExecutionError,
    _format_runner_failure,
    agent_available,
    build_escalation_message,
    get_agent_chain,
    handle_telegram_callback,
    parse_agent_result,
    parse_bullets,
    rescue_git_progress,
    run_tests,
    save_telegram_action,
    split_section,
    telegram_action_expired,
    write_prompt,
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
    return {
        "default_agent": "auto",
        "default_task_type": "implementation",
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


def test_get_agent_chain_skips_unavailable_deepseek(monkeypatch):
    cfg = _cfg({"debugging": ["claude", "deepseek", "codex"]})

    def fake_available(agent):
        return (agent != "deepseek", None if agent != "deepseek" else "not configured")

    monkeypatch.setattr("orchestrator.queue.agent_available", fake_available)
    chain = get_agent_chain({"task_type": "debugging"}, cfg)
    assert chain == ["claude", "codex"]


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
        """))
        result = parse_agent_result(tmp)
        assert result["status"] == "partial"
        assert result["blocker_code"] == "missing_context"


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


def test_agent_available_deepseek_requires_provider_config(monkeypatch):
    monkeypatch.setenv("CLINE_BIN", "cline")
    monkeypatch.delenv("DEEPSEEK_OPENROUTER_CONFIG", raising=False)
    monkeypatch.delenv("DEEPSEEK_NANOGPT_CONFIG", raising=False)
    monkeypatch.delenv("DEEPSEEK_CHUTES_CONFIG", raising=False)
    monkeypatch.setattr("orchestrator.queue._command_available", lambda cmd: True)
    monkeypatch.setattr("orchestrator.queue.Path.home", lambda: Path("/tmp/no-home-config"))

    available, reason = agent_available("deepseek")
    assert available is False
    assert "no DeepSeek provider config dir is set" in reason


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

        outcome = handle_telegram_callback({}, actions_dir, "plan:abcdef123456:approve")
        assert "Approved sprint plan" in outcome["text"]
        stored = actions_dir.joinpath("abcdef123456.json").read_text(encoding="utf-8")
        assert '"approval": "approved"' in stored
