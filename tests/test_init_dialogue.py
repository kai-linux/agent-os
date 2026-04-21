from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.init import dialogue
from orchestrator.init.github_scaffold import suggest_repo_name


def test_dialogue_defaults_allow_blank_answers(monkeypatch):
    answers = iter(["", "", "", ""])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

    result = dialogue.run()

    assert result["idea"] == dialogue.DEFAULT_IDEA
    assert result["kind"] == "other"
    assert result["stack_preference"] == "auto"
    assert result["success_criteria"] == dialogue.DEFAULT_SUCCESS


def test_suggest_repo_name_uses_idea_words():
    name = suggest_repo_name({"idea": "A multimodal cooking coach for busy parents"})
    assert name == "multimodal-cooking-coach-busy"


def test_suggest_repo_name_falls_back_when_idea_is_vague():
    name = suggest_repo_name({"idea": ""})
    assert name == "new-project"
