import re
from datetime import datetime
from pathlib import Path

import yaml

from orchestrator.paths import load_config, runtime_paths
from orchestrator.gh_project import (
    list_ready_issues,
    edit_issue_labels,
    add_issue_comment,
    find_project_item_for_issue,
    get_status_field_and_option,
    set_project_status,
)

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
        "repo_key": sections.get("repo", "").strip().lower(),
        "task_type": sections.get("task type", "").strip().lower() or "implementation",
        "agent_preference": sections.get("agent preference", "").strip().lower() or "auto",
        "constraints": sections.get("constraints", "").strip(),
        "context": sections.get("context", "").strip(),
    }


def repo_path_from_key(cfg: dict, repo_key: str) -> str:
    if repo_key in cfg["github_repos"]:
        key_map = {
            "writeaibook": "/srv/repos/writeaibook",
            "eigendark": "/srv/repos/eigendark",
        }
        return key_map.get(repo_key, "/srv/repos/writeaibook")
    return "/srv/repos/writeaibook"


def to_mailbox_task(cfg: dict, repo_full_name: str, issue: dict) -> tuple[str, str]:
    parsed = parse_issue_body(issue.get("body", ""))
    title = issue["title"]
    slug = slugify(title)
    task_id = f"task-{now_ts()}-{slug}"
    repo_path = repo_path_from_key(cfg, parsed["repo_key"])
    branch = f"agent/{task_id}"

    criteria = parsed["success_criteria"] or "- Match the issue goal\n- Keep the diff minimal\n- Leave a valid .agent_result.md"
    constraints = parsed["constraints"] or "- Work only inside the repo\n- Prefer minimal diffs"
    context = parsed["context"] or "None"

    frontmatter = {
        "task_id": task_id,
        "repo": repo_path,
        "agent": parsed["agent_preference"] or "auto",
        "task_type": parsed["task_type"] or "implementation",
        "branch": branch,
        "base_branch": cfg["default_base_branch"],
        "allow_push": cfg["default_allow_push"],
        "attempt": 1,
        "max_attempts": cfg["default_max_attempts"],
        "max_runtime_minutes": cfg["max_runtime_minutes"],
        "model_attempts": [],
        "github_repo": repo_full_name,
        "github_issue_number": issue["number"],
        "github_issue_url": issue["url"],
    }

    frontmatter_text = yaml.safe_dump(frontmatter, sort_keys=False).strip()

    task_md = f"""---
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
    return task_id, task_md


def dispatch_one():
    cfg = load_config()
    paths = runtime_paths(cfg)

    for repo_key, repo_full_name in cfg["github_repos"].items():
        issues = list_ready_issues(repo_full_name, limit=20)
        if not issues:
            continue

        issue = issues[0]
        task_id, task_md = to_mailbox_task(cfg, repo_full_name, issue)
        task_path = paths["INBOX"] / f"{task_id}.md"
        task_path.write_text(task_md, encoding="utf-8")

        edit_issue_labels(repo_full_name, issue["number"], add=["in-progress"], remove=["ready"])
        add_issue_comment(
            repo_full_name,
            issue["number"],
            f"🤖 Dispatched to orchestrator.\n\nTask ID: `{task_id}`",
        )

        try:
            item = find_project_item_for_issue(
                cfg["github_project_number"],
                cfg["github_owner"],
                issue["url"],
            )
            if item:
                field_id, option_id = get_status_field_and_option(
                    cfg["github_project_number"],
                    cfg["github_owner"],
                    cfg["github_project_status_field"],
                    cfg["github_project_in_progress_value"],
                )
                set_project_status(
                    cfg["github_project_number"],
                    cfg["github_owner"],
                    item["project"]["id"],
                    item["id"],
                    field_id,
                    option_id,
                )
        except Exception:
            pass

        print(f"Dispatched {repo_full_name}#{issue['number']} -> {task_path}")
        return

    print("No ready issues found.")


if __name__ == "__main__":
    dispatch_one()
