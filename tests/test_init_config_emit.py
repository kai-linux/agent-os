from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.init.config_emit import build_config, infer_test_command, merge_config
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


def test_merge_config_preserves_existing_repos_and_adds_new_one(tmp_path):
    existing = {
        "allowed_repos": ["/old/repo"],
        "github_projects": {
            "agent-os": {
                "project_number": 6,
                "repos": [{"key": "agent-os", "github_repo": "kai-linux/agent-os", "local_repo": "/old/repo"}],
            }
        },
        "github_repos": {"agent-os": "kai-linux/agent-os"},
        "trusted_authors": ["kai-linux"],
        "telegram_bot_token": "existing-token",
        "telegram_chat_id": "123",
    }

    merged = merge_config(
        existing,
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
        {"token": "new-token", "chat_id": "999"},
    )

    assert "/old/repo" in merged["allowed_repos"]
    assert str(tmp_path / "demo") in merged["allowed_repos"]
    assert "agent-os" in merged["github_projects"]
    assert "demo" in merged["github_projects"]
    assert merged["telegram_bot_token"] == "new-token"
    assert merged["telegram_chat_id"] == "999"
