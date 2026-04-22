from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.init.__main__ import _decide_resume_action
from orchestrator.init.state import State


def test_decide_resume_action_asks_user(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("orchestrator.init.state.ROOT", tmp_path)
    state = State.for_slug("demo")
    state.mark("intake", {"idea": "a game", "kind": "other", "stack_preference": "auto", "success_criteria": "a toddler"})
    state.mark("github.inputs", {"owner": "kai-linux", "repo_name": "new-game"})

    monkeypatch.setattr("orchestrator.init.__main__.ui.choice", lambda *args, **kwargs: "2")
    action = _decide_resume_action(state)

    out = capsys.readouterr().out
    assert "Saved unfinished setup" in out
    assert "a game" in out
    assert "new-game" in out
    assert action == "2"
