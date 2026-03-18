from __future__ import annotations

import json
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
    create_issue,
    gh,
    query_project,
    ensure_labels,
)
from orchestrator.task_formatter import format_task
from orchestrator.task_decomposer import decompose_task, format_sub_issue_body
from orchestrator.trust import is_trusted


SECTION_RE = re.compile(r"^##\s+(.+?)\n(.*?)(?=^##\s+|\Z)", re.MULTILINE | re.DOTALL)
DEPENDENCY_RE = re.compile(r"(?im)^\s*(?:depends on|blocked by)\s+#(\d+)\b")
MAX_DEPENDENCY_DEPTH = 3


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


def parse_issue_dependencies(body: str) -> list[int]:
    deps = []
    for match in DEPENDENCY_RE.findall(body or ""):
        number = int(match)
        if number not in deps:
            deps.append(number)
    return deps


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

    # Determine agent from issue labels (claude, codex, gemini, deepseek)
    # Takes precedence over agent_preference in issue body
    agent = parsed["agent_preference"] or "auto"
    valid_agents = {"claude", "codex", "gemini", "deepseek"}
    for lbl in label_names:
        if lbl in valid_agents:
            agent = lbl
            break

    frontmatter = {
        "task_id": task_id,
        "repo": repo_cfg["local_repo"],
        "agent": agent,
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
_WORKFLOW_LABELS = {
    "ready", "in-progress", "agent-dispatched", "blocked", "review", "done", "epic",
}


def _item_priority(item: dict) -> int:
    """Return sort key — lower number = higher priority."""
    for lbl in item.get("labels", set()):
        if lbl in _PRIO_ORDER:
            return _PRIO_ORDER[lbl]
    return _PRIO_ORDER["prio:normal"]


<<<<<<< HEAD
def _fetch_issue_dependency(repo_full: str, number: int) -> dict | None:
    raw = gh([
        "issue", "view", str(number), "-R", repo_full,
        "--json", "number,title,body,state,url,labels",
    ], check=False)
    if not raw:
        return None
    data = json.loads(raw)
    data["labels"] = {lbl["name"].lower() for lbl in data.get("labels", [])}
    return data


def _build_issue_lookup(queried: dict[int, tuple[dict, list[dict]]]) -> dict[tuple[str, int], dict]:
    lookup = {}
    for info, _ready_items in queried.values():
        for item in info.get("items", []):
            repo_full = item.get("repo")
            number = item.get("number")
            if repo_full and number:
                lookup[(repo_full, number)] = item
    return lookup


def _resolve_issue_dependencies(
    repo_full: str,
    issue: dict,
    issue_lookup: dict[tuple[str, int], dict],
    fetched_cache: dict[tuple[str, int], dict | None] | None = None,
    *,
    depth: int = 0,
    trail: tuple[int, ...] | None = None,
    remote_budget: list[int] | None = None,
) -> dict:
    fetched_cache = fetched_cache if fetched_cache is not None else {}
    trail = trail or (issue["number"],)
    remote_budget = remote_budget if remote_budget is not None else [1]

    deps = parse_issue_dependencies(issue.get("body", ""))
    if not deps:
        return {"status": "clear"}
    if depth >= MAX_DEPENDENCY_DEPTH:
        return {"status": "depth-limit", "dependency": deps[0]}

    for dep_number in deps:
        if dep_number in trail:
            return {"status": "circular", "dependency": dep_number, "trail": [*trail, dep_number]}

        key = (repo_full, dep_number)
        dep_issue = issue_lookup.get(key)
        if dep_issue is None:
            if key in fetched_cache:
                dep_issue = fetched_cache[key]
            elif remote_budget[0] > 0:
                remote_budget[0] -= 1
                dep_issue = _fetch_issue_dependency(repo_full, dep_number)
                fetched_cache[key] = dep_issue

        if dep_issue is None:
            return {"status": "unknown", "dependency": dep_number}

        if dep_issue.get("state") != "CLOSED":
            return {"status": "blocked", "dependency": dep_number}

        nested = _resolve_issue_dependencies(
            repo_full,
            dep_issue,
            issue_lookup,
            fetched_cache,
            depth=depth + 1,
            trail=(*trail, dep_number),
            remote_budget=remote_budget,
        )
        if nested["status"] != "clear":
            return nested

    return {"status": "clear"}


def _set_project_status(info: dict, item_id: str, status_value: str):
    option_id = info["status_options"].get(status_value)
    if info["status_field_id"] and option_id:
        set_item_status(
            info["project_id"],
            item_id,
            info["status_field_id"],
            option_id,
        )


def _mark_issue_blocked(repo_full: str, item: dict, info: dict, project_cfg: dict, dependency_number: int):
    blocked_value = project_cfg.get("blocked_value", "Blocked")
    try:
        _set_project_status(info, item["item_id"], blocked_value)
    except Exception as e:
        print(f"Warning: failed to set project status: {e}")
    add_issue_comment(repo_full, item["number"], f"Waiting for #{dependency_number}")


def _requeue_unblocked_items(queried, repo_to_project, issue_lookup):
    for info, _ready_items in queried.values():
        for item in info.get("items", []):
            if item.get("state") != "OPEN":
                continue

            repo_full = item.get("repo")
            if repo_full not in repo_to_project:
                continue

            _project_key, project_cfg, _repo_cfg = repo_to_project[repo_full]
            blocked_value = project_cfg.get("blocked_value", "Blocked")
            if item.get("status") != blocked_value:
                continue

            if not parse_issue_dependencies(item.get("body", "")):
                continue

            resolution = _resolve_issue_dependencies(repo_full, item, issue_lookup, {})
            if resolution["status"] == "clear":
                ready_value = project_cfg.get("ready_value", "Ready")
                try:
                    _set_project_status(info, item["item_id"], ready_value)
                    print(f"Dependency cleared for {repo_full}#{item['number']} -> {ready_value}")
                except Exception as e:
                    print(f"Warning: failed to set project status: {e}")


def _dispatch_item(cfg, paths, owner, repo_to_project, info, ready_items, issue_lookup) -> bool:
=======
def _extract_issue_number(issue_url: str) -> int | None:
    match = re.search(r"/issues/(\d+)$", issue_url or "")
    return int(match.group(1)) if match else None


def _copy_issue_labels(issue: dict) -> list[str]:
    labels = []
    for label in issue.get("labels", []):
        name = label["name"].lower()
        if name not in _WORKFLOW_LABELS:
            labels.append(name)
    return labels


def _set_issue_project_status(cfg: dict, project_cfg: dict, issue_url: str, status_value: str):
    owner = cfg.get("github_owner", "")
    if not owner:
        return
    info = query_project(project_cfg["project_number"], owner)
    option_id = info["status_options"].get(status_value)
    if not info["status_field_id"] or not option_id:
        return
    for project_item in info["items"]:
        if project_item["url"] == issue_url:
            set_item_status(
                info["project_id"],
                project_item["item_id"],
                info["status_field_id"],
                option_id,
            )
            break


def _maybe_decompose_issue(
    cfg: dict,
    project_key: str,
    project_cfg: dict,
    repo_cfg: dict,
    issue: dict,
) -> dict:
    del project_key
    plan = decompose_task(
        issue["title"],
        issue.get("body", ""),
        model=cfg.get("decomposer_model"),
    )
    if not plan or plan["classification"] != "epic":
        return issue

    repo_full = repo_cfg["github_repo"]
    parent_number = issue["number"]
    inherited_labels = _copy_issue_labels(issue)
    ensure_labels(repo_full, inherited_labels + ["epic"])

    created: list[dict] = []
    for idx, sub_issue in enumerate(plan["sub_issues"], start=1):
        title = f"{idx}. {sub_issue['title']}"
        body = format_sub_issue_body(parent_number, sub_issue)
        issue_url = create_issue(repo_full, title, body, inherited_labels).strip()
        number = _extract_issue_number(issue_url)
        if not number:
            raise RuntimeError(f"Could not parse issue number from {issue_url!r}")
        created.append({
            "number": number,
            "title": title,
            "body": body,
            "url": issue_url,
            "labels": [{"name": label} for label in inherited_labels],
        })

    backlog_value = project_cfg.get("backlog_value", "Backlog")
    ready_value = project_cfg.get("ready_value", "Ready")
    try:
        _set_issue_project_status(cfg, project_cfg, issue["url"], backlog_value)
        _set_issue_project_status(cfg, project_cfg, created[0]["url"], ready_value)
        for child in created[1:]:
            _set_issue_project_status(cfg, project_cfg, child["url"], backlog_value)
    except Exception as e:
        print(f"Warning: failed to update decomposed issue statuses: {e}")

    try:
        edit_issue_labels(
            repo_full,
            parent_number,
            add=["epic"],
            remove=project_cfg.get("required_labels", []),
        )
    except Exception as e:
        print(f"Warning: failed to relabel epic parent #{parent_number}: {e}")

    child_refs = "\n".join(f"- #{child['number']} {child['title']}" for child in created)
    add_issue_comment(
        repo_full,
        parent_number,
        "🤖 Task decomposed before dispatch.\n\n"
        f"Reason: {plan.get('reason') or 'Multiple independent deliverables detected.'}\n\n"
        f"Sub-issues:\n{child_refs}\n\n"
        f"Dispatching first sub-issue now: #{created[0]['number']}",
    )

    dispatched_issue = created[0].copy()
    try:
        info = query_project(project_cfg["project_number"], cfg["github_owner"])
        for project_item in info["items"]:
            if project_item["url"] == created[0]["url"]:
                dispatched_issue["item_id"] = project_item["item_id"]
                break
    except Exception as e:
        print(f"Warning: failed to refresh project item for #{created[0]['number']}: {e}")

    print(
        f"Decomposed {repo_full}#{parent_number} into {len(created)} sub-issues; "
        f"dispatching #{created[0]['number']}"
    )
    return dispatched_issue


def _dispatch_item(cfg, paths, owner, repo_to_project, info, ready_items) -> bool:
>>>>>>> 658e1bc (agent task-20260318-224806-task-task-decomposer-agent)
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

        resolution = _resolve_issue_dependencies(repo_full, item, issue_lookup, {})
        if resolution["status"] == "blocked":
            _mark_issue_blocked(repo_full, item, info, pcfg, resolution["dependency"])
            print(f"Skipped {repo_full}#{item['number']} — waiting for #{resolution['dependency']}")
            continue
        if resolution["status"] == "circular":
            print(f"Warning: circular dependency detected for {repo_full}#{item['number']}: {resolution['trail']}")
            continue
        if resolution["status"] == "depth-limit":
            print(f"Warning: dependency depth limit reached for {repo_full}#{item['number']}")
            continue
        if resolution["status"] == "unknown":
            print(f"Warning: could not resolve dependency #{resolution['dependency']} for {repo_full}#{item['number']}")
            continue

        # Build issue dict for build_mailbox_task
        issue = {
            "number": item["number"],
            "title": item["title"],
            "body": item["body"],
            "url": item["url"],
            "labels": [{"name": l} for l in item["labels"]],
        }
        dispatch_issue = issue
        dispatch_item = item
        try:
            dispatch_issue = _maybe_decompose_issue(cfg, pk, pcfg, rcfg, issue)
            if dispatch_issue is not issue:
                dispatch_item = {
                    "number": dispatch_issue["number"],
                    "item_id": dispatch_issue.get("item_id"),
                }
        except Exception as e:
            print(f"Warning: decomposition failed for #{item['number']}: {e}")
            dispatch_issue = issue
            dispatch_item = item

        task_id, task_md = build_mailbox_task(cfg, pk, rcfg, dispatch_issue)
        task_path = paths["INBOX"] / f"{task_id}.md"
        task_path.write_text(task_md, encoding="utf-8")

        # Update labels for visibility
        edit_issue_labels(
            repo_full,
            dispatch_item["number"],
            add=["in-progress", "agent-dispatched"],
            remove=pcfg.get("required_labels", []),
        )

        add_issue_comment(
            repo_full,
            dispatch_item["number"],
            f"🤖 Dispatched to orchestrator.\n\nTask ID: `{task_id}`\nProject key: `{pk}`",
        )

        # Set project Status to In Progress
        in_progress_value = pcfg.get("in_progress_value", "In Progress")
<<<<<<< HEAD
        try:
            _set_project_status(info, item["item_id"], in_progress_value)
        except Exception as e:
            print(f"Warning: failed to set project status: {e}")
=======
        option_id = info["status_options"].get(in_progress_value)
        if info["status_field_id"] and option_id and dispatch_item.get("item_id"):
            try:
                set_item_status(
                    info["project_id"],
                    dispatch_item["item_id"],
                    info["status_field_id"],
                    option_id,
                )
            except Exception as e:
                print(f"Warning: failed to set project status: {e}")
>>>>>>> 658e1bc (agent task-20260318-224806-task-task-decomposer-agent)

        print(f"Dispatched {repo_full}#{dispatch_item['number']} -> {task_path}")
        return True
    return False


_CLOSE_MSG = (
    "Closed automatically — this repository uses automated issue processing "
    "and only accepts issues from authorized authors.\n\n"
    "If you found a bug or have a feature request, please open a discussion "
    "or fork the repo instead. Thank you!"
)


def _close_untrusted_issues(cfg: dict):
    """Close open issues created by untrusted authors across all configured repos."""
    seen_repos: set[str] = set()
    for project_cfg in cfg.get("github_projects", {}).values():
        if not isinstance(project_cfg, dict):
            continue
        for repo_cfg in project_cfg.get("repos", []):
            repo = repo_cfg.get("github_repo", "")
            if not repo or repo in seen_repos:
                continue
            seen_repos.add(repo)

            try:
                raw = gh([
                    "issue", "list", "-R", repo, "--state", "open",
                    "--json", "number,author", "--limit", "50",
                ], check=False)
                if not raw:
                    continue
                issues = json.loads(raw)
            except Exception:
                continue

            for issue in issues:
                author = (issue.get("author") or {}).get("login", "")
                if is_trusted(author, cfg):
                    continue
                number = issue.get("number")
                if not number:
                    continue
                try:
                    gh([
                        "issue", "close", str(number), "-R", repo,
                        "--comment", _CLOSE_MSG,
                    ], check=False)
                    print(f"Closed untrusted issue {repo}#{number} (author: {author})")
                except Exception as e:
                    print(f"Warning: failed to close {repo}#{number}: {e}")


def dispatch_one():
    cfg = load_config()
    paths = runtime_paths(cfg)
    owner = cfg["github_owner"]

    # Housekeeping: close issues from untrusted authors
    _close_untrusted_issues(cfg)

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

    issue_lookup = _build_issue_lookup(queried)
    _requeue_unblocked_items(queried, repo_to_project, issue_lookup)

    # Dispatch first matching ready item (Status-based)
    for pn, (info, ready_items) in queried.items():
        dispatched = _dispatch_item(cfg, paths, owner, repo_to_project, info, ready_items, issue_lookup)
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
<<<<<<< HEAD
                    issue_with_label_set = {**issue, "labels": labels}
                    resolution = _resolve_issue_dependencies(repo_full, issue_with_label_set, {}, {})
                    if resolution["status"] == "blocked":
                        add_issue_comment(repo_full, issue["number"], f"Waiting for #{resolution['dependency']}")
                        print(f"Skipped {repo_full}#{issue['number']} — waiting for #{resolution['dependency']}")
                        continue
                    if resolution["status"] == "circular":
                        print(f"Warning: circular dependency detected for {repo_full}#{issue['number']}: {resolution['trail']}")
                        continue
                    if resolution["status"] in {"depth-limit", "unknown"}:
                        print(f"Warning: dependency check skipped dispatch for {repo_full}#{issue['number']}: {resolution}")
                        continue
                    task_id, task_md = build_mailbox_task(cfg, project_key, repo_cfg, issue)
=======
                    dispatch_issue = issue
                    try:
                        dispatch_issue = _maybe_decompose_issue(cfg, project_key, project_cfg, repo_cfg, issue)
                    except Exception as e:
                        print(f"Warning: decomposition failed for #{issue['number']}: {e}")
                        dispatch_issue = issue
                    task_id, task_md = build_mailbox_task(cfg, project_key, repo_cfg, dispatch_issue)
>>>>>>> 658e1bc (agent task-20260318-224806-task-task-decomposer-agent)
                    task_path = paths["INBOX"] / f"{task_id}.md"
                    task_path.write_text(task_md, encoding="utf-8")
                    edit_issue_labels(
                        repo_full, dispatch_issue["number"],
                        add=["in-progress", "agent-dispatched"],
                        remove=project_cfg.get("required_labels", []),
                    )
                    add_issue_comment(
                        repo_full, dispatch_issue["number"],
                        f"🤖 Dispatched to orchestrator.\n\nTask ID: `{task_id}`\nProject key: `{project_key}`",
                    )
                    print(f"Dispatched (label fallback) {repo_full}#{dispatch_issue['number']} -> {task_path}")
                    return

    print("No dispatchable issues found.")


if __name__ == "__main__":
    dispatch_one()
