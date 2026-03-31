from __future__ import annotations

import json
import re

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
from orchestrator.outcome_attribution import (
    append_outcome_record,
    extract_pr_number,
    get_repo_outcome_check_ids,
    format_outcome_checks_section,
)
from orchestrator.privacy import redact_text
from orchestrator.repo_modes import is_dispatcher_only_repo


_CI_CONTEXT_LINE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*-\s+PR:\s+.+$", re.IGNORECASE),
    re.compile(r"^\s*-\s+Failed checks:\s*$", re.IGNORECASE),
    re.compile(r"^\s*-\s+\*\*.+?\*\*:\s*`.+?`\s*(?:- .+)?$", re.IGNORECASE),
)
_FOLLOWUP_TITLE_PREFIX = "Follow up partial debug for root issue #"
MAX_DEBUG_FOLLOWUP_DEPTH = 2
_ROOT_ISSUE_RE = re.compile(r"^## Root Issue Number\s*\n(\d+)\s*$", re.MULTILINE)
_ROOT_PR_RE = re.compile(r"^## Root PR Number\s*\n(\d+)\s*$", re.MULTILINE)
_ROOT_BRANCH_RE = re.compile(r"^## Root Branch\s*\n(.+?)\s*$", re.MULTILINE)
_BRANCH_RE = re.compile(r"^## Branch\s*\n(.+?)\s*$", re.MULTILINE)
_ORIGINAL_ISSUE_RE = re.compile(r"Original issue:\s+#(\d+)", re.IGNORECASE)
_PR_URL_RE = re.compile(r"/pull/(\d+)")
_FOLLOWUP_DEPTH_RE = re.compile(r"^## Follow-up Depth\s*\n(\d+)\s*$", re.MULTILINE)


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


def _normalize_issue_labels(issue: dict | None) -> set[str]:
    labels = issue.get("labels", []) if isinstance(issue, dict) else []
    normalized: set[str] = set()
    for label in labels:
        if isinstance(label, dict):
            name = label.get("name", "")
        else:
            name = str(label)
        name = str(name).strip().lower()
        if name:
            normalized.add(name)
    return normalized


def _get_issue_snapshot(repo: str, issue_number: int) -> dict:
    try:
        return gh_json([
            "issue", "view", str(issue_number), "-R", repo,
            "--json", "number,state,body,labels,url",
        ]) or {}
    except Exception:
        return {}


def _issue_is_terminal(issue: dict | None) -> bool:
    if not issue:
        return False
    state = str(issue.get("state", "")).upper()
    labels = _normalize_issue_labels(issue)
    return state == "CLOSED" or "done" in labels


def _extract_root_issue_number(issue_number: int, body: str) -> int:
    match = _ROOT_ISSUE_RE.search(body or "")
    if match:
        return int(match.group(1))
    return issue_number


def _extract_root_pr_number(body: str) -> int | None:
    match = _ROOT_PR_RE.search(body or "")
    if match:
        return int(match.group(1))
    pr_match = _PR_URL_RE.search(body or "")
    if pr_match:
        return int(pr_match.group(1))
    return None


def _extract_root_branch(body: str, branch: str) -> str:
    for pattern in (_ROOT_BRANCH_RE, _BRANCH_RE):
        match = pattern.search(body or "")
        if match:
            return match.group(1).strip()
    return str(branch or "").strip()


def _reconcile_terminal_issue_state(cfg: dict, project_cfg: dict, repo: str, issue_number: int, issue_url: str):
    if not project_cfg:
        return

    edit_issue_labels(
        repo,
        issue_number,
        add=["done"],
        remove=["blocked", "ready", "in-progress", "agent-dispatched"],
    )

    try:
        info = query_project(project_cfg["project_number"], cfg["github_owner"])
        option_id = info["status_options"].get(project_cfg.get("done_value", "Done"))
        if not info["status_field_id"] or not option_id:
            return
        for item in info["items"]:
            if item["url"] == issue_url:
                set_item_status(info["project_id"], item["item_id"], info["status_field_id"], option_id)
                break
    except Exception as e:
        print(f"Warning: failed to reconcile terminal issue #{issue_number}: {e}")


def _find_open_partial_followup(
    repo: str,
    *,
    root_issue_number: int,
    root_branch: str,
    root_pr_number: int | None,
    title: str,
) -> dict | None:
    try:
        issues = gh_json([
            "issue", "list", "-R", repo, "--state", "open",
            "--search", title,
            "--json", "number,title,url,body",
            "--limit", "20",
        ]) or []
    except Exception:
        return None

    for issue in issues:
        if (issue.get("title") or "").strip() != title.strip():
            continue
        body = issue.get("body") or ""
        issue_root = _extract_root_issue_number(root_issue_number, body)
        issue_branch = _extract_root_branch(body, "")
        issue_pr = _extract_root_pr_number(body)
        if issue_root != root_issue_number:
            continue
        if root_branch and issue_branch and issue_branch != root_branch:
            continue
        if root_pr_number is not None and issue_pr is not None and issue_pr != root_pr_number:
            continue
        return issue
    return None


def _extract_preserved_ci_context(body: str) -> str:
    lines = [
        line.rstrip()
        for line in str(body or "").splitlines()
        if any(pattern.match(line) for pattern in _CI_CONTEXT_LINE_PATTERNS)
    ]
    return "\n".join(lines).strip()


def _get_issue_body(repo: str, issue_number: int) -> str:
    try:
        payload = gh_json(["issue", "view", str(issue_number), "-R", repo, "--json", "body"]) or {}
    except Exception:
        return ""
    return str(payload.get("body", "") or "")


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
    if is_dispatcher_only_repo(cfg, str(repo)):
        return None

    issue_snapshot = _get_issue_snapshot(str(repo), int(issue_number))
    issue_body = str(issue_snapshot.get("body", "") or "")

    # Check follow-up depth to prevent infinite recursion
    depth_match = _FOLLOWUP_DEPTH_RE.search(issue_body)
    current_depth = int(depth_match.group(1)) if depth_match else 0
    if current_depth >= MAX_DEBUG_FOLLOWUP_DEPTH:
        print(f"Follow-up depth {current_depth} >= {MAX_DEBUG_FOLLOWUP_DEPTH}, skipping further follow-ups for #{issue_number}")
        return None

    branch = meta.get("branch", "")
    base_branch = meta.get("base_branch", "main")
    root_issue_number = _extract_root_issue_number(int(issue_number), issue_body)
    root_branch = _extract_root_branch(issue_body, branch or base_branch)
    root_pr_number = _extract_root_pr_number(issue_body)

    title = f"{_FOLLOWUP_TITLE_PREFIX}{root_issue_number}"
    existing = _find_open_partial_followup(
        repo,
        root_issue_number=root_issue_number,
        root_branch=root_branch,
        root_pr_number=root_pr_number,
        title=title,
    )
    if existing:
        return existing.get("url")

    summary = redact_text(result.get("summary", "No summary provided."))
    blocker_code = str(result.get("blocker_code", "")).strip() or "none"
    preserved_ci_context = _extract_preserved_ci_context(issue_body)
    preserved_ci_block = (
        f"\n## Preserved CI Context\n{preserved_ci_context}\n"
        if preserved_ci_context
        else ""
    )
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

## Root Issue Number
{root_issue_number}

## Root Branch
{root_branch or branch or base_branch}
"""
    if root_pr_number is not None:
        body += f"""
## Root PR Number
{root_pr_number}
"""
    body += f"""
## Follow-up Depth
{current_depth + 1}

## Context
Original issue: #{issue_number}
{preserved_ci_block}

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
    body += format_outcome_checks_section(get_repo_outcome_check_ids(cfg, repo))
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
    current_issue = _get_issue_snapshot(str(repo), int(issue_number))
    if status in ("partial", "blocked") and _issue_is_terminal(current_issue):
        _reconcile_terminal_issue_state(cfg, project_cfg, str(repo), int(issue_number), str(issue_url))
        return {"followup_issue_url": None, "skipped_terminal_issue": True}

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
            append_outcome_record(
                cfg,
                {
                    "record_type": "attribution",
                    "event": "pr_opened",
                    "repo": repo,
                    "task_id": task_id,
                    "issue_number": issue_number,
                    "pr_number": extract_pr_number(pr_url),
                    "pr_url": pr_url,
                    "branch": branch,
                    "outcome_check_ids": list(meta.get("outcome_check_ids") or []),
                },
            )

    if has_manual:
        comment += f"\n### 🔧 Manual steps required\n```\n{public_manual_steps}\n```\n"

    try:
        add_issue_comment(repo, issue_number, comment)
    except Exception as e:
        print(f"Warning: failed to comment on issue #{issue_number}: {e}")

    if status == "complete":
        if pr_url:
            edit_issue_labels(
                repo,
                issue_number,
                add=["in-progress", "agent-dispatched"],
                remove=["ready", "blocked", "done"],
            )
            status_value = project_cfg["in_progress_value"]
        else:
            edit_issue_labels(
                repo,
                issue_number,
                add=["done"],
                remove=["in-progress", "ready", "blocked", "agent-dispatched"],
            )
            status_value = project_cfg["done_value"]

            # Close the GitHub issue only when there is no PR left to validate/merge.
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
