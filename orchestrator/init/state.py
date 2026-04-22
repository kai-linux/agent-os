from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestrator.paths import ROOT


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def init_state_dir() -> Path:
    path = ROOT / "runtime" / "init_state"
    path.mkdir(parents=True, exist_ok=True)
    return path


def slugify_repo_name(name: str) -> str:
    value = "".join(ch.lower() if ch.isalnum() or ch in "-_" else "-" for ch in name.strip())
    while "--" in value:
        value = value.replace("--", "-")
    return value.strip("-_") or "project"


def _new_state() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "started_at": utc_now_iso(),
        "completed_at": None,
    }


def _walk(data: dict[str, Any], dotted_key: str) -> tuple[dict[str, Any] | None, str]:
    current: dict[str, Any] = data
    parts = dotted_key.split(".")
    for part in parts[:-1]:
        value = current.get(part)
        if not isinstance(value, dict):
            return None, parts[-1]
        current = value
    return current, parts[-1]


@dataclass
class State:
    path: Path
    data: dict[str, Any]

    @classmethod
    def for_slug(cls, slug: str) -> "State":
        path = init_state_dir() / f"{slug}.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            data = _new_state()
        return cls(path=path, data=data)

    @classmethod
    def from_path(cls, path: Path) -> "State":
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            data = _new_state()
        return cls(path=path, data=data)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        tmp_path.write_text(json.dumps(self.data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, self.path)
        os.chmod(self.path, 0o600)

    def get(self, dotted_key: str, default: Any = None) -> Any:
        current: Any = self.data
        for part in dotted_key.split("."):
            if not isinstance(current, dict) or part not in current:
                return default
            current = current[part]
        return current

    def has(self, dotted_key: str) -> bool:
        sentinel = object()
        return self.get(dotted_key, sentinel) is not sentinel

    def mark(self, dotted_key: str, value: Any) -> None:
        current = self.data
        parts = dotted_key.split(".")
        for part in parts[:-1]:
            existing = current.get(part)
            if not isinstance(existing, dict):
                existing = {}
                current[part] = existing
            current = existing
        current[parts[-1]] = value
        self.save()

    def merge(self, dotted_key: str, values: dict[str, Any]) -> None:
        existing = self.get(dotted_key, {})
        if not isinstance(existing, dict):
            existing = {}
        existing.update(values)
        self.mark(dotted_key, existing)

    def complete(self) -> None:
        self.mark("completed_at", utc_now_iso())

    def reset(self) -> None:
        self.data = _new_state()
        self.save()


def list_state_paths() -> list[Path]:
    return sorted(init_state_dir().glob("*.json"))


def load_all_states() -> list[State]:
    return [State.from_path(path) for path in list_state_paths()]


def resumable_states() -> list[State]:
    states: list[State] = []
    for state in load_all_states():
        if state.data.get("completed_at"):
            continue
        states.append(state)
    return states


def delete_state(slug: str) -> Path:
    path = init_state_dir() / f"{slug}.json"
    if path.exists():
        path.unlink()
    return path
