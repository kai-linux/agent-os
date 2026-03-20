"""Strategic planner (Level 2).

Reads product context (README, CODEBASE.md, STRATEGY.md, last sprint
retrospective, recent PRs and closed issues) from each repository, generates a
prioritized 5-task sprint plan using Claude Sonnet, posts it to Telegram for
human approval, and conditionally creates GitHub issues upon confirmation.

On approval, created issues are automatically set to Status=Ready on the
project board, triggering immediate dispatch to agents. STRATEGY.md is updated
with the new sprint plan and retrospective findings. One tap on Telegram
starts an entire sprint.

Cron can invoke this frequently; per-repo cadence and dormancy are enforced in
code. Approval gate is mandatory — no issues are created without explicit human
confirmation via Telegram reply. If no response within 24 hours, the plan is
skipped until the repo is due again.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from html import unescape
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from orchestrator.paths import load_config, runtime_paths
from orchestrator.agent_scorer import load_recent_metrics
from orchestrator.gh_project import query_project, set_item_status, edit_issue_labels, ensure_labels
from orchestrator.queue import (
    planner_reply_markup,
    save_telegram_action,
    load_telegram_action,
    telegram_action_expired,
    process_telegram_callbacks,
)
from orchestrator.scheduler_state import is_due, record_run, job_lock
from orchestrator.backlog_groomer import groom_repo, _repo_groomer_cadence_days
from orchestrator.repo_context import read_north_star
from orchestrator.trust import is_trusted

DEFAULT_PLAN_SIZE = 5
DEFAULT_SPRINT_CADENCE_DAYS = 7
ANALYSIS_MODEL = "opus"
FOCUS_AREA_MODEL = "haiku"
METRICS_WINDOW_DAYS = 30
MIN_SPRINTS_FOR_FOCUS = 3
FOCUS_AREA_MARKER = "<!-- auto-focus-areas -->"
SIMILARITY_THRESHOLD = 0.75
CROSS_REPO_SUMMARY_MAX_CHARS = 1500  # per-repo summary cap for cross-repo context
APPROVAL_TIMEOUT_HOURS = 24
APPROVAL_KEYWORDS = {"yes", "approve", "approved", "ok", "go", "lgtm"}
REJECTION_KEYWORDS = {"no", "reject", "skip", "cancel", "nope"}
RESEARCH_ARTIFACT_DEFAULT = "PLANNING_RESEARCH.md"
SIGNALS_ARTIFACT_DEFAULT = "PLANNING_SIGNALS.md"
RESEARCH_ALLOWED_KINDS = {
    "official_docs",
    "competitor",
    "product_surface",
    "repo_reference",
}
SIGNAL_INPUT_TYPES = {"analytics", "user_feedback", "market_signal"}
RESEARCH_MAX_SOURCES = 4
RESEARCH_MAX_SOURCE_CHARS = 4000
RESEARCH_CONTEXT_MAX_CHARS = 4000
RESEARCH_FETCH_TIMEOUT_SECONDS = 20
SIGNALS_MAX_INPUTS = 6
SIGNALS_MAX_SOURCE_CHARS = 4000
SIGNALS_CONTEXT_MAX_CHARS = 5000


def _format_cadence(days: float) -> str:
    if days <= 0:
        return "dormant"
    hours = days * 24.0
    if hours < 1:
        minutes = max(1, round(hours * 60))
        return f"every {minutes}m"
    if hours < 24:
        return f"every {hours:g}h"
    return f"every {days:g}d"


def _approval_timeout_hours(cadence_days: float) -> float:
    """Return an approval window shorter than the next expected planning cycle."""
    cadence_hours = max(0.0, cadence_days * 24.0)
    if cadence_hours <= 0:
        return APPROVAL_TIMEOUT_HOURS
    return min(APPROVAL_TIMEOUT_HOURS, max(10 / 60, cadence_hours * 0.8))


def _format_duration_hours(hours: float) -> str:
    if hours < 1:
        minutes = max(1, round(hours * 60))
        return f"{minutes}m"
    if float(hours).is_integer():
        return f"{int(hours)}h"
    return f"{hours:g}h"


# ---------------------------------------------------------------------------
# Data gathering helpers
# ---------------------------------------------------------------------------

def _gh(cmd: list[str], *, check: bool = False) -> str:
    result = subprocess.run(["gh", *cmd], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(cmd[:3])}... exit {result.returncode}: {result.stderr.strip()}")
    return result.stdout.strip()


def _read_readme_goal(repo_path: Path) -> str:
    """Extract ## Goal section from README.md."""
    readme = repo_path / "README.md"
    if not readme.exists():
        return "(no README.md found)"
    content = readme.read_text(encoding="utf-8", errors="replace")
    # Try ## Goal first, then fall back to description after first heading
    match = re.search(
        r"##\s+Goal\s*\n(.*?)(?=\n##\s|\Z)",
        content, re.DOTALL | re.IGNORECASE,
    )
    if match:
        return match.group(1).strip()[:2000]
    # Fallback: first 500 chars of README
    return content[:500].strip()


def _read_planning_principles(repo_path: Path) -> str:
    """Read PLANNING_PRINCIPLES.md when present, else fall back to defaults."""
    principles = repo_path / "PLANNING_PRINCIPLES.md"
    if principles.exists():
        return principles.read_text(encoding="utf-8", errors="replace").strip()
    return (
        "Prefer backlog items that increase autonomy, evidence-driven planning, "
        "control-plane quality, or unblock other important work. Avoid stale, "
        "externally blocked, or vague issues."
    )


def _git_log(repo_path: Path, n: int = 30) -> str:
    """Return last N git commit summaries."""
    result = subprocess.run(
        ["git", "-C", str(repo_path), "log", f"-{n}",
         "--oneline", "--no-decorate"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return "(git log unavailable)"
    return result.stdout.strip() or "(no commits)"


def _issue_counts(repo: str) -> dict[str, int]:
    """Return counts of open, closed, and blocked issues."""
    counts = {"open": 0, "closed": 0, "blocked": 0}

    # Open issues
    raw = _gh(["issue", "list", "--repo", repo, "--state", "open",
               "--json", "number", "--limit", "500"])
    if raw:
        try:
            counts["open"] = len(json.loads(raw))
        except json.JSONDecodeError:
            pass

    # Closed issues (last 30 days)
    raw = _gh(["issue", "list", "--repo", repo, "--state", "closed",
               "--json", "number", "--limit", "500"])
    if raw:
        try:
            counts["closed"] = len(json.loads(raw))
        except json.JSONDecodeError:
            pass

    # Blocked issues
    raw = _gh(["issue", "list", "--repo", repo, "--state", "open",
               "--label", "blocked", "--json", "number", "--limit", "500"])
    if raw:
        try:
            counts["blocked"] = len(json.loads(raw))
        except json.JSONDecodeError:
            pass

    return counts


def _open_issues_summary(repo: str, cfg: dict) -> str:
    """Return formatted list of genuinely active issues from trusted authors."""
    raw = _gh(["issue", "list", "--repo", repo, "--state", "open",
               "--json", "number,title,labels,author", "--limit", "50"])
    if not raw:
        return "(no issues already in progress)"
    try:
        issues = json.loads(raw)
    except json.JSONDecodeError:
        return "(failed to parse)"
    if not issues:
        return "(no issues already in progress)"
    lines = []
    for i in issues[:50]:
        author = (i.get("author") or {}).get("login", "")
        if not is_trusted(author, cfg):
            continue
        label_names = {l.get("name", "").lower() for l in i.get("labels", [])}
        if not (label_names & {"in-progress", "agent-dispatched", "ready"}):
            continue
        labels = ", ".join(l.get("name", "") for l in i.get("labels", []))
        lbl = f" [{labels}]" if labels else ""
        lines.append(f"- #{i.get('number')}: {i.get('title')}{lbl}")
    return "\n".join(lines) if lines else "(no issues already in progress)"


def _backlog_issues(repo: str, cfg: dict) -> list[dict]:
    """Return open issues from trusted authors that are NOT active/blocked/done."""
    raw = _gh(["issue", "list", "--repo", repo, "--state", "open",
               "--json", "number,title,body,labels,createdAt,author",
               "--limit", "50"])
    if not raw:
        return []
    try:
        issues = json.loads(raw)
    except json.JSONDecodeError:
        return []
    backlog = []
    for i in issues:
        # Only ingest issues from trusted authors (prompt injection defense)
        author = (i.get("author") or {}).get("login", "")
        if not is_trusted(author, cfg):
            continue
        label_names = {l.get("name", "").lower() for l in i.get("labels", [])}
        # Skip issues already dispatched or in progress
        if label_names & {"in-progress", "agent-dispatched", "ready", "done", "blocked"}:
            continue
        backlog.append(i)
    return backlog


def _has_open_agent_pr(repo: str) -> bool:
    raw = _gh(["pr", "list", "--repo", repo, "--state", "open",
               "--json", "number,headRefName,isCrossRepository", "--limit", "100"])
    if not raw:
        return False
    try:
        prs = json.loads(raw)
    except json.JSONDecodeError:
        return False
    for pr in prs:
        if pr.get("isCrossRepository"):
            continue
        if str(pr.get("headRefName", "")).startswith("agent/"):
            return True
    return False


def _has_active_sprint_work(repo: str, cfg: dict) -> tuple[bool, str]:
    """Return whether the repo still has active sprint execution underway."""
    raw = _gh(["issue", "list", "--repo", repo, "--state", "open",
               "--json", "number,labels,author", "--limit", "100"])
    if raw:
        try:
            issues = json.loads(raw)
        except json.JSONDecodeError:
            issues = []
        for issue in issues:
            author = (issue.get("author") or {}).get("login", "")
            if not is_trusted(author, cfg):
                continue
            label_names = {l.get("name", "").lower() for l in issue.get("labels", [])}
            if "blocked" in label_names:
                continue
            if label_names & {"ready", "in-progress", "agent-dispatched"}:
                return True, f"active issue #{issue.get('number')}"
    if _has_open_agent_pr(repo):
        return True, "open agent PR"
    return False, "no active sprint work"


def _maybe_refresh_backlog_for_early_cycle(cfg: dict, github_slug: str, repo_path: Path) -> tuple[bool, str]:
    """Refresh backlog and allow an early planner cycle when the sprint is empty."""
    backlog = _backlog_issues(github_slug, cfg)
    if backlog:
        return True, f"early-complete with existing backlog ({len(backlog)} candidates)"

    print(f"  Early sprint completion override: refreshing backlog for {github_slug} before planning.")
    result = groom_repo(cfg, github_slug, repo_path)
    if result.get("status") != "error":
        record_run(cfg, "backlog_groomer", github_slug)
    return True, f"early-complete with groomer refresh ({result.get('status', 'unknown')})"


def _format_backlog_for_prompt(issues: list[dict]) -> str:
    """Format backlog issues for the planner prompt."""
    if not issues:
        return "(no backlog issues)"
    lines = []
    for i in issues:
        labels = ", ".join(l.get("name", "") for l in i.get("labels", []))
        lbl = f" [{labels}]" if labels else ""
        body_preview = (i.get("body") or "")[:200].replace("\n", " ")
        lines.append(f"- #{i['number']}: {i['title']}{lbl}\n  {body_preview}")
    return "\n".join(lines)


def _read_strategy(repo_path: Path) -> str:
    """Read STRATEGY.md from a repo, returning content or empty string."""
    strategy_md = repo_path / "STRATEGY.md"
    if not strategy_md.exists():
        return ""
    return strategy_md.read_text(encoding="utf-8", errors="replace").strip()


def _load_strategy_map(all_repos: list[tuple[str, Path]]) -> dict[str, str]:
    """Read STRATEGY.md for all configured repos once."""
    return {slug: _read_strategy(path) for slug, path in all_repos}


def _summarize_strategy(content: str) -> str:
    """Extract a compact summary from a STRATEGY.md for cross-repo context.

    Pulls Product Vision, Current Focus Areas, and the most recent sprint entry
    to keep prompt size bounded.
    """
    if not content:
        return "(no strategy yet)"
    parts = []

    # Product Vision
    match = re.search(
        r"## Product Vision\s*\n(.*?)(?=\n## |\Z)",
        content, re.DOTALL,
    )
    if match:
        vision = match.group(1).strip()[:500]
        if vision:
            parts.append(f"Vision: {vision}")

    # Current Focus Areas
    match = re.search(
        r"## Current Focus Areas\s*\n(.*?)(?=\n## |\Z)",
        content, re.DOTALL,
    )
    if match:
        areas = match.group(1).replace(FOCUS_AREA_MARKER, "").strip()[:500]
        if areas and areas != "(Updated each sprint with the key themes being pursued.)":
            parts.append(f"Focus areas:\n{areas}")

    # Most recent sprint entry
    entries = _extract_sprint_entries(content)
    if entries:
        latest = entries[0][:500]
        parts.append(f"Latest sprint:\n{latest}")

    summary = "\n\n".join(parts) if parts else "(no strategy yet)"
    return summary[:CROSS_REPO_SUMMARY_MAX_CHARS]


def _strategy_dependencies(
    all_repo_slugs: list[str],
    strategy_map: dict[str, str],
) -> dict[str, set[str]]:
    """Infer explicit cross-repo dependencies from strategy content.

    Only phrases near dependency keywords or dependency-focused headings count,
    which keeps matches conservative and reduces false positives.
    """
    dependencies: dict[str, set[str]] = {slug: set() for slug in all_repo_slugs}
    aliases = {
        slug: {slug.lower(), slug.rsplit("/", 1)[-1].lower()}
        for slug in all_repo_slugs
    }
    heading_re = re.compile(
        r"##\s+(?:Cross-Repo\s+)?Dependencies\s*\n(.*?)(?=\n## |\Z)",
        re.DOTALL | re.IGNORECASE,
    )
    dependency_phrase_re = re.compile(
        r"(depends on|blocked by|requires|after|prerequisite)",
        re.IGNORECASE,
    )

    for slug in all_repo_slugs:
        content = strategy_map.get(slug, "")
        if not content:
            continue

        candidate_lines: list[str] = []
        heading_match = heading_re.search(content)
        if heading_match:
            candidate_lines.extend(heading_match.group(1).splitlines())

        for line in content.splitlines():
            if dependency_phrase_re.search(line):
                candidate_lines.append(line)

        if not candidate_lines:
            continue

        current_aliases = aliases[slug]
        for other_slug in all_repo_slugs:
            if other_slug == slug:
                continue
            other_aliases = aliases[other_slug]
            for raw_line in candidate_lines:
                line = raw_line.lower()
                if not any(alias in line for alias in other_aliases):
                    continue

                if line.lstrip("-* ").startswith((
                    "depends on", "blocked by", "requires", "after", "prerequisite",
                )):
                    dependencies[slug].add(other_slug)
                    break

                matched = False
                for current_alias in current_aliases:
                    for other_alias in other_aliases:
                        if re.search(
                            rf"{re.escape(current_alias)}.*?(depends on|blocked by|requires|after|prerequisite).*?{re.escape(other_alias)}",
                            line,
                            re.IGNORECASE,
                        ):
                            dependencies[slug].add(other_slug)
                            matched = True
                            break
                    if matched:
                        break
                if matched:
                    break

    return dependencies


def _order_repos_by_dependencies(
    all_repos: list[tuple[str, Path]],
    dependencies: dict[str, set[str]],
) -> list[tuple[str, Path]]:
    """Plan prerequisite repos first when dependencies are known."""
    repo_map = dict(all_repos)
    indegree = {slug: 0 for slug, _ in all_repos}
    dependents: dict[str, set[str]] = defaultdict(set)

    for slug, deps in dependencies.items():
        for dep in deps:
            if dep not in indegree or dep == slug:
                continue
            indegree[slug] += 1
            dependents[dep].add(slug)

    ready = deque(sorted(slug for slug, degree in indegree.items() if degree == 0))
    ordered_slugs: list[str] = []

    while ready:
        slug = ready.popleft()
        ordered_slugs.append(slug)
        for dependent in sorted(dependents.get(slug, set())):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)

    for slug, _ in all_repos:
        if slug not in ordered_slugs:
            ordered_slugs.append(slug)

    return [(slug, repo_map[slug]) for slug in ordered_slugs]


def _gather_cross_repo_context(
    all_repos: list[tuple[str, Path]],
    current_slug: str,
    strategy_map: dict[str, str] | None = None,
    dependencies: dict[str, set[str]] | None = None,
) -> str:
    """Build cross-repo context by reading STRATEGY.md from all other repos.

    Returns a formatted string with summarized strategies from sibling repos,
    suitable for injection into the planning prompt.
    """
    strategy_map = strategy_map or _load_strategy_map(all_repos)
    dependencies = dependencies or _strategy_dependencies(
        [slug for slug, _ in all_repos],
        strategy_map,
    )
    prerequisite_repos = sorted(dependencies.get(current_slug, set()))
    dependent_repos = sorted(
        slug for slug, deps in dependencies.items()
        if current_slug in deps and slug != current_slug
    )

    sections = []
    relation_lines = []
    if prerequisite_repos:
        relation_lines.append(
            "Prerequisites for this repo: " + ", ".join(prerequisite_repos)
        )
    if dependent_repos:
        relation_lines.append(
            "Repos waiting on this repo: " + ", ".join(dependent_repos)
        )
    if relation_lines:
        sections.append("\n".join(relation_lines))

    for slug, path in all_repos:
        if slug == current_slug:
            continue
        strategy = strategy_map.get(slug)
        if strategy is None:
            strategy = _read_strategy(path)
        summary = _summarize_strategy(strategy)
        relation = []
        if slug in prerequisite_repos:
            relation.append("prerequisite for current repo")
        if slug in dependent_repos:
            relation.append("depends on current repo")
        relation_text = f" ({'; '.join(relation)})" if relation else ""
        sections.append(f"### {slug}{relation_text}\n{summary}")
    if not sections:
        return ""
    return "\n\n".join(sections)


def _read_codebase_md(repo_path: Path) -> str:
    """Read CODEBASE.md for context injection, truncated to 3000 chars."""
    codebase_md = repo_path / "CODEBASE.md"
    if not codebase_md.exists():
        return "(no CODEBASE.md)"
    content = codebase_md.read_text(encoding="utf-8", errors="replace").strip()
    return content[:3000] if content else "(empty CODEBASE.md)"


def _repo_research_config(cfg: dict, github_slug: str) -> dict:
    """Return merged planning research config for a repo."""
    research_cfg = dict(cfg.get("planning_research") or {})
    for project_cfg in cfg.get("github_projects", {}).values():
        if not isinstance(project_cfg, dict):
            continue
        for repo_cfg in project_cfg.get("repos", []):
            if repo_cfg.get("github_repo") != github_slug:
                continue
            override = repo_cfg.get("planning_research")
            if isinstance(override, dict):
                merged = dict(research_cfg)
                merged.update(override)
                research_cfg = merged
            return research_cfg
    return research_cfg


def _normalize_research_domain(domain: str) -> str:
    return domain.strip().lower().lstrip(".")


def _domain_allowed(host: str, allowed_domains: list[str]) -> bool:
    if not allowed_domains:
        return True
    normalized_host = _normalize_research_domain(host)
    for domain in allowed_domains:
        normalized_domain = _normalize_research_domain(domain)
        if normalized_host == normalized_domain or normalized_host.endswith(f".{normalized_domain}"):
            return True
    return False


def _clean_research_text(raw: str) -> str:
    text = raw or ""
    text = re.sub(r"(?is)<script.*?>.*?</script>", " ", text)
    text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = unescape(text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"\r", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def _artifact_path(repo_path: Path, artifact_name: str) -> Path:
    artifact = (repo_path / artifact_name).resolve()
    repo_root = repo_path.resolve()
    if repo_root == artifact or repo_root not in artifact.parents:
        raise ValueError(f"Research artifact must stay inside repo: {artifact_name}")
    return artifact


def _parse_signal_timestamp(raw: str | None) -> datetime | None:
    value = str(raw or "").strip()
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _allowed_research_file(repo_path: Path, raw_path: str) -> Path | None:
    if not raw_path or Path(raw_path).is_absolute():
        return None
    resolved = (repo_path / raw_path).resolve()
    repo_root = repo_path.resolve()
    allowed_roots = {repo_root, repo_root.parent}
    if not any(root == resolved or root in resolved.parents for root in allowed_roots):
        return None
    return resolved


def _read_research_source(repo_path: Path, source: dict, research_cfg: dict) -> tuple[str | None, str | None]:
    source_type = str(source.get("type", "")).strip().lower()
    max_chars = int(research_cfg.get("max_source_chars", RESEARCH_MAX_SOURCE_CHARS))
    if source_type == "web":
        url = str(source.get("url", "")).strip()
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc:
            return None, "web research sources must use https URLs"
        allowed_domains = [
            d for d in research_cfg.get("allowed_domains", [])
            if isinstance(d, str) and d.strip()
        ]
        if not _domain_allowed(parsed.hostname or "", allowed_domains):
            return None, f"domain not allowed: {parsed.hostname or '?'}"
        result = subprocess.run(
            ["curl", "-LfsS", "--max-time", str(RESEARCH_FETCH_TIMEOUT_SECONDS), url],
            capture_output=True,
            text=True,
            timeout=RESEARCH_FETCH_TIMEOUT_SECONDS + 5,
        )
        if result.returncode != 0:
            return None, f"fetch failed: {result.stderr.strip()[:200]}"
        return _clean_research_text(result.stdout)[:max_chars], None

    if source_type == "file":
        raw_path = str(source.get("path", "")).strip()
        file_path = _allowed_research_file(repo_path, raw_path)
        if not file_path:
            return None, f"file path not allowed: {raw_path}"
        if not file_path.exists():
            return None, f"file not found: {raw_path}"
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return None, f"failed to read file: {e}"
        return content[:max_chars].strip(), None

    return None, f"unsupported source type: {source_type or '?'}"


_RESEARCH_SUMMARY_PROMPT = """\
You are preparing tightly bounded planning research for a software sprint planner.

Summarize the source below without speculation. Use only the supplied text.
Focus on what changed, what capabilities exist, and what product or execution
implications matter for sprint selection this week.

Source label: {name}
Source kind: {kind}
Source location: {location}

Source text:
{source_text}

Return ONLY JSON with this schema:
{{
  "summary": "2-4 sentence factual summary",
  "planning_implications": ["bullet", "bullet", "bullet"]
}}
"""


def _summarize_research_source(source: dict, content: str) -> tuple[str, list[str]]:
    prompt = _RESEARCH_SUMMARY_PROMPT.format(
        name=source.get("name", "Unnamed source"),
        kind=source.get("kind", "repo_reference"),
        location=source.get("url") or source.get("path") or "(unknown)",
        source_text=content[:RESEARCH_MAX_SOURCE_CHARS],
    )
    try:
        raw = _call_haiku(prompt)
        text = raw.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        data = json.loads(text)
        summary = str(data.get("summary", "")).strip()
        implications = [
            str(item).strip()
            for item in data.get("planning_implications", [])
            if str(item).strip()
        ]
        if summary:
            return summary[:700], implications[:5]
    except Exception as e:
        print(f"  Research summarization failed for {source.get('name', '?')}: {e}")

    fallback_summary = content[:700].replace("\n", " ").strip()
    fallback_summary = re.sub(r"\s{2,}", " ", fallback_summary)
    if len(content) > 700:
        fallback_summary += "..."
    return fallback_summary or "(no content)", ["Treat as raw evidence; summarization fallback was used."]


def _write_research_artifact(repo_path: Path, artifact_path: Path, sections: list[dict], refresh_hours: float):
    generated = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Planning Research",
        "",
        f"- Generated: {generated}",
        f"- Refresh after: {refresh_hours:g}h",
        f"- Scope: bounded pre-planning research for sprint selection",
        "",
    ]
    if not sections:
        lines.extend([
            "## Findings",
            "",
            "(no usable research findings were gathered)",
            "",
        ])
    for section in sections:
        lines.extend([
            f"## {section['name']}",
            "",
            f"- Kind: {section['kind']}",
            f"- Location: {section['location']}",
            "",
            "### Summary",
            "",
            section["summary"],
            "",
            "### Planning Implications",
            "",
        ])
        for implication in section["planning_implications"]:
            lines.append(f"- {implication}")
        lines.append("")
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    updated = "\n".join(lines).rstrip() + "\n"
    artifact_path.write_text(updated, encoding="utf-8")
    _commit_repo_markdown_with_retry(
        repo_path,
        artifact_path.relative_to(repo_path),
        updated,
        f"chore: refresh {artifact_path.name}",
        f"  {artifact_path.name} updated and pushed for {repo_path.name}",
    )


def _planning_research_context(cfg: dict, github_slug: str, repo_path: Path) -> str:
    """Return bounded research context, refreshing the artifact when stale."""
    research_cfg = _repo_research_config(cfg, github_slug)
    if not research_cfg.get("enabled"):
        return "(planning research disabled)"

    sources = research_cfg.get("sources", [])
    if not isinstance(sources, list) or not sources:
        return "(planning research enabled but no sources configured)"

    artifact_name = str(research_cfg.get("artifact_file", RESEARCH_ARTIFACT_DEFAULT)).strip() or RESEARCH_ARTIFACT_DEFAULT
    refresh_hours = float(research_cfg.get("max_age_hours", 72))
    max_sources = max(1, min(int(research_cfg.get("max_sources", RESEARCH_MAX_SOURCES)), RESEARCH_MAX_SOURCES))
    artifact_path = _artifact_path(repo_path, artifact_name)

    if artifact_path.exists():
        age_seconds = time.time() - artifact_path.stat().st_mtime
        if age_seconds <= refresh_hours * 3600:
            content = artifact_path.read_text(encoding="utf-8", errors="replace").strip()
            return content[:RESEARCH_CONTEXT_MAX_CHARS] if content else "(empty planning research artifact)"

    sections = []
    for source in sources[:max_sources]:
        if not isinstance(source, dict):
            continue
        kind = str(source.get("kind", "")).strip().lower()
        if kind not in RESEARCH_ALLOWED_KINDS:
            print(f"  Skipping research source with unsupported kind: {kind or '?'}")
            continue
        content, error = _read_research_source(repo_path, source, research_cfg)
        if error:
            print(f"  Skipping research source {source.get('name', '?')}: {error}")
            continue
        summary, implications = _summarize_research_source(source, content or "")
        sections.append({
            "name": str(source.get("name", "Unnamed source")).strip() or "Unnamed source",
            "kind": kind,
            "location": str(source.get("url") or source.get("path") or "(unknown)").strip(),
            "summary": summary,
            "planning_implications": implications or ["No direct sprint implication captured."],
        })

    _write_research_artifact(repo_path, artifact_path, sections, refresh_hours)
    content = artifact_path.read_text(encoding="utf-8", errors="replace").strip()
    return content[:RESEARCH_CONTEXT_MAX_CHARS] if content else "(empty planning research artifact)"


def _repo_signals_config(cfg: dict, github_slug: str) -> dict:
    """Return merged planning signals config for a repo."""
    signals_cfg = dict(cfg.get("planning_signals") or {})
    for project_cfg in cfg.get("github_projects", {}).values():
        if not isinstance(project_cfg, dict):
            continue
        for repo_cfg in project_cfg.get("repos", []):
            if repo_cfg.get("github_repo") != github_slug:
                continue
            override = repo_cfg.get("planning_signals")
            if isinstance(override, dict):
                merged = dict(signals_cfg)
                merged.update(override)
                signals_cfg = merged
            return signals_cfg
    return signals_cfg


_SIGNAL_SUMMARY_PROMPT = """\
You are normalizing bounded planning signals for a software sprint planner.

Use only the provided source text and the explicit metadata. Do not invent facts.
Extract measurable evidence first, then concise planning implications.

Input type: {input_type}
Source name: {name}
Source location: {location}
Observed at: {observed_at}
Trust note: {trust_note}
Privacy note: {privacy_note}

Source text:
{source_text}

Return ONLY JSON with this schema:
{{
  "summary": "2-4 sentence factual summary",
  "key_metrics": ["metric with number or bounded qualitative count", "metric"],
  "planning_implications": ["bullet", "bullet", "bullet"]
}}
"""


def _summarize_signal_input(signal: dict, content: str) -> tuple[str, list[str], list[str]]:
    prompt = _SIGNAL_SUMMARY_PROMPT.format(
        input_type=signal.get("input_type", "unknown"),
        name=signal.get("name", "Unnamed input"),
        location=signal.get("url") or signal.get("path") or "(unknown)",
        observed_at=signal.get("observed_at") or "unspecified",
        trust_note=signal.get("trust_note") or "not specified",
        privacy_note=signal.get("privacy_note") or "public-safe source expected",
        source_text=content[:SIGNALS_MAX_SOURCE_CHARS],
    )
    try:
        raw = _call_haiku(prompt)
        text = raw.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        data = json.loads(text)
        summary = str(data.get("summary", "")).strip()
        metrics = [
            str(item).strip()
            for item in data.get("key_metrics", [])
            if str(item).strip()
        ]
        implications = [
            str(item).strip()
            for item in data.get("planning_implications", [])
            if str(item).strip()
        ]
        if summary:
            return summary[:700], metrics[:5], implications[:5]
    except Exception as e:
        print(f"  Signal summarization failed for {signal.get('name', '?')}: {e}")

    fallback_summary = content[:700].replace("\n", " ").strip()
    fallback_summary = re.sub(r"\s{2,}", " ", fallback_summary)
    if len(content) > 700:
        fallback_summary += "..."
    return (
        fallback_summary or "(no content)",
        ["No normalized metric extracted; raw evidence fallback was used."],
        ["Treat as raw evidence; normalization fallback was used."],
    )


def _format_freshness_hours(observed_at: datetime | None) -> str:
    if not observed_at:
        return "unknown"
    age_hours = max(0.0, (datetime.now(tz=timezone.utc) - observed_at).total_seconds() / 3600.0)
    if age_hours < 1:
        return "<1h old"
    if age_hours < 24:
        return f"{round(age_hours)}h old"
    return f"{round(age_hours / 24, 1):g}d old"


def _write_signals_artifact(repo_path: Path, artifact_path: Path, sections: list[dict], refresh_hours: float):
    generated = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Planning Signals",
        "",
        f"- Generated: {generated}",
        f"- Refresh after: {refresh_hours:g}h",
        "- Scope: bounded analytics, user feedback, and market signals for sprint selection",
        "",
    ]
    if not sections:
        lines.extend([
            "## Findings",
            "",
            "(no usable planning signals were gathered)",
            "",
        ])
    for section in sections:
        lines.extend([
            f"## {section['name']}",
            "",
            f"- Input Type: {section['input_type']}",
            f"- Source Location: {section['location']}",
            f"- Observed At: {section['observed_at']}",
            f"- Freshness: {section['freshness']}",
            f"- Provenance: {section['provenance']}",
            f"- Trust Boundary: {section['trust_note']}",
            f"- Privacy Boundary: {section['privacy_note']}",
            "",
            "### Signal Summary",
            "",
            section["summary"],
            "",
            "### Key Metrics",
            "",
        ])
        for metric in section["key_metrics"]:
            lines.append(f"- {metric}")
        lines.extend([
            "",
            "### Planning Implications",
            "",
        ])
        for implication in section["planning_implications"]:
            lines.append(f"- {implication}")
        lines.append("")
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    updated = "\n".join(lines).rstrip() + "\n"
    artifact_path.write_text(updated, encoding="utf-8")
    _commit_repo_markdown_with_retry(
        repo_path,
        artifact_path.relative_to(repo_path),
        updated,
        f"chore: refresh {artifact_path.name}",
        f"  {artifact_path.name} updated and pushed for {repo_path.name}",
    )


def _planning_signals_context(cfg: dict, github_slug: str, repo_path: Path) -> str:
    """Return normalized planning signals, refreshing the artifact when stale."""
    signals_cfg = _repo_signals_config(cfg, github_slug)
    if not signals_cfg.get("enabled"):
        return "(planning signals disabled)"

    inputs = signals_cfg.get("inputs", [])
    if not isinstance(inputs, list) or not inputs:
        return "(planning signals enabled but no inputs configured)"

    artifact_name = str(signals_cfg.get("artifact_file", SIGNALS_ARTIFACT_DEFAULT)).strip() or SIGNALS_ARTIFACT_DEFAULT
    refresh_hours = float(signals_cfg.get("max_age_hours", 24))
    max_inputs = max(1, min(int(signals_cfg.get("max_inputs", SIGNALS_MAX_INPUTS)), SIGNALS_MAX_INPUTS))
    artifact_path = _artifact_path(repo_path, artifact_name)

    if artifact_path.exists():
        age_seconds = time.time() - artifact_path.stat().st_mtime
        if age_seconds <= refresh_hours * 3600:
            content = artifact_path.read_text(encoding="utf-8", errors="replace").strip()
            return content[:SIGNALS_CONTEXT_MAX_CHARS] if content else "(empty planning signals artifact)"

    sections = []
    for signal in inputs[:max_inputs]:
        if not isinstance(signal, dict):
            continue
        input_type = str(signal.get("input_type", "")).strip().lower()
        if input_type not in SIGNAL_INPUT_TYPES:
            print(f"  Skipping signal input with unsupported type: {input_type or '?'}")
            continue
        content, error = _read_research_source(repo_path, signal, {
            "allowed_domains": signals_cfg.get("allowed_domains", []),
            "max_source_chars": signals_cfg.get("max_source_chars", SIGNALS_MAX_SOURCE_CHARS),
        })
        if error:
            print(f"  Skipping signal input {signal.get('name', '?')}: {error}")
            continue
        observed_at = _parse_signal_timestamp(signal.get("observed_at"))
        summary, key_metrics, implications = _summarize_signal_input(signal, content or "")
        sections.append({
            "name": str(signal.get("name", "Unnamed input")).strip() or "Unnamed input",
            "input_type": input_type,
            "location": str(signal.get("url") or signal.get("path") or "(unknown)").strip(),
            "observed_at": observed_at.strftime("%Y-%m-%d %H:%M UTC") if observed_at else "unspecified",
            "freshness": _format_freshness_hours(observed_at),
            "provenance": str(signal.get("provenance") or "configured source").strip(),
            "trust_note": str(signal.get("trust_note") or "treat as advisory external evidence; verify before irreversible action").strip(),
            "privacy_note": str(signal.get("privacy_note") or "public-safe summary only; do not include raw personal data").strip(),
            "summary": summary,
            "key_metrics": key_metrics or ["No explicit metric extracted."],
            "planning_implications": implications or ["No direct sprint implication captured."],
        })

    _write_signals_artifact(repo_path, artifact_path, sections, refresh_hours)
    content = artifact_path.read_text(encoding="utf-8", errors="replace").strip()
    return content[:SIGNALS_CONTEXT_MAX_CHARS] if content else "(empty planning signals artifact)"


def _recently_closed_issues(repo: str, days: int = 7) -> str:
    """Return recently closed issues with close reasons for retrospective."""
    since = (datetime.now(tz=timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    raw = _gh([
        "issue", "list", "--repo", repo, "--state", "closed",
        "--json", "number,title,closedAt,stateReason,labels",
        "--limit", "30",
    ])
    if not raw:
        return "(no recently closed issues)"
    try:
        issues = json.loads(raw)
    except json.JSONDecodeError:
        return "(failed to parse)"
    # Filter to last N days
    recent = []
    for i in issues:
        closed_at = i.get("closedAt", "")
        if closed_at >= since:
            labels = ", ".join(l.get("name", "") for l in i.get("labels", []))
            reason = i.get("stateReason", "completed")
            lbl = f" [{labels}]" if labels else ""
            recent.append(f"- #{i['number']}: {i['title']}{lbl} — {reason}")
    return "\n".join(recent) if recent else "(no issues closed in the last week)"


def _recent_merged_prs(repo: str, days: int = 7) -> str:
    """Return recently merged PRs for retrospective context."""
    raw = _gh([
        "pr", "list", "--repo", repo, "--state", "merged",
        "--json", "number,title,mergedAt,headRefName",
        "--limit", "30",
    ])
    if not raw:
        return "(no recently merged PRs)"
    try:
        prs = json.loads(raw)
    except json.JSONDecodeError:
        return "(failed to parse)"
    since = (datetime.now(tz=timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    recent = []
    for pr in prs:
        merged_at = pr.get("mergedAt", "")
        if merged_at >= since:
            recent.append(f"- PR #{pr['number']}: {pr['title']} (branch: {pr.get('headRefName', '?')})")
    return "\n".join(recent) if recent else "(no PRs merged in the last week)"


_STRATEGY_TEMPLATE = """\
# Strategy — {repo_name}

> Auto-maintained by agent-os strategic planner. Updated each sprint cycle.

## Product Vision

(Extracted from README on first run — agents refine this over time.)

## Current Focus Areas

{focus_marker}
(Updated each sprint with the key themes being pursued.)

## Sprint History

"""


# ---------------------------------------------------------------------------
# Focus area analysis
# ---------------------------------------------------------------------------

_FOCUS_AREA_PROMPT = """\
You are analyzing sprint history for a software project to identify recurring work themes.

Below are the most recent sprint entries (plans and retrospectives). Identify the 3-5
dominant focus areas that these sprints reveal. Each focus area should be a concise
bullet point (one line) describing a theme of ongoing work.

Focus on patterns that appear across multiple sprints, not one-off tasks.
Write from the perspective of guiding future sprint planning.

Sprint entries:
{sprint_entries}

Return ONLY a JSON array of 3-5 strings, each a concise focus area bullet point.
No markdown fences, no commentary. Example: ["Improve CI reliability", "Expand API coverage"]"""


def _call_haiku(prompt: str) -> str:
    """Call Claude Haiku for cheap, fast analysis."""
    claude_bin = os.environ.get("CLAUDE_BIN", "claude")
    codex_bin = os.environ.get("CODEX_BIN", "codex")
    errors: list[str] = []
    result = subprocess.run(
        [claude_bin, "-p", prompt, "--model", FOCUS_AREA_MODEL],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    errors.append(f"Claude exit {result.returncode}: {result.stderr[:300]}")

    result = subprocess.run(
        [codex_bin, "exec", "--skip-git-repo-check", prompt],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    errors.append(f"Codex exit {result.returncode}: {(result.stderr or result.stdout)[:300]}")
    raise RuntimeError(" | ".join(errors))


def _extract_sprint_entries(content: str) -> list[str]:
    """Extract individual sprint entries from STRATEGY.md content.

    Each entry starts with '### Sprint YYYY-MM-DD' and runs until the next
    ### heading or end of file.
    """
    pattern = r"(### Sprint \d{4}-\d{2}-\d{2}.*?)(?=### Sprint \d{4}-\d{2}-\d{2}|\Z)"
    entries = re.findall(pattern, content, re.DOTALL)
    return [e.strip() for e in entries if e.strip()]


def _is_focus_areas_manually_edited(content: str) -> bool:
    """Detect if the Current Focus Areas section was manually edited.

    If the auto-marker is missing but the section has content beyond the
    placeholder, assume manual editing.
    """
    match = re.search(
        r"## Current Focus Areas\n(.*?)(?=^## |\Z)",
        content, re.DOTALL | re.MULTILINE,
    )
    if not match:
        return False
    section = match.group(1).strip()
    # If the marker is present, it's auto-managed
    if FOCUS_AREA_MARKER in section:
        return False
    # If it's the default placeholder, empty, or whitespace-only, not manually edited
    stripped = section.strip()
    if not stripped or stripped == "(Updated each sprint with the key themes being pursued.)":
        return False
    # Has content without our marker → manual edit
    return True


def _analyze_focus_areas(sprint_entries: list[str]) -> list[str] | None:
    """Use Haiku to extract recurring themes from sprint entries.

    Returns a list of 3-5 focus area strings, or None on failure.
    """
    # Cap input to avoid huge prompts — use the most recent entries (max 10)
    entries_text = "\n\n---\n\n".join(sprint_entries[:10])
    prompt = _FOCUS_AREA_PROMPT.format(sprint_entries=entries_text)
    try:
        raw = _call_haiku(prompt)
    except Exception as e:
        print(f"  Focus area analysis failed: {e}")
        return None
    # Parse JSON
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        areas = json.loads(text)
    except json.JSONDecodeError:
        print(f"  Failed to parse focus areas: {text[:200]}")
        return None
    if not isinstance(areas, list) or not all(isinstance(a, str) for a in areas):
        return None
    return areas[:5]


def _update_focus_areas_section(content: str, areas: list[str]) -> str:
    """Replace the Current Focus Areas section content with new auto-generated areas.

    Preserves everything outside the section. Only replaces content between
    '## Current Focus Areas' and the next '## ' heading.
    """
    bullets = "\n".join(f"- {a}" for a in areas)
    new_section = f"{FOCUS_AREA_MARKER}\n{bullets}"

    # Match the section content between heading and next heading
    pattern = r"(## Current Focus Areas\s*\n).*?(?=\n## )"
    replacement = rf"\g<1>\n{new_section}\n"
    updated, count = re.subn(pattern, replacement, content, count=1, flags=re.DOTALL)
    if count == 0:
        # Section exists but is at end of file (no following ## heading)
        pattern_end = r"(## Current Focus Areas\s*\n).*"
        replacement_end = rf"\g<1>\n{new_section}\n"
        updated = re.sub(pattern_end, replacement_end, content, count=1, flags=re.DOTALL)
    return updated


def _commit_repo_markdown_with_retry(
    repo_path: Path,
    relative_path: Path,
    target_content: str,
    commit_message: str,
    success_message: str,
    max_attempts: int = 3,
):
    """Commit/push repo-local markdown updates without leaving the checkout dirty."""
    file_path = repo_path / relative_path
    relative_str = str(relative_path)

    for attempt in range(max_attempts):
        try:
            subprocess.run(
                ["git", "-C", str(repo_path), "add", relative_str],
                check=True, capture_output=True, text=True,
            )
            diff = subprocess.run(
                ["git", "-C", str(repo_path), "diff", "--cached", "--quiet"],
                capture_output=True,
            )
            if diff.returncode == 0:
                return

            subprocess.run(
                ["git", "-C", str(repo_path), "commit", "-m", commit_message],
                check=True, capture_output=True, text=True,
            )
            subprocess.run(
                ["git", "-C", str(repo_path), "push", "origin", "main"],
                check=True, capture_output=True, text=True,
            )
            print(success_message)
            return
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or "").strip()
            if "rejected" in stderr or "non-fast-forward" in stderr:
                pull = subprocess.run(
                    ["git", "-C", str(repo_path), "pull", "--rebase", "origin", "main"],
                    capture_output=True, text=True,
                )
                if pull.returncode != 0:
                    print(f"  Warning: failed to rebase {relative_str} for {repo_path.name}: {pull.stderr.strip()}")
                    return
                file_path.write_text(target_content, encoding="utf-8")
                continue
            print(f"  Warning: failed to push {relative_str} for {repo_path.name}: {stderr or e}")
            return

    print(f"  Warning: gave up updating {relative_str} for {repo_path.name} after {max_attempts} attempts")


def _update_strategy(
    repo_path: Path,
    repo_name: str,
    sprint_summary: str,
    retrospective: str,
):
    """Update STRATEGY.md with sprint plan and retrospective findings."""
    strategy_md = repo_path / "STRATEGY.md"
    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

    if not strategy_md.exists():
        # Bootstrap with template + README goal
        readme_goal = _read_readme_goal(repo_path)
        template = _STRATEGY_TEMPLATE.format(
            repo_name=repo_name, focus_marker=FOCUS_AREA_MARKER,
        )
        template = template.replace(
            "(Extracted from README on first run — agents refine this over time.)",
            readme_goal[:1500] if readme_goal else "(see README.md)",
        )
        strategy_md.write_text(template, encoding="utf-8")

    content = strategy_md.read_text(encoding="utf-8")

    entry = f"\n### Sprint {now}\n\n"
    if retrospective:
        entry += f"**Retrospective:**\n{retrospective}\n\n"
    entry += f"**Plan:**\n{sprint_summary}\n"

    anchor = "## Sprint History"
    if anchor in content:
        updated = content.replace(anchor, anchor + "\n" + entry, 1)
    else:
        updated = content + f"\n{anchor}\n{entry}"

    # Auto-update focus areas when 3+ sprint entries exist
    sprint_entries = _extract_sprint_entries(updated)
    if len(sprint_entries) >= MIN_SPRINTS_FOR_FOCUS:
        if not _is_focus_areas_manually_edited(updated):
            print(f"  Analyzing focus areas from {len(sprint_entries)} sprint entries...")
            areas = _analyze_focus_areas(sprint_entries)
            if areas:
                updated = _update_focus_areas_section(updated, areas)
                print(f"  Updated focus areas: {len(areas)} themes")
        else:
            print("  Skipping focus area update — section was manually edited")

    strategy_md.write_text(updated, encoding="utf-8")

    _commit_repo_markdown_with_retry(
        repo_path,
        Path("STRATEGY.md"),
        updated,
        f"chore: update STRATEGY.md — sprint {now}",
        f"  STRATEGY.md updated and pushed for {repo_name}",
    )


def _build_retrospective(repo: str, days: int = DEFAULT_SPRINT_CADENCE_DAYS) -> str:
    """Build a retrospective summary of the last sprint's outcomes."""
    closed = _recently_closed_issues(repo, days=days)
    merged = _recent_merged_prs(repo, days=days)
    parts = []
    if closed and not closed.startswith("(no"):
        parts.append(f"Issues completed:\n{closed}")
    if merged and not merged.startswith("(no"):
        parts.append(f"PRs merged:\n{merged}")
    if not parts:
        return f"(no activity in the last {days} days)"
    return "\n\n".join(parts)


def _recent_metrics_summary(cfg: dict) -> str:
    """Compact summary of recent task completions."""
    root = Path(cfg.get("root_dir", ".")).expanduser()
    metrics_file = root / "runtime" / "metrics" / "agent_stats.jsonl"
    records = load_recent_metrics(metrics_file, window_days=METRICS_WINDOW_DAYS)
    if not records:
        return "(no recent metrics)"
    lines = []
    for r in records[-100:]:
        lines.append(json.dumps({
            k: r.get(k) for k in ("task_id", "status", "task_type", "agent")
            if r.get(k)
        }))
    # Also compute summary stats
    total = len(records)
    success = sum(1 for r in records if r.get("status") == "complete")
    partial = sum(1 for r in records if r.get("status") == "partial")
    blocked = sum(1 for r in records if r.get("status") == "blocked")
    failed = total - success - partial - blocked
    summary = (
        f"Summary: {total} tasks total — {success} complete, "
        f"{partial} partial, {blocked} blocked, {failed} failed"
    )
    return summary + "\n\n" + "\n".join(lines[-50:])


# ---------------------------------------------------------------------------
# LLM plan generation
# ---------------------------------------------------------------------------

PLAN_PROMPT = """You are the Strategic Planner for an autonomous AI software team.
Your job is to select the next sprint from the existing backlog: up to {plan_size} prioritized tasks.

You may only PROMOTE existing backlog issues. Do not create new issues. Backlog
curation and issue creation belong to the backlog groomer; the planner's job is
to choose which existing backlog issues should move to Ready next.

Context about this repository:

--- Product Vision & Strategy ---
{strategy_context}

--- Product Goal (from README) ---
{readme_goal}

--- North Star (NORTH_STAR.md) ---
{north_star}

--- Stable Planning Principles (PLANNING_PRINCIPLES.md) ---
{planning_principles}

--- Planning Signals (PLANNING_SIGNALS.md) ---
{signals_context}

--- Codebase Context (CODEBASE.md) ---
{codebase_context}

--- Pre-Planning Research (PLANNING_RESEARCH.md) ---
{research_context}

--- Last Sprint Retrospective ---
{retrospective}

--- Last 30 git commits ---
{git_log}

--- Issue metrics ---
Open issues: {open_count} | Recently closed: {closed_count} | Blocked: {blocked_count}

--- Recent task completion metrics ---
{metrics_summary}

--- Backlog issues (candidates to PROMOTE — use issue number to reference) ---
{backlog_issues}

--- Currently open issues already in progress (do NOT select these) ---
{open_issues}

--- Sibling repositories (cross-repo context) ---
{cross_repo_context}

Rules:
- Each task must be atomic and executable by an AI agent in a single session
- Tasks must NOT be vague epics — they should have clear, testable success criteria
- Order by priority (most impactful first)
- Build on last sprint's outcomes — continue momentum, fix regressions, advance strategy
- Treat the planning principles as the stable north-star rubric when strategy and backlog quality are ambiguous
- When planning signals are present, prioritize measurable user, market, and analytics outcomes over narrative summaries alone
- When fresh research is present, use it to inform prioritization and rationale. Prefer work that is supported by repo-local strategy plus bounded external evidence.
- Prefer unblockers, autonomy gains, planning-quality gains, and evidence-driven improvements over local churn
- Include a mix of: feature work that advances the product vision, bug fixes, self-improvement, and infrastructure
- At least 1-2 tasks should push toward the long-term vision, not just maintain the status quo
- If sibling repos are listed above, consider cross-repo dependencies: sequence work so prerequisite changes (e.g. API changes in repo A that repo B depends on) are completed first. If a task depends on work in another repo, note the dependency in the goal (e.g. "Depends on owner/repo-a completing X")
- If there is at least one credible backlog candidate, return at least 1 promoted issue. Do not return an empty list just because no issue is perfect; choose the best available issue.
- Return an empty result only if every backlog candidate is clearly unsuitable this cycle.

Return ONLY one of these JSON shapes (no markdown fences, no commentary):

1. A JSON array of at most {plan_size} promotion objects.
2. A JSON object like {{"empty_reason": "why every backlog issue is unsuitable right now"}} when no issue should be promoted.

For promotion arrays:
Each object must have:
  "action"     - always "promote"
  "issue_number" - the GitHub issue number to promote, e.g. 8
  "title"      - the existing GitHub issue title
  "goal"       - one-paragraph goal statement
  "task_type"  - one of: implementation, debugging, architecture, research, docs, design, content
  "priority"   - one of: prio:high, prio:normal, prio:low
  "rationale"  - one sentence explaining why this task matters this week, referencing the strategy
  "labels"     - JSON array of label strings (choose from: enhancement, bug, tech-debt)

Return ONLY the JSON."""


def _planner_agents(cfg: dict) -> list[str]:
    """Return planner LLM fallback order.

    Defaults to Claude first for stronger long-form planning, with Codex as the
    only fallback. We intentionally do not fan out to Gemini/DeepSeek here:
    sprint planning is a control-plane function and should degrade narrowly.
    """
    agents = cfg.get("planner_agents")
    if isinstance(agents, list):
        cleaned = [str(agent).strip().lower() for agent in agents if str(agent).strip()]
        return cleaned or ["claude", "codex"]
    return ["claude", "codex"]


def _call_sonnet(prompt: str, cfg: dict) -> str:
    errors: list[str] = []
    planner_model = str(cfg.get("planner_claude_model", ANALYSIS_MODEL)).strip() or ANALYSIS_MODEL

    for agent in _planner_agents(cfg):
        try:
            if agent == "claude":
                claude_bin = os.environ.get("CLAUDE_BIN", "claude")
                result = subprocess.run(
                    [claude_bin, "-p", prompt, "--model", planner_model],
                    capture_output=True, text=True, timeout=180,
                )
                if result.returncode == 0:
                    return result.stdout.strip()
                detail = (result.stderr or result.stdout or "").strip()[:300]
                errors.append(f"Claude exit {result.returncode}: {detail}")
                continue

            if agent == "codex":
                codex_bin = os.environ.get("CODEX_BIN", "codex")
                result = subprocess.run(
                    [codex_bin, "exec", "--skip-git-repo-check", prompt],
                    capture_output=True, text=True, timeout=180,
                )
                if result.returncode == 0:
                    return result.stdout.strip()
                detail = (result.stderr or result.stdout or "").strip()[:300]
                errors.append(f"Codex exit {result.returncode}: {detail}")
                continue

            errors.append(f"Unsupported planner agent: {agent}")
        except FileNotFoundError as e:
            errors.append(f"{agent} missing: {e}")
        except Exception as e:
            errors.append(f"{agent} failed: {e}")

    raise RuntimeError(" | ".join(errors) if errors else "No planner agents configured")


def _parse_plan(text: str) -> tuple[list[dict], str | None]:
    """Parse planner JSON response, stripping markdown fences if present."""
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    data = json.loads(text)
    if isinstance(data, dict):
        reason = str(data.get("empty_reason", "")).strip() or "Planner selected no backlog issues."
        return [], reason
    if isinstance(data, list):
        return data, None
    raise ValueError(f"Unexpected planner response type: {type(data).__name__}")


def _build_plan_prompt(
    *,
    plan_size: int,
    strategy_context: str,
    readme_goal: str,
    north_star: str,
    planning_principles: str,
    codebase_context: str,
    signals_context: str,
    research_context: str,
    retrospective: str,
    git_log: str,
    counts: dict[str, int],
    metrics_summary: str,
    backlog_text: str,
    open_issues: str,
    cross_repo_context: str,
) -> str:
    return PLAN_PROMPT.format(
        plan_size=plan_size,
        strategy_context=strategy_context,
        readme_goal=readme_goal,
        north_star=north_star,
        planning_principles=planning_principles,
        signals_context=signals_context,
        codebase_context=codebase_context,
        research_context=research_context,
        retrospective=retrospective,
        git_log=git_log,
        open_count=counts["open"],
        closed_count=counts["closed"],
        blocked_count=counts["blocked"],
        metrics_summary=metrics_summary,
        backlog_issues=backlog_text,
        open_issues=open_issues,
        cross_repo_context=cross_repo_context or "(single-repo mode — no sibling repos configured)",
    )


# ---------------------------------------------------------------------------
# Telegram approval gate
# ---------------------------------------------------------------------------

def _send_telegram(cfg: dict, text: str, reply_markup: dict | None = None) -> int | None:
    """Send a Telegram message. Return message_id on success, None otherwise."""
    token = str(cfg.get("telegram_bot_token", "")).strip()
    chat_id = str(cfg.get("telegram_chat_id", "")).strip()
    if not token or not chat_id:
        return None
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    cmd = ["curl", "-sS", "-X", "POST", url, "-d", f"chat_id={chat_id}", "--data-urlencode", f"text={text}"]
    if reply_markup:
        cmd += ["--data-urlencode", f"reply_markup={json.dumps(reply_markup, separators=(',', ':'))}"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    try:
        data = json.loads(result.stdout)
        if data.get("ok"):
            return data.get("result", {}).get("message_id")
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def _format_plan_message(plan: list[dict], repo: str, cadence_days: float) -> str:
    """Format the sprint plan for Telegram display."""
    approval_timeout_hours = _approval_timeout_hours(cadence_days)

    lines = [
        f"📋 Sprint Plan — {repo}",
        f"Cadence: {_format_cadence(cadence_days)}",
        f"Generated: {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
    ]
    for i, task in enumerate(plan, 1):
        priority = task.get("priority", "prio:normal")
        action = task.get("action", "create")
        prio_icon = {"prio:high": "🔴", "prio:normal": "🟡", "prio:low": "🟢"}.get(priority, "⚪")
        source = f"#{task['issue_number']}" if action == "promote" else "NEW"
        lines.append(f"{i}. {prio_icon} [{source}] [{task.get('task_type', '?')}] {task.get('title', '?')}")
        lines.append(f"   {task.get('rationale', '')}")
        lines.append("")

    lines.append("Tap Approve to apply this plan: move selected backlog issues to Ready.")

    lines.append("Tap Skip to leave the plan unapplied.")
    lines.append(f"Auto-skip in {_format_duration_hours(approval_timeout_hours)} if no action.")
    return "\n".join(lines)


def _create_plan_approval_action(cfg: dict, repo: str, cadence_days: float) -> dict:
    now = datetime.now(timezone.utc)
    action_id = uuid4().hex[:12]
    timeout_hours = _approval_timeout_hours(cadence_days)
    return {
        "action_id": action_id,
        "type": "plan_approval",
        "status": "pending",
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=timeout_hours)).isoformat(),
        "chat_id": str(cfg.get("telegram_chat_id", "")).strip(),
        "message_id": None,
        "repo": repo,
        "approval": "pending",
        "timeout_hours": timeout_hours,
        "plan": [],
        "retrospective": "",
    }


def _list_pending_plan_actions(actions_dir: Path) -> list[dict]:
    actions: list[dict] = []
    for path in sorted(actions_dir.glob("*.json")):
        try:
            action = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if action.get("type") != "plan_approval":
            continue
        if action.get("status") in {"completed", "rejected", "expired", "invalid"}:
            continue
        actions.append(action)
    return actions


def _repo_pending_plan_action(actions_dir: Path, repo: str) -> dict | None:
    repo_actions = [
        action for action in _list_pending_plan_actions(actions_dir)
        if action.get("repo") == repo
    ]
    if not repo_actions:
        return None
    repo_actions.sort(key=lambda action: action.get("created_at", ""))
    return repo_actions[-1]


def _invalidate_pending_action_for_dormant_repo(paths: dict, action: dict, github_slug: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    print(f"  Skipping {github_slug}: dormant (discarding pending approval {action.get('action_id', '?')})")
    action["status"] = "invalid"
    action["completed_at"] = now
    action["invalid_reason"] = "repo is dormant"
    save_telegram_action(paths["TELEGRAM_ACTIONS"], action)


def _complete_plan_action(
    cfg: dict,
    paths: dict,
    action: dict,
    repo_path: Path,
) -> bool:
    repo = action.get("repo", "")
    action_id = action.get("action_id", "?")
    approval = action.get("approval", "pending")
    now = datetime.now(timezone.utc).isoformat()

    if approval == "approved":
        print(f"  Applying previously approved plan for {repo} ({action_id})...")
        plan = action.get("plan") or []
        retrospective = action.get("retrospective", "")
        if not plan:
            warn_msg = (
                f"⚠️ Approved sprint plan for {repo} could not be applied because the stored "
                "proposal did not include a persisted plan payload. A fresh plan will be generated "
                "on the next eligible cycle."
            )
            print(warn_msg)
            _send_telegram(cfg, warn_msg)
            action["status"] = "invalid"
            action["completed_at"] = now
            action["invalid_reason"] = "approved action missing persisted plan payload"
            save_telegram_action(paths["TELEGRAM_ACTIONS"], action)
            return True
        ready_urls, skipped = apply_plan_promotions(cfg, repo, plan)
        sprint_summary = "\n".join(
            f"- [{t.get('priority', '?')}] {t.get('title', '?')}: {t.get('rationale', '')}"
            for t in plan
        )
        _update_strategy(repo_path, repo, sprint_summary, retrospective)
        record_run(cfg, "strategic_planner", repo)
        summary = (
            f"✅ Approved sprint applied for {repo}\n"
            f"Issues moved to Ready: {len(ready_urls)} | Skipped: {len(skipped)}\n"
            f"{chr(10).join(ready_urls[:10]) if ready_urls else '(no issues promoted)'}"
        )
        print(summary)
        _send_telegram(cfg, summary)
        action["status"] = "completed"
        action["completed_at"] = now
        save_telegram_action(paths["TELEGRAM_ACTIONS"], action)
        return True

    if approval == "rejected":
        skip_msg = f"⏭️ Sprint plan for {repo} was not approved. Skipping this cycle."
        print(skip_msg)
        _send_telegram(cfg, skip_msg)
        record_run(cfg, "strategic_planner", repo)
        action["status"] = "rejected"
        action["completed_at"] = now
        save_telegram_action(paths["TELEGRAM_ACTIONS"], action)
        return True

    if telegram_action_expired(action):
        skip_msg = f"⏭️ Sprint plan for {repo} expired without approval. Skipping this cycle."
        print(skip_msg)
        _send_telegram(cfg, skip_msg)
        record_run(cfg, "strategic_planner", repo)
        action["status"] = "expired"
        action["expired_at"] = now
        save_telegram_action(paths["TELEGRAM_ACTIONS"], action)
        return True

    print(f"  Pending approval for {repo}: awaiting Telegram response ({action_id})")
    return True


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def _title_similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def _open_issue_exists(repo: str, title: str) -> bool:
    """Check if an open issue with similar title already exists."""
    result = subprocess.run(
        ["gh", "issue", "list", "--repo", repo, "--state", "open",
         "--search", title, "--json", "title", "--limit", "20"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return False
    try:
        issues = json.loads(result.stdout or "[]")
        return any(
            _title_similar(i.get("title", ""), title) >= SIMILARITY_THRESHOLD
            for i in issues
        )
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Issue creation
# ---------------------------------------------------------------------------

def _create_issue(repo: str, title: str, body: str, labels: list[str]) -> str:
    """Create a GitHub issue and return its URL."""
    cmd = ["gh", "issue", "create", "--repo", repo, "--title", title, "--body", body]
    for label in labels:
        cmd += ["--label", label]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"gh issue create failed: {result.stderr.strip()}")
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Per-repo resolution (reused from backlog_groomer pattern)
# ---------------------------------------------------------------------------

def _resolve_repos(cfg: dict) -> list[tuple[str, Path]]:
    """Return [(github_slug, local_path)] for configured repos."""
    repos = []
    github_repos = cfg.get("github_repos", {})
    owner = cfg.get("github_owner", "")
    allowed = cfg.get("allowed_repos", [])

    # Prefer explicit repo mappings from github_projects because they include
    # the correct local path and avoid lossy name matching against allowed_repos.
    for pv in cfg.get("github_projects", {}).values():
        if not isinstance(pv, dict):
            continue
        for rc in pv.get("repos", []):
            gh_repo = rc.get("github_repo", "")
            local = rc.get("local_repo", rc.get("repo", rc.get("path", "")))
            if gh_repo and local:
                repos.append((gh_repo, Path(local).expanduser()))

    if github_repos and owner:
        for key, slug in github_repos.items():
            full_slug = f"{owner}/{slug}" if "/" not in slug else slug
            for rp in allowed:
                rp = Path(rp).expanduser()
                repo_name = full_slug.rsplit("/", 1)[-1]
                if rp.name in {key, slug, repo_name}:
                    repos.append((full_slug, rp))
                    break

    if not repos:
        gh_repo = cfg.get("github_repo", "")
        if not gh_repo and owner:
            gh_repo = f"{owner}/agent-os"
        if gh_repo and allowed:
            repos.append((gh_repo, Path(allowed[0]).expanduser()))
        elif gh_repo:
            root = Path(cfg.get("root_dir", ".")).expanduser()
            repos.append((gh_repo, root))

    seen = set()
    unique = []
    for slug, path in repos:
        if slug not in seen:
            seen.add(slug)
            unique.append((slug, path))
    return unique


def _repo_planner_config(cfg: dict, github_slug: str) -> tuple[int, float]:
    """Return (plan_size, sprint_cadence_days) for a repo, checking per-repo overrides."""
    plan_size = cfg.get("plan_size", DEFAULT_PLAN_SIZE)
    cadence = cfg.get("sprint_cadence_days", DEFAULT_SPRINT_CADENCE_DAYS)

    # Check github_projects for per-repo overrides
    for pv in cfg.get("github_projects", {}).values():
        if not isinstance(pv, dict):
            continue
        for rc in pv.get("repos", []):
            if rc.get("github_repo") == github_slug:
                plan_size = rc.get("plan_size", plan_size)
                cadence = rc.get("sprint_cadence_days", cadence)
                return int(plan_size), float(cadence)

    return int(plan_size), float(cadence)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def plan_repo(
    cfg: dict,
    github_slug: str,
    repo_path: Path,
    cross_repo_context: str = "",
) -> tuple[list[dict] | None, str, str | None]:
    """Generate a sprint plan for one repo. Returns (plan, retrospective, empty_reason)."""
    plan_size, sprint_cadence_days = _repo_planner_config(cfg, github_slug)
    print(f"\n--- Planning {github_slug} (plan_size={plan_size}, cadence={sprint_cadence_days}d) ---")

    # 1. Read product context
    readme_goal = _read_readme_goal(repo_path)
    print(f"  README goal: {len(readme_goal)} chars")

    # 2. Read STRATEGY.md (cumulative strategy memory)
    strategy_context = _read_strategy(repo_path)
    if strategy_context:
        print(f"  STRATEGY.md: {len(strategy_context)} chars")
    else:
        print("  STRATEGY.md: not yet created (will bootstrap after approval)")
        strategy_context = "(No strategy document yet — this is the first sprint. Focus on establishing foundations.)"

    # 3. Read stable planning rubric
    north_star = read_north_star(repo_path)
    print(f"  NORTH_STAR.md: {len(north_star)} chars")

    # 4. Read stable planning rubric
    planning_principles = _read_planning_principles(repo_path)
    print(f"  PLANNING_PRINCIPLES.md: {len(planning_principles)} chars")

    # 5. Read CODEBASE.md
    codebase_context = _read_codebase_md(repo_path)
    print(f"  CODEBASE.md: {len(codebase_context)} chars")

    # 6. Planning signals
    signals_context = _planning_signals_context(cfg, github_slug, repo_path)
    print(f"  Signals context: {len(signals_context)} chars")

    # 7. Pre-planning research
    research_context = _planning_research_context(cfg, github_slug, repo_path)
    print(f"  Research context: {len(research_context)} chars")

    # 8. Sprint retrospective — what shipped last sprint
    retrospective = _build_retrospective(github_slug, days=sprint_cadence_days)
    print(f"  Retrospective: {len(retrospective)} chars")

    # 9. Last 30 git commits
    git_log = _git_log(repo_path, n=30)
    print(f"  Git log: {git_log.count(chr(10)) + 1} commits")

    # 10. Issue metrics
    counts = _issue_counts(github_slug)
    print(f"  Issues — open: {counts['open']}, closed: {counts['closed']}, blocked: {counts['blocked']}")

    # 11. Recent task metrics
    metrics_summary = _recent_metrics_summary(cfg)

    # 12. Backlog issues (candidates for promotion — trusted authors only)
    backlog = _backlog_issues(github_slug, cfg)
    backlog_text = _format_backlog_for_prompt(backlog)
    print(f"  Backlog candidates: {len(backlog)}")

    # 13. Open issues already in progress (for exclusion — trusted authors only)
    open_issues = _open_issues_summary(github_slug, cfg)

    # 14. Cross-repo context
    if cross_repo_context:
        print(f"  Cross-repo context: {len(cross_repo_context)} chars from sibling repos")

    # 15. Build prompt with all context
    prompt = _build_plan_prompt(
        plan_size=plan_size,
        strategy_context=strategy_context,
        readme_goal=readme_goal,
        north_star=north_star,
        planning_principles=planning_principles,
        codebase_context=codebase_context,
        signals_context=signals_context,
        research_context=research_context,
        retrospective=retrospective,
        git_log=git_log,
        counts=counts,
        metrics_summary=metrics_summary,
        backlog_text=backlog_text,
        open_issues=open_issues,
        cross_repo_context=cross_repo_context,
    )

    # 16. Call Sonnet
    try:
        raw = _call_sonnet(prompt, cfg)
    except Exception as e:
        print(f"  Plan generation failed: {e}")
        return None, "", None

    try:
        plan, empty_reason = _parse_plan(raw)
    except Exception as e:
        print(f"  Failed to parse plan: {e}\n  Raw: {raw[:300]}")
        return None, "", None

    print(f"  Generated {len(plan)} tasks")
    if not plan and empty_reason:
        print(f"  Planner rationale for empty plan: {empty_reason}")
    return plan[:plan_size], retrospective, empty_reason


def _set_issues_ready(cfg: dict, github_slug: str, issue_urls: list[str]):
    """Move created sprint issues to Status=Ready on the project board."""
    owner = cfg.get("github_owner", "")
    if not owner:
        return

    for project_cfg in cfg.get("github_projects", {}).values():
        if not isinstance(project_cfg, dict):
            continue
        # Check if this project covers the repo
        repo_match = any(
            rc.get("github_repo") == github_slug
            for rc in project_cfg.get("repos", [])
        )
        if not repo_match:
            continue

        ready_value = project_cfg.get("ready_value", "Ready")
        try:
            info = query_project(project_cfg["project_number"], owner)
            ready_option = info["status_options"].get(ready_value)
            if not info["status_field_id"] or not ready_option:
                print(f"  Warning: status option '{ready_value}' not found in project")
                return

            # Newly created issues may not be on the project yet; add them before
            # attempting to move them to Ready.
            known_urls = {item["url"] for item in info["items"]}
            for issue_url in issue_urls:
                if issue_url in known_urls:
                    continue
                add_result = subprocess.run(
                    [
                        "gh", "project", "item-add", str(project_cfg["project_number"]),
                        "--owner", owner,
                        "--url", issue_url,
                        "--format", "json",
                    ],
                    capture_output=True, text=True,
                )
                if add_result.returncode != 0:
                    print(f"  Warning: failed to add {issue_url} to project: {add_result.stderr.strip()}")
            # Refresh project state after adds.
            info = query_project(project_cfg["project_number"], owner)

            for item in info["items"]:
                if item["url"] in issue_urls:
                    set_item_status(
                        info["project_id"],
                        item["item_id"],
                        info["status_field_id"],
                        ready_option,
                    )
                    print(f"  Set #{item['number']} → Ready on project board")
                    # Also add the ready label for visibility
                    try:
                        ensure_labels(github_slug, ["ready"])
                        edit_issue_labels(github_slug, item["number"], add=["ready"])
                    except Exception:
                        pass
        except Exception as e:
            print(f"  Warning: failed to set project status: {e}")
        return  # Only process the first matching project


def _promote_issue(repo: str, issue_number: int, priority: str, labels: list[str]) -> str | None:
    """Add priority/ready labels to an existing backlog issue. Returns its URL."""
    try:
        # Add priority label and any missing labels
        all_labels = [l for l in labels if l] + [priority]
        ensure_labels(repo, all_labels + ["ready"])
        edit_issue_labels(repo, issue_number, add=all_labels)
        url = f"https://github.com/{repo}/issues/{issue_number}"
        print(f"  Promoted: #{issue_number} → {priority}")
        return url
    except Exception as e:
        print(f"  Failed to promote #{issue_number}: {e}")
        return None


def apply_plan_promotions(
    cfg: dict, github_slug: str, plan: list[dict],
) -> tuple[list[str], list[str]]:
    """Execute an approved plan by promoting existing backlog issues to Ready."""
    ready_urls: list[str] = []
    skipped: list[str] = []

    for task in plan:
        action = (task.get("action") or "promote").lower()
        title = (task.get("title") or "").strip()
        priority = task.get("priority", "prio:normal")
        labels = [str(l) for l in task.get("labels", []) if l]
        if priority not in labels:
            labels.append(priority)

        if action != "promote":
            print(f"  Skip (planner returned unsupported action {action!r}): {title!r}")
            skipped.append(title)
            continue

        issue_number = task.get("issue_number")
        if not issue_number:
            print(f"  Skip (promote without issue_number): {title!r}")
            skipped.append(title)
            continue
        url = _promote_issue(github_slug, int(issue_number), priority, labels)
        if url:
            ready_urls.append(url)
        else:
            skipped.append(title)

    # Move promoted issues to Ready — triggers dispatch on next cycle
    if ready_urls:
        print(f"\n  Setting {len(ready_urls)} issue(s) to Ready on project board...")
        _set_issues_ready(cfg, github_slug, ready_urls)

    return ready_urls, skipped


def run():
    cfg = load_config()
    paths = runtime_paths(cfg)
    with job_lock(cfg, "strategic_planner") as acquired:
        if not acquired:
            print("Strategic planner already running; skipping overlapping cron invocation.")
            return

        process_telegram_callbacks(cfg, paths)

        repos = _resolve_repos(cfg)

        if not repos:
            print("No repos configured; nothing to plan.")
            return

        print(f"Strategic planner starting for {len(repos)} repo(s).")

        # Pre-read all strategies for cross-repo context
        strategy_map = _load_strategy_map(repos)
        dependencies = _strategy_dependencies(
            [github_slug for github_slug, _ in repos],
            strategy_map,
        )
        if len(repos) > 1:
            print(f"Multi-repo mode: reading strategies from {len(repos)} repos for cross-repo awareness.")
            repos = _order_repos_by_dependencies(repos, dependencies)
            print("Planning order: " + " -> ".join(github_slug for github_slug, _ in repos))

        for github_slug, repo_path in repos:
            _, sprint_cadence_days = _repo_planner_config(cfg, github_slug)
            pending_action = _repo_pending_plan_action(paths["TELEGRAM_ACTIONS"], github_slug)
            if sprint_cadence_days <= 0:
                if pending_action:
                    _invalidate_pending_action_for_dormant_repo(paths, pending_action, github_slug)
                else:
                    print(f"  Skipping {github_slug}: dormant")
                continue
            if pending_action:
                _complete_plan_action(cfg, paths, pending_action, repo_path)
                continue

            due, reason = is_due(
                cfg,
                "strategic_planner",
                github_slug,
                cadence_hours=sprint_cadence_days * 24.0,
            )
            if not due:
                active, active_reason = _has_active_sprint_work(github_slug, cfg)
                if active:
                    print(f"  Skipping {github_slug}: {reason} ({active_reason})")
                    continue
                should_plan, override_reason = _maybe_refresh_backlog_for_early_cycle(cfg, github_slug, repo_path)
                if not should_plan:
                    print(f"  Skipping {github_slug}: {reason} ({override_reason})")
                    continue
                print(f"  Proceeding early for {github_slug}: {override_reason}")

            # Build cross-repo context from sibling repos
            cross_repo_ctx = _gather_cross_repo_context(
                repos,
                github_slug,
                strategy_map=strategy_map,
                dependencies=dependencies,
            )
            # Phase 1: Generate plan with retrospective
            plan, retrospective, empty_reason = plan_repo(cfg, github_slug, repo_path, cross_repo_ctx)
            if plan is None:
                print(f"  No plan generated for {github_slug}; skipping.")
                continue
            if not plan:
                record_run(cfg, "strategic_planner", github_slug)
                no_op_msg = (
                    f"ℹ️ Sprint plan for {github_slug}\n"
                    f"Cadence: {_format_cadence(sprint_cadence_days)}\n"
                    f"No backlog issues were promoted this cycle.\n"
                    f"Reason: {empty_reason or 'No suitable backlog issues were selected.'}"
                )
                print(no_op_msg)
                _send_telegram(cfg, no_op_msg)
                continue

            # Phase 2: Post to Telegram and wait for approval
            plan_text = _format_plan_message(plan, github_slug, sprint_cadence_days)
            print(f"\n{plan_text}\n")

            action = _create_plan_approval_action(cfg, github_slug, sprint_cadence_days)
            save_telegram_action(paths["TELEGRAM_ACTIONS"], action)
            msg_id = _send_telegram(cfg, plan_text, reply_markup=planner_reply_markup(action["action_id"]))
            if msg_id is None:
                print("  Failed to send plan to Telegram (or no credentials).")
                print("  Skipping plan application — approval gate is mandatory.")
                continue
            action["message_id"] = msg_id
            action["plan"] = plan
            action["retrospective"] = retrospective
            save_telegram_action(paths["TELEGRAM_ACTIONS"], action)

            print(f"  Approval requested for {github_slug}; continuing to other repos until a later cron tick resolves it.")


if __name__ == "__main__":
    run()
