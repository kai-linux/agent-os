"""Generate append-only ADRs from merged pull requests.

The curator writes one ADR per qualifying merged PR into ``docs/adrs/`` and
maintains ``docs/adrs/INDEX.md`` as a chronological listing. ADRs are append-
only: reruns dedupe by PR source marker and never rewrite an existing entry.
"""
from __future__ import annotations

import argparse
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from orchestrator.gh_project import gh_json

ADR_DIR = Path("docs/adrs")
ADR_INDEX = ADR_DIR / "INDEX.md"
DEFAULT_RECENT_ADR_LIMIT = 10

_ADR_SOURCE_RE = re.compile(r"<!--\s*adr-source:\s*([^#\s]+/[^#\s]+)#(\d+)\s*-->")
_ADR_TITLE_RE = re.compile(r"^#\s+ADR\s+\d+:\s+(.+?)\s*$", re.MULTILINE)
_ADR_DATE_RE = re.compile(r"^## Date\s*\n+(.+?)\s*$", re.MULTILINE)
_ADR_PR_LINK_RE = re.compile(r"^## PR Link\s*\n+\-\s+\[(.*?)\]\((.*?)\)\s*$", re.MULTILINE)

_ARCHITECTURE_LABELS = {"task:architecture"}
_SKIP_TITLE_PATTERNS = (
    re.compile(r"\bdependabot\b", re.IGNORECASE),
    re.compile(r"\bbump\b", re.IGNORECASE),
    re.compile(r"\bdeps?\b", re.IGNORECASE),
    re.compile(r"\bformat(?:ting)?\b", re.IGNORECASE),
    re.compile(r"\blint(?:ing)?\b", re.IGNORECASE),
    re.compile(r"\bstyle\b", re.IGNORECASE),
    re.compile(r"\btypo\b", re.IGNORECASE),
    re.compile(r"\bcleanup\b", re.IGNORECASE),
)
_ARCHITECTURAL_PATH_PATTERNS = (
    re.compile(r"(^|/)pyproject\.toml$", re.IGNORECASE),
    re.compile(r"(^|/)Dockerfile(\.[^/]+)?$", re.IGNORECASE),
    re.compile(r"(^|/)docker-compose[^/]*\.ya?ml$", re.IGNORECASE),
    re.compile(r"(^|/)(schema|schemas)(/|$)", re.IGNORECASE),
    re.compile(r"(^|/)(migrations?|db/migrate|alembic)(/|$)", re.IGNORECASE),
    re.compile(r"(^|/).*\.sql$", re.IGNORECASE),
    re.compile(r"(^|/)(openapi|swagger)[^/]*\.(json|ya?ml)$", re.IGNORECASE),
    re.compile(r"(^|/)package\.json$", re.IGNORECASE),
    re.compile(r"(^|/)package-lock\.json$", re.IGNORECASE),
    re.compile(r"(^|/)poetry\.lock$", re.IGNORECASE),
    re.compile(r"(^|/)requirements[^/]*\.txt$", re.IGNORECASE),
)

def _normalize_labels(labels: list[dict] | None) -> set[str]:
    names: set[str] = set()
    for label in labels or []:
        name = str((label or {}).get("name") or "").strip().lower()
        if name:
            names.add(name)
    return names

def _slugify(text: str, *, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    return slug[:60].strip("-") or fallback

def _adr_dir(repo_path: Path) -> Path:
    return repo_path / ADR_DIR

def _adr_index_path(repo_path: Path) -> Path:
    return repo_path / ADR_INDEX

def _existing_adr_for_pr(repo_path: Path, github_slug: str, pr_number: int) -> Path | None:
    adrs_dir = _adr_dir(repo_path)
    if not adrs_dir.exists():
        return None
    marker = f"<!-- adr-source: {github_slug}#{pr_number} -->"
    for path in sorted(adrs_dir.glob("[0-9][0-9][0-9][0-9]-*.md")):
        try:
            if marker in path.read_text(encoding="utf-8", errors="replace"):
                return path
        except OSError:
            continue
    return None

def _next_adr_number(repo_path: Path) -> int:
    adrs_dir = _adr_dir(repo_path)
    highest = 0
    for path in adrs_dir.glob("[0-9][0-9][0-9][0-9]-*.md"):
        prefix = path.name.split("-", 1)[0]
        try:
            highest = max(highest, int(prefix))
        except ValueError:
            continue
    return highest + 1

def _fetch_pr_details(github_slug: str, pr_number: int) -> dict:
    pr = gh_json(
        [
            "pr",
            "view",
            str(pr_number),
            "-R",
            github_slug,
            "--json",
            "number,title,body,url,mergedAt,author,labels",
        ]
    ) or {}
    files = gh_json(["api", f"repos/{github_slug}/pulls/{pr_number}/files?per_page=100"]) or []
    pr["files"] = [str(entry.get("filename") or "").strip() for entry in files if entry.get("filename")]
    return pr

def _architectural_hits(files: list[str]) -> list[str]:
    hits: list[str] = []
    for filename in files:
        if any(pattern.search(filename) for pattern in _ARCHITECTURAL_PATH_PATTERNS):
            hits.append(filename)
    return hits

def _is_bot_author(pr: dict) -> bool:
    login = str((pr.get("author") or {}).get("login") or "").strip().lower()
    return login.endswith("[bot]") or login.endswith("-bot") or login == "dependabot"

def _should_skip_pr(pr: dict) -> bool:
    title = str(pr.get("title") or "").strip()
    labels = _normalize_labels(pr.get("labels"))
    if "task:architecture" in labels:
        return False
    if _is_bot_author(pr):
        return True
    return any(pattern.search(title) for pattern in _SKIP_TITLE_PATTERNS)

def _qualifying_reason(pr: dict) -> tuple[str | None, list[str]]:
    labels = _normalize_labels(pr.get("labels"))
    files = [str(item) for item in pr.get("files") or [] if str(item).strip()]
    hits = _architectural_hits(files)
    if _ARCHITECTURE_LABELS & labels:
        return "PR carried `task:architecture`.", hits
    if hits:
        return f"PR touched architectural-surface files: {', '.join(f'`{p}`' for p in hits[:6])}.", hits
    return None, []

def _merged_date(pr: dict) -> str:
    raw = str(pr.get("mergedAt") or "").strip()
    if not raw:
        return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d")
    except ValueError:
        return raw[:10]

def _decision_summary(pr: dict, arch_hits: list[str]) -> str:
    title = str(pr.get("title") or "Merged architectural change").strip()
    if arch_hits:
        return (
            f"The repository accepted the architectural change in PR #{pr.get('number')}: "
            f"{title}. The merged diff updated {', '.join(f'`{path}`' for path in arch_hits[:4])}."
        )
    return f"The repository accepted the architectural change in PR #{pr.get('number')}: {title}."

def _consequence_bullets(files: list[str]) -> list[str]:
    lowered = [path.lower() for path in files]
    bullets: list[str] = []
    if any("dockerfile" in path or "docker-compose" in path for path in lowered):
        bullets.append("Build and runtime environment expectations changed; downstream deploy automation should assume the new container setup.")
    if any(
        "migration" in path
        or "alembic" in path
        or path.endswith(".sql")
        or "/schema" in path
        or path.startswith("schema")
        for path in lowered
    ):
        bullets.append("Persistence or schema contracts changed; future work should treat the merged data shape as the new baseline.")
    if any(
        path.endswith("pyproject.toml")
        or path.endswith("package.json")
        or path.endswith("package-lock.json")
        or path.endswith("poetry.lock")
        or "requirements" in path
        for path in lowered
    ):
        bullets.append("Dependency and packaging expectations changed; follow-on work should align with the merged toolchain and build metadata.")
    if any("openapi" in path or "swagger" in path for path in lowered):
        bullets.append("API contract surface changed; integrations and generated clients may need to track the new schema.")
    bullets.append("This record is append-only; any later reversal or refinement should be captured in a new ADR that supersedes this one.")
    return bullets[:4]

def _render_adr(number: int, github_slug: str, pr: dict, reason: str, arch_hits: list[str]) -> str:
    pr_number = int(pr["number"])
    title = str(pr.get("title") or f"PR #{pr_number}").strip()
    pr_url = str(pr.get("url") or f"https://github.com/{github_slug}/pull/{pr_number}").strip()
    date_text = _merged_date(pr)
    changed_files = [str(path) for path in pr.get("files") or [] if str(path).strip()]

    context_lines = [
        f"PR #{pr_number} merged into `{github_slug}` and was selected for ADR capture.",
        reason,
    ]
    if changed_files:
        preview = ", ".join(f"`{path}`" for path in changed_files[:8])
        extra = f" (+{len(changed_files) - 8} more)" if len(changed_files) > 8 else ""
        context_lines.append(f"Changed files: {preview}{extra}.")

    consequences = _consequence_bullets(changed_files)
    lines = [
        f"# ADR {number:04d}: {title}",
        "",
        f"<!-- adr-source: {github_slug}#{pr_number} -->",
        "",
        "## Context",
        "",
        *context_lines,
        "",
        "## Decision",
        "",
        _decision_summary(pr, arch_hits),
        "",
        "## Consequences",
        "",
    ]
    for bullet in consequences:
        lines.append(f"- {bullet}")
    lines.extend(
        [
            "",
            "## Date",
            "",
            date_text,
            "",
            "## PR Link",
            "",
            f"- [{github_slug}#{pr_number}]({pr_url})",
            "",
        ]
    )
    return "\n".join(lines)

def _parse_adr(path: Path) -> dict | None:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    title_match = _ADR_TITLE_RE.search(content)
    source_match = _ADR_SOURCE_RE.search(content)
    date_match = _ADR_DATE_RE.search(content)
    pr_match = _ADR_PR_LINK_RE.search(content)
    if not title_match:
        return None
    try:
        number = int(path.name.split("-", 1)[0])
    except ValueError:
        return None
    return {
        "path": path,
        "number": number,
        "title": title_match.group(1).strip(),
        "date": date_match.group(1).strip() if date_match else "",
        "source_repo": source_match.group(1).strip() if source_match else "",
        "source_pr": int(source_match.group(2)) if source_match else None,
        "pr_label": pr_match.group(1).strip() if pr_match else "",
        "pr_url": pr_match.group(2).strip() if pr_match else "",
    }

def _write_index(repo_path: Path) -> Path:
    adrs_dir = _adr_dir(repo_path)
    adrs_dir.mkdir(parents=True, exist_ok=True)
    records = []
    for path in sorted(adrs_dir.glob("[0-9][0-9][0-9][0-9]-*.md")):
        parsed = _parse_adr(path)
        if parsed:
            records.append(parsed)
    records.sort(key=lambda item: item["number"])
    lines = [
        "# ADR Index",
        "",
        "| ADR | Date | PR | Title |",
        "| --- | --- | --- | --- |",
    ]
    for record in records:
        rel_path = record["path"].name
        pr_cell = f"[{record['pr_label']}]({record['pr_url']})" if record["pr_url"] else ""
        lines.append(
            f"| [{record['number']:04d}]({rel_path}) | {record['date']} | {pr_cell} | {record['title']} |"
        )
    if len(lines) == 4:
        lines.append("| (none) |  |  | No ADRs recorded yet. |")
    index_path = _adr_index_path(repo_path)
    index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return index_path

def curate_pr(
    cfg: dict,
    github_slug: str,
    repo_path: Path,
    *,
    pr_number: int,
    pr: dict | None = None,
) -> Path | None:
    """Write an ADR for one merged PR when it qualifies."""

    _ = cfg  # Reserved for future per-repo overrides.

    _ = cfg

    repo_path = Path(repo_path).expanduser()
    existing = _existing_adr_for_pr(repo_path, github_slug, pr_number)
    if existing:
        _write_index(repo_path)
        return existing

    payload = dict(pr or {})
    if not payload.get("labels") or not payload.get("files") or not payload.get("mergedAt"):
        payload = _fetch_pr_details(github_slug, pr_number)
    if not payload or not payload.get("number"):
        return None
    if _should_skip_pr(payload):
        return None

    reason, arch_hits = _qualifying_reason(payload)
    if not reason:
        return None

    adrs_dir = _adr_dir(repo_path)
    adrs_dir.mkdir(parents=True, exist_ok=True)
    number = _next_adr_number(repo_path)
    slug = _slugify(str(payload.get("title") or f"pr-{pr_number}"), fallback=f"pr-{pr_number}")
    path = adrs_dir / f"{number:04d}-{slug}.md"
    path.write_text(_render_adr(number, github_slug, payload, reason, arch_hits), encoding="utf-8")
    _write_index(repo_path)
    return path

def _list_recent_merged_prs(github_slug: str, *, days: int, limit: int) -> list[dict]:
    prs = gh_json(
        [
            "pr",
            "list",
            "-R",
            github_slug,
            "--state",
            "merged",
            "--limit",
            str(limit),
            "--json",
            "number,mergedAt",
        ]
    ) or []
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=max(0, days))
    recent: list[dict] = []
    for pr in prs:
        raw = str(pr.get("mergedAt") or "").strip()
        if not raw:
            continue
        try:
            if raw.endswith("Z"):
                raw = raw[:-1] + "+00:00"
            merged_at = datetime.fromisoformat(raw)
        except ValueError:
            continue
        if merged_at.tzinfo is None:
            merged_at = merged_at.replace(tzinfo=timezone.utc)
        if merged_at.astimezone(timezone.utc) >= cutoff:
            recent.append(pr)
    recent.sort(key=lambda item: str(item.get("mergedAt") or ""))
    return recent

def _resolve_repos(cfg: dict) -> list[tuple[str, Path]]:
    repos: list[tuple[str, Path]] = []
    for project_cfg in (cfg.get("github_projects") or {}).values():
        if not isinstance(project_cfg, dict):
            continue
        for repo_cfg in project_cfg.get("repos", []) or []:
            github_slug = str(repo_cfg.get("github_repo") or "").strip()
            local_repo = str(repo_cfg.get("local_repo") or repo_cfg.get("path") or "").strip()
            if github_slug and local_repo:
                repos.append((github_slug, Path(local_repo).expanduser()))
    return repos

def curate_recent_prs(
    cfg: dict,
    github_slug: str,
    repo_path: Path,
    *,
    days: int = 1,
    limit: int = 30,
) -> list[Path]:
    created: list[Path] = []
    for pr in _list_recent_merged_prs(github_slug, days=days, limit=limit):
        if _existing_adr_for_pr(repo_path, github_slug, int(pr["number"])):
            continue
        path = curate_pr(cfg, github_slug, repo_path, pr_number=int(pr["number"]), pr=pr)
        if path is not None:
            created.append(path)
    return created

def read_recent_adrs(
    repo_path: Path,
    *,
    limit: int = DEFAULT_RECENT_ADR_LIMIT,
    max_chars: int = 2200,
) -> str:
    adrs_dir = _adr_dir(Path(repo_path).expanduser())
    records = []
    for path in adrs_dir.glob("[0-9][0-9][0-9][0-9]-*.md"):
        parsed = _parse_adr(path)
        if parsed:
            records.append(parsed)
    if not records:
        return "(no architectural decision records)"
    records.sort(key=lambda item: item["number"], reverse=True)
    lines = []
    for record in records[: max(1, limit)]:
        pr_suffix = f" ({record['pr_label']})" if record.get("pr_label") else ""
        lines.append(f"- {record['date']} — ADR {record['number']:04d}: {record['title']}{pr_suffix}")
    text = "\n".join(lines)
    return text[:max_chars]

def main(argv: list[str] | None = None) -> int:
    from orchestrator.paths import load_config

    parser = argparse.ArgumentParser(description="Curate ADRs from merged PRs.")
    parser.add_argument("--repo", help="GitHub slug to process (default: all configured repos).")
    parser.add_argument("--days", type=int, default=1, help="Scan merged PRs from the last N days.")
    parser.add_argument("--limit", type=int, default=30, help="Max merged PRs to inspect per repo.")
    args = parser.parse_args(argv)

    cfg = load_config()
    repos = _resolve_repos(cfg)
    if args.repo:
        repos = [(slug, path) for slug, path in repos if slug == args.repo]

    for github_slug, repo_path in repos:
        if not repo_path.exists():
            continue
        created = curate_recent_prs(cfg, github_slug, repo_path, days=args.days, limit=args.limit)
        print(f"{github_slug}: {len(created)} ADR(s) curated")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
