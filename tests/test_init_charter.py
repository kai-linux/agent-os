from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.init.charter import CharterError, parse_charter_response, strip_code_fences, validate_charter_payload


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
