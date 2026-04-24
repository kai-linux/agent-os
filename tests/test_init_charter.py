from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.init.charter import (
    CharterError,
    _commit_charter,
    build_revision_prompt,
    call_architect,
    parse_charter_response,
    strip_code_fences,
    validate_charter_payload,
)


def _payload():
    return {
        "stack_decision": "Quart + SQLite",
        "stack_rationale": "Simple stack.",
        "north_star_md": "# Demo\n",
        "seed_issues": [
            {
                "title": "Scaffold app",
                "priority": "prio:high",
                "goal": "Create app skeleton.",
                "success_criteria": ["App runs", "Health endpoint returns 200"],
                "constraints": ["Keep it minimal"],
            },
            {
                "title": "Add persistence",
                "priority": "prio:normal",
                "goal": "Add storage.",
                "success_criteria": ["Schema exists"],
                "constraints": ["Use sqlite"],
            },
            {
                "title": "Build first page",
                "priority": "prio:normal",
                "goal": "Add UI.",
                "success_criteria": ["Page loads"],
                "constraints": ["No auth yet"],
            },
        ],
    }


def test_strip_code_fences_removes_json_wrapper():
    raw = "```json\n{\"a\":1}\n```"
    assert strip_code_fences(raw) == '{"a":1}'


def test_parse_charter_response_accepts_fenced_json():
    raw = "```json\n" + json.dumps(_payload()) + "\n```"
    parsed = parse_charter_response(raw)
    assert parsed["stack_decision"] == "Quart + SQLite"


def test_validate_charter_payload_rejects_invalid_priority():
    payload = _payload()
    payload["seed_issues"][0]["priority"] = "urgent"
    with pytest.raises(CharterError):
        validate_charter_payload(payload)


def test_call_architect_falls_back_to_codex_when_claude_fails(monkeypatch):
    def fake_run(cmd, capture_output=True, text=True, timeout=300, check=False):
        if os.path.basename(cmd[0]) == "claude":
            return subprocess.CompletedProcess(cmd, 1, "", "You've hit your limit")
        if os.path.basename(cmd[0]) == "codex":
            return subprocess.CompletedProcess(cmd, 0, json.dumps(_payload()), "")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr("orchestrator.init.charter.subprocess.run", fake_run)
    raw = call_architect("Return JSON")
    assert json.loads(raw)["stack_decision"] == "Quart + SQLite"


def test_call_architect_raises_combined_error_when_both_agents_fail(monkeypatch):
    def fake_run(cmd, capture_output=True, text=True, timeout=300, check=False):
        if os.path.basename(cmd[0]) == "claude":
            return subprocess.CompletedProcess(cmd, 1, "", "quota")
        if os.path.basename(cmd[0]) == "codex":
            return subprocess.CompletedProcess(cmd, 1, "", "auth failed")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr("orchestrator.init.charter.subprocess.run", fake_run)
    with pytest.raises(CharterError) as exc:
        call_architect("Return JSON")
    assert "claude call failed" in str(exc.value)
    assert "codex call failed" in str(exc.value)


def test_build_revision_prompt_includes_plain_language_feedback():
    prompt = build_revision_prompt(
        {"idea": "a game", "kind": "other", "stack_preference": "auto", "success_criteria": "for a toddler"},
        _payload(),
        "I don't want Netlify. Keep it browser-based but use simpler deployment assumptions.",
    )
    assert "Operator feedback" in prompt
    assert "I don't want Netlify" in prompt
    assert '"stack_decision": "Quart + SQLite"' in prompt


def test_validate_charter_payload_rejects_empty_optional_docs():
    payload = _payload()
    payload["vision_md"] = ""
    with pytest.raises(CharterError, match="vision_md present but empty"):
        validate_charter_payload(payload)


def test_validate_charter_payload_accepts_legacy_without_optional_docs():
    # Older architect output that only has north_star_md must still validate —
    # a resumed init from a previous version should not fail.
    payload = _payload()
    assert "vision_md" not in payload
    validate_charter_payload(payload)  # no raise


def test_commit_charter_writes_all_present_markdown_docs(tmp_path, monkeypatch):
    calls = []

    def fake_run(cmd, cwd=None, check=False, capture_output=False, text=False):
        calls.append(list(cmd))
        if "rev-parse" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="abc123\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("orchestrator.init.charter.subprocess.run", fake_run)
    payload = _payload()
    payload["vision_md"] = "# Vision\nEnd state."
    payload["strategy_md"] = "# Strategy\n1. Phase one\n2. Phase two"
    payload["planning_principles_md"] = "- Prefer boring tech\n- Test before merge"

    sha = _commit_charter(tmp_path, payload, dry_run=False)

    assert sha == "abc123"
    assert (tmp_path / "NORTH_STAR.md").read_text().startswith("# Demo")
    assert (tmp_path / "VISION.md").read_text().startswith("# Vision")
    assert (tmp_path / "STRATEGY.md").read_text().startswith("# Strategy")
    assert (tmp_path / "PLANNING_PRINCIPLES.md").read_text().startswith("- Prefer")
    # `git add` should reference all four files.
    add_cmd = next(c for c in calls if c[:2] == ["git", "add"])
    for name in ("NORTH_STAR.md", "VISION.md", "STRATEGY.md", "PLANNING_PRINCIPLES.md"):
        assert name in add_cmd


def test_commit_charter_skips_missing_optional_docs(tmp_path, monkeypatch):
    """Legacy payloads with only north_star_md must commit cleanly."""
    monkeypatch.setattr("orchestrator.init.charter.subprocess.run",
                       lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="abc\n", stderr=""))
    payload = _payload()  # no vision/strategy/principles
    _commit_charter(tmp_path, payload, dry_run=False)
    assert (tmp_path / "NORTH_STAR.md").exists()
    assert not (tmp_path / "VISION.md").exists()
    assert not (tmp_path / "STRATEGY.md").exists()
    assert not (tmp_path / "PLANNING_PRINCIPLES.md").exists()
