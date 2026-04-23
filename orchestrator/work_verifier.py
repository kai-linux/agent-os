"""Pre-merge work verification for autonomous PRs.

The verifier blocks auto-merge when a PR appears incomplete, stubbed, mocked,
or mismatched against the issue it claims to resolve. It combines:

1. Deterministic anti-pattern detection on the raw diff.
2. Scope heuristics using diff size and test coverage signals.
3. A fixed-prompt LLM judge that must use a model-family independent from the
   worker that authored the PR.
4. An operator override path recorded in both the immutable audit log and a
   repo-local verifier override log.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestrator.audit_log import append_audit_event
from orchestrator.cost_tracker import resolve_attempt_provider
from orchestrator.gh_project import gh_json
from orchestrator.incident_router import classify_severity, escalate as route_incident
from orchestrator.pr_risk_assessment import RiskAssessment, assess_pr_risk

VERIFIER_DIRNAME = "work_verifier"
REPORTS_FILENAME = "reports.jsonl"
OVERRIDES_FILENAME = "overrides.jsonl"
_BLOCK_COMMENT_MARKER = "<!-- work-verifier-block -->"
_FIXED_CLAUDE_MODEL = "claude-sonnet-4"
_FIXED_GEMINI_MODEL = "gemini-2.5-flash"
_MAX_DIFF_CHARS = 24000
_MAX_ISSUE_CHARS = 12000
_SMALL_DIFF_LINES = 12
_SMALL_DIFF_FILES = 1
_LARGE_DIFF_LINES = 1200
_LARGE_DIFF_FILES = 30
_CODELIKE_EXTENSIONS = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".go",
    ".rs",
    ".sh",
    ".bash",
    ".zsh",
    ".yml",
    ".yaml",
    ".json",
    ".toml",
}

# NOTE: kept as a static string (no ``str.format`` templating) because the
# literal JSON schema below contains ``{``/``}`` characters that ``.format``
# would parse as placeholders — the past incident (KeyError: '\n  "verdict"')
# crashed pr_monitor mid-merge and burned PR merge attempts. The two real
# substitutions (issue body, diff) are appended via f-string below instead.
_JUDGE_PROMPT_HEADER = """You are the Agent OS work verifier. Your job is to judge whether a pull request actually fulfills the linked issue.

Rules:
- Extract the concrete acceptance criteria from the issue body. Prefer bullets in Success Criteria / Acceptance Criteria sections, but infer criteria from Goal when necessary.
- Judge ONLY from the provided issue body and diff. Do not assume unshown implementation.
- Flag scope mismatch when the diff is clearly too small, too broad, or misses expected tests for the requested work.
- Return strict JSON only, no markdown fences.
- Verdict meanings:
  - pass: the diff appears to satisfy the issue with no major gap.
  - block: the diff appears incomplete, stubbed, or materially mismatched.
  - uncertain: evidence is insufficient; treat missing evidence conservatively.

Return this JSON object:
{
  "verdict": "pass|block|uncertain",
  "summary": "short sentence",
  "criteria": [
    {
      "criterion": "text",
      "verdict": "pass|fail|uncertain",
      "reason": "short reason tied to the diff"
    }
  ],
  "scope_assessment": {
    "verdict": "match|too_small|too_large|uncertain",
    "reason": "short reason"
  },
  "missing_tests": true,
  "notes": ["short note"]
}
"""


@dataclass
class VerificationFinding:
    category: str
    detail: str
    severity: str = "high"
    path: str | None = None


@dataclass
class CriterionVerdict:
    criterion: str
    verdict: str
    reason: str


@dataclass
class WorkVerificationReport:
    verdict: str
    summary: str
    findings: list[VerificationFinding] = field(default_factory=list)
    criteria: list[CriterionVerdict] = field(default_factory=list)
    issue_number: int | None = None
    override_applied: bool = False
    override_reason: str = ""
    judge_model_family: str = ""
    judge_model: str = ""
    judge_summary: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return self.verdict == "block"

    @property
    def signature(self) -> str:
        payload = {
            "verdict": self.verdict,
            "summary": self.summary,
            "findings": [(f.category, f.path, f.detail) for f in self.findings[:12]],
            "criteria": [(c.criterion, c.verdict) for c in self.criteria[:12]],
            "override": self.override_applied,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _root_dir(cfg: dict) -> Path:
    return Path(cfg.get("root_dir", ".")).expanduser()


def _verifier_dir(cfg: dict) -> Path:
    path = _root_dir(cfg) / "runtime" / VERIFIER_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _reports_path(cfg: dict) -> Path:
    return _verifier_dir(cfg) / REPORTS_FILENAME


def _overrides_path(cfg: dict) -> Path:
    return _verifier_dir(cfg) / OVERRIDES_FILENAME


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, sort_keys=True) + "\n"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(existing + line)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _get_pr_diff(repo: str, pr_number: int) -> str:
    result = subprocess.run(
        ["gh", "pr", "diff", str(pr_number), "-R", repo],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"failed to load diff for PR #{pr_number}: {(result.stderr or '').strip()}")
    return result.stdout


def _current_diff_path(line: str) -> str | None:
    match = re.match(r"^\+\+\+ b/(.+)$", line)
    return match.group(1).strip() if match else None


def _is_codelike_path(path: str | None) -> bool:
    if not path:
        return False
    suffix = Path(path).suffix.lower()
    return suffix in _CODELIKE_EXTENSIONS or path.startswith(("bin/", ".github/workflows/"))


def _is_immediate_function_body(previous_line: str) -> bool:
    lowered = previous_line.strip().lower()
    return bool(re.match(r"^(async\s+def|def)\s+.+:\s*$", lowered))


def _deterministic_pattern_findings(diff_text: str) -> list[VerificationFinding]:
    findings: list[VerificationFinding] = []
    seen: set[tuple[str, str | None, str]] = set()
    current_path: str | None = None
    previous_added_line = ""

    def add(category: str, detail: str, *, path: str | None = None) -> None:
        key = (category, path, detail)
        if key in seen:
            return
        seen.add(key)
        findings.append(VerificationFinding(category=category, detail=detail, path=path))

    for raw_line in diff_text.splitlines():
        path = _current_diff_path(raw_line)
        if path:
            current_path = path
            previous_added_line = ""
            continue
        if not raw_line.startswith("+") or raw_line.startswith("+++"):
            continue
        line = raw_line[1:]
        stripped = line.strip()
        lowered = stripped.lower()

        is_codelike = _is_codelike_path(current_path)
        previous_lowered = previous_added_line.strip().lower()

        if is_codelike and "notimplementederror" in stripped.replace(" ", "").lower():
            add("stub", "Added `NotImplementedError` stub", path=current_path)
        if is_codelike and re.search(r"\b(todo|fixme|xxx)\b", lowered):
            add("todo", f"Added TODO-style placeholder: `{stripped[:120]}`", path=current_path)
        if is_codelike and (re.search(r"(^|[\s(])pass($|[\s#])", stripped) or stripped == "...") and _is_immediate_function_body(previous_added_line):
            add("stub", f"Added placeholder no-op: `{stripped}`", path=current_path)
        if (
            is_codelike
            and re.match(r"return(\s+None|\s+\[\]|\s+\{\}|\s+\"\"|\s+''|\s+False|\s+True)?\s*$", stripped)
            and _is_immediate_function_body(previous_added_line)
        ):
            add("bare_return", f"Added bare return stub: `{stripped}`", path=current_path)
        if is_codelike and re.search(r"@pytest\.mark\.skip|@unittest\.skip|\.skip\(|\bit\.skip\(", lowered):
            add("skipped_test", f"Added skipped test marker: `{stripped[:120]}`", path=current_path)
        if is_codelike and re.search(r"\b(mock|magicmock|mocker|jest\.mock|mockimplementation|sinon)\b|\bpatch\(", lowered):
            add("mock", f"Added mock construct: `{stripped[:120]}`", path=current_path)
        suffix = Path(current_path or "").suffix.lower()
        if is_codelike and suffix not in {".yml", ".yaml", ".toml"} and stripped.startswith(("#", "//", "/*")) and re.search(
            r"\b(if|for|while|return|def|class|const|let|var|function|await|try|except)\b",
            lowered,
        ):
            add("commented_code", f"Added commented-out code: `{stripped[:120]}`", path=current_path)
        if stripped:
            previous_added_line = stripped
    return findings


def _extract_linked_issue_number(pr_body: str) -> int | None:
    match = re.search(r"#(\d+)", pr_body or "")
    return int(match.group(1)) if match else None


def _fetch_issue_payload(repo: str, issue_number: int) -> dict[str, Any]:
    payload = gh_json(["issue", "view", str(issue_number), "-R", repo, "--json", "body,labels"]) or {}
    return payload if isinstance(payload, dict) else {}


def _provider_family(agent: str | None, cfg: dict | None = None) -> str:
    agent_name = str(agent or "").strip().lower()
    if not agent_name:
        return "unknown"
    provider = resolve_attempt_provider(agent_name, cfg)
    return str(provider or "unknown").strip().lower()


def _judge_command(worker_agent: str | None, cfg: dict) -> tuple[list[str], str, str]:
    worker_family = _provider_family(worker_agent, cfg)
    if worker_family == "anthropic":
        gemini_bin = os.environ.get("GEMINI_BIN", "gemini")
        return [gemini_bin, "-m", _FIXED_GEMINI_MODEL], "google", _FIXED_GEMINI_MODEL
    claude_bin = os.environ.get("CLAUDE_BIN", "claude")
    return [claude_bin], "anthropic", _FIXED_CLAUDE_MODEL


def _call_independent_judge(
    cfg: dict,
    *,
    worker_agent: str | None,
    issue_body: str,
    diff_text: str,
) -> tuple[dict[str, Any], str, str]:
    prompt = (
        f"{_JUDGE_PROMPT_HEADER}\n"
        f"Issue body:\n{(issue_body or '')[:_MAX_ISSUE_CHARS]}\n\n"
        f"PR diff:\n{(diff_text or '')[:_MAX_DIFF_CHARS]}\n"
    )
    cmd, family, model = _judge_command(worker_agent, cfg)
    if family == "anthropic":
        argv = [*cmd, "-p", prompt, "--model", model]
    else:
        argv = [*cmd, prompt]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=180)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"independent judge failed via {family}/{model}: {detail[:300]}")
    raw = (result.stdout or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise RuntimeError("independent judge returned non-object payload")
    return payload, family, model


def _criteria_from_payload(payload: dict[str, Any]) -> list[CriterionVerdict]:
    items: list[CriterionVerdict] = []
    for row in payload.get("criteria") or []:
        if not isinstance(row, dict):
            continue
        criterion = str(row.get("criterion") or "").strip()
        verdict = str(row.get("verdict") or "").strip().lower()
        reason = str(row.get("reason") or "").strip()
        if not criterion:
            continue
        items.append(CriterionVerdict(criterion=criterion, verdict=verdict or "uncertain", reason=reason))
    return items


def _scope_findings(risk: RiskAssessment, *, issue_body: str) -> list[VerificationFinding]:
    findings: list[VerificationFinding] = []
    lowered = (issue_body or "").lower()
    success_criteria_count = len(re.findall(r"(?m)^\s*[-*]\s+", issue_body or ""))
    expects_tests = any(token in lowered for token in ("test", "regression", "verify", "coverage"))
    if risk.has_source_changes and not risk.has_test_changes:
        findings.append(
            VerificationFinding(
                category="missing_tests",
                detail="Source changes landed without accompanying test changes",
                severity="medium",
            )
        )
    if success_criteria_count >= 2 and risk.files_changed <= _SMALL_DIFF_FILES and risk.lines_changed <= _SMALL_DIFF_LINES:
        findings.append(
            VerificationFinding(
                category="scope_too_small",
                detail=(
                    f"Diff looks too small for an issue with {success_criteria_count} acceptance bullets "
                    f"({risk.files_changed} file, {risk.lines_changed} changed lines)"
                ),
                severity="high",
            )
        )
    if risk.files_changed >= _LARGE_DIFF_FILES or risk.lines_changed >= _LARGE_DIFF_LINES:
        findings.append(
            VerificationFinding(
                category="scope_too_large",
                detail=(
                    f"Diff looks unusually large for a single issue ({risk.files_changed} files, "
                    f"{risk.lines_changed} changed lines)"
                ),
                severity="medium",
            )
        )
    if expects_tests and not risk.has_test_changes:
        findings.append(
            VerificationFinding(
                category="scope_mismatch",
                detail="Issue body references verification/testing but the PR adds no test changes",
                severity="high",
            )
        )
    return findings


def _latest_override(cfg: dict, repo: str, pr_number: int) -> dict[str, Any] | None:
    records = _read_jsonl(_overrides_path(cfg))
    for record in reversed(records):
        if record.get("repo") == repo and int(record.get("pr_number") or 0) == pr_number:
            return record
    return None


def record_override(
    cfg: dict,
    *,
    repo: str,
    pr_number: int,
    operator: dict[str, Any],
    reason: str = "",
) -> dict[str, Any]:
    prior = latest_report_record(cfg, repo=repo, pr_number=pr_number) or {}
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "repo": repo,
        "pr_number": pr_number,
        "reason": reason.strip(),
        "operator": {
            "chat_id": str(operator.get("chat_id") or "").strip(),
            "username": str(operator.get("username") or "").strip(),
            "display_name": str(operator.get("display_name") or "").strip(),
        },
        "overridden_verdict": str(prior.get("verdict") or "block"),
        "overridden_summary": str(prior.get("summary") or "").strip(),
    }
    _append_jsonl(_overrides_path(cfg), payload)
    append_audit_event(
        cfg,
        "work_verifier_override",
        {
            "repo": repo,
            "pr_number": pr_number,
            "operator": payload["operator"],
            "reason": payload["reason"],
            "overridden_verdict": payload["overridden_verdict"],
            "overridden_summary": payload["overridden_summary"],
        },
    )
    return payload


def latest_report_record(cfg: dict, *, repo: str, pr_number: int) -> dict[str, Any] | None:
    for record in reversed(_read_jsonl(_reports_path(cfg))):
        if record.get("repo") == repo and int(record.get("pr_number") or 0) == pr_number:
            return record
    return None


def _persist_report(cfg: dict, repo: str, pr_number: int, report: WorkVerificationReport) -> None:
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "repo": repo,
        "pr_number": pr_number,
        "issue_number": report.issue_number,
        "verdict": report.verdict,
        "summary": report.summary,
        "override_applied": report.override_applied,
        "override_reason": report.override_reason,
        "judge_model_family": report.judge_model_family,
        "judge_model": report.judge_model,
        "judge_summary": report.judge_summary,
        "findings": [asdict(item) for item in report.findings],
        "criteria": [asdict(item) for item in report.criteria],
        "metadata": report.metadata,
    }
    _append_jsonl(_reports_path(cfg), payload)


def verify_pull_request(
    cfg: dict,
    *,
    repo: str,
    pr_number: int,
    pr_body: str,
    worker_agent: str | None,
) -> WorkVerificationReport:
    override = _latest_override(cfg, repo, pr_number)
    if override:
        report = WorkVerificationReport(
            verdict="pass",
            summary=f"Operator override applied for PR #{pr_number}.",
            override_applied=True,
            override_reason=str(override.get("reason") or "").strip(),
            metadata={"override_operator": override.get("operator") or {}},
        )
        _persist_report(cfg, repo, pr_number, report)
        return report

    issue_number = _extract_linked_issue_number(pr_body)
    if not issue_number:
        report = WorkVerificationReport(
            verdict="block",
            summary="Work verifier could not find a linked issue in the PR body.",
            findings=[VerificationFinding(category="scope_mismatch", detail="No linked issue found in PR body", severity="high")],
        )
        _persist_report(cfg, repo, pr_number, report)
        return report

    diff_text = _get_pr_diff(repo, pr_number)
    pattern_findings = _deterministic_pattern_findings(diff_text)
    risk = assess_pr_risk(repo, pr_number)
    issue_payload = _fetch_issue_payload(repo, issue_number)
    issue_body = str(issue_payload.get("body") or "")
    if not worker_agent:
        labels = issue_payload.get("labels") or []
        for label in labels:
            name = ""
            if isinstance(label, dict):
                name = str(label.get("name") or "").strip().lower()
            if name in {"claude", "codex", "gemini", "deepseek"}:
                worker_agent = name
                break
    scope_findings = _scope_findings(risk, issue_body=issue_body)

    combined_findings = [*pattern_findings, *scope_findings]
    if any(item.severity == "high" for item in combined_findings):
        report = WorkVerificationReport(
            verdict="block",
            summary="Work verifier blocked auto-merge on deterministic anti-pattern or scope checks.",
            findings=combined_findings,
            issue_number=issue_number,
            metadata={
                "pattern_only": True,
                "files_changed": risk.files_changed,
                "lines_changed": risk.lines_changed,
            },
        )
        _persist_report(cfg, repo, pr_number, report)
        return report

    judge_payload, family, model = _call_independent_judge(
        cfg,
        worker_agent=worker_agent,
        issue_body=issue_body,
        diff_text=diff_text,
    )
    criteria = _criteria_from_payload(judge_payload)
    judge_verdict = str(judge_payload.get("verdict") or "uncertain").strip().lower()
    scope_assessment = judge_payload.get("scope_assessment") or {}
    notes = [str(item).strip() for item in (judge_payload.get("notes") or []) if str(item).strip()]
    if str(scope_assessment.get("verdict") or "").strip().lower() in {"too_small", "too_large"}:
        combined_findings.append(
            VerificationFinding(
                category="scope_mismatch",
                detail=str(scope_assessment.get("reason") or "Judge flagged scope mismatch"),
                severity="high" if scope_assessment.get("verdict") == "too_small" else "medium",
            )
        )
    if bool(judge_payload.get("missing_tests")):
        combined_findings.append(
            VerificationFinding(
                category="missing_tests",
                detail="Independent judge flagged missing tests for the claimed issue resolution",
                severity="high",
            )
        )
    failed_criteria = [item for item in criteria if item.verdict == "fail"]
    uncertain_criteria = [item for item in criteria if item.verdict == "uncertain"]
    verdict = "pass"
    if judge_verdict in {"block", "uncertain"} or failed_criteria or any(item.severity == "high" for item in combined_findings):
        verdict = "block"
    summary = str(judge_payload.get("summary") or "").strip() or (
        "Independent judge accepted the PR." if verdict == "pass" else "Independent judge could not verify the PR."
    )
    if verdict == "block" and not summary.endswith("."):
        summary += "."
    report = WorkVerificationReport(
        verdict=verdict,
        summary=summary,
        findings=combined_findings,
        criteria=criteria,
        issue_number=issue_number,
        judge_model_family=family,
        judge_model=model,
        judge_summary="; ".join(notes[:3]),
        metadata={
            "judge_verdict": judge_verdict,
            "scope_assessment": scope_assessment,
            "failed_criteria": len(failed_criteria),
            "uncertain_criteria": len(uncertain_criteria),
            "files_changed": risk.files_changed,
            "lines_changed": risk.lines_changed,
        },
    )
    _persist_report(cfg, repo, pr_number, report)
    return report


def format_block_comment(report: WorkVerificationReport) -> str:
    lines = [
        _BLOCK_COMMENT_MARKER,
        "## Auto-merge blocked: work verifier",
        "",
        report.summary,
        "",
    ]
    if report.findings:
        lines.append("### Findings")
        lines.extend(f"- `{item.category}`: {item.detail}" for item in report.findings[:8])
        lines.append("")
    if report.criteria:
        lines.append("### Acceptance Criteria Review")
        for item in report.criteria[:8]:
            lines.append(f"- `{item.verdict}` {item.criterion} — {item.reason}")
        lines.append("")
    lines.append("Use `/verify-override <repo> <pr_number> [reason]` to unblock with an audited operator override.")
    return "\n".join(lines).strip()


def send_block_telegram(cfg: dict, repo: str, pr_number: int, report: WorkVerificationReport) -> None:
    event = {
        "source": "work_verifier",
        "type": "work_verifier_block",
        "repo": repo,
        "pr_number": pr_number,
        "issue_number": report.issue_number,
        "summary": f"Work verifier blocked PR #{pr_number}: {report.summary}",
        "findings": [asdict(item) for item in report.findings[:8]],
        "dedup_key": f"work-verifier:{repo}:{pr_number}:{report.signature[:120]}",
    }
    severity = classify_severity(cfg, "work_verifier", event)
    route_incident(severity, event, cfg=cfg)
