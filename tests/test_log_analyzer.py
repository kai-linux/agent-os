import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.log_analyzer import (
    build_blocker_findings,
    build_issue_body,
    collect_structured_findings,
    dedupe_synthesized_issues,
)


def _ts(hours_ago: int) -> str:
    return (datetime.now(tz=timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def test_build_blocker_findings_emits_recent_spike():
    records = [
        {"timestamp": _ts(1), "repo": "kai-linux/agent-os", "status": "blocked", "blocker_code": "environment_failure"},
        {"timestamp": _ts(2), "repo": "kai-linux/agent-os", "status": "partial", "blocker_code": "environment_failure"},
        {"timestamp": _ts(3), "repo": "kai-linux/agent-os", "status": "blocked", "blocker_code": "environment_failure"},
        {"timestamp": _ts(30), "repo": "kai-linux/agent-os", "status": "blocked", "blocker_code": "environment_failure"},
    ]

    findings = build_blocker_findings(records, "kai-linux/agent-os")

    assert len(findings) == 1
    assert findings[0]["id"] == "blocker_spike:kai-linux/agent-os:environment_failure"
    assert findings[0]["count"] == 3


def test_collect_structured_findings_uses_scorer_artifact(tmp_path):
    artifact = tmp_path / "runtime" / "analysis" / "agent_scorer_findings.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text(
        """
{
  "generated_at": "2026-03-20T00:00:00+00:00",
  "findings": [
    {
      "id": "agent_degraded:codex",
      "source": "agent_scorer",
      "summary": "Codex dropped below threshold."
    }
  ]
}
""".strip()
        + "\n",
        encoding="utf-8",
    )

    findings = collect_structured_findings(tmp_path, [], "kai-linux/agent-os")

    assert findings == [
        {
            "id": "agent_degraded:codex",
            "source": "agent_scorer",
            "summary": "Codex dropped below threshold.",
        }
    ]


def test_dedupe_synthesized_issues_collapses_overlapping_evidence():
    issues = [
        {
            "title": "Investigate codex reliability",
            "evidence_ids": ["agent_degraded:codex", "metrics_window"],
        },
        {
            "title": "Investigate Codex reliability",
            "evidence_ids": ["agent_degraded:codex"],
        },
        {
            "title": "Harden runner failure recovery",
            "evidence_ids": ["queue_log_tail"],
        },
    ]

    deduped = dedupe_synthesized_issues(issues)

    assert deduped == [
        {
            "title": "Investigate codex reliability",
            "evidence_ids": ["agent_degraded:codex", "metrics_window"],
        },
        {
            "title": "Harden runner failure recovery",
            "evidence_ids": ["queue_log_tail"],
        },
    ]


def test_build_issue_body_includes_evidence_and_reasoning():
    issue = {
        "goal": "Restore codex routing reliability.",
        "success_criteria": ["Classify the main failure mode", "Implement one bounded mitigation"],
        "constraints": ["Prefer minimal diffs"],
        "next_steps": ["Update the repo-specific routing rule", "Verify the next scorer run improves"],
        "reasoning": "The same degradation signal appears across recent operational evidence.",
        "evidence_ids": ["agent_degraded:codex", "queue_log_tail"],
    }
    evidence_lookup = {
        "agent_degraded:codex": {
            "source": "agent_scorer",
            "summary": "Codex fell below the weekly success-rate threshold.",
        },
        "queue_log_tail": {
            "source": "runtime/logs/queue-summary.log",
            "summary": "Recent queue failures cluster around codex runs.",
        },
    }

    body = build_issue_body(issue, evidence_lookup)

    assert "## Next Steps" in body
    assert "- Update the repo-specific routing rule" in body
    assert "## Evidence" in body
    assert "`agent_degraded:codex` (agent_scorer): Codex fell below the weekly success-rate threshold." in body
    assert "## Reasoning" in body
    assert "The same degradation signal appears across recent operational evidence." in body
