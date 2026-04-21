from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from orchestrator.system_architect import (
    build_system_architect_findings,
    evaluate_system_architect,
    load_system_architect_report,
)


def _write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _cfg(tmp_path: Path) -> dict:
    repo = tmp_path / "repo"
    repo.mkdir()
    return {
        "root_dir": str(tmp_path),
        "github_repo": "owner/repo",
        "github_projects": {
            "proj": {
                "repos": [
                    {
                        "github_repo": "owner/repo",
                        "local_repo": str(repo),
                    }
                ]
            }
        },
        "system_architect": {
            "enabled": True,
            "cadence_days": 30,
            "target_model": "target_operating_model.yaml",
        },
    }


def test_system_architect_emits_expected_gap_findings(tmp_path):
    cfg = _cfg(tmp_path)
    _write_file(tmp_path / "orchestrator" / "strategic_planner.py", "def run():\n    return None\n")
    _write_file(tmp_path / "orchestrator" / "backlog_groomer.py", "def run():\n    return None\n")
    _write_file(tmp_path / "orchestrator" / "agent_scorer.py", 'METRICS = "agent_stats.jsonl"\n')
    _write_file(
        tmp_path / "target_operating_model.yaml",
        """
capabilities:
  roles: [strategic_planner, system_architect]
  jobs: [strategic_planner]
  agents: [codex]
sensors:
  signals: [agent_stats, customer_support_tickets]
  schemas: [agent_stats.jsonl, support_tickets.jsonl]
accepted_omissions: []
""".strip(),
    )

    report = evaluate_system_architect(cfg)

    names = {(finding["kind"], finding["detail_type"], finding["name"]) for finding in report["findings"]}
    assert ("capability_gap", "role", "system_architect") in names
    assert ("sensor_gap", "signal", "customer_support_tickets") in names
    assert ("sensor_gap", "schema", "support_tickets.jsonl") in names


def test_system_architect_suppresses_accepted_omissions(tmp_path):
    cfg = _cfg(tmp_path)
    _write_file(tmp_path / "orchestrator" / "strategic_planner.py", "def run():\n    return None\n")
    _write_file(
        tmp_path / "target_operating_model.yaml",
        """
capabilities:
  roles: [strategic_planner, growth_marketer]
accepted_omissions:
  - kind: capability_gap
    detail_type: role
    name: growth_marketer
""".strip(),
    )

    findings = build_system_architect_findings(cfg)
    report = load_system_architect_report(cfg)

    assert findings == []
    assert report is not None
    assert report["accepted_omissions"] == [
        {"detail_type": "role", "kind": "capability_gap", "name": "growth_marketer"}
    ]


def test_system_architect_requires_target_model(tmp_path):
    cfg = _cfg(tmp_path)
    _write_file(tmp_path / "orchestrator" / "strategic_planner.py", "def run():\n    return None\n")

    with pytest.raises(FileNotFoundError):
        evaluate_system_architect(cfg)
