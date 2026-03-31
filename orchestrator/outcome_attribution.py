from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


OUTCOME_LOG_FILENAME = "outcome_attribution.jsonl"
OUTCOME_INTERPRETATIONS = {"improved", "unchanged", "regressed", "inconclusive"}


def parse_outcome_check_ids(section_text: str) -> list[str]:
    ids: list[str] = []
    for raw_line in str(section_text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = re.sub(r"^[-*]\s*", "", line)
        if not line or line.lower() in {"none", "n/a", "na"}:
            continue
        for piece in re.split(r"[,\s]+", line):
            value = piece.strip().lower()
            if not value:
                continue
            value = re.sub(r"[^a-z0-9_.-]+", "", value)
            if value and value not in ids:
                ids.append(value)
    return ids


def get_repo_outcome_check_ids(cfg: dict, github_slug: str) -> list[str]:
    """Return outcome check IDs configured for a repo (global + repo override)."""
    outcome_cfg = dict(cfg.get("outcome_attribution") or {})
    for project_cfg in cfg.get("github_projects", {}).values():
        if not isinstance(project_cfg, dict):
            continue
        for repo_cfg in project_cfg.get("repos", []):
            if repo_cfg.get("github_repo") != github_slug:
                continue
            override = repo_cfg.get("outcome_attribution")
            if isinstance(override, dict):
                merged = dict(outcome_cfg)
                merged.update(override)
                outcome_cfg = merged
            break
        else:
            continue
        break
    checks = outcome_cfg.get("checks") or []
    return [str(c.get("id", "")).strip() for c in checks if isinstance(c, dict) and str(c.get("id", "")).strip()]


def format_outcome_checks_section(check_ids: list[str]) -> str:
    """Return an '## Outcome Checks' markdown section, or empty string if no IDs."""
    if not check_ids:
        return ""
    return "\n\n## Outcome Checks\n" + ", ".join(check_ids)


def extract_pr_number(value: str | None) -> int | None:
    match = re.search(r"/pull/(\d+)\b", str(value or ""))
    return int(match.group(1)) if match else None


def extract_task_id_from_pr_title(title: str | None) -> str | None:
    match = re.fullmatch(r"Agent:\s+(.+)", str(title or "").strip())
    return match.group(1).strip() if match else None


def outcome_log_path(cfg: dict) -> Path:
    root = Path(cfg.get("root_dir", ".")).expanduser()
    return root / "runtime" / "metrics" / OUTCOME_LOG_FILENAME


def append_outcome_record(cfg: dict, record: dict) -> Path:
    log_path = outcome_log_path(cfg)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(record)
    payload.setdefault("timestamp", datetime.now(tz=timezone.utc).isoformat())
    line = json.dumps(payload, sort_keys=True) + "\n"

    existing = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    fd, tmp_path = tempfile.mkstemp(dir=log_path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(existing + line)
        os.replace(tmp_path, log_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return log_path


def load_outcome_records(
    cfg: dict,
    *,
    repo: str | None = None,
    window_days: int | None = None,
) -> list[dict]:
    log_path = outcome_log_path(cfg)
    if not log_path.exists():
        return []

    cutoff = None
    if window_days is not None:
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=window_days)

    records: list[dict] = []
    with log_path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if repo and record.get("repo") != repo:
                continue
            if cutoff is not None:
                parsed = _parse_timestamp(record.get("timestamp"))
                if parsed is None or parsed < cutoff:
                    continue
            records.append(record)
    return records


def _parse_timestamp(value: str | None) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
