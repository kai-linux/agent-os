from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.init.state import State
from orchestrator.init import telegram_pair


def test_run_can_reuse_existing_config_telegram(monkeypatch, tmp_path):
    monkeypatch.setattr("orchestrator.init.state.ROOT", tmp_path)
    state = State.for_slug("demo")

    monkeypatch.setattr("orchestrator.init.telegram_pair._username_for_existing_token", lambda token: "clawlbot")
    monkeypatch.setattr("orchestrator.init.telegram_pair.ui.choice", lambda *args, **kwargs: "1")

    result = telegram_pair.run(
        state,
        "kai-linux/demo",
        existing_cfg={"telegram_bot_token": "123456:abcdefghijklmnopqrstuvwxyzABCDE_12345", "telegram_chat_id": "42"},
        dry_run=False,
    )

    assert result["bot_username"] == "clawlbot"
    assert result["chat_id"] == "42"
    assert state.get("telegram.chat_id") == "42"
