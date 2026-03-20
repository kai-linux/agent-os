from __future__ import annotations

import json

from orchestrator.paths import load_config
from orchestrator.gh_project import (
    add_issue_comment,
    edit_issue_labels,
    query_project,
    set_item_status,
    create_pr_for_branch,
    ensure_labels,
    gh,
    gh_json,
)
from orchestrator.privacy import redact_text


def _find_repo_project(cfg: dict, repo: str) -> tuple[dict, dict] | tuple[None, None]:
    for project_cfg in cfg.get("github_projects", {}).values():
        for repo_cfg in project_cfg.get("repos", []):
            if repo_cfg.get("github_repo") == repo:
                return project_cfg, repo_cfg
    return None, None


def _create_issue(repo: str, title: str, body: str, labels: list[str]) -> str:
    ensure_labels(repo, labels)
    cmd = ["issue", "create", "-R", repo, "--title", title, "--body", body]
    for label in labels:
        cmd += ["--label", label]
    return gh(cmd)


def _set_issue_ready(cfg: dict, repo: str, issue_url: str):
    owner = cfg.get("github_owner", "")
    project_cfg, _repo_cfg = _find_repo_project(cfg, repo)
    if not owner or not project_cfg:
        return

    ready_value = project_cfg.get("ready_value", "Ready")
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
        option_id = info["status_options"].get(ready_value)
        if info["status_field_id"] and option_id:
            set_item_status(info["project_id"], item_id, info["status_field_id"], option_id)
    except Exception as e:
        print(f"Warning: failed to set follow-up issue ready for {repo}: {e}")


def _find_open_partial_followup(repo: str, task_id: str, title: str) -> dict | None:
    try:
        issues = gh_json([
            "issue", "list", "-R", repo, "--state", "open",
            "--search", task_id,
            "--json", "number,title,url,body",
            "--limit", "20",
        ]) or []
    except Exception:
        return None

    task_marker = f"## Original Task ID\n{task_id}"
    for issue in issues:
        if (issue.get("title") or "").strip() != title.strip():
            continue
        body = issue.get("body") or ""
        if task_marker in body:
            return issue
    return None


def _maybe_create_partial_debug_followup(meta: dict, result: dict, cfg: dict) -> str | None:
    if result.get("status") != "partial":
        return None
    if meta.get("task_type") != "debugging":
        return None

    repo = meta.get("github_repo")
    issue_number = meta.get("github_issue_number")
    task_id = meta.get("task_id")
    next_step = str(result.get("next_step", "")).strip()
    if not repo or not issue_number or not task_id or not next_step or next_step.lower() == "none":
        return None

    title = f"Follow up partial debug for {task_id}"
    existing = _find_open_partial_followup(repo, task_id, title)
    if existing:
        return existing.get("url")

    summary = redact_text(result.get("summary", "No summary provided."))
    blocker_code = str(result.get("blocker_code", "")).strip() or "none"
    branch = meta.get("branch", "")
    base_branch = meta.get("base_branch", "main")
    bullets = {
        "done": "\n".join(result.get("done", ["- None"])),
        "blockers": "\n".join(result.get("blockers", ["- None"])),
        "files_changed": "\n".join(result.get("files_changed", ["- None"])),
        "tests_run": "\n".join(result.get("tests_run", ["- None"])),
        "attempted_approaches": "\n".join(result.get("attempted_approaches", ["- None"])),
    }
    body = f"""## Goal
{redact_text(next_step)}

## Success Criteria
- Advance the unresolved debugging work for original task `{task_id}`
- Resolve or narrow the remaining failure described below
- Preserve evidence and avoid repeating failed approaches without new evidence

## Task Type
debugging

## Base Branch
{branch or base_branch}

## Branch
{branch or f"agent/{task_id}"}

## Context
Original issue: #{issue_number}

## Original Task ID
{task_id}

## Remaining Failure
{summary}

## Prior Blocker Code
{blocker_code}

## Evidence
### Progress So Far
{bullets["done"]}

### Current Blockers
{bullets["blockers"]}

### Files Changed
{bullets["files_changed"]}

### Tests Run
{bullets["tests_run"]}

### Avoid Repeating
{bullets["attempted_approaches"]}
"""
    labels = ["ready"]
    priority = str(meta.get("priority", "")).strip().lower()
    if priority.startswith("prio:"):
        labels.append(priority)
    issue_url = _create_issue(repo, title, body, labels)
    _set_issue_ready(cfg, repo, issue_url)
    return issue_url


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
    summary = redact_text(result.get("summary", "No summary."))
    next_step = redact_text(result.get("next_step", "None"))
    blocker_code = str(result.get("blocker_code", "")).strip()
    manual_steps = result.get("manual_steps", "").strip()
    has_manual = bool(manual_steps and manual_steps.lower() not in ("- none", "none", ""))
    public_manual_steps = redact_text(manual_steps)
    followup_issue_url = _maybe_create_partial_debug_followup(meta, result, cfg)

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

    if blocker_code:
        comment += f"\n### Blocker code\n`{blocker_code}`\n"

    if followup_issue_url:
        comment += f"\n### Follow-up issue\n{followup_issue_url}\n"

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

    if has_manual:
        comment += f"\n### 🔧 Manual steps required\n```\n{public_manual_steps}\n```\n"

    try:
        add_issue_comment(repo, issue_number, comment)
    except Exception as e:
        print(f"Warning: failed to comment on issue #{issue_number}: {e}")

    if status == "complete":
        edit_issue_labels(
            repo,
            issue_number,
            add=["done"],
            remove=["in-progress", "ready", "blocked", "agent-dispatched"],
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

    return {"followup_issue_url": followup_issue_url}
