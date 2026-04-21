from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.init.state import State, delete_state, resumable_states, slugify_repo_name


def test_state_mark_and_reload(monkeypatch, tmp_path):
    monkeypatch.setattr("orchestrator.init.state.ROOT", tmp_path)
    state = State.for_slug("demo")
    state.mark("github.repo_name", "demo")
    state.mark("issues_created", [{"number": 1}])

    reloaded = State.for_slug("demo")
    assert reloaded.get("github.repo_name") == "demo"
    assert reloaded.get("issues_created")[0]["number"] == 1


def test_resumable_states_excludes_completed(monkeypatch, tmp_path):
    monkeypatch.setattr("orchestrator.init.state.ROOT", tmp_path)
    active = State.for_slug("active")
    active.mark("github.repo_name", "active")

    completed = State.for_slug("done")
    completed.complete()

    names = [state.get("github.repo_name", state.path.stem) for state in resumable_states()]
    assert names == ["active"]


def test_delete_state_uses_slugified_repo_name(monkeypatch, tmp_path):
    monkeypatch.setattr("orchestrator.init.state.ROOT", tmp_path)
    slug = slugify_repo_name("My Repo")
    state = State.for_slug(slug)
    state.save()

    deleted = delete_state(slug)
    assert deleted.name == f"{slug}.json"
    assert not deleted.exists()

