import re
import shutil
import subprocess
import traceback
from datetime import datetime
from pathlib import Path

import yaml

from orchestrator.paths import load_config, runtime_paths
from orchestrator.github_sync import sync_result


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
        raise RuntimeError(f"Command failed ({result.returncode}): {cmd_str}")
    return result


def send_telegram(cfg: dict, text: str, logfile: Path | None = None, queue_summary_log: Path | None = None):
    token = str(cfg.get("telegram_bot_token", "")).strip()
    chat_id = str(cfg.get("telegram_chat_id", "")).strip()
    if not token or not chat_id:
        return

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        result = subprocess.run(
            [
                "curl",
                "-sS",
                "-X",
                "POST",
                url,
                "-d",
                f"chat_id={chat_id}",
                "--data-urlencode",
                f"text={text}",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode != 0:
            log(f"Telegram send failed: {result.stderr}", logfile, queue_summary_log=queue_summary_log)
    except Exception as e:
        log(f"Telegram send exception: {e}", logfile, queue_summary_log=queue_summary_log)


def pick_task(inbox: Path):
    tasks = sorted(inbox.glob("*.md"))
    return tasks[0] if tasks else None


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


def split_section(text: str, start_label: str, end_labels: list[str]):
    if end_labels:
        pattern = rf"^{re.escape(start_label)}:\s*(.*?)(?=^(?:{'|'.join(map(re.escape, end_labels))}):|\Z)"
    else:
        pattern = rf"^{re.escape(start_label)}:\s*(.*)$"
    m = re.search(pattern, text, flags=re.MULTILINE | re.DOTALL)
    return m.group(1).strip() if m else ""


def parse_bullets(section_text: str):
    lines = [x.strip() for x in section_text.splitlines() if x.strip()]
    return lines or ["- None"]


def ensure_worktree(cfg: dict, repo: Path, base_branch: str, branch: str, task_id: str, logfile: Path, queue_summary_log: Path):
    worktree = Path(cfg["worktrees_dir"]) / repo.name / task_id
    worktree.parent.mkdir(parents=True, exist_ok=True)

    if worktree.exists():
        shutil.rmtree(worktree, ignore_errors=True)

    run(["git", "-C", repo, "fetch", "origin"], logfile=logfile, queue_summary_log=queue_summary_log)
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
SUMMARY: {r.get("summary", "No summary")}
BLOCKERS:
{chr(10).join(r.get("blockers", ["- None"]))}
ATTEMPTED_APPROACHES:
{chr(10).join(r.get("attempted_approaches", ["- None"]))}
"""
        )
    return "\n".join(chunks)


def write_prompt(task_id: str, meta: dict, body: str, current_agent: str, prior_results: list[dict], root: Path):
    prompt_file = root / "runtime" / "tmp" / f"{task_id}.txt"
    prompt_file.parent.mkdir(parents=True, exist_ok=True)

    prompt = f"""You are a coding worker running in a controlled automation environment.

You must work only inside the current repository.

Current agent:
{current_agent}

Task metadata:
{yaml.safe_dump(meta, sort_keys=False)}

Task instructions:
{body}

Prior model attempts in this task lineage:
{render_prior_attempt_history(prior_results)}

You must create or overwrite a file named .agent_result.md in the repository root before exiting.

Use EXACTLY this format:

STATUS: complete|partial|blocked

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

Rules:
- Prefer the smallest viable diff.
- Do not modify unrelated files.
- Do not touch secrets unless explicitly asked.
- If you complete the task, set STATUS: complete
- If you made progress but more work remains, set STATUS: partial
- If blocked by missing context, missing credentials, broken environment, or ambiguity, set STATUS: blocked
- Always write .agent_result.md even if no code changes were made
- In ATTEMPTED_APPROACHES, describe what you tried this run so future runs do not repeat the same failed path
- Read the prior model attempts above and avoid repeating clearly failed approaches unless you have a specific new reason
"""
    prompt_file.write_text(prompt, encoding="utf-8")
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


def commit_and_push(worktree: Path, branch: str, task_id: str, allow_push: bool, logfile: Path, queue_summary_log: Path):
    if not has_changes(worktree):
        log("No file changes detected. Skipping commit/push.", logfile, queue_summary_log=queue_summary_log)
        return False

    run(["git", "add", "-A"], cwd=worktree, logfile=logfile, queue_summary_log=queue_summary_log)
    run(["git", "commit", "-m", f"agent {task_id}"], cwd=worktree, logfile=logfile, queue_summary_log=queue_summary_log)

    if allow_push:
        run(["git", "push", "-u", "origin", branch], cwd=worktree, logfile=logfile, queue_summary_log=queue_summary_log)
    else:
        log("allow_push=false, skipping git push.", logfile, queue_summary_log=queue_summary_log)

    return True


def parse_agent_result(worktree: Path):
    result_file = worktree / ".agent_result.md"
    if not result_file.exists():
        raw = (
            "STATUS: blocked\n\n"
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
            "summary": "No .agent_result.md was produced.",
            "done": ["- No .agent_result.md was produced."],
            "blockers": ["- Worker did not write the required result file."],
            "next_step": "Inspect the worker prompt and rerun the task.",
            "files_changed": ["- None"],
            "tests_run": ["- None"],
            "decisions": ["- None"],
            "risks": ["- Missing result contract"],
            "attempted_approaches": ["- Worker failed to write the required handoff file"],
            "raw": raw,
        }

    text = result_file.read_text(encoding="utf-8")

    status_match = re.search(r"^STATUS:\s*(.+)$", text, flags=re.MULTILINE)
    summary = split_section(text, "SUMMARY", ["DONE", "BLOCKERS", "NEXT_STEP", "FILES_CHANGED", "TESTS_RUN", "DECISIONS", "RISKS", "ATTEMPTED_APPROACHES"])
    done = split_section(text, "DONE", ["BLOCKERS", "NEXT_STEP", "FILES_CHANGED", "TESTS_RUN", "DECISIONS", "RISKS", "ATTEMPTED_APPROACHES"])
    blockers = split_section(text, "BLOCKERS", ["NEXT_STEP", "FILES_CHANGED", "TESTS_RUN", "DECISIONS", "RISKS", "ATTEMPTED_APPROACHES"])
    next_step = split_section(text, "NEXT_STEP", ["FILES_CHANGED", "TESTS_RUN", "DECISIONS", "RISKS", "ATTEMPTED_APPROACHES"])
    files_changed = split_section(text, "FILES_CHANGED", ["TESTS_RUN", "DECISIONS", "RISKS", "ATTEMPTED_APPROACHES"])
    tests_run = split_section(text, "TESTS_RUN", ["DECISIONS", "RISKS", "ATTEMPTED_APPROACHES"])
    decisions = split_section(text, "DECISIONS", ["RISKS", "ATTEMPTED_APPROACHES"])
    risks = split_section(text, "RISKS", ["ATTEMPTED_APPROACHES"])
    attempted_approaches = split_section(text, "ATTEMPTED_APPROACHES", [])

    status = status_match.group(1).strip().lower() if status_match else "blocked"
    if status not in {"complete", "partial", "blocked"}:
        status = "blocked"

    return {
        "status": status,
        "summary": summary or "No summary provided.",
        "done": parse_bullets(done),
        "blockers": parse_bullets(blockers),
        "next_step": next_step.strip() if next_step else "Inspect result manually.",
        "files_changed": parse_bullets(files_changed),
        "tests_run": parse_bullets(tests_run),
        "decisions": parse_bullets(decisions),
        "risks": parse_bullets(risks),
        "attempted_approaches": parse_bullets(attempted_approaches),
        "raw": text,
    }


def get_agent_chain(meta: dict, cfg: dict) -> list[str]:
    task_type = meta.get("task_type", cfg["default_task_type"])
    fallback_map = cfg.get("agent_fallbacks", {})
    task_chain = list(fallback_map.get(task_type, fallback_map.get(cfg["default_task_type"], ["codex", "claude", "gemini", "deepseek"])))

    requested = str(meta.get("agent", cfg["default_agent"])).strip().lower()

    if requested in {"", "auto"}:
        return task_chain

    chain = [requested] + [a for a in task_chain if a != requested]
    return chain


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

    frontmatter = {
        "task_id": new_task_id,
        "repo": repo,
        "agent": "auto",
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
    }

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


def synthesize_exhausted_result(model_attempts: list[str]) -> dict:
    return {
        "status": "blocked",
        "summary": "All configured models for this task type were already tried.",
        "done": ["- Multiple model attempts were made."],
        "blockers": [f"- No remaining fallback models. Tried: {', '.join(model_attempts) if model_attempts else 'none'}"],
        "next_step": "Review the escalation note and decide whether to refine the task, add missing context, or intervene manually.",
        "files_changed": ["- Unknown / inspect branch"],
        "tests_run": ["- Unknown / inspect prior logs"],
        "decisions": ["- Exhausted configured model fallback chain"],
        "risks": ["- Further automated retries may repeat unproductive behavior"],
        "attempted_approaches": [f"- Models tried so far: {', '.join(model_attempts) if model_attempts else 'none'}"],
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

    task = pick_task(INBOX)
    if not task:
        print("No tasks in inbox.")
        return

    processing = PROCESSING / task.name
    shutil.move(str(task), str(processing))

    task_id = processing.stem
    logfile = LOGS / f"{task_id}.log"
    worktree = None
    repo = None

    try:
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

        log(f"Processing task: {task_id}", logfile, also_summary=True, queue_summary_log=QUEUE_SUMMARY_LOG)
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
            prompt_file = write_prompt(task_id, meta_for_prompt, body, current_agent, prior_results, ROOT)

            if model_attempts:
                send_telegram(
                    cfg,
                    f"🔁 Fallback\nTask: {task_id}\nRepo: {repo.name}\nBranch: {branch}\nNext model: {current_agent}\nTask type: {task_type}",
                    logfile,
                    QUEUE_SUMMARY_LOG,
                )
            else:
                send_telegram(
                    cfg,
                    f"🚀 Started\nTask: {task_id}\nRepo: {repo.name}\nBranch: {branch}\nModel: {current_agent}\nTask type: {task_type}",
                    logfile,
                    QUEUE_SUMMARY_LOG,
                )

            log(f"Current agent: {current_agent}", logfile, also_summary=True, queue_summary_log=QUEUE_SUMMARY_LOG)
            log(f"Timeout minutes for {current_agent}: {timeout_minutes}", logfile, queue_summary_log=QUEUE_SUMMARY_LOG)

            try:
                run_agent(current_agent, worktree, prompt_file, logfile, timeout_minutes=timeout_minutes, root=ROOT, queue_summary_log=QUEUE_SUMMARY_LOG)
            except subprocess.TimeoutExpired:
                timeout_result = {
                    "agent": current_agent,
                    "status": "blocked",
                    "summary": f"{current_agent} timed out.",
                    "done": ["- Execution started but timed out."],
                    "blockers": ["- Model execution exceeded timeout."],
                    "next_step": "Try the next fallback model or split the task into a smaller step.",
                    "files_changed": ["- Inspect git status"],
                    "tests_run": ["- None"],
                    "decisions": ["- Timed out during execution"],
                    "risks": ["- Long-running task may be too broad for one attempt"],
                    "attempted_approaches": ["- Timed run exceeded timeout budget"],
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

        pushed = commit_and_push(worktree, branch, task_id, allow_push, logfile, QUEUE_SUMMARY_LOG)

        commit_hash = None
        if pushed:
            commit_hash = run(["git", "rev-parse", "HEAD"], cwd=worktree, logfile=logfile, queue_summary_log=QUEUE_SUMMARY_LOG).stdout.strip()
            log(f"Final commit hash: {commit_hash}", logfile, also_summary=True, queue_summary_log=QUEUE_SUMMARY_LOG)
        else:
            log("Task completed but nothing changed, so no commit/push happened.", logfile, also_summary=True, queue_summary_log=QUEUE_SUMMARY_LOG)

        if final_result is None:
            final_result = synthesize_exhausted_result(model_attempts)

        # Sync back to GitHub if this task originated from an issue.
        try:
            sync_result(meta, final_result, commit_hash)
        except Exception as e:
            log(f"GitHub sync warning: {e}", logfile, queue_summary_log=QUEUE_SUMMARY_LOG)

        if final_result["status"] == "complete":
            log("Final queue state: done", logfile, also_summary=True, queue_summary_log=QUEUE_SUMMARY_LOG)
            shutil.move(str(processing), str(DONE / processing.name))
            send_telegram(
                cfg,
                f"✅ Complete\nTask: {task_id}\nRepo: {repo.name}\nBranch: {branch}\nModel: {final_agent}\nCommit: {commit_hash or 'none'}\nSummary: {final_result['summary']}",
                logfile,
                QUEUE_SUMMARY_LOG,
            )
        elif final_result["status"] in ("partial", "blocked"):
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
                send_telegram(
                    cfg,
                    f"🛑 Escalated\nTask: {task_id}\nRepo: {repo.name}\nBranch: {branch}\nModels tried: {', '.join(model_attempts)}\nNote: {esc.name}",
                    logfile,
                    QUEUE_SUMMARY_LOG,
                )
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
        log("Final queue state: failed", logfile, also_summary=True, queue_summary_log=QUEUE_SUMMARY_LOG)
        if processing.exists():
            shutil.move(str(processing), str(FAILED / processing.name))
        send_telegram(cfg, f"❌ Error\nTask: {task_id}\nError: {e}", logfile, QUEUE_SUMMARY_LOG)
        raise
    finally:
        if repo is not None and worktree is not None:
            cleanup_worktree(repo, worktree, logfile, QUEUE_SUMMARY_LOG)


if __name__ == "__main__":
    main()
