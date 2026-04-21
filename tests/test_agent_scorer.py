import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import json

from orchestrator.agent_scorer import (
    build_debug_hypothesis_findings,
    build_degradation_findings,
    build_pipeline_anomaly_findings,
    build_recurring_risk_findings,
    build_retry_storm_findings,
    compute_success_rates,
)
from orchestrator.sprint_history import append_sprint_report


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


def _write_sprint_history(tmp_path: Path, repo: str, reports: list[dict]) -> dict:
    cfg = {
        "root_dir": str(tmp_path),
        "github_projects": {
            "demo": {
                "repos": [
                    {
                        "key": "api",
                        "github_repo": repo,
                        "local_repo": str(tmp_path),
                    }
                ]
            }
        },
    }
    for report in reports:
        append_sprint_report(cfg, repo, report)
    return cfg


def test_recurring_risk_detects_concern_across_sprints(tmp_path):
    """Same risk bullet across 3 sprint reports should trigger a recurring_risk
    finding so the groomer can convert the retro-only complaint into concrete
    backlog work."""
    reports = [
        {
            "headline": f"Sprint {i}",
            "movement_summary": "work shipped",
            "progress_points": ["shipped a thing"],
            "risks_and_gaps": [
                "Outcome-measurement coverage remains thin for planner work",
            ],
            "next_sprint_focus": ["ship more"],
        }
        for i in range(3)
    ]
    cfg = _write_sprint_history(tmp_path, "acme/api", reports)
    findings = build_recurring_risk_findings(cfg)
    assert len(findings) == 1
    f = findings[0]
    assert f["kind"] == "recurring_risk"
    assert f["repo"] == "acme/api"
    assert f["metrics"]["sprint_count"] == 3
    assert "outcome" in f["title_hint"].lower() or "measurement" in f["title_hint"].lower()


def test_recurring_risk_quiet_below_min_repeats(tmp_path):
    """Two occurrences of the same concern is not yet a pattern."""
    reports = [
        {
            "headline": f"Sprint {i}",
            "movement_summary": "work shipped",
            "progress_points": ["shipped a thing"],
            "risks_and_gaps": ["Outcome measurement thin"],
            "next_sprint_focus": ["ship more"],
        }
        for i in range(2)
    ]
    cfg = _write_sprint_history(tmp_path, "acme/api", reports)
    assert build_recurring_risk_findings(cfg) == []


def _write_agent_stats(tmp_path: Path, records: list[dict]) -> dict:
    metrics_dir = tmp_path / "runtime" / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    log = metrics_dir / "agent_stats.jsonl"
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


def test_retry_storm_flags_high_deep_retry_ratio(tmp_path):
    """When >=30% of recent tasks hit attempt 2 or deeper, emit a pipeline_anomaly
    finding so the operator investigates pipeline friction rather than letting
    the retry chain absorb it."""
    base_ts = _ts(5)
    records = [
        {"timestamp": base_ts, "task_id": f"task-20260101-120000-feature-retry-2", "github_repo": "acme/api", "agent": "codex", "status": "complete"},
        {"timestamp": base_ts, "task_id": f"task-20260102-120000-feature-retry-3", "github_repo": "acme/api", "agent": "claude", "status": "complete"},
        {"timestamp": base_ts, "task_id": f"task-20260103-120000-feature-retry-2", "github_repo": "acme/api", "agent": "gemini", "status": "complete"},
        {"timestamp": base_ts, "task_id": f"task-20260104-120000-feature", "github_repo": "acme/api", "agent": "codex", "status": "complete"},
        {"timestamp": base_ts, "task_id": f"task-20260105-120000-feature", "github_repo": "acme/api", "agent": "codex", "status": "complete"},
        {"timestamp": base_ts, "task_id": f"task-20260106-120000-feature-retry-1", "github_repo": "acme/api", "agent": "codex", "status": "complete"},
        {"timestamp": base_ts, "task_id": f"task-20260107-120000-feature", "github_repo": "acme/api", "agent": "codex", "status": "complete"},
    ]
    cfg = _write_agent_stats(tmp_path, records)
    findings = build_retry_storm_findings(cfg)
    assert len(findings) == 1
    f = findings[0]
    assert f["kind"] == "pipeline_anomaly"
    assert f["metrics"]["deep_retry_tasks"] == 3
    assert f["metrics"]["total_tasks"] == 7
    assert "retry" in f["title_hint"].lower()


def test_retry_storm_quiet_when_retries_are_shallow(tmp_path):
    """Attempt 0 and 1 are normal — only attempt 2+ should count."""
    base_ts = _ts(5)
    records = [
        {"timestamp": base_ts, "task_id": f"task-20260101-120000-feature-retry-1", "github_repo": "acme/api", "agent": "codex", "status": "complete"},
        {"timestamp": base_ts, "task_id": f"task-20260102-120000-feature", "github_repo": "acme/api", "agent": "codex", "status": "complete"},
        {"timestamp": base_ts, "task_id": f"task-20260103-120000-feature", "github_repo": "acme/api", "agent": "codex", "status": "complete"},
        {"timestamp": base_ts, "task_id": f"task-20260104-120000-feature-retry-1", "github_repo": "acme/api", "agent": "codex", "status": "complete"},
        {"timestamp": base_ts, "task_id": f"task-20260105-120000-feature", "github_repo": "acme/api", "agent": "codex", "status": "complete"},
        {"timestamp": base_ts, "task_id": f"task-20260106-120000-feature", "github_repo": "acme/api", "agent": "codex", "status": "complete"},
    ]
    cfg = _write_agent_stats(tmp_path, records)
    assert build_retry_storm_findings(cfg) == []


def test_debug_hypothesis_promoted_to_finding(tmp_path):
    """A concrete structural hypothesis in the latest retro should emit a
    pipeline_anomaly finding so the groomer converts it into a debug issue."""
    reports = [
        {
            "headline": "Sprint shipped 3 issues",
            "movement_summary": "work continued",
            "progress_points": ["shipped feature X"],
            "risks_and_gaps": ["outcome checks inconclusive"],
            "next_sprint_focus": ["ship feature Y"],
            "debug_hypothesis": (
                "outcome_check_ids are being dropped between github_dispatcher "
                "task creation and pr_monitor merge cleanup, because github_sync "
                "never runs when the agent opens a PR directly via gh pr create"
            ),
        }
    ]
    cfg = _write_sprint_history(tmp_path, "acme/api", reports)
    findings = build_debug_hypothesis_findings(cfg)
    assert len(findings) == 1
    f = findings[0]
    assert f["kind"] == "pipeline_anomaly"
    assert "retro hypothesis" in f["title_hint"].lower()
    assert "github_sync" in f["summary"] or "github_sync" in " ".join(str(e) for e in f["evidence"])


def test_debug_hypothesis_filters_generic_phrasings(tmp_path):
    """Vague hypotheses like 'quality could improve, more tests needed' must be
    filtered out — they are not actionable investigation starters."""
    reports = [
        {
            "headline": "Sprint shipped 2 issues",
            "movement_summary": "work continued",
            "progress_points": ["shipped feature X"],
            "risks_and_gaps": [],
            "next_sprint_focus": [],
            "debug_hypothesis": "quality could improve, more tests needed",
        }
    ]
    cfg = _write_sprint_history(tmp_path, "acme/api", reports)
    assert build_debug_hypothesis_findings(cfg) == []


def test_debug_hypothesis_quiet_when_empty(tmp_path):
    reports = [
        {
            "headline": "Sprint shipped 2 issues",
            "movement_summary": "work continued",
            "progress_points": ["shipped feature X"],
            "risks_and_gaps": [],
            "next_sprint_focus": [],
            "debug_hypothesis": "",
        }
    ]
    cfg = _write_sprint_history(tmp_path, "acme/api", reports)
    assert build_debug_hypothesis_findings(cfg) == []


def test_recurring_risk_quiet_when_concerns_differ(tmp_path):
    """Different concerns across sprints should not cluster."""
    risks = [
        "Quickstart friction slowed new users",
        "PR cycle time regressed this week",
        "Cross-repo coordination overhead spiked",
    ]
    reports = [
        {
            "headline": f"Sprint {i}",
            "movement_summary": "work shipped",
            "progress_points": ["shipped a thing"],
            "risks_and_gaps": [risks[i]],
            "next_sprint_focus": ["ship more"],
        }
        for i in range(3)
    ]
    cfg = _write_sprint_history(tmp_path, "acme/api", reports)
    assert build_recurring_risk_findings(cfg) == []
