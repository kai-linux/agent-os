import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import json

from orchestrator.agent_scorer import (
    build_degradation_findings,
    build_pipeline_anomaly_findings,
    compute_success_rates,
)


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


# --- Codex stabilization regression tests (task-20260414-220519) ---

from orchestrator.agent_scorer import filter_healthy_agents, ADAPTIVE_HEALTH_THRESHOLD


def test_codex_task_type_scoped_health_gate_passes_strong_debugging():
    """Codex with good debugging rate should not be gated even if overall rate is low."""
    records = []
    for i in range(5):
        records.append({"timestamp": _ts(i + 1), "agent": "codex", "status": "complete", "task_type": "debugging"})
    for i in range(5):
        records.append({"timestamp": _ts(i + 6), "agent": "codex", "status": "blocked", "blocker_code": "environment_failure", "task_type": "implementation"})

    import tempfile, json
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
        metrics_file = Path(f.name)

    try:
        from orchestrator.agent_scorer import _RECENT_RATE_CACHE
        _RECENT_RATE_CACHE.clear()
        healthy, skipped = filter_healthy_agents(
            ["codex", "claude"],
            metrics_file,
            threshold=ADAPTIVE_HEALTH_THRESHOLD,
            window_days=7,
            task_type="debugging",
            min_task_count=3,
        )
        assert "codex" in healthy, "codex should pass gate when debugging-specific rate is high"
        assert "codex" not in skipped
    finally:
        metrics_file.unlink(missing_ok=True)


def test_codex_small_sample_not_gated_by_strict_24h():
    """Codex with fewer than min_task_count tasks should not be filtered by the 24h gate."""
    records = [
        {"timestamp": _ts(1), "agent": "codex", "status": "blocked", "blocker_code": "environment_failure"},
        {"timestamp": _ts(2), "agent": "codex", "status": "blocked", "blocker_code": "environment_failure"},
    ]

    import tempfile, json
    with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
        metrics_file = Path(f.name)

    try:
        from orchestrator.agent_scorer import _RECENT_RATE_CACHE
        _RECENT_RATE_CACHE.clear()
        healthy, skipped = filter_healthy_agents(
            ["codex"],
            metrics_file,
            threshold=0.80,
            window_days=1,
            min_task_count=5,
        )
        assert "codex" in healthy, "codex with <5 tasks must not be gated"
    finally:
        metrics_file.unlink(missing_ok=True)


def test_codex_degradation_finding_scoped_to_task_type():
    """When codex fails on implementation but succeeds on debugging, no finding is emitted."""
    records = [
        {"timestamp": _ts(1), "agent": "codex", "github_repo": "acme/api", "status": "blocked", "blocker_code": "missing_context", "task_type": "implementation"},
        {"timestamp": _ts(2), "agent": "codex", "github_repo": "acme/api", "status": "blocked", "blocker_code": "missing_context", "task_type": "implementation"},
        {"timestamp": _ts(3), "agent": "codex", "github_repo": "acme/api", "status": "blocked", "blocker_code": "missing_context", "task_type": "implementation"},
        {"timestamp": _ts(4), "agent": "codex", "github_repo": "acme/api", "status": "blocked", "blocker_code": "missing_context", "task_type": "implementation"},
        {"timestamp": _ts(5), "agent": "codex", "github_repo": "acme/api", "status": "complete", "task_type": "debugging"},
        {"timestamp": _ts(6), "agent": "codex", "github_repo": "acme/api", "status": "complete", "task_type": "debugging"},
        {"timestamp": _ts(7), "agent": "codex", "github_repo": "acme/api", "status": "complete", "task_type": "debugging"},
        {"timestamp": _ts(8), "agent": "codex", "github_repo": "acme/api", "status": "complete", "task_type": "debugging"},
    ]

    findings = build_degradation_findings(records, preferred_task_type="debugging")
    assert findings == [], "No finding when preferred task_type slice is above threshold"


def _write_outcome_log(tmp_path: Path, records: list[dict]) -> dict:
    """Materialize a minimal cfg + outcome_attribution.jsonl for scorer tests."""
    metrics_dir = tmp_path / "runtime" / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    log = metrics_dir / "outcome_attribution.jsonl"
    log.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return {
        "root_dir": str(tmp_path),
        "github_projects": {
            "demo": {
                "repos": [
                    {
                        "key": "api",
                        "github_repo": "acme/api",
                        "local_repo": str(tmp_path),
                    }
                ]
            }
        },
    }


def test_pipeline_anomaly_detects_empty_outcome_check_ids(tmp_path):
    """When most merged PRs carry empty outcome_check_ids, emit a pipeline_anomaly
    finding so the groomer can file a debug ticket instead of another
    'configure more metrics' task."""
    records = [
        {
            "record_type": "attribution",
            "event": "merged",
            "repo": "acme/api",
            "pr_number": n,
            "outcome_check_ids": [],
            "timestamp": _ts(hours_ago),
            "merged_at": _ts(hours_ago),
        }
        for n, hours_ago in [(101, 10), (102, 20), (103, 30), (104, 40)]
    ]
    cfg = _write_outcome_log(tmp_path, records)

    findings = build_pipeline_anomaly_findings(cfg)
    assert len(findings) == 1
    f = findings[0]
    assert f["kind"] == "pipeline_anomaly"
    assert f["repo"] == "acme/api"
    assert f["metrics"]["merges_with_empty_ids"] == 4
    assert f["metrics"]["empty_ratio"] == 1.0
    assert "debug" in f["title_hint"].lower()
    assert any("pr_opened" in str(ev).lower() or "plumbing" in str(ev).lower()
               for ev in f["evidence"])


def test_pipeline_anomaly_quiet_when_ids_populated(tmp_path):
    """When outcome_check_ids are populated, no anomaly — plumbing is healthy."""
    records = [
        {
            "record_type": "attribution",
            "event": "merged",
            "repo": "acme/api",
            "pr_number": n,
            "outcome_check_ids": ["agent_success_rate", "github_stars"],
            "timestamp": _ts(hours_ago),
            "merged_at": _ts(hours_ago),
        }
        for n, hours_ago in [(201, 10), (202, 20), (203, 30), (204, 40)]
    ]
    cfg = _write_outcome_log(tmp_path, records)
    assert build_pipeline_anomaly_findings(cfg) == []


def test_pipeline_anomaly_quiet_below_min_merges(tmp_path):
    """Detector needs at least PIPELINE_ANOMALY_MIN_MERGES merges to fire — one
    empty PR is noise, not a pattern."""
    records = [
        {
            "record_type": "attribution",
            "event": "merged",
            "repo": "acme/api",
            "pr_number": 301,
            "outcome_check_ids": [],
            "timestamp": _ts(5),
            "merged_at": _ts(5),
        }
    ]
    cfg = _write_outcome_log(tmp_path, records)
    assert build_pipeline_anomaly_findings(cfg) == []
