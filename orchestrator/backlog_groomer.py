"""Backlog groomer.

Reviews each repository's open issues, recent task completions, CODEBASE.md
Known Issues section, and risk flags from completed tasks to identify gaps
and technical debt. Generates new improvement issues into Backlog so the
strategic planner always has roughly twice the next sprint's worth of
candidates to choose from.

Sizing is target-driven, not data-driven: each run computes
``target_depth = plan_size * 2`` (configurable) and only generates issues to
refill the gap, capped to ``MAX_ISSUES_PER_RUN`` per cycle. The groomer also
runs at half the planner cadence by default so the backlog refills before the
next sprint plan, and reserves at least 40% of generated slots for
adoption-facing work.

Cron can invoke this frequently; per-repo cadence and dormancy are enforced in
code.
"""
from __future__ import annotations

import glob as _glob_mod
import json
import math
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

from orchestrator.paths import load_config
from orchestrator.agent_scorer import load_recent_metrics, findings_path as scorer_findings_path
from orchestrator.gh_project import ensure_labels, query_project, set_item_status, gh
from orchestrator.objectives import load_repo_objective, format_objective_for_prompt
from orchestrator.outcome_attribution import get_repo_outcome_check_ids, format_outcome_checks_section
from orchestrator.repo_context import (
    read_evaluation_rubric,
    read_product_inspection_artifact,
    read_production_feedback_artifact,
    read_north_star,
    read_planning_principles,
    read_planning_research_artifact,
    read_readme_goal,
    read_strategy_context,
)
from orchestrator.scheduler_state import is_due, record_run, job_lock
from orchestrator.repo_modes import is_dispatcher_only_repo
from orchestrator.skip_signals import load_skip_signals, skip_penalty_for_issue as _skip_penalty_for_issue
from orchestrator.trust import is_trusted

WINDOW_DAYS = 30
STALE_DAYS = 30
MAX_ISSUES_PER_RUN = 5  # Per-run generation budget (not steady-state backlog depth)
DEFAULT_BACKLOG_DEPTH_MULTIPLIER = 2  # Target backlog = plan_size * multiplier
DEFAULT_PLAN_SIZE = 5
ADOPTION_BALANCE_FRACTION = 0.4  # At least 40% of generated issues must be adoption-facing
ANALYSIS_MODEL = "haiku"
SIMILARITY_THRESHOLD = 0.75  # Title similarity threshold for dedup
PR_NUMBER_RE = re.compile(r"\bPR\s*#(\d+)\b|/pull/(\d+)\b", re.IGNORECASE)
_BACKLOG_EXCLUDE_LABELS = {"in-progress", "agent-dispatched", "ready", "done", "blocked"}


def _repo_plan_size(cfg: dict, github_slug: str) -> int:
    """Return the configured planner plan_size for a repo (mirrors strategic_planner)."""
    plan_size = cfg.get("plan_size", DEFAULT_PLAN_SIZE)
    for pv in cfg.get("github_projects", {}).values():
        if not isinstance(pv, dict):
            continue
        for rc in pv.get("repos", []):
            if rc.get("github_repo") == github_slug:
                return int(rc.get("plan_size", plan_size))
    return int(plan_size)


def _repo_backlog_depth_target(cfg: dict, github_slug: str) -> int:
    """Return the target number of open backlog issues for a repo.

    Defaults to ``plan_size * DEFAULT_BACKLOG_DEPTH_MULTIPLIER`` so the planner
    always has roughly twice the next sprint's worth of candidates to choose
    from. Overridable per repo via ``target_backlog_depth`` (absolute) or
    ``backlog_depth_multiplier`` (relative to plan_size).
    """
    multiplier = cfg.get("backlog_depth_multiplier", DEFAULT_BACKLOG_DEPTH_MULTIPLIER)
    for pv in cfg.get("github_projects", {}).values():
        if not isinstance(pv, dict):
            continue
        for rc in pv.get("repos", []):
            if rc.get("github_repo") == github_slug:
                if "target_backlog_depth" in rc:
                    return max(0, int(rc["target_backlog_depth"]))
                multiplier = rc.get("backlog_depth_multiplier", multiplier)
                break
    if "target_backlog_depth" in cfg:
        return max(0, int(cfg["target_backlog_depth"]))
    return max(0, int(_repo_plan_size(cfg, github_slug) * float(multiplier)))


def _count_backlog_issues(open_issues: list[dict]) -> int:
    """Count open issues that are still candidates for promotion.

    Mirrors ``strategic_planner._backlog_issues``: excludes anything already
    ready/in-progress/done/blocked. Used to decide how much new work the
    groomer should generate this cycle.
    """
    n = 0
    for i in open_issues:
        names = {(l.get("name") or "").lower() for l in i.get("labels", [])}
        if names & _BACKLOG_EXCLUDE_LABELS:
            continue
        n += 1
    return n


def _repo_groomer_cadence_days(cfg: dict, github_slug: str) -> float:
    """Return cadence in days for backlog grooming.

    Defaults to **half** the planner cadence so the groomer runs ahead of the
    planner and can refill the backlog before the next sprint plan. Explicit
    ``groomer_cadence_days`` overrides this at top level, per project, or per
    repo. ``0`` means dormant.
    """
    top_explicit = cfg.get("groomer_cadence_days")
    top_sprint = float(cfg.get("sprint_cadence_days", 7))

    for pv in cfg.get("github_projects", {}).values():
        if not isinstance(pv, dict):
            continue
        for rc in pv.get("repos", []):
            if rc.get("github_repo") == github_slug:
                if "groomer_cadence_days" in rc:
                    return float(rc["groomer_cadence_days"])
                if top_explicit is not None:
                    return float(top_explicit)
                sprint = float(rc.get("sprint_cadence_days", top_sprint))
                return sprint * 0.5

    if top_explicit is not None:
        return float(top_explicit)
    return top_sprint * 0.5


# ---------------------------------------------------------------------------
# Data gathering helpers
# ---------------------------------------------------------------------------

def _gh(cmd: list[str], *, check: bool = False) -> str:
    result = subprocess.run(["gh", *cmd], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(cmd[:3])}... exit {result.returncode}: {result.stderr.strip()}")
    return result.stdout.strip()


def _list_open_issues(repo: str, cfg: dict) -> list[dict]:
    """Return open issues from trusted authors for a repo via gh CLI."""
    raw = _gh([
        "issue", "list", "--repo", repo, "--state", "open",
        "--json", "number,title,createdAt,updatedAt,labels,author,url",
        "--limit", "100",
    ])
    if not raw:
        return []
    try:
        issues = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return [
        i for i in issues
        if is_trusted((i.get("author") or {}).get("login", ""), cfg)
    ]


def _extract_pr_number(text: str) -> int | None:
    match = PR_NUMBER_RE.search(text or "")
    if not match:
        return None
    return int(match.group(1) or match.group(2))


def _get_pr_state(repo: str, pr_number: int) -> dict | None:
    raw = _gh([
        "pr", "view", str(pr_number), "--repo", repo,
        "--json", "number,state,mergedAt,url",
    ])
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _set_issue_done(cfg: dict, github_slug: str, issue_url: str):
    owner = cfg.get("github_owner", "")
    if not owner:
        return
    for project_cfg in cfg.get("github_projects", {}).values():
        if not isinstance(project_cfg, dict):
            continue
        repo_match = any(rc.get("github_repo") == github_slug for rc in project_cfg.get("repos", []))
        if not repo_match:
            continue
        done_value = project_cfg.get("done_value", "Done")
        try:
            info = query_project(project_cfg["project_number"], owner)
            option_id = info["status_options"].get(done_value)
            if not info["status_field_id"] or not option_id:
                return
            for item in info["items"]:
                if item["url"] == issue_url:
                    set_item_status(info["project_id"], item["item_id"], info["status_field_id"], option_id)
                    return
        except Exception as e:
            print(f"Warning: failed to set {issue_url} to Done: {e}")
        return


def _cleanup_stale_issues(cfg: dict, github_slug: str, open_issues: list[dict]) -> list[int]:
    """Close stale PR-linked backlog issues whose referenced PR is already merged/closed."""
    cleaned: list[int] = []
    for issue in open_issues:
        pr_number = _extract_pr_number(issue.get("title", ""))
        if not pr_number:
            continue
        pr = _get_pr_state(github_slug, pr_number)
        if not pr:
            continue
        if pr.get("mergedAt") or pr.get("state") == "MERGED":
            comment = f"Closed automatically by backlog groomer: referenced PR #{pr_number} is already merged."
        elif pr.get("state") == "CLOSED":
            comment = f"Closed automatically by backlog groomer: referenced PR #{pr_number} is already closed."
        else:
            continue
        try:
            gh(["issue", "close", str(issue["number"]), "-R", github_slug, "--comment", comment], check=False)
            _set_issue_done(cfg, github_slug, issue.get("url", ""))
            cleaned.append(int(issue["number"]))
            print(f"  Closed stale issue #{issue['number']} (PR #{pr_number} already resolved)")
        except Exception as e:
            print(f"  Warning: failed to close stale issue #{issue.get('number')}: {e}")
    return cleaned


def _stale_issues(issues: list[dict], stale_days: int = STALE_DAYS) -> list[dict]:
    """Return issues with no activity for more than stale_days."""
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=stale_days)
    stale = []
    for issue in issues:
        updated = issue.get("updatedAt", "")
        try:
            ts = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            if ts < cutoff:
                stale.append(issue)
        except (ValueError, TypeError):
            pass
    return stale


def _parse_known_issues(repo_path: Path) -> list[str]:
    """Extract Known Issues entries from CODEBASE.md."""
    codebase_md = repo_path / "CODEBASE.md"
    if not codebase_md.exists():
        return []
    content = codebase_md.read_text(encoding="utf-8", errors="replace")

    # Extract content between "## Known Issues" and the next "##" heading
    match = re.search(
        r"##\s+Known Issues\s*/?\s*Gotchas\s*\n(.*?)(?=\n##\s|\Z)",
        content, re.DOTALL | re.IGNORECASE,
    )
    if not match:
        return []
    section = match.group(1).strip()
    if not section or section.startswith("("):
        return []  # Still placeholder text

    lines = []
    for line in section.splitlines():
        line = line.strip()
        if line.startswith("- ") or line.startswith("* "):
            lines.append(line.lstrip("-* ").strip())
        elif line and not line.startswith("("):
            lines.append(line)
    return lines


def _find_risk_flags(cfg: dict) -> list[dict]:
    """Scan recent worktrees for .agent_result.md files and extract RISKS."""
    worktrees_dir = Path(cfg.get("worktrees_dir", "/srv/worktrees"))
    if not worktrees_dir.exists():
        return []

    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=WINDOW_DAYS)
    risks = []

    for wt in worktrees_dir.iterdir():
        if not wt.is_dir():
            continue
        result_file = wt / ".agent_result.md"
        if not result_file.exists():
            continue

        # Check file age
        try:
            mtime = datetime.fromtimestamp(result_file.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                continue
        except OSError:
            continue

        content = result_file.read_text(encoding="utf-8", errors="replace")

        # Extract RISKS section
        match = re.search(r"^RISKS:\s*\n(.*?)(?=^[A-Z_]+:|\Z)", content, re.MULTILINE | re.DOTALL)
        if not match:
            continue
        risk_text = match.group(1).strip()
        if not risk_text or risk_text == "- None":
            continue

        # Extract task_id from the result or directory name
        task_id_match = re.search(r"task-\d{8}-\d{6}-[a-z0-9-]+", wt.name)
        task_id = task_id_match.group(0) if task_id_match else wt.name

        for line in risk_text.splitlines():
            line = line.strip().lstrip("-").strip()
            if line and line.lower() != "none":
                risks.append({"task_id": task_id, "risk": line})

    return risks


def _recent_completions_summary(records: list[dict]) -> str:
    """Compact summary of recent completions for the LLM prompt."""
    if not records:
        return "(no recent completions)"
    lines = []
    for r in records[-100:]:
        lines.append(json.dumps({
            k: r.get(k) for k in ("task_id", "repo", "agent", "status", "task_type")
            if r.get(k)
        }))
    return "\n".join(lines)


def _filter_records_for_repo(records: list[dict], github_slug: str, repo_path: Path) -> list[dict]:
    """Keep only recent metrics that clearly belong to this repo."""
    repo_names = {
        github_slug.lower(),
        github_slug.rsplit("/", 1)[-1].lower(),
        repo_path.name.lower(),
        str(repo_path).lower(),
    }
    filtered = []
    for rec in records:
        repo_value = str(rec.get("repo", "")).strip().lower()
        if not repo_value:
            continue
        if repo_value in repo_names:
            filtered.append(rec)
            continue
        if any(repo_value.endswith(name) for name in repo_names if "/" in repo_value or "/" in name):
            filtered.append(rec)
    return filtered


def _recent_blocked_tasks(records: list[dict]) -> list[dict]:
    """Return recent blocked/partial outcomes for follow-up generation."""
    return [
        rec for rec in records
        if str(rec.get("status", "")).strip().lower() in {"blocked", "partial"}
    ]


def _repo_gap_signals(repo_path: Path, open_issues: list[dict]) -> list[str]:
    """Detect high-leverage repo gaps worth turning into backlog issues."""
    signals: list[str] = []
    open_titles = " ".join(i.get("title", "") for i in open_issues).lower()

    def missing_issue_hint(*needles: str) -> bool:
        return not any(needle in open_titles for needle in needles)

    if not (repo_path / "STRATEGY.md").exists() and missing_issue_hint("strategy.md", "strategy", "planning"):
        signals.append("STRATEGY.md is missing; backlog may lack durable product direction and planning memory.")

    readme = repo_path / "README.md"
    if not readme.exists() and missing_issue_hint("readme", "goal"):
        signals.append("README.md is missing; the repo lacks an explicit goal and operator-facing product context.")
    elif readme.exists():
        content = readme.read_text(encoding="utf-8", errors="replace")
        if "## goal" not in content.lower() and len(content.strip()) < 400 and missing_issue_hint("goal", "vision"):
            signals.append("README.md lacks a clear Goal section or strong product framing for planning.")

    codebase = repo_path / "CODEBASE.md"
    if not codebase.exists() and missing_issue_hint("codebase", "known issues"):
        signals.append("CODEBASE.md is missing; maintainers and agents lack codebase memory and known-issues context.")

    return signals


def _blocked_issue_signals(open_issues: list[dict]) -> list[dict]:
    """Return open blocked issues that may justify unblocker tasks."""
    blocked = []
    for issue in open_issues:
        labels = {l.get("name", "").lower() for l in issue.get("labels", [])}
        if "blocked" in labels:
            blocked.append(issue)
    return blocked


# ---------------------------------------------------------------------------
# Adoption / credibility signal gathering
# ---------------------------------------------------------------------------

def _fetch_github_stars_forks(github_slug: str) -> dict:
    """Fetch current star and fork count for a repo via gh api."""
    try:
        raw = _gh(["api", f"repos/{github_slug}", "--jq", ".stargazers_count,.forks_count"], check=True)
        parts = raw.strip().splitlines()
        if len(parts) >= 2:
            return {"stars": int(parts[0]), "forks": int(parts[1])}
    except Exception:
        pass
    return {}


def _assess_readme(repo_path: Path) -> dict:
    """Assess README structure for adoption-relevant qualities."""
    readme = repo_path / "README.md"
    if not readme.exists():
        return {"exists": False}
    content = readme.read_text(encoding="utf-8", errors="replace")
    lower = content.lower()
    lines = content.strip().splitlines()
    return {
        "exists": True,
        "line_count": len(lines),
        "has_quickstart": any(h in lower for h in ("quick start", "quickstart", "getting started", "## install")),
        "has_demo": any(d in lower for d in ("demo", "demo.svg", "demo.gif", "screencast", "animation")),
        "has_badge": "![" in content and ("badge" in lower or "shields.io" in lower or "stars" in lower),
        "has_goal": "## goal" in lower or "## what" in lower,
    }


def _assess_demo_availability(repo_path: Path) -> bool:
    """Check whether a visual demo asset exists in the repo."""
    for pattern in ("docs/demo.*", "demo.*", "assets/demo.*", "docs/*.gif", "docs/*.svg"):
        if _glob_mod.glob(str(repo_path / pattern)):
            return True
    return False


def _gather_adoption_signals(github_slug: str, repo_path: Path) -> str:
    """Build a compact text block of adoption/credibility signals for the LLM."""
    lines: list[str] = []

    gh_stats = _fetch_github_stars_forks(github_slug)
    if gh_stats:
        lines.append(f"GitHub stars: {gh_stats.get('stars', '?')}, forks: {gh_stats.get('forks', '?')}")
    else:
        lines.append("GitHub stars/forks: (could not fetch)")

    readme_info = _assess_readme(repo_path)
    if not readme_info.get("exists"):
        lines.append("README: MISSING — critical adoption blocker")
    else:
        parts = [f"{readme_info.get('line_count', 0)} lines"]
        if not readme_info.get("has_quickstart"):
            parts.append("NO quickstart section")
        else:
            parts.append("has quickstart")
        if not readme_info.get("has_demo"):
            parts.append("NO demo reference")
        else:
            parts.append("has demo reference")
        if not readme_info.get("has_badge"):
            parts.append("NO star/social badge")
        else:
            parts.append("has badge")
        if not readme_info.get("has_goal"):
            parts.append("NO goal section")
        else:
            parts.append("has goal section")
        lines.append(f"README assessment: {', '.join(parts)}")

    has_demo = _assess_demo_availability(repo_path)
    lines.append(f"Demo asset available: {'yes' if has_demo else 'NO — no visual demo found in repo'}")

    # Quickstart friction: check if install/setup looks simple
    readme = repo_path / "README.md"
    if readme.exists():
        content = readme.read_text(encoding="utf-8", errors="replace").lower()
        if "pip install" in content or "npm install" in content or "brew install" in content:
            lines.append("Quickstart friction: low (package manager install documented)")
        elif "git clone" in content and ("make" in content or "setup" in content or "install" in content):
            lines.append("Quickstart friction: medium (clone + manual setup)")
        else:
            lines.append("Quickstart friction: high (no clear install path in README)")
    else:
        lines.append("Quickstart friction: critical (no README)")

    return "\n".join(lines)


def _bootstrap_doc_issues(repo_path: Path, open_issues: list[dict]) -> list[dict]:
    """Return deterministic bootstrap issues for missing core context docs."""
    open_titles = [i.get("title", "") for i in open_issues]
    bootstrap: list[dict] = []

    def add_issue(title: str, goal: str, success: list[str], labels: list[str] | None = None):
        if _is_duplicate(title, open_titles):
            return
        bootstrap.append({
            "title": title,
            "body": (
                f"## Goal\n{goal}\n\n"
                "## Success Criteria\n"
                + "\n".join(f"- {item}" for item in success)
                + "\n\n## Constraints\n- Prefer minimal diffs\n- Create the initial scaffold only; avoid overfitting early details"
            ),
            "task_type": "docs",
            "priority": "prio:high",
            "labels": labels or ["enhancement"],
        })
        open_titles.append(title)

    readme = repo_path / "README.md"
    if not readme.exists():
        add_issue(
            "Bootstrap README.md with repo goal and operator context",
            "Create an initial README.md that explains the repo goal, operating model, and what good looks like for future planning and execution.",
            [
                "README.md exists with a clear Goal section",
                "README.md explains the repo purpose in terms an agent can use for planning",
                "The document is concise and avoids speculative detail",
            ],
        )
    else:
        content = readme.read_text(encoding="utf-8", errors="replace")
        if "## goal" not in content.lower():
            add_issue(
                "Add a Goal section to README.md",
                "Add a concise Goal section to README.md so planners and workers have an explicit product objective.",
                [
                    "README.md contains a Goal section",
                    "The goal is concrete enough to guide backlog prioritization",
                    "Existing README content remains intact aside from focused edits",
                ],
            )

    if not (repo_path / "STRATEGY.md").exists():
        add_issue(
            "Bootstrap STRATEGY.md from repo state",
            "Create the initial STRATEGY.md so the repo has durable strategy memory across sprint cycles.",
            [
                "STRATEGY.md exists with an initial product vision",
                "The file includes at least Product Vision, Current Focus Areas, and Sprint History scaffolding",
                "The strategy reflects current repo state rather than generic boilerplate",
            ],
        )

    if not (repo_path / "PLANNING_PRINCIPLES.md").exists():
        add_issue(
            "Bootstrap PLANNING_PRINCIPLES.md for stable planning rules",
            "Create a stable planner rubric that defines how this repo should choose backlog work over time.",
            [
                "PLANNING_PRINCIPLES.md exists",
                "It defines selection priorities, tie-breakers, and what to avoid",
                "The rubric is stable and higher level than sprint-specific strategy",
            ],
        )

    if not (repo_path / "NORTH_STAR.md").exists():
        add_issue(
            "Bootstrap NORTH_STAR.md for long-term direction",
            "Create a stable long-term north-star document that defines the repo's capability ladder and enduring destination separately from sprint strategy.",
            [
                "NORTH_STAR.md exists",
                "It describes the long-term destination and capability ladder",
                "It stays higher level and more stable than STRATEGY.md",
            ],
        )

    if not (repo_path / "CODEBASE.md").exists():
        add_issue(
            "Bootstrap CODEBASE.md for execution memory",
            "Create the initial CODEBASE.md scaffold so agents have a place to accumulate architecture notes, key files, and known gotchas.",
            [
                "CODEBASE.md exists",
                "It includes sections for architecture, key files, known issues, and recent changes",
                "The initial scaffold is lightweight and ready for later agent updates",
            ],
            labels=["tech-debt"],
        )

    return bootstrap


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def _title_similar(a: str, b: str) -> float:
    """Return similarity ratio between two issue titles."""
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def _is_duplicate(title: str, existing_titles: list[str]) -> bool:
    """Check if title is semantically similar to any existing title."""
    for existing in existing_titles:
        if _title_similar(title, existing) >= SIMILARITY_THRESHOLD:
            return True
    return False


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
# LLM analysis
# ---------------------------------------------------------------------------

ANALYSIS_PROMPT = """You are an AI agent system analyst performing backlog grooming.
Review the data below and create exactly {num_issues} targeted, atomic improvement tasks.

Backlog state: {current_backlog} open backlog issues vs target depth {target_depth}.
You are refilling the backlog so the next sprint planner has at least 2x the
sprint plan size to choose from. Quality over volume — do not pad with
duplicates or low-value churn.

IMPORTANT: Your job is to create a BALANCED backlog that advances the repo's
stated objectives — not just internal infrastructure. Read the objectives and
strategy carefully. If the objective includes external metrics (e.g. GitHub
stars, adoption, user growth), you MUST generate issues that move those metrics,
not just internal plumbing. A system that only polishes its own engine but never
does anything visible to users will fail its objectives.

Focus on:
1. Objective-driven work — tasks that directly improve the metrics defined in the repo objective (adoption, stars, demos, quickstart, README, public proof, credibility)
2. Domain rubric gaps — if a domain evaluation rubric is present, identify work that closes gaps against the rubric's quality criteria and skill dimensions
3. Stale issues (open >30 days with no activity) — suggest closing or scoping down
4. Known Issues from CODEBASE.md that have no linked GitHub issue — create one
5. Risk flags from recently completed tasks — create follow-up mitigation tasks
6. Recent blocked or partial task outcomes — create unblock or hardening follow-ups
7. Repository foundation gaps (missing planning/research/ops scaffolding) — create enabling tasks
8. Backlog pressure or blocked-work patterns visible in open issues — create high-leverage backlog items

Balance rule: At least {adoption_min} of the {num_issues} issues you generate
this run (≥40%) MUST target adoption, credibility, activation, demos,
quickstart, README, public proof, or external-facing improvement — not internal
infrastructure. This is a hard floor, not a target. If the objectives include
external metrics (stars, forks, user growth), the floor applies regardless of
how many internal signals are present in the data below. Use the
"Adoption and credibility signals" section to ground these issues in concrete
gaps (low star/fork counts, missing demo, weak README, friction in quickstart).

Rules:
- Each task must be atomic and clearly scoped (one specific thing to fix/improve)
- Do NOT propose re-opening or modifying existing issues
- Do NOT create tasks that duplicate existing open issues
- Issue body must use ## Goal, ## Success Criteria, ## Constraints sections
- Order by priority (most impactful first)
- Assign priority based on objective alignment first, then risk and impact —
  work that moves tracked objective metrics is high priority;
  security/data-loss risks are high; tech-debt cleanup is normal; nice-to-haves are low

Return ONLY a JSON array (no markdown fences, no commentary) of exactly {num_issues} objects.
Each object must have:
  "title"     - concise GitHub issue title under 70 chars
  "body"      - structured body with ## Goal\\n...\\n## Success Criteria\\n...\\n## Constraints\\n- Prefer minimal diffs
  "task_type" - one of: implementation, debugging, architecture, research, docs, design, content
  "priority"  - one of: prio:high, prio:normal, prio:low
  "labels"    - JSON array of label strings (choose from: enhancement, bug, tech-debt)

--- Stale issues (open >30 days, no activity) ---
{stale_issues}

--- CODEBASE.md Known Issues (may lack linked GitHub issues) ---
{known_issues}

--- Risk flags from recent task completions ---
{risk_flags}

--- Recent blocked or partial task outcomes ---
{blocked_tasks}

--- Repository foundation gaps ---
{repo_gaps}

--- Open blocked issues that may need unblockers ---
{blocked_issues}

--- Repo Objectives (what this repo is measured on — generate work that moves these metrics) ---
{objectives_context}

--- Product goal (README.md) ---
{readme_goal}

--- North star (NORTH_STAR.md) ---
{north_star}

--- Strategy context (STRATEGY.md) ---
{strategy_context}

--- Planning principles (PLANNING_PRINCIPLES.md) ---
{planning_principles}

--- Production feedback artifact (PRODUCTION_FEEDBACK.md) ---
{production_feedback}

--- Live product inspection (PRODUCT_INSPECTION.md) ---
{product_inspection}

--- Planning research artifact (PLANNING_RESEARCH.md) ---
{research_context}

--- Adoption and credibility signals (use these to generate adoption-focused issues) ---
{adoption_signals}

--- Domain evaluation rubric (repo-specific quality criteria — use to shape issue goals and success criteria) ---
{evaluation_rubric}

--- Agent performance degradation findings (from weekly scorer) ---
{scorer_findings}

--- Recent task completions (last 30 days) ---
{completions}

--- Currently open issues (for dedup reference) ---
{open_issues}

--- Recently skipped plan issues (anti-repeat signal) ---
{skip_signal_context}

Return ONLY the JSON array."""


def _call_haiku(prompt: str) -> str:
    """Call groomer analysis model with Claude first, then Codex fallback."""
    errors: list[str] = []

    claude_bin = os.environ.get("CLAUDE_BIN", "claude")
    result = subprocess.run(
        [claude_bin, "-p", prompt, "--model", ANALYSIS_MODEL],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    errors.append(f"Claude exit {result.returncode}: {(result.stderr or result.stdout)[:300]}")

    codex_bin = os.environ.get("CODEX_BIN", "codex")
    result = subprocess.run(
        [codex_bin, "exec", "--skip-git-repo-check", prompt],
        capture_output=True, text=True, timeout=180,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    errors.append(f"Codex exit {result.returncode}: {(result.stderr or result.stdout)[:300]}")

    raise RuntimeError(" | ".join(errors))


def _parse_issues(text: str) -> list[dict]:
    """Parse JSON array from Claude response, stripping markdown fences if present."""
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(text)


# ---------------------------------------------------------------------------
# Issue creation
# ---------------------------------------------------------------------------

def _create_issue(repo: str, title: str, body: str, labels: list[str]) -> str:
    """Create a GitHub issue and return its URL."""
    # Ensure priority labels exist
    prio_labels = [l for l in labels if l.startswith("prio:")]
    try:
        ensure_labels(repo, prio_labels)
    except Exception:
        pass
    cmd = ["gh", "issue", "create", "--repo", repo, "--title", title, "--body", body]
    for label in labels:
        cmd += ["--label", label]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"gh issue create failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _set_issue_backlog(cfg: dict, github_slug: str, issue_url: str):
    owner = cfg.get("github_owner", "")
    if not owner:
        return

    for project_cfg in cfg.get("github_projects", {}).values():
        if not isinstance(project_cfg, dict):
            continue
        repo_match = any(rc.get("github_repo") == github_slug for rc in project_cfg.get("repos", []))
        if not repo_match:
            continue

        backlog_value = project_cfg.get("backlog_value", "Backlog")
        try:
            raw = gh([
                "project", "item-add", str(project_cfg["project_number"]),
                "--owner", owner,
                "--url", issue_url,
                "--format", "json",
            ], check=False)
            if not raw:
                return
            item_data = json.loads(raw)
            item_id = item_data.get("id")
            if not item_id:
                return

            info = query_project(project_cfg["project_number"], owner)
            option_id = info["status_options"].get(backlog_value)
            if info["status_field_id"] and option_id:
                set_item_status(info["project_id"], item_id, info["status_field_id"], option_id)
        except Exception as e:
            print(f"Warning: failed to add {issue_url} to project backlog: {e}")
        return



def _send_telegram(cfg: dict, text: str):
    token = str(cfg.get("telegram_bot_token", "")).strip()
    chat_id = str(cfg.get("telegram_chat_id", "")).strip()
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    subprocess.run(
        ["curl", "-sS", "-X", "POST", url,
         "-d", f"chat_id={chat_id}",
         "--data-urlencode", f"text={text}"],
        capture_output=True, text=True, timeout=20,
    )


# ---------------------------------------------------------------------------
# Per-repo resolution
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

    # Fallback: single repo from top-level config
    if not repos:
        gh_repo = cfg.get("github_repo", "")
        if not gh_repo and owner:
            gh_repo = f"{owner}/agent-os"
        if gh_repo and allowed:
            repos.append((gh_repo, Path(allowed[0]).expanduser()))
        elif gh_repo:
            root = Path(cfg.get("root_dir", ".")).expanduser()
            repos.append((gh_repo, root))

    # Deduplicate by github slug
    seen = set()
    unique = []
    for slug, path in repos:
        if slug not in seen:
            seen.add(slug)
            unique.append((slug, path))
    return unique


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def groom_repo(cfg: dict, github_slug: str, repo_path: Path) -> dict:
    """Groom a single repo. Returns summary dict."""
    print(f"\n--- Grooming {github_slug} ---")

    # 1. Gather open issues (trusted authors only — prompt injection defense)
    open_issues = _list_open_issues(github_slug, cfg)
    cleaned = _cleanup_stale_issues(cfg, github_slug, open_issues)
    if cleaned:
        open_issues = [issue for issue in open_issues if issue.get("number") not in cleaned]
    open_titles = [i.get("title", "") for i in open_issues]
    stale = _stale_issues(open_issues)
    print(f"  Open issues: {len(open_issues)}, stale (>{STALE_DAYS}d): {len(stale)}, cleaned: {len(cleaned)}")

    # 2. Recent completions from metrics
    root = Path(cfg.get("root_dir", ".")).expanduser()
    metrics_file = root / "runtime" / "metrics" / "agent_stats.jsonl"
    all_records = load_recent_metrics(metrics_file, window_days=WINDOW_DAYS)
    records = _filter_records_for_repo(all_records, github_slug, repo_path)
    print(f"  Completions (last {WINDOW_DAYS}d): {len(records)}")

    # 3. CODEBASE.md Known Issues
    known_issues = _parse_known_issues(repo_path)
    print(f"  Known Issues entries: {len(known_issues)}")

    # 4. Risk flags from .agent_result.md
    risk_flags = _find_risk_flags(cfg)
    print(f"  Risk flags found: {len(risk_flags)}")

    # 5. Additional signals for richer backlog creation
    blocked_tasks = _recent_blocked_tasks(records)
    blocked_issues = _blocked_issue_signals(open_issues)
    repo_gaps = _repo_gap_signals(repo_path, open_issues)
    bootstrap_issues = _bootstrap_doc_issues(repo_path, open_issues)
    readme_goal = read_readme_goal(repo_path)
    north_star = read_north_star(repo_path, max_chars=1400)
    strategy_context = read_strategy_context(repo_path, max_chars=1600)
    planning_principles = read_planning_principles(repo_path, max_chars=1400)
    production_feedback = read_production_feedback_artifact(repo_path, max_chars=1800)
    product_inspection = read_product_inspection_artifact(repo_path, max_chars=1600)
    research_context = read_planning_research_artifact(repo_path, max_chars=1600)
    objective = load_repo_objective(cfg, github_slug, repo_path)
    objectives_context = format_objective_for_prompt(objective, max_chars=1600)
    adoption_signals = _gather_adoption_signals(github_slug, repo_path)
    evaluation_rubric = read_evaluation_rubric(repo_path, max_chars=1600)
    print(f"  Blocked/partial task outcomes: {len(blocked_tasks)}")
    print(f"  Repo gaps: {len(repo_gaps)}, blocked issues: {len(blocked_issues)}")
    print(f"  Bootstrap doc issues: {len(bootstrap_issues)}")
    print(f"  Adoption signals: gathered")

    # Skip if no data to analyze
    if not stale and not known_issues and not risk_flags and not blocked_tasks and not blocked_issues and not repo_gaps and not bootstrap_issues and not records:
        print("  No data to analyze, skipping.")
        return {"status": "no-data", "created": 0, "skipped": 0, "cleaned": len(cleaned)}

    # 5. Determine how many issues to propose based on backlog depth target.
    # Goal: keep open backlog at ~plan_size * 2 so the planner always has
    # twice the next sprint's worth of candidates to choose from.
    plan_size = _repo_plan_size(cfg, github_slug)
    target_depth = _repo_backlog_depth_target(cfg, github_slug)
    current_backlog = _count_backlog_issues(open_issues)
    needed = max(0, target_depth - current_backlog)
    num_issues = min(MAX_ISSUES_PER_RUN, needed)
    print(
        f"  Backlog depth: {current_backlog}/{target_depth} (plan_size={plan_size}); "
        f"generating up to {num_issues} this run"
    )

    # Cadence backoff: if recent plans were auto-skipped (user may be away),
    # reduce generation volume to avoid filling the backlog with unreviewed work.
    skip_signals_path = root / "runtime" / "metrics" / "plan_skip_signals.jsonl"
    repo_skip_signals = load_skip_signals(skip_signals_path, github_slug, max_age_days=14)
    recent_auto_skips = sum(
        1 for s in repo_skip_signals if s.get("skip_type") == "auto_skip"
    )
    if recent_auto_skips >= 2 and num_issues > 1:
        reduced = max(1, num_issues // 2)
        print(f"  Cadence backoff: {recent_auto_skips} recent auto-skips → reducing generation from {num_issues} to {reduced}")
        num_issues = reduced

    if num_issues == 0 and not bootstrap_issues:
        print(f"  Backlog already at target depth ({current_backlog} ≥ {target_depth}); skipping generation.")
        return {"status": "skipped", "created": 0, "skipped": 0, "cleaned": len(cleaned)}

    # Reserve at least 40% of generated slots for adoption-facing work.
    adoption_min = math.ceil(num_issues * ADOPTION_BALANCE_FRACTION) if num_issues > 0 else 0

    # 6. Build prompt
    stale_text = "\n".join(
        f"- #{i.get('number')}: {i.get('title')} (updated: {i.get('updatedAt', 'unknown')})"
        for i in stale[:20]
    ) or "(none)"

    known_text = "\n".join(f"- {ki}" for ki in known_issues[:20]) or "(none)"

    risk_text = "\n".join(
        f"- [{r['task_id']}] {r['risk']}" for r in risk_flags[:20]
    ) or "(none)"

    blocked_tasks_text = "\n".join(
        f"- [{r.get('task_id', '?')}] status={r.get('status', '?')} task_type={r.get('task_type', '?')} agent={r.get('agent', '?')}"
        for r in blocked_tasks[:20]
    ) or "(none)"

    repo_gaps_text = "\n".join(f"- {gap}" for gap in repo_gaps[:20]) or "(none)"

    blocked_issues_text = "\n".join(
        f"- #{i.get('number')}: {i.get('title')}"
        for i in blocked_issues[:20]
    ) or "(none)"

    completions_text = _recent_completions_summary(records)

    # Load agent scorer findings for richer remediation signals
    scorer_text = "(none)"
    try:
        artifact = scorer_findings_path(root)
        if artifact.exists():
            payload = json.loads(artifact.read_text(encoding="utf-8"))
            findings = payload.get("findings", [])
            scorer_text = "\n".join(
                f"- {f.get('title_hint', '?')} (agent={f.get('agent', '?')}, cause={f.get('degradation_cause', '?')}, rate={f.get('metrics', {}).get('rate', '?')})"
                for f in findings[:10]
            ) or "(none)"
    except Exception:
        pass

    open_text = "\n".join(
        f"- #{i.get('number')}: {i.get('title')}" for i in open_issues[:30]
    ) or "(none)"

    # Build anti-repeat context from explicit skips so the groomer avoids
    # creating more issues similar to recently-rejected plan items.
    explicit_skips = [s for s in repo_skip_signals if s.get("skip_type") == "explicit"]
    if explicit_skips:
        skipped_issue_nums: set[int] = set()
        for s in explicit_skips:
            skipped_issue_nums.update(s.get("issues", []))
        # Match skipped issue numbers to open issue titles for LLM context
        skip_lines = []
        for oi in open_issues:
            num = oi.get("number")
            if num in skipped_issue_nums:
                penalty = _skip_penalty_for_issue(num, repo_skip_signals)
                skip_lines.append(
                    f"- #{num}: {oi.get('title', '?')} (skip penalty: {penalty:.1f})"
                )
        skip_signal_text = "\n".join(skip_lines) if skip_lines else "(none)"
        if skip_lines:
            skip_signal_text += (
                "\nThese issues were in plans that the operator explicitly skipped. "
                "Avoid creating new issues that overlap with these topics unless "
                "context has clearly changed."
            )
    else:
        skip_signal_text = "(none)"

    prompt = ANALYSIS_PROMPT.format(
        num_issues=num_issues,
        adoption_min=adoption_min,
        current_backlog=current_backlog,
        target_depth=target_depth,
        stale_issues=stale_text,
        known_issues=known_text,
        risk_flags=risk_text,
        blocked_tasks=blocked_tasks_text,
        repo_gaps=repo_gaps_text,
        blocked_issues=blocked_issues_text,
        objectives_context=objectives_context,
        readme_goal=readme_goal,
        north_star=north_star,
        strategy_context=strategy_context,
        planning_principles=planning_principles,
        production_feedback=production_feedback,
        product_inspection=product_inspection,
        research_context=research_context,
        adoption_signals=adoption_signals,
        evaluation_rubric=evaluation_rubric or "(no domain rubric defined — using generic evaluation criteria)",
        scorer_findings=scorer_text,
        completions=completions_text,
        open_issues=open_text,
        skip_signal_context=skip_signal_text,
    )

    # Bootstrap issues consume slots first, but the LLM is still called for any
    # remaining slots so adoption-facing issues are not silenced when bootstrap
    # docs need creating.
    proposed: list[dict] = list(bootstrap_issues[:num_issues])
    remaining_slots = max(num_issues - len(proposed), 0)
    if remaining_slots > 0:
        # 7. Call LLM for the remaining slots
        try:
            raw = _call_haiku(prompt)
        except Exception as e:
            if proposed:
                print(f"  Analysis failed after bootstrap issue synthesis: {e}")
            else:
                print(f"  Analysis failed: {e}")
                return {"status": "error", "created": 0, "skipped": 0, "error": str(e)}
        else:
            try:
                llm_proposed = _parse_issues(raw)
            except Exception as e:
                if proposed:
                    print(f"  Failed to parse LLM response after bootstrap issue synthesis: {e}\n  Raw: {raw[:300]}")
                else:
                    print(f"  Failed to parse response: {e}\n  Raw: {raw[:300]}")
                    return {"status": "error", "created": 0, "skipped": 0, "error": str(e)}
            else:
                if not isinstance(llm_proposed, list):
                    if proposed:
                        print(f"  Unexpected LLM response format after bootstrap issue synthesis:\n  {raw[:300]}")
                    else:
                        print(f"  Unexpected response format:\n  {raw[:300]}")
                        return {"status": "error", "created": 0, "skipped": 0, "error": "unexpected LLM response format"}
                else:
                    proposed.extend(llm_proposed[:remaining_slots])

    # 8. Dedup and create issues
    created_urls: list[str] = []
    skipped: list[str] = []

    for issue in proposed[:MAX_ISSUES_PER_RUN]:
        title = (issue.get("title") or "").strip()
        labels = [str(l) for l in issue.get("labels", []) if l]
        if "bot-generated" not in labels:
            labels.append("bot-generated")
        outcome_section = format_outcome_checks_section(
            get_repo_outcome_check_ids(cfg, github_slug, issue_labels=labels)
        )
        body = (issue.get("body") or "").strip() + outcome_section
        priority = issue.get("priority", "prio:normal")

        # Add priority label
        if priority not in labels:
            labels.append(priority)

        if not title:
            continue

        # Local dedup against existing open issues
        if _is_duplicate(title, open_titles):
            print(f"  Skip (similar to open): {title!r}")
            skipped.append(title)
            continue

        # Remote dedup via gh search
        if _open_issue_exists(github_slug, title):
            print(f"  Skip (exists remotely): {title!r}")
            skipped.append(title)
            continue

        try:
            url = _create_issue(github_slug, title, body, labels)
            _set_issue_backlog(cfg, github_slug, url)
            print(f"  Created: {url}")
            created_urls.append(url)
            open_titles.append(title)  # Prevent self-duplication within batch
        except Exception as e:
            print(f"  Failed to create {title!r}: {e}")
            skipped.append(title)

    if created_urls:
        status = "created"
    elif skipped:
        status = "skipped"
    else:
        status = "error"

    result = {
        "status": status,
        "created": len(created_urls),
        "skipped": len(skipped),
        "cleaned": len(cleaned),
        "urls": created_urls,
    }
    if status == "error":
        result["error"] = "LLM returned no usable issues"
    return result


def run():
    cfg = load_config()
    with job_lock(cfg, "backlog_groomer") as acquired:
        if not acquired:
            print("Backlog groomer already running; skipping overlapping cron invocation.")
            return

        repos = _resolve_repos(cfg)

        if not repos:
            print("No repos configured; nothing to groom.")
            return

        print(f"Backlog groomer starting for {len(repos)} repo(s).")

        all_created = 0
        all_skipped = 0
        all_cleaned = 0
        status_counts = {"created": 0, "skipped": 0, "no-data": 0, "error": 0, "dormant": 0}
        summaries = []
        notify = False

        for github_slug, repo_path in repos:
            if is_dispatcher_only_repo(cfg, github_slug):
                print(f"  Skipping {github_slug}: automation_mode=dispatcher_only")
                status_counts["skipped"] = status_counts.get("skipped", 0) + 1
                summaries.append(f"{github_slug}: skipped (automation_mode=dispatcher_only)")
                continue
            cadence_days = _repo_groomer_cadence_days(cfg, github_slug)
            due, reason = is_due(
                cfg,
                "backlog_groomer",
                github_slug,
                cadence_hours=cadence_days * 24.0,
            )
            if not due:
                print(f"  Skipping {github_slug}: {reason}")
                status_key = "dormant" if reason == "dormant" else "skipped"
                status_counts[status_key] = status_counts.get(status_key, 0) + 1
                summaries.append(f"{github_slug}: skipped ({reason})")
                continue

            result = groom_repo(cfg, github_slug, repo_path)
            status = result.get("status", "error")
            status_counts[status] = status_counts.get(status, 0) + 1
            all_created += result.get("created", 0)
            all_skipped += result.get("skipped", 0)
            all_cleaned += result.get("cleaned", 0)
            if status in {"created", "error"} or result.get("cleaned", 0) > 0:
                notify = True
            if status in {"created", "skipped"}:
                record_run(cfg, "backlog_groomer", github_slug)

            if status == "created":
                summaries.append(f"{github_slug}: {result.get('created', 0)} created, {result.get('skipped', 0)} skipped, {result.get('cleaned', 0)} cleaned")
            elif status == "skipped":
                summaries.append(f"{github_slug}: skipped ({result.get('skipped', 0)} duplicate/failed creates, {result.get('cleaned', 0)} cleaned)")
            elif status == "no-data":
                summaries.append(f"{github_slug}: no-data ({result.get('cleaned', 0)} cleaned)")
            else:
                summaries.append(f"{github_slug}: error ({result.get('error', 'unknown error')})")

        summary = (
            f"Backlog Groomer complete\n"
            f"Issues created: {all_created} | Skipped: {all_skipped} | Cleaned: {all_cleaned}\n"
            f"Repo statuses: created={status_counts['created']} skipped={status_counts['skipped']} "
            f"no-data={status_counts['no-data']} error={status_counts['error']} dormant={status_counts['dormant']}\n"
            + "\n".join(summaries)
        )
        print(f"\n{summary}")
        if notify:
            _send_telegram(cfg, summary)


if __name__ == "__main__":
    run()
