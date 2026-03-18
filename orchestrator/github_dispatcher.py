from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import yaml

from orchestrator.paths import load_config, runtime_paths
from orchestrator.gh_project import (
    get_ready_items,
    set_item_status,
    edit_issue_labels,
    add_issue_comment,
    list_ready_issues,
)
from orchestrator.task_formatter import format_task
from orchestrator.trust import is_trusted


SECTION_RE = re.compile(r"^##\s+(.+?)\n(.*?)(?=^##\s+|\Z)", re.MULTILINE | re.DOTALL)


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")[:50] or "task"


def now_ts() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def parse_issue_body(body: str) -> dict:
    sections = {}
    for name, content in SECTION_RE.findall(body or ""):
        sections[name.strip().lower()] = content.strip()

    return {
        "goal": sections.get("goal", "").strip(),
        "success_criteria": sections.get("success criteria", "").strip(),
        "task_type": sections.get("task type", "").strip().lower() or "implementation",
        "agent_preference": sections.get("agent preference", "").strip().lower() or "auto",
        "constraints": sections.get("constraints", "").strip(),
        "context": sections.get("context", "").strip(),
    }


def build_mailbox_task(cfg: dict, project_key: str, repo_cfg: dict, issue: dict) -> tuple[str, str]:
    title = issue["title"]
    body_text = issue.get("body", "")
    slug = slugify(title)
    task_id = f"task-{now_ts()}-{slug}"

    # Try LLM formatting first, fall back to raw section parsing
    formatter_model = cfg.get("formatter_model")
    parsed = format_task(title, body_text, model=formatter_model)
    if parsed is None:
        parsed = parse_issue_body(body_text)

    criteria = parsed["success_criteria"] or "- Match the issue goal\n- Keep the diff minimal\n- Leave a valid .agent_result.md"
    constraints = parsed["constraints"] or "- Work only inside the repo\n- Prefer minimal diffs"
    context = parsed["context"] or "None"

    # Determine priority from issue labels (prio:high / prio:normal / prio:low)
    label_names = {lbl["name"].lower() for lbl in issue.get("labels", [])}
    priority = "prio:normal"  # default
    for lbl in ("prio:high", "prio:normal", "prio:low"):
        if lbl in label_names:
            priority = lbl
            break

    frontmatter = {
        "task_id": task_id,
        "repo": repo_cfg["local_repo"],
        "agent": parsed["agent_preference"] or "auto",
        "task_type": parsed["task_type"] or cfg["default_task_type"],
        "branch": f"agent/{task_id}",
        "base_branch": cfg["default_base_branch"],
        "allow_push": cfg["default_allow_push"],
        "attempt": 1,
        "max_attempts": cfg["default_max_attempts"],
        "max_runtime_minutes": cfg["max_runtime_minutes"],
        "model_attempts": [],
        "priority": priority,
        "github_project_key": project_key,
        "github_repo": repo_cfg["github_repo"],
        "github_issue_number": issue["number"],
        "github_issue_url": issue["url"],
    }

    frontmatter_text = yaml.safe_dump(frontmatter, sort_keys=False).strip()

    body = f"""---
{frontmatter_text}
---

# Goal

{parsed["goal"] or title}

# Success Criteria

{criteria}

# Constraints

{constraints}

# Context

{context}
"""
    return task_id, body


_PRIO_ORDER = {"prio:high": 0, "prio:normal": 1, "prio:low": 2}


def _item_priority(item: dict) -> int:
    """Return sort key — lower number = higher priority."""
    for lbl in item.get("labels", set()):
        if lbl in _PRIO_ORDER:
            return _PRIO_ORDER[lbl]
    return _PRIO_ORDER["prio:normal"]


def _dispatch_item(cfg, paths, owner, repo_to_project, info, ready_items) -> bool:
    """Try to dispatch one ready item (highest priority first). Returns True if dispatched."""
    ready_items = sorted(ready_items, key=_item_priority)
    for item in ready_items:
        repo_full = item["repo"]
        if repo_full not in repo_to_project:
            continue

        # Skip issues from untrusted authors (prompt injection defense)
        if not is_trusted(item.get("author"), cfg):
            print(f"Skipped #{item['number']} — untrusted author: {item.get('author', '?')!r}")
            continue

        pk, pcfg, rcfg = repo_to_project[repo_full]

        # Skip items with excluded labels
        excluded = {x.lower() for x in pcfg.get("excluded_labels", [])}
        if item["labels"].intersection(excluded):
            continue

        # Build issue dict for build_mailbox_task
        issue = {
            "number": item["number"],
            "title": item["title"],
            "body": item["body"],
            "url": item["url"],
            "labels": [{"name": l} for l in item["labels"]],
        }

        task_id, task_md = build_mailbox_task(cfg, pk, rcfg, issue)
        task_path = paths["INBOX"] / f"{task_id}.md"
        task_path.write_text(task_md, encoding="utf-8")

        # Update labels for visibility
        edit_issue_labels(
            repo_full,
            item["number"],
            add=["in-progress", "agent-dispatched"],
            remove=pcfg.get("required_labels", []),
        )

        add_issue_comment(
            repo_full,
            item["number"],
            f"🤖 Dispatched to orchestrator.\n\nTask ID: `{task_id}`\nProject key: `{pk}`",
        )

        # Set project Status to In Progress
        in_progress_value = pcfg.get("in_progress_value", "In Progress")
        option_id = info["status_options"].get(in_progress_value)
        if info["status_field_id"] and option_id:
            try:
                set_item_status(
                    info["project_id"],
                    item["item_id"],
                    info["status_field_id"],
                    option_id,
                )
            except Exception as e:
                print(f"Warning: failed to set project status: {e}")

        print(f"Dispatched {repo_full}#{item['number']} -> {task_path}")
        return True
    return False


def dispatch_one():
    cfg = load_config()
    paths = runtime_paths(cfg)
    owner = cfg["github_owner"]

    # Build repo -> (project_key, project_cfg, repo_cfg) mapping
    repo_to_project: dict[str, tuple[str, dict, dict]] = {}
    for project_key, project_cfg in cfg["github_projects"].items():
        for repo_cfg in project_cfg.get("repos", []):
            repo_to_project[repo_cfg["github_repo"]] = (project_key, project_cfg, repo_cfg)

    # Query each unique project_number only once
    queried: dict[int, tuple[dict, list[dict]]] = {}
    graphql_ok = True
    for project_key, project_cfg in cfg["github_projects"].items():
        pn = project_cfg["project_number"]
        if pn in queried:
            continue
        ready_value = project_cfg.get("ready_value", "Ready")
        try:
            info, ready = get_ready_items(pn, owner, ready_value)
            queried[pn] = (info, ready)
        except Exception as e:
            print(f"Warning: failed to query project {pn}: {e}")
            graphql_ok = False
            continue

    # Dispatch first matching ready item (Status-based)
    for pn, (info, ready_items) in queried.items():
        dispatched = _dispatch_item(cfg, paths, owner, repo_to_project, info, ready_items)
        if dispatched:
            return

    # Fallback: label-based dispatch if GraphQL failed
    if not graphql_ok:
        print("Falling back to label-based dispatch...")
        for project_key, project_cfg in cfg["github_projects"].items():
            for repo_cfg in project_cfg.get("repos", []):
                repo_full = repo_cfg["github_repo"]
                issues = list_ready_issues(repo_full, limit=20)
                for issue in issues:
                    author = (issue.get("author") or {}).get("login", "")
                    if not is_trusted(author, cfg):
                        print(f"Skipped #{issue['number']} — untrusted author: {author!r}")
                        continue
                    labels = {lbl["name"].lower() for lbl in issue.get("labels", [])}
                    excluded = {x.lower() for x in project_cfg.get("excluded_labels", [])}
                    if labels.intersection(excluded):
                        continue
                    task_id, task_md = build_mailbox_task(cfg, project_key, repo_cfg, issue)
                    task_path = paths["INBOX"] / f"{task_id}.md"
                    task_path.write_text(task_md, encoding="utf-8")
                    edit_issue_labels(
                        repo_full, issue["number"],
                        add=["in-progress", "agent-dispatched"],
                        remove=project_cfg.get("required_labels", []),
                    )
                    add_issue_comment(
                        repo_full, issue["number"],
                        f"🤖 Dispatched to orchestrator.\n\nTask ID: `{task_id}`\nProject key: `{project_key}`",
                    )
                    print(f"Dispatched (label fallback) {repo_full}#{issue['number']} -> {task_path}")
                    return

    print("No dispatchable issues found.")


if __name__ == "__main__":
    dispatch_one()
