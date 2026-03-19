"""Weekly backlog groomer.

Reviews each repository's open issues, recent task completions, CODEBASE.md
Known Issues section, and risk flags from completed tasks to identify gaps
and technical debt.  Creates 3-5 targeted, scoped improvement tasks per repo
with priorities assigned. Issues land in Backlog for the strategic planner
to review and promote to Ready.

Runs weekly on Saturday at 20:00 (one day before the strategic planner).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

from orchestrator.paths import load_config, runtime_paths
from orchestrator.agent_scorer import load_recent_metrics
from orchestrator.gh_project import ensure_labels
from orchestrator.trust import is_trusted

WINDOW_DAYS = 30
STALE_DAYS = 30
MAX_ISSUES_PER_REPO = 5
ANALYSIS_MODEL = "haiku"
SIMILARITY_THRESHOLD = 0.75  # Title similarity threshold for dedup


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
        "--json", "number,title,createdAt,updatedAt,labels,author",
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

ANALYSIS_PROMPT = """You are an AI agent system analyst performing weekly backlog grooming.
Review the data below and create exactly {num_issues} targeted, atomic improvement tasks.

Focus on:
1. Stale issues (open >30 days with no activity) — suggest closing or scoping down
2. Known Issues from CODEBASE.md that have no linked GitHub issue — create one
3. Risk flags from recently completed tasks — create follow-up mitigation tasks
4. General technical debt or gaps visible from recent completions

Rules:
- Each task must be atomic and clearly scoped (one specific thing to fix/improve)
- Do NOT propose re-opening or modifying existing issues
- Do NOT create tasks that duplicate existing open issues
- Issue body must use ## Goal, ## Success Criteria, ## Constraints sections
- Order by priority (most impactful first)
- Assign priority based on risk and impact — security/data-loss risks are high,
  tech-debt cleanup is normal, nice-to-haves are low

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

--- Recent task completions (last 30 days) ---
{completions}

--- Currently open issues (for dedup reference) ---
{open_issues}

Return ONLY the JSON array."""


def _call_haiku(prompt: str) -> str:
    claude_bin = os.environ.get("CLAUDE_BIN", "claude")
    result = subprocess.run(
        [claude_bin, "-p", prompt, "--model", ANALYSIS_MODEL],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Claude exit {result.returncode}: {result.stderr[:300]}")
    return result.stdout.strip()


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
    # Ensure priority and ready labels exist
    prio_labels = [l for l in labels if l.startswith("prio:")]
    custom_labels = prio_labels + ["ready"]
    try:
        ensure_labels(repo, custom_labels)
    except Exception:
        pass
    cmd = ["gh", "issue", "create", "--repo", repo, "--title", title, "--body", body]
    for label in labels:
        cmd += ["--label", label]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"gh issue create failed: {result.stderr.strip()}")
    return result.stdout.strip()



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
    # Try github_repos mapping first
    github_repos = cfg.get("github_repos", {})
    owner = cfg.get("github_owner", "")
    allowed = cfg.get("allowed_repos", [])

    if github_repos and owner:
        for key, slug in github_repos.items():
            full_slug = f"{owner}/{slug}" if "/" not in slug else slug
            # Find matching allowed_repo path
            for rp in allowed:
                rp = Path(rp).expanduser()
                if rp.name == key or rp.name == slug:
                    repos.append((full_slug, rp))
                    break
            else:
                # Use first allowed repo or guess path
                if allowed:
                    repos.append((full_slug, Path(allowed[0]).expanduser()))

    # Also check github_projects for repo configs
    for pv in cfg.get("github_projects", {}).values():
        if not isinstance(pv, dict):
            continue
        for rc in pv.get("repos", []):
            gh_repo = rc.get("github_repo", "")
            local = rc.get("repo", rc.get("path", ""))
            if gh_repo and local:
                repos.append((gh_repo, Path(local).expanduser()))

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
    open_titles = [i.get("title", "") for i in open_issues]
    stale = _stale_issues(open_issues)
    print(f"  Open issues: {len(open_issues)}, stale (>{STALE_DAYS}d): {len(stale)}")

    # 2. Recent completions from metrics
    root = Path(cfg.get("root_dir", ".")).expanduser()
    metrics_file = root / "runtime" / "metrics" / "agent_stats.jsonl"
    records = load_recent_metrics(metrics_file, window_days=WINDOW_DAYS)
    print(f"  Completions (last {WINDOW_DAYS}d): {len(records)}")

    # 3. CODEBASE.md Known Issues
    known_issues = _parse_known_issues(repo_path)
    print(f"  Known Issues entries: {len(known_issues)}")

    # 4. Risk flags from .agent_result.md
    risk_flags = _find_risk_flags(cfg)
    print(f"  Risk flags found: {len(risk_flags)}")

    # Skip if no data to analyze
    if not stale and not known_issues and not risk_flags and not records:
        print("  No data to analyze, skipping.")
        return {"created": 0, "skipped": 0}

    # 5. Determine how many issues to propose (3-5, based on data richness)
    data_signals = len(stale) + len(known_issues) + len(risk_flags)
    num_issues = min(MAX_ISSUES_PER_REPO, max(3, data_signals))

    # 6. Build prompt
    stale_text = "\n".join(
        f"- #{i.get('number')}: {i.get('title')} (updated: {i.get('updatedAt', 'unknown')})"
        for i in stale[:20]
    ) or "(none)"

    known_text = "\n".join(f"- {ki}" for ki in known_issues[:20]) or "(none)"

    risk_text = "\n".join(
        f"- [{r['task_id']}] {r['risk']}" for r in risk_flags[:20]
    ) or "(none)"

    completions_text = _recent_completions_summary(records)

    open_text = "\n".join(
        f"- #{i.get('number')}: {i.get('title')}" for i in open_issues[:30]
    ) or "(none)"

    prompt = ANALYSIS_PROMPT.format(
        num_issues=num_issues,
        stale_issues=stale_text,
        known_issues=known_text,
        risk_flags=risk_text,
        completions=completions_text,
        open_issues=open_text,
    )

    # 7. Call LLM
    try:
        raw = _call_haiku(prompt)
    except Exception as e:
        print(f"  Analysis failed: {e}")
        return {"created": 0, "skipped": 0, "error": str(e)}

    try:
        proposed = _parse_issues(raw)
    except Exception as e:
        print(f"  Failed to parse response: {e}\n  Raw: {raw[:300]}")
        return {"created": 0, "skipped": 0, "error": str(e)}

    if not isinstance(proposed, list):
        print(f"  Unexpected response format:\n  {raw[:300]}")
        return {"created": 0, "skipped": 0}

    # 8. Dedup and create issues
    created_urls: list[str] = []
    skipped: list[str] = []

    for issue in proposed[:MAX_ISSUES_PER_REPO]:
        title = (issue.get("title") or "").strip()
        body = (issue.get("body") or "").strip()
        labels = [str(l) for l in issue.get("labels", []) if l]
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
            print(f"  Created: {url}")
            created_urls.append(url)
            open_titles.append(title)  # Prevent self-duplication within batch
        except Exception as e:
            print(f"  Failed to create {title!r}: {e}")
            skipped.append(title)

    return {"created": len(created_urls), "skipped": len(skipped), "urls": created_urls}


def run():
    cfg = load_config()
    repos = _resolve_repos(cfg)

    if not repos:
        print("No repos configured; nothing to groom.")
        return

    print(f"Backlog groomer starting for {len(repos)} repo(s).")

    all_created = 0
    all_skipped = 0
    summaries = []

    for github_slug, repo_path in repos:
        result = groom_repo(cfg, github_slug, repo_path)
        all_created += result.get("created", 0)
        all_skipped += result.get("skipped", 0)
        summaries.append(f"{github_slug}: {result.get('created', 0)} created, {result.get('skipped', 0)} skipped")

    summary = (
        f"Backlog Groomer complete\n"
        f"Issues created: {all_created} | Skipped: {all_skipped}\n"
        + "\n".join(summaries)
    )
    print(f"\n{summary}")
    _send_telegram(cfg, summary)


if __name__ == "__main__":
    run()
