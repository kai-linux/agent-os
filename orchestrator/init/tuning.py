"""Interactive tuning step for `agentos init`.

Walks the operator through cadence and concurrency settings so a newly
onboarded project isn't silently stuck with the hardcoded defaults.

When an existing ``config.yaml`` already has a value set, that existing
value is shown as the default so "just hit enter" preserves what the
operator already tuned — don't make them re-type the same number every
time they add a new project.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from orchestrator.init import ui
from orchestrator.init.state import State


@dataclass(frozen=True)
class TuningField:
    key: str
    prompt: str
    default: Any
    cast: type
    hint: str

    def explain(self) -> str:
        return f"{self.prompt}\n  ({self.hint})"


# Each field corresponds to a config.yaml key the operator should be aware of.
# `dependency_watcher_cadence_days` is flattened here and re-nested by
# ``config_emit.apply_tuning``.
_FIELDS: tuple[TuningField, ...] = (
    TuningField(
        key="sprint_cadence_days",
        prompt="Strategic planner cadence (days)",
        default=7,
        cast=int,
        hint="How often the planner re-prioritizes the backlog. 7 = weekly sprints.",
    ),
    TuningField(
        key="groomer_cadence_days",
        prompt="Backlog groomer cadence (days)",
        default=3.5,
        cast=float,
        hint="How often the groomer generates new issues for this repo. 3.5 = twice a week.",
    ),
    TuningField(
        key="max_parallel_workers",
        prompt="Max parallel agent workers",
        default=1,
        cast=int,
        hint="Tasks running at the same time. Start at 1; raise only after you've observed stability.",
    ),
    TuningField(
        key="max_runtime_minutes",
        prompt="Per-task runtime cap (minutes)",
        default=40,
        cast=int,
        hint="Hard timeout for a single agent run. Tasks exceeding this are marked failed.",
    ),
    TuningField(
        key="plan_size",
        prompt="Issues the groomer generates per run",
        default=5,
        cast=int,
        hint="Batch size. 3-5 is a good range for solo operators.",
    ),
    TuningField(
        key="default_max_attempts",
        prompt="Retries before giving up on a task",
        default=4,
        cast=int,
        hint="Each retry re-dispatches the task with a fresh worktree.",
    ),
    TuningField(
        key="dependency_watcher_cadence_days",
        prompt="Dependency watcher cadence (days)",
        default=7,
        cast=int,
        hint="How often the watcher checks for dependency updates. 0 disables.",
    ),
)


def _resolve_default(field: TuningField, existing_cfg: dict | None) -> Any:
    if not existing_cfg:
        return field.default
    if field.key == "dependency_watcher_cadence_days":
        dw = existing_cfg.get("dependency_watcher") or {}
        return dw.get("cadence_days", field.default)
    return existing_cfg.get(field.key, field.default)


def _cast_or_default(raw: str, field: TuningField, default: Any) -> Any:
    value = (raw or "").strip()
    if not value:
        return default
    try:
        return field.cast(value)
    except ValueError:
        ui.warn(f"Could not parse {value!r} as {field.cast.__name__}; keeping {default}.")
        return default


def run(state: State, *, existing_cfg: dict | None = None) -> dict[str, Any]:
    """Prompt the operator for cadence & threshold values.

    Returns a dict of tuning overrides (empty when operator accepts all
    defaults). The caller passes this to ``config_emit.run`` which overlays
    it onto the generated config payload.
    """
    if state.get("tuning"):
        return state.get("tuning")

    print("These control how fast the orchestrator runs your project. Press ")
    print("enter on each to accept the shown default — it's the current value ")
    print("from config.yaml when one exists, otherwise the Agent OS default.")
    print()

    tuning: dict[str, Any] = {}
    for field in _FIELDS:
        default = _resolve_default(field, existing_cfg)
        raw = ui.prompt(field.explain(), default=str(default))
        chosen = _cast_or_default(raw, field, default)
        # Only record the choice when it differs from the existing-config
        # default, so we don't churn the file with identical values.
        existing_value = _resolve_default(field, existing_cfg)
        if chosen != existing_value:
            tuning[field.key] = chosen

    state.mark("tuning", tuning)
    return tuning
