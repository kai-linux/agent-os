"""Unit tests for pure functions in orchestrator/queue.py"""
import json
import sys
import textwrap
import tempfile

import yaml
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# Ensure orchestrator package is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.repo_context import read_evaluation_rubric
from orchestrator.queue import (
    CommandExecutionError,
    _format_runner_failure,
    _parse_unblock_notes,
    _validate_workflow_files,
    agent_available,
    build_escalation_message,
    create_followup_task,
    get_agent_chain,
    handle_telegram_callback,
    maybe_requeue_prompt_inspection_recovery,
    parse_agent_result,
    parse_bullets,
    rescue_git_progress,
    run_tests,
    save_telegram_action,
    split_section,
    telegram_action_expired,
    verify_pr_ci_debug_completion,
    write_unblock_notes_artifact,
    WorkflowValidationError,
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

        outcome = handle_telegram_callback({}, actions_dir, "plan:abcdef123456:approve")
        assert "Approved sprint plan" in outcome["text"]
        stored = actions_dir.joinpath("abcdef123456.json").read_text(encoding="utf-8")
        assert '"approval": "approved"' in stored


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
