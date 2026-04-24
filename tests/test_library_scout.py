from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator import library_scout as scout


def _cfg(tmp_path: Path, repo: Path) -> dict:
    return {
        "root_dir": str(tmp_path),
        "tool_registry": {"library_catalog_file": str(tmp_path / "library_catalog.yaml")},
        "library_scout": {"enabled": True, "cadence_days": 30, "max_suggestions_per_repo": 3},
        "github_projects": {"proj": {"repos": [{"github_repo": "owner/repo", "local_repo": str(repo)}]}},
    }


def test_scout_repo_suggests_only_catalog_listed_packages(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text(
        "We need structured extraction with schema validation and strong pydantic outputs.\n",
        encoding="utf-8",
    )
    (tmp_path / "library_catalog.yaml").write_text(
        """libraries:
  - package: instructor
    ecosystem: python
    summary: Structured extraction
    keywords: [structured extraction, schema validation]
  - package: dspy
    ecosystem: python
    summary: Retrieval optimization
    keywords: [retrieval, optimization]
""",
        encoding="utf-8",
    )

    result = scout.scout_repo(_cfg(tmp_path, repo), "owner/repo", repo)

    assert [item["package"] for item in result["suggestions"]] == ["instructor"]
    assert all(item["package"] in {"instructor", "dspy"} for item in result["suggestions"])


def test_scout_repo_does_not_suggest_library_already_present(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("structured extraction and schema validation\n", encoding="utf-8")
    (repo / "requirements.txt").write_text("instructor==1.0.0\n", encoding="utf-8")
    (tmp_path / "library_catalog.yaml").write_text(
        """libraries:
  - package: instructor
    ecosystem: python
    summary: Structured extraction
    keywords: [structured extraction, schema validation]
""",
        encoding="utf-8",
    )

    result = scout.scout_repo(_cfg(tmp_path, repo), "owner/repo", repo)

    assert result["suggestions"] == []
