from __future__ import annotations

import sys
from pathlib import Path

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
