"""Incident scanner tests.

The anchor test (`test_template_echo_incident_would_have_been_auto_detected`)
reconstructs the 2026-04-23 escalation that required manual diagnosis and
asserts the scanner's deterministic rule would have filed a self-fix issue
for it without an operator in the loop.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import orchestrator.incident_scanner as scanner


def _setup_runtime(tmp_path: Path) -> Path:
    root = tmp_path
    (root / "runtime" / "incidents").mkdir(parents=True, exist_ok=True)
    (root / "runtime" / "mailbox" / "escalated").mkdir(parents=True, exist_ok=True)
    (root / "runtime" / "audit").mkdir(parents=True, exist_ok=True)
    (root / "runtime" / "state").mkdir(parents=True, exist_ok=True)
    return root


def _now_iso(offset_hours: float = 0.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=offset_hours)).isoformat()


def test_collect_signals_reads_incidents(tmp_path: Path):
    root = _setup_runtime(tmp_path)
    (root / "runtime" / "incidents" / "incidents.jsonl").write_text(
        json.dumps({
            "ts": _now_iso(1),
            "source": "pr_monitor",
            "type": "stuck_pr_merge",
            "severity": "sev2",
            "summary": "PR #42 stuck for 4h on merge_conflict",
            "dedup_key": "stuck-pr:owner/repo:42:self_heal",
        }) + "\n",
        encoding="utf-8",
    )
    records = scanner.collect_signals(root, window_hours=24)
    assert len(records) == 1
    assert records[0].source == "incidents"
    assert records[0].category == "stuck_pr_merge"


def test_collect_signals_filters_by_window(tmp_path: Path):
    root = _setup_runtime(tmp_path)
    (root / "runtime" / "incidents" / "incidents.jsonl").write_text(
        json.dumps({"ts": _now_iso(72), "source": "x", "type": "old", "summary": "old"}) + "\n",
        encoding="utf-8",
    )
    assert scanner.collect_signals(root, window_hours=24) == []


def test_collect_signals_reads_escalation_notes(tmp_path: Path):
    root = _setup_runtime(tmp_path)
    note = root / "runtime" / "mailbox" / "escalated" / "task-demo-escalation.md"
    note.write_text(
        "# Escalation Note\n\n"
        "## Parent Task ID\ntask-demo\n\n"
        "## Error Patterns\n`One line. Required when STATUS is partial or blocked. "
        "Use `none` when STATUS is complete.` repeated 3 time(s)\n"
        "- \"bullet\" repeated 6 time(s)\n\n"
        "## Prompt Snapshot\nruntime/prompts/task-demo.txt\n",
        encoding="utf-8",
    )
    records = scanner.collect_signals(root, window_hours=24)
    assert len(records) == 1
    assert records[0].source == "escalation"
    assert "One line" in records[0].summary


def test_audit_events_only_captures_whitelisted_types(tmp_path: Path):
    root = _setup_runtime(tmp_path)
    (root / "runtime" / "audit" / "audit.jsonl").write_text(
        json.dumps({"ts": _now_iso(1), "event_type": "pr_e2e_terminal_close",
                    "payload": {"blocker_signature": "merge_conflict"}}) + "\n"
        + json.dumps({"ts": _now_iso(1), "event_type": "telegram_callback", "payload": {}}) + "\n",
        encoding="utf-8",
    )
    records = scanner.collect_signals(root, window_hours=24)
    assert len(records) == 1
    assert records[0].category == "pr_e2e_terminal_close"


def test_aggregate_signals_counts_and_samples():
    now = datetime.now(timezone.utc)
    records = [
        scanner.SignalRecord("escalation", now - timedelta(hours=h), "x", "sig-A", "sev2", f"msg {h}")
        for h in range(5)
    ] + [
        scanner.SignalRecord("escalation", now - timedelta(hours=2), "x", "sig-B", "sev2", "B"),
    ]
    agg = scanner.aggregate_signals(records)
    assert agg["sig-A"]["count"] == 5
    assert len(agg["sig-A"]["examples"]) == 3  # capped at 3
    assert agg["sig-B"]["count"] == 1


def test_template_echo_rule_detects_echoed_prose(tmp_path: Path):
    """Deterministic detector: if escalation error patterns contain the prompt
    template prose ("One line. Required when..."), the rule proposes a fix
    without needing the LLM.
    """
    now = datetime.now(timezone.utc)
    rec = scanner.SignalRecord(
        source="escalation",
        ts=now,
        category="blocked_task_escalation",
        signature="escalation:`One line. Required when STATUS is partial or blocked. Use `none` when ST",
        severity="sev2",
        summary="Task fix-blank-assignment escalated: `One line. Required when STATUS is partial or blocked...",
        context={},
    )
    agg = scanner.aggregate_signals([rec, rec])
    proposals = scanner._rule_template_echo(agg)
    assert len(proposals) == 1
    assert "template" in proposals[0].title.lower() or "agent_result" in proposals[0].title.lower()
    assert proposals[0].rule_name == "template_echo"


def test_repeated_terminal_close_rule_fires_at_threshold():
    now = datetime.now(timezone.utc)
    records = [
        scanner.SignalRecord(
            source="audit",
            ts=now - timedelta(hours=h),
            category="pr_e2e_terminal_close",
            signature="audit:pr_e2e_terminal_close:merge_conflict",
            severity="sev2",
            summary="pr_e2e_terminal_close merge_conflict",
        )
        for h in range(4)
    ]
    agg = scanner.aggregate_signals(records)
    proposals = scanner._rule_repeated_e2e_terminal_close(agg)
    assert len(proposals) == 1
    assert "merge_conflict" in proposals[0].title


def test_repeated_terminal_close_rule_holds_below_threshold():
    now = datetime.now(timezone.utc)
    records = [
        scanner.SignalRecord(
            source="audit", ts=now - timedelta(hours=h),
            category="pr_e2e_terminal_close",
            signature="audit:pr_e2e_terminal_close:merge_conflict",
            severity="sev2", summary="x",
        )
        for h in range(2)
    ]
    proposals = scanner._rule_repeated_e2e_terminal_close(scanner.aggregate_signals(records))
    assert proposals == []


def test_file_proposals_dedups_against_recent_state(tmp_path: Path):
    root = _setup_runtime(tmp_path)
    sig = "escalation:demo"
    # Pre-record a decision for the same signature 1h ago.
    scanner._record_scanner_decision(root, sig, "https://github.com/x/y/issues/1", "template_echo")
    proposal = scanner.FixProposal(signature=sig, title="X", body="Y", rule_name="template_echo")
    # Dry-run to avoid real gh calls
    results = scanner.file_proposals({}, root, [proposal], agent_os_repo="kai-linux/agent-os", dry_run=True)
    assert results[0][1] is None  # skipped


def test_template_echo_incident_would_have_been_auto_detected(tmp_path: Path, monkeypatch):
    """End-to-end replay of the 2026-04-23 template-echo incident.

    Given the escalation note that actually fired (reconstructed from the
    Telegram message), the scanner must:
    1. Parse the note into a SignalRecord.
    2. Match the template_echo deterministic rule.
    3. Produce a FixProposal targeting the parser/template enforcement.

    This replaces the manual diagnosis → fix loop that took hours of
    operator attention today.
    """
    root = _setup_runtime(tmp_path)
    # Reconstruct the actual escalation note shape (from the user's Telegram)
    note = root / "runtime" / "mailbox" / "escalated" / "task-20260423-090230-fix-blank-assignment-page-escalation.md"
    note.write_text(
        "# Escalation Note\n\n"
        "## Parent Task ID\ntask-20260423-090230-fix-blank-assignment-page\n\n"
        "## Repo\nkai-linux/eigendark-website\n\n"
        "## Error Patterns\n"
        "`One line. Required when STATUS is partial or blocked. Use `none` when STATUS is complete.` "
        "repeated 3 time(s)\n"
        "- \"bullet\" repeated 6 time(s)\n\n"
        "## Prompt Snapshot\n"
        "runtime/prompts/task-20260423-090230-fix-blank-assignment-page.txt\n",
        encoding="utf-8",
    )
    # Emulate the issue-already-open check returning False so the proposal
    # would actually be filed.
    monkeypatch.setattr(scanner, "_open_issue_with_title_exists", lambda *args, **kwargs: False)
    filed: list[dict] = []

    def fake_create_issue(repo, title, body, labels):
        filed.append({"repo": repo, "title": title, "body": body, "labels": list(labels)})
        return f"https://github.com/{repo}/issues/999"

    monkeypatch.setattr(scanner, "_create_issue", fake_create_issue)
    monkeypatch.setattr(scanner, "append_audit_event", lambda *a, **kw: None)

    records = scanner.collect_signals(root, window_hours=24)
    aggregates = scanner.aggregate_signals(records)

    proposals: list[scanner.FixProposal] = []
    for rule in scanner.DETERMINISTIC_RULES:
        proposals.extend(rule(aggregates))

    assert proposals, "scanner must produce a fix proposal for the echoed template"
    assert any(p.rule_name == "template_echo" for p in proposals)

    scanner.file_proposals({}, root, proposals, agent_os_repo="kai-linux/agent-os", dry_run=False)
    assert filed, "proposal must result in a filed GitHub issue"
    assert filed[0]["repo"] == "kai-linux/agent-os"
    assert "autonomous-fix" in filed[0]["labels"]
    assert "ready" in filed[0]["labels"]
