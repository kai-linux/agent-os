from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from orchestrator.ci_artifact_validator import validate_ci_artifacts, format_validation_log
from orchestrator.ci_failure_signatures import extract_ci_failure_signature, extract_signature_from_body
from orchestrator.git_branches import resolve_base_branch
from orchestrator.paths import load_config, runtime_paths
from orchestrator.gh_project import (
    get_ready_items,
    set_item_status,
    edit_issue_labels,
    add_issue_comment,
    list_ready_issues,
    gh,
    gh_json,
)
from orchestrator.repo_modes import is_dispatcher_only_repo
from orchestrator.task_formatter import format_task
from orchestrator.task_decomposer import decompose_issue, create_sub_issues
from orchestrator.outcome_attribution import parse_outcome_check_ids
from orchestrator.agent_scorer import filter_healthy_agents, log_gate_decision, ADAPTIVE_HEALTH_WINDOW_DAYS, ADAPTIVE_HEALTH_THRESHOLD
from orchestrator.trust import is_trusted
from orchestrator.queue import (
    send_telegram,
    save_telegram_action,
    write_unblock_notes_artifact,
)


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
PUSH_NOT_READY_LABEL = "dispatch:push-not-ready"
PUSH_NOT_READY_CODE = "push_not_ready"
RETRY_DECISION_SECTION = "retry decision"
RETRY_DECISION_APPLIED_MARKER = "<!-- agent-os-retry-decision-applied -->"
VALID_RETRY_ACTIONS = {"retry", "reroute", "stop"}
VALID_REROUTE_AGENTS = {"auto", "claude", "codex", "gemini", "deepseek"}
UNASSIGNED_BLOCKED_SEEN_AT = "unassigned_blocked_seen_at"
VALID_ASSIGNABLE_AGENTS = {"auto", "claude", "codex", "gemini", "deepseek"}
VALID_FALLBACK_AGENTS = VALID_ASSIGNABLE_AGENTS - {"auto"}
AGENT_UNAVAILABLE_LABEL = "dispatch:agent-unavailable"
AGENT_UNAVAILABLE_CODE = "agent_unavailable"
CONTEXT_VALIDATION_LABEL = "dispatch:incomplete-context"
CI_CONTEXT_LINE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*-\s+PR:\s+.+$", re.IGNORECASE),
    re.compile(r"^\s*-\s+Failed checks:\s*$", re.IGNORECASE),
    re.compile(r"^\s*-\s+\*\*.+?\*\*:\s*`.+?`\s*(?:- .+)?$", re.IGNORECASE),
)
BLOCKED_ESCALATION_COMMENT_MARKER = "<!-- agent-os-blocked-task-escalation -->"
_DUPLICATE_PARENT_RE = re.compile(r"^## Duplicate CI Signature Parent\s*\n#?(\d+)\s*$", re.MULTILINE)


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


def validate_task_context(
    parsed: dict,
    issue: dict,
    repo_cfg: dict,
) -> dict:
    """Non-blocking validation of task context fields.

    Returns {"complete": bool, "missing": list[str], "present": list[str]}.
    """
    missing: list[str] = []
    present: list[str] = []

    if issue.get("url"):
        present.append("issue_link")
    else:
        missing.append("issue_link")

    if repo_cfg.get("github_repo"):
        present.append("repo")
    else:
        missing.append("repo")

    goal = parsed.get("goal", "").strip()
    if goal and goal.lower() not in ("none", "n/a", ""):
        present.append("task_description")
    else:
        missing.append("task_description")

    criteria = parsed.get("success_criteria", "").strip()
    if criteria and criteria.lower() not in ("none", "n/a", ""):
        present.append("acceptance_criteria")
    else:
        missing.append("acceptance_criteria")

    return {
        "complete": len(missing) == 0,
        "missing": missing,
        "present": present,
    }


def _record_context_completeness(
    cfg: dict,
    task_id: str,
    task_type: str,
    agent: str,
    validation: dict,
) -> None:
    """Append a context completeness record to the telemetry log."""
    metrics_dir = Path(cfg.get("root_dir", ".")).expanduser() / "runtime" / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    log_file = metrics_dir / "context_completeness.jsonl"
    record = {
        "timestamp": datetime.now().isoformat(),
        "task_id": task_id,
        "task_type": task_type,
        "agent": agent,
        "complete": validation["complete"],
        "missing": validation["missing"],
        "present": validation["present"],
    }
    try:
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass


def _preserve_ci_context(raw_context: str, formatted_context: str) -> str:
    raw = str(raw_context or "").strip()
    formatted = str(formatted_context or "").strip()
    if not raw:
        return formatted

    preserved_lines = [
        line.rstrip()
        for line in raw.splitlines()
        if any(pattern.match(line) for pattern in CI_CONTEXT_LINE_PATTERNS)
    ]
    if not preserved_lines:
        return formatted or raw

    merged_lines = formatted.splitlines() if formatted else []
    existing = {line.strip() for line in merged_lines if line.strip()}
    for line in preserved_lines:
        if line.strip() not in existing:
            merged_lines.append(line)
            existing.add(line.strip())
    return "\n".join(merged_lines).strip() or raw


def parse_issue_dependencies(body: str) -> list[int]:
    deps = []
    for match in DEPENDENCY_RE.findall(body or ""):
        number = int(match)
        if number not in deps:
            deps.append(number)
    return deps


def _extract_duplicate_parent_issue(body: str) -> int | None:
    match = _DUPLICATE_PARENT_RE.search(body or "")
    if not match:
        return None
    return int(match.group(1))


def _issue_debug_signature(issue: dict) -> str | None:
    body = issue.get("body", "") or ""
    parsed = parse_issue_body(body)
    if parsed.get("task_type") != "debugging":
        return None
    return extract_ci_failure_signature(issue.get("title", "") or "", body)


def _attach_duplicate_signature_dependency(
    repo_full: str,
    dependent_issue: dict,
    primary_issue: dict,
    info: dict | None,
    project_cfg: dict,
    signature: str,
):
    dependent_number = int(dependent_issue["number"])
    primary_number = int(primary_issue["number"])
    body = dependent_issue.get("body", "") or ""
    dependencies = parse_issue_dependencies(body)
    if dependencies:
        return

    signature_section = ""
    if not extract_signature_from_body(body):
        signature_section = f"\n## CI Failure Signature\n{signature}\n"
    suffix = (
        f"\n\nDepends on #{primary_number}\n"
        f"{signature_section}\n"
        f"## Duplicate CI Signature Parent\n#{primary_number}\n"
    )
    gh([
        "api",
        f"repos/{repo_full}/issues/{dependent_number}",
        "-X", "PATCH",
        "-f", f"body={body.rstrip()}{suffix}",
    ], check=False)

    edit_issue_labels(
        repo_full,
        dependent_number,
        add=["blocked"],
        remove=["ready", "in-progress", "agent-dispatched"],
    )
    add_issue_comment(
        repo_full,
        dependent_number,
        (
            f"Blocked automatically behind #{primary_number} because it matches the same CI failure signature.\n\n"
            f"`{signature}`"
        ),
    )
    if info is not None and dependent_issue.get("item_id"):
        _set_project_status(info, dependent_issue["item_id"], project_cfg.get("blocked_value", "Blocked"))


def _cluster_duplicate_debug_issues(
    cfg: dict,
    repo_full: str,
    primary_issue: dict,
    info: dict | None,
    project_cfg: dict,
    ready_items: list[dict],
):
    signature = _issue_debug_signature(primary_issue)
    if not signature:
        return

    for candidate in ready_items:
        if candidate.get("repo") != repo_full or candidate.get("number") == primary_issue.get("number"):
            continue
        if parse_issue_dependencies(candidate.get("body", "") or ""):
            continue
        if _extract_duplicate_parent_issue(candidate.get("body", "") or ""):
            continue
        if _issue_debug_signature(candidate) != signature:
            continue
        _attach_duplicate_signature_dependency(repo_full, candidate, primary_issue, info, project_cfg, signature)


def _repo_agent_fallbacks(cfg: dict, project_key: str) -> dict:
    project_cfg = cfg.get("github_projects", {}).get(project_key, {})
    if isinstance(project_cfg, dict):
        fallbacks = project_cfg.get("agent_fallbacks", {})
        if isinstance(fallbacks, dict):
            return fallbacks
    return {}


def _build_requested_agent_chain(cfg: dict, project_key: str, task_type: str, requested_agent: str) -> list[str]:
    fallback_map = _repo_agent_fallbacks(cfg, project_key) or cfg.get("agent_fallbacks", {})
    default_task_type = cfg["default_task_type"]
    task_chain = list(
        fallback_map.get(
            task_type,
            fallback_map.get(default_task_type, ["codex", "claude", "gemini", "deepseek"]),
        )
    )
    if requested_agent in {"", "auto"}:
        chain = task_chain
    else:
        chain = [requested_agent] + [agent for agent in task_chain if agent != requested_agent]

    deduped: list[str] = []
    for agent in chain:
        normalized = str(agent).strip().lower()
        if normalized and normalized not in deduped:
            deduped.append(normalized)
    return deduped


def _validated_agent_assignment(cfg: dict, project_key: str, task_type: str, requested_agent: str) -> str:
    requested_agent = str(requested_agent or cfg["default_agent"]).strip().lower()
    if requested_agent not in VALID_ASSIGNABLE_AGENTS:
        raise ValueError(
            f"Unsupported agent preference: {requested_agent}. "
            f"Expected one of: {', '.join(sorted(VALID_ASSIGNABLE_AGENTS))}."
        )

    chain = _build_requested_agent_chain(cfg, project_key, task_type, requested_agent)
    invalid_candidates = [agent for agent in chain if agent not in VALID_FALLBACK_AGENTS]
    if invalid_candidates:
        raise ValueError(
            f"Unsupported agent fallback(s) for task_type={task_type!r}: "
            + ", ".join(invalid_candidates)
            + f". Expected only: {', '.join(sorted(VALID_FALLBACK_AGENTS))}."
        )

    if not chain:
        raise ValueError(
            f"No agents configured for task_type={task_type!r} "
            f"(project_key={project_key!r}, requested_agent={requested_agent!r})."
        )

    metrics_file = Path(cfg.get("root_dir", ".")).expanduser() / "runtime" / "metrics" / "agent_stats.jsonl"
    # Adaptive gate: skip agents with <25% success rate over 7 days
    chain, adaptive_skipped = filter_healthy_agents(
        chain,
        metrics_file,
        threshold=ADAPTIVE_HEALTH_THRESHOLD,
        window_days=ADAPTIVE_HEALTH_WINDOW_DAYS,
        task_type=task_type,
    )
    for agent, stats in adaptive_skipped.items():
        print(f"agent={agent} skipped: {round(stats['rate'] * 100)}% success rate (7d adaptive health gate)")
    if adaptive_skipped:
        log_gate_decision(
            metrics_file.parent,
            gate="adaptive_7d_25pct",
            skipped=adaptive_skipped,
            passed=chain,
            context=f"dispatcher:resolve_agent task_type={task_type}",
        )
    # 24h dispatcher gate: with the chain trimmed to [claude, codex] one bad
    # day at the prior 80% bar killed all dispatch. Lower to 50% and require
    # 10 same-day samples so a small noisy window cannot gate everything out.
    dispatcher_gate_threshold = 0.50
    dispatcher_gate_min_tasks = 10
    healthy_chain, skipped_agents = filter_healthy_agents(
        chain,
        metrics_file,
        task_type=task_type,
        threshold=dispatcher_gate_threshold,
        min_task_count=dispatcher_gate_min_tasks,
    )
    if not healthy_chain:
        skipped_summary = ", ".join(
            f"{agent} ({round(stats['rate'] * 100, 1)}% success over {stats['total']} task(s) in the last 24h)"
            for agent, stats in skipped_agents.items()
        ) or "none"
        raise ValueError(
            f"No healthy agents available for task_type={task_type!r}. "
            f"All configured candidates are at or below the {round(dispatcher_gate_threshold * 100)}% "
            f"success-rate gate (min {dispatcher_gate_min_tasks} tasks/24h): {skipped_summary}."
        )

    return requested_agent


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
    parsed["context"] = _preserve_ci_context(
        raw_parsed.get("context", ""),
        parsed.get("context", ""),
    )

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
    task_type = parsed["task_type"] or cfg["default_task_type"]
    agent = _validated_agent_assignment(cfg, project_key, task_type, agent)

    frontmatter = {
        "task_id": task_id,
        "repo": repo_cfg["local_repo"],
        "agent": agent,
        "task_type": task_type,
        "branch": parsed.get("branch") or f"agent/{task_id}",
        "base_branch": resolve_base_branch(
            Path(repo_cfg["local_repo"]),
            parsed.get("base_branch"),
            cfg["default_base_branch"],
        ),
        "allow_push": cfg["default_allow_push"],
        "attempt": 1,
        "max_attempts": cfg["default_max_attempts"],
        "max_runtime_minutes": cfg["max_runtime_minutes"],
        "model_attempts": [],
        "priority": priority,
        "github_project_key": project_key,
        "github_repo": repo_cfg["github_repo"],
        "github_issue_number": issue["number"],
        "github_issue_title": issue["title"],
        "github_issue_url": issue["url"],
        "prompt_snapshot_path": str(Path(cfg.get("root_dir", Path.cwd())) / "runtime" / "prompts" / f"{task_id}.txt"),
        "outcome_check_ids": parsed.get("outcome_checks", []),
    }

    # Persist failed CI check names as structured metadata so the CI
    # verification gate survives follow-up body reformatting (PR-98 RCA).
    if task_type == "debugging":
        ci_checks = _extract_ci_checks_from_body(body_text)
        if ci_checks:
            frontmatter["failed_checks"] = [c.get("name", "") for c in ci_checks if c.get("name")]

    # Non-blocking context validation gate
    ctx_validation = validate_task_context(parsed, issue, repo_cfg)
    frontmatter["context_complete"] = ctx_validation["complete"]
    if not ctx_validation["complete"]:
        frontmatter["context_missing"] = ctx_validation["missing"]
        print(
            f"Warning: incomplete task context for #{issue.get('number', '?')}: "
            f"missing {', '.join(ctx_validation['missing'])}"
        )
    _record_context_completeness(cfg, task_id, task_type, agent, ctx_validation)

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


def _parse_markdown_heading(text: str, heading: str, next_headings: list[str]) -> str:
    if next_headings:
        pattern = rf"(?ms)^# {re.escape(heading)}\s*\n(.*?)(?=^# (?:{'|'.join(map(re.escape, next_headings))})\s*$|\Z)"
    else:
        pattern = rf"(?ms)^# {re.escape(heading)}\s*\n(.*)$"
    match = re.search(pattern, text or "")
    return match.group(1).strip() if match else ""


def _parse_log_result_section(text: str, label: str, next_labels: list[str]) -> str:
    if next_labels:
        pattern = rf"(?ms)^{re.escape(label)}:\s*(.*?)(?=^(?:{'|'.join(map(re.escape, next_labels))}):|\Z)"
    else:
        pattern = rf"(?ms)^{re.escape(label)}:\s*(.*)$"
    match = re.search(pattern, text or "")
    return match.group(1).strip() if match else ""


def _extract_task_result_snapshot(paths: dict, task_id: str, body: str) -> dict:
    log_path = paths["LOGS"] / f"{task_id}.log"
    if log_path.exists():
        text = log_path.read_text(encoding="utf-8")
        status_match = re.findall(r"Worker status from .*?: (\w+)", text)
        status = status_match[-1].strip().lower() if status_match else ""
        summary = _parse_log_result_section(
            text,
            "SUMMARY",
            ["DONE", "BLOCKERS", "NEXT_STEP", "FILES_CHANGED", "TESTS_RUN", "DECISIONS", "RISKS", "ATTEMPTED_APPROACHES", "MANUAL_STEPS"],
        )
        blocker_code = _parse_log_result_section(
            text,
            "BLOCKER_CODE",
            ["SUMMARY", "DONE", "BLOCKERS", "NEXT_STEP", "FILES_CHANGED", "TESTS_RUN", "DECISIONS", "RISKS", "ATTEMPTED_APPROACHES", "MANUAL_STEPS"],
        ).splitlines()[0].strip() if "BLOCKER_CODE:" in text else ""
        blockers = _parse_log_result_section(
            text,
            "BLOCKERS",
            ["NEXT_STEP", "FILES_CHANGED", "TESTS_RUN", "DECISIONS", "RISKS", "ATTEMPTED_APPROACHES", "MANUAL_STEPS"],
        )
        return {
            "status": status or "blocked",
            "summary": summary or "No summary captured.",
            "blocker_code": blocker_code or "unknown",
            "blockers": [line.strip() for line in blockers.splitlines() if line.strip()] or ["- None"],
        }

    prior_summary = _parse_markdown_heading(
        body,
        "Prior Summary",
        ["Prior Blocker Code", "Prior Progress", "Known Blockers", "Files Changed So Far", "Tests Run So Far", "Prior Decisions", "Known Risks", "Avoid Repeating These Approaches", "Models Already Tried In This Task Lineage"],
    )
    prior_blocker = _parse_markdown_heading(
        body,
        "Prior Blocker Code",
        ["Prior Progress", "Known Blockers", "Files Changed So Far", "Tests Run So Far", "Prior Decisions", "Known Risks", "Avoid Repeating These Approaches", "Models Already Tried In This Task Lineage"],
    )
    known_blockers = _parse_markdown_heading(
        body,
        "Known Blockers",
        ["Files Changed So Far", "Tests Run So Far", "Prior Decisions", "Known Risks", "Avoid Repeating These Approaches", "Models Already Tried In This Task Lineage"],
    )
    return {
        "status": "blocked",
        "summary": prior_summary or "No summary captured.",
        "blocker_code": prior_blocker or "unknown",
        "blockers": [line.strip() for line in known_blockers.splitlines() if line.strip()] or ["- None"],
    }


def _collect_lineage_entries(paths: dict, parent_task_id: str) -> list[dict]:
    candidates: list[dict] = []
    for base_key in ("INBOX", "PROCESSING", "DONE", "FAILED", "BLOCKED", "ESCALATED"):
        base = paths[base_key]
        for task_path in sorted(base.glob("*.md")):
            if task_path.name.endswith("-escalation.md"):
                continue
            try:
                meta, body = _parse_mailbox_task(task_path)
            except Exception:
                continue
            task_id = str(meta.get("task_id", "")).strip()
            lineage_id = str(meta.get("parent_task_id") or task_id).strip()
            if task_id != parent_task_id and lineage_id != parent_task_id:
                continue
            snapshot = _extract_task_result_snapshot(paths, task_id or task_path.stem, body)
            candidates.append({
                "path": task_path,
                "meta": meta,
                "body": body,
                "task_id": task_id or task_path.stem,
                "attempt": int(meta.get("attempt", 1) or 1),
                "mtime": task_path.stat().st_mtime,
                "state": base_key.lower(),
                **snapshot,
            })

    candidates.sort(key=lambda item: (item["attempt"], item["mtime"], item["task_id"]))
    deduped: list[dict] = []
    seen_ids: set[str] = set()
    for item in candidates:
        if item["task_id"] in seen_ids:
            continue
        seen_ids.add(item["task_id"])
        deduped.append(item)
    return deduped


def _blocked_task_error_patterns(entries: list[dict]) -> tuple[list[str], list[str]]:
    blocker_codes: list[str] = []
    blocker_counts: dict[str, int] = {}
    bullet_counts: dict[str, int] = {}
    for entry in entries:
        code = str(entry.get("blocker_code", "")).strip() or "unknown"
        blocker_counts[code] = blocker_counts.get(code, 0) + 1
        if code not in blocker_codes:
            blocker_codes.append(code)
        for raw_bullet in entry.get("blockers", []):
            bullet = raw_bullet[2:].strip() if raw_bullet.startswith("- ") else raw_bullet.strip()
            if not bullet or bullet.lower() == "none":
                continue
            bullet_counts[bullet] = bullet_counts.get(bullet, 0) + 1

    patterns: list[str] = []
    for code, count in sorted(blocker_counts.items(), key=lambda item: (-item[1], item[0])):
        patterns.append(f"`{code}` repeated {count} time(s)")
    for bullet, count in sorted(bullet_counts.items(), key=lambda item: (-item[1], item[0]))[:3]:
        if count > 1:
            patterns.append(f"\"{bullet}\" repeated {count} time(s)")
    return blocker_codes, patterns or ["No repeated error pattern extracted from blockers."]


def _format_attempt_log(entries: list[dict]) -> list[str]:
    lines: list[str] = []
    for entry in entries:
        summary = str(entry.get("summary", "No summary captured.")).strip().replace("\n", " ")
        if len(summary) > 160:
            summary = summary[:157] + "..."
        lines.append(
            f"- Attempt {entry['attempt']} | task `{entry['task_id']}` | state `{entry['state']}` | blocker `{entry.get('blocker_code', 'unknown')}` | {summary}"
        )
    return lines or ["- No prior attempt records found."]


def _format_blocked_elapsed(task_path: Path) -> str:
    age = datetime.now() - datetime.fromtimestamp(task_path.stat().st_mtime)
    if age < timedelta(hours=1):
        minutes = max(1, int(age.total_seconds() // 60))
        return f"{minutes} minute(s)"
    if age < timedelta(days=1):
        return f"{int(age.total_seconds() // 3600)} hour(s)"
    return f"{age.days} day(s)"


def _resolve_prompt_snapshot_path(meta: dict) -> str:
    """Return the prompt snapshot path from task metadata, or 'none'."""
    return meta.get("prompt_snapshot_path") or "none"


def _build_blocked_task_escalation_note(meta: dict, body: str, entries: list[dict], task_path: Path, reason: str) -> str:
    blocker_codes, error_patterns = _blocked_task_error_patterns(entries)
    return f"""# Escalation Note

## Parent Task ID
{meta.get("parent_task_id", meta.get("task_id", "unknown"))}

## Branch
{meta.get("branch", "unknown")}

## Repo
{meta.get("repo", "unknown")}

## Task Type
{meta.get("task_type", "unknown")}

## Prompt Snapshot
{_resolve_prompt_snapshot_path(meta)}

## Escalation Trigger
{reason}

## Blocked Age
{_format_blocked_elapsed(task_path)}

## Previous Blocker Codes
{", ".join(blocker_codes) if blocker_codes else "unknown"}

## Error Patterns
{chr(10).join(f"- {pattern}" for pattern in error_patterns)}

## Attempt Log
{chr(10).join(_format_attempt_log(entries))}

## Original Task
{body}
"""


def _build_blocked_task_comment(meta: dict, task_path: Path, entries: list[dict], reason: str) -> str:
    blocker_codes, error_patterns = _blocked_task_error_patterns(entries)
    attempt_log = _format_attempt_log(entries)
    return f"""## Blocked task escalation
{BLOCKED_ESCALATION_COMMENT_MARKER}

**Task ID:** `{meta.get("task_id", task_path.stem)}`
**Parent Task ID:** `{meta.get("parent_task_id", meta.get("task_id", task_path.stem))}`
**Branch:** `{meta.get("branch", "unknown")}`
**Prompt snapshot:** `{_resolve_prompt_snapshot_path(meta)}`
**Trigger:** {reason}
**Blocked age:** {_format_blocked_elapsed(task_path)}

### Previous blocker codes
{", ".join(f"`{code}`" for code in blocker_codes) if blocker_codes else "`unknown`"}

### Error patterns
{chr(10).join(f"- {pattern}" for pattern in error_patterns)}

### Attempt log
{chr(10).join(attempt_log)}
"""


def _blocked_escalation_reply_markup(action_id: str) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "Retry", "callback_data": f"esc:{action_id}:retry"},
            {"text": "Close", "callback_data": f"esc:{action_id}:close"},
            {"text": "Skip", "callback_data": f"esc:{action_id}:skip"},
        ]]
    }


def _build_blocked_task_telegram_message(meta: dict, task_path: Path, entries: list[dict], reason: str, note_path: Path) -> str:
    blocker_codes, error_patterns = _blocked_task_error_patterns(entries)
    prompt_snap = _resolve_prompt_snapshot_path(meta)
    lines = [
        "🛑 Blocked task escalation",
        f"Issue: {meta.get('github_issue_url', 'n/a')}",
        f"Task ID: {meta.get('task_id', task_path.stem)}",
        f"Prompt snapshot: {prompt_snap}",
        f"Attempts: {max((entry['attempt'] for entry in entries), default=int(meta.get('attempt', 1) or 1))}",
        f"Blocked age: {_format_blocked_elapsed(task_path)}",
        f"Trigger: {reason}",
        f"Blocker codes: {', '.join(blocker_codes) if blocker_codes else 'unknown'}",
        "",
        "Error patterns:",
        *[f"- {pattern}" for pattern in error_patterns[:3]],
        "",
        "Attempt log:",
        *_format_attempt_log(entries)[:4],
        "",
        f"Note: {note_path.name}",
    ]
    text = "\n".join(lines).strip()
    return text if len(text) <= 4000 else text[:3997] + "..."


def _create_blocked_task_action(meta: dict, note_path: Path, chat_id: str) -> dict:
    now = datetime.now(timezone.utc)
    action_id = os.urandom(6).hex()
    return {
        "action_id": action_id,
        "type": "blocked_task_escalation",
        "status": "pending",
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=48)).isoformat(),
        "chat_id": str(chat_id),
        "message_id": None,
        "task_id": meta.get("task_id"),
        "github_project_key": meta.get("github_project_key"),
        "github_repo": meta.get("github_repo"),
        "github_issue_number": meta.get("github_issue_number"),
        "github_issue_url": meta.get("github_issue_url"),
        "prompt_snapshot_path": meta.get("prompt_snapshot_path"),
        "escalation_note": note_path.name,
    }


def _blocked_task_escalation_reason(task_path: Path, meta: dict, attempt_threshold: int, age_hours: int) -> str | None:
    attempt = int(meta.get("attempt", 1) or 1)
    age = datetime.now() - datetime.fromtimestamp(task_path.stat().st_mtime)
    if attempt >= attempt_threshold:
        return f"Blocked task reached retry attempt {attempt} (threshold: {attempt_threshold})."
    if age >= timedelta(hours=age_hours):
        return f"Blocked task has remained unowned for {_format_blocked_elapsed(task_path)} (threshold: {age_hours} hour(s))."
    return None


def _escalate_over_retried_blocked_tasks(cfg: dict, paths: dict) -> bool:
    attempt_threshold = int(cfg.get("blocked_escalation_attempt_threshold", 3) or 3)
    age_hours = int(cfg.get("blocked_escalation_age_hours", 24) or 24)
    chat_id = str(cfg.get("telegram_chat_id", "")).strip()

    for task_path in sorted(paths["BLOCKED"].glob("*.md")):
        try:
            meta, body = _parse_mailbox_task(task_path)
        except Exception:
            continue

        repo_full = str(meta.get("github_repo", "")).strip()
        issue_number = meta.get("github_issue_number")
        project_key = str(meta.get("github_project_key", "")).strip()
        if not repo_full or not issue_number or not project_key:
            continue

        # Skip escalation for dispatcher-only repos and closed issues
        if is_dispatcher_only_repo(cfg, repo_full):
            shutil.move(str(task_path), str(paths["DONE"] / task_path.name))
            print(f"Skipped escalation for dispatcher-only repo {repo_full}: {task_path.stem}")
            continue
        try:
            snapshot = gh_json(["issue", "view", str(issue_number), "-R", repo_full, "--json", "state"]) or {}
            if str(snapshot.get("state", "")).upper() == "CLOSED":
                shutil.move(str(task_path), str(paths["DONE"] / task_path.name))
                print(f"Skipped escalation for closed issue #{issue_number}: {task_path.stem}")
                continue
        except Exception:
            pass

        reason = _blocked_task_escalation_reason(task_path, meta, attempt_threshold, age_hours)
        if not reason:
            continue

        parent_task_id = str(meta.get("parent_task_id") or meta.get("task_id") or task_path.stem).strip()
        note_path = paths["ESCALATED"] / f"{parent_task_id}-escalation.md"
        if note_path.exists():
            continue

        entries = _collect_lineage_entries(paths, parent_task_id)
        note_path.write_text(
            _build_blocked_task_escalation_note(meta, body, entries, task_path, reason),
            encoding="utf-8",
        )
        add_issue_comment(repo_full, int(issue_number), _build_blocked_task_comment(meta, task_path, entries, reason))

        if chat_id:
            action = _create_blocked_task_action(meta, note_path, chat_id)
            save_telegram_action(paths["TELEGRAM_ACTIONS"], action)
            message_id = send_telegram(
                cfg,
                _build_blocked_task_telegram_message(meta, task_path, entries, reason, note_path),
                reply_markup=_blocked_escalation_reply_markup(action["action_id"]),
            )
            action["message_id"] = message_id
            save_telegram_action(paths["TELEGRAM_ACTIONS"], action)

        print(f"Escalated blocked task for human review: {meta.get('task_id', task_path.stem)}")
        return True

    return False


def _build_unassigned_blocked_escalation_note(meta: dict, body: str) -> str:
    parent_task_id = meta.get("parent_task_id", meta.get("task_id", "unknown"))
    return f"""# Escalation Note

## Parent Task ID
{parent_task_id}

## Branch
{meta.get("branch", "unknown")}

## Repo
{meta.get("repo", "unknown")}

## Task Type
{meta.get("task_type", "unknown")}

## Models Tried
None

## Final Status
blocked

## Blocker Code
manual_intervention_required

## Summary
Blocked task has no assigned agent after one scheduler cycle.

## Original Task
{body}

## Completed
- None

## Blockers
- Task remains blocked with `agent=none`.
- Current blocker context: no assigned agent is available to take the next attempt.

## Next Suggested Step
Assign a valid agent or add an escalation retry decision so automation can resume safely.

## Files Changed
- None

## Tests Run
- None

## Decisions
- Scheduler escalated a blocked unassigned task after one cycle to avoid silent stalls.

## Risks
- Without reassignment or a retry decision, the task will remain escalated.

## Attempted Approaches
- Observed the blocked task for one scheduler cycle before escalation.
"""


def _escalate_unassigned_blocked_tasks(cfg: dict, paths: dict) -> bool:
    for task_path in sorted(paths["BLOCKED"].glob("*.md")):
        try:
            meta, body = _parse_mailbox_task(task_path)
        except Exception:
            continue

        if str(meta.get("agent", "")).strip().lower() != "none":
            continue

        # Skip for dispatcher-only repos and closed issues
        repo_full = str(meta.get("github_repo", "")).strip()
        issue_number = meta.get("github_issue_number")
        if repo_full and is_dispatcher_only_repo(cfg, repo_full):
            shutil.move(str(task_path), str(paths["DONE"] / task_path.name))
            print(f"Skipped escalation for dispatcher-only repo {repo_full}: {task_path.stem}")
            continue
        if repo_full and issue_number:
            try:
                snapshot = gh_json(["issue", "view", str(issue_number), "-R", repo_full, "--json", "state"]) or {}
                if str(snapshot.get("state", "")).upper() == "CLOSED":
                    shutil.move(str(task_path), str(paths["DONE"] / task_path.name))
                    print(f"Skipped escalation for closed issue #{issue_number}: {task_path.stem}")
                    continue
            except Exception:
                pass

        if not meta.get(UNASSIGNED_BLOCKED_SEEN_AT):
            meta = dict(meta)
            meta[UNASSIGNED_BLOCKED_SEEN_AT] = _now_iso()
            task_path.write_text(_render_mailbox_task(meta, body), encoding="utf-8")
            print(f"Marked blocked unassigned task for escalation next cycle: {meta.get('task_id', task_path.stem)}")
            return True

        meta = dict(meta)
        parent_task_id = meta.get("parent_task_id", meta.get("task_id", task_path.stem))
        note_path = paths["ESCALATED"] / f"{parent_task_id}-escalation.md"
        if not note_path.exists():
            note_path.write_text(_build_unassigned_blocked_escalation_note(meta, body), encoding="utf-8")

        meta["escalation_note"] = note_path.name
        meta["escalated_at"] = _now_iso()
        destination = paths["ESCALATED"] / task_path.name
        destination.write_text(_render_mailbox_task(meta, body), encoding="utf-8")
        if destination != task_path:
            task_path.unlink()
        print(f"Escalated blocked unassigned task: {meta.get('task_id', task_path.stem)}")
        return True

    return False


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


def _run_git_readiness(repo_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True,
        text=True,
    )


def _check_push_readiness(cfg: dict, repo_cfg: dict) -> dict:
    failures: list[dict[str, str]] = []

    if not bool(cfg.get("default_allow_push", True)):
        failures.append({
            "code": "allow_push_disabled",
            "detail": "default_allow_push is false",
        })

    if shutil.which("git") is None:
        failures.append({
            "code": "git_unavailable",
            "detail": "git executable is not available on PATH",
        })

    local_repo = str(repo_cfg.get("local_repo", "")).strip()
    if not local_repo:
        failures.append({
            "code": "missing_local_repo",
            "detail": "repo config does not define local_repo",
        })
        return {"ready": not failures, "failures": failures}

    repo_path = Path(local_repo)
    if not repo_path.is_dir():
        failures.append({
            "code": "missing_local_repo",
            "detail": f"local_repo does not exist: {repo_path}",
        })
        return {"ready": not failures, "failures": failures}

    if shutil.which("git") is None:
        return {"ready": not failures, "failures": failures}

    work_tree = _run_git_readiness(repo_path, "rev-parse", "--is-inside-work-tree")
    if work_tree.returncode != 0 or work_tree.stdout.strip() != "true":
        failures.append({
            "code": "not_git_repo",
            "detail": (work_tree.stderr or work_tree.stdout or "git rev-parse failed").strip(),
        })
        return {"ready": False, "failures": failures}

    common_dir = _run_git_readiness(repo_path, "rev-parse", "--git-common-dir")
    common_dir_text = common_dir.stdout.strip()
    if common_dir.returncode != 0 or not common_dir_text:
        failures.append({
            "code": "git_metadata_unavailable",
            "detail": (common_dir.stderr or common_dir.stdout or "git common dir lookup failed").strip(),
        })
    else:
        common_dir_path = Path(common_dir_text)
        if not common_dir_path.is_absolute():
            common_dir_path = (repo_path / common_dir_path).resolve()
        if not common_dir_path.exists():
            failures.append({
                "code": "git_metadata_unavailable",
                "detail": f"git common dir does not exist: {common_dir_path}",
            })
        elif not os.access(common_dir_path, os.W_OK):
            failures.append({
                "code": "git_metadata_not_writable",
                "detail": f"git common dir is not writable: {common_dir_path}",
            })

    remote = _run_git_readiness(repo_path, "remote", "get-url", "origin")
    if remote.returncode != 0 or not remote.stdout.strip():
        failures.append({
            "code": "missing_origin_remote",
            "detail": (remote.stderr or remote.stdout or "origin remote is not configured").strip(),
        })

    return {"ready": not failures, "failures": failures}


def _skip_push_not_ready(
    cfg: dict,
    repo_full: str,
    item: dict,
    info: dict | None,
    project_cfg: dict,
    requirements: list[str],
    readiness: dict,
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
        add=["blocked", PUSH_NOT_READY_LABEL],
        remove=["ready", "in-progress", "agent-dispatched"],
    )

    payload = json.dumps(
        {
            "code": PUSH_NOT_READY_CODE,
            "requirements": requirements,
            "push_readiness": readiness.get("failures", []),
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
            "Blocked automatically: task requires publish capability but push-readiness checks failed.",
        ]),
    )

    # Persist structured block reason for backlog grooming and retry logic
    failure_codes = [f["code"] for f in readiness.get("failures", [])]
    reason = f"Push readiness failed: {', '.join(failure_codes)}" if failure_codes else "Push readiness checks failed"
    task_id = f"dispatch-{repo_full.replace('/', '-')}-{item['number']}"
    unblock_notes = {
        "blocking_cause": reason,
        "next_action": "Resolve push-readiness failures and re-dispatch.",
    }
    result = {
        "status": "blocked",
        "blocker_code": PUSH_NOT_READY_CODE,
    }
    try:
        write_unblock_notes_artifact(task_id, unblock_notes, result)
    except Exception as e:
        print(f"Warning: failed to write push-not-ready unblock artifact: {e}")


def _skip_agent_unavailable(
    repo_full: str,
    item: dict,
    info: dict | None,
    project_cfg: dict,
    error: Exception,
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
        add=["blocked", AGENT_UNAVAILABLE_LABEL],
        remove=["ready", "in-progress", "agent-dispatched"],
    )

    payload = json.dumps(
        {
            "code": AGENT_UNAVAILABLE_CODE,
            "detail": str(error),
        },
        sort_keys=True,
    )
    human_review_required = "No healthy agents available" in str(error)
    add_issue_comment(
        repo_full,
        item["number"],
        "\n".join([
            "<!-- agent-os-dispatch-skip",
            payload,
            "-->",
            (
                "Blocked automatically: no healthy available agent matched this task's requirements. "
                "Escalated for human review."
                if human_review_required
                else "Blocked automatically: no available agent matched this task's requirements."
            ),
        ]),
    )


CI_ARTIFACTS_MISSING_LABEL = "dispatch:ci-artifacts-missing"
CI_ARTIFACTS_MISSING_CODE = "ci_artifacts_missing"

_CHECK_LINK_RE = re.compile(r"\[link\]\(([^)]+)\)")


def _extract_ci_checks_from_body(body: str) -> list[dict]:
    """Build a minimal checks list from the issue body's Failed checks section.

    Returns a list of dicts with ``name``, ``state``, and ``link`` keys so
    that ``validate_ci_artifacts`` can extract the run ID.
    """
    checks: list[dict] = []
    in_checks = False
    for line in (body or "").splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("- failed checks"):
            in_checks = True
            continue
        if in_checks:
            if stripped.startswith("- **"):
                name_end = stripped.find("**", 4)
                name = stripped[4:name_end] if name_end > 4 else "unknown"
                link_match = _CHECK_LINK_RE.search(stripped)
                link = link_match.group(1) if link_match else ""
                checks.append({"name": name, "state": "FAILURE", "link": link})
            elif stripped.startswith("##") or (stripped and not stripped.startswith("-")):
                break
    return checks


def _skip_ci_artifacts_missing(
    repo_full: str,
    item: dict,
    info: dict | None,
    project_cfg: dict,
    validation,
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
        add=["blocked", CI_ARTIFACTS_MISSING_LABEL],
        remove=["ready", "in-progress", "agent-dispatched"],
    )

    payload = json.dumps(
        {
            "code": CI_ARTIFACTS_MISSING_CODE,
            "run_id": validation.run_id,
            "errors": validation.errors,
        },
        sort_keys=True,
    )
    guidance = "; ".join(validation.errors) or "CI artifacts incomplete or inaccessible."
    add_issue_comment(
        repo_full,
        item["number"],
        "\n".join([
            "<!-- agent-os-dispatch-skip",
            payload,
            "-->",
            f"Blocked automatically: CI artifacts missing or incomplete. {guidance}",
        ]),
    )


def _reconcile_closed_items_to_done(queried):
    """Set project status to Done for any CLOSED issue whose board status drifted.

    Closed issues never dispatch (get_ready_items filters state==OPEN), but the
    project board can drift out of sync when an issue is closed via merge,
    manual close, or untrusted-author cleanup without the status field being
    updated. This keeps the board tidy so the user's view matches reality.
    """
    for info in (v[0] for v in queried.values()):
        done_option = info.get("status_options", {}).get("Done")
        field_id = info.get("status_field_id")
        project_id = info.get("project_id")
        if not (done_option and field_id and project_id):
            continue
        for item in info.get("items", []):
            if item.get("state") != "CLOSED":
                continue
            if item.get("status") == "Done":
                continue
            try:
                set_item_status(project_id, item["item_id"], field_id, done_option)
                item["status"] = "Done"
                print(f"Reconciled closed item to Done: {item.get('repo','?')}#{item.get('number','?')}")
            except Exception as e:
                print(f"Warning: failed to reconcile {item.get('repo','?')}#{item.get('number','?')}: {e}")


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

        # Per-repo Telegram switch — operator can pause a single repo without
        # touching config or the global kill-switch.
        from orchestrator.control_state import is_repo_disabled
        if is_repo_disabled(paths["ROOT"], rcfg.get("key", "")):
            print(f"Skipped {repo_full}#{item['number']} — repo disabled via /repo off")
            continue

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

        requirements = _detect_publish_requirements(item)
        if requirements:
            readiness = _check_push_readiness(cfg, rcfg)
            if not readiness["ready"]:
                _skip_push_not_ready(cfg, repo_full, item, info, pcfg, requirements, readiness)
                reason_codes = ", ".join(failure["code"] for failure in readiness["failures"])
                print(
                    f"Skipped {repo_full}#{item['number']} — "
                    f"{PUSH_NOT_READY_CODE}: {reason_codes}"
                )
                continue

        _cluster_duplicate_debug_issues(cfg, repo_full, item, info, pcfg, ready_items)

        # --- CI artifact validation for debugging tasks ---
        parsed_body = parse_issue_body(item.get("body", "") or "")
        if parsed_body.get("task_type") == "debugging":
            ci_checks = _extract_ci_checks_from_body(item.get("body", "") or "")
            if ci_checks:
                validation = validate_ci_artifacts(repo_full, ci_checks)
                print(format_validation_log(validation, task_context=f"dispatch#{item['number']}"))
                if not validation.valid:
                    _skip_ci_artifacts_missing(repo_full, item, info, pcfg, validation)
                    print(
                        f"Skipped {repo_full}#{item['number']} — ci_artifacts_missing: "
                        f"{'; '.join(validation.errors)}"
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
            try:
                task_id, task_md = build_mailbox_task(cfg, pk, rcfg, child_issue)
            except ValueError as exc:
                _skip_agent_unavailable(repo_full, first_child, info, pcfg, exc)
                print(f"Skipped {repo_full}#{first_child['number']} — {AGENT_UNAVAILABLE_CODE}: {exc}")
                continue
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

        try:
            task_id, task_md = build_mailbox_task(cfg, pk, rcfg, issue)
        except ValueError as exc:
            _skip_agent_unavailable(repo_full, item, info, pcfg, exc)
            print(f"Skipped {repo_full}#{item['number']} — {AGENT_UNAVAILABLE_CODE}: {exc}")
            continue
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

    _reconcile_closed_items_to_done(queried)

    issue_lookup = _build_issue_lookup(queried)
    _requeue_unblocked_items(queried, repo_to_project, issue_lookup)
    if _escalate_unassigned_blocked_tasks(cfg, paths):
        return
    if _escalate_over_retried_blocked_tasks(cfg, paths):
        return
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
        from orchestrator.control_state import is_repo_disabled
        for project_key, project_cfg in cfg["github_projects"].items():
            for repo_cfg in project_cfg.get("repos", []):
                repo_full = repo_cfg["github_repo"]
                if is_repo_disabled(paths["ROOT"], repo_cfg.get("key", "")):
                    print(f"Skipped {repo_full} — repo disabled via /repo off")
                    continue
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
                    requirements = _detect_publish_requirements(issue)
                    if requirements:
                        readiness = _check_push_readiness(cfg, repo_cfg)
                        if not readiness["ready"]:
                            _skip_push_not_ready(cfg, repo_full, issue, None, project_cfg, requirements, readiness)
                            reason_codes = ", ".join(failure["code"] for failure in readiness["failures"])
                            print(
                                f"Skipped {repo_full}#{issue['number']} — "
                                f"{PUSH_NOT_READY_CODE}: {reason_codes}"
                            )
                            continue
                    _cluster_duplicate_debug_issues(cfg, repo_full, issue_with_label_set, None, project_cfg, issues)
                    try:
                        task_id, task_md = build_mailbox_task(cfg, project_key, repo_cfg, issue)
                    except ValueError as exc:
                        _skip_agent_unavailable(repo_full, issue, None, project_cfg, exc)
                        print(f"Skipped {repo_full}#{issue['number']} — {AGENT_UNAVAILABLE_CODE}: {exc}")
                        continue
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
