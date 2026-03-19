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
from pathlib import Path
from uuid import uuid4

from orchestrator.paths import load_config, runtime_paths
from orchestrator.agent_scorer import load_recent_metrics
from orchestrator.gh_project import query_project, set_item_status, edit_issue_labels, ensure_labels
from orchestrator.queue import planner_reply_markup, save_telegram_action, load_telegram_action, telegram_action_expired
from orchestrator.scheduler_state import is_due, record_run, job_lock
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
POLL_INTERVAL_SECONDS = 300   # 5 minutes
APPROVAL_TIMEOUT_HOURS = 24
APPROVAL_KEYWORDS = {"yes", "approve", "approved", "ok", "go", "lgtm"}
REJECTION_KEYWORDS = {"no", "reject", "skip", "cancel", "nope"}


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
    """Return formatted list of open issues from trusted authors for dedup context."""
    raw = _gh(["issue", "list", "--repo", repo, "--state", "open",
               "--json", "number,title,labels,author", "--limit", "50"])
    if not raw:
        return "(no open issues)"
    try:
        issues = json.loads(raw)
    except json.JSONDecodeError:
        return "(failed to parse)"
    if not issues:
        return "(no open issues)"
    lines = []
    for i in issues[:50]:
        author = (i.get("author") or {}).get("login", "")
        if not is_trusted(author, cfg):
            continue
        labels = ", ".join(l.get("name", "") for l in i.get("labels", []))
        lbl = f" [{labels}]" if labels else ""
        lines.append(f"- #{i.get('number')}: {i.get('title')}{lbl}")
    return "\n".join(lines) if lines else "(no open issues)"


def _backlog_issues(repo: str, cfg: dict) -> list[dict]:
    """Return open issues from trusted authors that are NOT in-progress/ready/done."""
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
        if label_names & {"in-progress", "agent-dispatched", "ready", "done"}:
            continue
        backlog.append(i)
    return backlog


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
    result = subprocess.run(
        [claude_bin, "-p", prompt, "--model", FOCUS_AREA_MODEL],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Claude exit {result.returncode}: {result.stderr[:300]}")
    return result.stdout.strip()


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

    # Commit and push
    try:
        subprocess.run(
            ["git", "-C", str(repo_path), "add", "STRATEGY.md"],
            check=True, capture_output=True,
        )
        diff = subprocess.run(
            ["git", "-C", str(repo_path), "diff", "--cached", "--quiet"],
            capture_output=True,
        )
        if diff.returncode == 0:
            return  # No changes
        subprocess.run(
            ["git", "-C", str(repo_path), "commit", "-m",
             f"chore: update STRATEGY.md — sprint {now}"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repo_path), "push", "origin", "main"],
            check=True, capture_output=True,
        )
        print(f"  STRATEGY.md updated and pushed for {repo_name}")
    except Exception as e:
        print(f"  Warning: failed to push STRATEGY.md for {repo_name}: {e}")


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
Your job is to select next week's sprint: exactly {plan_size} prioritized tasks.

You can either PROMOTE existing backlog issues or CREATE new ones. Prefer
promoting existing issues when they align with the strategy — the backlog
groomer already identified these as valuable. If there are relevant backlog
issues, prefer PROMOTE over CREATE. Only create new issues when the backlog
doesn't cover a strategic need.

Context about this repository:

--- Product Vision & Strategy ---
{strategy_context}

--- Product Goal (from README) ---
{readme_goal}

--- Codebase Context (CODEBASE.md) ---
{codebase_context}

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
- Do NOT create tasks whose only purpose is bootstrapping STRATEGY.md; the planner updates STRATEGY.md automatically after approval
- Build on last sprint's outcomes — continue momentum, fix regressions, advance strategy
- Include a mix of: feature work that advances the product vision, bug fixes, self-improvement, and infrastructure
- At least 1-2 tasks should push toward the long-term vision, not just maintain the status quo
- If sibling repos are listed above, consider cross-repo dependencies: sequence work so prerequisite changes (e.g. API changes in repo A that repo B depends on) are completed first. If a task depends on work in another repo, note the dependency in the goal (e.g. "Depends on owner/repo-a completing X")
- For NEW issues: body must use ## Goal, ## Success Criteria, ## Constraints sections

Return ONLY a JSON array (no markdown fences, no commentary) of exactly {plan_size} objects.
Each object must have:
  "action"     - either "promote" (existing backlog issue) or "create" (new issue)
  "issue_number" - (promote only) the GitHub issue number to promote, e.g. 8
  "title"      - concise GitHub issue title under 70 chars (for create; for promote, the existing title)
  "goal"       - one-paragraph goal statement
  "task_type"  - one of: implementation, debugging, architecture, research, docs, design, content
  "priority"   - one of: prio:high, prio:normal, prio:low
  "rationale"  - one sentence explaining why this task matters this week, referencing the strategy
  "body"       - (create only) structured body with ## Goal\\n...\\n## Success Criteria\\n...\\n## Constraints\\n- Prefer minimal diffs
  "labels"     - JSON array of label strings (choose from: enhancement, bug, tech-debt)

Return ONLY the JSON array."""


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


def _parse_plan(text: str) -> list[dict]:
    """Parse JSON array from Claude response, stripping markdown fences if present."""
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(text)


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
    create_count = sum(1 for task in plan if (task.get("action") or "create").lower() == "create")
    promote_count = sum(1 for task in plan if (task.get("action") or "create").lower() == "promote")
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

    if create_count and promote_count:
        lines.append("Tap Approve to apply this plan: create new issues and move selected backlog issues to Ready.")
    elif create_count:
        lines.append("Tap Approve to apply this plan: create issues and move them to Ready.")
    else:
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
    }


def _poll_approval(paths: dict, action_id: str, timeout_hours: float = APPROVAL_TIMEOUT_HOURS, poll_interval: int = POLL_INTERVAL_SECONDS) -> bool:
    """Poll the persisted planner approval action. Returns True if approved."""
    actions_dir = paths["TELEGRAM_ACTIONS"]
    deadline = time.time() + timeout_hours * 3600
    print(f"Polling for approval (timeout: {timeout_hours}h, interval: {poll_interval}s)...")

    while time.time() < deadline:
        action = load_telegram_action(actions_dir, action_id)
        if action:
            if action.get("approval") == "approved":
                print("Approval received via Telegram button.")
                return True
            if action.get("approval") == "rejected":
                print("Rejection received via Telegram button.")
                return False
            if telegram_action_expired(action):
                print("Approval timed out.")
                return False
        time.sleep(poll_interval)

    print("Approval timed out.")
    return False


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
) -> tuple[list[dict] | None, str]:
    """Generate a sprint plan for one repo. Returns (plan, retrospective) or (None, "")."""
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

    # 3. Read CODEBASE.md
    codebase_context = _read_codebase_md(repo_path)
    print(f"  CODEBASE.md: {len(codebase_context)} chars")

    # 4. Sprint retrospective — what shipped last sprint
    retrospective = _build_retrospective(github_slug, days=sprint_cadence_days)
    print(f"  Retrospective: {len(retrospective)} chars")

    # 5. Last 30 git commits
    git_log = _git_log(repo_path, n=30)
    print(f"  Git log: {git_log.count(chr(10)) + 1} commits")

    # 6. Issue metrics
    counts = _issue_counts(github_slug)
    print(f"  Issues — open: {counts['open']}, closed: {counts['closed']}, blocked: {counts['blocked']}")

    # 7. Recent task metrics
    metrics_summary = _recent_metrics_summary(cfg)

    # 8. Backlog issues (candidates for promotion — trusted authors only)
    backlog = _backlog_issues(github_slug, cfg)
    backlog_text = _format_backlog_for_prompt(backlog)
    print(f"  Backlog candidates: {len(backlog)}")

    # 9. Open issues already in progress (for exclusion — trusted authors only)
    open_issues = _open_issues_summary(github_slug, cfg)

    # 10. Cross-repo context
    if cross_repo_context:
        print(f"  Cross-repo context: {len(cross_repo_context)} chars from sibling repos")

    # 11. Build prompt with all context
    prompt = PLAN_PROMPT.format(
        plan_size=plan_size,
        strategy_context=strategy_context,
        readme_goal=readme_goal,
        codebase_context=codebase_context,
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

    # 12. Call Sonnet
    try:
        raw = _call_sonnet(prompt, cfg)
    except Exception as e:
        print(f"  Plan generation failed: {e}")
        return None, ""

    try:
        plan = _parse_plan(raw)
    except Exception as e:
        print(f"  Failed to parse plan: {e}\n  Raw: {raw[:300]}")
        return None, ""

    if not isinstance(plan, list):
        print(f"  Unexpected response format:\n  {raw[:300]}")
        return None, ""

    print(f"  Generated {len(plan)} tasks")
    return plan[:plan_size], retrospective


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


def create_plan_issues(
    cfg: dict, github_slug: str, plan: list[dict],
) -> tuple[list[str], list[str]]:
    """Execute an approved plan: promote existing issues or create new ones, then set all to Ready."""
    ready_urls: list[str] = []
    skipped: list[str] = []

    for task in plan:
        action = (task.get("action") or "create").lower()
        title = (task.get("title") or "").strip()
        priority = task.get("priority", "prio:normal")
        labels = [str(l) for l in task.get("labels", []) if l]
        if priority not in labels:
            labels.append(priority)

        if action == "promote":
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
        else:
            # Create new issue
            body = (task.get("body") or "").strip()
            if not title:
                continue

            if _open_issue_exists(github_slug, title):
                print(f"  Skip (duplicate): {title!r}")
                skipped.append(title)
                continue

            try:
                url = _create_issue(github_slug, title, body, labels)
                print(f"  Created: {url}")
                ready_urls.append(url)
            except Exception as e:
                print(f"  Failed to create {title!r}: {e}")
                skipped.append(title)

    # Move all issues (promoted + created) to Ready — triggers dispatch on next cycle
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
            due, reason = is_due(
                cfg,
                "strategic_planner",
                github_slug,
                cadence_hours=sprint_cadence_days * 24.0,
            )
            if not due:
                print(f"  Skipping {github_slug}: {reason}")
                continue

            # Build cross-repo context from sibling repos
            cross_repo_ctx = _gather_cross_repo_context(
                repos,
                github_slug,
                strategy_map=strategy_map,
                dependencies=dependencies,
            )
            # Phase 1: Generate plan with retrospective
            plan, retrospective = plan_repo(cfg, github_slug, repo_path, cross_repo_ctx)
            if not plan:
                print(f"  No plan generated for {github_slug}; skipping.")
                continue

            # Phase 2: Post to Telegram and wait for approval
            plan_text = _format_plan_message(plan, github_slug, sprint_cadence_days)
            print(f"\n{plan_text}\n")

            action = _create_plan_approval_action(cfg, github_slug, sprint_cadence_days)
            save_telegram_action(paths["TELEGRAM_ACTIONS"], action)
            msg_id = _send_telegram(cfg, plan_text, reply_markup=planner_reply_markup(action["action_id"]))
            if msg_id is None:
                print("  Failed to send plan to Telegram (or no credentials).")
                print("  Skipping issue creation — approval gate is mandatory.")
                continue
            action["message_id"] = msg_id
            save_telegram_action(paths["TELEGRAM_ACTIONS"], action)

            # Count this as a planning run once the approval request exists.
            record_run(cfg, "strategic_planner", github_slug)

            # Phase 3: Wait for human approval
            approved = _poll_approval(paths, action["action_id"], timeout_hours=action["timeout_hours"])

            if not approved:
                skip_msg = f"⏭️ Sprint plan for {github_slug} was not approved. Skipping this cycle."
                print(skip_msg)
                _send_telegram(cfg, skip_msg)
                continue

            # Phase 4: Create issues (only on approval)
            print(f"\n  Creating issues for approved plan ({github_slug})...")
            created_urls, skipped = create_plan_issues(cfg, github_slug, plan)

            # Phase 5: Update STRATEGY.md with sprint plan and retrospective
            sprint_summary = "\n".join(
                f"- [{t.get('priority', '?')}] {t.get('title', '?')}: {t.get('rationale', '')}"
                for t in plan
            )
            _update_strategy(repo_path, github_slug, sprint_summary, retrospective)

            summary = (
                f"✅ Sprint plan approved for {github_slug}\n"
                f"Issues created: {len(created_urls)} | Skipped (duplicate): {len(skipped)}\n"
                f"Status: Ready → dispatch will begin within 1 minute"
            )
            for url in created_urls:
                summary += f"\n  {url}"
            print(summary)
            _send_telegram(cfg, summary)


if __name__ == "__main__":
    run()
