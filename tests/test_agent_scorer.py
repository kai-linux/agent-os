import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.agent_scorer import build_degradation_findings, compute_success_rates


def _ts(hours_ago: int) -> str:
    return (datetime.now(tz=timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def test_build_degradation_findings_classifies_quota_and_targets_repo():
    records = [
        {"timestamp": _ts(1), "agent": "codex", "github_repo": "acme/api", "status": "blocked", "blocker_code": "quota_limited"},
        {"timestamp": _ts(2), "agent": "codex", "github_repo": "acme/api", "status": "blocked", "blocker_code": "quota_limited"},
        {"timestamp": _ts(3), "agent": "codex", "github_repo": "acme/api", "status": "partial", "blocker_code": "quota_limited"},
        {"timestamp": _ts(4), "agent": "codex", "github_repo": "acme/api", "status": "complete", "blocker_code": "none"},
        {"timestamp": _ts(5), "agent": "claude", "github_repo": "acme/api", "status": "complete", "blocker_code": "none"},
    ]

    findings = build_degradation_findings(records)

    assert len(findings) == 1
    finding = findings[0]
    assert finding["kind"] == "agent_remediation"
    assert finding["repo"] == "acme/api"
    assert finding["degradation_cause"] == "quota"
    assert finding["title_hint"] == "Reduce codex quota exhaustion in api"
    assert any("routing or fallback policy" in step for step in finding["next_steps"])


def test_build_degradation_findings_classifies_model_selection_from_peer_success():
    records = [
        {"timestamp": _ts(1), "agent": "codex", "github_repo": "acme/web", "status": "blocked", "blocker_code": "dependency_blocked"},
        {"timestamp": _ts(2), "agent": "codex", "github_repo": "acme/web", "status": "partial", "blocker_code": "dependency_blocked"},
        {"timestamp": _ts(3), "agent": "codex", "github_repo": "acme/web", "status": "blocked", "blocker_code": "dependency_blocked"},
        {"timestamp": _ts(4), "agent": "codex", "github_repo": "acme/web", "status": "complete", "blocker_code": "none"},
        {"timestamp": _ts(5), "agent": "claude", "github_repo": "acme/web", "status": "complete", "blocker_code": "none"},
        {"timestamp": _ts(6), "agent": "claude", "github_repo": "acme/web", "status": "complete", "blocker_code": "none"},
        {"timestamp": _ts(7), "agent": "claude", "github_repo": "acme/web", "status": "complete", "blocker_code": "none"},
    ]

    findings = build_degradation_findings(records)

    assert len(findings) == 1
    finding = findings[0]
    assert finding["degradation_cause"] == "model_selection"
    assert finding["title_hint"] == "Improve web routing away from codex"
    assert "claude" in finding["reasoning_hint"]
    assert any("prefer `claude`" in step for step in finding["next_steps"])


def test_sentinel_agents_excluded_from_degradation_analysis():
    """Sentinel agent names like 'none' and 'unknown' represent exhausted
    fallback chains, not real agents.  They must never produce degradation
    findings or appear in success-rate maps."""
    records = [
        {"timestamp": _ts(1), "agent": "none", "github_repo": "acme/api", "status": "blocked", "blocker_code": "fallback_exhausted"},
        {"timestamp": _ts(2), "agent": "none", "github_repo": "acme/api", "status": "blocked", "blocker_code": "fallback_exhausted"},
        {"timestamp": _ts(3), "agent": "none", "github_repo": "acme/api", "status": "blocked", "blocker_code": "fallback_exhausted"},
        {"timestamp": _ts(4), "agent": "none", "github_repo": "acme/api", "status": "blocked", "blocker_code": "fallback_exhausted"},
        {"timestamp": _ts(5), "agent": "unknown", "github_repo": "acme/api", "status": "blocked", "blocker_code": "fallback_exhausted"},
        {"timestamp": _ts(6), "agent": "claude", "github_repo": "acme/api", "status": "complete", "blocker_code": "none"},
    ]

    rates = compute_success_rates(records)
    assert "none" not in rates
    assert "unknown" not in rates
    assert "claude" in rates

    findings = build_degradation_findings(records)
    for f in findings:
        assert f["agent"] not in ("none", "unknown")


def test_build_degradation_findings_prefers_debugging_slice_for_codex():
    records = [
        {"timestamp": _ts(1), "agent": "codex", "github_repo": "acme/api", "status": "blocked", "blocker_code": "environment_failure", "task_type": "implementation"},
        {"timestamp": _ts(2), "agent": "codex", "github_repo": "acme/api", "status": "blocked", "blocker_code": "environment_failure", "task_type": "implementation"},
        {"timestamp": _ts(3), "agent": "codex", "github_repo": "acme/api", "status": "blocked", "blocker_code": "environment_failure", "task_type": "implementation"},
        {"timestamp": _ts(4), "agent": "codex", "github_repo": "acme/api", "status": "complete", "blocker_code": "none", "task_type": "debugging"},
        {"timestamp": _ts(5), "agent": "codex", "github_repo": "acme/api", "status": "complete", "blocker_code": "none", "task_type": "debugging"},
        {"timestamp": _ts(6), "agent": "codex", "github_repo": "acme/api", "status": "complete", "blocker_code": "none", "task_type": "debugging"},
        {"timestamp": _ts(7), "agent": "codex", "github_repo": "acme/api", "status": "complete", "blocker_code": "none", "task_type": "debugging"},
    ]

    findings = build_degradation_findings(records)

    assert findings == []
