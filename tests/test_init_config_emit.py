from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.init.config_emit import build_config, infer_test_command
from orchestrator.paths import load_config


def test_infer_test_command_prefers_python_and_node_defaults():
    assert infer_test_command("web", "Quart + SQLite") == "pytest -q"
    assert infer_test_command("web", "Next.js + Postgres") == "npm test"
    assert infer_test_command("cli", "Go") == "go test ./..."


def test_build_config_loads_through_existing_loader(monkeypatch, tmp_path):
    root = tmp_path / "agent-os"
    root.mkdir()
    monkeypatch.setattr("orchestrator.init.config_emit.ROOT", root)

    config = build_config(
        {"kind": "web"},
        {
            "owner": "kai-linux",
            "repo_name": "demo",
            "repo_full_name": "kai-linux/demo",
            "project_number": 14,
            "local_clone_path": str(tmp_path / "demo"),
            "status_value_names": {"Ready": "Todo", "In Progress": "In Progress", "Blocked": "Todo", "Done": "Done"},
        },
        {"stack_decision": "Quart + SQLite"},
        {"token": "123456:abcdefghijklmnopqrstuvwxyzABCDE_12345", "chat_id": "42"},
    )

    config_path = root / "config.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    monkeypatch.setenv("AGENT_OS_CONFIG", str(config_path))
    loaded = load_config()

    assert loaded["github_owner"] == "kai-linux"
    assert loaded["github_projects"]["demo"]["project_number"] == 14
    assert loaded["github_projects"]["demo"]["ready_value"] == "Todo"
    assert loaded["repo_configs"][str(tmp_path / "demo")]["test_command"] == "pytest -q"
