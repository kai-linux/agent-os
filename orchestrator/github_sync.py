from __future__ import annotations

from orchestrator.paths import load_config
from orchestrator.gh_project import (
    add_issue_comment,
    edit_issue_labels,
    query_project,
    set_item_status,
    create_pr_for_branch,
    gh,
)


def sync_result(meta: dict, result: dict, commit_hash: str | None):
    cfg = load_config()

    project_key = meta.get("github_project_key")
    repo = meta.get("github_repo")
    issue_number = meta.get("github_issue_number")
    issue_url = meta.get("github_issue_url")
    branch = meta.get("branch")
    task_id = meta.get("task_id")

    if not project_key or project_key not in cfg["github_projects"]:
        return
    if not repo or not issue_number or not issue_url:
        return

    project_cfg = cfg["github_projects"][project_key]
    owner = cfg["github_owner"]

    status = result.get("status", "blocked")
    summary = result.get("summary", "No summary.")
    next_step = result.get("next_step", "None")

    comment = f"""## Orchestrator update

**Task:** `{task_id}`
**Branch:** `{branch}`
**Status:** `{status}`
**Commit:** `{commit_hash or 'none'}`

### Summary
{summary}

### Next step
{next_step}
"""
    pr_url = None
    if status == "complete":
        pr_url = create_pr_for_branch(
            repo,
            branch,
            f"Agent: {task_id}",
            f"Automated changes for issue #{issue_number}",
        )
        if pr_url:
            comment += f"\n### PR\n{pr_url}\n"

    add_issue_comment(repo, issue_number, comment)

    if status == "complete":
        edit_issue_labels(
            repo,
            issue_number,
            add=["done"],
            remove=["in-progress", "ready", "blocked", "review", "agent-dispatched"],
        )
        status_value = project_cfg["done_value"]

        # Close the GitHub issue
        try:
            gh(["issue", "close", str(issue_number), "-R", repo])
        except Exception as e:
            print(f"Warning: failed to close issue #{issue_number}: {e}")

    elif status in ("partial", "blocked"):
        edit_issue_labels(
            repo,
            issue_number,
            add=["blocked"],
            remove=["in-progress", "ready", "agent-dispatched"],
        )
        status_value = project_cfg["blocked_value"]

    else:
        edit_issue_labels(
            repo,
            issue_number,
            add=["blocked"],
            remove=["in-progress", "ready", "agent-dispatched"],
        )
        status_value = project_cfg["blocked_value"]

    # Update project board Status via GraphQL
    try:
        info = query_project(project_cfg["project_number"], owner)
        option_id = info["status_options"].get(status_value)
        if not info["status_field_id"] or not option_id:
            print(f"Warning: status option '{status_value}' not found in project")
            return

        # Find the item matching this issue
        for item in info["items"]:
            if item["url"] == issue_url:
                set_item_status(
                    info["project_id"],
                    item["item_id"],
                    info["status_field_id"],
                    option_id,
                )
                print(f"Project status set to '{status_value}' for #{issue_number}")
                break
        else:
            print(f"Warning: issue #{issue_number} not found in project {project_cfg['project_number']}")
    except Exception as e:
        print(f"Warning: failed to update project status for #{issue_number}: {e}")
