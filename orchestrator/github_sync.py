from orchestrator.paths import load_config
from orchestrator.gh_project import (
    add_issue_comment,
    edit_issue_labels,
    find_project_item_for_issue,
    get_status_field_and_option,
    set_project_status,
    create_pr_for_branch,
)


def sync_result(meta: dict, result: dict, commit_hash: str | None):
    cfg = load_config()

    repo = meta.get("github_repo")
    issue_number = meta.get("github_issue_number")
    issue_url = meta.get("github_issue_url")
    branch = meta.get("branch")
    task_id = meta.get("task_id")

    if not repo or not issue_number or not issue_url:
        return

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
        edit_issue_labels(repo, issue_number, add=["review"], remove=["in-progress", "ready", "blocked"])
        status_value = cfg["github_project_review_value"]
    elif status in ("partial", "blocked"):
        edit_issue_labels(repo, issue_number, add=["blocked"], remove=["in-progress", "ready"])
        status_value = cfg["github_project_blocked_value"]
    else:
        edit_issue_labels(repo, issue_number, add=["blocked"], remove=["in-progress", "ready"])
        status_value = cfg["github_project_blocked_value"]

    try:
        item = find_project_item_for_issue(cfg["github_project_number"], cfg["github_owner"], issue_url)
        if item:
            field_id, option_id = get_status_field_and_option(
                cfg["github_project_number"],
                cfg["github_owner"],
                cfg["github_project_status_field"],
                status_value,
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
