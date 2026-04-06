"""Deterministic semantic risk assessment for agent PRs.

Analyzes PR diffs using bounded file-pattern heuristics to surface risk
signals without open-ended browsing or LLM calls.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field


# --- Risk pattern definitions ---

# Paths that carry elevated risk when modified by agents
_HIGH_RISK_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^\.github/workflows/"), "CI/CD workflow modified"),
    (re.compile(r"(^|/)secrets"), "secrets-related path touched"),
    (re.compile(r"(^|/)\.env"), "environment config touched"),
    (re.compile(r"(^|/)credentials"), "credentials path touched"),
    (re.compile(r"^orchestrator/queue\.py$"), "core queue logic modified"),
    (re.compile(r"^orchestrator/supervisor\.py$"), "supervisor modified"),
    (re.compile(r"^orchestrator/paths\.py$"), "runtime paths modified"),
    (re.compile(r"^bin/"), "shell entry-point modified"),
]

_MEDIUM_RISK_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^orchestrator/github_dispatcher\.py$"), "dispatcher modified"),
    (re.compile(r"^orchestrator/pr_monitor\.py$"), "PR monitor modified"),
    (re.compile(r"^orchestrator/github_sync\.py$"), "GitHub sync modified"),
    (re.compile(r"(^|/)config\.yaml$"), "config file modified"),
    (re.compile(r"^example\.config\.yaml$"), "example config modified"),
    (re.compile(r"^setup\.py$|^pyproject\.toml$|^requirements"), "dependency file modified"),
]

# Thresholds
_LARGE_DIFF_FILES = 15
_LARGE_DIFF_LINES = 500


@dataclass
class RiskSignal:
    """A single risk signal detected in a PR."""
    category: str  # e.g. "risky_path", "coverage_gap", "large_diff"
    severity: str  # "high" or "medium"
    detail: str


@dataclass
class RiskAssessment:
    """Aggregated risk assessment for a PR."""
    level: str = "low"  # "low", "medium", "high"
    signals: list[RiskSignal] = field(default_factory=list)
    files_changed: int = 0
    lines_changed: int = 0
    has_test_changes: bool = False
    has_source_changes: bool = False

    @property
    def summary(self) -> str:
        if not self.signals:
            return f"Low risk ({self.files_changed} file(s), {self.lines_changed} line(s))"
        lines = [f"**Risk: {self.level.upper()}** ({self.files_changed} file(s), {self.lines_changed} line(s))"]
        for s in self.signals:
            lines.append(f"- [{s.severity}] {s.detail}")
        return "\n".join(lines)

    @property
    def short_summary(self) -> str:
        """One-line summary for Telegram."""
        if not self.signals:
            return f"risk:low ({self.files_changed} files)"
        top = self.signals[0].detail
        extra = f" +{len(self.signals) - 1} more" if len(self.signals) > 1 else ""
        return f"risk:{self.level} — {top}{extra}"


def _get_pr_diff_stat(repo: str, pr_number: int) -> tuple[list[str], int]:
    """Return (changed_file_paths, total_lines_changed) from PR diff stat."""
    try:
        result = subprocess.run(
            ["gh", "pr", "diff", str(pr_number), "-R", repo, "--stat"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return [], 0
    except Exception:
        return [], 0

    files: list[str] = []
    total_lines = 0
    for line in result.stdout.strip().splitlines():
        # diff stat lines look like: " path/to/file | 42 ++---"
        # summary line looks like: " 5 files changed, 120 insertions(+), 30 deletions(-)"
        m = re.match(r"^\s*(.+?)\s+\|\s+(\d+)", line)
        if m:
            files.append(m.group(1).strip())
            total_lines += int(m.group(2))
    return files, total_lines


def assess_pr_risk(repo: str, pr_number: int) -> RiskAssessment:
    """Run deterministic risk heuristics on a PR's changed files.

    This function is bounded: it reads only the diff stat (no file contents),
    applies pattern matching, and returns a structured assessment.
    """
    files, total_lines = _get_pr_diff_stat(repo, pr_number)
    assessment = RiskAssessment(
        files_changed=len(files),
        lines_changed=total_lines,
    )

    # Classify files
    test_re = re.compile(r"(^|/)tests?/|test_|_test\.")
    source_re = re.compile(r"\.(py|js|ts|go|rs|sh)$")

    for f in files:
        if test_re.search(f):
            assessment.has_test_changes = True
        if source_re.search(f) and not test_re.search(f):
            assessment.has_source_changes = True

        # Check high-risk patterns
        for pattern, reason in _HIGH_RISK_PATTERNS:
            if pattern.search(f):
                assessment.signals.append(RiskSignal(
                    category="risky_path",
                    severity="high",
                    detail=f"{reason}: `{f}`",
                ))
                break
        else:
            # Check medium-risk patterns only if no high-risk match
            for pattern, reason in _MEDIUM_RISK_PATTERNS:
                if pattern.search(f):
                    assessment.signals.append(RiskSignal(
                        category="risky_path",
                        severity="medium",
                        detail=f"{reason}: `{f}`",
                    ))
                    break

    # Coverage gap: source changes without corresponding test changes
    if assessment.has_source_changes and not assessment.has_test_changes:
        assessment.signals.append(RiskSignal(
            category="coverage_gap",
            severity="medium",
            detail="Source files changed without test changes",
        ))

    # Large diff
    if len(files) >= _LARGE_DIFF_FILES:
        assessment.signals.append(RiskSignal(
            category="large_diff",
            severity="medium",
            detail=f"Large PR: {len(files)} files changed",
        ))
    if total_lines >= _LARGE_DIFF_LINES:
        assessment.signals.append(RiskSignal(
            category="large_diff",
            severity="medium",
            detail=f"Large PR: {total_lines} lines changed",
        ))

    # Determine overall level
    has_high = any(s.severity == "high" for s in assessment.signals)
    has_medium = any(s.severity == "medium" for s in assessment.signals)
    if has_high:
        assessment.level = "high"
    elif has_medium:
        assessment.level = "medium"
    else:
        assessment.level = "low"

    return assessment
