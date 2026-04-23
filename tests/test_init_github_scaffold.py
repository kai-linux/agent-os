from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.init import github_scaffold as gs


def test_ensure_project_sets_new_project_private(monkeypatch):
    calls: list[list[str]] = []

    monkeypatch.setattr(gs, "_project_by_title", lambda owner, title: None)
    monkeypatch.setattr(
        gs,
        "gh_json",
        lambda args, timeout=60: {"id": "PVT_123", "number": 14, "url": "https://github.com/users/kai-linux/projects/14"},
    )

    def fake_gh_run(args, timeout=60, cwd=None):
        calls.append(args)
        return ""

    monkeypatch.setattr(gs, "gh_run", fake_gh_run)

    project = gs._ensure_project("kai-linux", "new-game")

    assert project["project_number"] == 14
    assert calls == [["project", "edit", "14", "--owner", "kai-linux", "--visibility", "PRIVATE"]]


def test_ensure_project_keeps_existing_project_without_edit(monkeypatch):
    monkeypatch.setattr(
        gs,
        "_project_by_title",
        lambda owner, title: {"id": "PVT_existing", "number": 3, "url": "https://github.com/users/kai-linux/projects/3"},
    )
    monkeypatch.setattr(gs, "gh_run", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not edit existing project")))

    project = gs._ensure_project("kai-linux", "new-game")

    assert project["project_number"] == 3


def test_ensure_agent_result_ignored_creates_new_gitignore(tmp_path):
    gi = tmp_path / ".gitignore"
    assert gs._ensure_agent_result_ignored(gi) is True
    assert ".agent_result.md" in gi.read_text()


def test_ensure_agent_result_ignored_appends_when_missing(tmp_path):
    gi = tmp_path / ".gitignore"
    gi.write_text("node_modules/\n.env\n")
    assert gs._ensure_agent_result_ignored(gi) is True
    body = gi.read_text()
    assert "node_modules/" in body
    assert ".agent_result.md" in body


def test_ensure_agent_result_ignored_idempotent(tmp_path):
    gi = tmp_path / ".gitignore"
    gi.write_text("node_modules/\n.agent_result.md\n.env\n")
    assert gs._ensure_agent_result_ignored(gi) is False
    assert gi.read_text() == "node_modules/\n.agent_result.md\n.env\n"


def test_ensure_agent_result_ignored_handles_missing_trailing_newline(tmp_path):
    gi = tmp_path / ".gitignore"
    gi.write_text("node_modules/")
    gs._ensure_agent_result_ignored(gi)
    body = gi.read_text()
    assert body.startswith("node_modules/\n")
    assert ".agent_result.md" in body
