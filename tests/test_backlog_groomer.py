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


def test_groom_repo_adds_created_issue_to_backlog(tmp_path, monkeypatch):
    cfg = {
        "root_dir": str(tmp_path),
        "worktrees_dir": str(tmp_path / "worktrees"),
        "github_owner": "owner",
        "github_projects": {
            "proj": {
                "project_number": 1,
                "backlog_value": "Backlog",
                "repos": [{"github_repo": "owner/repo", "local_repo": str(tmp_path / "repo")}],
            }
        },
    }
    repo = tmp_path / "repo"
    repo.mkdir()

    monkeypatch.setattr(bg, "_list_open_issues", lambda repo, cfg: [])
    monkeypatch.setattr(bg, "load_recent_metrics", lambda *args, **kwargs: [{"task_id": "t1"}])
    monkeypatch.setattr(bg, "_parse_known_issues", lambda repo_path: [])
    monkeypatch.setattr(bg, "_find_risk_flags", lambda cfg: [])
    monkeypatch.setattr(bg, "_call_haiku", lambda prompt: '[{"title":"Fix thing","body":"## Goal\\nX\\n## Success Criteria\\nY\\n## Constraints\\n- Prefer minimal diffs","priority":"prio:high","labels":["bug"]}]')
    monkeypatch.setattr(bg, "_open_issue_exists", lambda repo, title: False)

    created_urls = []
    monkeypatch.setattr(bg, "_create_issue", lambda repo, title, body, labels: created_urls.append("https://github.com/owner/repo/issues/10") or created_urls[-1])
    backlog_urls = []
    monkeypatch.setattr(bg, "_set_issue_backlog", lambda cfg, github_slug, issue_url: backlog_urls.append(issue_url))

    result = bg.groom_repo(cfg, "owner/repo", repo)

    assert result["status"] == "created"
    assert created_urls == ["https://github.com/owner/repo/issues/10"]
    assert backlog_urls == ["https://github.com/owner/repo/issues/10"]
