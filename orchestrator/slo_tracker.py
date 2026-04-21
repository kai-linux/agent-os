"""Daily SLO state tracker.

Reads opt-in per-repo SLO definitions from ``slos/<repo>.yaml``, computes the
current state from runtime telemetry, and rewrites
``runtime/metrics/slo_state.jsonl`` with one row per tracked SLO.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from orchestrator.agent_scorer import _TRANSIENT_BLOCKER_CODES
from orchestrator.outcome_attribution import load_outcome_records
from orchestrator.paths import load_config


SLO_STATE_FILENAME = "slo_state.jsonl"
_SLO_DIRNAME = "slos"
_DEFAULT_WINDOW_DAYS = 30
_DEFAULT_SLOS = {
    "success_rate": {
        "metric": "success_rate",
        "operator": ">=",
        "target": 0.90,
        "window_days": _DEFAULT_WINDOW_DAYS,
    },
    "merge_cycle_p95": {
        "metric": "merge_cycle_p95",
        "operator": "<=",
        "target": 4.0,  # hours
        "window_days": _DEFAULT_WINDOW_DAYS,
    },
}


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


def _parse_window_days(value: object, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return max(1, int(value))
    text = str(value).strip().lower()
    if not text:
        return default
    if text.endswith("d"):
        text = text[:-1]
    try:
        return max(1, int(float(text)))
    except ValueError:
        return default


def _parse_hours(value: object, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower()
    if not text:
        return default
    multipliers = {"h": 1.0, "hr": 1.0, "hrs": 1.0, "hour": 1.0, "hours": 1.0, "m": 1 / 60.0, "min": 1 / 60.0, "mins": 1 / 60.0, "minute": 1 / 60.0, "minutes": 1 / 60.0, "d": 24.0, "day": 24.0, "days": 24.0}
    for suffix, multiplier in multipliers.items():
        if text.endswith(suffix):
            try:
                return float(text[: -len(suffix)].strip()) * multiplier
            except ValueError:
                return default
    try:
        return float(text)
    except ValueError:
        return default


def _repo_slug_aliases(cfg: dict) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for project_cfg in cfg.get("github_projects", {}).values():
        if not isinstance(project_cfg, dict):
            continue
        for repo_cfg in project_cfg.get("repos", []):
            if not isinstance(repo_cfg, dict):
                continue
            slug = str(repo_cfg.get("github_repo") or "").strip()
            if not slug:
                continue
            path_value = str(repo_cfg.get("path") or repo_cfg.get("local_repo") or "").strip()
            candidates = {slug, slug.rsplit("/", 1)[-1]}
            if path_value:
                path = Path(path_value).expanduser()
                candidates.add(path.name)
                candidates.add(str(path))
            for candidate in candidates:
                candidate = str(candidate).strip()
                if candidate and candidate not in aliases:
                    aliases[candidate] = slug
    return aliases


def _resolve_repo_slug(cfg: dict, slo_path: Path, payload: dict) -> str:
    explicit = str(payload.get("repo") or payload.get("github_repo") or "").strip()
    aliases = _repo_slug_aliases(cfg)
    if explicit:
        return aliases.get(explicit, explicit)
    return aliases.get(slo_path.stem, slo_path.stem)


def _normalize_slo(repo: str, raw: dict) -> dict | None:
    if not isinstance(raw, dict):
        return None
    slo_id = str(raw.get("id") or raw.get("metric") or "").strip()
    if not slo_id:
        return None
    defaults = _DEFAULT_SLOS.get(slo_id, {})
    metric = str(raw.get("metric") or defaults.get("metric") or slo_id).strip()
    operator = str(raw.get("operator") or raw.get("op") or defaults.get("operator") or "").strip() or ">="
    if metric == "success_rate":
        target = float(raw.get("target", defaults.get("target", 0.90)))
    elif metric == "merge_cycle_p95":
        target = _parse_hours(raw.get("target"), float(defaults.get("target", 4.0)))
    else:
        return None
    return {
        "repo": repo,
        "slo_id": slo_id,
        "metric": metric,
        "operator": operator,
        "target": target,
        "window_days": _parse_window_days(raw.get("window_days"), int(defaults.get("window_days", _DEFAULT_WINDOW_DAYS))),
    }


def load_slo_definitions(cfg: dict) -> list[dict]:
    root = Path(cfg.get("root_dir", ".")).expanduser()
    slo_dir = root / _SLO_DIRNAME
    if not slo_dir.exists():
        return []

    definitions: list[dict] = []
    for slo_path in sorted(list(slo_dir.glob("*.yaml")) + list(slo_dir.glob("*.yml"))):
        try:
            payload = yaml.safe_load(slo_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(payload, dict):
            continue
        repo = _resolve_repo_slug(cfg, slo_path, payload)
        raw_slos = payload.get("slos")
        if isinstance(raw_slos, list):
            iterable = raw_slos
        else:
            iterable = []
            for key, value in payload.items():
                if key in {"repo", "github_repo", "defaults"}:
                    continue
                if isinstance(value, dict):
                    item = dict(value)
                    item.setdefault("id", key)
                    iterable.append(item)
        for raw in iterable:
            normalized = _normalize_slo(repo, raw)
            if normalized:
                definitions.append(normalized)
    return definitions


def _load_agent_stats(metrics_dir: Path) -> list[dict]:
    stats_path = metrics_dir / "agent_stats.jsonl"
    if not stats_path.exists():
        return []
    records: list[dict] = []
    with stats_path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            records.append(record)
    return records


def _calc_success_rate_slo(records: list[dict], slo: dict, now: datetime) -> dict:
    cutoff = now - timedelta(days=int(slo["window_days"]))
    relevant: list[dict] = []
    for rec in records:
        repo = str(rec.get("github_repo") or rec.get("repo") or "").strip()
        if repo != slo["repo"]:
            continue
        ts = _parse_timestamp(rec.get("timestamp"))
        if ts is None or ts < cutoff:
            continue
        blocker_code = str(rec.get("blocker_code") or "").strip().lower()
        if blocker_code in _TRANSIENT_BLOCKER_CODES:
            continue
        relevant.append(rec)

    total = len(relevant)
    if total == 0:
        return {"current": None, "budget_remaining_pct": None, "burn_rate": None}

    successes = sum(1 for rec in relevant if str(rec.get("status") or "").strip().lower() == "complete")
    current = successes / total
    target = float(slo["target"])
    error_budget = max(1e-9, 1.0 - target)
    actual_error = max(0.0, 1.0 - current)
    budget_remaining_pct = max(0.0, ((error_budget - actual_error) / error_budget) * 100.0)
    burn_rate = actual_error / error_budget
    return {
        "current": round(current, 4),
        "budget_remaining_pct": round(budget_remaining_pct, 2),
        "burn_rate": round(burn_rate, 4),
    }


def _nearest_rank_percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def _calc_merge_cycle_slo(outcome_records: list[dict], slo: dict, now: datetime) -> dict:
    opened_by_pr: dict[tuple[str, int], datetime] = {}
    for rec in outcome_records:
        if rec.get("record_type") != "attribution" or rec.get("event") != "pr_opened":
            continue
        repo = str(rec.get("repo") or "").strip()
        pr_number = rec.get("pr_number")
        opened_at = _parse_timestamp(rec.get("timestamp"))
        if not repo or not isinstance(pr_number, int) or opened_at is None:
            continue
        key = (repo, pr_number)
        prior = opened_by_pr.get(key)
        if prior is None or opened_at < prior:
            opened_by_pr[key] = opened_at

    cutoff = now - timedelta(days=int(slo["window_days"]))
    durations_hours: list[float] = []
    for rec in outcome_records:
        if rec.get("record_type") != "attribution" or rec.get("event") != "merged":
            continue
        repo = str(rec.get("repo") or "").strip()
        if repo != slo["repo"]:
            continue
        merged_at = _parse_timestamp(rec.get("merged_at") or rec.get("timestamp"))
        if merged_at is None or merged_at < cutoff:
            continue
        pr_number = rec.get("pr_number")
        if not isinstance(pr_number, int):
            continue
        opened_at = opened_by_pr.get((repo, pr_number))
        if opened_at is None or opened_at > merged_at:
            continue
        durations_hours.append((merged_at - opened_at).total_seconds() / 3600.0)

    current = _nearest_rank_percentile(durations_hours, 0.95)
    if current is None:
        return {"current": None, "budget_remaining_pct": None, "burn_rate": None}

    target = float(slo["target"])
    budget_remaining_pct = max(0.0, ((target - current) / max(target, 1e-9)) * 100.0)
    burn_rate = current / max(target, 1e-9)
    return {
        "current": round(current, 4),
        "budget_remaining_pct": round(budget_remaining_pct, 2),
        "burn_rate": round(burn_rate, 4),
    }


def build_slo_state_records(cfg: dict, now: datetime | None = None) -> list[dict]:
    observed_at = (now or datetime.now(tz=timezone.utc)).astimezone(timezone.utc)
    root = Path(cfg.get("root_dir", ".")).expanduser()
    metrics_dir = root / "runtime" / "metrics"
    agent_stats = _load_agent_stats(metrics_dir)
    outcome_records = load_outcome_records(cfg)
    records: list[dict] = []

    for slo in load_slo_definitions(cfg):
        if slo["metric"] == "success_rate":
            state = _calc_success_rate_slo(agent_stats, slo, observed_at)
        elif slo["metric"] == "merge_cycle_p95":
            state = _calc_merge_cycle_slo(outcome_records, slo, observed_at)
        else:
            continue
        records.append(
            {
                "repo": slo["repo"],
                "slo_id": slo["slo_id"],
                "target": round(float(slo["target"]), 4),
                "current": state["current"],
                "budget_remaining_pct": state["budget_remaining_pct"],
                "burn_rate": state["burn_rate"],
                "ts": observed_at.isoformat(),
            }
        )
    return records


def write_slo_state(metrics_dir: Path, records: list[dict]) -> Path:
    metrics_dir.mkdir(parents=True, exist_ok=True)
    output_path = metrics_dir / SLO_STATE_FILENAME
    payload = "".join(json.dumps(record, sort_keys=True) + "\n" for record in records)
    fd, tmp_path = tempfile.mkstemp(dir=metrics_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp_path, output_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return output_path


def rebuild_slo_state(cfg: dict, now: datetime | None = None) -> Path:
    root = Path(cfg.get("root_dir", ".")).expanduser()
    metrics_dir = root / "runtime" / "metrics"
    return write_slo_state(metrics_dir, build_slo_state_records(cfg, now=now))


def main() -> int:
    cfg = load_config()
    output_path = rebuild_slo_state(cfg)
    print(f"Wrote {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
