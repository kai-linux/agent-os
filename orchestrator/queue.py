from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import shlex
import subprocess
import tempfile
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import yaml

from orchestrator.audit_log import append_audit_event
from orchestrator.git_branches import detect_default_branch, remote_branch_exists
from orchestrator.paths import load_config, runtime_paths
from orchestrator.github_sync import sync_result
from orchestrator.gh_project import gh_json as _gh_json
from orchestrator.codebase_memory import read_codebase_context, update_codebase_memory
from orchestrator.commit_signature import with_agent_os_trailer
from orchestrator.gh_project import add_issue_comment, gh, gh_json, query_project, set_item_status
from orchestrator.incident_router import classify_severity, escalate as route_incident, open_incidents, update_incident_status
from orchestrator.repo_context import build_execution_context, gather_recent_git_state, gather_objective_alignment, read_sprint_directives
from orchestrator.repo_modes import is_dispatcher_only_repo
from orchestrator.agent_scorer import filter_healthy_agents, log_gate_decision, ADAPTIVE_HEALTH_WINDOW_DAYS, ADAPTIVE_HEALTH_THRESHOLD
from orchestrator.scheduler_state import is_due, job_lock, record_run
from orchestrator.tool_registry import format_tool_bundle_for_prompt, resolve_tools_for

from orchestrator.cost_tracker import rebuild_cost_records, resolve_attempt_model, resolve_attempt_provider, estimate_text_tokens
from orchestrator.budgets import (
    check_budget_alerts,
    filter_budget_compliant_agents,
    record_cost_events,
    warn_if_budgets_missing,
)

from orchestrator.quality_harness import (
    FIXED_SUITE_TAXONOMY,
    clear_pending_qa_action,
    create_pending_qa_action,
    load_pending_qa_action,
    parse_qa_failure_response,
    resolve_repo_local_path,
    write_field_failure_fixture,
)
from orchestrator.work_verifier import record_override

from orchestrator.task_formatter import format_goal_ancestry_block

TELEGRAM_ACTION_TTL_HOURS = 48
BLOCKER_CODE_DESCRIPTIONS = {
    "missing_context": "The task cannot continue because required specs or repository context are missing.",
    "missing_credentials": "The task is blocked on authentication, secrets, or access that the worker does not have.",
    "environment_failure": "The local execution environment, tooling, or infrastructure is broken or unavailable.",
    "dependency_blocked": "Progress depends on another task, issue, or external dependency resolving first.",
    "quota_limited": "The selected model or tool hit a usage, quota, or rate limit.",
    "runner_failure": "The agent runner failed before the task could complete normally.",
    "timeout": "Execution exceeded the allowed time budget.",
    "test_failure": "Implementation landed, but verification failed and follow-up work is still required.",
    "workflow_validation_failed": "A modified GitHub Actions workflow failed local validation before push.",
    "manual_intervention_required": "A human or out-of-band action is required before automation can continue.",
    "fallback_exhausted": "All configured automated fallback attempts were exhausted.",
    "invalid_result_contract": "The worker produced an invalid or missing task outcome contract.",
    "push_not_ready": "Task requires git publish capability but push-readiness checks failed.",
    "no_diff_produced": "The agent claimed completion but produced no file changes for a task type that requires a diff.",
    "prompt_too_large": "The rendered prompt exceeded the per-argv byte ceiling; retrying with more context will not help.",
    "no_web_artifact": "A homepage/website implementation task completed without producing the expected HTML entry point (e.g. index.html at repo root).",
}
VALID_BLOCKER_CODES = set(BLOCKER_CODE_DESCRIPTIONS)
# Blockers that are not agent-quality issues and will not be fixed by rotating
# to the next fallback model. Short-circuit the fallback chain for these so we
# stop burning credits on deterministic environmental or human-gated failures.
PERMANENT_INFRA_BLOCKERS = frozenset({"manual_intervention_required", "prompt_too_large"})
NON_RETRYABLE_FOLLOWUP_BLOCKERS = PERMANENT_INFRA_BLOCKERS | frozenset({"environment_failure", "quota_limited"})
RUNNER_ENVIRONMENT_FAILURE_MARKERS = (
    "bwrap:",
    "bubblewrap",
    "failed rtm_newaddr",
    "operation not permitted",
    "failed to write file",
)
# Linux caps a single argv string at PAGE_SIZE * 32 = 128 KiB (MAX_ARG_STRLEN).
# Keep prompts well under that so we never hit E2BIG during execve.
PROMPT_SIZE_LIMIT_BYTES = 100_000


class PromptTooLargeError(RuntimeError):
    """Raised when the rendered prompt would exceed the argv size ceiling."""

    def __init__(self, size_bytes: int, limit_bytes: int = PROMPT_SIZE_LIMIT_BYTES):
        self.size_bytes = size_bytes
        self.limit_bytes = limit_bytes
        super().__init__(
            f"prompt size {size_bytes} bytes exceeds limit {limit_bytes} "
            f"(MAX_ARG_STRLEN would cause execve E2BIG)"
        )
# Task types where status=complete is meaningless without an accompanying
# diff. Investigative types (research, architecture) can legitimately conclude
# with only a result-contract summary, so they are excluded.
DIFF_REQUIRED_TASK_TYPES = frozenset({
    "implementation",
    "debugging",
    "docs",
    "content",
    "design",
    "browser_automation",
})
PROMPT_INSPECTION_BLOCKER_CODES = {"invalid_result_contract"}
STALL_WATCHDOG_JOB_NAME = "processing_stall_watchdog"
STALL_WATCHDOG_SCOPE = "__global__"
PROCESSING_LOCK_SUFFIX = ".lock.json"

# Web-implementation task detection. "Build homepage / landing page" tasks
# repeatedly shipped markdown copy-specs (BVT_HOMEPAGE.md, etc.) instead of
# the actual runnable HTML entry point. The rubric below nails down that a
# web task's first deliverable is a file renderable in a browser at the web
# root, not a content plan.
_WEB_HOMEPAGE_KEYWORDS = (
    "homepage", "home page", "landing page", "index page", "index.html",
)
_WEB_GENERAL_KEYWORDS = (
    "homepage", "home page", "landing page", "index page", "index.html",
    "website", "web page", "webpage", "static site", "hero section",
)


def _web_task_kind(meta: dict, body: str) -> str | None:
    """Return "homepage" for explicit homepage tasks, "web" for broader web
    implementation tasks, or None otherwise.

    Only applies to implementation / content / design task types — research
    and architecture tasks can legitimately produce a markdown plan.
    """
    task_type = str(meta.get("task_type", "") or "").strip().lower()
    if task_type not in {"implementation", "content", "design"}:
        return None
    haystack = f"{body or ''}".lower()
    if any(kw in haystack for kw in _WEB_HOMEPAGE_KEYWORDS):
        return "homepage"
    if any(kw in haystack for kw in _WEB_GENERAL_KEYWORDS):
        return "web"
    return None


_WEB_TASK_RUBRIC = """
## Web deliverable rubric (task matched a web-implementation pattern)

This is a web task. The acceptance bar is a file that renders in a browser,
not a content plan.

Hard rules:
- The first file you write must be a runnable page. For a homepage / landing
  page task, produce `index.html` at the repository root before any other
  file. For a named page (About, Services, Insights), produce
  `<name>.html` at the repository root.
- Write actual HTML with working markup, not a Markdown copy-spec. If you
  also produce a markdown reference (voice notes, copy drafts), it must be a
  *supplement* to the HTML file, never a substitute.
- The page must be self-contained enough to open in a browser: valid
  `<html>/<head>/<body>`, a `<title>`, and either inline CSS or a linked
  stylesheet that actually exists in the diff.
- If the task mentions sections (hero, value prop, testimonials, features),
  each section must appear in the HTML as a real element (`<section>`,
  `<header>`, etc.), not only described in prose.
- Status = complete is only legitimate if the expected .html artifact is
  present in the diff. Reporting STATUS: complete while shipping only a .md
  file for a web task is a contract violation and will be auto-downgraded.
"""


def _web_task_rubric_for(kind: str) -> str:
    if not kind:
        return ""
    return _WEB_TASK_RUBRIC
_CI_REMEDIATION_TITLE_RE = re.compile(r"^fix ci failure on pr #(\d+)$", re.IGNORECASE)
_CI_REMEDIATION_PR_RE = re.compile(r"\bPR\s*#(\d+)\b", re.IGNORECASE)
_CI_REMEDIATION_PR_URL_RE = re.compile(r"/pull/(\d+)", re.IGNORECASE)
_CI_FAILED_CHECK_RE = re.compile(r"^- \*\*(.+?)\*\*:", re.MULTILINE)

def now_ts():
    return datetime.now().strftime("%Y%m%d-%H%M%S")

def sanitize_slug(text: str, max_len: int = 50) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    return slug[:max_len] or "task"

def log(msg: str, logfile: Path | None = None, also_summary: bool = False, queue_summary_log: Path | None = None):
    print(msg)
    if logfile:
        with logfile.open("a", encoding="utf-8") as f:
            f.write(msg + "\n")
    if also_summary and queue_summary_log:
        with queue_summary_log.open("a", encoding="utf-8") as f:
            f.write(msg + "\n")

class CommandExecutionError(RuntimeError):
    def __init__(self, cmd: list[str], returncode: int, stdout: str, stderr: str):
        self.cmd = [str(c) for c in cmd]
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

        details = [f"Command failed ({returncode}): {' '.join(self.cmd)}"]
        if stdout.strip():
            details.append(f"stdout: {_tail_text(stdout)}")
        if stderr.strip():
            details.append(f"stderr: {_tail_text(stderr)}")
        super().__init__(" | ".join(details))

class WorkflowValidationError(RuntimeError):
    """Raised when a modified GitHub Actions workflow is invalid before push."""

def _tail_text(text: str, max_lines: int = 12, max_chars: int = 1200) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        return ""
    lines = cleaned.splitlines()
    tail = "\n".join(lines[-max_lines:])
    if len(tail) > max_chars:
        tail = tail[-max_chars:]
    return tail

def _changed_workflow_files(worktree: Path) -> list[Path]:
    commands = [
        ["git", "diff", "--name-only", "--", ".github/workflows"],
        ["git", "diff", "--name-only", "--cached", "--", ".github/workflows"],
        ["git", "ls-files", "--others", "--exclude-standard", "--", ".github/workflows"],
    ]
    seen: set[Path] = set()
    changed: list[Path] = []
    for cmd in commands:
        result = subprocess.run(
            cmd,
            cwd=worktree,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            continue
        for raw_line in result.stdout.splitlines():
            rel_path = Path(raw_line.strip())
            if rel_path.suffix.lower() not in {".yml", ".yaml"}:
                continue
            abs_path = worktree / rel_path
            if abs_path in seen:
                continue
            seen.add(abs_path)
            changed.append(abs_path)
    return changed

def _validate_workflow_files(worktree: Path) -> None:
    changed = _changed_workflow_files(worktree)
    for workflow_path in changed:
        if not workflow_path.exists():
            continue
        rel_path = workflow_path.relative_to(worktree)
        try:
            data = yaml.safe_load(workflow_path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            raise WorkflowValidationError(
                f"Workflow validation failed for {rel_path}: invalid YAML ({exc})."
            ) from exc

        violations: list[str] = []

        def inspect_env(scope: str, env_map: object) -> None:
            if not isinstance(env_map, dict):
                return
            for key, value in env_map.items():
                if isinstance(value, str) and re.search(r"\${{\s*runner\.", value, re.IGNORECASE):
                    violations.append(
                        f"{scope} env `{key}` references `runner.*`, which is not available until a runner starts; set it in a step via `$GITHUB_ENV` instead."
                    )

        inspect_env("workflow-level", data.get("env"))
        jobs = data.get("jobs")
        if isinstance(jobs, dict):
            for job_name, job_cfg in jobs.items():
                if isinstance(job_cfg, dict):
                    inspect_env(f"job `{job_name}`", job_cfg.get("env"))

        if violations:
            raise WorkflowValidationError(
                f"Workflow validation failed for {rel_path}: " + " ".join(violations)
            )

def _classify_runner_failure(text: str) -> str | None:
    lowered = (text or "").lower()
    if not lowered:
        return None
    if "argument list too long" in lowered or "e2big" in lowered:
        return "prompt too large"
    if any(token in lowered for token in [
        "rate limit",
        "rate-limit",
        "rate_limit_exceeded",
        "too many requests",
        "429",
        "usage limit",
        "usage_limit",
        "quota",
        "insufficient_quota",
        "exceeded your current quota",
        "hit your limit",
        "reached your limit",
        "model is at capacity",
    ]):
        return "usage limit / rate limit"
    if any(token in lowered for token in ["authentication", "unauthorized", "forbidden", "invalid api key", "not authenticated", "login required"]):
        return "authentication failure"
    if any(token in lowered for token in RUNNER_ENVIRONMENT_FAILURE_MARKERS):
        return "runner environment failure"
    if any(token in lowered for token in ["command not found", "no such file or directory", "unknown option", "unrecognized option"]):
        return "runner/cli configuration failure"
    return None

def _quota_reset_at(text: str, *, now: datetime | None = None) -> datetime | None:
    """Parse provider reset hints like ``resets 8am (Europe/Berlin)``."""
    raw = str(text or "")
    match = re.search(
        r"\bresets?(?:\s+at)?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?(?:\s*\(([^)]+)\))?",
        raw,
        flags=re.IGNORECASE,
    )
    if not match:
        return None

    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = (match.group(3) or "").lower()
    zone_name = (match.group(4) or "UTC").strip()
    if meridiem == "pm" and hour != 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None

    try:
        zone = ZoneInfo(zone_name)
    except Exception:
        zone = timezone.utc

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local_now = current.astimezone(zone)
    reset_local = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if reset_local <= local_now:
        reset_local += timedelta(days=1)
    return reset_local.astimezone(timezone.utc)

def _format_runner_failure(exc: Exception) -> tuple[str, list[str], str | None]:
    if isinstance(exc, CommandExecutionError):
        stdout_tail = _tail_text(exc.stdout)
        stderr_tail = _tail_text(exc.stderr)
        classification = _classify_runner_failure("\n".join([stdout_tail, stderr_tail]))
        summary = (
            f"Runner exited with code {exc.returncode} while executing "
            f"`{' '.join(exc.cmd)}`."
        )
        if classification:
            summary += f" Classified as: {classification}."
        blockers = [summary]
        if stdout_tail:
            blockers.append(f"- stdout tail: {stdout_tail}")
        if stderr_tail:
            blockers.append(f"- stderr tail: {stderr_tail}")
        detail = stderr_tail or stdout_tail or ""
        return summary, blockers, detail
    return str(exc), [f"- Runner/model failure: {exc}"], str(exc)

def _blocker_code_from_runner_failure(summary: str, detail: str | None = None) -> str:
    classification = _classify_runner_failure("\n".join(part for part in [summary, detail or ""] if part))
    if classification == "prompt too large":
        return "prompt_too_large"
    if classification == "usage limit / rate limit":
        return "quota_limited"
    if classification == "authentication failure":
        return "missing_credentials"
    if classification in {"runner/cli configuration failure", "runner environment failure"}:
        return "environment_failure"
    return "runner_failure"

def normalize_blocker_code(value: str | None) -> str:
    raw = str(value or "").strip().lower()
    if raw in {"", "none", "- none", "n/a", "na"}:
        return ""
    return raw

def result_contract_blocker_guidance() -> str:
    return "\n".join(f"- `{code}`: {desc}" for code, desc in BLOCKER_CODE_DESCRIPTIONS.items())

def _invalid_result_contract_result(
    *,
    reason: str,
    raw: str,
    done: list[str],
    files_changed: list[str],
    tests_run: list[str],
    decisions: list[str],
    risks: list[str],
    attempted_approaches: list[str],
    manual_steps: str,
) -> dict:
    blockers = [f"- {reason}"]
    return {
        "status": "blocked",
        "blocker_code": "invalid_result_contract",
        "summary": "The worker produced an invalid .agent_result.md contract.",
        "done": done or ["- Worker produced an unusable handoff file."],
        "blockers": blockers,
        "next_step": "Fix the task outcome contract and rerun the task.",
        "files_changed": files_changed or ["- Unknown / inspect worktree"],
        "tests_run": tests_run or ["- None"],
        "decisions": decisions or ["- Queue rejected an invalid task outcome contract."],
        "risks": risks or ["- Automatic recovery routing may be degraded until the contract is fixed."],
        "attempted_approaches": attempted_approaches or ["- Parsed the worker result and rejected it during validation."],
        "manual_steps": manual_steps,
        "unblock_notes": {
            "blocking_cause": reason,
            "next_action": "Fix the task outcome contract and rerun the task.",
        },
        "raw": raw,
    }

def _upsert_single_value_section(text: str, section: str, value: str, next_sections: list[str]) -> str:
    pattern = rf"(?ms)^({section}:\s*\n?)(.*?)(?=^\s*(?:{'|'.join(re.escape(s) for s in next_sections)})\s*:|\Z)"
    match = re.search(pattern, text)
    replacement = f"{section}:\n{value}\n"
    if match:
        return text[:match.start()] + replacement + text[match.end():]
    anchor = re.search(r"^BLOCKERS:\s*$", text, re.MULTILINE)
    if anchor:
        return text[:anchor.start()] + replacement + "\n" + text[anchor.start():]
    return text.rstrip("\n") + f"\n\n{replacement}"

def _result_contract_text(result: dict) -> str:
    status = str(result.get("status", "blocked")).strip().lower()
    blocker_code = "none" if status == "complete" else (str(result.get("blocker_code", "")).strip() or "none")
    sections = [
        ("STATUS", status),
        ("BLOCKER_CODE", blocker_code),
        ("SUMMARY", str(result.get("summary", "No summary provided.")).strip() or "No summary provided."),
        ("DONE", "\n".join(result.get("done", ["- None"]))),
        ("BLOCKERS", "\n".join(result.get("blockers", ["- None"]))),
        ("NEXT_STEP", str(result.get("next_step", "None")).strip() or "None"),
        ("FILES_CHANGED", "\n".join(result.get("files_changed", ["- None"]))),
        ("TESTS_RUN", "\n".join(result.get("tests_run", ["- None"]))),
        ("DECISIONS", "\n".join(result.get("decisions", ["- None"]))),
        ("RISKS", "\n".join(result.get("risks", ["- None"]))),
        ("ATTEMPTED_APPROACHES", "\n".join(result.get("attempted_approaches", ["- None"]))),
        ("MANUAL_STEPS", str(result.get("manual_steps", "- None")).strip() or "- None"),
    ]
    unblock = result.get("unblock_notes") or {}
    if unblock:
        sections.append(("UNBLOCK_NOTES", f"- blocking_cause: {unblock['blocking_cause']}\n- next_action: {unblock['next_action']}"))
    return "\n\n".join(f"{name}:\n{value}" for name, value in sections) + "\n"

def _write_result_contract(worktree: Path, result: dict) -> None:
    (worktree / ".agent_result.md").write_text(_result_contract_text(result), encoding="utf-8")

def _extract_ci_remediation_pr_number(meta: dict, body: str) -> int | None:
    issue_title = str(meta.get("github_issue_title", "")).strip()
    if issue_title:
        match = _CI_REMEDIATION_TITLE_RE.match(issue_title)
        if match:
            return int(match.group(1))

    for pattern in (_CI_REMEDIATION_PR_URL_RE, _CI_REMEDIATION_PR_RE):
        match = pattern.search(body or "")
        if match:
            return int(match.group(1))
    return None

def _extract_ci_failed_checks(body: str) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for raw_name in _CI_FAILED_CHECK_RE.findall(body or ""):
        name = raw_name.strip()
        key = name.lower()
        if not name or key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names

def _parse_github_timestamp(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None

def _ci_completion_partial_result(
    result: dict,
    *,
    pr_number: int,
    reason: str,
    blocker_code: str,
    detail: str,
    next_step: str,
) -> dict:
    updated = dict(result)
    updated["status"] = "partial"
    updated["blocker_code"] = blocker_code
    updated["ci_rerun_reason"] = reason
    updated["summary"] = f"CI rerun verification for PR #{pr_number} is incomplete ({reason}). {detail}"

    blockers = [b for b in result.get("blockers", ["- None"]) if b != "- None"]
    blockers.append(f"- CI_RERUN_REASON: {reason}")
    blockers.append(f"- {detail}")
    updated["blockers"] = blockers

    decisions = list(result.get("decisions", ["- None"]))
    decisions.append("- Queue requires a post-fix CI rerun with the previously failing job passing before closing PR CI remediation tasks.")
    updated["decisions"] = decisions
    updated["next_step"] = next_step
    updated["unblock_notes"] = {
        "blocking_cause": f"CI rerun verification for PR #{pr_number} incomplete: {reason}",
        "next_action": next_step,
    }
    return updated

def verify_pr_ci_debug_completion(
    meta: dict,
    body: str,
    result: dict,
    *,
    commit_hash: str | None,
    task_started_at: datetime,
) -> dict:
    if result.get("status") != "complete":
        return result
    if str(meta.get("task_type", "")).strip().lower() != "debugging":
        return result

    pr_number = _extract_ci_remediation_pr_number(meta, body)
    if pr_number is None:
        return result

    repo = str(meta.get("github_repo", "")).strip()
    branch = str(meta.get("branch", "")).strip()
    # Prefer structured metadata over markdown parsing to prevent cascading
    # failures when follow-up reformatting strips the check names (PR-98 RCA).
    meta_checks = meta.get("failed_checks")
    if isinstance(meta_checks, list) and meta_checks:
        failed_checks = [str(c) for c in meta_checks if str(c).strip()]
    else:
        failed_checks = _extract_ci_failed_checks(body)
    if not repo or not branch:
        return _ci_completion_partial_result(
            result,
            pr_number=pr_number,
            reason="missing_pr_context",
            blocker_code="missing_context",
            detail="The remediation task did not retain the PR repo/branch metadata needed for CI verification.",
            next_step="Restore the PR remediation context and rerun the task so CI status can be verified.",
        )
    if not failed_checks:
        return _ci_completion_partial_result(
            result,
            pr_number=pr_number,
            reason="missing_failed_job_context",
            blocker_code="missing_context",
            detail="The remediation task did not include the previously failing job name, so the queue could not verify the intended CI repair.",
            next_step="Preserve the failing check name in the remediation issue context and rerun the task.",
        )

    try:
        runs_payload = gh_json(["api", f"repos/{repo}/actions/runs?branch={branch}&per_page=20"]) or {}
    except Exception as exc:
        return _ci_completion_partial_result(
            result,
            pr_number=pr_number,
            reason="verification_unavailable",
            blocker_code="environment_failure",
            detail=f"GitHub Actions verification failed before the rerun could be confirmed: {exc}",
            next_step="Retry once GitHub Actions metadata is reachable and confirm the PR workflow rerun is green.",
        )

    runs = runs_payload.get("workflow_runs") or []
    # Feature-branch pushes typically don't trigger CI directly (workflows
    # usually fire on push-to-master or pull_request events), and follow-up
    # tasks often only push metadata-only commits like .agent_result.md. So
    # an exact-SHA match is frequently unavailable even when the branch is
    # genuinely green. Prefer exact-SHA evidence when present; fall back to
    # any post-task-start run on the branch so we reflect the actual branch
    # state instead of looping on missing_rerun.
    exact_match_runs: list[dict] = []
    fallback_runs: list[dict] = []
    for run in runs:
        if str(run.get("head_branch", "")).strip() != branch:
            continue
        created_at = _parse_github_timestamp(run.get("created_at")) or _parse_github_timestamp(run.get("run_started_at"))
        if created_at and created_at < task_started_at:
            continue
        if commit_hash and str(run.get("head_sha", "")).strip() == commit_hash:
            exact_match_runs.append(run)
        fallback_runs.append(run)

    candidate_runs = exact_match_runs or fallback_runs

    if not candidate_runs:
        return _ci_completion_partial_result(
            result,
            pr_number=pr_number,
            reason="missing_rerun",
            blocker_code="dependency_blocked",
            detail="No GitHub Actions workflow rerun was recorded for the PR branch after this fix attempt.",
            next_step="Wait for or trigger a rerun of the affected CI workflow on the PR branch, then re-check the remediation task.",
        )

    candidate_runs.sort(
        key=lambda run: _parse_github_timestamp(run.get("created_at")) or _parse_github_timestamp(run.get("run_started_at")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    expected = {name.lower() for name in failed_checks}
    latest_run = candidate_runs[0]

    for run in candidate_runs:
        try:
            jobs_payload = gh_json(["api", f"repos/{repo}/actions/runs/{run['id']}/jobs?per_page=100"]) or {}
        except Exception as exc:
            return _ci_completion_partial_result(
                result,
                pr_number=pr_number,
                reason="verification_unavailable",
                blocker_code="environment_failure",
                detail=f"GitHub Actions jobs for rerun {run.get('id')} could not be read: {exc}",
                next_step="Retry the verification once the GitHub Actions job metadata can be fetched.",
            )

        jobs = jobs_payload.get("jobs") or []
        matched_jobs = [job for job in jobs if str(job.get("name", "")).strip().lower() in expected]
        if not matched_jobs:
            continue

        latest_run = run
        if str(run.get("status", "")).lower() != "completed":
            return _ci_completion_partial_result(
                result,
                pr_number=pr_number,
                reason="rerun_pending",
                blocker_code="dependency_blocked",
                detail=f"Workflow run {run.get('id')} is still {run.get('status', 'pending')}, so the fix has not been verified yet.",
                next_step="Wait for the current GitHub Actions rerun to finish and verify the previously failing job passes.",
            )

        failing_jobs = [
            job.get("name", "unknown")
            for job in matched_jobs
            if str(job.get("conclusion", "")).lower() != "success"
        ]
        if failing_jobs:
            return _ci_completion_partial_result(
                result,
                pr_number=pr_number,
                reason="rerun_failed",
                blocker_code="test_failure",
                detail=f"Workflow run {run.get('id')} reran the prior failing job(s), but they still are not green: {', '.join(failing_jobs)}.",
                next_step="Inspect the rerun logs for the still-failing CI job, fix the branch again, and wait for a successful rerun.",
            )

        return result

    return _ci_completion_partial_result(
        result,
        pr_number=pr_number,
        reason="missing_relevant_job",
        blocker_code="dependency_blocked",
        detail=f"Workflow run {latest_run.get('id')} did not include the previously failing job(s): {', '.join(failed_checks)}.",
        next_step="Rerun the workflow that owns the previously failing job and verify those job names complete successfully.",
    )

def _command_available(cmd: str) -> bool:
    if not cmd:
        return False
    first = shlex.split(str(cmd))[0]
    return shutil.which(first) is not None

def _load_json_file(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None

def _openrouter_api_key_status(config_dir: Path) -> tuple[bool, str | None]:
    secrets_path = config_dir / "secrets.json"
    if not secrets_path.is_file():
        return False, f"OpenRouter secrets file missing: {secrets_path}"
    data = _load_json_file(secrets_path)
    if data is None:
        return False, f"OpenRouter secrets file is unreadable or invalid JSON: {secrets_path}"
    api_key = str(data.get("openRouterApiKey", "")).strip()
    if not api_key:
        return False, f"OpenRouter credential missing: {secrets_path} has no openRouterApiKey"
    lowered = api_key.lower()
    if lowered in {"your_openrouter_api_key", "your-api-key", "changeme"} or "your_openrouter_api_key" in lowered:
        return False, f"OpenRouter credential is placeholder text in {secrets_path}"
    return True, None

def agent_available(agent: str) -> tuple[bool, str | None]:
    agent = str(agent).strip().lower()
    if agent == "codex":
        cmd = os.environ.get("CODEX_BIN", "codex")
        return (_command_available(cmd), None if _command_available(cmd) else f"{cmd} not found on PATH")
    if agent == "claude":
        cmd = os.environ.get("CLAUDE_BIN", "claude")
        return (_command_available(cmd), None if _command_available(cmd) else f"{cmd} not found on PATH")
    if agent == "gemini":
        cmd = os.environ.get("GEMINI_BIN", "gemini")
        return (_command_available(cmd), None if _command_available(cmd) else f"{cmd} not found on PATH")
    if agent == "deepseek":
        cline_cmd = os.environ.get("CLINE_BIN", "cline")
        if not _command_available(cline_cmd):
            return False, f"{cline_cmd} not found on PATH"
        provider_reasons: list[str] = []

        openrouter_cfg = Path(
            os.environ.get("DEEPSEEK_OPENROUTER_CONFIG", str(Path.home() / ".config" / "openrouter"))
        )
        if openrouter_cfg.is_dir():
            available, reason = _openrouter_api_key_status(openrouter_cfg)
            if available:
                return True, None
            if reason:
                provider_reasons.append(reason)
        else:
            provider_reasons.append(f"OpenRouter config dir missing: {openrouter_cfg}")

        for provider_name, env_key in (
            ("NanoGPT", "DEEPSEEK_NANOGPT_CONFIG"),
            ("Chutes", "DEEPSEEK_CHUTES_CONFIG"),
        ):
            raw_cfg = os.environ.get(env_key, "").strip()
            if not raw_cfg:
                provider_reasons.append(f"{provider_name} config dir not set")
                continue
            if Path(raw_cfg).is_dir():
                return True, None
            provider_reasons.append(f"{provider_name} config dir missing: {raw_cfg}")

        if provider_reasons:
            return False, "; ".join(provider_reasons)
        return True, None
    return True, None

def run(cmd, *, cwd=None, logfile: Path | None = None, check=True, timeout=None, queue_summary_log: Path | None = None):
    cmd_str = " ".join(map(str, cmd))
    log(f"$ {cmd_str}", logfile, queue_summary_log=queue_summary_log)
    result = subprocess.run(
        [str(c) for c in cmd],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.stdout:
        log(result.stdout.rstrip(), logfile, queue_summary_log=queue_summary_log)
    if result.stderr:
        log(result.stderr.rstrip(), logfile, queue_summary_log=queue_summary_log)
    if check and result.returncode != 0:
        raise CommandExecutionError(cmd, result.returncode, result.stdout, result.stderr)
    return result

def telegram_api(
    cfg: dict,
    method: str,
    payload: dict[str, object] | None = None,
    logfile: Path | None = None,
    queue_summary_log: Path | None = None,
) -> dict | None:
    token = str(cfg.get("telegram_bot_token", "")).strip()
    if not token:
        return None

    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        cmd = ["curl", "-sS", "-X", "POST", url]
        for key, value in (payload or {}).items():
            if isinstance(value, (dict, list)):
                value = json.dumps(value, separators=(",", ":"))
            elif isinstance(value, bool):
                value = "true" if value else "false"
            cmd.extend(["--data-urlencode", f"{key}={value}"])
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if result.returncode != 0:
            log(f"Telegram {method} failed: {result.stderr}", logfile, queue_summary_log=queue_summary_log)
            return None
        data = json.loads(result.stdout) if result.stdout else {}
        if not data.get("ok"):
            log(f"Telegram {method} error: {data}", logfile, queue_summary_log=queue_summary_log)
            return None
        return data
    except Exception as e:
        log(f"Telegram {method} exception: {e}", logfile, queue_summary_log=queue_summary_log)
        return None

def send_telegram(
    cfg: dict,
    text: str,
    logfile: Path | None = None,
    queue_summary_log: Path | None = None,
    reply_markup: dict | None = None,
    *,
    chat_id: str | None = None,
    bypass_kill_switch: bool = False,
) -> int | None:
    del bypass_kill_switch  # Delivery is transport-level; router decides whether to call it.
    resolved_chat_id = str(chat_id or cfg.get("telegram_chat_id", "")).strip()
    if not resolved_chat_id:
        return None
    payload: dict[str, object] = {"chat_id": resolved_chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    data = telegram_api(cfg, "sendMessage", payload, logfile, queue_summary_log)
    if not data:
        return None
    return data.get("result", {}).get("message_id")

def answer_telegram_callback(
    cfg: dict,
    callback_query_id: str,
    text: str,
    logfile: Path | None = None,
    queue_summary_log: Path | None = None,
    *,
    show_alert: bool = False,
):
    telegram_api(
        cfg,
        "answerCallbackQuery",
        {
            "callback_query_id": callback_query_id,
            "text": text,
            "show_alert": show_alert,
        },
        logfile,
        queue_summary_log,
    )

def clear_telegram_reply_markup(
    cfg: dict,
    chat_id: str,
    message_id: int,
    logfile: Path | None = None,
    queue_summary_log: Path | None = None,
):
    telegram_api(
        cfg,
        "editMessageReplyMarkup",
        {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": {"inline_keyboard": []},
        },
        logfile,
        queue_summary_log,
    )

def _telegram_action_path(actions_dir: Path, action_id: str) -> Path:
    return actions_dir / f"{action_id}.json"

def save_telegram_action(actions_dir: Path, action: dict) -> Path:
    path = _telegram_action_path(actions_dir, action["action_id"])
    path.write_text(json.dumps(action, indent=2, sort_keys=True), encoding="utf-8")
    return path

def load_telegram_action(actions_dir: Path, action_id: str) -> dict | None:
    path = _telegram_action_path(actions_dir, action_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))

def telegram_action_expired(action: dict, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    expires_at = datetime.fromisoformat(action["expires_at"])
    return now >= expires_at

def _telegram_lines(title: str, items: list[str], *, fallback: str, limit: int = 6) -> list[str]:
    lines = [title]
    cleaned = [item[2:] if item.startswith("- ") else item for item in items if item and item != "- None"]
    if not cleaned:
        lines.append(f"- {fallback}")
        return lines
    for item in cleaned[:limit]:
        lines.append(f"- {item}")
    if len(cleaned) > limit:
        lines.append(f"- …and {len(cleaned) - limit} more")
    return lines

def build_escalation_message(meta: dict, result: dict, esc_path: Path | None = None) -> str:
    lines = [
        "🛑 Escalated",
        f"Issue: {meta.get('github_issue_url', 'n/a')}",
        f"Task ID: {meta.get('task_id', 'unknown')}",
        f"Repo: {Path(meta.get('repo', 'unknown')).name}",
        f"Blocker code: {result.get('blocker_code', 'none') or 'none'}",
        "",
        "Summary:",
        result.get("summary", "No summary provided."),
        "",
    ]
    goal_ancestry = format_goal_ancestry_block(meta)
    if goal_ancestry:
        lines.extend(["Goal ancestry:", *goal_ancestry.replace("## Goal Ancestry\n", "").splitlines(), ""])
    lines.extend(_telegram_lines("Blockers:", result.get("blockers", ["- None"]), fallback="None"))
    lines.append("")
    lines.extend(_telegram_lines("Files changed:", result.get("files_changed", ["- None"]), fallback="None"))
    if esc_path is not None:
        lines.extend(["", f"Note: {esc_path.name}", f"Actions expire in {TELEGRAM_ACTION_TTL_HOURS}h."])
    text = "\n".join(lines).strip()
    if len(text) <= 4000:
        return text
    return text[:3997] + "..."

def create_escalation_action(meta: dict, result: dict, esc_path: Path, chat_id: str) -> dict:
    now = datetime.now(timezone.utc)
    action_id = uuid4().hex[:12]
    return {
        "action_id": action_id,
        "status": "pending",
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=TELEGRAM_ACTION_TTL_HOURS)).isoformat(),
        "chat_id": str(chat_id),
        "message_id": None,
        "task_id": meta.get("task_id"),
        "github_project_key": meta.get("github_project_key"),
        "github_repo": meta.get("github_repo"),
        "github_issue_number": meta.get("github_issue_number"),
        "github_issue_url": meta.get("github_issue_url"),
        "branch": meta.get("branch"),
        "blocker_code": result.get("blocker_code", ""),
        "summary": result.get("summary", ""),
        "blockers": result.get("blockers", ["- None"]),
        "files_changed": result.get("files_changed", ["- None"]),
        "escalation_note": esc_path.name,
        "objective_id": meta.get("objective_id"),
        "sprint_id": meta.get("sprint_id"),
        "parent_issue": meta.get("parent_issue"),
        "parent_goal_summary": meta.get("parent_goal_summary"),
    }

def escalation_reply_markup(action_id: str) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "Re-queue", "callback_data": f"esc:{action_id}:requeue"},
            {"text": "Close (won't fix)", "callback_data": f"esc:{action_id}:close"},
        ]]
    }

def _append_retry_decision_note(note_path: Path, operation: str) -> str:
    note_text = note_path.read_text(encoding="utf-8") if note_path.exists() else ""
    if "<!-- agent-os-blocked-task-decision -->" in note_text:
        return "A decision was already recorded for this blocked-task escalation."

    if operation == "retry":
        decision_block = "action: retry\nreason: Human selected retry from Telegram blocked-task escalation."
        human_text = "Recorded retry for dispatcher pickup."
    elif operation == "close":
        decision_block = "action: stop\nreason: Human selected close from Telegram blocked-task escalation."
        human_text = "Recorded close for dispatcher pickup."
    else:
        decision_block = "action: skip\nreason: Human acknowledged the escalation and deferred action."
        human_text = "Marked this blocked-task escalation as skipped."

    suffix = (
        "\n\n<!-- agent-os-blocked-task-decision -->\n"
        "## Retry Decision\n"
        f"{decision_block}\n"
    )
    note_path.write_text(note_text.rstrip() + suffix, encoding="utf-8")
    return human_text

def _handle_blocked_task_escalation_callback(
    cfg: dict,
    action: dict,
    operation: str,
    logfile: Path | None = None,
    queue_summary_log: Path | None = None,
) -> str:
    if operation not in {"retry", "close", "skip"}:
        raise RuntimeError(f"Unsupported blocked-task escalation operation: {operation}")

    paths = runtime_paths(cfg)
    note_name = str(action.get("escalation_note", "")).strip()
    if not note_name:
        raise RuntimeError("Blocked-task escalation action is missing an escalation note.")

    note_path = paths["ESCALATED"] / note_name
    if not note_path.exists():
        raise RuntimeError(f"Escalation note not found: {note_path}")

    result_text = _append_retry_decision_note(note_path, operation)
    repo = str(action.get("github_repo", "")).strip()
    issue_number = action.get("github_issue_number")
    if repo and issue_number:
        decision_name = {"retry": "retry", "close": "close", "skip": "skip"}[operation]
        add_issue_comment(
            repo,
            int(issue_number),
            (
                "## Blocked task escalation decision\n"
                f"**Task:** `{action.get('task_id', 'unknown')}`\n"
                f"**Decision:** `{decision_name}`\n"
                "Recorded from Telegram and preserved in the escalation note for dispatcher review."
            ),
        )
    log(
        f"Blocked-task escalation decision recorded: task={action.get('task_id')} operation={operation}",
        logfile,
        queue_summary_log=queue_summary_log,
    )
    return result_text

def planner_reply_markup(action_id: str) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "Approve", "callback_data": f"plan:{action_id}:approve"},
            {"text": "Skip", "callback_data": f"plan:{action_id}:reject"},
        ]]
    }

def _load_telegram_offset(offset_path: Path) -> int:
    if not offset_path.exists():
        return 0
    try:
        return int(offset_path.read_text(encoding="utf-8").strip() or "0")
    except ValueError:
        return 0

def _save_telegram_offset(offset_path: Path, update_id: int):
    offset_path.write_text(str(update_id), encoding="utf-8")

def _get_telegram_updates(cfg: dict, offset: int, logfile: Path | None = None, queue_summary_log: Path | None = None) -> list[dict]:
    token = str(cfg.get("telegram_bot_token", "")).strip()
    if not token:
        return []
    url = f"https://api.telegram.org/bot{token}/getUpdates?offset={offset}&timeout=0"
    try:
        result = subprocess.run(["curl", "-sS", url], capture_output=True, text=True, timeout=20)
        if result.returncode != 0:
            log(f"Telegram getUpdates failed: {result.stderr}", logfile, queue_summary_log=queue_summary_log)
            return []
        data = json.loads(result.stdout) if result.stdout else {}
        if not data.get("ok"):
            log(f"Telegram getUpdates error: {data}", logfile, queue_summary_log=queue_summary_log)
            return []
        return data.get("result", [])
    except Exception as e:
        log(f"Telegram getUpdates exception: {e}", logfile, queue_summary_log=queue_summary_log)
        return []

def _project_cfg(cfg: dict, project_key: str) -> dict:
    project_cfg = cfg.get("github_projects", {}).get(project_key)
    if not isinstance(project_cfg, dict):
        raise RuntimeError(f"Project config missing for key: {project_key}")
    return project_cfg

def ensure_issue_project_status(cfg: dict, project_key: str, issue_url: str, status_value: str):
    project_cfg = _project_cfg(cfg, project_key)
    owner = cfg["github_owner"]
    project_number = project_cfg["project_number"]
    try:
        gh(["project", "item-add", str(project_number), "--owner", owner, "--url", issue_url], check=False)
    except Exception:
        pass
    info = query_project(project_number, owner)
    option_id = info["status_options"].get(status_value)
    if not info["status_field_id"] or not option_id:
        raise RuntimeError(f"Project status option not found: {status_value}")
    for item in info["items"]:
        if item["url"] == issue_url:
            set_item_status(info["project_id"], item["item_id"], info["status_field_id"], option_id)
            return
    raise RuntimeError(f"Issue not found on project board: {issue_url}")

def _is_dispatcher_only_task(cfg: dict, meta: dict) -> bool:
    github_repo = str(meta.get("github_repo", "")).strip()
    return bool(github_repo) and is_dispatcher_only_repo(cfg, github_repo)

def get_issue_title_and_body(repo: str, issue_number: int) -> tuple[str, str]:
    raw = gh([
        "issue", "view", str(issue_number), "-R", repo,
        "--json", "title,body",
    ])
    data = json.loads(raw)
    return data.get("title", ""), data.get("body", "")

def build_requeue_issue_body(original_body: str, action: dict) -> str:
    appendix = [
        "## Re-queued Context",
        f"- Original issue: {action.get('github_issue_url', 'n/a')}",
        f"- Escalated task: {action.get('task_id', 'unknown')}",
        "",
        "### Last agent summary",
        action.get("summary", "No summary provided."),
        "",
    ]
    appendix.extend(_telegram_lines("### Blockers", action.get("blockers", ["- None"]), fallback="None"))
    appendix.append("")
    appendix.extend(_telegram_lines("### Files changed", action.get("files_changed", ["- None"]), fallback="None"))
    base = (original_body or "").rstrip()
    if base:
        return base + "\n\n" + "\n".join(appendix).strip()
    return "\n".join(appendix).strip()

def requeue_escalation(cfg: dict, action: dict, logfile: Path | None = None, queue_summary_log: Path | None = None) -> str:
    repo = action["github_repo"]
    issue_number = int(action["github_issue_number"])
    project_key = action["github_project_key"]
    title, original_body = get_issue_title_and_body(repo, issue_number)
    new_body = build_requeue_issue_body(original_body, action)
    created = gh(["issue", "create", "-R", repo, "--title", title, "--body", new_body])
    new_issue_url = created.strip().splitlines()[-1].strip()
    ensure_issue_project_status(cfg, project_key, new_issue_url, _project_cfg(cfg, project_key).get("ready_value", "Ready"))
    add_issue_comment(repo, issue_number, f"♻️ Re-queued from Telegram escalation: {new_issue_url}")
    log(f"Telegram re-queue created {new_issue_url} for {repo}#{issue_number}", logfile, queue_summary_log=queue_summary_log)
    return new_issue_url

def close_escalation(cfg: dict, action: dict, logfile: Path | None = None, queue_summary_log: Path | None = None):
    repo = action["github_repo"]
    issue_number = int(action["github_issue_number"])
    issue_url = action["github_issue_url"]
    project_key = action["github_project_key"]
    gh([
        "api",
        f"repos/{repo}/issues/{issue_number}",
        "-X", "PATCH",
        "-f", "state=closed",
        "-f", "state_reason=not_planned",
    ])
    add_issue_comment(repo, issue_number, "Closed from Telegram escalation as won't-fix.")
    ensure_issue_project_status(cfg, project_key, issue_url, _project_cfg(cfg, project_key).get("done_value", "Done"))
    log(f"Telegram close completed for {repo}#{issue_number}", logfile, queue_summary_log=queue_summary_log)

def handle_telegram_callback(
    cfg: dict,
    actions_dir: Path,
    callback_data: str,
    logfile: Path | None = None,
    queue_summary_log: Path | None = None,
) -> dict:
    from orchestrator.audit_log import append_audit_event
    from orchestrator import approvals

    m = re.fullmatch(r"(esc|plan|rvt):([a-f0-9]{12}):(requeue|retry|close|skip|approve|reject|cancel)", callback_data or "")
    if not m:
        return {"text": "Unknown action.", "show_alert": True, "remove_keyboard": False}

    action_type, action_id, operation = m.groups()
    action = load_telegram_action(actions_dir, action_id)
    if not action:
        return {"text": "This escalation action is no longer available.", "show_alert": True, "remove_keyboard": True}

    if action.get("status") == "done":
        return {"text": "This escalation was already handled.", "show_alert": True, "remove_keyboard": True}

    if telegram_action_expired(action):
        action["status"] = "expired"
        action["expired_at"] = datetime.now(timezone.utc).isoformat()
        save_telegram_action(actions_dir, action)
        try:
            from orchestrator import approvals

            decision, reason = approvals.AUTO_EXPIRY_DEFAULTS.get(
                str(action.get("type") or ""),
                ("hold", "Auto-expired at approval deadline; defaulted to hold."),
            )
            if action.get("type") == "plan_approval":
                decision, reason = ("skip", "Auto-expired at approval deadline; defaulted to skip.")
            elif action.get("type") == "system_architect_approval":
                decision, reason = ("skip", "Auto-expired at approval deadline; defaulted to skip.")
            approvals.resolve(cfg, action_id, decision, reason)
        except Exception:
            pass
        return {"text": "This escalation action expired after 48 hours.", "show_alert": True, "remove_keyboard": True}

    if action_type == "plan":
        if operation not in {"approve", "reject"}:
            return {"text": "Unknown planner action.", "show_alert": True, "remove_keyboard": True}
        action["status"] = "done"
        action["handled_action"] = operation
        action["handled_at"] = datetime.now(timezone.utc).isoformat()
        action["approval"] = "approved" if operation == "approve" else "rejected"
        approval_decision = "approve" if operation == "approve" else "skip"
        approval_reason = (
            "Approved from Telegram callback."
            if operation == "approve"
            else "Skipped from Telegram callback."
        )
        try:
            approvals.resolve(cfg, action_id, approval_decision, approval_reason)
        except FileNotFoundError:
            pass
        action_kind = str(action.get("type") or "")
        if action_kind == "system_architect_approval":
            action["result_text"] = (
                f"Approved system architect proposal for {action.get('repo', 'repo')}."
                if operation == "approve"
                else f"Skipped system architect proposal for {action.get('repo', 'repo')}."
            )
        else:
            action["result_text"] = (
                f"Approved sprint plan for {action.get('repo', 'repo')}."
                if operation == "approve"
                else f"Skipped sprint plan for {action.get('repo', 'repo')}."
            )
        save_telegram_action(actions_dir, action)
        append_audit_event(
            cfg,
            "telegram_callback",
            {
                "action_type": action_type,
                "action_id": action_id,
                "operation": operation,
                "repo": action.get("repo"),
                "approval": action.get("approval"),
                "result_text": action.get("result_text"),
            },
        )
        return {"text": action["result_text"], "show_alert": False, "remove_keyboard": True}

    if action_type == "rvt":
        if operation not in {"approve", "cancel"}:
            return {"text": "Unknown revert action.", "show_alert": True, "remove_keyboard": True}
        from orchestrator.deploy_watchdog import handle_revert_callback

        result_text = handle_revert_callback(cfg, action, operation, logfile, queue_summary_log)
        action["status"] = "done"
        action["handled_action"] = operation
        action["handled_at"] = datetime.now(timezone.utc).isoformat()
        action["approval"] = "approved" if operation == "approve" else "canceled"
        action["result_text"] = result_text
        save_telegram_action(actions_dir, action)
        append_audit_event(
            cfg,
            "telegram_callback",
            {
                "action_type": action_type,
                "action_id": action_id,
                "operation": operation,
                "repo": action.get("repo"),
                "approval": action.get("approval"),
                "result_text": result_text,
                "revert_pr_number": action.get("revert_pr_number"),
            },
        )
        return {"text": result_text, "show_alert": False, "remove_keyboard": True}

    if action.get("type") == "blocked_task_escalation":
        result_text = _handle_blocked_task_escalation_callback(cfg, action, operation, logfile, queue_summary_log)
    elif operation == "requeue":
        new_issue_url = requeue_escalation(cfg, action, logfile, queue_summary_log)
        result_text = f"Re-queued: {new_issue_url}"
    else:
        close_escalation(cfg, action, logfile, queue_summary_log)
        result_text = f"Closed issue #{action['github_issue_number']} as won't-fix."

    action["status"] = "done"
    action["handled_action"] = operation
    action["handled_at"] = datetime.now(timezone.utc).isoformat()
    action["result_text"] = result_text
    save_telegram_action(actions_dir, action)
    append_audit_event(
        cfg,
        "telegram_callback",
        {
            "action_type": action_type,
            "action_id": action_id,
            "operation": operation,
            "repo": action.get("github_repo"),
            "issue_number": action.get("github_issue_number"),
            "escalation_type": action.get("type"),
            "result_text": result_text,
        },
    )
    return {"text": result_text, "show_alert": False, "remove_keyboard": True}

def _kill_switch_path(paths: dict) -> Path:
    return paths["ROOT"] / "runtime" / "state" / "disabled"

def _kill_switch_state(paths: dict) -> tuple[bool, str]:
    """Returns (is_disabled, since_or_empty)."""
    path = _kill_switch_path(paths)
    if not path.exists():
        return False, ""
    try:
        since = path.read_text(encoding="utf-8").strip()
    except Exception:
        since = ""
    return True, since

def _format_open_incident_counts(cfg: dict) -> str:
    rows = open_incidents(cfg)
    if not rows:
        return "Open incidents: 0"
    counts = {sev: 0 for sev in ("sev1", "sev2", "sev3")}
    for row in rows:
        sev = str(row.get("sev") or "").lower()
        if sev in counts:
            counts[sev] += 1
    parts = [f"{sev.upper()} {count}" for sev, count in counts.items() if count]
    return f"Open incidents: {len(rows)} ({', '.join(parts)})"

def _format_open_incidents(cfg: dict, *, limit: int = 10) -> str:
    rows = open_incidents(cfg)
    if not rows:
        return "No open incidents."
    lines = [_format_open_incident_counts(cfg) + ":"]
    for row in rows[:limit]:
        event = row.get("event") or {}
        subject = event.get("task_id") or event.get("repo") or event.get("github_repo") or event.get("summary") or "incident"
        lines.append(f"• {row.get('id')} — {str(row.get('sev') or '').upper()} {row.get('source')}: {subject}")
    if len(rows) > limit:
        lines.append(f"...and {len(rows) - limit} more.")
    return "\n".join(lines)

def handle_telegram_command(
    cfg: dict,
    paths: dict,
    text: str,
    operator: dict | None = None,
    logfile: Path | None = None,
    queue_summary_log: Path | None = None,
) -> str | None:
    """Handle a Telegram text command. Returns a reply string, or None to ignore."""
    from orchestrator.audit_log import append_audit_event
    from orchestrator import control_state as cs

    raw = (text or "").strip()
    if not raw.startswith("/"):
        return None
    parts = raw.split()
    first = parts[0][1:]
    command = first.split("@", 1)[0].lower()
    args = parts[1:]
    root = paths["ROOT"]
    cfg_path = paths["CONFIG"]

    if command in {"off", "disable", "stop"}:
        disabled, _ = _kill_switch_state(paths)
        if disabled:
            return "🔴 Agent-OS already OFF."
        switch = _kill_switch_path(paths)
        switch.parent.mkdir(parents=True, exist_ok=True)
        switch.write_text(
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z"),
            encoding="utf-8",
        )
        log("Telegram command: kill-switch engaged (off)", logfile, queue_summary_log=queue_summary_log)
        append_audit_event(cfg, "kill_switch_toggle", {"command": command, "state": "off", "scope": "global"})
        return "🔴 Agent-OS OFF — cron entrypoints will exit early until /on."

    if command in {"on", "enable", "start"}:
        switch = _kill_switch_path(paths)
        if switch.exists():
            switch.unlink()
            log("Telegram command: kill-switch cleared (on)", logfile, queue_summary_log=queue_summary_log)
            append_audit_event(cfg, "kill_switch_toggle", {"command": command, "state": "on", "scope": "global"})
            return "🟢 Agent-OS ON — cron will resume on next tick."
        return "🟢 Agent-OS already ON."

    if command == "status":
        disabled, since = _kill_switch_state(paths)
        incident_summary = _format_open_incident_counts(cfg)
        if disabled:
            since_txt = f" since {since}" if since else ""
            return f"🔴 Agent-OS OFF{since_txt}. {incident_summary}."
        return f"🟢 Agent-OS ON. {incident_summary}."

    if command == "incidents":
        return _format_open_incidents(cfg)

    if command in {"ack", "resolve"}:
        if not args:
            return f"Usage: /{command} <incident_id>"
        updated = update_incident_status(args[0], action=command, cfg=cfg)
        if not updated:
            return f"Incident not found: {args[0]}"
        if command == "ack" and updated.get("_already_acknowledged"):
            return f"Incident {args[0]} already acknowledged at {updated.get('ack_at')}."
        if command == "resolve" and updated.get("_already_resolved"):
            return f"Incident {args[0]} already resolved at {updated.get('resolved_at')}."
        return (
            f"Incident {args[0]} marked acknowledged."
            if command == "ack"
            else f"Incident {args[0]} marked resolved."
        )

    if command == "verify-override":
        if len(args) < 2:
            return "Usage: /verify-override <repo> <pr_number> [reason]"
        repo = str(args[0]).strip()
        try:
            pr_number = int(args[1])
        except ValueError:
            return f"pr_number must be an integer, got {args[1]!r}."
        reason = " ".join(args[2:]).strip()
        applied = record_override(
            cfg,
            repo=repo,
            pr_number=pr_number,
            reason=reason,
            operator=operator or {},
        )
        operator_name = (
            str((operator or {}).get("username") or "").strip()
            or str((operator or {}).get("display_name") or "").strip()
            or str((operator or {}).get("chat_id") or "").strip()
            or "unknown-operator"
        )
        return (
            f"Override recorded for {repo} PR #{pr_number} by {operator_name}. "
            f"Overridden verdict: {applied['overridden_verdict']}."
        )

    if command == "repos":
        rows = cs.list_repos(cfg)
        if not rows:
            return "No repos configured."
        lines = ["Repos:"]
        for r in rows:
            state = "🔴 OFF" if cs.is_repo_disabled(root, r["key"]) else "🟢 ON"
            mode = "full" if r["automation_mode"] == "full" else "dispatcher"
            cad = r.get("sprint_cadence_days")
            cad_txt = f" · {cad}d" if cad is not None else ""
            lines.append(f"• {r['key']} — {state} · {mode}{cad_txt} ({r['github_repo']})")
        return "\n".join(lines)

    if command == "repo":
        return _handle_repo_subcommand(cfg, cfg_path, root, args, logfile, queue_summary_log)

    if command == "qa-fail":
        return _handle_qa_fail_command(cfg, paths, chat_id=str(cfg.get("telegram_chat_id", "")).strip(), args=args)

    if command == "jobs":
        lines = ["Cron jobs:"]
        for job in cs.KNOWN_JOBS:
            state = "🔴 OFF" if cs.is_job_disabled(root, job) else "🟢 ON"
            lines.append(f"• {job} — {state}")
        lines.append(f"\n(protected, always on: {', '.join(sorted(cs.PROTECTED_JOBS))})")
        return "\n".join(lines)

    if command == "job":
        return _handle_job_subcommand(root, args, logfile, queue_summary_log)

    if command == "help":
        return (
            "Agent-OS control:\n"
            "/on /off /status — global kill-switch\n"
            "/incidents — list unresolved incidents only\n"
            "/ack <incident_id> /resolve <incident_id> — update an incident in runtime/incidents/incidents.jsonl\n"
            "/verify-override <repo> <pr_number> [reason] — unblock a work-verifier rejection with audit trail\n"
            "/repos — list repos\n"
            "/repo on|off <key> — pause/resume a single repo\n"
            "/repo mode <key> full|dispatcher — set parent project's automation_mode\n"
            "/repo cadence <key> <days> — set sprint cadence in days (groomer auto-halves)\n"
            "/qa-fail <repo> <suite> <fixture_id> [issue_number] — capture a regression fixture\n"
            "/jobs — list cron jobs\n"
            "/job on|off <name> — pause/resume a single cron entrypoint\n"
            "/help — this message"
        )

    return None

def _handle_repo_subcommand(cfg, cfg_path, root, args, logfile, queue_summary_log) -> str:
    from orchestrator.audit_log import append_audit_event
    from orchestrator import control_state as cs

    if not args:
        return "Usage: /repo on|off|mode|cadence <key> [value]"
    sub = args[0].lower()

    if sub in {"on", "off"}:
        if len(args) < 2:
            return f"Usage: /repo {sub} <key>"
        key = args[1]
        if not cs.find_repo(cfg, key):
            return f"Unknown repo key: {key}. Try /repos."
        cs.set_repo_disabled(root, key, sub == "off")
        log(f"Telegram: /repo {sub} {key}", logfile, queue_summary_log=queue_summary_log)
        append_audit_event(
            cfg,
            "repo_toggle",
            {"repo_key": key, "state": sub, "project_key": cs.find_repo(cfg, key).get("project_key")},
        )
        return f"{'🔴' if sub == 'off' else '🟢'} Repo {key} now {sub.upper()}."

    if sub == "mode":
        if len(args) < 3:
            return "Usage: /repo mode <key> full|dispatcher"
        key, mode_arg = args[1], args[2].lower()
        normalized = "dispatcher_only" if mode_arg in {"dispatcher", "dispatcher_only"} else mode_arg
        if normalized not in cs.VALID_AUTOMATION_MODES:
            return f"Invalid mode {mode_arg!r}. Use full or dispatcher."
        repo = cs.find_repo(cfg, key)
        if not repo:
            return f"Unknown repo key: {key}. Try /repos."
        try:
            cs.set_project_automation_mode(cfg_path, repo["project_key"], normalized)
        except Exception as exc:
            return f"Failed to update config.yaml: {exc}"
        log(f"Telegram: /repo mode {key} {normalized}", logfile, queue_summary_log=queue_summary_log)
        append_audit_event(
            cfg,
            "mode_change",
            {
                "repo_key": key,
                "project_key": repo["project_key"],
                "automation_mode": normalized,
                "scope": "project",
            },
        )
        return (
            f"✅ Project {repo['project_key']} (containing {key}) → automation_mode={normalized}.\n"
            "Note: this affects every repo in that project."
        )

    if sub == "cadence":
        if len(args) < 3:
            return "Usage: /repo cadence <key> <days>"
        key, days_arg = args[1], args[2]
        try:
            days = float(days_arg)
        except ValueError:
            return f"Days must be a number, got {days_arg!r}."
        if days < 0:
            return f"Days must be >= 0, got {days}."
        repo = cs.find_repo(cfg, key)
        if not repo:
            return f"Unknown repo key: {key}. Try /repos."
        try:
            cs.set_repo_cadence(cfg_path, key, days, repo["project_key"])
        except Exception as exc:
            return f"Failed to update config.yaml: {exc}"
        log(f"Telegram: /repo cadence {key} {days}", logfile, queue_summary_log=queue_summary_log)
        append_audit_event(
            cfg,
            "repo_cadence_change",
            {"repo_key": key, "project_key": repo["project_key"], "days": days},
        )
        groomer_days = days / 2 if days > 0 else 0
        return (
            f"✅ Repo {key} → sprint_cadence_days={days}. "
            f"Groomer auto-derives to {groomer_days}d (half the sprint)."
        )

    return f"Unknown /repo subcommand: {sub}. Try /help."

def _handle_job_subcommand(root, args, logfile, queue_summary_log) -> str:
    from orchestrator import control_state as cs

    if len(args) < 2 or args[0].lower() not in {"on", "off"}:
        return "Usage: /job on|off <name>"
    sub = args[0].lower()
    name = args[1]
    if name in cs.PROTECTED_JOBS:
        return f"❌ {name} is protected and cannot be disabled (would lock you out)."
    if name not in cs.KNOWN_JOBS:
        return f"Unknown job {name!r}. Try /jobs."
    cs.set_job_disabled(root, name, sub == "off")
    log(f"Telegram: /job {sub} {name}", logfile, queue_summary_log=queue_summary_log)
    return f"{'🔴' if sub == 'off' else '🟢'} Job {name} now {sub.upper()}."


def _queue_incident_event(
    event_type: str,
    meta: dict,
    result: dict,
    *,
    repo_name: str,
    branch: str,
    model_attempts: list[str],
    note_path: Path | None = None,
    reply_markup: dict | None = None,
    extra_line: str | None = None,
) -> dict:
    summary = result.get("summary", "No summary provided.")
    if extra_line:
        summary = f"{summary} ({extra_line})"
    event = {
        "source": "queue",
        "type": event_type,
        "repo": repo_name,
        "github_repo": meta.get("github_repo"),
        "github_issue_number": meta.get("github_issue_number"),
        "github_issue_url": meta.get("github_issue_url"),
        "task_id": meta.get("task_id"),
        "branch": branch,
        "agent": meta.get("resolved_agent") or meta.get("agent"),
        "models_tried": list(model_attempts),
        "blocker_code": result.get("blocker_code"),
        "summary": summary,
        "force_notify": True,
        "dedup_key": (
            f"{event_type}:{meta.get('task_id')}:{result.get('blocker_code') or 'none'}:{extra_line or ''}"
        ),
    }
    if note_path is not None:
        event["escalation_note"] = note_path.name
        event["summary"] = f"{summary} (note: {note_path.name})"
    if reply_markup:
        event["reply_markup"] = reply_markup
    return event

def _handle_qa_fail_command(cfg: dict, paths: dict, chat_id: str, args: list[str]) -> str:
    if len(args) < 3:
        suites = ", ".join(FIXED_SUITE_TAXONOMY)
        return f"Usage: /qa-fail <repo> <suite> <fixture_id> [issue_number]\nSuites: {suites}"
    github_repo = str(args[0]).strip()
    suite = str(args[1]).strip().lower()
    fixture_id = sanitize_slug(str(args[2]).strip(), max_len=64)
    if suite not in FIXED_SUITE_TAXONOMY:
        return f"Invalid suite {suite!r}. Use one of: {', '.join(FIXED_SUITE_TAXONOMY)}"
    repo_path = resolve_repo_local_path(cfg, github_repo)
    if repo_path is None:
        return f"Unknown repo {github_repo!r}."
    issue_number = None
    if len(args) >= 4:
        try:
            issue_number = int(args[3])
        except ValueError:
            return f"issue_number must be an integer, got {args[3]!r}."
    create_pending_qa_action(
        paths["TELEGRAM_ACTIONS"],
        chat_id=chat_id,
        github_repo=github_repo,
        suite=suite,
        fixture_id=fixture_id,
        issue_number=issue_number,
    )
    return (
        f"Capture ready for {github_repo} → tests/fixtures/{suite}/{fixture_id}/\n"
        "Reply with:\n"
        "INPUT:\n"
        "<bad output input>\n"
        "EXPECTED_OUTPUT:\n"
        "<correct expected output>\n"
        "VERIFIED: yes\n"
        "NOTES:\n"
        "<optional context>"
    )

def _handle_pending_qa_reply(cfg: dict, paths: dict, message_chat: str, message_text: str) -> str | None:
    action = load_pending_qa_action(paths["TELEGRAM_ACTIONS"], message_chat)
    if not action:
        return None
    payload = parse_qa_failure_response(message_text)
    if not payload.get("input"):
        return "Pending /qa-fail capture needs an INPUT section."

    github_repo = str(action.get("github_repo", "")).strip()
    repo_path = resolve_repo_local_path(cfg, github_repo)
    if repo_path is None or not repo_path.exists():
        return f"Failed to resolve local repo for {github_repo}."

    manifest_path = write_field_failure_fixture(
        repo_path,
        suite=str(action.get("suite", "")),
        fixture_id=str(action.get("fixture_id", "")),
        payload=payload,
        github_repo=github_repo,
        issue_number=action.get("issue_number"),
    )
    clear_pending_qa_action(paths["TELEGRAM_ACTIONS"], message_chat)
    issue_number = action.get("issue_number")
    if issue_number:
        try:
            add_issue_comment(
                github_repo,
                int(issue_number),
                (
                    "## Quality harness field failure fixture captured\n"
                    f"Stored at `{manifest_path.relative_to(repo_path).as_posix()}`.\n"
                    f"Verified: `{bool(payload.get('verified') and payload.get('expected_output'))}`"
                ),
            )
        except Exception:
            pass
    return f"Stored field-failure fixture at {manifest_path.relative_to(repo_path).as_posix()}."

def process_telegram_callbacks(
    cfg: dict,
    paths: dict,
    logfile: Path | None = None,
    queue_summary_log: Path | None = None,
):
    token = str(cfg.get("telegram_bot_token", "")).strip()
    chat_id = str(cfg.get("telegram_chat_id", "")).strip()
    if not token or not chat_id:
        return

    offset_path = paths["TELEGRAM_OFFSET"]
    actions_dir = paths["TELEGRAM_ACTIONS"]
    last_update_id = _load_telegram_offset(offset_path)
    updates = _get_telegram_updates(cfg, last_update_id + 1, logfile, queue_summary_log)
    if not updates:
        return

    max_update_id = last_update_id
    for update in updates:
        max_update_id = max(max_update_id, int(update.get("update_id", 0)))

        # Handle /commands from the authorized chat (control-tower mode).
        message = update.get("message") or {}
        message_text = str(message.get("text", "")).strip()
        message_chat = str((message.get("chat") or {}).get("id", ""))
        if message_text.startswith("/") and message_chat == chat_id:
            try:
                operator = {
                    "chat_id": message_chat,
                    "username": str((message.get("from") or {}).get("username") or "").strip(),
                    "display_name": " ".join(
                        part
                        for part in [
                            str((message.get("from") or {}).get("first_name") or "").strip(),
                            str((message.get("from") or {}).get("last_name") or "").strip(),
                        ]
                        if part
                    ).strip(),
                }
                reply = handle_telegram_command(
                    cfg,
                    paths,
                    message_text,
                    operator=operator,
                    logfile=logfile,
                    queue_summary_log=queue_summary_log,
                )
                if reply is not None:
                    send_telegram(cfg, reply, logfile, queue_summary_log)
            except Exception as e:
                log(f"Telegram command handling failed: {e}", logfile, queue_summary_log=queue_summary_log)
                send_telegram(cfg, f"Command failed: {e}", logfile, queue_summary_log)
            continue
        if message_text and message_chat == chat_id:
            try:
                reply = _handle_pending_qa_reply(cfg, paths, message_chat, message_text)
                if reply is not None:
                    send_telegram(cfg, reply, logfile, queue_summary_log)
                    continue
            except Exception as e:
                log(f"Telegram qa-fail handling failed: {e}", logfile, queue_summary_log=queue_summary_log)
                send_telegram(cfg, f"/qa-fail capture failed: {e}", logfile, queue_summary_log)
                continue

        callback = update.get("callback_query") or {}
        if not callback:
            continue
        callback_id = callback.get("id")
        data = callback.get("data", "")
        message = callback.get("message") or {}
        message_chat_id = str((message.get("chat") or {}).get("id", ""))
        message_id = message.get("message_id")
        if message_chat_id != chat_id or not callback_id:
            continue
        try:
            outcome = handle_telegram_callback(cfg, actions_dir, data, logfile, queue_summary_log)
            answer_telegram_callback(
                cfg,
                callback_id,
                outcome["text"],
                logfile,
                queue_summary_log,
                show_alert=bool(outcome.get("show_alert")),
            )
            if outcome.get("remove_keyboard") and message_id:
                clear_telegram_reply_markup(cfg, message_chat_id, int(message_id), logfile, queue_summary_log)
        except Exception as e:
            log(f"Telegram callback handling failed: {e}", logfile, queue_summary_log=queue_summary_log)
            answer_telegram_callback(
                cfg,
                callback_id,
                f"Action failed: {e}",
                logfile,
                queue_summary_log,
                show_alert=True,
            )
    _save_telegram_offset(offset_path, max_update_id)

def priority_score(task: Path, cfg: dict) -> float:
    """Compute priority score = priority_weight + age_bonus (1 pt/hr)."""
    weights = cfg.get("priority_weights", {"prio:high": 30, "prio:normal": 10, "prio:low": 0})
    default_weight = weights.get("prio:normal", 10)
    try:
        text = task.read_text(encoding="utf-8")
        m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, flags=re.DOTALL)
        meta = yaml.safe_load(m.group(1)) if m else {}
        label = str(meta.get("priority", "prio:normal")).lower()
    except Exception:
        label = "prio:normal"
    weight = weights.get(label, default_weight)
    age_hours = (datetime.now().timestamp() - task.stat().st_mtime) / 3600
    return weight + age_hours

def pick_task(inbox: Path, cfg: dict | None = None):
    tasks = list(inbox.glob("*.md"))
    if not tasks:
        return None
    if cfg is None:
        return sorted(tasks)[0]
    return max(tasks, key=lambda t: priority_score(t, cfg))

def parse_task(path: Path):
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, flags=re.DOTALL)
    if not m:
        raise ValueError(f"Invalid task format in {path}. Expected YAML frontmatter.")
    meta = yaml.safe_load(m.group(1)) or {}
    body = m.group(2).strip()
    if "task_id" not in meta:
        raise ValueError("Missing task_id")
    if "repo" not in meta:
        raise ValueError("Missing repo")
    return meta, body

def render_task(meta: dict, body: str) -> str:
    frontmatter_text = yaml.safe_dump(meta, sort_keys=False).strip()
    return f"---\n{frontmatter_text}\n---\n\n{body.rstrip()}\n"


def _processing_lock_path(task_path: Path) -> Path:
    return task_path.with_name(f"{task_path.name}{PROCESSING_LOCK_SUFFIX}")


def _write_processing_lock(task_path: Path, meta: dict, worker_id: str) -> Path:
    lock_path = _processing_lock_path(task_path)
    payload = {
        "task_id": meta.get("task_id"),
        "worker_id": worker_id,
        "pid": os.getpid(),
        "agent": meta.get("resolved_agent") or meta.get("agent"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    lock_path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    return lock_path


def _clear_processing_lock(task_path: Path) -> None:
    try:
        _processing_lock_path(task_path).unlink()
    except FileNotFoundError:
        return


def _read_processing_lock(task_path: Path) -> dict:
    lock_path = _processing_lock_path(task_path)
    if not lock_path.exists():
        return {}
    try:
        data = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _pid_is_live(pid: object) -> bool:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    try:
        os.kill(value, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _processing_age_minutes(task_path: Path, now: datetime | None = None) -> float:
    current = now or datetime.now(timezone.utc)
    return max(0.0, (current.timestamp() - task_path.stat().st_mtime) / 60.0)


def recover_stalled_processing_tasks(
    cfg: dict,
    paths: dict,
    *,
    now: datetime | None = None,
    logfile: Path | None = None,
    queue_summary_log: Path | None = None,
) -> list[dict]:
    current = now or datetime.now(timezone.utc)
    max_processing_minutes = float(cfg.get("max_processing_minutes", 30) or 30)
    recovered: list[dict] = []

    for task_path in sorted(paths["PROCESSING"].glob("*.md")):
        try:
            age_minutes = _processing_age_minutes(task_path, current)
        except FileNotFoundError:
            continue
        if age_minutes < max_processing_minutes:
            continue

        lock_data = _read_processing_lock(task_path)
        if _pid_is_live(lock_data.get("pid")):
            continue

        try:
            meta, body = parse_task(task_path)
        except Exception as exc:
            log(
                f"Stall watchdog skipped unreadable task {task_path.name}: {exc}",
                logfile,
                queue_summary_log=queue_summary_log,
            )
            continue

        current_attempt = int(meta.get("attempt", 1) or 1)
        max_attempts = int(meta.get("max_attempts", cfg.get("default_max_attempts", 4)) or cfg.get("default_max_attempts", 4))
        last_agent = str(meta.get("resolved_agent") or meta.get("agent") or "unknown").strip() or "unknown"
        stale_pid = lock_data.get("pid")
        payload = {
            "task_id": meta.get("task_id"),
            "task_file": task_path.name,
            "agent": last_agent,
            "duration_minutes": round(age_minutes, 1),
            "attempt": current_attempt,
            "max_attempts": max_attempts,
            "stale_pid": stale_pid,
        }

        meta["stalled_recovered_at"] = current.isoformat()
        meta["stalled_recovered_by"] = STALL_WATCHDOG_JOB_NAME
        meta["stalled_duration_minutes"] = round(age_minutes, 1)
        if stale_pid is not None:
            meta["stalled_worker_pid"] = stale_pid

        if current_attempt >= max_attempts:
            meta["blocker_code"] = "worker_crash"
            task_path.write_text(render_task(meta, body), encoding="utf-8")
            destination = paths["BLOCKED"] / task_path.name
            shutil.move(str(task_path), str(destination))
            _clear_processing_lock(task_path)
            send_telegram(
                cfg,
                f"🚧 Worker crash escalated\nTask: {meta.get('task_id')}\nAgent: {last_agent}\nStuck: {round(age_minutes, 1)} min\nAction: moved to blocked (worker_crash)",
                logfile,
                queue_summary_log,
            )
            append_audit_event(
                cfg,
                "stalled_task_blocked",
                {**payload, "action": "blocked", "blocker_code": "worker_crash"},
            )
            recovered.append({"task_id": meta.get("task_id"), "action": "blocked", "path": destination})
            continue

        meta["attempt"] = current_attempt + 1
        meta["model_attempts"] = []
        task_path.write_text(render_task(meta, body), encoding="utf-8")
        destination = paths["INBOX"] / task_path.name
        shutil.move(str(task_path), str(destination))
        _clear_processing_lock(task_path)
        send_telegram(
            cfg,
            f"♻️ Recovered stalled task\nTask: {meta.get('task_id')}\nAgent: {last_agent}\nStuck: {round(age_minutes, 1)} min\nAction: returned to inbox (attempt {meta['attempt']}/{max_attempts})",
            logfile,
            queue_summary_log,
        )
        append_audit_event(
            cfg,
            "stalled_task_requeued",
            {**payload, "action": "requeued", "new_attempt": meta["attempt"]},
        )
        recovered.append({"task_id": meta.get("task_id"), "action": "requeued", "path": destination})

    return recovered


def maybe_run_stall_watchdog(
    cfg: dict,
    paths: dict,
    *,
    worker_id: str = "watchdog",
    now: datetime | None = None,
    logfile: Path | None = None,
    queue_summary_log: Path | None = None,
) -> list[dict]:
    current = now or datetime.now(timezone.utc)
    interval_minutes = float(cfg.get("stall_watchdog_interval_minutes", 5) or 5)
    if interval_minutes <= 0:
        return []

    with job_lock(cfg, STALL_WATCHDOG_JOB_NAME) as acquired:
        if not acquired:
            return []
        due, _reason = is_due(
            cfg,
            STALL_WATCHDOG_JOB_NAME,
            STALL_WATCHDOG_SCOPE,
            cadence_hours=interval_minutes / 60.0,
            now=current,
        )
        if not due:
            return []
        recovered = recover_stalled_processing_tasks(
            cfg,
            paths,
            now=current,
            logfile=logfile,
            queue_summary_log=queue_summary_log,
        )
        record_run(cfg, STALL_WATCHDOG_JOB_NAME, STALL_WATCHDOG_SCOPE, now=current)
        if recovered:
            log(
                f"[{worker_id}] Stall watchdog recovered {len(recovered)} task(s).",
                logfile,
                also_summary=True,
                queue_summary_log=queue_summary_log,
            )
        return recovered

def split_section(text: str, start_label: str, end_labels: list[str]):
    if end_labels:
        pattern = rf"^{re.escape(start_label)}:\s*(.*?)(?=^(?:{'|'.join(map(re.escape, end_labels))}):|\Z)"
    else:
        pattern = rf"^{re.escape(start_label)}:\s*(.*)$"
    m = re.search(pattern, text, flags=re.MULTILINE | re.DOTALL)
    return m.group(1).strip() if m else ""

def _extract_markdown_section(text: str, heading: str, end_headings: list[str]) -> str:
    if end_headings:
        pattern = rf"(?ms)^## {re.escape(heading)}\s*\n(.*?)(?=^## (?:{'|'.join(map(re.escape, end_headings))})\s*$|\Z)"
    else:
        pattern = rf"(?ms)^## {re.escape(heading)}\s*\n(.*)$"
    match = re.search(pattern, text)
    return match.group(1).strip() if match else ""

def _prompt_inspection_recovery_request(meta: dict, body: str) -> dict | None:
    trigger = str(
        meta.get("recovery_trigger")
        or _extract_markdown_section(body, "Recovery Trigger", ["Original Task ID", "Prior Blocker Code"])
    ).strip().lower().replace(" ", "_").replace("-", "_")
    target_task_id = str(
        meta.get("recovery_target_task_id")
        or _extract_markdown_section(body, "Original Task ID", ["Prior Blocker Code", "Recovery Trigger"])
    ).strip()
    prior_blocker_code = normalize_blocker_code(
        meta.get("recovery_target_blocker_code")
        or _extract_markdown_section(body, "Prior Blocker Code", ["Recovery Trigger", "Prompt Snapshot", "Context"])
    )

    inferred_prompt_context = bool(re.search(r"(?i)\b(worker prompt|prompt inspection|prompt snapshot)\b", body or ""))
    if trigger and trigger not in {"prompt_inspection", "worker_prompt_inspection"}:
        return None
    if not trigger and not inferred_prompt_context:
        return None
    if not target_task_id or prior_blocker_code not in PROMPT_INSPECTION_BLOCKER_CODES:
        return None
    return {
        "target_task_id": target_task_id,
        "prior_blocker_code": prior_blocker_code,
    }

def _find_blocked_task_for_recovery(blocked_dir: Path, task_id: str) -> tuple[Path, dict, str] | tuple[None, None, None]:
    candidates: list[tuple[float, Path, dict, str]] = []
    for task_path in blocked_dir.glob("*.md"):
        try:
            meta, body = parse_task(task_path)
        except Exception:
            continue
        if meta.get("task_id") != task_id:
            continue
        candidates.append((task_path.stat().st_mtime, task_path, meta, body))

    if not candidates:
        return None, None, None

    candidates.sort(key=lambda item: item[0], reverse=True)
    _mtime, task_path, meta, body = candidates[0]
    return task_path, meta, body

def maybe_requeue_prompt_inspection_recovery(
    paths: dict,
    completed_meta: dict,
    completed_body: str,
    result: dict,
    logfile: Path,
    queue_summary_log: Path,
) -> Path | None:
    if result.get("status") != "complete":
        return None

    recovery = _prompt_inspection_recovery_request(completed_meta, completed_body)
    if recovery is None:
        return None

    blocked_path, blocked_meta, blocked_body = _find_blocked_task_for_recovery(
        paths["BLOCKED"],
        recovery["target_task_id"],
    )
    if blocked_path is None or blocked_meta is None:
        log(
            f"Prompt inspection recovery skipped: blocked task {recovery['target_task_id']} not found.",
            logfile,
            queue_summary_log=queue_summary_log,
        )
        return None

    if blocked_meta.get("prompt_inspection_requeued_by"):
        log(
            f"Prompt inspection recovery skipped: {recovery['target_task_id']} was already requeued by {blocked_meta['prompt_inspection_requeued_by']}.",
            logfile,
            queue_summary_log=queue_summary_log,
        )
        return None

    new_task_id = f"task-{now_ts()}-rerun-{sanitize_slug(recovery['target_task_id'], max_len=24)}"
    rerun_meta = dict(blocked_meta)
    rerun_meta["task_id"] = new_task_id
    rerun_meta["attempt"] = int(blocked_meta.get("attempt", 1)) + 1
    rerun_meta["model_attempts"] = []
    rerun_meta["recovery_source_task_id"] = blocked_meta.get("task_id")
    rerun_meta["recovery_trigger"] = "prompt_inspection"
    rerun_meta["recovery_trigger_task_id"] = completed_meta.get("task_id")
    rerun_meta["recovery_trigger_blocker_code"] = recovery["prior_blocker_code"]
    prompt_snapshot_dir = Path(
        rerun_meta.get("prompt_snapshot_path")
        or (paths["ROOT"] / "runtime" / "prompts" / f"{new_task_id}.txt")
    ).parent
    rerun_meta["prompt_snapshot_path"] = str(prompt_snapshot_dir / f"{new_task_id}.txt")

    blocked_meta["prompt_inspection_requeued_by"] = completed_meta.get("task_id")
    blocked_meta["prompt_inspection_requeued_at"] = datetime.now(timezone.utc).isoformat()
    blocked_meta["prompt_inspection_recovery_task_id"] = new_task_id
    blocked_path.write_text(render_task(blocked_meta, blocked_body), encoding="utf-8")

    rerun_path = paths["INBOX"] / f"{new_task_id}.md"
    rerun_path.write_text(render_task(rerun_meta, blocked_body), encoding="utf-8")
    log(
        f"Prompt inspection recovery requeued {recovery['target_task_id']} as {new_task_id}.",
        logfile,
        also_summary=True,
        queue_summary_log=queue_summary_log,
    )
    return rerun_path

def parse_bullets(section_text: str):
    lines = [x.strip() for x in section_text.splitlines() if x.strip()]
    return lines or ["- None"]

def _parse_unblock_notes(raw: str) -> dict:
    """Parse UNBLOCK_NOTES section into {blocking_cause, next_action}.

    Accepts bullet-style (``- blocking_cause: ...``) or label-style
    (``blocking_cause: ...``) lines.  Returns an empty dict when either
    required field is missing or trivially empty.
    """
    cause = ""
    action = ""
    for line in raw.splitlines():
        stripped = line.strip().lstrip("- ").strip()
        if stripped.lower().startswith("blocking_cause:"):
            cause = stripped.split(":", 1)[1].strip()
        elif stripped.lower().startswith("next_action:"):
            action = stripped.split(":", 1)[1].strip()
    if not cause or cause.lower() in ("none", "n/a"):
        return {}
    if not action or action.lower() in ("none", "n/a"):
        return {}
    return {"blocking_cause": cause, "next_action": action}

def _format_unblock_notes_for_followup(unblock_notes: dict | None) -> str:
    if not unblock_notes:
        return "- None"
    return f"- blocking_cause: {unblock_notes['blocking_cause']}\n- next_action: {unblock_notes['next_action']}"

def write_unblock_notes_artifact(task_id: str, unblock_notes: dict, result: dict) -> Path | None:
    """Write a machine-readable YAML unblock-notes artifact for downstream automation."""
    if not unblock_notes:
        return None
    artifact_dir = Path(os.environ.get("ORCH_ROOT", Path(__file__).resolve().parents[1])) / "runtime" / "unblock_notes"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact = {
        "task_id": task_id,
        "status": result.get("status", "blocked"),
        "blocker_code": result.get("blocker_code", ""),
        "blocking_cause": unblock_notes["blocking_cause"],
        "next_action": unblock_notes["next_action"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    path = artifact_dir / f"{task_id}.yaml"
    path.write_text(yaml.safe_dump(artifact, sort_keys=False), encoding="utf-8")
    return path

def _git_fetch_with_retry(repo: Path, logfile: Path, queue_summary_log: Path, max_retries: int = 3):
    """Fetch origin with retries to handle git lock contention from concurrent cron pulls."""
    import time
    for attempt in range(1, max_retries + 1):
        try:
            run(["git", "-C", repo, "fetch", "origin"], logfile=logfile, queue_summary_log=queue_summary_log)
            return
        except RuntimeError:
            if attempt == max_retries:
                raise
            wait = attempt * 5  # 5s, 10s, 15s
            log(f"git fetch failed (attempt {attempt}/{max_retries}), retrying in {wait}s...", logfile, queue_summary_log=queue_summary_log)
            time.sleep(wait)
            # Remove stale lock file if it exists (only after waiting)
            lock_file = repo / ".git" / "index.lock"
            if lock_file.exists():
                try:
                    lock_file.unlink()
                    log("Removed stale .git/index.lock", logfile, queue_summary_log=queue_summary_log)
                except OSError:
                    pass

def _ensure_local_excludes(repo: Path) -> None:
    """Add .agent_result.md to the repo's local exclude file.

    .agent_result.md is the agent → orchestrator handoff contract; it lives in
    every worktree but must never enter a commit. Adding it to
    .git/info/exclude (per-repo, not committed) means `git add -A` skips it
    even on managed repos that have not added it to their tracked .gitignore.
    Cheap, idempotent, runs once per worktree creation.
    """
    exclude_path = repo / ".git" / "info" / "exclude"
    try:
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
        if any(line.strip() == ".agent_result.md" for line in existing.splitlines()):
            return
        prefix = "" if existing.endswith("\n") or existing == "" else "\n"
        with exclude_path.open("a", encoding="utf-8") as fh:
            fh.write(f"{prefix}# agent-os: handoff contract, never commit\n.agent_result.md\n")
    except OSError:
        pass  # best-effort; commit_and_push has a defensive untrack as backstop

def ensure_worktree(cfg: dict, repo: Path, base_branch: str, branch: str, task_id: str, logfile: Path, queue_summary_log: Path):
    worktree = Path(cfg["worktrees_dir"]) / repo.name / task_id
    worktree.parent.mkdir(parents=True, exist_ok=True)

    if worktree.exists():
        shutil.rmtree(worktree, ignore_errors=True)

    _ensure_local_excludes(repo)
    _git_fetch_with_retry(repo, logfile, queue_summary_log)

    # Defensive recovery for tasks queued before per-repo default-branch
    # detection landed: if the configured base branch has no origin ref,
    # fall back to the repo's actual default (e.g. master).
    if not remote_branch_exists(repo, base_branch):
        detected = detect_default_branch(repo)
        if detected and detected != base_branch:
            log(
                f"origin/{base_branch} missing in {repo.name}; "
                f"falling back to detected default branch '{detected}'",
                logfile,
                queue_summary_log=queue_summary_log,
            )
            base_branch = detected

    run(
        ["git", "-C", repo, "worktree", "add", "-B", branch, worktree, f"origin/{base_branch}"],
        logfile=logfile,
        queue_summary_log=queue_summary_log,
    )
    return worktree

_PRIOR_HISTORY_MAX_ATTEMPTS = 2
_PRIOR_HISTORY_SUMMARY_CHARS = 400
_PRIOR_HISTORY_LIST_ITEMS = 4
_PRIOR_HISTORY_LIST_ITEM_CHARS = 160


def _truncate_list(items: list[str], max_items: int, max_chars: int) -> list[str]:
    kept = []
    for line in items[:max_items]:
        s = str(line)
        if len(s) > max_chars:
            s = s[:max_chars].rstrip() + "…"
        kept.append(s)
    dropped = max(0, len(items) - max_items)
    if dropped:
        kept.append(f"- …({dropped} older item(s) truncated)")
    return kept or ["- None"]


def render_prior_attempt_history(prior_results: list[dict]) -> str:
    """Render only the last few attempts with per-field truncation.

    Unbounded prior-attempt history made rendered prompts balloon past the
    MAX_ARG_STRLEN (128 KiB) on retry-3/retry-4, causing execve E2BIG.
    """
    if not prior_results:
        return "None"

    recent = prior_results[-_PRIOR_HISTORY_MAX_ATTEMPTS:]
    skipped = len(prior_results) - len(recent)
    chunks = []
    if skipped:
        chunks.append(f"(…{skipped} earlier attempt(s) truncated for prompt-size budget…)")
    for offset, r in enumerate(recent):
        idx = skipped + offset + 1
        summary = str(r.get("summary", "No summary"))
        if len(summary) > _PRIOR_HISTORY_SUMMARY_CHARS:
            summary = summary[:_PRIOR_HISTORY_SUMMARY_CHARS].rstrip() + "…"
        blockers = _truncate_list(r.get("blockers", []), _PRIOR_HISTORY_LIST_ITEMS, _PRIOR_HISTORY_LIST_ITEM_CHARS)
        approaches = _truncate_list(r.get("attempted_approaches", []), _PRIOR_HISTORY_LIST_ITEMS, _PRIOR_HISTORY_LIST_ITEM_CHARS)
        chunks.append(
            f"""Attempt {idx}
MODEL: {r.get("agent", "unknown")}
STATUS: {r.get("status", "unknown")}
BLOCKER_CODE: {r.get("blocker_code", "none") or "none"}
SUMMARY: {summary}
BLOCKERS:
{chr(10).join(blockers)}
ATTEMPTED_APPROACHES:
{chr(10).join(approaches)}
"""
        )
    return "\n".join(chunks)

def write_prompt(task_id: str, meta: dict, body: str, current_agent: str, prior_results: list[dict], root: Path, worktree: Path | None = None):
    prompt_file = root / "runtime" / "tmp" / f"{task_id}.txt"
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path = Path(meta.get("prompt_snapshot_path") or (root / "runtime" / "prompts" / f"{task_id}.txt"))
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)

    codebase_context = read_codebase_context(worktree) if worktree else ""
    repo_path = worktree or root
    layered_context = build_execution_context(
        repo_path,
        meta.get("task_type", "implementation"),
        body,
    ) if worktree else ""

    # --- Enhanced context to reduce missing_context blockers ---
    base_branch = meta.get("base_branch", "main")
    git_state = gather_recent_git_state(repo_path, base_branch) if worktree else ""
    github_slug = meta.get("github_repo", "")
    try:
        cfg_for_obj = load_config()
    except Exception:
        cfg_for_obj = {}
    objective_context = gather_objective_alignment(repo_path, cfg_for_obj, github_slug)
    sprint_directives = read_sprint_directives(repo_path) if worktree else ""
    curated_tools = ""
    if cfg_for_obj and github_slug:
        try:
            bundle = resolve_tools_for(github_slug, meta.get("task_type", "implementation"), cfg_for_obj)
            if not bundle.get("default_toolset_allowed", True):
                curated_tools = format_tool_bundle_for_prompt(bundle)
        except Exception as exc:
            curated_tools = f"Curated tool registry unavailable: {exc}"

    enhanced_sections = []
    if git_state and git_state != "(recent git state unavailable)":
        enhanced_sections.append(f"## Recent Git State (base: {base_branch})\n\n{git_state}")
    if objective_context:
        enhanced_sections.append(f"## Objective Alignment\n\n{objective_context}")
    if sprint_directives and not sprint_directives.startswith("(no sprint directives"):
        enhanced_sections.append(f"## Sprint Directives\n\n{sprint_directives}")
    if curated_tools:
        enhanced_sections.append(f"## Curated Tools\n\n{curated_tools}")
    web_kind = _web_task_kind(meta, body)
    if web_kind:
        enhanced_sections.append(_web_task_rubric_for(web_kind).strip())
    enhanced_context = "\n\n".join(enhanced_sections)
    if enhanced_context:
        enhanced_context = f"\n\n---\n# Dispatch Context (structured)\n\n{enhanced_context}\n\n---\n"
    # --- End enhanced context ---

    prompt = f"""You are a coding worker running in a controlled automation environment.

You must work only inside the current repository.

Current agent:
{current_agent}

Task metadata:
{yaml.safe_dump(meta, sort_keys=False)}

Task instructions:
{body}
{layered_context}
{codebase_context}
{enhanced_context}
Prior model attempts in this task lineage:
{render_prior_attempt_history(prior_results)}

You must create or overwrite a file named .agent_result.md in the repository root before exiting.

Use EXACTLY this format. The lines inside <...> brackets are placeholders —
replace them with your actual answer. Do NOT copy the template verbatim.

STATUS: <one of: complete, partial, blocked>

BLOCKER_CODE:
<one blocker code from the Rules list below, or `none` when STATUS is complete>

SUMMARY:
<one short paragraph describing what you did this run>

DONE:
- <one concrete thing you completed; add more lines or remove if none>

BLOCKERS:
- <one real blocker; write a single line `- None` when nothing is blocking>

NEXT_STEP:
<one short paragraph for the next run, or the literal word None when STATUS is complete>

FILES_CHANGED:
- <repo-relative path you modified; write `- None` when no files changed>

TESTS_RUN:
- <command + pass/fail result, e.g. `pytest -q → 42 passed`; `- None` when no tests ran>

DECISIONS:
- <decision you made and why; `- None` when no notable decisions>

RISKS:
- <risk you're leaving behind for future runs; `- None` when none>

ATTEMPTED_APPROACHES:
- <approach you tried this run, successful or not, so future runs do not repeat a failed path>

MANUAL_STEPS:
- <action the operator must take before automation can continue; `- None` when none>

Rules:
- Prefer the smallest viable diff.
- Do not modify unrelated files.
- Do not touch secrets unless explicitly asked.
- If you complete the task, set STATUS: complete
- If you made progress but more work remains, set STATUS: partial
- If blocked by missing context, missing credentials, broken environment, or ambiguity, set STATUS: blocked
- If STATUS is partial or blocked, BLOCKER_CODE is required and must be one of:
{result_contract_blocker_guidance()}
- Always write .agent_result.md even if no code changes were made
- In ATTEMPTED_APPROACHES, describe what you tried this run so future runs do not repeat the same failed path
- Never copy the <...> placeholders into your answer. Replace each with a real value or with `None`.
- Read the prior model attempts above and avoid repeating clearly failed approaches unless you have a specific new reason
- Automation-first escalation policy: before emitting ANY item under MANUAL_STEPS
  or marking the task blocked on a "manual action", attempt to automate it. The
  operator should only be asked to do things that genuinely cannot be automated.
  Attempt in this order:
    * Cron / systemd timers: run `crontab -l` and pipe an updated crontab via
      `crontab -` to install the entry directly. Do not just print the line for
      the operator to paste. Only list as MANUAL_STEP if the host is not
      writable or cron is not the scheduler in use.
    * GitHub UI actions (labels, assignees, project moves, issue/PR pinning,
      release creation, milestone assignment): try `gh api` / `gh api graphql`
      first. `gh` is authenticated in this environment. Example: pinning an
      issue → `gh api graphql -f query='mutation{{pinIssue(...)}}'`. Only list as
      MANUAL_STEP if no API exists for the action (e.g. GitHub Discussions
      pinning is UI-only and has no GraphQL mutation — that is genuinely manual).
    * External service posts (dev.to, Twitter, Slack, Telegram, etc.): check
      for a credential in the environment (DEV_API_KEY, SLACK_WEBHOOK_URL,
      TELEGRAM_BOT_TOKEN, etc.). If present, use the service's REST API to post
      directly. Only list as MANUAL_STEP if no credential is configured, and in
      that case name the exact env var the operator must set.
    * Config files under the repo (config.yaml, .env.example, systemd units in
      the repo): edit the file directly and include it in the diff. Do not
      emit a MANUAL_STEP telling the operator to make an edit you could have
      made yourself.
  When you DO automate one of these steps, record it in DONE (not MANUAL_STEPS)
  and mention the command you ran in DECISIONS so the operator can audit it.
  Escalating a step to MANUAL_STEPS that was actually automatable is a task
  quality regression — the operator will re-queue the work.
- In MANUAL_STEPS, list only the residual actions the human operator must take
  after the above automation attempts. Typical legitimate entries:
    * GitHub Discussions pinning (UI-only, no API)
    * Browser-only SaaS configuration with no public API
    * Secret rotation or new credential provisioning
    * Physical/out-of-band actions (DNS changes, billing, domain transfers)
  Format cron entries as ready-to-paste crontab lines with a comment (only if
  the automated install above actually failed). Format config.yaml additions
  as indented YAML snippets. Write exactly "- None" if no manual action is
  required. This section is CRITICAL — the operator depends on it to know
  what genuinely cannot be automated.
"""
    prompt_size = len(prompt.encode("utf-8"))
    if prompt_size > PROMPT_SIZE_LIMIT_BYTES:
        raise PromptTooLargeError(prompt_size)
    prompt_file.write_text(prompt, encoding="utf-8")
    snapshot_path.write_text(prompt, encoding="utf-8")
    return prompt_file

def run_agent(agent: str, worktree: Path, prompt_file: Path, logfile: Path, timeout_minutes: int, root: Path, queue_summary_log: Path):
    runner = root / "bin" / "agent_runner.sh"
    timeout_seconds = max(60, int(timeout_minutes) * 60)
    run([runner, agent, worktree, prompt_file], logfile=logfile, timeout=timeout_seconds, queue_summary_log=queue_summary_log)

def _runner_environment_failure_from_log(logfile: Path | None) -> dict | None:
    if logfile is None or not logfile.exists():
        return None
    try:
        tail = "\n".join(logfile.read_text(encoding="utf-8", errors="replace").splitlines()[-160:])
    except OSError:
        return None
    lowered = tail.lower()
    if not any(marker in lowered for marker in RUNNER_ENVIRONMENT_FAILURE_MARKERS):
        return None
    return {
        "status": "blocked",
        "blocker_code": "environment_failure",
        "summary": "Agent runner environment failed before it could produce a result contract.",
        "done": ["- Agent runner started but could not access or write the worktree reliably."],
        "blockers": [f"- Runner environment failure detected in task log: {_tail_text(tail, max_lines=8, max_chars=500)}"],
        "next_step": "Restore the runner sandbox/worktree read-write path, then rerun the original task.",
        "files_changed": ["- None"],
        "tests_run": ["- None"],
        "decisions": ["- Classified missing .agent_result.md as environment_failure because the log contains runner sandbox/write failures."],
        "risks": ["- Retrying the same task without fixing the runner environment will only create retry storms."],
        "attempted_approaches": ["- Inspected task log after missing result contract."],
        "unblock_notes": {
            "blocking_cause": "Runner sandbox or worktree write path failed before .agent_result.md could be created.",
            "next_action": "Fix the runner environment, then rerun the task.",
        },
        "raw": "",
    }

def has_changes(worktree: Path):
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=worktree,
        capture_output=True,
        text=True,
    )
    return bool(result.stdout.strip())

def has_unpushed_commits(worktree: Path, branch: str) -> bool:
    """Return True if HEAD has commits that are not reachable from any origin ref.

    Checks ``HEAD --not --remotes=origin`` so a freshly-created task branch
    that still points at origin/<base> (no agent work) correctly reports zero
    unpushed commits. Earlier logic returned True for any non-empty history on
    a never-pushed branch, which caused fallback-exhausted tasks to push empty
    branches and be wrongly marked complete by the rescue path.
    """
    result = subprocess.run(
        ["git", "rev-list", "--count", "HEAD", "--not", "--remotes=origin"],
        cwd=worktree, capture_output=True, text=True,
    )
    try:
        return int(result.stdout.strip() or "0") > 0
    except ValueError:
        return False

def commit_and_push(worktree: Path, branch: str, task_id: str, allow_push: bool, logfile: Path, queue_summary_log: Path):
    uncommitted = has_changes(worktree)
    unpushed = has_unpushed_commits(worktree, branch)

    if not uncommitted and not unpushed:
        log("No file changes detected. Skipping commit/push.", logfile, queue_summary_log=queue_summary_log)
        return False

    _validate_workflow_files(worktree)

    if uncommitted:
        run(["git", "add", "-A"], cwd=worktree, logfile=logfile, queue_summary_log=queue_summary_log)
        # .agent_result.md is a worktree-local artifact (gitignored). Some
        # historical branches tracked it before the ignore landed; defensively
        # unstage and untrack so agent commits never carry it forward into PRs.
        run(
            ["git", "rm", "--cached", "-f", "--ignore-unmatch", ".agent_result.md"],
            cwd=worktree,
            logfile=logfile,
            queue_summary_log=queue_summary_log,
        )
        commit_msg = with_agent_os_trailer(f"Agent OS: {task_id}")
        run(["git", "commit", "-m", commit_msg], cwd=worktree, logfile=logfile, queue_summary_log=queue_summary_log)
    else:
        log("Agent already committed changes; pushing unpushed commits.", logfile, queue_summary_log=queue_summary_log)

    if allow_push:
        try:
            run(["git", "push", "-u", "origin", branch], cwd=worktree, logfile=logfile, queue_summary_log=queue_summary_log)
        except CommandExecutionError as e:
            detail = "\n".join(part for part in [e.stdout or "", e.stderr or ""] if part)
            if "non-fast-forward" not in detail.lower():
                raise
            fetch_ref = f"+refs/heads/{branch}:refs/remotes/origin/{branch}"
            run(["git", "fetch", "origin", fetch_ref], cwd=worktree, logfile=logfile, queue_summary_log=queue_summary_log)
            contains = subprocess.run(
                ["git", "merge-base", "--is-ancestor", "HEAD", f"origin/{branch}"],
                cwd=worktree,
                capture_output=True,
                text=True,
            )
            if contains.returncode == 0:
                log(
                    f"Remote branch origin/{branch} already contains local HEAD; treating non-fast-forward push as no-op.",
                    logfile,
                    queue_summary_log=queue_summary_log,
                )
            else:
                raise
    else:
        log("allow_push=false, skipping git push.", logfile, queue_summary_log=queue_summary_log)

    return True

def should_attempt_git_rescue(result: dict, worktree: Path, branch: str) -> bool:
    if not result:
        return False
    if result.get("status") not in ("partial", "blocked"):
        return False
    # fallback_exhausted means no agent ran — there is nothing to rescue, and
    # pushing would flip the task to status=complete against an empty branch.
    if result.get("blocker_code") == "fallback_exhausted":
        return False
    return has_changes(worktree) or has_unpushed_commits(worktree, branch)

def rescue_git_progress(
    cfg: dict,
    result: dict,
    worktree: Path,
    branch: str,
    task_id: str,
    allow_push: bool,
    logfile: Path,
    queue_summary_log: Path,
) -> tuple[dict | None, bool]:
    if not should_attempt_git_rescue(result, worktree, branch):
        return None, False
    if cfg.get("test_command"):
        run_tests(cfg, worktree, worktree, logfile, queue_summary_log)
        validated = parse_agent_result(worktree)
        if validated.get("status") != "complete":
            summary = validated.get("summary", result.get("summary", "Recovered worktree changes failed validation."))
            validated["summary"] = f"{summary} Orchestrator rescue validation failed, so changes were not pushed."
            next_step = validated.get("next_step", "Inspect the failing tests and rerun the task.")
            validated["next_step"] = next_step or "Inspect the failing tests and rerun the task."
            decisions = list(validated.get("decisions", ["- None"]))
            decisions.append("- Queue attempted git rescue but withheld push because rescue validation did not finish cleanly.")
            validated["decisions"] = decisions
            return validated, False
    try:
        committed = commit_and_push(worktree, branch, task_id, allow_push, logfile, queue_summary_log)
    except WorkflowValidationError:
        raise
    except Exception as e:
        log(f"Git rescue failed: {e}", logfile, also_summary=True, queue_summary_log=queue_summary_log)
        return None, False

    if not committed:
        return None, False

    rescued = dict(result)
    rescued["rescued_by_orchestrator"] = True
    summary = result.get("summary", "Recovered worktree changes.")
    rescued["summary"] = f"{summary} Orchestrator rescued and pushed the worktree changes."
    if result.get("blocker_code") == "manual_intervention_required":
        rescued["status"] = result.get("status", "blocked")
        rescued["next_step"] = result.get("next_step") or "Complete the documented manual handoff steps."
    else:
        rescued["status"] = "complete"
        rescued["next_step"] = "Monitor the pushed branch and rerun CI/PR checks."
    done = list(result.get("done", ["- None"]))
    done.append("- Orchestrator committed and pushed recovered worktree changes.")
    rescued["done"] = done
    decisions = list(result.get("decisions", ["- None"]))
    decisions.append("- Queue performed git rescue after the agent left valid changes behind.")
    rescued["decisions"] = decisions
    return rescued, True

def parse_agent_result(worktree: Path):
    result_file = worktree / ".agent_result.md"
    if not result_file.exists():
        raw = (
            "STATUS: blocked\n\n"
            "BLOCKER_CODE:\ninvalid_result_contract\n\n"
            "SUMMARY:\nNo .agent_result.md was produced.\n\n"
            "DONE:\n- No .agent_result.md was produced.\n\n"
            "BLOCKERS:\n- Worker did not write the required result file.\n\n"
            "NEXT_STEP:\nInspect the worker prompt and rerun the task.\n\n"
            "FILES_CHANGED:\n- None\n\n"
            "TESTS_RUN:\n- None\n\n"
            "DECISIONS:\n- None\n\n"
            "RISKS:\n- Missing result contract\n\n"
            "ATTEMPTED_APPROACHES:\n- Worker failed to write the required handoff file\n"
        )
        return {
            "status": "blocked",
            "blocker_code": "invalid_result_contract",
            "summary": "No .agent_result.md was produced.",
            "done": ["- No .agent_result.md was produced."],
            "blockers": ["- Worker did not write the required result file."],
            "next_step": "Inspect the worker prompt and rerun the task.",
            "files_changed": ["- None"],
            "tests_run": ["- None"],
            "decisions": ["- None"],
            "risks": ["- Missing result contract"],
            "attempted_approaches": ["- Worker failed to write the required handoff file"],
            "unblock_notes": {
                "blocking_cause": "Worker did not produce .agent_result.md",
                "next_action": "Inspect the worker prompt and rerun the task.",
            },
            "raw": raw,
        }

    text = result_file.read_text(encoding="utf-8")

    status_match = re.search(r"^STATUS:\s*(.+)$", text, flags=re.MULTILINE)
    all_sections = ["BLOCKER_CODE", "DONE", "BLOCKERS", "NEXT_STEP", "FILES_CHANGED", "TESTS_RUN", "DECISIONS", "RISKS", "ATTEMPTED_APPROACHES", "MANUAL_STEPS", "UNBLOCK_NOTES"]
    summary = split_section(text, "SUMMARY", all_sections)
    blocker_code = split_section(text, "BLOCKER_CODE", ["SUMMARY", *all_sections[1:]])
    done = split_section(text, "DONE", all_sections[2:])
    blockers = split_section(text, "BLOCKERS", all_sections[3:])
    next_step = split_section(text, "NEXT_STEP", all_sections[4:])
    files_changed = split_section(text, "FILES_CHANGED", all_sections[5:])
    tests_run = split_section(text, "TESTS_RUN", all_sections[6:])
    decisions = split_section(text, "DECISIONS", all_sections[7:])
    risks = split_section(text, "RISKS", all_sections[8:])
    attempted_approaches = split_section(text, "ATTEMPTED_APPROACHES", all_sections[9:])
    manual_steps = split_section(text, "MANUAL_STEPS", all_sections[10:])
    unblock_notes_raw = split_section(text, "UNBLOCK_NOTES", [])

    status = status_match.group(1).strip().lower() if status_match else "blocked"
    if status not in {"complete", "partial", "blocked"}:
        status = "blocked"

    blocker_code_value = normalize_blocker_code(blocker_code)
    if blocker_code_value and blocker_code_value not in VALID_BLOCKER_CODES:
        return _invalid_result_contract_result(
            reason=(
                "BLOCKER_CODE must be one of: "
                + ", ".join(sorted(VALID_BLOCKER_CODES))
                + f". Received: {blocker_code_value}"
            ),
            raw=text,
            done=parse_bullets(done),
            files_changed=parse_bullets(files_changed),
            tests_run=parse_bullets(tests_run),
            decisions=parse_bullets(decisions),
            risks=parse_bullets(risks),
            attempted_approaches=parse_bullets(attempted_approaches),
            manual_steps=manual_steps.strip() if manual_steps else "",
        )
    if status in {"partial", "blocked"} and not blocker_code_value:
        return _invalid_result_contract_result(
            reason="Blocked and partial outcomes must include a valid BLOCKER_CODE.",
            raw=text,
            done=parse_bullets(done),
            files_changed=parse_bullets(files_changed),
            tests_run=parse_bullets(tests_run),
            decisions=parse_bullets(decisions),
            risks=parse_bullets(risks),
            attempted_approaches=parse_bullets(attempted_approaches),
            manual_steps=manual_steps.strip() if manual_steps else "",
        )

    unblock_notes = _parse_unblock_notes(unblock_notes_raw) if unblock_notes_raw else {}

    if status in {"partial", "blocked"} and not unblock_notes:
        return _invalid_result_contract_result(
            reason="Blocked and partial outcomes must include UNBLOCK_NOTES with blocking_cause and next_action.",
            raw=text,
            done=parse_bullets(done),
            files_changed=parse_bullets(files_changed),
            tests_run=parse_bullets(tests_run),
            decisions=parse_bullets(decisions),
            risks=parse_bullets(risks),
            attempted_approaches=parse_bullets(attempted_approaches),
            manual_steps=manual_steps.strip() if manual_steps else "",
        )

    return {
        "status": status,
        "blocker_code": blocker_code_value,
        "summary": summary or "No summary provided.",
        "done": parse_bullets(done),
        "blockers": parse_bullets(blockers),
        "next_step": next_step.strip() if next_step else "Inspect result manually.",
        "files_changed": parse_bullets(files_changed),
        "tests_run": parse_bullets(tests_run),
        "decisions": parse_bullets(decisions),
        "risks": parse_bullets(risks),
        "attempted_approaches": parse_bullets(attempted_approaches),
        "manual_steps": manual_steps.strip() if manual_steps else "",
        "unblock_notes": unblock_notes,
        "raw": text,
    }

def _repo_agent_fallbacks(meta: dict, cfg: dict) -> dict:
    project_key = str(meta.get("github_project_key", "")).strip()
    if project_key:
        project_cfg = cfg.get("github_projects", {}).get(project_key, {})
        if isinstance(project_cfg, dict):
            fallbacks = project_cfg.get("agent_fallbacks", {})
            if isinstance(fallbacks, dict):
                return fallbacks
    return {}

VALID_ASSIGNABLE_AGENTS = {"auto", "claude", "codex", "gemini", "deepseek"}
VALID_FALLBACK_AGENTS = VALID_ASSIGNABLE_AGENTS - {"auto"}

def get_agent_chain(meta: dict, cfg: dict) -> list[str]:
    task_type = meta.get("task_type", cfg["default_task_type"])
    fallback_map = _repo_agent_fallbacks(meta, cfg) or cfg.get("agent_fallbacks", {})
    task_chain = list(fallback_map.get(task_type, fallback_map.get(cfg["default_task_type"], ["codex", "claude", "gemini", "deepseek"])))

    requested = str(meta.get("agent", cfg["default_agent"])).strip().lower()
    if requested not in VALID_ASSIGNABLE_AGENTS:
        return []

    if requested in {"", "auto"}:
        chain = task_chain
    else:
        chain = [requested] + [a for a in task_chain if a != requested]

    available_chain = []
    for agent in chain:
        if agent not in VALID_FALLBACK_AGENTS:
            continue
        cooldown_left = agent_cooldown_remaining(cfg, agent)
        if cooldown_left > 0:
            mins = (cooldown_left + 59) // 60
            print(f"agent={agent} skipped: quota cooldown active ({mins} min remaining)")
            continue
        available, _reason = agent_available(agent)
        if available:
            available_chain.append(agent)
    metrics_file = Path(cfg.get("root_dir", ".")).expanduser() / "runtime" / "metrics" / "agent_stats.jsonl"
    # Adaptive gate: skip agents with <25% success rate over 7 days
    after_7d, adaptive_skipped = filter_healthy_agents(
        available_chain,
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
            passed=after_7d,
            context=f"queue:get_agent_chain task_type={meta.get('task_type', 'unknown')}",
        )
    # 24h health gate: keep aligned with dispatcher (50% over min 10 tasks).
    # Tighter values left the trimmed [claude, codex] chain empty after one
    # bad day, causing the worker to arm cooldown without trying any agent.
    healthy, skipped_24h = filter_healthy_agents(
        after_7d,
        metrics_file,
        task_type=task_type,
        threshold=0.50,
        min_task_count=10,
    )
    # Safety net: if both gates combined leave the chain empty but credential
    # checks passed agents, fall back to the credential-available list. Better
    # to try an under-performing agent than to spam fallback-exhausted
    # cooldowns every 15 minutes.
    if not healthy:
        if after_7d:
            print(f"24h gate would empty the chain; relaxing and keeping {after_7d}")
            healthy = after_7d
        elif available_chain:
            print(f"both gates would empty the chain; falling back to credential-available {available_chain}")
            healthy = available_chain
    # Budget hard-stop gate runs last so the health-gate safety net above
    # cannot resurrect an agent that has already blown its monthly spend cap.
    after_budget, budget_skipped = filter_budget_compliant_agents(healthy, cfg)
    if budget_skipped:
        for agent, stats in budget_skipped.items():
            print(
                f"agent={agent} skipped: monthly spend ${stats['spend_usd']:.2f} "
                f">= hard-stop ${stats['hard_stop_usd']:.2f} ({stats['month_key']})"
            )
        log_gate_decision(
            metrics_file.parent,
            gate="budget_hard_stop_monthly",
            skipped={
                agent: {"total": 0, "successes": 0, "rate": 0.0, **stats}
                for agent, stats in budget_skipped.items()
            },
            passed=after_budget,
            context=f"queue:get_agent_chain task_type={meta.get('task_type', 'unknown')}",
        )
    return after_budget

def get_next_agent(meta: dict, cfg: dict, model_attempts: list[str]) -> str | None:
    chain = get_agent_chain(meta, cfg)
    for agent in chain:
        if agent not in model_attempts:
            return agent
    return None

def timeout_for_agent(agent: str, meta: dict, cfg: dict) -> int:
    timeout_map = cfg.get("agent_timeout_minutes", {})
    if agent in timeout_map:
        return int(timeout_map[agent])
    return int(meta.get("max_runtime_minutes", cfg["max_runtime_minutes"]))

def should_try_fallback(result: dict) -> bool:
    status = result.get("status", "blocked")
    if status != "blocked":
        return False
    # Permanent environmental failures are not going to be fixed by the next
    # model in the chain — short-circuit so we stop burning credits.
    blocker_code = (result.get("blocker_code") or "").strip()
    if blocker_code in PERMANENT_INFRA_BLOCKERS:
        return False
    return True

# Short, human-readable headlines per blocker_code. Telegram surface beats the
# old "⏸️ Partial/Blocked" label, which required the user to open the task to
# understand what went wrong.
_BLOCKER_HEADLINES = {
    "missing_context": ("📭", "Needs more context"),
    "missing_credentials": ("🔑", "Missing credentials/access"),
    "environment_failure": ("🧰", "Environment/tooling broken"),
    "dependency_blocked": ("🔗", "Waiting on another task"),
    "quota_limited": ("📉", "Model rate/quota limit hit"),
    "runner_failure": ("💥", "Agent runner crashed"),
    "timeout": ("⏱️", "Timed out"),
    "test_failure": ("🧪", "Tests failed"),
    "workflow_validation_failed": ("📝", "Workflow validation failed"),
    "manual_intervention_required": ("🙋", "Needs a human decision"),
    "fallback_exhausted": ("🧊", "All models exhausted"),
    "invalid_result_contract": ("📄", "Invalid result file"),
    "push_not_ready": ("⛔", "Push readiness check failed"),
    "no_diff_produced": ("🫥", "No code changes produced"),
    "prompt_too_large": ("📏", "Prompt exceeded argv size limit"),
}

def _first_concrete_blocker(blockers: list[str]) -> str:
    """Return the most informative single-line blocker (for a TG one-liner)."""
    for raw in blockers or []:
        line = str(raw).strip()
        if not line or line.lower() in ("- none", "none"):
            continue
        # Prefer the stdout/stderr tail when present — that's where rate-limit
        # messages and real errors surface.
        if "stdout tail:" in line.lower() or "stderr tail:" in line.lower():
            return line.lstrip("- ").strip()
        # Otherwise take the first real blocker but remember it in case a tail
        # shows up later in the list.
        first = line.lstrip("- ").strip()
        for more in blockers[blockers.index(raw) + 1:]:
            if "tail:" in str(more).lower():
                return str(more).lstrip("- ").strip()
        return first
    return ""

def build_partial_blocked_message(
    task_id: str,
    repo_name: str,
    branch: str,
    final_agent: str | None,
    model_attempts: list[str],
    result: dict,
    meta: dict,
    *,
    extra_line: str = "",
) -> str:
    """One Telegram message that tells the operator what's wrong at a glance.

    Replaces the old "⏸️ Partial/Blocked" label + bare "Next:" line, which left
    the operator guessing. Leads with an emoji + plain-English headline derived
    from blocker_code, shows the original issue title (not the autogenerated
    task_id slug), and surfaces the first concrete blocker (usually the stdout
    tail — e.g. "You've hit your limit · resets 12am").
    """
    blocker_code = str(result.get("blocker_code", "")).strip() or "unknown"
    emoji, headline = _BLOCKER_HEADLINES.get(blocker_code, ("⏸️", "Paused"))

    issue_title = str(meta.get("github_issue_title") or "").strip()
    issue_number = meta.get("github_issue_number")

    lines = [f"{emoji} {headline}"]
    if issue_title:
        if issue_number:
            lines.append(f"Task: {issue_title} (#{issue_number})")
        else:
            lines.append(f"Task: {issue_title}")
    else:
        lines.append(f"Task: {task_id}")
    lines.append(f"Repo: {repo_name}")

    summary = str(result.get("summary") or "").strip()
    if summary:
        # Trim to keep the TG surface scannable; full detail lives in the note.
        if len(summary) > 200:
            summary = summary[:197] + "..."
        lines.append(f"Why: {summary}")

    detail = _first_concrete_blocker(result.get("blockers", []))
    if detail and detail != summary:
        if len(detail) > 200:
            detail = detail[:197] + "..."
        lines.append(f"Detail: {detail}")

    tried = ", ".join([a for a in model_attempts if a]) or (final_agent or "none")
    lines.append(f"Models tried: {tried}")

    if extra_line:
        lines.append(extra_line)

    return "\n".join(lines)

def create_followup_task(
    original_meta: dict,
    original_body: str,
    result: dict,
    logfile: Path,
    default_max_attempts: int,
    model_attempts: list[str],
    inbox: Path,
    queue_summary_log: Path,
):
    if result["status"] not in ("partial", "blocked"):
        return None

    blocker_code = str(result.get("blocker_code") or "").strip()
    if blocker_code in NON_RETRYABLE_FOLLOWUP_BLOCKERS:
        log(
            f"Not creating follow-up: blocker_code={blocker_code!r} is a non-retryable infrastructure failure.",
            logfile,
            also_summary=True,
            queue_summary_log=queue_summary_log,
        )
        return None

    next_step = result.get("next_step", "").strip()
    if not next_step or next_step.lower() == "none":
        return None

    current_attempt = int(original_meta.get("attempt", 1))
    max_attempts = int(original_meta.get("max_attempts", default_max_attempts))

    if current_attempt >= max_attempts:
        log(
            f"Max attempts reached ({current_attempt}/{max_attempts}). Not creating follow-up task.",
            logfile,
            also_summary=True,
            queue_summary_log=queue_summary_log,
        )
        return None

    repo = original_meta["repo"]
    base_branch = original_meta.get("base_branch", "main")
    allow_push = original_meta.get("allow_push", True)
    parent_task_id = original_meta.get("parent_task_id", original_meta["task_id"])
    original_branch = original_meta.get("branch", f'agent/{original_meta["task_id"]}')
    task_type = original_meta.get("task_type", "implementation")
    next_attempt = current_attempt + 1
    max_runtime_minutes = original_meta.get("max_runtime_minutes", 40)

    # Derive follow-up slug from the parent task so retries stay identifiable
    # (e.g. task-…-build-services-page-retry-2). Previously we used next_step,
    # which produced garbage IDs like "task-…-try-the-next-fallback-model-…"
    # when the model only suggested a recovery instruction.
    parent_slug_match = re.match(r"^task-\d{8}-\d{6}-(.+?)(-retry-\d+)?$", parent_task_id)
    if parent_slug_match:
        parent_slug = parent_slug_match.group(1)
    else:
        parent_slug = sanitize_slug(next_step)
    slug = f"{parent_slug}-retry-{next_attempt}"[:80]
    new_task_id = f"task-{now_ts()}-{slug}"

    # Inherit resolved agent from the original task when available, so
    # follow-ups are routed to a concrete agent instead of stalling unassigned.
    inherited_agent = str(original_meta.get("resolved_agent", "") or "").strip().lower()
    if inherited_agent not in VALID_FALLBACK_AGENTS:
        inherited_agent = "auto"

    frontmatter = {
        "task_id": new_task_id,
        "repo": repo,
        "agent": inherited_agent,
        "task_type": task_type,
        "branch": original_branch,
        "base_branch": base_branch,
        "allow_push": bool(allow_push),
        "parent_task_id": parent_task_id,
        "attempt": next_attempt,
        "max_attempts": max_attempts,
        "max_runtime_minutes": max_runtime_minutes,
        # Follow-ups are a NEW task (the parent's next_step), so every agent
        # deserves a fresh attempt. Inheriting the parent's model_attempts
        # caused cascades where a fallback_exhausted parent produced a
        # follow-up that instantly exhausted again, spamming telegrams.
        "model_attempts": [],
        "github_repo": original_meta.get("github_repo"),
        "github_issue_number": original_meta.get("github_issue_number"),
        "github_issue_url": original_meta.get("github_issue_url"),
        "prompt_snapshot_path": str(Path(original_meta.get("prompt_snapshot_path", inbox.parent.parent / "prompts" / f"{new_task_id}.txt")).parent / f"{new_task_id}.txt"),
    }
    for key in ("objective_id", "sprint_id", "parent_issue", "parent_goal_summary"):
        value = str(original_meta.get(key) or "").strip()
        if value:
            frontmatter[key] = value
    # Propagate structured failed_checks so CI verification survives follow-up reformatting.
    if original_meta.get("failed_checks"):
        frontmatter["failed_checks"] = original_meta["failed_checks"]

    frontmatter_text = yaml.safe_dump(frontmatter, sort_keys=False).strip()
    goal_ancestry_block = format_goal_ancestry_block(frontmatter)

    content = f"""---
{frontmatter_text}
---

# Goal

{next_step}

# Success Criteria

- Continue progressing toward the original task goal
- Avoid repeating failed approaches unless new evidence justifies it
- Update .agent_result.md with final status and a high-quality handoff packet

# Constraints

- Continue on the same branch
- Work only inside the repo
- Prefer minimal diffs
- Do not repeat failed approaches from ATTEMPTED_APPROACHES unless there is a clear reason

{goal_ancestry_block}

# Original Task

{original_body}

# Prior Summary

{result.get("summary", "No summary provided.")}

# Prior Blocker Code

{result.get("blocker_code", "none") or "none"}

# Prior Progress

{chr(10).join(result.get("done", ["- None"]))}

# Known Blockers

{chr(10).join(result.get("blockers", ["- None"]))}

# Files Changed So Far

{chr(10).join(result.get("files_changed", ["- None"]))}

# Tests Run So Far

{chr(10).join(result.get("tests_run", ["- None"]))}

# Prior Decisions

{chr(10).join(result.get("decisions", ["- None"]))}

# Known Risks

{chr(10).join(result.get("risks", ["- None"]))}

# Avoid Repeating These Approaches

{chr(10).join(result.get("attempted_approaches", ["- None"]))}

# Prior Unblock Notes

{_format_unblock_notes_for_followup(result.get("unblock_notes"))}

# Models Already Tried In This Task Lineage

{chr(10).join(f"- {m}" for m in model_attempts) if model_attempts else "- None"}
"""

    followup_name = f"{new_task_id}.md"
    followup_path = inbox / followup_name
    followup_path.write_text(content, encoding="utf-8")
    log(f"Created follow-up task: {followup_path}", logfile, also_summary=True, queue_summary_log=queue_summary_log)
    return followup_path

def create_escalation_note(original_meta: dict, original_body: str, result: dict, logfile: Path, model_attempts: list[str], escalated: Path, queue_summary_log: Path):
    parent_task_id = original_meta.get("parent_task_id", original_meta["task_id"])
    esc_path = escalated / f"{parent_task_id}-escalation.md"
    goal_ancestry_block = format_goal_ancestry_block(original_meta)

    content = f"""# Escalation Note

## Parent Task ID
{parent_task_id}

## Branch
{original_meta.get("branch", "unknown")}

## Repo
{original_meta.get("repo", "unknown")}

## Task Type
{original_meta.get("task_type", "unknown")}

## Prompt Snapshot
{original_meta.get("prompt_snapshot_path") or "none"}

## Models Tried
{", ".join(model_attempts) if model_attempts else "None"}

## Final Status
{result.get("status", "blocked")}

## Blocker Code
{result.get("blocker_code", "none") or "none"}

## Summary
{result.get("summary", "No summary provided.")}

{goal_ancestry_block}

## Original Task
{original_body}

## Completed
{chr(10).join(result.get("done", ["- None"]))}

## Blockers
{chr(10).join(result.get("blockers", ["- None"]))}

## Next Suggested Step
{result.get("next_step", "None")}

## Files Changed
{chr(10).join(result.get("files_changed", ["- None"]))}

## Tests Run
{chr(10).join(result.get("tests_run", ["- None"]))}

## Decisions
{chr(10).join(result.get("decisions", ["- None"]))}

## Risks
{chr(10).join(result.get("risks", ["- None"]))}

## Attempted Approaches
{chr(10).join(result.get("attempted_approaches", ["- None"]))}

## Unblock Notes
{_format_unblock_notes_for_followup(result.get("unblock_notes"))}
"""
    esc_path.write_text(content, encoding="utf-8")
    log(f"Created escalation note: {esc_path}", logfile, also_summary=True, queue_summary_log=queue_summary_log)
    return esc_path

def cleanup_worktree(repo: Path, worktree: Path, logfile: Path, queue_summary_log: Path):
    try:
        run(["git", "-C", repo, "worktree", "remove", "--force", worktree], logfile=logfile, check=False, queue_summary_log=queue_summary_log)
        run(["git", "-C", repo, "worktree", "prune"], logfile=logfile, check=False, queue_summary_log=queue_summary_log)
    except Exception as e:
        log(f"Worktree cleanup warning: {e}", logfile, queue_summary_log=queue_summary_log)

def move_processing_task(processing: Path, destination_dir: Path, logfile: Path, queue_summary_log: Path, *, state_label: str) -> bool:
    """Move a processing task to its terminal mailbox without re-failing if it was already moved.

    Historical runs showed a task could finish its blocked transition, then the
    final move raised ``FileNotFoundError`` and the outer exception handler
    converted a legitimate blocked outcome into a noisy infrastructure
    failure. Treat a missing processing file as an idempotent transition.
    """
    destination = destination_dir / processing.name
    if not processing.exists():
        if destination.exists():
            log(
                f"Task file already present in {state_label}; treating mailbox move as idempotent.",
                logfile,
                queue_summary_log=queue_summary_log,
            )
            return True
        log(
            f"Task file missing before move to {state_label}; skipping mailbox move without raising.",
            logfile,
            queue_summary_log=queue_summary_log,
        )
        return False
    shutil.move(str(processing), str(destination))
    return True

def run_tests(cfg: dict, repo: Path, worktree: Path, logfile: Path, queue_summary_log: Path) -> None:
    """Run configured test suite in worktree. Modifies .agent_result.md in-place."""
    repo_configs = cfg.get("repo_configs", {})
    repo_cfg = repo_configs.get(str(repo.resolve()), repo_configs.get(repo.name, {}))
    test_command = (repo_cfg or {}).get("test_command") or cfg.get("test_command")
    if not test_command:
        return  # No test command configured; skip silently

    timeout_secs = int(cfg.get("test_timeout_minutes", 5)) * 60
    log(f"Running tests: {test_command}", logfile, queue_summary_log=queue_summary_log)

    try:
        proc = subprocess.run(
            test_command,
            shell=True,
            cwd=str(worktree),
            capture_output=True,
            text=True,
            timeout=timeout_secs,
        )
        passed = proc.returncode == 0
        status_label = "PASSED" if passed else f"FAILED (exit {proc.returncode})"
    except subprocess.TimeoutExpired:
        passed = False
        status_label = f"TIMEOUT after {timeout_secs}s"
    except Exception as exc:
        passed = False
        status_label = f"ERROR: {exc}"

    log(f"Test result: {status_label}", logfile, queue_summary_log=queue_summary_log)

    result_path = worktree / ".agent_result.md"
    if not result_path.exists():
        return

    text = result_path.read_text(encoding="utf-8")
    test_bullet = f"- {test_command} → {status_label}"

    # Append bullet to TESTS_RUN section
    m = re.search(r'^TESTS_RUN:\s*$', text, re.MULTILINE)
    if m:
        rest = text[m.end():]
        next_hdr = re.search(r'\n\n[A-Z][A-Z_]+:', rest)
        if next_hdr:
            ins = m.end() + next_hdr.start()
            text = text[:ins] + '\n' + test_bullet + text[ins:]
        else:
            text = text.rstrip('\n') + '\n' + test_bullet + '\n'
    else:
        text = text.rstrip('\n') + f'\n\nTESTS_RUN:\n{test_bullet}\n'

    # Override STATUS if tests failed and agent reported complete
    if not passed and re.search(r'^STATUS:\s*complete\s*$', text, re.MULTILINE):
        text = re.sub(r'^STATUS:\s*complete\s*$', 'STATUS: partial', text, flags=re.MULTILINE)
        text = _upsert_single_value_section(
            text,
            "BLOCKER_CODE",
            "test_failure",
            ["SUMMARY", "DONE", "BLOCKERS", "NEXT_STEP", "FILES_CHANGED", "TESTS_RUN", "DECISIONS", "RISKS", "ATTEMPTED_APPROACHES", "MANUAL_STEPS"],
        )
        blocker = f"- Tests failed: {test_command} → {status_label}"
        bm = re.search(r'^BLOCKERS:\s*$', text, re.MULTILINE)
        if bm:
            text = text[:bm.end()] + '\n' + blocker + text[bm.end():]
        else:
            text = text.rstrip('\n') + f'\n\nBLOCKERS:\n{blocker}\n'

        # Add UNBLOCK_NOTES for the test-failure downgrade
        if not re.search(r'^UNBLOCK_NOTES:', text, re.MULTILINE):
            text = text.rstrip('\n') + f'\n\nUNBLOCK_NOTES:\n- blocking_cause: Tests failed ({test_command} → {status_label})\n- next_action: Fix the failing tests and rerun the task.\n'

    result_path.write_text(text, encoding="utf-8")

def record_metrics(
    cfg: dict,
    meta: dict,
    final_result: dict,
    final_agent: str | None,
    model_attempts: list[str],
    start_time: datetime,
    logfile: Path | None,
    queue_summary_log: Path | None,
) -> None:
    """Atomically append task metrics to runtime/metrics/agent_stats.jsonl.

    Skips records that don't reflect agent quality (fallback_exhausted
    sentinels with agent=none, and runner_failure infra errors) so the
    health gates don't wrongly starve the chain after infra incidents.
    """
    blocker_code = final_result.get("blocker_code", "")
    # Keep in sync with agent_scorer._TRANSIENT_BLOCKER_CODES — transient
    # provider/infra failures don't reflect agent quality and must not gate
    # the fallback chain.
    if final_agent in (None, "", "none") or blocker_code in ("fallback_exhausted", "runner_failure", "quota_limited"):
        return

    metrics_dir = Path(cfg.get("root_dir", ".")).expanduser() / "runtime" / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    metrics_file = metrics_dir / "agent_stats.jsonl"

    duration = (datetime.now(tz=timezone.utc) - start_time).total_seconds()
    record = {
        "timestamp": datetime.now().isoformat(),
        "task_id": meta.get("task_id", "unknown"),
        "repo": str(meta.get("repo", "unknown")),

        "github_repo": str(meta.get("github_repo", "")).strip(),

        "github_repo": meta.get("github_repo"),
        "github_issue_number": meta.get("github_issue_number"),

        "agent": final_agent or "unknown",
        "status": final_result.get("status", "unknown"),
        "blocker_code": blocker_code,
        "attempt_count": len(model_attempts),
        "duration_seconds": round(duration, 1),
        "task_type": meta.get("task_type", "unknown"),
        "model_attempt_details": list(meta.get("model_attempt_details") or []),
    }
    for key in ("objective_id", "sprint_id", "parent_issue", "parent_goal_summary"):
        value = meta.get(key)
        if value:
            record[key] = value
    line = json.dumps(record) + "\n"

    # Rotate at 10 MB to prevent unbounded growth
    if metrics_file.exists() and metrics_file.stat().st_size > 10 * 1024 * 1024:
        metrics_file.rename(metrics_file.with_suffix(".jsonl.1"))

    # Atomic append: read existing + new line → temp file → rename
    existing = metrics_file.read_text(encoding="utf-8") if metrics_file.exists() else ""
    fd, tmp_path = tempfile.mkstemp(dir=metrics_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(existing + line)
        os.replace(tmp_path, metrics_file)
    except Exception:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        raise

    log(f"Metrics recorded for {record['task_id']}: status={record['status']}, agent={record['agent']}", logfile, queue_summary_log=queue_summary_log)
    try:
        rebuild_cost_records(cfg)
    except Exception as exc:
        log(f"Cost tracking warning: {exc}", logfile, queue_summary_log=queue_summary_log)
    try:
        record_cost_events(
            cfg,
            task_id=str(record.get("task_id", "unknown")),
            timestamp=str(record.get("timestamp") or ""),
            github_repo=str(record.get("github_repo") or ""),
            model_attempt_details=list(record.get("model_attempt_details") or []),
        )
        warn_if_budgets_missing(
            cfg,
            logger=lambda msg: log(msg, logfile, queue_summary_log=queue_summary_log),
        )
        check_budget_alerts(
            cfg,
            lambda c, text: send_telegram(c, text, logfile, queue_summary_log),
            logger=lambda msg: log(msg, logfile, queue_summary_log=queue_summary_log),
        )
    except Exception as exc:
        log(f"Budget tracking warning: {exc}", logfile, queue_summary_log=queue_summary_log)

FALLBACK_COOLDOWN_MINUTES = 120
FALLBACK_COOLDOWN_FILE = "fallback_cooldown_until.txt"
AGENT_COOLDOWN_FILE = "agent_cooldowns.json"

def _cooldown_path(cfg: dict) -> Path:
    state_dir = Path(cfg.get("root_dir", ".")).expanduser() / "runtime" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / FALLBACK_COOLDOWN_FILE

def _agent_cooldown_path(cfg: dict) -> Path:
    state_dir = Path(cfg.get("root_dir", ".")).expanduser() / "runtime" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / AGENT_COOLDOWN_FILE

def _read_agent_cooldowns(cfg: dict) -> dict[str, str]:
    path = _agent_cooldown_path(cfg)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k).strip().lower(): str(v) for k, v in data.items() if str(k).strip()}

def agent_cooldown_remaining(cfg: dict, agent: str) -> int:
    """Return seconds remaining for an agent-specific quota cooldown."""
    agent_key = str(agent or "").strip().lower()
    if not agent_key:
        return 0
    raw_until = _read_agent_cooldowns(cfg).get(agent_key)
    if not raw_until:
        return 0
    try:
        until = datetime.fromisoformat(raw_until)
    except ValueError:
        return 0
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    delta = (until - datetime.now(timezone.utc)).total_seconds()
    return max(0, int(delta))

def start_agent_cooldown(
    cfg: dict,
    agent: str,
    minutes: int = FALLBACK_COOLDOWN_MINUTES,
    *,
    until: datetime | None = None,
) -> datetime:
    """Pause dispatch to one quota-limited agent without stopping other agents."""
    agent_key = str(agent or "").strip().lower()
    if not agent_key:
        return datetime.now(timezone.utc)
    path = _agent_cooldown_path(cfg)
    now = datetime.now(timezone.utc)
    cooldowns = _read_agent_cooldowns(cfg)
    existing_raw = cooldowns.get(agent_key)
    if existing_raw:
        try:
            existing = datetime.fromisoformat(existing_raw)
            if existing.tzinfo is None:
                existing = existing.replace(tzinfo=timezone.utc)
            if existing > now:
                return existing
        except ValueError:
            pass
    until = until or (now + timedelta(minutes=minutes))
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    until = until.astimezone(timezone.utc)
    if until <= now:
        until = now + timedelta(minutes=minutes)
    cooldowns[agent_key] = until.isoformat()
    path.write_text(json.dumps(cooldowns, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return until

def fallback_cooldown_remaining(cfg: dict) -> int:
    """Return seconds remaining in the fallback-exhausted cooldown, or 0."""
    path = _cooldown_path(cfg)
    if not path.exists():
        return 0
    try:
        until = datetime.fromisoformat(path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return 0
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    delta = (until - datetime.now(timezone.utc)).total_seconds()
    return max(0, int(delta))

def start_fallback_cooldown(cfg: dict, minutes: int = FALLBACK_COOLDOWN_MINUTES) -> datetime:
    """Arm the cooldown if not already active; returns the active expiry timestamp.

    Idempotent: workers that race into the exhausted branch simultaneously
    will all see the same expiry rather than each extending the window. This
    is what stops 'one telegram per worker' spam — combined with the
    prior_cooldown==0 debounce in the caller, only the first arming sends.
    """
    path = _cooldown_path(cfg)
    now = datetime.now(timezone.utc)
    if path.exists():
        try:
            existing = datetime.fromisoformat(path.read_text(encoding="utf-8").strip())
            if existing.tzinfo is None:
                existing = existing.replace(tzinfo=timezone.utc)
            if existing > now:
                return existing
        except (ValueError, OSError):
            pass
    until = now + timedelta(minutes=minutes)
    path.write_text(until.isoformat(), encoding="utf-8")
    return until

def downgrade_no_diff_complete(meta: dict, final_result: dict, agent: str | None) -> dict:
    """Downgrade status=complete to partial when a diff-required task produced no changes.

    Some agents (notably gemini in observed runs) report STATUS: complete even
    when they wrote nothing to the worktree, leading the orchestrator to close
    issues with no actual work delivered. For task types that should produce
    a diff, treat zero-change "complete" as partial with a no_diff_produced
    blocker so the issue stays open and gets re-attempted.
    """
    task_type = str(meta.get("task_type", "") or "").strip().lower()
    if task_type not in DIFF_REQUIRED_TASK_TYPES:
        return final_result
    if final_result.get("status") != "complete":
        return final_result

    agent_label = agent or "unknown"
    summary = (
        f"Agent {agent_label} reported STATUS: complete but produced no file "
        f"changes for a {task_type} task."
    )
    next_step = (
        "Re-dispatch the task. If this recurs, inspect the agent transcript for "
        "tooling failures (e.g. missing write_file/run_shell_command) or refine "
        "the task brief so the expected diff is unambiguous."
    )
    downgraded = dict(final_result)
    downgraded["status"] = "partial"
    downgraded["blocker_code"] = "no_diff_produced"
    downgraded["summary"] = summary
    downgraded["blockers"] = list(final_result.get("blockers") or []) + [
        f"- No file changes were committed despite STATUS: complete (agent={agent_label})."
    ]
    downgraded["next_step"] = next_step
    downgraded["unblock_notes"] = {
        "blocking_cause": summary,
        "next_action": next_step,
    }
    return downgraded

def downgrade_web_no_artifact(
    meta: dict,
    body: str,
    final_result: dict,
    agent: str | None,
    worktree: Path | None,
) -> dict:
    """Downgrade status=complete to partial when a homepage/web-implementation
    task shipped without the expected HTML entry point.

    This closes the failure mode observed on liminalconsultants#2, where the
    worker interpreted "build homepage" as a copy-planning task and produced a
    markdown spec instead of an index.html at the web root. The check is
    narrowly scoped — it only fires for tasks whose body explicitly names a
    homepage/landing-page concern, so "build About page" style tasks that
    already produce <name>.html are unaffected.
    """
    if final_result.get("status") != "complete":
        return final_result
    kind = _web_task_kind(meta, body)
    if kind != "homepage":
        return final_result
    if worktree is None or not worktree.exists():
        return final_result
    if (worktree / "index.html").exists():
        return final_result

    agent_label = agent or "unknown"
    summary = (
        f"Agent {agent_label} reported STATUS: complete for a homepage-class task, "
        "but no index.html is present at the repo root."
    )
    next_step = (
        "Re-dispatch after producing index.html at the repository root. Any markdown "
        "copy-specs must be a supplement to the HTML file, not a substitute."
    )
    downgraded = dict(final_result)
    downgraded["status"] = "partial"
    downgraded["blocker_code"] = "no_web_artifact"
    downgraded["summary"] = summary
    downgraded["blockers"] = list(final_result.get("blockers") or []) + [
        f"- Homepage task completed without index.html at repo root (agent={agent_label}).",
    ]
    downgraded["next_step"] = next_step
    downgraded["unblock_notes"] = {
        "blocking_cause": summary,
        "next_action": next_step,
    }
    return downgraded


def synthesize_exhausted_result(model_attempts: list[str]) -> dict:
    return {
        "status": "blocked",
        "blocker_code": "fallback_exhausted",
        "summary": "All configured models for this task type were already tried.",
        "done": ["- Multiple model attempts were made."],
        "blockers": [f"- No remaining fallback models. Tried: {', '.join(model_attempts) if model_attempts else 'none'}"],
        "next_step": "Review the escalation note and decide whether to refine the task, add missing context, or intervene manually.",
        "files_changed": ["- Unknown / inspect branch"],
        "tests_run": ["- Unknown / inspect prior logs"],
        "decisions": ["- Exhausted configured model fallback chain"],
        "risks": ["- Further automated retries may repeat unproductive behavior"],
        "attempted_approaches": [f"- Models tried so far: {', '.join(model_attempts) if model_attempts else 'none'}"],
        "unblock_notes": {
            "blocking_cause": f"All fallback models exhausted ({', '.join(model_attempts) if model_attempts else 'none'})",
            "next_action": "Review the escalation note and decide whether to refine the task, add missing context, or intervene manually.",
        },
        "raw": "",
    }

def main():
    cfg = load_config()
    paths = runtime_paths(cfg)

    ROOT = paths["ROOT"]
    INBOX = paths["INBOX"]
    PROCESSING = paths["PROCESSING"]
    DONE = paths["DONE"]
    FAILED = paths["FAILED"]
    BLOCKED = paths["BLOCKED"]
    ESCALATED = paths["ESCALATED"]
    LOGS = paths["LOGS"]
    QUEUE_SUMMARY_LOG = paths["QUEUE_SUMMARY_LOG"]

    worker_id = os.environ.get("QUEUE_WORKER_ID", "w0")
    maybe_run_stall_watchdog(cfg, paths, worker_id=worker_id, queue_summary_log=QUEUE_SUMMARY_LOG)

    cooldown_left = fallback_cooldown_remaining(cfg)
    if cooldown_left > 0:
        mins = (cooldown_left + 59) // 60
        print(f"[{worker_id}] Fallback cooldown active — {mins} min remaining. Skipping this tick.")
        return

    task = pick_task(INBOX, cfg)
    if not task:
        print(f"[{worker_id}] No tasks in inbox.")
        return

    processing = PROCESSING / task.name
    try:
        shutil.move(str(task), str(processing))
        os.utime(processing, None)
    except FileNotFoundError:
        print(f"[{worker_id}] Task picked by another worker. Exiting.")
        return

    task_id = processing.stem
    logfile = LOGS / f"{task_id}.log"
    worktree = None
    repo = None
    repo_lock_fh = None

    try:
        # UTC-aware so it can be compared to GitHub API timestamps (which are
        # tz-aware ISO-8601) in verify_pr_ci_debug_completion. Using a naive
        # datetime here caused an infinite infra-retry loop on CI-debug tasks
        # when PRs #163/#165 failed CI on 2026-04-09.
        start_time = datetime.now(tz=timezone.utc)
        meta, body = parse_task(processing)

        task_id = meta["task_id"]
        logfile = LOGS / f"{task_id}.log"
        _write_processing_lock(processing, meta, worker_id)

        repo = Path(meta["repo"])
        base_branch = meta.get("base_branch", cfg["default_base_branch"])
        branch = meta.get("branch", f"agent/{task_id}")
        allow_push = bool(meta.get("allow_push", cfg["default_allow_push"]))
        task_type = meta.get("task_type", cfg["default_task_type"])
        model_attempts = list(meta.get("model_attempts", []))
        model_attempt_details = []
        prior_results = []
        dispatcher_only_mode = _is_dispatcher_only_task(cfg, meta)

        _priority_label = meta.get("priority", "prio:normal")
        _weights = cfg.get("priority_weights", {"prio:high": 30, "prio:normal": 10, "prio:low": 0})
        _weight = _weights.get(_priority_label, _weights.get("prio:normal", 10))
        _score = priority_score(processing, cfg)
        log(f"[{worker_id}] Processing task: {task_id}", logfile, also_summary=True, queue_summary_log=QUEUE_SUMMARY_LOG)
        log(f"Priority: {_priority_label} (weight={_weight}, score={_score:.2f})", logfile, also_summary=True, queue_summary_log=QUEUE_SUMMARY_LOG)
        log(f"Repo: {repo}", logfile, queue_summary_log=QUEUE_SUMMARY_LOG)
        log(f"Base branch: {base_branch}", logfile, queue_summary_log=QUEUE_SUMMARY_LOG)
        log(f"Branch: {branch}", logfile, queue_summary_log=QUEUE_SUMMARY_LOG)
        log(f"Task type: {task_type}", logfile, queue_summary_log=QUEUE_SUMMARY_LOG)
        log(f"Requested agent: {meta.get('agent', cfg['default_agent'])}", logfile, queue_summary_log=QUEUE_SUMMARY_LOG)
        log(f"Prior model attempts: {model_attempts}", logfile, queue_summary_log=QUEUE_SUMMARY_LOG)
        log(f"Allow push: {allow_push}", logfile, queue_summary_log=QUEUE_SUMMARY_LOG)

        if str(repo) not in cfg["allowed_repos"]:
            raise RuntimeError(f"Repo not allowed: {repo}")

        if not repo.exists():
            raise RuntimeError(f"Repo does not exist: {repo}")

        # Skip tasks whose linked GitHub issue is already closed/done
        _gh_repo = meta.get("github_repo")
        _gh_issue = meta.get("github_issue_number")
        if _gh_repo and _gh_issue:
            try:
                _snapshot = _gh_json([
                    "issue", "view", str(_gh_issue), "-R", str(_gh_repo),
                    "--json", "state,labels",
                ]) or {}
                _state = str(_snapshot.get("state", "")).upper()
                _labels = {
                    (l.get("name", "") if isinstance(l, dict) else str(l)).strip().lower()
                    for l in (_snapshot.get("labels") or [])
                }
                if _state == "CLOSED" or "done" in _labels:
                    log(
                        f"[{worker_id}] Skipping task {task_id}: linked issue #{_gh_issue} is already {_state} (labels: {_labels}). Moving to done.",
                        logfile, also_summary=True, queue_summary_log=QUEUE_SUMMARY_LOG,
                    )
                    shutil.move(str(processing), str(DONE / processing.name))
                    return
            except Exception as e:
                log(f"Warning: could not check issue #{_gh_issue} state: {e}", logfile, queue_summary_log=QUEUE_SUMMARY_LOG)

        # Per-repo lock: prevent concurrent access to the same repository
        repo_key = hashlib.md5(str(repo.resolve()).encode()).hexdigest()[:12]
        repo_lock_path = Path(f"/tmp/agent_os_repo_{repo_key}.lock")
        repo_lock_fh = repo_lock_path.open("w")
        try:
            fcntl.flock(repo_lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            repo_lock_fh.close()
            repo_lock_fh = None
            log(f"[{worker_id}] Repo {repo.name} locked by another worker. Returning task to inbox.", logfile, also_summary=True, queue_summary_log=QUEUE_SUMMARY_LOG)
            shutil.move(str(processing), str(INBOX / processing.name))
            return
        log(f"[{worker_id}] Acquired repo lock: {repo.name}", logfile, queue_summary_log=QUEUE_SUMMARY_LOG)

        worktree = ensure_worktree(cfg, repo, base_branch, branch, task_id, logfile, QUEUE_SUMMARY_LOG)

        final_result = None
        final_agent = None

        while True:
            current_agent = get_next_agent(meta, cfg, model_attempts)

            if current_agent is None:
                final_result = synthesize_exhausted_result(model_attempts)
                final_agent = "none"
                log("No remaining agents in fallback chain.", logfile, also_summary=True, queue_summary_log=QUEUE_SUMMARY_LOG)
                # Arm a cooldown so the next cron ticks don't chew through the
                # whole queue spamming the same failure. Notify only on the
                # transition into cooldown, not once per queued task.
                prior_cooldown = fallback_cooldown_remaining(cfg)
                until = start_fallback_cooldown(cfg)
                if prior_cooldown == 0:
                    send_telegram(
                        cfg,
                        f"🧊 Fallback cooldown\nAll agents exhausted on {task_id} ({repo.name}).\n"
                        f"Queue paused for {FALLBACK_COOLDOWN_MINUTES // 60}h{(' %dm' % (FALLBACK_COOLDOWN_MINUTES % 60)) if FALLBACK_COOLDOWN_MINUTES % 60 else ''} (until {until.strftime('%H:%M')} UTC).\n"
                        f"Models tried: {', '.join(model_attempts) or 'none'}",
                        logfile,
                        QUEUE_SUMMARY_LOG,
                    )
                break

            timeout_minutes = timeout_for_agent(current_agent, meta, cfg)
            meta_for_prompt = dict(meta)
            meta_for_prompt["resolved_agent"] = current_agent
            meta_for_prompt["model_attempts"] = model_attempts
            try:
                prompt_file = write_prompt(task_id, meta_for_prompt, body, current_agent, prior_results, ROOT, worktree=worktree)
            except PromptTooLargeError as e:
                oversize_result = {
                    "agent": current_agent,
                    "status": "blocked",
                    "blocker_code": "prompt_too_large",
                    "summary": f"Rendered prompt is {e.size_bytes} bytes, exceeding the {e.limit_bytes}-byte ceiling.",
                    "done": ["- Attempted to render prompt before dispatch."],
                    "blockers": [
                        f"- Prompt size {e.size_bytes} bytes exceeds {e.limit_bytes}-byte limit.",
                        "- Retrying with more prior-attempt context will not help; the task body itself must be trimmed.",
                    ],
                    "next_step": "none",
                    "files_changed": ["- None"],
                    "tests_run": ["- None"],
                    "decisions": ["- Refused to dispatch oversized prompt to avoid execve E2BIG."],
                    "risks": ["- Downstream retry loops would burn credits without making progress."],
                    "attempted_approaches": [f"- Rendered prompt for {current_agent}; size={e.size_bytes}B."],
                    "unblock_notes": {
                        "blocking_cause": f"Prompt size {e.size_bytes}B exceeds argv ceiling {e.limit_bytes}B.",
                        "next_action": "Split the issue, shorten the body, or reduce prior-attempt history before re-queuing.",
                    },
                    "raw": "",
                }
                model_attempts.append(current_agent)
                model_attempt_details.append({
                    **{
                        "attempt": len(model_attempts),
                        "agent": current_agent,
                        "provider": resolve_attempt_provider(current_agent, cfg),
                        "model": resolve_attempt_model(current_agent, cfg),
                        "input_chars": e.size_bytes,
                        "input_tokens_estimate": 0,
                    },
                    "status": oversize_result["status"],
                    "blocker_code": oversize_result["blocker_code"],
                    "output_chars": 0,
                    "output_tokens_estimate": 0,
                })
                prior_results.append(oversize_result)
                log(
                    f"Refusing to dispatch: prompt {e.size_bytes}B exceeds {e.limit_bytes}B limit.",
                    logfile,
                    also_summary=True,
                    queue_summary_log=QUEUE_SUMMARY_LOG,
                )
                final_result = oversize_result
                final_agent = current_agent
                break
            prompt_text = prompt_file.read_text(encoding="utf-8")
            attempt_number = len(model_attempts) + 1
            attempt_base = {
                "attempt": attempt_number,
                "agent": current_agent,
                "provider": resolve_attempt_provider(current_agent, cfg),
                "model": resolve_attempt_model(current_agent, cfg),
                "input_chars": len(prompt_text),
                "input_tokens_estimate": estimate_text_tokens(prompt_text),
            }

            if not model_attempts:
                send_telegram(
                    cfg,
                    f"🚀 Started\nTask: {task_id}\nRepo: {repo.name}\nBranch: {branch}\nModel: {current_agent}\nTask type: {task_type}",
                    logfile,
                    QUEUE_SUMMARY_LOG,
                )

            log(f"Current agent: {current_agent}", logfile, also_summary=True, queue_summary_log=QUEUE_SUMMARY_LOG)
            log(f"Timeout minutes for {current_agent}: {timeout_minutes}", logfile, queue_summary_log=QUEUE_SUMMARY_LOG)

            try:
                run_agent(
                    current_agent,
                    worktree,
                    prompt_file,
                    logfile,
                    timeout_minutes=timeout_minutes,
                    root=ROOT,
                    queue_summary_log=QUEUE_SUMMARY_LOG,
                )
            except subprocess.TimeoutExpired:
                timeout_result = {
                    "agent": current_agent,
                    "status": "blocked",
                    "blocker_code": "timeout",
                    "summary": f"{current_agent} timed out.",
                    "done": ["- Execution started but timed out."],
                    "blockers": ["- Model execution exceeded timeout."],
                    "next_step": "Try the next fallback model or split the task into a smaller step.",
                    "files_changed": ["- Inspect git status"],
                    "tests_run": ["- None"],
                    "decisions": ["- Timed out during execution"],
                    "risks": ["- Long-running task may be too broad for one attempt"],
                    "attempted_approaches": ["- Timed run exceeded timeout budget"],
                    "unblock_notes": {
                        "blocking_cause": f"{current_agent} execution exceeded timeout budget",
                        "next_action": "Try the next fallback model or split the task into a smaller step.",
                    },
                    "raw": "",
                }
                model_attempts.append(current_agent)
                model_attempt_details.append({
                    **attempt_base,
                    "status": timeout_result["status"],
                    "blocker_code": timeout_result["blocker_code"],
                    "output_chars": 0,
                    "output_tokens_estimate": 0,
                })
                prior_results.append(timeout_result)
                log(f"{current_agent} timed out.", logfile, also_summary=True, queue_summary_log=QUEUE_SUMMARY_LOG)

                if get_next_agent(meta, cfg, model_attempts) is None:
                    final_result = timeout_result
                    final_agent = current_agent
                    break
                continue

            except Exception as e:
                failure_summary, failure_blockers, failure_detail = _format_runner_failure(e)
                runner_blocker_code = _blocker_code_from_runner_failure(failure_summary, failure_detail)
                runner_next_step = "Try the next fallback model. If all models fail, inspect runner config, credentials, or quotas."
                runner_unblock_notes = {
                    "blocking_cause": f"{current_agent} runner failure: {failure_summary}",
                    "next_action": runner_next_step,
                }
                if runner_blocker_code == "quota_limited":
                    runner_next_step = (
                        f"Wait for {current_agent} quota cooldown to expire, or reroute to another available model."
                    )
                    runner_unblock_notes = {
                        "blocking_cause": f"{current_agent} hit a provider quota/rate limit.",
                        "next_action": runner_next_step,
                    }
                runner_result = {
                    "agent": current_agent,
                    "status": "blocked",
                    "blocker_code": runner_blocker_code,
                    "summary": f"{current_agent} failed before producing a valid result file. {failure_summary}",
                    "done": ["- Agent runner was invoked."],
                    "blockers": failure_blockers,
                    "next_step": runner_next_step,
                    "files_changed": ["- Unknown / inspect worktree"],
                    "tests_run": ["- None"],
                    "decisions": ["- Treat runner failure as model-level failure and continue fallback chain if possible."],
                    "risks": ["- Model quota/auth/CLI issues may affect multiple tasks."],
                    "attempted_approaches": [f"- Attempted model: {current_agent}", f"- Failure detail: {failure_summary}"],
                    "unblock_notes": runner_unblock_notes,
                    "raw": "",
                }
                model_attempts.append(current_agent)
                model_attempt_details.append({
                    **attempt_base,
                    "status": runner_result["status"],
                    "blocker_code": runner_result["blocker_code"],
                    "output_chars": 0,
                    "output_tokens_estimate": 0,
                })
                prior_results.append(runner_result)
                log(f"{current_agent} runner failure: {e}", logfile, also_summary=True, queue_summary_log=QUEUE_SUMMARY_LOG)
                for blocker in failure_blockers:
                    log(blocker, logfile, queue_summary_log=QUEUE_SUMMARY_LOG)

                if runner_result["blocker_code"] == "quota_limited":
                    reset_at = _quota_reset_at("\n".join([failure_summary, failure_detail or ""]))
                    until = start_agent_cooldown(cfg, current_agent, until=reset_at)
                    log(
                        f"Agent {current_agent} quota-limited; cooling down until {until.isoformat()}.",
                        logfile,
                        also_summary=True,
                        queue_summary_log=QUEUE_SUMMARY_LOG,
                    )

                next_agent = get_next_agent(meta, cfg, model_attempts)
                if next_agent is not None:
                    detail_line = f"\nDetail: {_tail_text(failure_detail, max_lines=4, max_chars=300)}" if failure_detail else ""
                    send_telegram(
                        cfg,
                        f"🔁 Fallback\nTask: {task_id}\nRepo: {repo.name}\nBranch: {branch}\nPrevious model: {current_agent}\nNext model: {next_agent}\nReason: {failure_summary}{detail_line}",
                        logfile,
                        QUEUE_SUMMARY_LOG,
                    )
                    continue

                final_result = runner_result
                final_agent = current_agent
                break

            run_tests(cfg, repo, worktree, logfile, QUEUE_SUMMARY_LOG)

            result = parse_agent_result(worktree)
            if result.get("blocker_code") == "invalid_result_contract":
                environment_result = _runner_environment_failure_from_log(logfile)
                if environment_result is not None:
                    result = environment_result
            result["agent"] = current_agent
            model_attempts.append(current_agent)
            output_text = result.get("raw", "") or ""
            model_attempt_details.append({
                **attempt_base,
                "status": result.get("status", "unknown"),
                "blocker_code": result.get("blocker_code", "none") or "none",
                "output_chars": len(output_text),
                "output_tokens_estimate": estimate_text_tokens(output_text),
            })
            prior_results.append(result)

            log(f"Worker status from {current_agent}: {result['status']}", logfile, queue_summary_log=QUEUE_SUMMARY_LOG)
            log("Worker result file:", logfile, queue_summary_log=QUEUE_SUMMARY_LOG)
            log(result["raw"], logfile, queue_summary_log=QUEUE_SUMMARY_LOG)

            if result["status"] == "complete":
                final_result = result
                final_agent = current_agent
                break

            if result["status"] == "partial":
                final_result = result
                final_agent = current_agent
                break

            if result["status"] == "blocked":
                next_agent = get_next_agent(meta, cfg, model_attempts)
                if should_try_fallback(result) and next_agent is not None:
                    send_telegram(
                        cfg,
                        f"⏸️ Blocked\nTask: {task_id}\nRepo: {repo.name}\nBranch: {branch}\nModel: {current_agent}\nTrying next model: {next_agent}\nReason: {result['summary']}",
                        logfile,
                        QUEUE_SUMMARY_LOG,
                    )
                    continue

                final_result = result
                final_agent = current_agent
                break

        meta["model_attempts"] = model_attempts
        meta["model_attempt_details"] = model_attempt_details
        if final_agent and final_agent != "none":
            meta["resolved_agent"] = final_agent

        rescued_result = None
        rescued_push = False
        pushed = False
        commit_hash = None

        try:
            rescued_result, rescued_push = rescue_git_progress(
                cfg,
                final_result,
                worktree,
                branch,
                task_id,
                allow_push,
                logfile,
                QUEUE_SUMMARY_LOG,
            )
            if rescued_result is not None:
                final_result = rescued_result

            pushed = False if rescued_result is not None else commit_and_push(worktree, branch, task_id, allow_push, logfile, QUEUE_SUMMARY_LOG)

            if pushed or rescued_push:
                commit_hash = run(["git", "rev-parse", "HEAD"], cwd=worktree, logfile=logfile, queue_summary_log=QUEUE_SUMMARY_LOG).stdout.strip()
                log(f"Final commit hash: {commit_hash}", logfile, also_summary=True, queue_summary_log=QUEUE_SUMMARY_LOG)
            else:
                log("Task completed but nothing changed, so no commit/push happened.", logfile, also_summary=True, queue_summary_log=QUEUE_SUMMARY_LOG)
                if final_result is not None:
                    # If the branch already has commits ahead of the REPO
                    # default branch, a prior run delivered the work and this
                    # run correctly did nothing. Don't downgrade. Using the
                    # repo default (not meta["base_branch"]) because follow-ups
                    # often inherit base_branch = <agent branch>, which would
                    # make this check trivially compare HEAD against itself.
                    repo_default = detect_default_branch(repo) or base_branch
                    branch_has_prior_work = False
                    try:
                        ahead = run(
                            ["git", "rev-list", "--count", f"origin/{repo_default}..HEAD"],
                            cwd=worktree, logfile=logfile, queue_summary_log=QUEUE_SUMMARY_LOG,
                            check=False,
                        )
                        branch_has_prior_work = int((ahead.stdout or "0").strip() or "0") > 0
                    except Exception:
                        branch_has_prior_work = False

                    if branch_has_prior_work and final_result.get("status") == "complete":
                        if not commit_hash:
                            commit_hash = run(
                                ["git", "rev-parse", "HEAD"],
                                cwd=worktree,
                                logfile=logfile,
                                queue_summary_log=QUEUE_SUMMARY_LOG,
                            ).stdout.strip()
                        log(
                            f"Branch {branch} already has commits ahead of {repo_default}; treating no-diff complete as legitimate (prior-run work).",
                            logfile,
                            also_summary=True,
                            queue_summary_log=QUEUE_SUMMARY_LOG,
                        )
                    else:
                        downgraded = downgrade_no_diff_complete(meta, final_result, final_agent)
                        if downgraded is not final_result:
                            final_result = downgraded
                            log(
                                f"Downgraded STATUS: complete → partial (no_diff_produced) for {meta.get('task_type')} task; agent did not write any files.",
                                logfile,
                                also_summary=True,
                                queue_summary_log=QUEUE_SUMMARY_LOG,
                            )

            if final_result is not None:
                web_downgraded = downgrade_web_no_artifact(meta, body, final_result, final_agent, worktree)
                if web_downgraded is not final_result:
                    final_result = web_downgraded
                    log(
                        "Downgraded STATUS: complete → partial (no_web_artifact); "
                        "homepage task shipped without index.html at repo root.",
                        logfile,
                        also_summary=True,
                        queue_summary_log=QUEUE_SUMMARY_LOG,
                    )
        except WorkflowValidationError as e:
            push_validation_error = str(e)
            final_result = {
                "status": "blocked",
                "blocker_code": "workflow_validation_failed",
                "summary": push_validation_error,
                "done": list((final_result or {}).get("done", ["- Agent work completed locally."])),
                "blockers": [f"- {push_validation_error}"],
                "next_step": "Update the workflow file to satisfy local validation, then rerun the task.",
                "files_changed": list((final_result or {}).get("files_changed", ["- Inspect workflow diff in the worktree."])),
                "tests_run": list((final_result or {}).get("tests_run", ["- None"])),
                "decisions": list((final_result or {}).get("decisions", ["- None"])) + ["- Queue blocked push because a modified GitHub Actions workflow failed local validation."],
                "risks": list((final_result or {}).get("risks", ["- None"])) + ["- Pushing this branch would strand the PR without the required CI status."],
                "attempted_approaches": list((final_result or {}).get("attempted_approaches", ["- None"])) + ["- Queue validated modified workflow files before push and rejected the invalid configuration."],
                "manual_steps": "",
                "unblock_notes": {
                    "blocking_cause": push_validation_error,
                    "next_action": "Update the workflow file to satisfy local validation, then rerun the task.",
                },
                "raw": (final_result or {}).get("raw", ""),
            }
            log(push_validation_error, logfile, also_summary=True, queue_summary_log=QUEUE_SUMMARY_LOG)

        if final_result is None:
            final_result = synthesize_exhausted_result(model_attempts)

        verified_result = verify_pr_ci_debug_completion(
            meta,
            body,
            final_result,
            commit_hash=commit_hash,
            task_started_at=start_time,
        )
        if verified_result is not final_result:
            final_result = verified_result
            _write_result_contract(worktree, final_result)
            log(
                f"CI remediation completion gate downgraded task to {final_result['status']} ({final_result.get('ci_rerun_reason', 'verification_required')}).",
                logfile,
                also_summary=True,
                queue_summary_log=QUEUE_SUMMARY_LOG,
            )

        try:
            record_metrics(cfg, meta, final_result, final_agent, model_attempts, start_time, logfile, QUEUE_SUMMARY_LOG)
        except Exception as e:
            log(f"Metrics recording warning: {e}", logfile, queue_summary_log=QUEUE_SUMMARY_LOG)

        # Write machine-readable unblock notes artifact for partial/blocked outcomes.
        if final_result.get("unblock_notes"):
            try:
                artifact_path = write_unblock_notes_artifact(task_id, final_result["unblock_notes"], final_result)
                if artifact_path:
                    log(f"Wrote unblock notes artifact: {artifact_path}", logfile, queue_summary_log=QUEUE_SUMMARY_LOG)
            except Exception as e:
                log(f"Unblock notes artifact warning: {e}", logfile, queue_summary_log=QUEUE_SUMMARY_LOG)

        # Sync back to GitHub if this task originated from an issue.
        sync_info = {}
        try:
            sync_info = sync_result(meta, final_result, commit_hash) or {}
        except Exception as e:
            log(f"GitHub sync warning: {e}", logfile, queue_summary_log=QUEUE_SUMMARY_LOG)

        recovery_rerun = None
        if not dispatcher_only_mode:
            recovery_rerun = maybe_requeue_prompt_inspection_recovery(
                paths,
                meta,
                body,
                final_result,
                logfile,
                QUEUE_SUMMARY_LOG,
            )

        # Update CODEBASE.md memory on the main repo branch after completion.
        if final_result["status"] == "complete":
            try:
                update_codebase_memory(repo, task_id, final_result, meta)
            except Exception as e:
                log(f"CODEBASE.md update warning: {e}", logfile, queue_summary_log=QUEUE_SUMMARY_LOG)

        if final_result["status"] == "complete":
            log("Final queue state: done", logfile, also_summary=True, queue_summary_log=QUEUE_SUMMARY_LOG)
            move_processing_task(processing, DONE, logfile, QUEUE_SUMMARY_LOG, state_label="done")
            send_telegram(
                cfg,
                f"✅ Complete\nTask: {task_id}\nRepo: {repo.name}\nBranch: {branch}\nModel: {final_agent}\nCommit: {commit_hash or 'none'}\nSummary: {final_result['summary']}"
                + (f"\nRecovery rerun: {recovery_rerun.name}" if recovery_rerun else ""),
                logfile,
                QUEUE_SUMMARY_LOG,
            )
            manual_steps = final_result.get("manual_steps", "").strip()
            if manual_steps and manual_steps.lower() not in ("- none", "none", ""):
                send_telegram(
                    cfg,
                    f"🔧 Manual action required\nTask: {task_id}\nRepo: {repo.name}\n\n{manual_steps}",
                    logfile,
                    QUEUE_SUMMARY_LOG,
                )
        elif final_result["status"] in ("partial", "blocked"):
            # Suppress per-task Partial/Blocked telegrams when the cause is
            # fallback_exhausted — that case already emits the consolidated
            # "🧊 Fallback cooldown" telegram (debounced once per cooldown
            # window). Sending both per-task spams the channel.
            suppress_partial_telegram = final_result.get("blocker_code") == "fallback_exhausted"
            github_followup_url = sync_info.get("followup_issue_url")
            if github_followup_url:
                log("Final queue state: blocked", logfile, also_summary=True, queue_summary_log=QUEUE_SUMMARY_LOG)
                move_processing_task(processing, BLOCKED, logfile, QUEUE_SUMMARY_LOG, state_label="blocked")
                if not suppress_partial_telegram:
                    event = _queue_incident_event(
                        "task_blocked",
                        meta,
                        final_result,
                        repo_name=repo.name,
                        branch=branch,
                        model_attempts=model_attempts,
                        extra_line=f"Follow-up issue: {github_followup_url}",
                    )
                    severity = classify_severity(cfg, "queue", event)
                    route_incident(severity, event, cfg=cfg, logfile=logfile, queue_summary_log=QUEUE_SUMMARY_LOG)
            elif dispatcher_only_mode:
                log(
                    "Dispatcher-only repo: skipping automated follow-up/escalation for partial/blocked outcome.",
                    logfile,
                    also_summary=True,
                    queue_summary_log=QUEUE_SUMMARY_LOG,
                )
                log("Final queue state: blocked", logfile, also_summary=True, queue_summary_log=QUEUE_SUMMARY_LOG)
                move_processing_task(processing, BLOCKED, logfile, QUEUE_SUMMARY_LOG, state_label="blocked")
                if not suppress_partial_telegram:
                    event = _queue_incident_event(
                        "task_blocked",
                        meta,
                        final_result,
                        repo_name=repo.name,
                        branch=branch,
                        model_attempts=model_attempts,
                        extra_line="Automation: dispatcher_only (manual requeue required)",
                    )
                    severity = classify_severity(cfg, "queue", event)
                    route_incident(severity, event, cfg=cfg, logfile=logfile, queue_summary_log=QUEUE_SUMMARY_LOG)
            else:
                followup = create_followup_task(
                    meta,
                    body,
                    final_result,
                    logfile,
                    cfg["default_max_attempts"],
                    model_attempts,
                    INBOX,
                    QUEUE_SUMMARY_LOG,
                )
                if followup is not None:
                    log("Final queue state: blocked", logfile, also_summary=True, queue_summary_log=QUEUE_SUMMARY_LOG)
                    move_processing_task(processing, BLOCKED, logfile, QUEUE_SUMMARY_LOG, state_label="blocked")
                    if not suppress_partial_telegram:
                        event = _queue_incident_event(
                            "task_blocked",
                            meta,
                            final_result,
                            repo_name=repo.name,
                            branch=branch,
                            model_attempts=model_attempts,
                            extra_line=f"Retry queued: {followup.name}",
                        )
                        severity = classify_severity(cfg, "queue", event)
                        route_incident(severity, event, cfg=cfg, logfile=logfile, queue_summary_log=QUEUE_SUMMARY_LOG)
                else:
                    esc = create_escalation_note(meta, body, final_result, logfile, model_attempts, ESCALATED, QUEUE_SUMMARY_LOG)
                    log("No follow-up created. Final queue state: escalated", logfile, also_summary=True, queue_summary_log=QUEUE_SUMMARY_LOG)
                    move_processing_task(processing, ESCALATED, logfile, QUEUE_SUMMARY_LOG, state_label="escalated")
                    chat_id = str(cfg.get("telegram_chat_id", "")).strip()
                    action = None
                    reply_markup = None
                    if chat_id and meta.get("github_repo") and meta.get("github_issue_number") and meta.get("github_project_key"):
                        action = create_escalation_action(meta, final_result, esc, chat_id)
                        save_telegram_action(paths["TELEGRAM_ACTIONS"], action)
                        reply_markup = escalation_reply_markup(action["action_id"])
                    event = _queue_incident_event(
                        "task_escalated",
                        meta,
                        final_result,
                        repo_name=repo.name,
                        branch=branch,
                        model_attempts=model_attempts,
                        note_path=esc,
                        reply_markup=reply_markup,
                    )
                    severity = classify_severity(cfg, "queue", event)
                    incident = route_incident(
                        severity,
                        event,
                        cfg=cfg,
                        logfile=logfile,
                        queue_summary_log=QUEUE_SUMMARY_LOG,
                    )
                    if action is not None:
                        action["message_id"] = incident.get("message_id")
                        save_telegram_action(paths["TELEGRAM_ACTIONS"], action)
        else:
            log("Final queue state: failed", logfile, also_summary=True, queue_summary_log=QUEUE_SUMMARY_LOG)
            move_processing_task(processing, FAILED, logfile, QUEUE_SUMMARY_LOG, state_label="failed")
            event = _queue_incident_event(
                "task_failed",
                meta,
                final_result,
                repo_name=repo.name,
                branch=branch,
                model_attempts=model_attempts,
            )
            severity = classify_severity(cfg, "queue", event)
            route_incident(severity, event, cfg=cfg, logfile=logfile, queue_summary_log=QUEUE_SUMMARY_LOG)

    except Exception as e:
        log(f"ERROR: {e}", logfile, also_summary=True, queue_summary_log=QUEUE_SUMMARY_LOG)
        log(traceback.format_exc(), logfile, queue_summary_log=QUEUE_SUMMARY_LOG)

        # Infrastructure failures (git lock, network, worktree setup) should auto-retry
        # by returning the task to inbox — not moving it to the graveyard.
        infra_attempt = int(meta.get("infra_retry", 0)) + 1
        max_infra_retries = 3

        if infra_attempt <= max_infra_retries and processing.exists():
            # Patch the task file with updated retry counter before returning to inbox
            try:
                task_content = processing.read_text(encoding="utf-8")
                if "infra_retry:" in task_content:
                    task_content = re.sub(r"infra_retry:\s*\d+", f"infra_retry: {infra_attempt}", task_content)
                else:
                    # Insert after the first --- line (frontmatter)
                    task_content = task_content.replace("\n---\n", f"\ninfra_retry: {infra_attempt}\n---\n", 1)
                processing.write_text(task_content, encoding="utf-8")
            except Exception as patch_err:
                log(f"Warning: could not patch infra_retry counter: {patch_err}", logfile, queue_summary_log=QUEUE_SUMMARY_LOG)

            shutil.move(str(processing), str(INBOX / processing.name))
            log(
                f"Infrastructure failure (attempt {infra_attempt}/{max_infra_retries}). "
                f"Task returned to inbox for automatic retry.",
                logfile, also_summary=True, queue_summary_log=QUEUE_SUMMARY_LOG,
            )
            send_telegram(
                cfg,
                f"🔄 Infra retry ({infra_attempt}/{max_infra_retries})\nTask: {task_id}\nError: {e}\nReturned to inbox — will retry next cycle.",
                logfile, QUEUE_SUMMARY_LOG,
            )
        else:
            # Exhausted infra retries — escalate to failed
            log("Final queue state: failed (infra retries exhausted)", logfile, also_summary=True, queue_summary_log=QUEUE_SUMMARY_LOG)

            failure_result = {
                "status": "blocked",
                "blocker_code": "environment_failure",
                "summary": f"Queue execution failed after {max_infra_retries} infrastructure retries: {e}",
                "done": ["- Task was dispatched and execution started."],
                "blockers": [f"- Hard execution failure: {e}"],
                "next_step": "Inspect the task log and agent runner configuration, then retry.",
                "files_changed": ["- Unknown"],
                "tests_run": ["- None"],
                "decisions": ["- Execution aborted on runner error"],
                "risks": ["- GitHub issue may remain in-progress without sync unless updated here"],
                "attempted_approaches": [f"- Queue attempted {max_infra_retries} infrastructure retries"],
                "unblock_notes": {
                    "blocking_cause": f"Infrastructure failure after {max_infra_retries} retries: {e}",
                    "next_action": "Inspect the task log and agent runner configuration, then retry.",
                },
                "raw": "",
            }

            try:
                sync_result(meta, failure_result, None)
            except Exception as sync_err:
                log(f"GitHub sync warning during exception handling: {sync_err}", logfile, queue_summary_log=QUEUE_SUMMARY_LOG)

            if processing.exists():
                shutil.move(str(processing), str(FAILED / processing.name))

            event = _queue_incident_event(
                "task_failed",
                meta,
                failure_result,
                repo_name=repo.name if repo is not None else "unknown",
                branch=branch if "branch" in locals() else "unknown",
                model_attempts=model_attempts if "model_attempts" in locals() else [],
            )
            severity = classify_severity(cfg, "queue", event)
            route_incident(severity, event, cfg=cfg, logfile=logfile, queue_summary_log=QUEUE_SUMMARY_LOG)
    finally:
        _clear_processing_lock(processing)
        if repo is not None and worktree is not None:
            cleanup_worktree(repo, worktree, logfile, QUEUE_SUMMARY_LOG)
        if repo_lock_fh is not None:
            try:
                fcntl.flock(repo_lock_fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            repo_lock_fh.close()

if __name__ == "__main__":
    main()
