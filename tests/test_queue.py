"""Unit tests for pure functions in orchestrator/queue.py"""
import sys
import os
import textwrap
import tempfile
from pathlib import Path

# Ensure orchestrator package is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.queue import split_section, parse_bullets, get_agent_chain, parse_agent_result


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
    chain = get_agent_chain({"task_type": "implementation"}, _cfg())
    assert chain == ["codex", "claude", "gemini", "deepseek"]


def test_get_agent_chain_requested_first():
    chain = get_agent_chain({"agent": "claude", "task_type": "implementation"}, _cfg())
    assert chain[0] == "claude"
    assert set(chain) == {"codex", "claude", "gemini", "deepseek"}


def test_get_agent_chain_unknown_type_falls_back_to_default():
    chain = get_agent_chain({"task_type": "unknown"}, _cfg())
    assert "codex" in chain


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
        assert "Implemented the feature" in result["summary"]
        assert result["files_changed"] == ["- src/foo.py"]


def test_parse_agent_result_missing_file():
    with tempfile.TemporaryDirectory() as d:
        result = parse_agent_result(Path(d))
        assert result["status"] == "blocked"
        assert "No .agent_result.md" in result["summary"]


def test_parse_agent_result_invalid_status_normalised():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _write_result(tmp, "STATUS: weirdvalue\n\nSUMMARY:\nOops.\n")
        result = parse_agent_result(tmp)
        assert result["status"] == "blocked"


def test_parse_agent_result_manual_steps():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        _write_result(tmp, textwrap.dedent("""\
            STATUS: complete

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
            - Add cron: 0 7 * * 1 /home/kai/agent-os/bin/run_thing.sh
        """))
        result = parse_agent_result(tmp)
        assert "Add cron" in result["manual_steps"]
