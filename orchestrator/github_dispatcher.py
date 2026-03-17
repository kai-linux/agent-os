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


def issue_labels(issue: dict) -> set[str]:
    return {lbl["name"].lower() for lbl in issue.get("labels", [])}


def repo_lookup(cfg: dict, project_key: str, repo_key: str | None):
    project_cfg = cfg["github_projects"][project_key]
    repos = project_cfg.get("repos", [])

    if repo_key:
        for repo in repos:
            if repo["key"].lower() == repo_key.lower():
                return repo

    if len(repos) == 1:
        return repos[0]

    return None


def issue_matches_project(issue: dict, project_cfg: dict) -> bool:
    labels = issue_labels(issue)

    required = {x.lower() for x in project_cfg.get("required_labels", [])}
    excluded = {x.lower() for x in project_cfg.get("excluded_labels", [])}

    if required and not required.issubset(labels):
        return False

    if excluded and labels.intersection(excluded):
        return False

    return True


def build_mailbox_task(cfg: dict, project_key: str, repo_cfg: dict, issue: dict) -> tuple[str, str]:
    parsed = parse_issue_body(issue.get("body", ""))
    title = issue["title"]
    slug = slugify(title)
    task_id = f"task-{now_ts()}-{slug}"

    criteria = parsed["success_criteria"] or "- Match the issue goal\n- Keep the diff minimal\n- Leave a valid .agent_result.md"
    constraints = parsed["constraints"] or "- Work only inside the repo\n- Prefer minimal diffs"
    context = parsed["context"] or "None"

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


def dispatch_one():
    cfg = load_config()
    paths = runtime_paths(cfg)

    for project_key, project_cfg in cfg["github_projects"].items():
        for repo_cfg in project_cfg.get("repos", []):
            repo_full_name = repo_cfg["github_repo"]
            issues = list_ready_issues(repo_full_name, limit=20)

            for issue in issues:
                if not issue_matches_project(issue, project_cfg):
                    continue

                parsed = parse_issue_body(issue.get("body", ""))
                desired_repo_key = parsed["repo_key"] or repo_cfg["key"]
                selected_repo = repo_lookup(cfg, project_key, desired_repo_key)

                if selected_repo is None:
                    continue

                task_id, task_md = build_mailbox_task(cfg, project_key, selected_repo, issue)
                task_path = paths["INBOX"] / f"{task_id}.md"
                task_path.write_text(task_md, encoding="utf-8")

                edit_issue_labels(
                    repo_full_name,
                    issue["number"],
                    add=["in-progress"],
                    remove=project_cfg.get("required_labels", []),
                )

                add_issue_comment(
                    repo_full_name,
                    issue["number"],
                    f"🤖 Dispatched to orchestrator.\n\nTask ID: `{task_id}`\nProject key: `{project_key}`",
                )

                try:
                    item = find_project_item_for_issue(
                        project_cfg["project_number"],
                        cfg["github_owner"],
                        issue["url"],
                    )
                    if item:
                        field_id, option_id = get_status_field_and_option(
                            project_cfg["project_number"],
                            cfg["github_owner"],
                            project_cfg["status_field"],
                            project_cfg["in_progress_value"],
                        )
                        set_project_status(
                            project_cfg["project_number"],
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

    print("No dispatchable issues found.")


if __name__ == "__main__":
    dispatch_one()