from __future__ import annotations

import json
import os
import re
import subprocess
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


def get_repo_outcome_check_ids(
    cfg: dict,
    github_slug: str,
    issue_labels: list[str] | None = None,
) -> list[str]:
    """Return outcome check IDs configured for a repo (global + repo override + objective).

    When *issue_labels* is provided, only checks whose ``labels`` field
    overlaps with the issue labels (or checks with no ``labels`` restriction)
    are returned.  This enables per-issue-type outcome measurement.

    Objective-derived checks (from ``objectives/*.yaml``) are merged in
    automatically so issues get the correct outcome check IDs at dispatch time.
    """
    outcome_cfg = dict(cfg.get("outcome_attribution") or {})
    matched_repo_path: str | None = None
    for project_cfg in cfg.get("github_projects", {}).values():
        if not isinstance(project_cfg, dict):
            continue
        for repo_cfg in project_cfg.get("repos", []):
            if repo_cfg.get("github_repo") != github_slug:
                continue
            local_repo = str(repo_cfg.get("path") or repo_cfg.get("local_repo") or "").strip()
            if local_repo:
                matched_repo_path = local_repo
            override = repo_cfg.get("outcome_attribution")
            if isinstance(override, dict):
                merged = dict(outcome_cfg)
                merged.update(override)
                outcome_cfg = merged
            break
        else:
            continue
        break

    # Merge in objective-derived checks so dispatch-time IDs match planner-time IDs
    if matched_repo_path:
        try:
            from orchestrator.objectives import load_repo_objective, objective_outcome_checks
            objective = load_repo_objective(cfg, github_slug, Path(matched_repo_path).expanduser())
            obj_checks = objective_outcome_checks(objective)
            if obj_checks:
                existing_checks = list(outcome_cfg.get("checks") or [])
                existing_ids = {
                    str(c.get("id", "")).strip().lower()
                    for c in existing_checks
                    if isinstance(c, dict) and str(c.get("id", "")).strip()
                }
                for oc in obj_checks:
                    oc_id = str(oc.get("id", "")).strip().lower()
                    if oc_id and oc_id not in existing_ids:
                        existing_checks.append(oc)
                        existing_ids.add(oc_id)
                outcome_cfg["checks"] = existing_checks
        except Exception:
            pass  # Objective loading is best-effort

    checks = outcome_cfg.get("checks") or []
    normalized_labels = {l.strip().lower() for l in (issue_labels or []) if l.strip()} if issue_labels else None
    ids: list[str] = []
    for c in checks:
        if not isinstance(c, dict):
            continue
        check_id = str(c.get("id", "")).strip()
        if not check_id:
            continue
        check_labels = c.get("labels")
        if check_labels and normalized_labels is not None:
            check_label_set = {str(l).strip().lower() for l in check_labels if str(l).strip()}
            if not check_label_set & normalized_labels:
                continue
        ids.append(check_id)
    return ids


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


def capture_github_baseline(repo_slug: str) -> dict:
    """Fetch current GitHub stars/forks as a baseline snapshot at merge time.

    Uses the public GitHub API via ``gh``. Returns a dict with keys
    ``stars``, ``forks``, ``captured_at`` or an empty dict on failure.
    """
    try:
        raw = subprocess.run(
            ["gh", "api", f"repos/{repo_slug}",
             "--jq", "{stars: .stargazers_count, forks: .forks_count}"],
            capture_output=True, text=True, timeout=15,
        )
        if raw.returncode != 0:
            return {}
        data = json.loads(raw.stdout.strip())
        return {
            "stars": int(data.get("stars", 0)),
            "forks": int(data.get("forks", 0)),
            "captured_at": datetime.now(tz=timezone.utc).isoformat(),
        }
    except Exception:
        return {}


def load_metrics_baseline_from_stats(
    root_dir: str | Path,
    window_days: int = 7,
) -> dict:
    """Compute operational metric baselines from agent_stats.jsonl.

    Returns dict with ``success_rate``, ``avg_completion_seconds``,
    ``escalation_rate``, ``sample_size``, ``window_days`` or empty on failure.
    """
    stats_path = Path(root_dir).expanduser() / "runtime" / "metrics" / "agent_stats.jsonl"
    if not stats_path.exists():
        return {}
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=window_days)
    total = 0
    successes = 0
    escalations = 0
    durations: list[float] = []
    try:
        with open(stats_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = _parse_timestamp(rec.get("timestamp"))
                if ts is None or ts < cutoff:
                    continue
                total += 1
                status = str(rec.get("status", "")).lower()
                if status == "complete":
                    successes += 1
                if status in ("blocked", "escalated"):
                    escalations += 1
                dur = rec.get("duration_seconds")
                if dur is not None:
                    try:
                        durations.append(float(dur))
                    except (TypeError, ValueError):
                        pass
    except OSError:
        return {}
    if total == 0:
        return {}
    return {
        "success_rate": round(successes / total, 4),
        "avg_completion_seconds": round(sum(durations) / len(durations), 1) if durations else None,
        "escalation_rate": round(escalations / total, 4),
        "sample_size": total,
        "window_days": window_days,
        "captured_at": datetime.now(tz=timezone.utc).isoformat(),
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
