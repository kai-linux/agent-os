"""Coverage for the interactive tuning step added to `agentos init`.

Previously the cadence/threshold knobs (sprint_cadence_days,
groomer_cadence_days, max_parallel_workers, etc.) were hardcoded in
config_emit.build_config with no way for the operator to set them during
onboarding. A freshly onboarded project had to be hand-edited after the
fact. The tuning step walks the operator through each knob with the
current value as default so hitting enter preserves what's already set.
"""
from __future__ import annotations

from pathlib import Path

from orchestrator.init import tuning
from orchestrator.init.state import State


def _fresh_state(tmp_path: Path) -> State:
    return State(path=tmp_path / "demo.json", data={})


def test_tuning_uses_agentos_defaults_when_no_existing_cfg(monkeypatch, tmp_path):
    state = _fresh_state(tmp_path)
    # Simulate operator pressing enter on every prompt.
    monkeypatch.setattr(tuning.ui, "prompt", lambda *args, **kwargs: kwargs.get("default", ""))

    result = tuning.run(state, existing_cfg=None)

    # Accepting every default means no overrides are recorded — the tuning
    # dict is empty and config_emit will use the hardcoded build_config defaults.
    assert result == {}


def test_tuning_captures_operator_overrides(monkeypatch, tmp_path):
    state = _fresh_state(tmp_path)
    # Operator raises sprint cadence from 7 to 14, leaves everything else.
    prompts = iter([
        "14",   # sprint_cadence_days
        "",     # groomer_cadence_days (accept)
        "",     # max_parallel_workers (accept)
        "",     # max_runtime_minutes (accept)
        "",     # plan_size (accept)
        "",     # default_max_attempts (accept)
        "",     # dependency_watcher_cadence_days (accept)
    ])
    monkeypatch.setattr(tuning.ui, "prompt", lambda *args, **kwargs: next(prompts) or kwargs.get("default", ""))

    result = tuning.run(state, existing_cfg=None)

    assert result == {"sprint_cadence_days": 14}


def test_tuning_uses_existing_cfg_as_default(monkeypatch, tmp_path):
    state = _fresh_state(tmp_path)
    # Operator previously set sprint=14, groomer=7. If they press enter, those
    # existing values must survive — don't snap back to Agent OS defaults.
    existing = {
        "sprint_cadence_days": 14,
        "groomer_cadence_days": 7,
        "dependency_watcher": {"cadence_days": 30, "enabled": True},
    }
    monkeypatch.setattr(tuning.ui, "prompt", lambda *args, **kwargs: kwargs.get("default", ""))

    result = tuning.run(state, existing_cfg=existing)

    # All accepts match existing values → no overrides needed (they're already
    # in config.yaml and merge_config preserves them).
    assert result == {}


def test_tuning_casts_to_correct_types(monkeypatch, tmp_path):
    state = _fresh_state(tmp_path)
    prompts = iter([
        "7",     # sprint_cadence_days (int)
        "1.75",  # groomer_cadence_days (float)
        "2",     # max_parallel_workers (int)
        "60",    # max_runtime_minutes (int)
        "3",     # plan_size (int)
        "5",     # default_max_attempts (int)
        "14",    # dependency_watcher_cadence_days (int)
    ])
    monkeypatch.setattr(tuning.ui, "prompt", lambda *args, **kwargs: next(prompts))

    result = tuning.run(state, existing_cfg=None)

    assert result["groomer_cadence_days"] == 1.75
    assert isinstance(result["groomer_cadence_days"], float)
    assert result["max_parallel_workers"] == 2
    assert isinstance(result["max_parallel_workers"], int)


def test_tuning_resume_returns_stored_values(monkeypatch, tmp_path):
    state = _fresh_state(tmp_path)
    state.mark("tuning", {"plan_size": 3})
    # Should NOT prompt on resume; returns stored value directly.
    monkeypatch.setattr(tuning.ui, "prompt", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not prompt")))

    assert tuning.run(state, existing_cfg=None) == {"plan_size": 3}


def test_tuning_handles_invalid_input_by_keeping_default(monkeypatch, tmp_path):
    state = _fresh_state(tmp_path)
    prompts = iter(["not-a-number", "", "", "", "", "", ""])
    monkeypatch.setattr(tuning.ui, "prompt", lambda *args, **kwargs: next(prompts))

    result = tuning.run(state, existing_cfg=None)

    # Bad parse → keep default → no override recorded
    assert result == {}
