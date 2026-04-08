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

import yaml

from orchestrator.paths import load_config, runtime_paths
from orchestrator.github_sync import sync_result
from orchestrator.gh_project import gh_json as _gh_json
from orchestrator.codebase_memory import read_codebase_context, update_codebase_memory
from orchestrator.gh_project import add_issue_comment, gh, gh_json, query_project, set_item_status
from orchestrator.repo_context import build_execution_context
from orchestrator.repo_modes import is_dispatcher_only_repo
from orchestrator.agent_scorer import filter_healthy_agents


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
}
VALID_BLOCKER_CODES = set(BLOCKER_CODE_DESCRIPTIONS)
PROMPT_INSPECTION_BLOCKER_CODES = {"invalid_result_contract"}
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
    if any(token in lowered for token in ["rate limit", "rate-limit", "too many requests", "429", "usage limit", "quota"]):
        return "usage limit / rate limit"
    if any(token in lowered for token in ["authentication", "unauthorized", "forbidden", "invalid api key", "not authenticated", "login required"]):
        return "authentication failure"
    if any(token in lowered for token in ["command not found", "no such file or directory", "unknown option", "unrecognized option"]):
        return "runner/cli configuration failure"
    return None


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
    if classification == "usage limit / rate limit":
        return "quota_limited"
    if classification == "authentication failure":
        return "missing_credentials"
    if classification == "runner/cli configuration failure":
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
    candidate_runs: list[dict] = []
    for run in runs:
        if str(run.get("head_branch", "")).strip() != branch:
            continue
        if commit_hash and str(run.get("head_sha", "")).strip() != commit_hash:
            continue
        created_at = _parse_github_timestamp(run.get("created_at")) or _parse_github_timestamp(run.get("run_started_at"))
        if created_at and created_at < task_started_at:
            continue
        candidate_runs.append(run)

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
) -> int | None:
    chat_id = str(cfg.get("telegram_chat_id", "")).strip()
    if not chat_id:
        return None
    payload: dict[str, object] = {"chat_id": chat_id, "text": text}
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
    m = re.fullmatch(r"(esc|plan):([a-f0-9]{12}):(requeue|retry|close|skip|approve|reject)", callback_data or "")
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
        return {"text": "This escalation action expired after 48 hours.", "show_alert": True, "remove_keyboard": True}

    if action_type == "plan":
        if operation not in {"approve", "reject"}:
            return {"text": "Unknown planner action.", "show_alert": True, "remove_keyboard": True}
        action["status"] = "done"
        action["handled_action"] = operation
        action["handled_at"] = datetime.now(timezone.utc).isoformat()
        action["approval"] = "approved" if operation == "approve" else "rejected"
        action["result_text"] = (
            f"Approved sprint plan for {action.get('repo', 'repo')}."
            if operation == "approve"
            else f"Skipped sprint plan for {action.get('repo', 'repo')}."
        )
        save_telegram_action(actions_dir, action)
        return {"text": action["result_text"], "show_alert": False, "remove_keyboard": True}

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
    return {"text": result_text, "show_alert": False, "remove_keyboard": True}


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


def ensure_worktree(cfg: dict, repo: Path, base_branch: str, branch: str, task_id: str, logfile: Path, queue_summary_log: Path):
    worktree = Path(cfg["worktrees_dir"]) / repo.name / task_id
    worktree.parent.mkdir(parents=True, exist_ok=True)

    if worktree.exists():
        shutil.rmtree(worktree, ignore_errors=True)

    _git_fetch_with_retry(repo, logfile, queue_summary_log)
    run(
        ["git", "-C", repo, "worktree", "add", "-B", branch, worktree, f"origin/{base_branch}"],
        logfile=logfile,
        queue_summary_log=queue_summary_log,
    )
    return worktree


def render_prior_attempt_history(prior_results: list[dict]) -> str:
    if not prior_results:
        return "None"

    chunks = []
    for idx, r in enumerate(prior_results, start=1):
        chunks.append(
            f"""Attempt {idx}
MODEL: {r.get("agent", "unknown")}
STATUS: {r.get("status", "unknown")}
BLOCKER_CODE: {r.get("blocker_code", "none") or "none"}
SUMMARY: {r.get("summary", "No summary")}
BLOCKERS:
{chr(10).join(r.get("blockers", ["- None"]))}
ATTEMPTED_APPROACHES:
{chr(10).join(r.get("attempted_approaches", ["- None"]))}
"""
        )
    return "\n".join(chunks)


def write_prompt(task_id: str, meta: dict, body: str, current_agent: str, prior_results: list[dict], root: Path, worktree: Path | None = None):
    prompt_file = root / "runtime" / "tmp" / f"{task_id}.txt"
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path = Path(meta.get("prompt_snapshot_path") or (root / "runtime" / "prompts" / f"{task_id}.txt"))
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)

    codebase_context = read_codebase_context(worktree) if worktree else ""
    layered_context = build_execution_context(
        worktree or root,
        meta.get("task_type", "implementation"),
        body,
    ) if worktree else ""

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
Prior model attempts in this task lineage:
{render_prior_attempt_history(prior_results)}

You must create or overwrite a file named .agent_result.md in the repository root before exiting.

Use EXACTLY this format:

STATUS: complete|partial|blocked

BLOCKER_CODE:
One line. Required when STATUS is partial or blocked. Use `none` when STATUS is complete.

SUMMARY:
One short paragraph.

DONE:
- bullet
- bullet

BLOCKERS:
- bullet
- bullet

NEXT_STEP:
One short paragraph. If complete, write: None

FILES_CHANGED:
- path
- path

TESTS_RUN:
- command + result
- command + result

DECISIONS:
- bullet
- bullet

RISKS:
- bullet
- bullet

ATTEMPTED_APPROACHES:
- bullet
- bullet

MANUAL_STEPS:
- None

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
- Read the prior model attempts above and avoid repeating clearly failed approaches unless you have a specific new reason
- In MANUAL_STEPS, list every action the human operator must take to activate this feature.
  This includes: crontab entries, config.yaml changes, environment variables, server commands, package installs.
  Format cron entries as ready-to-paste crontab lines with a comment.
  Format config.yaml additions as indented YAML snippets.
  Write exactly "- None" if no manual action is required.
  This section is CRITICAL — the operator depends on it to know what to do after deployment.
"""
    prompt_file.write_text(prompt, encoding="utf-8")
    snapshot_path.write_text(prompt, encoding="utf-8")
    return prompt_file


def run_agent(agent: str, worktree: Path, prompt_file: Path, logfile: Path, timeout_minutes: int, root: Path, queue_summary_log: Path):
    runner = root / "bin" / "agent_runner.sh"
    timeout_seconds = max(60, int(timeout_minutes) * 60)
    run([runner, agent, worktree, prompt_file], logfile=logfile, timeout=timeout_seconds, queue_summary_log=queue_summary_log)


def has_changes(worktree: Path):
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=worktree,
        capture_output=True,
        text=True,
    )
    return bool(result.stdout.strip())


def has_unpushed_commits(worktree: Path, branch: str) -> bool:
    """Return True if local branch has commits not yet on origin."""
    # Check whether remote branch exists at all
    remote_check = subprocess.run(
        ["git", "ls-remote", "--heads", "origin", branch],
        cwd=worktree, capture_output=True, text=True,
    )
    if not remote_check.stdout.strip():
        # Remote branch doesn't exist — any local commit counts as unpushed
        has_any = subprocess.run(
            ["git", "log", "--oneline", "-1"],
            cwd=worktree, capture_output=True, text=True,
        )
        return bool(has_any.stdout.strip())
    # Remote exists — check for commits ahead of origin
    ahead = subprocess.run(
        ["git", "log", f"origin/{branch}..HEAD", "--oneline"],
        cwd=worktree, capture_output=True, text=True,
    )
    return bool(ahead.stdout.strip())


def commit_and_push(worktree: Path, branch: str, task_id: str, allow_push: bool, logfile: Path, queue_summary_log: Path):
    uncommitted = has_changes(worktree)
    unpushed = has_unpushed_commits(worktree, branch)

    if not uncommitted and not unpushed:
        log("No file changes detected. Skipping commit/push.", logfile, queue_summary_log=queue_summary_log)
        return False

    _validate_workflow_files(worktree)

    if uncommitted:
        run(["git", "add", "-A"], cwd=worktree, logfile=logfile, queue_summary_log=queue_summary_log)
        run(["git", "commit", "-m", f"agent {task_id}"], cwd=worktree, logfile=logfile, queue_summary_log=queue_summary_log)
    else:
        log("Agent already committed changes; pushing unpushed commits.", logfile, queue_summary_log=queue_summary_log)

    if allow_push:
        run(["git", "push", "-u", "origin", branch], cwd=worktree, logfile=logfile, queue_summary_log=queue_summary_log)
    else:
        log("allow_push=false, skipping git push.", logfile, queue_summary_log=queue_summary_log)

    return True


def should_attempt_git_rescue(result: dict, worktree: Path, branch: str) -> bool:
    if not result:
        return False
    if result.get("status") not in ("partial", "blocked"):
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
    rescued["status"] = "complete"
    summary = result.get("summary", "Recovered worktree changes.")
    rescued["summary"] = f"{summary} Orchestrator rescued and pushed the worktree changes."
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

    filtered = []
    for agent in chain:
        if agent not in VALID_FALLBACK_AGENTS:
            continue
        available, _reason = agent_available(agent)
        if available:
            filtered.append(agent)
    metrics_file = Path(cfg.get("root_dir", ".")).expanduser() / "runtime" / "metrics" / "agent_stats.jsonl"
    healthy, _skipped = filter_healthy_agents(filtered, metrics_file)
    return healthy


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
    if status == "blocked":
        return True
    return False


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

    slug = sanitize_slug(next_step)
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
        "model_attempts": model_attempts,
        "github_repo": original_meta.get("github_repo"),
        "github_issue_number": original_meta.get("github_issue_number"),
        "github_issue_url": original_meta.get("github_issue_url"),
        "prompt_snapshot_path": str(Path(original_meta.get("prompt_snapshot_path", inbox.parent.parent / "prompts" / f"{new_task_id}.txt")).parent / f"{new_task_id}.txt"),
    }
    # Propagate structured failed_checks so CI verification survives follow-up reformatting.
    if original_meta.get("failed_checks"):
        frontmatter["failed_checks"] = original_meta["failed_checks"]

    frontmatter_text = yaml.safe_dump(frontmatter, sort_keys=False).strip()

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

    content = f"""# Escalation Note

## Parent Task ID
{parent_task_id}

## Branch
{original_meta.get("branch", "unknown")}

## Repo
{original_meta.get("repo", "unknown")}

## Task Type
{original_meta.get("task_type", "unknown")}

## Models Tried
{", ".join(model_attempts) if model_attempts else "None"}

## Final Status
{result.get("status", "blocked")}

## Blocker Code
{result.get("blocker_code", "none") or "none"}

## Summary
{result.get("summary", "No summary provided.")}

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
    """Atomically append task metrics to runtime/metrics/agent_stats.jsonl."""
    metrics_dir = Path(cfg.get("root_dir", ".")).expanduser() / "runtime" / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    metrics_file = metrics_dir / "agent_stats.jsonl"

    duration = (datetime.now() - start_time).total_seconds()
    record = {
        "timestamp": datetime.now().isoformat(),
        "task_id": meta.get("task_id", "unknown"),
        "repo": str(meta.get("repo", "unknown")),
        "agent": final_agent or "unknown",
        "status": final_result.get("status", "unknown"),
        "blocker_code": final_result.get("blocker_code", ""),
        "attempt_count": len(model_attempts),
        "duration_seconds": round(duration, 1),
        "task_type": meta.get("task_type", "unknown"),
    }
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

    task = pick_task(INBOX, cfg)
    if not task:
        print(f"[{worker_id}] No tasks in inbox.")
        return

    processing = PROCESSING / task.name
    try:
        shutil.move(str(task), str(processing))
    except FileNotFoundError:
        print(f"[{worker_id}] Task picked by another worker. Exiting.")
        return

    task_id = processing.stem
    logfile = LOGS / f"{task_id}.log"
    worktree = None
    repo = None
    repo_lock_fh = None

    try:
        start_time = datetime.now()
        meta, body = parse_task(processing)

        task_id = meta["task_id"]
        logfile = LOGS / f"{task_id}.log"

        repo = Path(meta["repo"])
        base_branch = meta.get("base_branch", cfg["default_base_branch"])
        branch = meta.get("branch", f"agent/{task_id}")
        allow_push = bool(meta.get("allow_push", cfg["default_allow_push"]))
        task_type = meta.get("task_type", cfg["default_task_type"])
        model_attempts = list(meta.get("model_attempts", []))
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
                break

            timeout_minutes = timeout_for_agent(current_agent, meta, cfg)
            meta_for_prompt = dict(meta)
            meta_for_prompt["resolved_agent"] = current_agent
            meta_for_prompt["model_attempts"] = model_attempts
            prompt_file = write_prompt(task_id, meta_for_prompt, body, current_agent, prior_results, ROOT, worktree=worktree)

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
                prior_results.append(timeout_result)
                log(f"{current_agent} timed out.", logfile, also_summary=True, queue_summary_log=QUEUE_SUMMARY_LOG)

                if get_next_agent(meta, cfg, model_attempts) is None:
                    final_result = timeout_result
                    final_agent = current_agent
                    break
                continue

            except Exception as e:
                failure_summary, failure_blockers, failure_detail = _format_runner_failure(e)
                runner_result = {
                    "agent": current_agent,
                    "status": "blocked",
                    "blocker_code": _blocker_code_from_runner_failure(failure_summary, failure_detail),
                    "summary": f"{current_agent} failed before producing a valid result file. {failure_summary}",
                    "done": ["- Agent runner was invoked."],
                    "blockers": failure_blockers,
                    "next_step": "Try the next fallback model. If all models fail, inspect runner config, credentials, or quotas.",
                    "files_changed": ["- Unknown / inspect worktree"],
                    "tests_run": ["- None"],
                    "decisions": ["- Treat runner failure as model-level failure and continue fallback chain if possible."],
                    "risks": ["- Model quota/auth/CLI issues may affect multiple tasks."],
                    "attempted_approaches": [f"- Attempted model: {current_agent}", f"- Failure detail: {failure_summary}"],
                    "unblock_notes": {
                        "blocking_cause": f"{current_agent} runner failure: {failure_summary}",
                        "next_action": "Try the next fallback model. If all models fail, inspect runner config, credentials, or quotas.",
                    },
                    "raw": "",
                }
                model_attempts.append(current_agent)
                prior_results.append(runner_result)
                log(f"{current_agent} runner failure: {e}", logfile, also_summary=True, queue_summary_log=QUEUE_SUMMARY_LOG)
                for blocker in failure_blockers:
                    log(blocker, logfile, queue_summary_log=QUEUE_SUMMARY_LOG)

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
            result["agent"] = current_agent
            model_attempts.append(current_agent)
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
            shutil.move(str(processing), str(DONE / processing.name))
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
            github_followup_url = sync_info.get("followup_issue_url")
            if github_followup_url:
                log("Final queue state: blocked", logfile, also_summary=True, queue_summary_log=QUEUE_SUMMARY_LOG)
                shutil.move(str(processing), str(BLOCKED / processing.name))
                send_telegram(
                    cfg,
                        f"⏸️ Partial/Blocked\nTask: {task_id}\nRepo: {repo.name}\nBranch: {branch}\nLast model: {final_agent}\nModels tried: {', '.join(model_attempts)}\nNext: {final_result['next_step']}\nFollow-up: {github_followup_url}",
                        logfile,
                        QUEUE_SUMMARY_LOG,
                    )
            elif dispatcher_only_mode:
                log(
                    "Dispatcher-only repo: skipping automated follow-up/escalation for partial/blocked outcome.",
                    logfile,
                    also_summary=True,
                    queue_summary_log=QUEUE_SUMMARY_LOG,
                )
                log("Final queue state: blocked", logfile, also_summary=True, queue_summary_log=QUEUE_SUMMARY_LOG)
                shutil.move(str(processing), str(BLOCKED / processing.name))
                send_telegram(
                    cfg,
                    f"⏸️ Partial/Blocked\nTask: {task_id}\nRepo: {repo.name}\nBranch: {branch}\nLast model: {final_agent}\nModels tried: {', '.join(model_attempts)}\nNext: {final_result['next_step']}\nAutomation: dispatcher_only (manual requeue required)",
                    logfile,
                    QUEUE_SUMMARY_LOG,
                )
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
                    shutil.move(str(processing), str(BLOCKED / processing.name))
                    send_telegram(
                        cfg,
                        f"⏸️ Partial/Blocked\nTask: {task_id}\nRepo: {repo.name}\nBranch: {branch}\nLast model: {final_agent}\nModels tried: {', '.join(model_attempts)}\nNext: {final_result['next_step']}\nFollow-up: {followup.name}",
                        logfile,
                        QUEUE_SUMMARY_LOG,
                    )
                else:
                    esc = create_escalation_note(meta, body, final_result, logfile, model_attempts, ESCALATED, QUEUE_SUMMARY_LOG)
                    log("No follow-up created. Final queue state: escalated", logfile, also_summary=True, queue_summary_log=QUEUE_SUMMARY_LOG)
                    shutil.move(str(processing), str(ESCALATED / processing.name))
                    chat_id = str(cfg.get("telegram_chat_id", "")).strip()
                    action = None
                    reply_markup = None
                    if chat_id and meta.get("github_repo") and meta.get("github_issue_number") and meta.get("github_project_key"):
                        action = create_escalation_action(meta, final_result, esc, chat_id)
                        save_telegram_action(paths["TELEGRAM_ACTIONS"], action)
                        reply_markup = escalation_reply_markup(action["action_id"])
                    message_id = send_telegram(
                        cfg,
                        build_escalation_message(meta, final_result, esc),
                        logfile,
                        QUEUE_SUMMARY_LOG,
                        reply_markup=reply_markup,
                    )
                    if action is not None:
                        action["message_id"] = message_id
                        save_telegram_action(paths["TELEGRAM_ACTIONS"], action)
        else:
            log("Final queue state: failed", logfile, also_summary=True, queue_summary_log=QUEUE_SUMMARY_LOG)
            shutil.move(str(processing), str(FAILED / processing.name))
            send_telegram(
                cfg,
                f"❌ Failed\nTask: {task_id}\nRepo: {repo.name}\nBranch: {branch}\nModels tried: {', '.join(model_attempts)}",
                logfile,
                QUEUE_SUMMARY_LOG,
            )

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

            send_telegram(cfg, f"❌ Failed (infra)\nTask: {task_id}\nError: {e}\nRetries exhausted.", logfile, QUEUE_SUMMARY_LOG)
    finally:
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
