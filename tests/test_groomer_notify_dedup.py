"""Suppress identical backlog-groomer Telegram notifications.

The groomer cron fires hourly. Before this change, a persistent stable-state
error (e.g. eigendark-website's "LLM returned no usable issues") re-triggered
the full summary Telegram every hour. Nothing new happened, yet the operator
got the same message repeatedly. The dedup path keeps first-time-seen
notifications but suppresses re-sends of an identical signature.
"""
from __future__ import annotations

from orchestrator.backlog_groomer import (
    _groomer_notify_is_duplicate,
    _groomer_notify_record,
    _groomer_notify_state_path,
)


def _cfg(tmp_path) -> dict:
    return {"root_dir": str(tmp_path)}


def test_is_duplicate_returns_false_when_no_state_file(tmp_path):
    cfg = _cfg(tmp_path)
    assert _groomer_notify_is_duplicate(cfg, "eigendark-website:error:LLM returned no usable issues") is False


def test_is_duplicate_true_after_record_same_signature(tmp_path):
    cfg = _cfg(tmp_path)
    sig = "kai-linux/eigendark-website:error:LLM returned no usable issues"
    _groomer_notify_record(cfg, sig)
    assert _groomer_notify_is_duplicate(cfg, sig) is True


def test_is_duplicate_false_when_signature_changes(tmp_path):
    cfg = _cfg(tmp_path)
    _groomer_notify_record(cfg, "kai-linux/foo:error:timeout")
    # New run produces different signature
    assert _groomer_notify_is_duplicate(cfg, "kai-linux/foo:created:2") is False


def test_record_is_idempotent_and_writes_signature_and_ts(tmp_path):
    cfg = _cfg(tmp_path)
    sig = "kai-linux/eigendark:created:3"
    _groomer_notify_record(cfg, sig)
    _groomer_notify_record(cfg, sig)  # second call must not corrupt state
    import json
    payload = json.loads(_groomer_notify_state_path(cfg).read_text(encoding="utf-8"))
    assert payload["last_signature"] == sig
    assert "ts" in payload


def test_state_file_lives_under_runtime_state(tmp_path):
    cfg = _cfg(tmp_path)
    path = _groomer_notify_state_path(cfg)
    assert path.parent.name == "state"
    assert path.parent.parent.name == "runtime"
