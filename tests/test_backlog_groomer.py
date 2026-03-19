"""Tests for backlog groomer cadence config."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.backlog_groomer import _repo_groomer_cadence_days
from orchestrator import backlog_groomer as bg


def test_repo_groomer_cadence_defaults_to_sprint_cadence():
    cfg = {"sprint_cadence_days": 2}
    assert _repo_groomer_cadence_days(cfg, "owner/repo") == 2.0


def test_repo_groomer_cadence_uses_top_level_override():
    cfg = {"sprint_cadence_days": 7, "groomer_cadence_days": 0.5}
    assert _repo_groomer_cadence_days(cfg, "owner/repo") == 0.5


def test_repo_groomer_cadence_uses_per_repo_override():
    cfg = {
        "groomer_cadence_days": 7,
        "github_projects": {
            "proj": {
                "repos": [
                    {"github_repo": "owner/repo-a", "groomer_cadence_days": 0},
                    {"github_repo": "owner/repo-b", "sprint_cadence_days": 1.5},
                ]
            }
        },
    }
    assert _repo_groomer_cadence_days(cfg, "owner/repo-a") == 0.0
    assert _repo_groomer_cadence_days(cfg, "owner/repo-b") == 1.5


def test_groom_repo_no_data_status(tmp_path):
    cfg = {"root_dir": str(tmp_path), "worktrees_dir": str(tmp_path / "worktrees")}
    repo = tmp_path / "repo"
    repo.mkdir()

    result = bg.groom_repo(cfg, "owner/repo", repo)

    assert result["status"] == "no-data"
    assert result["created"] == 0
    assert result["skipped"] == 0
