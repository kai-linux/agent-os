"""Daily Telegram digest for the last 24 hours of orchestrator activity."""
from __future__ import annotations

import json
import re
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from orchestrator.paths import load_config, runtime_paths

WINDOW_HOURS = 24
MAX_TASKS_PER_SECTION = 3
MAX_AGENTS = 5
QUEUE_TASK_RE = re.compile(r"Processing task:\s+([A-Za-z0-9._-]+)")
QUEUE_STATUS_RE = re.compile(r"Worker status from\s+([A-Za-z0-9._-]+):\s+([A-Za-z0-9._-]+)")


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, flags=re.DOTALL)
    if not match:
        return {}, text
    meta = yaml.safe_load(match.group(1)) or {}
    return meta, match.group(2)


def _parse_timestamp(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _goal_line(body: str) -> str:
    lines = body.splitlines()
    for idx, line in enumerate(lines):
        if line.strip().lower() == "# goal":
            for candidate in lines[idx + 1:]:
                candidate = candidate.strip()
                if candidate:
                    return candidate
    return ""


def _task_slug(task_id: str) -> str:
    parts = task_id.split("-")
    if len(parts) > 3:
        return "-".join(parts[3:])
    return task_id


def _task_label(path: Path, meta: dict, body: str) -> str:
    issue_number = meta.get("github_issue_number")
    task_id = str(meta.get("task_id") or path.stem)
    goal = _goal_line(body)
    slug = _task_slug(task_id).replace("-", " ").strip()
    if issue_number:
        return f"#{issue_number} {goal or slug}".strip()
    return goal or task_id


def _event_timestamp(path: Path, meta: dict) -> datetime:
    for key in (
        "completed_at",
        "blocked_at",
        "escalated_at",
        "result_timestamp",
        "updated_at",
        "timestamp",
    ):
        parsed = _parse_timestamp(meta.get(key))
        if parsed is not None:
            return parsed
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def load_recent_mailbox_entries(directory: Path, status: str, cutoff: datetime) -> list[dict]:
    entries: list[dict] = []
    if not directory.exists():
        return entries

    for path in sorted(directory.glob("*.md")):
        if path.name.endswith("-escalation.md"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        meta, body = _parse_frontmatter(text)
        event_time = _event_timestamp(path, meta)
        if event_time < cutoff:
            continue
        entries.append(
            {
                "task_id": str(meta.get("task_id") or path.stem),
                "label": _task_label(path, meta, body),
                "status": status,
                "timestamp": event_time,
            }
        )

    entries.sort(key=lambda item: item["timestamp"], reverse=True)
    return entries


def parse_queue_summary_log(log_file: Path) -> dict[str, dict]:
    details: dict[str, dict] = {}
    if not log_file.exists():
        return details

    current_task: str | None = None
    for raw_line in log_file.read_text(encoding="utf-8", errors="replace").splitlines():
        task_match = QUEUE_TASK_RE.search(raw_line)
        if task_match:
            current_task = task_match.group(1)
            details.setdefault(current_task, {})
            continue

        if not current_task:
            continue

        status_match = QUEUE_STATUS_RE.search(raw_line)
        if status_match:
            details.setdefault(current_task, {})
            details[current_task]["agent"] = status_match.group(1)
            details[current_task]["worker_status"] = status_match.group(2)
            continue

        if "Final queue state:" in raw_line:
            details.setdefault(current_task, {})
            details[current_task]["queue_state"] = raw_line.split("Final queue state:", 1)[1].strip()

    return details


def compute_agent_success_rates(entries: list[dict], queue_details: dict[str, dict]) -> dict[str, dict]:
    counts: dict[str, dict] = defaultdict(lambda: {"total": 0, "successes": 0})
    for entry in entries:
        task_info = queue_details.get(entry["task_id"], {})
        agent = str(task_info.get("agent") or "unknown")
        counts[agent]["total"] += 1
        if entry["status"] == "complete":
            counts[agent]["successes"] += 1

    return {
        agent: {
            "total": stats["total"],
            "successes": stats["successes"],
            "rate": (stats["successes"] / stats["total"]) if stats["total"] else 0.0,
        }
        for agent, stats in counts.items()
    }


def _configured_repos(cfg: dict) -> list[str]:
    repos: set[str] = set()
    for project_cfg in cfg.get("github_projects", {}).values():
        if not isinstance(project_cfg, dict):
            continue
        for repo_cfg in project_cfg.get("repos", []):
            repo = repo_cfg.get("github_repo")
            if repo:
                repos.add(str(repo))
    github_repo = cfg.get("github_repo")
    if github_repo:
        repos.add(str(github_repo))
    return sorted(repos)


def _gh_json(args: list[str]) -> list[dict]:
    result = subprocess.run(["gh", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "gh command failed")
    return json.loads(result.stdout or "[]")


def collect_pr_activity(cfg: dict, cutoff: datetime) -> dict[str, int]:
    repos = _configured_repos(cfg)
    if not repos:
        return {"created": 0, "merged": 0}

    created = 0
    merged = 0
    for repo in repos:
        try:
            all_prs = _gh_json(
                [
                    "pr",
                    "list",
                    "--repo",
                    repo,
                    "--state",
                    "all",
                    "--limit",
                    "200",
                    "--json",
                    "title,headRefName,createdAt,mergedAt",
                ]
            )
        except Exception:
            continue

        for pr in all_prs:
            is_agent_pr = str(pr.get("headRefName", "")).startswith("agent/") or str(pr.get("title", "")).startswith("Agent:")
            if not is_agent_pr:
                continue
            created_at = _parse_timestamp(pr.get("createdAt"))
            merged_at = _parse_timestamp(pr.get("mergedAt"))
            if created_at and created_at >= cutoff:
                created += 1
            if merged_at and merged_at >= cutoff:
                merged += 1

    return {"created": created, "merged": merged}


def format_digest_message(
    completed: list[dict],
    blocked: list[dict],
    escalated: list[dict],
    agent_rates: dict[str, dict],
    pr_activity: dict[str, int],
    now: datetime,
) -> str:
    total_activity = len(completed) + len(blocked) + len(escalated) + pr_activity["created"] + pr_activity["merged"]
    if total_activity == 0:
        return "📬 Daily Digest\nℹ️ No activity yesterday."

    lines = [
        "📬 Daily Digest",
        f"Window: last {WINDOW_HOURS}h ending {now.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"✅ Completed: {len(completed)}",
    ]

    for entry in completed[:MAX_TASKS_PER_SECTION]:
        lines.append(f"- {entry['label']}")
    if len(completed) > MAX_TASKS_PER_SECTION:
        lines.append(f"- +{len(completed) - MAX_TASKS_PER_SECTION} more")

    lines.append(f"⏸️ Blocked: {len(blocked)}")
    for entry in blocked[:MAX_TASKS_PER_SECTION]:
        lines.append(f"- {entry['label']}")
    if len(blocked) > MAX_TASKS_PER_SECTION:
        lines.append(f"- +{len(blocked) - MAX_TASKS_PER_SECTION} more")

    lines.append(f"🛑 Escalated: {len(escalated)}")
    for entry in escalated[:MAX_TASKS_PER_SECTION]:
        lines.append(f"- {entry['label']}")
    if len(escalated) > MAX_TASKS_PER_SECTION:
        lines.append(f"- +{len(escalated) - MAX_TASKS_PER_SECTION} more")

    lines.append("🤖 Success Rates")
    ranked_agents = sorted(
        agent_rates.items(),
        key=lambda item: (-item[1]["total"], item[0]),
    )[:MAX_AGENTS]
    if ranked_agents:
        for agent, stats in ranked_agents:
            pct = round(stats["rate"] * 100)
            lines.append(f"- {agent}: {pct}% ({stats['successes']}/{stats['total']})")
    else:
        lines.append("- No routed tasks")

    lines.append("🔀 PR Activity")
    lines.append(f"- Created: {pr_activity['created']}")
    lines.append(f"- Merged: {pr_activity['merged']}")

    return "\n".join(lines[:39])


def _send_telegram(cfg: dict, text: str):
    token = str(cfg.get("telegram_bot_token", "")).strip()
    chat_id = str(cfg.get("telegram_chat_id", "")).strip()
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    subprocess.run(
        [
            "curl",
            "-sS",
            "-X",
            "POST",
            url,
            "-d",
            f"chat_id={chat_id}",
            "--data-urlencode",
            f"text={text}",
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )


def run():
    cfg = load_config()
    paths = runtime_paths(cfg)
    now = datetime.now(tz=timezone.utc)
    cutoff = now - timedelta(hours=WINDOW_HOURS)

    completed = load_recent_mailbox_entries(paths["DONE"], "complete", cutoff)
    blocked = load_recent_mailbox_entries(paths["BLOCKED"], "blocked", cutoff)
    escalated = load_recent_mailbox_entries(paths["ESCALATED"], "escalated", cutoff)
    queue_details = parse_queue_summary_log(paths["QUEUE_SUMMARY_LOG"])
    agent_rates = compute_agent_success_rates(completed + blocked + escalated, queue_details)
    pr_activity = collect_pr_activity(cfg, cutoff)

    message = format_digest_message(completed, blocked, escalated, agent_rates, pr_activity, now)
    print(message)
    _send_telegram(cfg, message)


if __name__ == "__main__":
    run()
