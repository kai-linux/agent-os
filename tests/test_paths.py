from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.paths import load_config, resolve_config_path


def test_resolve_config_path_defaults_to_repo_config(monkeypatch, tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    repo_cfg = repo_root / "config.yaml"
    repo_cfg.write_text("github_owner: repo\n", encoding="utf-8")

    monkeypatch.delenv("AGENT_OS_CONFIG", raising=False)
    monkeypatch.setattr("orchestrator.paths.ROOT", repo_root)

    assert resolve_config_path() == repo_cfg
    cfg = load_config()
    assert cfg["github_owner"] == "repo"
    assert cfg["_config_path"] == str(repo_cfg)


def test_load_config_sets_repo_objective_defaults(monkeypatch, tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    config_path = repo_root / "config.yaml"
    config_path.write_text(yaml.safe_dump({"root_dir": str(tmp_path / "runtime-root")}), encoding="utf-8")

    monkeypatch.setenv("AGENT_OS_CONFIG", str(config_path))
    cfg = load_config()

    assert cfg["config_dir"] == str(repo_root)
    assert cfg["objectives_dir"] == str(repo_root / "objectives")


def test_load_config_rejects_non_local_dashboard_bind_without_auth(monkeypatch, tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    config_path = repo_root / "config.yaml"
    config_path.write_text('dashboard_bind_address: "0.0.0.0"\n', encoding="utf-8")

    monkeypatch.setenv("AGENT_OS_CONFIG", str(config_path))

    with pytest.raises(ValueError, match="dashboard_bind_address must remain 127.0.0.1"):
        load_config()


def test_load_config_can_fallback_to_local_readonly_dashboard(monkeypatch, tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    config_path = repo_root / "config.yaml"
    config_path.write_text(
        'dashboard_bind_address: "0.0.0.0"\n'
        "dashboard_readonly_fallback: true\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("AGENT_OS_CONFIG", str(config_path))

    cfg = load_config()

    assert cfg["dashboard_bind_address"] == "127.0.0.1"
    assert cfg["dashboard_readonly_mode"] is True


def test_load_config_accepts_verified_mcp_checksum(monkeypatch, tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "verified_packages.yaml").write_text(
        yaml.safe_dump(
            {
                "packages": [
                    {
                        "ecosystem": "npm",
                        "package": "@acme/mcp-linear",
                        "version": "1.2.3",
                        "sha256": "a" * 64,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    config_path = repo_root / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "tool_registry": {
                    "verified_packages_file": "verified_packages.yaml",
                    "mcp_servers": {
                        "linear_mcp": {
                            "package": "@acme/mcp-linear",
                            "version": "1.2.3",
                            "sha256": "a" * 64,
                            "env": {"LINEAR_API_KEY": "${LINEAR_API_KEY}"},
                            "task_permissions": {"groomer": ["issues:read"]},
                        }
                    },
                },
                "github_projects": {"proj": {"repos": [{"github_repo": "owner/repo", "enabled_tools": ["linear_mcp"]}]}},
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("AGENT_OS_CONFIG", str(config_path))
    monkeypatch.setenv("LINEAR_API_KEY", "secret")

    cfg = load_config()

    assert cfg["_tool_registry_status"]["status"] == "verified"


def test_load_config_rejects_mutated_mcp_checksum(monkeypatch, tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "verified_packages.yaml").write_text(
        yaml.safe_dump(
            {
                "packages": [
                    {
                        "ecosystem": "npm",
                        "package": "@acme/mcp-linear",
                        "version": "1.2.3",
                        "sha256": "a" * 64,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    config_path = repo_root / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "tool_registry": {
                    "verified_packages_file": "verified_packages.yaml",
                    "mcp_servers": {
                        "linear_mcp": {
                            "package": "@acme/mcp-linear",
                            "version": "1.2.3",
                            "sha256": "b" * 64,
                            "env": {"LINEAR_API_KEY": "${LINEAR_API_KEY}"},
                            "task_permissions": {"groomer": ["issues:read"]},
                        }
                    },
                },
                "github_projects": {"proj": {"repos": [{"github_repo": "owner/repo", "enabled_tools": ["linear_mcp"]}]}},
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("AGENT_OS_CONFIG", str(config_path))
    monkeypatch.setenv("LINEAR_API_KEY", "secret")

    with pytest.raises(ValueError, match="sha256 mismatch"):
        load_config()


def test_load_config_ignores_unenabled_tool_env_requirements(monkeypatch, tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "verified_packages.yaml").write_text(
        yaml.safe_dump(
            {
                "packages": [
                    {
                        "ecosystem": "npm",
                        "package": "@acme/mcp-linear",
                        "version": "1.2.3",
                        "sha256": "a" * 64,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    config_path = repo_root / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "tool_registry": {
                    "verified_packages_file": "verified_packages.yaml",
                    "mcp_servers": {
                        "linear_mcp": {
                            "package": "@acme/mcp-linear",
                            "version": "1.2.3",
                            "sha256": "a" * 64,
                            "env": {"LINEAR_API_KEY": "${LINEAR_API_KEY}"},
                            "task_permissions": {"groomer": ["issues:read"]},
                        }
                    },
                },
                "github_projects": {"proj": {"repos": [{"github_repo": "owner/repo"}]}},
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("AGENT_OS_CONFIG", str(config_path))
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)

    cfg = load_config()

    assert cfg["_tool_registry_status"]["status"] == "configured"
