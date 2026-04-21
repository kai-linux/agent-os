"""Blocker triage — attempt to auto-resolve `blocked`-labeled issues.

The dispatcher treats ``label:blocked`` as a one-way trapdoor: once an issue
lands there, nothing re-dispatches it without operator action. That is the
right safety default for permanent failures, but it leaves transient infra
issues (timeouts, quota throttling, runner glitches) and already-fixed root
causes (prompt_too_large once the queue gate landed) parked forever.

This module implements a conservative second-chance loop that runs on each
backlog-groomer pass. Policy:

- **Fixed-by-commit** blockers (``prompt_too_large``): auto-unblock once the
  named remediation commit has landed *after* the block timestamp.
- **Transient infra** blockers (``runner_failure``, ``timeout``,
  ``environment_failure``, ``quota_limited``): auto-unblock after a cool-down
  window has elapsed, capped at a small number of auto-retries per issue.
- **Everything else**: leave the block in place. Existing escalation paths
  (``_escalate_over_retried_blocked_tasks`` etc.) remain the operator surface.

State: retry counts are persisted to ``runtime/state/blocker_triage.json`` via
the same atomic tempfile+rename pattern used elsewhere, so a crash mid-write
cannot corrupt the file.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from orchestrator.gh_project import add_issue_comment, edit_issue_labels, gh_json


TRIAGE_COOLDOWN_HOURS = 2
MAX_AUTO_RETRIES = 2
TRIAGE_STATE_FILENAME = "blocker_triage.json"

TRANSIENT_BLOCKER_CODES = frozenset({
    "runner_failure",
    "timeout",
    "environment_failure",
    "quota_limited",
})

# blocker_code -> (commit_sha, short human-readable reason).
# Only add entries here when the named commit is a deliberate, isolated fix
# for the named blocker code — this is the manual curation surface.
FIXED_BY_COMMIT_BLOCKERS: dict[str, tuple[str, str]] = {
    "prompt_too_large": (
        "e4ae9d8",
        "CODEBASE.md Recent-Changes cap landed (commit e4ae9d8), following the "
        "earlier prompt-size gate and retry-history trim (fab5144). The three "
        "layered fixes together resolve the known E2BIG causes.",
    ),
}

_BLOCKER_CODE_RE = re.compile(r"###\s+Blocker code\s*\n+`([^`]+)`", re.IGNORECASE)
_ORCH_UPDATE_MARKER = "## Orchestrator update"
_TASK_ID_RE = re.compile(r"`(task-\d{8}-\d{6}-[A-Za-z0-9._\-]+)`")

# Signatures that refine a generic `runner_failure` classification into a more
# specific code. The original block was filed before the queue's classifier
# recognized these patterns, so we re-inspect the comment text at triage time
# and upgrade the code to the correct fixed-by-commit or transient bucket.
_BLOCKER_CODE_REFINEMENTS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"argument list too long", re.IGNORECASE), "prompt_too_large"),
    (re.compile(r"\be2big\b", re.IGNORECASE), "prompt_too_large"),
)


def _extract_task_id(comment_body: str) -> Optional[str]:
    m = _TASK_ID_RE.search(comment_body or "")
    return m.group(1) if m else None


def _read_task_log_tail(agent_os_root: Path, task_id: str, max_bytes: int = 16_000) -> str:
    """Return up to ``max_bytes`` from the end of the task log, or "" if missing.

    The GitHub comment surfaces only a short summary; the raw stderr that
    actually names the failure (e.g. "Argument list too long" for E2BIG) lives
    in the per-task log file. Reading the tail lets us refine a generic
    runner_failure classification into the specific blocker that best matches.
    """
    log_path = agent_os_root / "runtime" / "logs" / f"{task_id}.log"
    try:
        if not log_path.exists():
            return ""
        with log_path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
        return data.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _refine_blocker_code(
    original_code: str,
    comment_body: str,
    agent_os_root: Optional[Path] = None,
) -> str:
    """If the blocker text matches a more specific signature, return the refined
    code; otherwise return the original. Only refines the generic
    ``runner_failure`` / ``unknown`` codes to avoid clobbering explicit
    classifications.

    Looks first at the comment body, then falls back to the task-log tail when
    the comment only carries a shortened summary (the common case for
    exit-126 / E2BIG, where the full stderr never makes it into the comment)."""
    if original_code not in {"runner_failure", "unknown"}:
        return original_code
    haystacks = [comment_body or ""]
    if agent_os_root is not None:
        task_id = _extract_task_id(comment_body or "")
        if task_id:
            haystacks.append(_read_task_log_tail(agent_os_root, task_id))
    for text in haystacks:
        for pattern, refined_code in _BLOCKER_CODE_REFINEMENTS:
            if pattern.search(text):
                return refined_code
    return original_code


@dataclass
class TriageDecision:
    action: str  # "unblock" | "leave"
    reason: str
    new_label: str = "ready"


def _state_path(agent_os_root: Path) -> Path:
    return agent_os_root / "runtime" / "state" / TRIAGE_STATE_FILENAME


def _load_state(agent_os_root: Path) -> dict:
    p = _state_path(agent_os_root)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(agent_os_root: Path, state: dict) -> None:
    p = _state_path(agent_os_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


def _parse_blocker_code(comment_body: str) -> Optional[str]:
    m = _BLOCKER_CODE_RE.search(comment_body or "")
    if not m:
        return None
    return m.group(1).strip().lower() or None


def _latest_orchestrator_blocker(
    comments: list[dict],
    agent_os_root: Optional[Path] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Return (blocker_code, createdAt) from the most recent orchestrator-update
    comment that reported ``Status: blocked``. Returns (None, None) if no such
    comment is present. When ``agent_os_root`` is supplied, the task log tail
    is consulted to refine a generic ``runner_failure`` into a more specific
    code (e.g. ``prompt_too_large`` for the E2BIG signature)."""
    for comment in reversed(comments or []):
        body = comment.get("body") or ""
        if _ORCH_UPDATE_MARKER not in body:
            continue
        if "`blocked`" not in body:
            continue
        code = _parse_blocker_code(body)
        if not code:
            return None, comment.get("createdAt")
        refined = _refine_blocker_code(code, body, agent_os_root=agent_os_root)
        return refined, comment.get("createdAt")
    return None, None


def _list_blocked_issues(repo: str) -> list[dict]:
    return gh_json([
        "issue", "list", "-R", repo,
        "--search", "is:open label:blocked",
        "--limit", "50",
        "--json", "number,title,labels,url,updatedAt,comments",
    ]) or []


def _commit_landed_after(sha: str, block_iso: str, agent_os_root: Path) -> bool:
    """True iff ``sha`` exists locally and was committed strictly after ``block_iso``."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(agent_os_root), "show", "-s", "--format=%cI", sha],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return False
    if proc.returncode != 0 or not proc.stdout.strip():
        return False
    try:
        commit_time = datetime.fromisoformat(proc.stdout.strip().replace("Z", "+00:00"))
        block_time = datetime.fromisoformat((block_iso or "").replace("Z", "+00:00"))
    except ValueError:
        return False
    return commit_time > block_time


def decide(
    blocker_code: Optional[str],
    block_time_iso: Optional[str],
    retry_count: int,
    agent_os_root: Path,
    now: Optional[datetime] = None,
) -> TriageDecision:
    if not blocker_code:
        return TriageDecision(
            "leave",
            "No machine-readable blocker code on the latest orchestrator comment.",
        )
    if retry_count >= MAX_AUTO_RETRIES:
        return TriageDecision(
            "leave",
            f"Auto-retry cap reached ({retry_count}/{MAX_AUTO_RETRIES}); needs human review.",
        )

    if blocker_code in FIXED_BY_COMMIT_BLOCKERS:
        sha, reason = FIXED_BY_COMMIT_BLOCKERS[blocker_code]
        if block_time_iso and _commit_landed_after(sha, block_time_iso, agent_os_root):
            return TriageDecision("unblock", reason)
        return TriageDecision(
            "leave",
            f"Blocker `{blocker_code}` has no confirmed post-block fix yet.",
        )

    if blocker_code in TRANSIENT_BLOCKER_CODES:
        if not block_time_iso:
            return TriageDecision(
                "leave",
                "No block timestamp; cannot enforce the cool-down window.",
            )
        try:
            block_time = datetime.fromisoformat(block_time_iso.replace("Z", "+00:00"))
        except ValueError:
            return TriageDecision("leave", "Unparseable block timestamp.")
        current = now or datetime.now(timezone.utc)
        age = current - block_time
        if age < timedelta(hours=TRIAGE_COOLDOWN_HOURS):
            return TriageDecision(
                "leave",
                f"Cool-down not elapsed ({age} < {TRIAGE_COOLDOWN_HOURS}h).",
            )
        return TriageDecision(
            "unblock",
            f"Transient blocker `{blocker_code}` older than {TRIAGE_COOLDOWN_HOURS}h cool-down; re-queueing.",
        )

    return TriageDecision(
        "leave",
        f"Blocker `{blocker_code}` requires operator action.",
    )


def triage_repo(cfg: dict, github_slug: str, agent_os_root: Path) -> dict:
    stats = {"considered": 0, "unblocked": 0, "left": 0, "errors": 0}
    del cfg
    issues = _list_blocked_issues(github_slug)
    state = _load_state(agent_os_root)
    repo_state = state.setdefault(github_slug, {})
    changed = False

    for issue in issues:
        stats["considered"] += 1
        number = int(issue["number"])
        key = str(number)
        record = repo_state.get(key, {"retries": 0, "last_action": None, "last_action_at": None})
        retry_count = int(record.get("retries", 0))

        comments = issue.get("comments", []) or []
        blocker_code, block_time = _latest_orchestrator_blocker(
            comments, agent_os_root=agent_os_root,
        )
        decision = decide(blocker_code, block_time, retry_count, agent_os_root)

        if decision.action != "unblock":
            stats["left"] += 1
            continue

        try:
            edit_issue_labels(
                github_slug, number,
                add=[decision.new_label], remove=["blocked"],
            )
            add_issue_comment(
                github_slug, number,
                (
                    "## Blocker triage\n\n"
                    f"Auto-cleared `blocked` label (attempt {retry_count + 1}/{MAX_AUTO_RETRIES}).\n\n"
                    f"**Reason:** {decision.reason}\n\n"
                    "If the underlying issue is not actually resolved, re-apply `blocked` "
                    "and the triage loop will leave it for operator escalation."
                ),
            )
            record["retries"] = retry_count + 1
            record["last_action"] = "unblock"
            record["last_action_at"] = datetime.now(timezone.utc).isoformat()
            repo_state[key] = record
            changed = True
            stats["unblocked"] += 1
            print(f"  Blocker triage: unblocked #{number} — {decision.reason}")
        except Exception as e:
            print(f"  Blocker triage: failed on #{number}: {e}")
            stats["errors"] += 1

    if changed:
        _save_state(agent_os_root, state)
    return stats
