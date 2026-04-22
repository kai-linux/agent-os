from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.init.charter import CharterError, call_architect, parse_charter_response, strip_code_fences, validate_charter_payload


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
