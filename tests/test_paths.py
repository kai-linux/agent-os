from __future__ import annotations

import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.paths import load_config, resolve_config_path


def test_resolve_config_path_prefers_external_copy(monkeypatch, tmp_path):
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    external_dir = tmp_path / ".config" / "agent-os"
    external_dir.mkdir(parents=True)
    external_cfg = external_dir / "config.yaml"
    external_cfg.write_text("github_owner: external\n", encoding="utf-8")

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("AGENT_OS_CONFIG", raising=False)
    monkeypatch.delenv("AGENT_OS_CONFIG_DIR", raising=False)
    monkeypatch.setattr("orchestrator.paths.ROOT", repo_root)
    monkeypatch.setattr("orchestrator.paths.DEFAULT_CONFIG_DIR", external_dir)

    assert resolve_config_path() == external_cfg
    cfg = load_config()
    assert cfg["github_owner"] == "external"
    assert cfg["_config_path"] == str(external_cfg)


def test_load_config_sets_external_objective_defaults(monkeypatch, tmp_path):
    config_dir = tmp_path / ".config" / "agent-os"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "config.yaml"
    config_path.write_text(yaml.safe_dump({"root_dir": str(tmp_path / "runtime-root")}), encoding="utf-8")

    monkeypatch.setenv("AGENT_OS_CONFIG", str(config_path))
    cfg = load_config()

    assert cfg["config_dir"] == str(config_dir)
    assert cfg["objectives_dir"] == str(config_dir / "objectives")
