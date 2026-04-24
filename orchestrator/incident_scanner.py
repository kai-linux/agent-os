"""Autonomous incident scanner.

Reads the last N hours of incident signals (incidents.jsonl, escalation
notes, audit events) and converts recurring patterns into self-fix
GitHub issues in the agent-os repo. The dispatcher/groomer pipeline
then picks up those issues like any other work item, closing the loop
between "something went wrong at runtime" and "agent fixes the class
of bug."

Design principles:

- Deterministic rule matchers run first so known-bad patterns (e.g.
  agents echoing the `.agent_result.md` prompt template into the
  blocker-code field) don't need an LLM call to be diagnosed.
- An LLM fallback handles unclassified recurring signatures so new
  failure classes aren't ignored — but the LLM only gets aggregated
  signatures, not raw logs, and only when the deterministic rules
  couldn't classify them.
- Dedup is two-layered: against the scanner's own recent-action log
  (prevent double-filing in the same 24h window) and against open
  issues in agent-os with matching signature (prevent double-filing
  across scanner runs).

The scanner creates issues; it never edits code, merges PRs, or
changes branches. Everything else is the existing pipeline's job.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from orchestrator.audit_log import append_audit_event
from orchestrator.paths import ROOT, load_config

SCANNER_STATE_FILENAME = "incident_scanner_state.jsonl"
SCANNER_WINDOW_HOURS_DEFAULT = 24
SCANNER_MIN_OCCURRENCES_DEFAULT = 2
ISSUE_LABELS = ["ready", "prio:high", "bot-generated", "autonomous-fix"]
ISSUE_TITLE_PREFIX = "[auto-fix] "


@dataclass
class SignalRecord:
    """Normalized view of one incident signal from any source."""
    source: str          # "incidents" | "escalation" | "audit"
    ts: datetime
    category: str        # e.g. "stuck_pr_merge", "template_echo", "pr_e2e_terminal_close"
    signature: str       # stable key across similar incidents
    severity: str        # "sev1" | "sev2" | "sev3" | ""
    summary: str         # short human-readable line
    context: dict = field(default_factory=dict)  # additional fields for issue body


@dataclass
class FixProposal:
    signature: str
    title: str
    body: str
    rule_name: str  # which deterministic rule matched, or "llm" for the fallback


# ---------------------------------------------------------------------------
# Signal ingestion
# ---------------------------------------------------------------------------

def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _incidents_records(root: Path, since: datetime) -> list[SignalRecord]:
    path = root / "runtime" / "incidents" / "incidents.jsonl"
    records: list[SignalRecord] = []
    for row in _read_jsonl(path):
        ts = _parse_iso(row.get("ts") or row.get("timestamp"))
        if not ts or ts < since:
            continue
        source = str(row.get("source") or "incident")
        category = str(row.get("type") or "incident")
        severity = str(row.get("severity") or "").lower()
        summary = str(row.get("summary") or "").strip()[:200]
        sig = str(row.get("dedup_key") or f"{source}:{category}:{summary[:60]}")
        records.append(SignalRecord(
            source="incidents",
            ts=ts,
            category=category,
            signature=sig,
            severity=severity,
            summary=summary,
            context={"raw": row},
        ))
    return records


def _audit_records(root: Path, since: datetime, event_types: set[str]) -> list[SignalRecord]:
    path = root / "runtime" / "audit" / "audit.jsonl"
    records: list[SignalRecord] = []
    for row in _read_jsonl(path):
        et = str(row.get("event_type") or "")
        if et not in event_types:
            continue
        ts = _parse_iso(row.get("ts") or row.get("timestamp"))
        if not ts or ts < since:
            continue
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else row
        sub = str(payload.get("blocker_signature") or payload.get("reason") or "")[:80]
        sig = f"audit:{et}:{sub}" if sub else f"audit:{et}"
        records.append(SignalRecord(
            source="audit",
            ts=ts,
            category=et,
            signature=sig,
            severity="sev2",
            summary=f"{et} {sub}".strip(),
            context={"raw": row},
        ))
    return records


_ESCALATION_FIELD_RE = re.compile(r"^##\s+([^\n]+)\s*\n([\s\S]*?)(?=\n##\s|\Z)", re.MULTILINE)


def _parse_escalation_note(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    fields: dict[str, str] = {}
    for match in _ESCALATION_FIELD_RE.finditer(text):
        key = match.group(1).strip()
        value = match.group(2).strip()
        fields[key] = value
    return fields


def _escalation_records(root: Path, since: datetime) -> list[SignalRecord]:
    escalated_dir = root / "runtime" / "mailbox" / "escalated"
    if not escalated_dir.is_dir():
        return []
    cutoff = since.timestamp()
    records: list[SignalRecord] = []
    for note in sorted(escalated_dir.glob("*-escalation.md")):
        try:
            if note.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        fields = _parse_escalation_note(note)
        task_id = fields.get("Parent Task ID") or note.stem
        error_patterns = fields.get("Error Patterns") or fields.get("Error patterns") or ""
        prompt_snapshot_rel = fields.get("Prompt Snapshot") or ""
        # Signature keys on the first error-pattern line so a recurring
        # template-echo or repeated verifier-block aggregates across tasks.
        first_pattern_line = next(
            (line for line in error_patterns.splitlines() if line.strip()),
            "",
        ).strip()
        sig = f"escalation:{first_pattern_line[:80]}" if first_pattern_line else f"escalation:{task_id}"
        records.append(SignalRecord(
            source="escalation",
            ts=datetime.fromtimestamp(note.stat().st_mtime, tz=timezone.utc),
            category="blocked_task_escalation",
            signature=sig,
            severity="sev2",
            summary=f"Task {task_id} escalated: {first_pattern_line[:100]}",
            context={
                "task_id": task_id,
                "error_patterns": error_patterns,
                "prompt_snapshot": prompt_snapshot_rel,
                "note_path": str(note),
            },
        ))
    return records


def collect_signals(root: Path, window_hours: int = SCANNER_WINDOW_HOURS_DEFAULT) -> list[SignalRecord]:
    since = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    audit_events = {
        "pr_e2e_terminal_close",
        "work_verifier_override",
        "stuck_pr_merge",
    }
    return [
        *_incidents_records(root, since),
        *_audit_records(root, since, audit_events),
        *_escalation_records(root, since),
    ]


# ---------------------------------------------------------------------------
# Deterministic rule matchers
# ---------------------------------------------------------------------------

_TEMPLATE_ECHO_MARKERS = (
    "one line.",
    "required when status",
    "- bullet",
    "one short paragraph",
)


def _rule_template_echo(aggregates: dict[str, dict]) -> list[FixProposal]:
    """Detect `.agent_result.md` prompt template echoed as content.

    When an agent copies the template placeholders verbatim, error-pattern
    lines in escalation notes contain things like "One line. Required when
    STATUS..." — those are unmistakably template prose, not a real error.
    """
    proposals: list[FixProposal] = []
    for signature, agg in aggregates.items():
        examples_text = " ".join(r.summary.lower() for r in agg["examples"])
        if not any(marker in examples_text for marker in _TEMPLATE_ECHO_MARKERS):
            continue
        title = f"{ISSUE_TITLE_PREFIX}Agent echoed .agent_result.md template as blocker/summary"
        body = (
            "## Goal\n"
            "Tighten `.agent_result.md` enforcement so agents cannot copy the prompt "
            "template placeholders (`<one of: ...>`, `<one blocker code...>`, `- None`) "
            "into their answer without the parser rejecting the whole contract.\n\n"
            "## Signal\n"
            f"- Scanner signature: `{signature}`\n"
            f"- Occurrences in last 24h: {agg['count']}\n"
            f"- Example summary: {agg['examples'][0].summary[:200]}\n\n"
            "## Success Criteria\n"
            "- Parser detects template-echo patterns (prose in BLOCKER_CODE field, "
            "placeholder text like `- bullet` in DONE/BLOCKERS/etc.) and returns "
            "`invalid_result_contract`.\n"
            "- Added regression test with a fixture `.agent_result.md` that contains "
            "the template echoed verbatim; parser must reject it.\n"
            "- Existing tests still pass.\n\n"
            "## Constraints\n"
            "- Do not change the template itself as part of this task (that has been "
            "updated separately). Focus on the parser-side defense.\n"
            "- Keep the diff minimal. No broad refactor.\n"
        )
        proposals.append(FixProposal(signature=signature, title=title, body=body, rule_name="template_echo"))
    return proposals


def _rule_repeated_e2e_terminal_close(aggregates: dict[str, dict]) -> list[FixProposal]:
    """Detect the same blocker_signature hitting the e2e terminal close path
    repeatedly — means the underlying class of bug isn't being fixed by the
    dispatcher's re-spawn, and likely needs an orchestrator-level fix.
    """
    proposals: list[FixProposal] = []
    for signature, agg in aggregates.items():
        if not signature.startswith("audit:pr_e2e_terminal_close"):
            continue
        if agg["count"] < 3:
            continue
        blocker_sig = signature.split(":", 2)[-1]
        title = f"{ISSUE_TITLE_PREFIX}Repeated e2e terminal close on `{blocker_sig}` — root-cause fix needed"
        body = (
            "## Goal\n"
            f"The pr_monitor e2e-health gate has terminal-closed PRs stuck on the "
            f"blocker signature `{blocker_sig}` at least {agg['count']} times in the "
            "last 24 hours. Re-spawning isn't solving it — the underlying cause needs "
            "a code fix in the orchestrator or in the relevant per-project repo.\n\n"
            "## Signal\n"
            f"- Scanner signature: `{signature}`\n"
            f"- Occurrences: {agg['count']}\n"
            "- Sample audit events: see `runtime/audit/audit.jsonl` "
            "(filter event_type=pr_e2e_terminal_close).\n\n"
            "## Success Criteria\n"
            "- Identify the actual root cause of the blocker (inspect the logs + "
            "diffs of the closed PRs).\n"
            "- Land a fix that prevents this exact blocker signature from recurring.\n"
            "- Add a regression test pinning the fix.\n\n"
            "## Constraints\n"
            "- The fix may be in `orchestrator/`, in the affected product repo, or "
            "both. Prefer the smallest surface that prevents recurrence.\n"
        )
        proposals.append(FixProposal(signature=signature, title=title, body=body, rule_name="repeated_terminal_close"))
    return proposals


DETERMINISTIC_RULES = [_rule_template_echo, _rule_repeated_e2e_terminal_close]


# ---------------------------------------------------------------------------
# LLM fallback
# ---------------------------------------------------------------------------

def _llm_fallback_enabled() -> bool:
    # Allow operator to disable in case of billing concerns.
    return os.environ.get("AGENT_OS_INCIDENT_SCANNER_DISABLE_LLM", "").lower() not in {"1", "true", "yes"}


def _call_architect(prompt: str) -> str:
    claude_bin = os.environ.get("CLAUDE_BIN", "claude")
    result = subprocess.run(
        [claude_bin, "-p", prompt, "--model", "claude-sonnet-4-6"],
        capture_output=True, text=True, timeout=180, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"architect call failed: {(result.stderr or result.stdout).strip()[:200]}")
    return (result.stdout or "").strip()


_LLM_PROMPT = """You are the Agent OS incident triage. Aggregated incident signatures from the last 24 hours are below. For each signature decide if a code-level fix inside the Agent OS orchestrator (or a product repo it manages) is likely to prevent recurrence. Skip signatures that are external outages, operator-gated, or genuinely one-off.

Output a JSON array (no code fences). Each element must be:
{"signature": "<verbatim>", "title": "<PR-friendly title <=80 chars>", "body": "<full issue body with Goal / Signal / Success Criteria / Constraints sections>"}

If none are actionable, output [].

Signatures:
"""


def _llm_fallback(aggregates: dict[str, dict], already_proposed: set[str]) -> list[FixProposal]:
    if not _llm_fallback_enabled():
        return []
    remaining = {
        sig: agg for sig, agg in aggregates.items()
        if sig not in already_proposed and agg["count"] >= SCANNER_MIN_OCCURRENCES_DEFAULT
    }
    if not remaining:
        return []
    prompt_parts = [_LLM_PROMPT]
    for signature, agg in remaining.items():
        prompt_parts.append(
            f"- signature: {signature}\n  count: {agg['count']}\n  example: {agg['examples'][0].summary[:240]}\n"
        )
    try:
        raw = _call_architect("\n".join(prompt_parts))
    except Exception as e:
        print(f"  LLM fallback skipped: {e}")
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        print(f"  LLM fallback returned non-JSON; skipping.")
        return []
    proposals: list[FixProposal] = []
    for entry in parsed or []:
        sig = str(entry.get("signature") or "").strip()
        title = str(entry.get("title") or "").strip()
        body = str(entry.get("body") or "").strip()
        if not sig or not title or not body:
            continue
        if not title.startswith(ISSUE_TITLE_PREFIX):
            title = ISSUE_TITLE_PREFIX + title
        proposals.append(FixProposal(signature=sig, title=title, body=body, rule_name="llm"))
    return proposals


# ---------------------------------------------------------------------------
# Aggregation, dedup, issue filing
# ---------------------------------------------------------------------------

def aggregate_signals(records: list[SignalRecord]) -> dict[str, dict]:
    buckets: dict[str, dict] = defaultdict(lambda: {"count": 0, "examples": [], "severities": Counter()})
    for record in sorted(records, key=lambda r: r.ts):
        bucket = buckets[record.signature]
        bucket["count"] += 1
        if len(bucket["examples"]) < 3:
            bucket["examples"].append(record)
        if record.severity:
            bucket["severities"][record.severity] += 1
    return dict(buckets)


def _scanner_state_path(root: Path) -> Path:
    return root / "runtime" / "state" / SCANNER_STATE_FILENAME


def _recent_already_filed(root: Path, signature: str, hours: int = 72) -> bool:
    path = _scanner_state_path(root)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    for row in _read_jsonl(path):
        if row.get("signature") != signature:
            continue
        ts = _parse_iso(row.get("ts"))
        if ts and ts >= cutoff:
            return True
    return False


def _record_scanner_decision(root: Path, signature: str, issue_url: str | None, rule_name: str) -> None:
    path = _scanner_state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(),
            "signature": signature,
            "issue_url": issue_url,
            "rule_name": rule_name,
        }) + "\n")


def _open_issue_with_title_exists(repo: str, title: str) -> bool:
    try:
        result = subprocess.run(
            ["gh", "issue", "list", "--repo", repo, "--state", "open", "--search", title, "--json", "title", "--limit", "20"],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if result.returncode != 0:
            return False
        for row in json.loads(result.stdout or "[]"):
            if str(row.get("title", "")).strip() == title.strip():
                return True
    except Exception:
        return False
    return False


def _create_issue(repo: str, title: str, body: str, labels: list[str]) -> str:
    cmd = ["gh", "issue", "create", "--repo", repo, "--title", title, "--body", body]
    for label in labels:
        cmd += ["--label", label]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"gh issue create failed: {(result.stderr or '').strip()[:200]}")
    return (result.stdout or "").strip()


def file_proposals(cfg: dict, root: Path, proposals: list[FixProposal], *, agent_os_repo: str, dry_run: bool = False) -> list[tuple[FixProposal, str | None]]:
    results: list[tuple[FixProposal, str | None]] = []
    for proposal in proposals:
        if _recent_already_filed(root, proposal.signature):
            print(f"  Skip (already filed recently): {proposal.title}")
            results.append((proposal, None))
            continue
        if _open_issue_with_title_exists(agent_os_repo, proposal.title):
            print(f"  Skip (open issue exists): {proposal.title}")
            _record_scanner_decision(root, proposal.signature, None, proposal.rule_name)
            results.append((proposal, None))
            continue
        if dry_run:
            print(f"  Would file: {proposal.title}")
            results.append((proposal, "DRYRUN"))
            continue
        try:
            url = _create_issue(agent_os_repo, proposal.title, proposal.body, ISSUE_LABELS)
        except Exception as e:
            print(f"  Failed to file issue for {proposal.signature}: {e}")
            results.append((proposal, None))
            continue
        _record_scanner_decision(root, proposal.signature, url, proposal.rule_name)
        try:
            append_audit_event(cfg, "incident_scanner_issue_created", {
                "signature": proposal.signature,
                "title": proposal.title,
                "issue_url": url,
                "rule_name": proposal.rule_name,
            })
        except Exception:
            pass
        print(f"  Filed: {url} ({proposal.rule_name})")
        results.append((proposal, url))
    return results


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _resolve_agent_os_repo(cfg: dict) -> str:
    projects = cfg.get("github_projects") or {}
    for project_cfg in projects.values():
        if not isinstance(project_cfg, dict):
            continue
        for repo_cfg in project_cfg.get("repos", []):
            gh = str(repo_cfg.get("github_repo") or "")
            if gh.endswith("/agent-os"):
                return gh
    return "kai-linux/agent-os"


def run(*, window_hours: int = SCANNER_WINDOW_HOURS_DEFAULT, dry_run: bool = False) -> None:
    cfg = load_config()
    root = Path(cfg.get("root_dir", ROOT)).expanduser()
    agent_os_repo = _resolve_agent_os_repo(cfg)
    print(f"Incident scanner: window={window_hours}h, target={agent_os_repo}")

    records = collect_signals(root, window_hours=window_hours)
    if not records:
        print("No incident signals in window.")
        return
    aggregates = aggregate_signals(records)
    print(f"Aggregated {len(records)} signal(s) into {len(aggregates)} signature(s).")

    all_proposals: list[FixProposal] = []
    matched_signatures: set[str] = set()
    for rule in DETERMINISTIC_RULES:
        for proposal in rule(aggregates):
            all_proposals.append(proposal)
            matched_signatures.add(proposal.signature)

    all_proposals.extend(_llm_fallback(aggregates, matched_signatures))
    if not all_proposals:
        print("No actionable patterns detected.")
        return

    file_proposals(cfg, root, all_proposals, agent_os_repo=agent_os_repo, dry_run=dry_run)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Scan recent incident signals and file self-fix issues.")
    parser.add_argument("--window-hours", type=int, default=SCANNER_WINDOW_HOURS_DEFAULT)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(window_hours=args.window_hours, dry_run=args.dry_run)
