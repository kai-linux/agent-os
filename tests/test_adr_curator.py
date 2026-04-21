"""Tests for ADR extraction and planner-facing ADR context."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator import adr_curator


def test_curate_pr_writes_adr_and_index(tmp_path, monkeypatch):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    cfg = {"root_dir": str(tmp_path)}

    def fake_gh_json(cmd: list[str]):
        if cmd[:3] == ["pr", "view", "12"]:
            return {
                "number": 12,
                "title": "Adopt SQLite event schema and migrations",
                "body": "Architectural work.",
                "url": "https://github.com/owner/repo/pull/12",
                "mergedAt": "2026-04-21T10:00:00Z",
                "author": {"login": "kai"},
                "labels": [{"name": "task:architecture"}],
            }
        if cmd[:2] == ["api", "repos/owner/repo/pulls/12/files?per_page=100"]:
            return [
                {"filename": "db/migrations/0001_init.sql"},
                {"filename": "pyproject.toml"},
            ]
        raise AssertionError(cmd)

    monkeypatch.setattr(adr_curator, "gh_json", fake_gh_json)

    adr_path = adr_curator.curate_pr(cfg, "owner/repo", repo_path, pr_number=12)

    assert adr_path is not None
    assert adr_path.exists()
    content = adr_path.read_text(encoding="utf-8")
    assert "## Context" in content
    assert "## Decision" in content
    assert "## Consequences" in content
    assert "## Date" in content
    assert "## PR Link" in content
    assert "<!-- adr-source: owner/repo#12 -->" in content

    index_path = repo_path / "docs" / "adrs" / "INDEX.md"
    assert index_path.exists()
    index = index_path.read_text(encoding="utf-8")
    assert "Adopt SQLite event schema and migrations" in index
    assert "owner/repo#12" in index


def test_curate_pr_skips_dependency_bump_noise(tmp_path, monkeypatch):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    def fake_gh_json(cmd: list[str]):
        if cmd[:3] == ["pr", "view", "7"]:
            return {
                "number": 7,
                "title": "chore(deps): bump requests from 2.31 to 2.32",
                "url": "https://github.com/owner/repo/pull/7",
                "mergedAt": "2026-04-21T10:00:00Z",
                "author": {"login": "dependabot[bot]"},
                "labels": [],
            }
        if cmd[:2] == ["api", "repos/owner/repo/pulls/7/files?per_page=100"]:
            return [{"filename": "pyproject.toml"}]
        raise AssertionError(cmd)

    monkeypatch.setattr(adr_curator, "gh_json", fake_gh_json)

    assert adr_curator.curate_pr({}, "owner/repo", repo_path, pr_number=7) is None
    assert not (repo_path / "docs" / "adrs").exists()


def test_read_recent_adrs_returns_latest_first(tmp_path):
    repo_path = tmp_path / "repo"
    adrs = repo_path / "docs" / "adrs"
    adrs.mkdir(parents=True)
    (adrs / "0001-first.md").write_text(
        "# ADR 0001: First decision\n\n<!-- adr-source: owner/repo#1 -->\n\n## Date\n\n2026-04-20\n\n## PR Link\n\n- [owner/repo#1](https://example.com/1)\n",
        encoding="utf-8",
    )
    (adrs / "0002-second.md").write_text(
        "# ADR 0002: Second decision\n\n<!-- adr-source: owner/repo#2 -->\n\n## Date\n\n2026-04-21\n\n## PR Link\n\n- [owner/repo#2](https://example.com/2)\n",
        encoding="utf-8",
    )

    summary = adr_curator.read_recent_adrs(repo_path, limit=1)

    assert "ADR 0002: Second decision" in summary
    assert "ADR 0001" not in summary
