"""Weekly strategic planner.

Reads product context (README Goal section, last 30 git commits, issue metrics)
from each repository, generates a prioritized 5-task sprint plan using Claude
Sonnet, posts it to Telegram for human approval, and conditionally creates
GitHub issues upon confirmation.

On approval, created issues are automatically set to Status=Ready on the
project board, triggering immediate dispatch to agents. One tap on Telegram
starts an entire sprint.

Runs weekly on Sundays at 20:00.  Approval gate is mandatory — no issues are
created without explicit human confirmation via Telegram reply. If no response
within 24 hours, the plan is skipped and a fresh one is generated next week.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

from orchestrator.paths import load_config, runtime_paths
from orchestrator.agent_scorer import load_recent_metrics
from orchestrator.gh_project import query_project, set_item_status, edit_issue_labels, ensure_labels

PLAN_SIZE = 5
ANALYSIS_MODEL = "sonnet"
METRICS_WINDOW_DAYS = 30
SIMILARITY_THRESHOLD = 0.75
POLL_INTERVAL_SECONDS = 300   # 5 minutes
APPROVAL_TIMEOUT_HOURS = 24
APPROVAL_KEYWORDS = {"yes", "approve", "approved", "ok", "go", "lgtm"}
REJECTION_KEYWORDS = {"no", "reject", "skip", "cancel", "nope"}


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


def _open_issues_summary(repo: str) -> str:
    """Return formatted list of open issues for dedup context."""
    raw = _gh(["issue", "list", "--repo", repo, "--state", "open",
               "--json", "number,title,labels", "--limit", "50"])
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
        labels = ", ".join(l.get("name", "") for l in i.get("labels", []))
        lbl = f" [{labels}]" if labels else ""
        lines.append(f"- #{i.get('number')}: {i.get('title')}{lbl}")
    return "\n".join(lines)


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
Your job is to create next week's sprint plan: exactly {plan_size} prioritized,
atomic, executable tasks that move the product forward.

Context about this repository:

--- Product Goal ---
{readme_goal}

--- Last 30 git commits ---
{git_log}

--- Issue metrics ---
Open issues: {open_count} | Recently closed: {closed_count} | Blocked: {blocked_count}

--- Recent task completion metrics ---
{metrics_summary}

--- Currently open issues (for dedup — do NOT duplicate these) ---
{open_issues}

Rules:
- Each task must be atomic and executable by an AI agent in a single session
- Tasks must NOT be vague epics — they should have clear, testable success criteria
- Do NOT duplicate existing open issues
- Order by priority (most impactful first)
- Include a mix of: feature work, bug fixes, improvements, and infrastructure
- Issue body must use ## Goal, ## Success Criteria, ## Constraints sections

Return ONLY a JSON array (no markdown fences, no commentary) of exactly {plan_size} objects.
Each object must have:
  "title"      - concise GitHub issue title under 70 chars
  "goal"       - one-paragraph goal statement
  "task_type"  - one of: implementation, debugging, architecture, research, docs
  "priority"   - one of: prio:high, prio:normal, prio:low
  "rationale"  - one sentence explaining why this task matters this week
  "body"       - structured body with ## Goal\\n...\\n## Success Criteria\\n...\\n## Constraints\\n- Prefer minimal diffs
  "labels"     - JSON array of label strings (choose from: enhancement, bug, tech-debt, agent-os)

Return ONLY the JSON array."""


def _call_sonnet(prompt: str) -> str:
    claude_bin = os.environ.get("CLAUDE_BIN", "claude")
    result = subprocess.run(
        [claude_bin, "-p", prompt, "--model", ANALYSIS_MODEL],
        capture_output=True, text=True, timeout=180,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Claude exit {result.returncode}: {result.stderr[:300]}")
    return result.stdout.strip()


def _parse_plan(text: str) -> list[dict]:
    """Parse JSON array from Claude response, stripping markdown fences if present."""
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(text)


# ---------------------------------------------------------------------------
# Telegram approval gate
# ---------------------------------------------------------------------------

def _send_telegram(cfg: dict, text: str) -> int | None:
    """Send a Telegram message. Return message_id on success, None otherwise."""
    token = str(cfg.get("telegram_bot_token", "")).strip()
    chat_id = str(cfg.get("telegram_chat_id", "")).strip()
    if not token or not chat_id:
        return None
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    result = subprocess.run(
        ["curl", "-sS", "-X", "POST", url,
         "-d", f"chat_id={chat_id}",
         "--data-urlencode", f"text={text}"],
        capture_output=True, text=True, timeout=20,
    )
    try:
        data = json.loads(result.stdout)
        if data.get("ok"):
            return data.get("result", {}).get("message_id")
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def _format_plan_message(plan: list[dict], repo: str) -> str:
    """Format the sprint plan for Telegram display."""
    lines = [
        f"📋 Weekly Sprint Plan — {repo}",
        f"Generated: {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
    ]
    for i, task in enumerate(plan, 1):
        priority = task.get("priority", "prio:normal")
        prio_icon = {"prio:high": "🔴", "prio:normal": "🟡", "prio:low": "🟢"}.get(priority, "⚪")
        lines.append(f"{i}. {prio_icon} [{task.get('task_type', '?')}] {task.get('title', '?')}")
        lines.append(f"   {task.get('rationale', '')}")
        lines.append("")
    lines.append("Reply YES to approve and create issues.")
    lines.append("Reply NO to skip this week.")
    lines.append(f"Auto-skip in {APPROVAL_TIMEOUT_HOURS}h if no response.")
    return "\n".join(lines)


def _poll_approval(
    cfg: dict,
    plan_message_id: int,
    timeout_hours: float = APPROVAL_TIMEOUT_HOURS,
    poll_interval: int = POLL_INTERVAL_SECONDS,
) -> bool:
    """Poll Telegram for approval reply. Returns True if approved."""
    token = str(cfg.get("telegram_bot_token", "")).strip()
    chat_id = str(cfg.get("telegram_chat_id", "")).strip()
    if not token or not chat_id:
        print("No Telegram credentials; cannot poll for approval.")
        return False

    deadline = time.time() + timeout_hours * 3600
    last_update_id = 0

    # Get current update_id to skip old messages
    try:
        init = subprocess.run(
            ["curl", "-sS",
             f"https://api.telegram.org/bot{token}/getUpdates?offset=-1&limit=1"],
            capture_output=True, text=True, timeout=15,
        )
        data = json.loads(init.stdout)
        for u in data.get("result", []):
            last_update_id = max(last_update_id, u.get("update_id", 0))
    except Exception:
        pass

    print(f"Polling for approval (timeout: {timeout_hours}h, interval: {poll_interval}s)...")

    while time.time() < deadline:
        time.sleep(poll_interval)

        try:
            url = (
                f"https://api.telegram.org/bot{token}/getUpdates"
                f"?offset={last_update_id + 1}&timeout=10"
            )
            result = subprocess.run(
                ["curl", "-sS", url],
                capture_output=True, text=True, timeout=30,
            )
            data = json.loads(result.stdout)
        except Exception:
            continue

        for update in data.get("result", []):
            uid = update.get("update_id", 0)
            last_update_id = max(last_update_id, uid)

            msg = update.get("message", {})
            msg_chat_id = str(msg.get("chat", {}).get("id", ""))
            if msg_chat_id != str(chat_id):
                continue

            text = (msg.get("text") or "").strip().lower()

            # Accept reply to our message or standalone keyword
            reply_to = msg.get("reply_to_message", {})
            is_reply = reply_to.get("message_id") == plan_message_id

            if text in APPROVAL_KEYWORDS or (is_reply and text in APPROVAL_KEYWORDS):
                print(f"Approval received: {text!r}")
                return True
            if text in REJECTION_KEYWORDS or (is_reply and text in REJECTION_KEYWORDS):
                print(f"Rejection received: {text!r}")
                return False

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

    if github_repos and owner:
        for key, slug in github_repos.items():
            full_slug = f"{owner}/{slug}" if "/" not in slug else slug
            for rp in allowed:
                rp = Path(rp).expanduser()
                if rp.name == key or rp.name == slug:
                    repos.append((full_slug, rp))
                    break
            else:
                if allowed:
                    repos.append((full_slug, Path(allowed[0]).expanduser()))

    for pv in cfg.get("github_projects", {}).values():
        if not isinstance(pv, dict):
            continue
        for rc in pv.get("repos", []):
            gh_repo = rc.get("github_repo", "")
            local = rc.get("repo", rc.get("path", ""))
            if gh_repo and local:
                repos.append((gh_repo, Path(local).expanduser()))

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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def plan_repo(cfg: dict, github_slug: str, repo_path: Path) -> list[dict] | None:
    """Generate a sprint plan for one repo. Returns the plan or None on failure."""
    print(f"\n--- Planning {github_slug} ---")

    # 1. Read product context
    readme_goal = _read_readme_goal(repo_path)
    print(f"  README goal: {len(readme_goal)} chars")

    # 2. Last 30 git commits
    git_log = _git_log(repo_path, n=30)
    print(f"  Git log: {git_log.count(chr(10)) + 1} commits")

    # 3. Issue metrics
    counts = _issue_counts(github_slug)
    print(f"  Issues — open: {counts['open']}, closed: {counts['closed']}, blocked: {counts['blocked']}")

    # 4. Recent task metrics
    metrics_summary = _recent_metrics_summary(cfg)

    # 5. Open issues for dedup
    open_issues = _open_issues_summary(github_slug)

    # 6. Build prompt
    prompt = PLAN_PROMPT.format(
        plan_size=PLAN_SIZE,
        readme_goal=readme_goal,
        git_log=git_log,
        open_count=counts["open"],
        closed_count=counts["closed"],
        blocked_count=counts["blocked"],
        metrics_summary=metrics_summary,
        open_issues=open_issues,
    )

    # 7. Call Sonnet
    try:
        raw = _call_sonnet(prompt)
    except Exception as e:
        print(f"  Plan generation failed: {e}")
        return None

    try:
        plan = _parse_plan(raw)
    except Exception as e:
        print(f"  Failed to parse plan: {e}\n  Raw: {raw[:300]}")
        return None

    if not isinstance(plan, list):
        print(f"  Unexpected response format:\n  {raw[:300]}")
        return None

    print(f"  Generated {len(plan)} tasks")
    return plan[:PLAN_SIZE]


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


def create_plan_issues(
    cfg: dict, github_slug: str, plan: list[dict],
) -> tuple[list[str], list[str]]:
    """Create GitHub issues from an approved plan, then set them to Ready on the project board."""
    created_urls: list[str] = []
    skipped: list[str] = []

    for task in plan:
        title = (task.get("title") or "").strip()
        body = (task.get("body") or "").strip()
        labels = [str(l) for l in task.get("labels", []) if l]

        if not title:
            continue

        if _open_issue_exists(github_slug, title):
            print(f"  Skip (duplicate): {title!r}")
            skipped.append(title)
            continue

        try:
            url = _create_issue(github_slug, title, body, labels)
            print(f"  Created: {url}")
            created_urls.append(url)
        except Exception as e:
            print(f"  Failed to create {title!r}: {e}")
            skipped.append(title)

    # Move all created issues to Ready — triggers dispatch on next cycle
    if created_urls:
        print(f"\n  Setting {len(created_urls)} issue(s) to Ready on project board...")
        _set_issues_ready(cfg, github_slug, created_urls)

    return created_urls, skipped


def run():
    cfg = load_config()
    repos = _resolve_repos(cfg)

    if not repos:
        print("No repos configured; nothing to plan.")
        return

    print(f"Strategic planner starting for {len(repos)} repo(s).")

    for github_slug, repo_path in repos:
        # Phase 1: Generate plan
        plan = plan_repo(cfg, github_slug, repo_path)
        if not plan:
            print(f"  No plan generated for {github_slug}; skipping.")
            continue

        # Phase 2: Post to Telegram and wait for approval
        plan_text = _format_plan_message(plan, github_slug)
        print(f"\n{plan_text}\n")

        msg_id = _send_telegram(cfg, plan_text)
        if msg_id is None:
            print("  Failed to send plan to Telegram (or no credentials).")
            print("  Skipping issue creation — approval gate is mandatory.")
            continue

        # Phase 3: Wait for human approval
        approved = _poll_approval(cfg, msg_id)

        if not approved:
            skip_msg = f"⏭️ Sprint plan for {github_slug} was not approved. Skipping this week."
            print(skip_msg)
            _send_telegram(cfg, skip_msg)
            continue

        # Phase 4: Create issues (only on approval)
        print(f"\n  Creating issues for approved plan ({github_slug})...")
        created_urls, skipped = create_plan_issues(cfg, github_slug, plan)

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
