"""Monthly curated library scout with operator-gated spike suggestions."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from orchestrator.paths import load_config
from orchestrator.scheduler_state import is_due, job_lock, record_run
from orchestrator.tool_registry import load_library_catalog


SCOUT_JOB_NAME = "library_scout"
DEFAULT_CADENCE_DAYS = 30.0
DEFAULT_MAX_SUGGESTIONS_PER_REPO = 3
_WORD_RE = re.compile(r"[a-z0-9_./+-]+", re.IGNORECASE)
_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+([A-Za-z0-9_]+)", re.MULTILINE)
_REQUIREMENTS_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*(?:[<>=!~].*)?$", re.MULTILINE)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _slugify_repo(github_slug: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", str(github_slug or "").strip()) or "repo"


def _artifact_path(cfg: dict, github_slug: str) -> Path:
    root = Path(cfg.get("root_dir", ".")).expanduser()
    path = root / "runtime" / "analysis" / "library_scout" / f"{_slugify_repo(github_slug)}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _repo_library_scout_config(cfg: dict, github_slug: str) -> dict[str, Any]:
    merged = dict(cfg.get("library_scout") or {})
    merged.setdefault("enabled", True)
    merged.setdefault("cadence_days", DEFAULT_CADENCE_DAYS)
    merged.setdefault("max_suggestions_per_repo", DEFAULT_MAX_SUGGESTIONS_PER_REPO)
    for project_cfg in (cfg.get("github_projects") or {}).values():
        if not isinstance(project_cfg, dict):
            continue
        for repo_cfg in project_cfg.get("repos", []) or []:
            if repo_cfg.get("github_repo") != github_slug:
                continue
            override = repo_cfg.get("library_scout")
            if isinstance(override, dict):
                updated = dict(merged)
                updated.update(override)
                return updated
            return merged
    return merged


def _resolve_repos(cfg: dict) -> list[tuple[str, Path]]:
    repos: list[tuple[str, Path]] = []
    seen: set[tuple[str, str]] = set()
    for project_cfg in (cfg.get("github_projects") or {}).values():
        if not isinstance(project_cfg, dict):
            continue
        for repo_cfg in project_cfg.get("repos", []) or []:
            github_slug = str(repo_cfg.get("github_repo") or "").strip()
            local_repo = str(repo_cfg.get("local_repo") or repo_cfg.get("path") or "").strip()
            if not github_slug or not local_repo:
                continue
            key = (github_slug, local_repo)
            if key in seen:
                continue
            seen.add(key)
            repos.append((github_slug, Path(local_repo).expanduser()))
    return repos


def _read_signal_text(repo_path: Path) -> str:
    chunks: list[str] = []
    for name in (
        "README.md",
        "NORTH_STAR.md",
        "STRATEGY.md",
        "PLANNING_PRINCIPLES.md",
        "RUBRIC.md",
        "PRODUCTION_FEEDBACK.md",
        "PRODUCT_INSPECTION.md",
    ):
        path = repo_path / name
        if not path.exists():
            continue
        try:
            chunks.append(path.read_text(encoding="utf-8", errors="replace")[:12000])
        except OSError:
            continue
    return "\n".join(chunks).lower()


def _repo_tokens(repo_path: Path) -> set[str]:
    tokens: set[str] = set()
    for name in ("requirements.txt", "pyproject.toml", "package.json"):
        path = repo_path / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        tokens.update(match.lower() for match in _WORD_RE.findall(text))
        tokens.update(match.group(1).lower() for match in _REQUIREMENTS_RE.finditer(text))
    for py_file in repo_path.rglob("*.py"):
        try:
            text = py_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        tokens.update(match.group(1).lower() for match in _IMPORT_RE.finditer(text[:8000]))
    return tokens


def scout_repo(cfg: dict, github_slug: str, repo_path: Path) -> dict[str, Any]:
    scout_cfg = _repo_library_scout_config(cfg, github_slug)
    if not scout_cfg.get("enabled", True):
        return {"repo": github_slug, "suggestions": [], "skipped": "disabled"}
    if not repo_path.exists():
        return {"repo": github_slug, "suggestions": [], "skipped": "missing_repo"}

    signal_text = _read_signal_text(repo_path)
    repo_tokens = _repo_tokens(repo_path)
    suggestions: list[dict[str, Any]] = []
    for entry in load_library_catalog(cfg):
        package = str(entry.get("package") or "").strip()
        if not package:
            continue
        package_token = package.split("/", 1)[-1].replace("-", "_").lower()
        if package.lower() in repo_tokens or package_token in repo_tokens:
            continue
        keywords = [str(item).strip().lower() for item in entry.get("keywords") or [] if str(item).strip()]
        matched = [keyword for keyword in keywords if keyword in signal_text]
        if not matched:
            continue
        suggestions.append(
            {
                "id": f"{github_slug}:{package}",
                "package": package,
                "ecosystem": str(entry.get("ecosystem") or "python").strip().lower(),
                "summary": str(entry.get("summary") or "").strip(),
                "reason": str(entry.get("reason") or entry.get("summary") or "").strip(),
                "keywords": matched[:4],
                "spike_title": str(entry.get("spike_title") or f"Spike {package} for repo workflow fit").strip(),
                "task_type": str(entry.get("task_type") or "research").strip().lower(),
                "labels": [str(label).strip() for label in entry.get("labels") or ["enhancement"] if str(label).strip()],
            }
        )

    suggestions.sort(key=lambda item: (-len(item.get("keywords") or []), item.get("package") or ""))
    suggestions = suggestions[: int(scout_cfg.get("max_suggestions_per_repo", DEFAULT_MAX_SUGGESTIONS_PER_REPO))]
    payload = {
        "generated_at": _now_utc().isoformat(),
        "repo": github_slug,
        "suggestions": suggestions,
    }
    _artifact_path(cfg, github_slug).write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return {"repo": github_slug, "suggestions": suggestions, "skipped": None if suggestions else "no_matches"}


def load_recent_suggestions(cfg: dict, github_slug: str, *, max_age_days: float = 90.0) -> list[dict[str, Any]]:
    path = _artifact_path(cfg, github_slug)
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    generated_at = payload.get("generated_at")
    try:
        ts = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00"))
    except Exception:
        return []
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    if _now_utc() - ts.astimezone(timezone.utc) > timedelta(days=max_age_days):
        return []
    suggestions = payload.get("suggestions") or []
    return [item for item in suggestions if isinstance(item, dict)]


def issue_for_suggestion(suggestion: dict[str, Any]) -> dict[str, Any]:
    package = str(suggestion.get("package") or "").strip()
    summary = str(suggestion.get("summary") or "").strip()
    keywords = [str(item).strip() for item in suggestion.get("keywords") or [] if str(item).strip()]
    labels = [str(label).strip() for label in suggestion.get("labels") or ["enhancement"] if str(label).strip()]
    if "library-spike" not in labels:
        labels.append("library-spike")
    if "operator-approval-required" not in labels:
        labels.append("operator-approval-required")
    lines = [
        "## Goal",
        f"Run a bounded spike on curated library `{package}` and decide whether it should be adopted.",
        "",
        "## Success Criteria",
        f"- Evaluate `{package}` against the repo's stated workflow/problem area.",
        "- Produce a short recommendation covering fit, risks, and migration cost.",
        "- Do not add the dependency to production code without explicit operator follow-up approval.",
        "",
        "## Constraints",
        "- Prefer minimal diffs.",
        "- Suggestion-only scouting must not open dependency PRs.",
    ]
    if summary or keywords:
        lines.extend(["", "## Scout Evidence"])
        if summary:
            lines.append(f"- Catalog rationale: {summary}")
        if keywords:
            lines.append(f"- Matched repo signals: {', '.join(keywords)}")
    return {
        "title": str(suggestion.get("spike_title") or f"Spike {package} for repo workflow fit").strip(),
        "body": "\n".join(lines),
        "task_type": str(suggestion.get("task_type") or "research").strip().lower(),
        "priority": "prio:normal",
        "labels": labels,
    }


def run_library_scout(cfg: dict | None = None, now: datetime | None = None) -> list[dict[str, Any]]:
    cfg = cfg or load_config()
    current = now or _now_utc()
    summaries: list[dict[str, Any]] = []
    with job_lock(cfg, SCOUT_JOB_NAME) as acquired:
        if not acquired:
            return [{"repo": "*", "suggestions": [], "skipped": "locked"}]
        for github_slug, repo_path in _resolve_repos(cfg):
            repo_cfg = _repo_library_scout_config(cfg, github_slug)
            cadence_days = float(repo_cfg.get("cadence_days", DEFAULT_CADENCE_DAYS) or DEFAULT_CADENCE_DAYS)
            due, reason = is_due(cfg, SCOUT_JOB_NAME, github_slug, cadence_hours=cadence_days * 24.0, now=current)
            if not due:
                summaries.append({"repo": github_slug, "suggestions": [], "skipped": reason})
                continue
            summary = scout_repo(cfg, github_slug, repo_path)
            summaries.append(summary)
            record_run(cfg, SCOUT_JOB_NAME, github_slug, now=current)
    return summaries


def main() -> int:
    for summary in run_library_scout():
        print(
            f"{summary.get('repo')}: suggestions={len(summary.get('suggestions') or [])}, "
            f"status={summary.get('skipped') or 'ran'}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

