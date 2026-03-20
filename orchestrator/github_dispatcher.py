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
    gh,
)
from orchestrator.task_formatter import format_task
from orchestrator.task_decomposer import decompose_issue, create_sub_issues
from orchestrator.outcome_attribution import parse_outcome_check_ids
from orchestrator.trust import is_trusted


SECTION_RE = re.compile(r"^##\s+(.+?)\n(.*?)(?=^##\s+|\Z)", re.MULTILINE | re.DOTALL)
DEPENDENCY_RE = re.compile(r"(?im)^\s*(?:depends on|blocked by)\s+#(\d+)\b")
PUBLISH_ACTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("git_commit", re.compile(r"\bgit\s+commit\b", re.IGNORECASE)),
    ("git_push", re.compile(r"\bgit\s+push\b", re.IGNORECASE)),
    ("push_branch", re.compile(r"\bpush(?:ing)?\s+(?:the\s+)?branch\b", re.IGNORECASE)),
    ("open_pr", re.compile(r"\b(?:open|create|submit|raise)\s+(?:a\s+|the\s+)?(?:pull request|pr)\b", re.IGNORECASE)),
    ("publish_changes", re.compile(r"\bpublish(?:ing|ed)?\b", re.IGNORECASE)),
)
MAX_DEPENDENCY_DEPTH = 3
MISSING_PUBLISH_CAPABILITY_LABEL = "dispatch:missing-publish-capability"
MISSING_PUBLISH_CAPABILITY_CODE = "missing_publish_capability"
RETRY_DECISION_SECTION = "retry decision"
RETRY_DECISION_APPLIED_MARKER = "<!-- agent-os-retry-decision-applied -->"
VALID_RETRY_ACTIONS = {"retry", "reroute", "stop"}
VALID_REROUTE_AGENTS = {"auto", "claude", "codex", "gemini", "deepseek"}


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")[:50] or "task"


def now_ts() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _now_iso() -> str:
    return datetime.now().isoformat()


def parse_issue_body(body: str) -> dict:
    sections = {}
    for name, content in SECTION_RE.findall(body or ""):
        sections[name.strip().lower()] = content.strip()

    return {
        "goal": sections.get("goal", "").strip(),
        "success_criteria": sections.get("success criteria", "").strip(),
        "task_type": sections.get("task type", "").strip().lower() or "implementation",
        "agent_preference": sections.get("agent preference", "").strip().lower() or "auto",
        "outcome_checks": parse_outcome_check_ids(sections.get("outcome checks", "")),
        "constraints": sections.get("constraints", "").strip(),
        "context": sections.get("context", "").strip(),
        "base_branch": sections.get("base branch", "").strip(),
        "branch": sections.get("branch", "").strip(),
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

    raw_parsed = parse_issue_body(body_text)
    # Try LLM formatting first, then fill any missing control fields from the raw issue body.
    formatter_model = cfg.get("formatter_model")
    parsed = format_task(title, body_text, model=formatter_model)
    if parsed is None:
        parsed = raw_parsed
    else:
        parsed = dict(parsed)
        for key, value in raw_parsed.items():
            if value and not parsed.get(key):
                parsed[key] = value

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
        "branch": parsed.get("branch") or f"agent/{task_id}",
        "base_branch": parsed.get("base_branch") or cfg["default_base_branch"],
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
        "prompt_snapshot_path": str(Path(cfg.get("root_dir", Path.cwd())) / "runtime" / "prompts" / f"{task_id}.txt"),
        "outcome_check_ids": parsed.get("outcome_checks", []),
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


def _render_mailbox_task(meta: dict, body: str) -> str:
    frontmatter_text = yaml.safe_dump(meta, sort_keys=False).strip()
    return f"---\n{frontmatter_text}\n---\n\n{body.rstrip()}\n"


def _parse_mailbox_task(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, flags=re.DOTALL)
    if not match:
        raise ValueError(f"Invalid task format in {path}")
    return yaml.safe_load(match.group(1)) or {}, match.group(2).strip()


def _normalize_retry_decision_payload(payload: dict) -> dict | None:
    normalized = {str(key).strip().lower(): value for key, value in payload.items()}
    action = str(normalized.get("action", "")).strip().lower()
    if action not in VALID_RETRY_ACTIONS:
        return None

    reason = str(
        normalized.get("reason")
        or normalized.get("summary")
        or normalized.get("next_step")
        or ""
    ).strip()
    decision = {
        "action": action,
        "reason": reason or "No reason provided.",
    }

    reroute_agent = str(
        normalized.get("agent")
        or normalized.get("target_agent")
        or normalized.get("reroute_to")
        or ""
    ).strip().lower()
    if action == "reroute":
        if reroute_agent not in VALID_REROUTE_AGENTS:
            return None
        decision["agent"] = reroute_agent

    return decision


def parse_retry_decision(note_text: str) -> dict | None:
    if RETRY_DECISION_APPLIED_MARKER in (note_text or ""):
        return None

    sections = {}
    for name, content in SECTION_RE.findall(note_text or ""):
        sections[name.strip().lower()] = content.strip()

    section = sections.get(RETRY_DECISION_SECTION, "").strip()
    if not section:
        return None

    fenced = re.fullmatch(r"```(?:json|yaml|yml)?\s*\n(.*?)\n```", section, flags=re.DOTALL)
    if fenced:
        section = fenced.group(1).strip()

    for loader in (json.loads, yaml.safe_load):
        try:
            payload = loader(section)
        except Exception:
            continue
        if isinstance(payload, dict):
            return _normalize_retry_decision_payload(payload)
    return None


def _find_originating_task(paths: dict, parent_task_id: str) -> tuple[Path, dict, str] | tuple[None, None, None]:
    candidates: list[tuple[float, Path, dict, str]] = []
    for base in (paths["ESCALATED"], paths["BLOCKED"]):
        for task_path in base.glob("*.md"):
            if task_path.name.endswith("-escalation.md"):
                continue
            try:
                meta, body = _parse_mailbox_task(task_path)
            except Exception:
                continue
            if meta.get("task_id") == parent_task_id or meta.get("parent_task_id") == parent_task_id:
                candidates.append((task_path.stat().st_mtime, task_path, meta, body))

    if not candidates:
        return None, None, None

    candidates.sort(key=lambda item: item[0], reverse=True)
    _mtime, task_path, meta, body = candidates[0]
    return task_path, meta, body


def _find_issue_item(queried: dict[int, tuple[dict, list[dict]]], repo_full: str, issue_number: int) -> tuple[dict, dict] | tuple[None, None]:
    for info, _ready_items in queried.values():
        for item in info.get("items", []):
            if item.get("repo") == repo_full and item.get("number") == issue_number:
                return info, item
    return None, None


def _append_retry_decision_trace(note_path: Path, task_id: str, decision: dict):
    trace = (
        f"\n\n{RETRY_DECISION_APPLIED_MARKER}\n"
        f"## Dispatcher Action\n"
        f"- Applied at: {_now_iso()}\n"
        f"- Task: {task_id}\n"
        f"- Action: {decision['action']}\n"
        f"- Reason: {decision['reason']}\n"
    )
    if decision.get("agent"):
        trace += f"- Agent: {decision['agent']}\n"
    note_path.write_text(note_path.read_text(encoding="utf-8").rstrip() + trace, encoding="utf-8")


def _apply_retry_decision_to_task(
    cfg: dict,
    paths: dict,
    queried: dict[int, tuple[dict, list[dict]]],
    task_path: Path,
    note_path: Path,
    meta: dict,
    body: str,
    decision: dict,
):
    meta = dict(meta)
    meta["escalation_note"] = note_path.name
    meta["escalation_decision"] = decision["action"]
    meta["escalation_decision_reason"] = decision["reason"]
    meta["escalation_decision_applied_at"] = _now_iso()
    if decision.get("agent"):
        meta["escalation_decision_target_agent"] = decision["agent"]

    repo_full = meta.get("github_repo")
    issue_number = meta.get("github_issue_number")
    project_cfg = cfg.get("github_projects", {}).get(meta.get("github_project_key", ""), {})
    info = None
    item = None
    if repo_full and issue_number:
        info, item = _find_issue_item(queried, repo_full, int(issue_number))

    task_path.write_text(_render_mailbox_task(meta, body), encoding="utf-8")

    action = decision["action"]
    if action == "reroute":
        meta["agent"] = decision["agent"]
        task_path.write_text(_render_mailbox_task(meta, body), encoding="utf-8")

    comment_lines = [
        "## Dispatcher retry decision",
        f"**Task:** `{meta.get('task_id', 'unknown')}`",
        f"**Action:** `{action}`",
        f"**Reason:** {decision['reason']}",
    ]
    if decision.get("agent"):
        comment_lines.append(f"**Agent:** `{decision['agent']}`")

    if action in {"retry", "reroute"}:
        destination = paths["INBOX"] / task_path.name
        destination.write_text(_render_mailbox_task(meta, body), encoding="utf-8")
        if destination != task_path and task_path.exists():
            task_path.unlink()

        if repo_full and issue_number:
            edit_issue_labels(
                repo_full,
                int(issue_number),
                add=["ready"],
                remove=["blocked", "in-progress", "agent-dispatched"],
            )
            add_issue_comment(repo_full, int(issue_number), "\n".join(comment_lines))
            if info is not None and item is not None:
                _set_project_status(
                    info,
                    item["item_id"],
                    project_cfg.get("ready_value", "Ready"),
                )
    else:
        destination = paths["FAILED"] / task_path.name
        destination.write_text(_render_mailbox_task(meta, body), encoding="utf-8")
        if destination != task_path and task_path.exists():
            task_path.unlink()

        if repo_full and issue_number:
            gh([
                "api",
                f"repos/{repo_full}/issues/{issue_number}",
                "-X", "PATCH",
                "-f", "state=closed",
                "-f", "state_reason=not_planned",
            ], check=False)
            edit_issue_labels(
                repo_full,
                int(issue_number),
                add=["done"],
                remove=["blocked", "ready", "in-progress", "agent-dispatched"],
            )
            add_issue_comment(repo_full, int(issue_number), "\n".join(comment_lines))
            if info is not None and item is not None:
                _set_project_status(
                    info,
                    item["item_id"],
                    project_cfg.get("done_value", "Done"),
                )

    _append_retry_decision_trace(note_path, meta.get("task_id", "unknown"), decision)
    return action


def _consume_retry_decisions(cfg: dict, paths: dict, queried: dict[int, tuple[dict, list[dict]]]):
    for note_path in sorted(paths["ESCALATED"].glob("*-escalation.md")):
        note_text = note_path.read_text(encoding="utf-8")
        decision = parse_retry_decision(note_text)
        if decision is None:
            continue

        parent_task_id = note_path.name[:-len("-escalation.md")]
        task_path, meta, body = _find_originating_task(paths, parent_task_id)
        if task_path is None or meta is None:
            continue

        action = _apply_retry_decision_to_task(cfg, paths, queried, task_path, note_path, meta, body, decision)
        print(f"Applied escalation retry decision for {parent_task_id}: {action}")
        return True
    return False


def _mark_issue_blocked(repo_full: str, item: dict, info: dict, project_cfg: dict, dependency_number: int):
    blocked_value = project_cfg.get("blocked_value", "Blocked")
    try:
        _set_project_status(info, item["item_id"], blocked_value)
    except Exception as e:
        print(f"Warning: failed to set project status: {e}")
    add_issue_comment(repo_full, item["number"], f"Waiting for #{dependency_number}")


def _detect_publish_requirements(issue: dict, parsed: dict | None = None) -> list[str]:
    text_parts = [
        issue.get("title", ""),
        issue.get("body", ""),
    ]
    if parsed:
        text_parts.extend([
            parsed.get("goal", ""),
            parsed.get("success_criteria", ""),
            parsed.get("constraints", ""),
            parsed.get("context", ""),
        ])

    text = "\n".join(part for part in text_parts if part)
    matches: list[str] = []
    for code, pattern in PUBLISH_ACTION_PATTERNS:
        if pattern.search(text):
            matches.append(code)
    return matches


def _skip_missing_publish_capability(
    cfg: dict,
    repo_full: str,
    item: dict,
    info: dict | None,
    project_cfg: dict,
    requirements: list[str],
):
    blocked_value = project_cfg.get("blocked_value", "Blocked")
    if info is not None and item.get("item_id"):
        try:
            _set_project_status(info, item["item_id"], blocked_value)
        except Exception as e:
            print(f"Warning: failed to set project status: {e}")

    edit_issue_labels(
        repo_full,
        item["number"],
        add=["blocked", MISSING_PUBLISH_CAPABILITY_LABEL],
        remove=["ready", "in-progress", "agent-dispatched"],
    )

    payload = json.dumps(
        {
            "code": MISSING_PUBLISH_CAPABILITY_CODE,
            "requirements": requirements,
            "runtime_allow_push": bool(cfg.get("default_allow_push", True)),
        },
        sort_keys=True,
    )
    add_issue_comment(
        repo_full,
        item["number"],
        "\n".join([
            "<!-- agent-os-dispatch-skip",
            payload,
            "-->",
            "Blocked automatically: task requires publish capability but this runtime cannot push.",
        ]),
    )


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


def _try_decompose(cfg, repo_full, item, info, pcfg) -> list[dict] | None:
    """Attempt to decompose an epic issue into sub-issues.

    Returns list of created child issue dicts (first = to dispatch, rest = backlog),
    or None if the issue is atomic or decomposition fails.
    """
    decomposer_model = cfg.get("decomposer_model")
    result = decompose_issue(item["title"], item["body"], model=decomposer_model)
    if result is None or result["type"] == "atomic":
        return None

    sub_issues = result["sub_issues"]

    # Carry over priority labels from parent
    parent_labels = []
    for lbl in item.get("labels", set()):
        if lbl.startswith("prio:"):
            parent_labels.append(lbl)

    created = create_sub_issues(
        repo_full,
        item["number"],
        sub_issues,
        labels=parent_labels or None,
    )

    if len(created) < 2:
        # Not enough sub-issues were actually created — treat as atomic
        return None

    # Fetch the body of created sub-issues so we can build mailbox tasks
    for ci in created:
        try:
            raw = gh([
                "issue", "view", str(ci["number"]), "-R", repo_full,
                "--json", "body",
            ], check=False)
            if raw:
                ci["body"] = json.loads(raw).get("body", "")
        except Exception:
            ci["body"] = ""

    # Comment on parent linking to children
    child_list = "\n".join(f"- #{c['number']} {c['title']}" for c in created)
    add_issue_comment(
        repo_full,
        item["number"],
        f"🤖 Decomposed into sub-issues:\n\n{child_list}\n\nDispatching #{created[0]['number']} first.",
    )

    # Close the parent epic (work is tracked in sub-issues now)
    try:
        gh(["issue", "close", str(item["number"]), "-R", repo_full,
            "--comment", "Closed — tracked via sub-issues above."], check=False)
    except Exception as e:
        print(f"Warning: failed to close parent #{item['number']}: {e}")

    # Send remaining sub-issues (index 1+) to Backlog
    backlog_value = pcfg.get("backlog_value", "Backlog")
    ready_value = pcfg.get("ready_value", "Ready")
    for ci in created[1:]:
        # Add to project and set status to Backlog
        try:
            raw = gh([
                "project", "item-add", str(pcfg["project_number"]),
                "--owner", cfg["github_owner"],
                "--url", ci["url"],
                "--format", "json",
            ], check=False)
            if raw:
                item_data = json.loads(raw)
                child_item_id = item_data.get("id")
                if child_item_id:
                    option_id = info["status_options"].get(backlog_value)
                    if info["status_field_id"] and option_id:
                        set_item_status(
                            info["project_id"],
                            child_item_id,
                            info["status_field_id"],
                            option_id,
                        )
                        print(f"Sub-issue #{ci['number']} -> {backlog_value}")
        except Exception as e:
            print(f"Warning: failed to set backlog status for #{ci['number']}: {e}")

    # Add first child to project and set to Ready
    try:
        raw = gh([
            "project", "item-add", str(pcfg["project_number"]),
            "--owner", cfg["github_owner"],
            "--url", created[0]["url"],
            "--format", "json",
        ], check=False)
        if raw:
            item_data = json.loads(raw)
            child_item_id = item_data.get("id")
            if child_item_id:
                option_id = info["status_options"].get(ready_value)
                if info["status_field_id"] and option_id:
                    set_item_status(
                        info["project_id"],
                        child_item_id,
                        info["status_field_id"],
                        option_id,
                    )
    except Exception as e:
        print(f"Warning: failed to set ready status for #{created[0]['number']}: {e}")

    return created


def _dispatch_item(cfg, paths, owner, repo_to_project, info, ready_items, issue_lookup) -> bool:
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

        if not cfg.get("default_allow_push", True):
            requirements = _detect_publish_requirements(item)
            if requirements:
                _skip_missing_publish_capability(cfg, repo_full, item, info, pcfg, requirements)
                print(
                    f"Skipped {repo_full}#{item['number']} — "
                    f"{MISSING_PUBLISH_CAPABILITY_CODE}: {', '.join(requirements)}"
                )
                continue

        # --- Task decomposition: split epics into sub-issues ---
        decomp = _try_decompose(cfg, repo_full, item, info, pcfg)
        if decomp is not None:
            # Epic was decomposed — dispatch the first sub-issue
            first_child = decomp[0]
            child_issue = {
                "number": first_child["number"],
                "title": first_child["title"],
                "body": first_child.get("body", ""),
                "url": first_child["url"],
                "labels": [{"name": l} for l in item["labels"]],
            }
            task_id, task_md = build_mailbox_task(cfg, pk, rcfg, child_issue)
            task_path = paths["INBOX"] / f"{task_id}.md"
            task_path.write_text(task_md, encoding="utf-8")

            edit_issue_labels(
                repo_full, first_child["number"],
                add=["in-progress", "agent-dispatched"],
            )
            add_issue_comment(
                repo_full, first_child["number"],
                f"🤖 Dispatched to orchestrator.\n\nTask ID: `{task_id}`\nProject key: `{pk}`",
            )
            print(f"Dispatched (decomposed child) {repo_full}#{first_child['number']} -> {task_path}")
            return True

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
        try:
            _set_project_status(info, item["item_id"], in_progress_value)
        except Exception as e:
            print(f"Warning: failed to set project status: {e}")

        print(f"Dispatched {repo_full}#{item['number']} -> {task_path}")
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
    if _consume_retry_decisions(cfg, paths, queried):
        return

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
                    if not cfg.get("default_allow_push", True):
                        requirements = _detect_publish_requirements(issue)
                        if requirements:
                            _skip_missing_publish_capability(cfg, repo_full, issue, None, project_cfg, requirements)
                            print(
                                f"Skipped {repo_full}#{issue['number']} — "
                                f"{MISSING_PUBLISH_CAPABILITY_CODE}: {', '.join(requirements)}"
                            )
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
