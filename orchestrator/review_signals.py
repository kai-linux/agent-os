"""Review signal extraction and follow-up generation for merged PRs.

Records structured review signals (coverage gap, risk level, diff size) when
PRs merge, persists them to a JSONL log, and generates bounded follow-up
issues for PRs that merged with quality flags.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from orchestrator.pr_risk_assessment import RiskAssessment


REVIEW_SIGNALS_FILENAME = "review_signals.jsonl"

# Follow-up generation bounds
MAX_FOLLOWUPS_PER_SPRINT = 3
FOLLOWUP_TITLE_PREFIX = "Review follow-up: "
_FOLLOWUP_MARKER = "<!-- review-signal-followup -->"

# Signal thresholds for follow-up creation
_COVERAGE_GAP_THRESHOLD = True  # any source change without tests
_HIGH_RISK_THRESHOLD = "high"
_LARGE_DIFF_LINES = 500


def _signals_log_path(cfg: dict) -> Path:
    root = Path(cfg.get("root_dir", ".")).expanduser()
    return root / "runtime" / "metrics" / REVIEW_SIGNALS_FILENAME


def record_review_signal(
    cfg: dict,
    *,
    repo: str,
    pr_number: int,
    task_id: str | None,
    issue_number: int | None,
    risk: RiskAssessment,
    branch: str | None = None,
) -> dict:
    """Record review signals for a merged PR. Returns the signal record."""
    signals: list[str] = []
    for s in risk.signals:
        signals.append(f"{s.category}:{s.severity}:{s.detail}")

    record = {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "repo": repo,
        "pr_number": pr_number,
        "task_id": task_id,
        "issue_number": issue_number,
        "branch": branch,
        "risk_level": risk.level,
        "files_changed": risk.files_changed,
        "lines_changed": risk.lines_changed,
        "has_test_changes": risk.has_test_changes,
        "has_source_changes": risk.has_source_changes,
        "coverage_gap": risk.has_source_changes and not risk.has_test_changes,
        "signals": signals,
    }

    log_path = _signals_log_path(cfg)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True) + "\n"
    existing = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    fd, tmp_path = tempfile.mkstemp(dir=log_path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(existing + line)
        os.replace(tmp_path, log_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return record


def load_review_signals(
    cfg: dict,
    *,
    repo: str | None = None,
    window_days: int = 7,
) -> list[dict]:
    """Load recent review signal records, optionally filtered by repo."""
    log_path = _signals_log_path(cfg)
    if not log_path.exists():
        return []

    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=window_days)
    records: list[dict] = []
    with log_path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if repo and record.get("repo") != repo:
                continue
            ts = record.get("timestamp", "")
            try:
                parsed = datetime.fromisoformat(ts)
                if parsed < cutoff:
                    continue
            except (ValueError, TypeError):
                pass
            records.append(record)
    return records


def query_flagged_signals(
    cfg: dict,
    *,
    repo: str | None = None,
    window_days: int = 7,
) -> list[dict]:
    """Return review signals that warrant follow-up action.

    A signal is flagged when it has:
    - coverage_gap: source changes merged without test changes
    - high risk level from risk assessment
    """
    signals = load_review_signals(cfg, repo=repo, window_days=window_days)
    flagged: list[dict] = []
    for s in signals:
        reasons: list[str] = []
        if s.get("coverage_gap"):
            reasons.append("coverage_gap")
        if s.get("risk_level") == _HIGH_RISK_THRESHOLD:
            reasons.append("high_risk")
        if reasons:
            s["followup_reasons"] = reasons
            flagged.append(s)
    return flagged


def generate_followup_issues(
    cfg: dict,
    repo: str,
    *,
    window_days: int = 7,
    dry_run: bool = False,
) -> list[dict]:
    """Create follow-up issues for merged PRs with quality flags.

    Returns list of created follow-up records. Bounded to
    MAX_FOLLOWUPS_PER_SPRINT per sprint window. Deduplicates against
    existing open issues with the same title.
    """
    from orchestrator.gh_project import ensure_labels, gh, gh_json

    flagged = query_flagged_signals(cfg, repo=repo, window_days=window_days)
    if not flagged:
        return []

    # Check how many review follow-ups already exist this sprint
    existing_count = _count_existing_followups(repo)
    remaining = MAX_FOLLOWUPS_PER_SPRINT - existing_count
    if remaining <= 0:
        print(f"Review follow-up limit reached ({MAX_FOLLOWUPS_PER_SPRINT}), skipping")
        return []

    created: list[dict] = []
    for signal in flagged:
        if len(created) >= remaining:
            break

        pr_number = signal.get("pr_number")
        task_id = signal.get("task_id", "unknown")
        reasons = signal.get("followup_reasons", [])
        title = f"{FOLLOWUP_TITLE_PREFIX}PR #{pr_number} ({', '.join(reasons)})"

        # Dedup: skip if an open issue with this title already exists
        if _followup_exists(repo, title):
            continue

        body = _build_followup_body(signal)

        if dry_run:
            created.append({"title": title, "body": body, "signal": signal, "dry_run": True})
            continue

        labels = ["ready", "prio:normal"]
        try:
            ensure_labels(repo, labels)
            cmd = ["issue", "create", "-R", repo, "--title", title, "--body", body]
            for label in labels:
                cmd += ["--label", label]
            issue_url = gh(cmd)
            created.append({
                "title": title,
                "issue_url": issue_url,
                "signal": signal,
            })
            print(f"Created review follow-up: {title}")
        except Exception as e:
            print(f"Warning: failed to create follow-up for PR #{pr_number}: {e}")

    return created


def _count_existing_followups(repo: str) -> int:
    """Count open review follow-up issues in the repo."""
    from orchestrator.gh_project import gh_json

    try:
        issues = gh_json([
            "issue", "list", "-R", repo, "--state", "open",
            "--search", f'"{FOLLOWUP_TITLE_PREFIX}"',
            "--json", "number,title",
            "--limit", "20",
        ]) or []
    except Exception:
        return 0
    return sum(1 for i in issues if (i.get("title") or "").startswith(FOLLOWUP_TITLE_PREFIX))


def _followup_exists(repo: str, title: str) -> bool:
    """Check if an open issue with the exact title already exists."""
    from orchestrator.gh_project import gh_json

    try:
        issues = gh_json([
            "issue", "list", "-R", repo, "--state", "open",
            "--search", title,
            "--json", "number,title",
            "--limit", "10",
        ]) or []
    except Exception:
        return False
    return any((i.get("title") or "").strip() == title.strip() for i in issues)


def _build_followup_body(signal: dict) -> str:
    """Build the issue body for a review signal follow-up."""
    reasons = signal.get("followup_reasons", [])
    pr_number = signal.get("pr_number")
    task_id = signal.get("task_id", "unknown")
    issue_number = signal.get("issue_number")
    risk_level = signal.get("risk_level", "unknown")
    files_changed = signal.get("files_changed", 0)
    lines_changed = signal.get("lines_changed", 0)
    coverage_gap = signal.get("coverage_gap", False)
    raw_signals = signal.get("signals", [])

    goal_parts: list[str] = []
    criteria_parts: list[str] = []

    if "coverage_gap" in reasons:
        goal_parts.append(
            f"Add test coverage for source changes merged in PR #{pr_number} "
            f"that shipped without corresponding test updates"
        )
        criteria_parts.append("- New or updated tests cover the changed source paths")
        criteria_parts.append("- Test suite passes with the added coverage")

    if "high_risk" in reasons:
        goal_parts.append(
            f"Review and validate high-risk changes merged in PR #{pr_number}"
        )
        criteria_parts.append("- High-risk file changes are verified correct")
        criteria_parts.append("- Any issues found are fixed or documented")

    goal = ". ".join(goal_parts) if goal_parts else f"Review PR #{pr_number} quality signals"
    criteria = "\n".join(criteria_parts) if criteria_parts else "- Quality concern addressed"

    signal_lines = "\n".join(f"- {s}" for s in raw_signals) if raw_signals else "- None"

    return f"""{_FOLLOWUP_MARKER}
## Goal
{goal}

## Success Criteria
{criteria}

## Constraints
- This is a review-driven follow-up, not a bug fix
- Keep changes minimal and focused on the flagged concern
- Do not refactor unrelated code

## Task Type
implementation

## Context
- Source PR: #{pr_number}
- Original task: `{task_id}`
- Original issue: #{issue_number or 'unknown'}
- Risk level: {risk_level}
- Files changed: {files_changed}
- Lines changed: {lines_changed}
- Coverage gap: {coverage_gap}

## Review Signals
{signal_lines}

## Flagged Reasons
{', '.join(reasons)}
"""
