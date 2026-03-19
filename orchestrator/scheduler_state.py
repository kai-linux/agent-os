"""Helpers for config-driven scheduler cadence and persisted run state."""
from __future__ import annotations

import json
import fcntl
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from orchestrator.paths import runtime_paths


def _state_path(cfg: dict) -> Path:
    paths = runtime_paths(cfg)
    state_dir = paths["ROOT"] / "runtime" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / "scheduler_state.json"


def load_scheduler_state(cfg: dict) -> dict:
    path = _state_path(cfg)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_scheduler_state(cfg: dict, state: dict) -> None:
    path = _state_path(cfg)
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def get_last_run(cfg: dict, job_name: str, github_slug: str) -> datetime | None:
    state = load_scheduler_state(cfg)
    raw = ((state.get(job_name) or {}).get(github_slug) or {}).get("last_run")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def record_run(cfg: dict, job_name: str, github_slug: str, now: datetime | None = None) -> None:
    state = load_scheduler_state(cfg)
    now = now or datetime.now(timezone.utc)
    state.setdefault(job_name, {})
    state[job_name][github_slug] = {"last_run": now.isoformat()}
    save_scheduler_state(cfg, state)


def is_due(
    cfg: dict,
    job_name: str,
    github_slug: str,
    cadence_hours: float,
    now: datetime | None = None,
) -> tuple[bool, str]:
    now = now or datetime.now(timezone.utc)
    if cadence_hours <= 0:
        return False, "dormant"

    last_run = get_last_run(cfg, job_name, github_slug)
    if last_run is None:
        return True, "never-run"

    due_at = last_run + timedelta(hours=cadence_hours)
    if now >= due_at:
        return True, "due"

    remaining = due_at - now
    remaining_hours = max(remaining.total_seconds() / 3600.0, 0.0)
    return False, f"next due in {remaining_hours:.1f}h"


@contextmanager
def job_lock(cfg: dict, job_name: str):
    """Prevent overlapping cron runs of the same scheduled job."""
    paths = runtime_paths(cfg)
    state_dir = paths["ROOT"] / "runtime" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / f"{job_name}.lock"
    fh = lock_path.open("w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield True
    except BlockingIOError:
        yield False
    finally:
        try:
            fh.close()
        except Exception:
            pass
