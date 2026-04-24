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
    # Existing Telegram credentials must survive — the operator has already
    # paired a bot and adding a new repo shouldn't clobber that pairing.
    assert merged["telegram_bot_token"] == "existing-token"
    assert merged["telegram_chat_id"] == "123"


def test_merge_config_writes_telegram_when_not_previously_set(tmp_path):
    existing = {"allowed_repos": ["/old/repo"]}
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
    assert merged["telegram_bot_token"] == "new-token"
    assert merged["telegram_chat_id"] == "999"


def test_merge_config_preserves_existing_github_project_entry(tmp_path):
    existing = {
        "github_projects": {
            "demo": {
                "project_number": 99,  # operator previously customized
                "repos": [{"key": "demo", "github_repo": "kai-linux/demo", "local_repo": "/custom/path"}],
            }
        },
        "github_repos": {"demo": "kai-linux/demo"},
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
        {"token": "t", "chat_id": "1"},
    )
    # Re-running init for an existing repo must not clobber the operator's
    # customized project entry.
    assert merged["github_projects"]["demo"]["project_number"] == 99
    assert merged["github_projects"]["demo"]["repos"][0]["local_repo"] == "/custom/path"
    assert merged["github_repos"]["demo"] == "kai-linux/demo"


def test_apply_tuning_overlays_operator_choices():
    from orchestrator.init.config_emit import apply_tuning
    payload = {
        "sprint_cadence_days": 7,
        "groomer_cadence_days": 3.5,
        "max_parallel_workers": 1,
        "dependency_watcher": {"enabled": True, "cadence_days": 7},
    }
    tuning = {"sprint_cadence_days": 14, "max_parallel_workers": 2, "dependency_watcher_cadence_days": 30}
    apply_tuning(payload, tuning)
    assert payload["sprint_cadence_days"] == 14
    assert payload["max_parallel_workers"] == 2
    # Nested dict merge, not replace — `enabled` must survive.
    assert payload["dependency_watcher"]["cadence_days"] == 30
    assert payload["dependency_watcher"]["enabled"] is True
    # Untouched keys unchanged.
    assert payload["groomer_cadence_days"] == 3.5


def test_apply_tuning_noop_when_empty():
    from orchestrator.init.config_emit import apply_tuning
    payload = {"sprint_cadence_days": 7}
    before = dict(payload)
    apply_tuning(payload, None)
    apply_tuning(payload, {})
    assert payload == before
