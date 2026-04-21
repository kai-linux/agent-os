"""Weekly agent performance scorer.

Reads runtime/metrics/agent_stats.jsonl, computes per-agent success rates
over the past 7 days, and emits structured degradation findings for the
log analyzer's evidence-synthesis flow.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from orchestrator.objectives import load_repo_objective, objective_metrics, score_objective_snapshots
from orchestrator.incident_router import classify_severity, escalate as route_incident
from orchestrator.outcome_attribution import load_outcome_records
from orchestrator.paths import load_config
from orchestrator.repo_modes import is_dispatcher_only_repo
from orchestrator.sprint_history import find_recurring_concerns, load_sprint_history
from orchestrator.system_architect import build_system_architect_findings


GATE_DECISIONS_FILENAME = "health_gate_decisions.jsonl"
DEGRADED_THRESHOLD = 0.60
WINDOW_DAYS = 7
# Sentinel agent names that represent exhausted fallback chains, not real agents.
# These must be excluded from degradation analysis to avoid spurious remediation tasks.
_SENTINEL_AGENTS = frozenset({"none", "unknown"})
# Blocker codes that describe transient provider-side or infra problems, not
# agent quality. These must not count against an agent's success rate — a model
# that hit its own rate limit today is still capable tomorrow. Excluding them
# here also retroactively heals the gate: if the same model is still the only
# fallback left after a peer rate-limited, the chain stays intact.
_TRANSIENT_BLOCKER_CODES = frozenset({"quota_limited", "runner_failure", "fallback_exhausted"})
HEALTH_CHECK_WINDOW_DAYS = 1
HEALTHY_SUCCESS_RATE_THRESHOLD = 0.80
ADAPTIVE_HEALTH_WINDOW_DAYS = 7
ADAPTIVE_HEALTH_THRESHOLD = 0.25
MIN_TASKS_FOR_DEGRADED_FINDING = 4
MIN_PEER_TASKS_FOR_MODEL_SELECTION = 3
FINDINGS_FILENAME = "agent_scorer_findings.json"
BLOCKER_CAUSE_MAP = {
    "quota_limited": "quota",
    "missing_credentials": "authentication",
    "environment_failure": "environment",
    "runner_failure": "environment",
    "workflow_validation_failed": "environment",
    "test_failure": "environment",
}
BUSINESS_SCORE_THRESHOLD = -0.15
_RECENT_RATE_CACHE: dict[tuple[str, int, float, str], dict[str, dict]] = {}


def _normalize_task_type(task_type: str | None) -> str:
    return str(task_type or "").strip().lower()


def load_recent_metrics(metrics_file: Path, window_days: int = WINDOW_DAYS) -> list[dict]:
    if not metrics_file.exists():
        return []
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=window_days)
    records = []
    with metrics_file.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts_raw = rec.get("timestamp", "")
            try:
                ts = datetime.fromisoformat(ts_raw)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue
            if ts >= cutoff:
                records.append(rec)
    return records


def compute_success_rates(
    records: list[dict],
    *,
    task_type: str | None = None,
    exclude_transient: bool = False,
) -> dict[str, dict]:
    """Return {agent: {total, successes, rate}} for agents with >= 1 task.

    Sentinel agent names (e.g. ``"none"``, ``"unknown"``) that represent
    exhausted fallback chains rather than real agents are excluded from the
    result so they cannot trigger spurious degradation findings.

    ``exclude_transient=True`` drops records whose ``blocker_code`` is in
    ``_TRANSIENT_BLOCKER_CODES`` (quota/runner/fallback-exhausted). This is what
    the fallback-chain health gate should use — a rate-limited model isn't an
    unhealthy model. Leave it False for operator-facing degradation findings so
    the analyst can still see "codex is being rate-limited a lot in repo X".
    """
    selected_task_type = _normalize_task_type(task_type)
    counts: dict[str, dict] = defaultdict(lambda: {"total": 0, "successes": 0})
    for rec in records:
        if selected_task_type and _normalize_task_type(rec.get("task_type")) != selected_task_type:
            continue
        agent = rec.get("agent", "unknown")
        if agent in _SENTINEL_AGENTS:
            continue
        if exclude_transient and str(rec.get("blocker_code", "")).strip() in _TRANSIENT_BLOCKER_CODES:
            continue
        counts[agent]["total"] += 1
        if rec.get("status") == "complete":
            counts[agent]["successes"] += 1
    return {
        agent: {**v, "rate": v["successes"] / v["total"] if v["total"] else 0.0}
        for agent, v in counts.items()
    }


def recent_success_rates(
    metrics_file: Path,
    window_days: float = HEALTH_CHECK_WINDOW_DAYS,
    *,
    task_type: str | None = None,
) -> dict[str, dict]:
    """Return cached recent success rates keyed by agent."""
    try:
        mtime_ns = metrics_file.stat().st_mtime_ns
    except FileNotFoundError:
        mtime_ns = -1
    normalized_task_type = _normalize_task_type(task_type)
    cache_key = (str(metrics_file), mtime_ns, window_days, normalized_task_type)
    cached = _RECENT_RATE_CACHE.get(cache_key)
    if cached is not None:
        return cached

    rates = compute_success_rates(
        load_recent_metrics(metrics_file, window_days=window_days),
        task_type=normalized_task_type or None,
        # The health gate must not punish transient provider failures (rate
        # limits, runner crashes). Degradation findings still count them.
        exclude_transient=True,
    )
    _RECENT_RATE_CACHE.clear()
    _RECENT_RATE_CACHE[cache_key] = rates
    return rates


def log_gate_decision(
    metrics_dir: Path,
    *,
    gate: str,
    skipped: dict[str, dict],
    passed: list[str],
    context: str = "",
) -> None:
    """Append a gate decision record to the audit log."""
    log_path = metrics_dir / GATE_DECISIONS_FILENAME
    record = {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "gate": gate,
        "skipped": {
            agent: {"total": s["total"], "successes": s["successes"], "rate": round(s["rate"], 4)}
            for agent, s in skipped.items()
        },
        "passed": passed,
        "context": context,
    }
    try:
        metrics_dir.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except OSError:
        pass


def filter_healthy_agents(
    agents: list[str],
    metrics_file: Path,
    *,
    threshold: float = HEALTHY_SUCCESS_RATE_THRESHOLD,
    window_days: float = HEALTH_CHECK_WINDOW_DAYS,
    task_type: str | None = None,
    min_task_count: int = 3,
) -> tuple[list[str], dict[str, dict]]:
    """Return healthy agents and skipped-agent stats for the recent metrics window."""
    overall_rates = recent_success_rates(metrics_file, window_days=window_days)
    scoped_task_type = _normalize_task_type(task_type)
    scoped_rates = (
        recent_success_rates(metrics_file, window_days=window_days, task_type=scoped_task_type)
        if scoped_task_type
        else {}
    )
    healthy: list[str] = []
    skipped: dict[str, dict] = {}
    for agent in agents:
        stats = overall_rates.get(agent)
        scope = "overall"
        scoped_stats = scoped_rates.get(agent)
        if scoped_stats and scoped_stats.get("total", 0) >= min_task_count:
            stats = scoped_stats
            scope = scoped_task_type
        if stats and stats.get("total", 0) >= min_task_count and stats.get("rate", 0.0) <= threshold:
            skipped[agent] = {**stats, "scope": scope}
            continue
        healthy.append(agent)
    return healthy, skipped


def degraded_agents(rates: dict[str, dict], threshold: float = DEGRADED_THRESHOLD) -> list[tuple[str, float, int]]:
    """Return [(agent, rate, total)] for agents below threshold."""
    out = []
    for agent, stats in rates.items():
        if stats["total"] > 0 and stats["rate"] < threshold:
            out.append((agent, stats["rate"], stats["total"]))
    return sorted(out, key=lambda x: x[1])


def issue_title(agent: str, rate: float) -> str:
    pct = round(rate * 100)
    return f"Agent {agent} degraded ({pct}% success rate)"


def _repo_slug(rec: dict) -> str:
    return str(rec.get("github_repo", "")).strip()


def _repo_display_name(repo_slug: str, records: list[dict]) -> str:
    if repo_slug:
        return repo_slug.rsplit("/", 1)[-1]
    for rec in records:
        repo_path = str(rec.get("repo", "")).strip()
        if repo_path:
            return Path(repo_path).name
    return "the repo"


def _pick_target_repo(agent_records: list[dict], failures: list[dict]) -> str:
    candidates = failures or agent_records
    counts: dict[str, int] = defaultdict(int)
    for rec in candidates:
        repo_slug = _repo_slug(rec)
        if repo_slug:
            counts[repo_slug] += 1
    if not counts:
        return ""
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]


def _blocker_cause_counts(records: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for rec in records:
        cause = BLOCKER_CAUSE_MAP.get(str(rec.get("blocker_code", "")).strip().lower())
        if cause:
            counts[cause] += 1
    return dict(counts)


def _best_peer_for_repo(records: list[dict], repo_slug: str, degraded_agent: str) -> tuple[str, dict] | tuple[None, None]:
    if not repo_slug:
        return None, None
    repo_records = [rec for rec in records if _repo_slug(rec) == repo_slug and rec.get("agent") != degraded_agent]
    rates = compute_success_rates(repo_records)
    eligible = [
        (agent, stats)
        for agent, stats in rates.items()
        if stats["total"] >= MIN_PEER_TASKS_FOR_MODEL_SELECTION
    ]
    if not eligible:
        return None, None
    eligible.sort(key=lambda item: (-item[1]["rate"], -item[1]["total"], item[0]))
    return eligible[0]


def _classify_degradation_cause(
    *,
    agent: str,
    agent_rate: float,
    records: list[dict],
    agent_records: list[dict],
    failures: list[dict],
    target_repo: str,
) -> tuple[str, dict]:
    repo_failures = [rec for rec in failures if _repo_slug(rec) == target_repo] if target_repo else list(failures)
    blocker_counts = _blocker_cause_counts(repo_failures or failures)
    if blocker_counts:
        dominant_cause, dominant_count = sorted(
            blocker_counts.items(),
            key=lambda item: (-item[1], item[0]),
        )[0]
        if dominant_count >= max(2, len(repo_failures or failures) // 2):
            return dominant_cause, {"dominant_count": dominant_count, "blocker_counts": blocker_counts}

    peer_agent, peer_stats = _best_peer_for_repo(records, target_repo, agent)
    if peer_agent and peer_stats and peer_stats["rate"] >= DEGRADED_THRESHOLD and (peer_stats["rate"] - agent_rate) >= 0.25:
        return "model_selection", {"peer_agent": peer_agent, "peer_stats": peer_stats}

    if blocker_counts:
        dominant_cause, dominant_count = sorted(
            blocker_counts.items(),
            key=lambda item: (-item[1], item[0]),
        )[0]
        return dominant_cause, {"dominant_count": dominant_count, "blocker_counts": blocker_counts}

    if target_repo:
        peer_agent, peer_stats = _best_peer_for_repo(records, target_repo, agent)
        if peer_agent and peer_stats:
            return "model_selection", {"peer_agent": peer_agent, "peer_stats": peer_stats}

    return "environment", {}


def _finding_title(agent: str, cause: str, repo_name: str) -> str:
    if cause == "quota":
        return f"Reduce {agent} quota exhaustion in {repo_name}"
    if cause == "authentication":
        return f"Fix {agent} auth failures in {repo_name}"
    if cause == "model_selection":
        return f"Improve {repo_name} routing away from {agent}"
    return f"Stabilize {agent} runtime in {repo_name}"


def _finding_plan(
    *,
    agent: str,
    cause: str,
    repo_name: str,
    repo_slug: str,
    success_rate: float,
    successes: int,
    total: int,
    context: dict,
) -> dict:
    pct = round(success_rate * 100)
    repo_ref = repo_slug or repo_name
    if cause == "quota":
        goal = f"Reduce {agent} quota-driven task failures in {repo_name} by adjusting routing or capacity before retries are exhausted."
        success_criteria = [
            f"Identify why {agent} is hitting quota limits on {repo_ref}.",
            f"Implement one bounded routing, concurrency, or retry change that lowers quota-limited outcomes for {agent}.",
            f"Confirm the next {WINDOW_DAYS}-day metrics window improves on the current {successes}/{total} success rate.",
        ]
        next_steps = [
            f"Inspect recent `{agent}` runs for quota-limited outcomes in `{repo_ref}` and document the dominant trigger.",
            f"Adjust the routing or fallback policy so quota-sensitive work in `{repo_ref}` shifts before the final retry.",
            f"Add or refine a guardrail that records when the new routing path activates for `{agent}`.",
        ]
        reasoning = f"{agent} is at {pct}% success and recent failures in {repo_name} cluster around quota-limited outcomes."
    elif cause == "authentication":
        goal = f"Restore reliable {agent} authentication for {repo_name} so the queue does not repeatedly dispatch work into credential failures."
        success_criteria = [
            f"Identify the missing or invalid credential path affecting `{agent}` in `{repo_ref}`.",
            f"Add one bounded preflight or routing fallback that avoids repeated authentication failures.",
            f"Reduce authentication-related blocked outcomes for `{repo_ref}` in the next weekly scorer run.",
        ]
        next_steps = [
            f"Review recent `{agent}` task outcomes in `{repo_ref}` for `missing_credentials` evidence and the failing command path.",
            f"Add a preflight check or alternate routing rule so `{agent}` is skipped when the required credential is unavailable.",
            f"Document the repo-specific remediation path the queue should take after another auth failure.",
        ]
        reasoning = f"{agent} is at {pct}% success and recent failures in {repo_name} mostly look like authentication problems."
    elif cause == "model_selection":
        peer_agent = context.get("peer_agent", "another agent")
        peer_stats = context.get("peer_stats") or {}
        peer_pct = round(float(peer_stats.get("rate", 0.0)) * 100)
        goal = f"Improve routing for {repo_name} so work now sent to {agent} shifts toward the agent with stronger recent outcomes."
        success_criteria = [
            f"Explain why `{agent}` underperforms on `{repo_ref}` while `{peer_agent}` succeeds more often.",
            f"Implement one bounded routing-policy change for `{repo_ref}` that prefers the better-performing agent on similar tasks.",
            f"Measure whether the affected task slice improves beyond the current {pct}% success baseline.",
        ]
        next_steps = [
            f"Compare recent `{agent}` and `{peer_agent}` attempts in `{repo_ref}` to isolate the task types where routing should change.",
            f"Update the repo-specific routing or fallback policy so those tasks prefer `{peer_agent}` before retrying `{agent}`.",
            f"Record the policy decision in logs or metrics so future scorer runs can confirm the reroute helped.",
        ]
        reasoning = f"{agent} is at {pct}% success on {repo_name} while {peer_agent} is succeeding at {peer_pct}% on the same repo."
    else:
        goal = f"Stabilize the {agent} execution environment for {repo_name} so repeated runtime failures stop degrading weekly success rates."
        success_criteria = [
            f"Identify the dominant environment or runner failure mode affecting `{agent}` in `{repo_ref}`.",
            f"Land one bounded fix or guardrail that prevents the same failure from recurring.",
            f"Improve the next weekly success-rate snapshot beyond the current {successes}/{total} baseline.",
        ]
        next_steps = [
            f"Inspect recent `{agent}` failures in `{repo_ref}` and group them by runner, workflow, or local environment cause.",
            f"Implement the smallest repo-specific fix or preflight check that would have prevented the dominant failure mode.",
            f"Add enough logging to verify the fix if the same path fails again.",
        ]
        reasoning = f"{agent} is at {pct}% success and the failure evidence for {repo_name} points to environment instability rather than successful recovery."

    return {
        "goal_hint": goal,
        "success_criteria_hint": success_criteria,
        "constraints_hint": [
            "Prefer minimal diffs",
            "Reuse existing metrics and queue artifacts before adding telemetry",
            "Do not create duplicate remediation issues for the same agent/cause/repo combination",
        ],
        "next_steps": next_steps,
        "reasoning_hint": reasoning,
    }


def build_degradation_findings(
    records: list[dict],
    *,
    threshold: float = DEGRADED_THRESHOLD,
    min_total: int = MIN_TASKS_FOR_DEGRADED_FINDING,
    preferred_task_type: str | None = "debugging",
) -> list[dict]:
    # Warn about sentinel-agent records so operators can see fallback-exhaustion volume.
    sentinel_count = sum(1 for r in records if r.get("agent", "unknown") in _SENTINEL_AGENTS)
    if sentinel_count:
        print(
            f"[agent_scorer] Excluded {sentinel_count} sentinel-agent record(s) "
            f"(fallback-exhausted) from degradation analysis."
        )
    rates = compute_success_rates(records)
    findings: list[dict] = []
    for agent, rate, total in degraded_agents(rates, threshold):
        agent_records = [rec for rec in records if rec.get("agent") == agent]
        selected_task_type = None
        scoped_records = agent_records
        if preferred_task_type:
            preferred_records = [
                rec for rec in agent_records
                if _normalize_task_type(rec.get("task_type")) == _normalize_task_type(preferred_task_type)
            ]
            if len(preferred_records) >= min_total:
                scoped_records = preferred_records
                selected_task_type = _normalize_task_type(preferred_task_type)

        scoped_rates = compute_success_rates(scoped_records)
        scoped_stats = scoped_rates.get(agent)
        if not scoped_stats:
            continue
        rate = scoped_stats["rate"]
        total = scoped_stats["total"]
        if total < min_total or rate >= threshold:
            continue
        successes = scoped_stats["successes"]
        failures = [rec for rec in scoped_records if rec.get("status") != "complete"]
        target_repo = _pick_target_repo(agent_records, failures)
        repo_records = [rec for rec in scoped_records if _repo_slug(rec) == target_repo] if target_repo else scoped_records
        repo_name = _repo_display_name(target_repo, repo_records)
        cause, cause_context = _classify_degradation_cause(
            agent=agent,
            agent_rate=rate,
            records=records,
            agent_records=scoped_records,
            failures=failures,
            target_repo=target_repo,
        )
        plan = _finding_plan(
            agent=agent,
            cause=cause,
            repo_name=repo_name,
            repo_slug=target_repo,
            success_rate=rate,
            successes=successes,
            total=total,
            context=cause_context,
        )
        findings.append({
            "id": f"agent_remediation:{agent}:{target_repo or 'unknown'}:{selected_task_type or 'all'}:{cause}",
            "source": "agent_scorer",
            "kind": "agent_remediation",
            "title_hint": _finding_title(agent, cause, repo_name),
            "summary": (
                f"{agent} completed {successes} of {total} tasks in the last "
                f"{WINDOW_DAYS} days in {repo_name}"
                + (f" for {selected_task_type} tasks" if selected_task_type else "")
                + f", below the {round(threshold * 100)}% threshold; "
                f"likely cause: {cause.replace('_', ' ')}."
            ),
            "agent": agent,
            "repo": target_repo,
            "repo_name": repo_name,
            "task_type": selected_task_type,
            "degradation_cause": cause,
            "window_days": WINDOW_DAYS,
            "threshold": threshold,
            "metrics": {
                "total": total,
                "successes": successes,
                "rate": rate,
            },
            **plan,
            "evidence": [
                f"runtime/metrics/agent_stats.jsonl last {WINDOW_DAYS} days",
                f"repo={target_repo or repo_name}",
                f"success_rate={round(rate * 100)}% ({successes}/{total})",
            ],
        })
    return findings


def findings_path(root: Path) -> Path:
    return root / "runtime" / "analysis" / FINDINGS_FILENAME


def configured_repos(cfg: dict) -> list[tuple[str, Path]]:
    repos: list[tuple[str, Path]] = []
    seen: set[tuple[str, str]] = set()
    for project_cfg in cfg.get("github_projects", {}).values():
        if not isinstance(project_cfg, dict):
            continue
        for repo_cfg in project_cfg.get("repos", []):
            if not isinstance(repo_cfg, dict):
                continue
            github_repo = str(repo_cfg.get("github_repo") or "").strip()
            local_repo = str(repo_cfg.get("path") or repo_cfg.get("local_repo") or "").strip()
            if not github_repo or not local_repo:
                continue
            if is_dispatcher_only_repo(cfg, github_repo):
                continue
            key = (github_repo, local_repo)
            if key in seen:
                continue
            seen.add(key)
            repos.append((github_repo, Path(local_repo).expanduser()))
    return repos


def build_business_objective_findings(cfg: dict) -> list[dict]:
    findings: list[dict] = []
    for github_slug, repo_path in configured_repos(cfg):
        objective = load_repo_objective(cfg, github_slug, repo_path)
        if not objective or not objective_metrics(objective):
            continue
        snapshots = load_outcome_records(
            cfg,
            repo=github_slug,
            window_days=max(7, int(objective.get("evaluation_window_days", WINDOW_DAYS))),
        )
        score = score_objective_snapshots(objective, snapshots)
        if score["score"] > BUSINESS_SCORE_THRESHOLD:
            continue
        primary = str(objective.get("primary_outcome") or objective.get("north_star") or "business growth").strip()
        findings.append({
            "id": f"business_objective:{github_slug}",
            "source": "agent_scorer",
            "kind": "business_objective_regressed",
            "repo": github_slug,
            "title_hint": f"Improve measured business outcomes in {github_slug.rsplit('/', 1)[-1]}",
            "summary": (
                f"Weighted business outcome score for {github_slug} is {score['score']:+.2f} over the last "
                f"{score['window_days']} days against the external objective `{primary}`."
            ),
            "objective": primary,
            "metrics": {
                "score": score["score"],
                "window_days": score["window_days"],
                "counts": score["counts"],
            },
            "evidence": [
                f"external objective config: {objective.get('_objective_path', '(unknown)')}",
                f"weighted_outcome_score={score['score']:+.2f}",
                f"regressed={score['counts'].get('regressed', 0)}, inconclusive={score['counts'].get('inconclusive', 0)}",
            ],
        })
    return findings


PIPELINE_ANOMALY_WINDOW_DAYS = 14
PIPELINE_ANOMALY_MIN_MERGES = 3


def build_pipeline_anomaly_findings(cfg: dict) -> list[dict]:
    """Scan outcome_attribution.jsonl for structural data-flow anomalies.

    The sprint retrospective already surfaces symptoms like "every PR was
    inconclusive", but it has no way to distinguish "we need more metrics"
    from "the plumbing that records metrics is broken". This detector reads
    the raw attribution log and emits a finding the groomer can convert into
    a debugging issue — so the planner/groomer stop asking for more config
    when the actual fault is in the data path.

    Currently detects:
      - Runs of merged attribution records with empty ``outcome_check_ids``
        (signals check-ID plumbing is dropped somewhere between issue body
        and merge-time attribution).
      - Runs of snapshot records with source="none" / "no measurable
        external metric" (symptom of the same gap, viewed from the other side).
    """
    findings: list[dict] = []
    for github_slug, _repo_path in configured_repos(cfg):
        records = load_outcome_records(
            cfg, repo=github_slug, window_days=PIPELINE_ANOMALY_WINDOW_DAYS
        )
        if not records:
            continue

        merges = [
            r for r in records
            if r.get("record_type") == "attribution" and r.get("event") == "merged"
        ]
        if len(merges) < PIPELINE_ANOMALY_MIN_MERGES:
            continue
        empty_merges = [m for m in merges if not (m.get("outcome_check_ids") or [])]
        empty_ratio = len(empty_merges) / len(merges)

        snapshots = [r for r in records if r.get("record_type") == "snapshot"]
        unmeasured_snapshots = [
            s for s in snapshots
            if str(s.get("source") or "").lower() == "none"
        ]

        if empty_ratio < 0.7 or len(empty_merges) < PIPELINE_ANOMALY_MIN_MERGES:
            continue

        sample_prs = [m.get("pr_number") for m in empty_merges[-5:] if m.get("pr_number")]
        findings.append({
            "id": f"pipeline_anomaly:outcome_check_ids:{github_slug}",
            "source": "agent_scorer",
            "kind": "pipeline_anomaly",
            "repo": github_slug,
            "title_hint": (
                f"Debug: outcome_check_ids dropped on merged PRs in "
                f"{github_slug.rsplit('/', 1)[-1]}"
            ),
            "summary": (
                f"{len(empty_merges)}/{len(merges)} merged PRs in the last "
                f"{PIPELINE_ANOMALY_WINDOW_DAYS} days were recorded with empty "
                f"outcome_check_ids. This is a data-flow defect, not a missing-"
                f"metric configuration — the checks are configured but are not "
                f"reaching the attribution log. The sprint retrospective will "
                f"keep reporting 'inconclusive' until this plumbing is fixed."
            ),
            "metrics": {
                "merges_total": len(merges),
                "merges_with_empty_ids": len(empty_merges),
                "empty_ratio": round(empty_ratio, 3),
                "unmeasured_snapshots": len(unmeasured_snapshots),
                "window_days": PIPELINE_ANOMALY_WINDOW_DAYS,
            },
            "evidence": [
                f"attribution log: runtime/metrics/outcome_attribution.jsonl",
                f"recent affected PRs: {sample_prs or '(none captured)'}",
                "flow: issue body (## Outcome Checks) → "
                "github_dispatcher.build_mailbox_task → task meta → "
                "github_sync.sync_result (writes pr_opened attribution) → "
                "pr_monitor._cleanup_merged_pr_issues (reads it back)",
                "likely break point: pr_opened record is never written when the "
                "agent opens PRs directly via `gh pr create` (skipping github_sync)",
            ],
        })

    return findings


RETRY_STORM_WINDOW_DAYS = 14
RETRY_STORM_MIN_TASKS = 6
RETRY_STORM_RATIO_THRESHOLD = 0.3
_RETRY_SUFFIX_RE = re.compile(r"-retry-(\d+)$")


def _retry_attempt_from_task_id(task_id: str) -> int:
    """Extract the retry attempt number from a task_id's `-retry-N` suffix.

    Task IDs without the suffix are attempt 0 (first attempt).
    """
    if not task_id:
        return 0
    match = _RETRY_SUFFIX_RE.search(str(task_id))
    if not match:
        return 0
    try:
        return int(match.group(1))
    except ValueError:
        return 0


def build_retry_storm_findings(cfg: dict) -> list[dict]:
    """Flag repos where too many tasks escalate past attempt 2.

    Retries are normal — a single flaky model cascade is cheap and expected.
    Deep retry chains (attempt 2+ = third try or later) are a structural
    signal that agents are fighting the same underlying problem repeatedly
    instead of the planner/groomer intervening. If >30%% of recent tasks hit
    attempt 2+, that is a pipeline-level defect that no per-agent degradation
    finding will catch.
    """
    findings: list[dict] = []
    root = Path(cfg.get("root_dir", ".")).expanduser()
    metrics_file = root / "runtime" / "metrics" / "agent_stats.jsonl"
    if not metrics_file.exists():
        return findings

    records = load_recent_metrics(metrics_file, window_days=RETRY_STORM_WINDOW_DAYS)
    if not records:
        return findings

    by_repo: dict[str, list[dict]] = defaultdict(list)
    for rec in records:
        repo = _repo_slug(rec)
        if repo:
            by_repo[repo].append(rec)

    eligible_repos = {github_slug for github_slug, _ in configured_repos(cfg)}
    for repo, recs in by_repo.items():
        if eligible_repos and repo not in eligible_repos:
            continue
        if len(recs) < RETRY_STORM_MIN_TASKS:
            continue
        deep_retries = [
            r for r in recs
            if _retry_attempt_from_task_id(r.get("task_id") or "") >= 2
        ]
        ratio = len(deep_retries) / len(recs)
        if ratio < RETRY_STORM_RATIO_THRESHOLD:
            continue
        sample_task_ids = [
            r.get("task_id") for r in deep_retries[-5:] if r.get("task_id")
        ]
        findings.append({
            "id": f"pipeline_anomaly:retry_storm:{repo}",
            "source": "agent_scorer",
            "kind": "pipeline_anomaly",
            "repo": repo,
            "title_hint": (
                f"Debug: deep retry chains dominating task flow in "
                f"{repo.rsplit('/', 1)[-1]}"
            ),
            "summary": (
                f"{len(deep_retries)}/{len(recs)} tasks in the last "
                f"{RETRY_STORM_WINDOW_DAYS} days escalated to attempt 2 or "
                f"deeper. High retry density is a pipeline-level defect — the "
                f"planner or groomer should have caught whatever is causing "
                f"repeated failure before the task re-entered the fallback "
                f"chain. Investigate root-cause (task specification gap? "
                f"environment drift? model/prompt mismatch?) rather than "
                f"letting the retry chain absorb the noise."
            ),
            "metrics": {
                "total_tasks": len(recs),
                "deep_retry_tasks": len(deep_retries),
                "deep_retry_ratio": round(ratio, 3),
                "window_days": RETRY_STORM_WINDOW_DAYS,
            },
            "evidence": [
                f"metrics log: runtime/metrics/agent_stats.jsonl",
                f"recent deep-retry task ids: {sample_task_ids or '(none captured)'}",
                "check: are the same task types repeatedly failing, or spread across types?",
                "check: are the same agents repeatedly failing, or spread across agents?",
            ],
        })
    return findings


DEBUG_HYPOTHESIS_LOOKBACK = 3
_GENERIC_HYPOTHESIS_TOKENS = frozenset({
    "quality", "improve", "better", "more tests", "test coverage",
    "needs", "should", "could", "maybe", "might",
})


def build_debug_hypothesis_findings(cfg: dict) -> list[dict]:
    """Promote retro-provided ``debug_hypothesis`` fields into scorer findings.

    When the sprint retro's LLM call emits a concrete structural hypothesis
    (e.g. "outcome_check_ids are dropped between issue creation and pr_monitor
    cleanup"), we want the groomer to turn that into an atomic debug issue
    instead of the hypothesis dying inside the retrospective. Skip hypotheses
    that are too vague to act on.
    """
    findings: list[dict] = []
    for github_slug, _repo_path in configured_repos(cfg):
        reports = load_sprint_history(cfg, github_slug, limit=DEBUG_HYPOTHESIS_LOOKBACK)
        if not reports:
            continue
        latest = reports[-1]
        hypothesis = str(latest.get("debug_hypothesis") or "").strip()
        if not hypothesis:
            continue
        # Filter out boilerplate phrasings that add no investigation value.
        lower = hypothesis.lower()
        if len(hypothesis) < 20 or not any(ch.isalpha() for ch in hypothesis):
            continue
        generic_hits = sum(1 for token in _GENERIC_HYPOTHESIS_TOKENS if token in lower)
        if generic_hits >= 2 and len(hypothesis) < 120:
            continue
        findings.append({
            "id": f"pipeline_anomaly:debug_hypothesis:{github_slug}:{hash(hypothesis) & 0xffff:x}",
            "source": "agent_scorer",
            "kind": "pipeline_anomaly",
            "repo": github_slug,
            "title_hint": (
                f"Investigate retro hypothesis: "
                f"{hypothesis[:80]}{'...' if len(hypothesis) > 80 else ''}"
            ),
            "summary": (
                f"The latest sprint retrospective emitted a structural "
                f"root-cause hypothesis: '{hypothesis}'. Convert this into a "
                f"single atomic debug issue that tests the hypothesis in code, "
                f"rather than letting it live only in retro prose."
            ),
            "metrics": {
                "hypothesis_length": len(hypothesis),
                "sprint_headline": str(latest.get("headline") or "")[:200],
            },
            "evidence": [
                f"sprint history log: runtime/metrics/sprint_reports.jsonl",
                f"hypothesis: {hypothesis}",
                f"sprint headline: {str(latest.get('headline') or '(no headline)')}",
            ],
        })
    return findings


RECURRING_RISK_SPRINT_LIMIT = 10
RECURRING_RISK_MIN_REPEATS = 3


def build_recurring_risk_findings(cfg: dict) -> list[dict]:
    """Scan sprint_reports.jsonl for risks/gaps that recur across sprints.

    When the same concern appears in the last 3+ sprint retrospectives, the
    team has flagged it repeatedly but not resolved it. This detector emits a
    finding the groomer can convert into a dedicated backlog issue so the
    problem stops living only in ephemeral retro prose.
    """
    findings: list[dict] = []
    for github_slug, _repo_path in configured_repos(cfg):
        reports = load_sprint_history(cfg, github_slug, limit=RECURRING_RISK_SPRINT_LIMIT)
        if len(reports) < RECURRING_RISK_MIN_REPEATS:
            continue
        recurring = find_recurring_concerns(
            reports, min_repeats=RECURRING_RISK_MIN_REPEATS
        )
        if not recurring:
            continue
        for entry in recurring:
            example = str(entry.get("example") or "").strip()
            if not example:
                continue
            sprint_count = int(entry.get("sprint_count") or 0)
            total = int(entry.get("total_sprints_considered") or len(reports))
            keywords = entry.get("keywords") or []
            # Build a stable id so repeat detections update the same finding
            # instead of creating a new issue each cycle.
            kw_slug = "-".join(sorted(keywords)[:4]) or "generic"
            findings.append({
                "id": f"recurring_risk:{github_slug}:{kw_slug}",
                "source": "agent_scorer",
                "kind": "recurring_risk",
                "repo": github_slug,
                "title_hint": (
                    f"Resolve recurring sprint risk: "
                    f"{example[:80]}{'...' if len(example) > 80 else ''}"
                ),
                "summary": (
                    f"This concern has appeared in {sprint_count}/{total} recent "
                    f"sprint reports without being resolved. Previous sprints flagged "
                    f"it as a risk but no backlog work closed it out. Either promote "
                    f"concrete remediation work, or open an investigation to root-cause "
                    f"why the concern keeps recurring."
                ),
                "metrics": {
                    "sprint_count": sprint_count,
                    "total_sprints": total,
                    "keywords": keywords,
                },
                "evidence": [
                    f"latest phrasing: {example}",
                    f"sprint history log: runtime/metrics/sprint_reports.jsonl",
                    f"keywords: {', '.join(keywords) if keywords else '(none)'}",
                ],
            })
    return findings


def write_findings(path: Path, findings: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "window_days": WINDOW_DAYS,
        "threshold": DEGRADED_THRESHOLD,
        "findings": findings,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _compute_merge_cycle_hours(records: list[dict]) -> dict:
    """Compute mean/median hours from task start to completion for complete tasks."""
    durations = [
        rec["duration_seconds"] / 3600
        for rec in records
        if rec.get("status") == "complete" and rec.get("duration_seconds")
    ]
    if not durations:
        return {"mean": None, "median": None, "count": 0}
    durations.sort()
    mid = len(durations) // 2
    median = durations[mid] if len(durations) % 2 else (durations[mid - 1] + durations[mid]) / 2
    return {"mean": sum(durations) / len(durations), "median": median, "count": len(durations)}


def _compute_escalation_rate(records: list[dict]) -> dict:
    """Fraction of tasks that exhausted all agents (status=blocked with no follow-up)."""
    total = len(records)
    if not total:
        return {"rate": 0.0, "escalated": 0, "total": 0}
    escalated = sum(1 for r in records if r.get("status") == "blocked")
    return {"rate": escalated / total, "escalated": escalated, "total": total}


def write_metrics_report(root: Path, metrics_file: Path):
    """Write a rolling 2-week metrics report for outcome check consumption."""
    now = datetime.now(tz=timezone.utc)
    current_week = load_recent_metrics(metrics_file, window_days=7)
    prior_week_all = load_recent_metrics(metrics_file, window_days=14)
    # Subtract current week from 14-day window to get prior week
    cutoff_7d = now - timedelta(days=7)
    prior_week = [
        r for r in prior_week_all
        if _parse_ts(r) and _parse_ts(r) < cutoff_7d
    ]

    cur_rates = compute_success_rates(current_week)
    prev_rates = compute_success_rates(prior_week)
    cur_cycle = _compute_merge_cycle_hours(current_week)
    prev_cycle = _compute_merge_cycle_hours(prior_week)
    cur_esc = _compute_escalation_rate(current_week)
    prev_esc = _compute_escalation_rate(prior_week)

    def _rate_line(agent: str, stats: dict) -> str:
        pct = round(stats["rate"] * 100)
        return f"- {agent}: {pct}% ({stats['successes']}/{stats['total']})"

    def _overall(rates: dict) -> dict:
        total = sum(s["total"] for s in rates.values())
        successes = sum(s["successes"] for s in rates.values())
        return {"total": total, "successes": successes, "rate": successes / total if total else 0.0}

    cur_overall = _overall(cur_rates)
    prev_overall = _overall(prev_rates)

    lines = [
        f"# Agent-OS Metrics Report",
        f"Generated: {now.isoformat()}",
        "",
        f"## Agent Success Rate (current week, last 7 days)",
        f"Overall: {round(cur_overall['rate'] * 100)}% ({cur_overall['successes']}/{cur_overall['total']} tasks complete)",
    ]
    for agent in sorted(cur_rates):
        lines.append(_rate_line(agent, cur_rates[agent]))
    lines += [
        "",
        f"## Agent Success Rate (prior week, days 8-14)",
        f"Overall: {round(prev_overall['rate'] * 100)}% ({prev_overall['successes']}/{prev_overall['total']} tasks complete)",
    ]
    for agent in sorted(prev_rates):
        lines.append(_rate_line(agent, prev_rates[agent]))
    lines += [
        "",
        f"## Task Completion Time",
        f"Current week: mean {cur_cycle['mean']:.1f}h, median {cur_cycle['median']:.1f}h ({cur_cycle['count']} tasks)" if cur_cycle["mean"] else "Current week: no data",
        f"Prior week: mean {prev_cycle['mean']:.1f}h, median {prev_cycle['median']:.1f}h ({prev_cycle['count']} tasks)" if prev_cycle["mean"] else "Prior week: no data",
        "",
        f"## Escalation Rate (tasks blocked after exhausting all agents)",
        f"Current week: {round(cur_esc['rate'] * 100)}% ({cur_esc['escalated']}/{cur_esc['total']})",
        f"Prior week: {round(prev_esc['rate'] * 100)}% ({prev_esc['escalated']}/{prev_esc['total']})",
        "",
        f"## Degradation Threshold",
        f"Agents below {round(DEGRADED_THRESHOLD * 100)}% success rate are flagged for remediation.",
        "",
    ]

    report_path = root / "runtime" / "analysis" / "metrics_report.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote metrics report to {report_path}")
    return report_path


def write_production_feedback(root: Path, metrics_file: Path):
    """Write PRODUCTION_FEEDBACK.md in repo root for groomer/planner consumption."""
    now = datetime.now(tz=timezone.utc)
    records = load_recent_metrics(metrics_file, window_days=14)
    if not records:
        return

    rates = compute_success_rates(records)
    cycle = _compute_merge_cycle_hours(records)
    esc = _compute_escalation_rate(records)
    overall_total = sum(s["total"] for s in rates.values())
    overall_successes = sum(s["successes"] for s in rates.values())
    overall_rate = overall_successes / overall_total if overall_total else 0.0

    # Count tasks by type
    type_counts: dict[str, int] = defaultdict(int)
    for r in records:
        type_counts[r.get("task_type", "unknown")] += 1

    # Top blocker codes
    blocker_counts: dict[str, int] = defaultdict(int)
    for r in records:
        bc = r.get("blocker_code", "")
        if bc and bc != "none":
            blocker_counts[bc] += 1

    lines = [
        f"# Production Feedback",
        f"Auto-generated: {now.isoformat()}",
        f"Window: last 14 days | Source: agent_stats.jsonl",
        "",
        "## Key Metrics",
        f"- Overall success rate: {round(overall_rate * 100)}% ({overall_successes}/{overall_total})",
        f"- Escalation rate: {round(esc['rate'] * 100)}% ({esc['escalated']}/{esc['total']})",
        f"- Mean completion time: {cycle['mean']:.1f}h" if cycle["mean"] else "- Mean completion time: no data",
        "",
        "## Per-Agent Performance",
    ]
    for agent in sorted(rates):
        s = rates[agent]
        lines.append(f"- {agent}: {round(s['rate'] * 100)}% ({s['successes']}/{s['total']})")
    lines += ["", "## Task Type Distribution"]
    for tt in sorted(type_counts, key=lambda k: -type_counts[k]):
        lines.append(f"- {tt}: {type_counts[tt]}")
    if blocker_counts:
        lines += ["", "## Top Blocker Codes"]
        for bc in sorted(blocker_counts, key=lambda k: -blocker_counts[k])[:5]:
            lines.append(f"- {bc}: {blocker_counts[bc]}")
        lines += [
            "",
            "## Blocker Regression Monitoring",
            "",
            "Alert thresholds (rolling 24h, forward-looking post-fix only):",
            "- missing_context > 5 → Telegram alert with RCA runbook",
            "",
            "Regression test guidance:",
            "- missing_context: Verify write_prompt() injects structured dispatch context "
            "(git state, objectives, sprint directives). Test with test_queue.py prompt tests.",
            "- fallback_exhausted: All configured agents failed health gate. "
            "Check agent_scorer.py health thresholds and agent_stats.jsonl recent windows.",
            "- missing_credentials: Runner credential preflight failed. "
            "Check queue.py agent_available() and secrets.json presence.",
        ]
    lines.append("")

    feedback_path = root / "PRODUCTION_FEEDBACK.md"
    feedback_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote production feedback to {feedback_path}")


def _parse_ts(rec: dict) -> datetime | None:
    try:
        ts = datetime.fromisoformat(rec.get("timestamp", ""))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    except (ValueError, TypeError):
        return None


def run():
    cfg = load_config()
    root = Path(cfg.get("root_dir", ".")).expanduser()
    metrics_file = root / "runtime" / "metrics" / "agent_stats.jsonl"

    records = load_recent_metrics(metrics_file)
    eligible_repos = {github_slug for github_slug, _repo_path in configured_repos(cfg)}
    if eligible_repos:
        filtered_records = []
        for rec in records:
            repo_slug = _repo_slug(rec)
            if repo_slug and repo_slug not in eligible_repos:
                continue
            filtered_records.append(rec)
        records = filtered_records
    if not records:
        print(f"No metrics found in {metrics_file} for the past {WINDOW_DAYS} days.")
        return

    rates = compute_success_rates(records)
    print(f"Agent success rates (last {WINDOW_DAYS} days, {len(records)} tasks):")
    for agent, stats in sorted(rates.items()):
        pct = round(stats["rate"] * 100)
        print(f"  {agent}: {pct}% ({stats['successes']}/{stats['total']})")

    findings = build_degradation_findings(records)
    findings.extend(build_business_objective_findings(cfg))
    findings.extend(build_pipeline_anomaly_findings(cfg))
    findings.extend(build_retry_storm_findings(cfg))
    findings.extend(build_debug_hypothesis_findings(cfg))
    findings.extend(build_recurring_risk_findings(cfg))
    findings.extend(build_system_architect_findings(cfg))
    artifact = findings_path(root)
    write_findings(artifact, findings)

    # Write metrics report for outcome check consumption
    write_metrics_report(root, metrics_file)
    # Write production feedback for groomer/planner consumption
    write_production_feedback(root, metrics_file)

    if not findings:
        print(f"All agents above threshold. Wrote empty findings artifact to {artifact}.")
        return

    for finding in findings:
        if finding.get("source") != "agent_scorer":
            continue
        event = {
            "source": "agent_scorer",
            "type": finding.get("kind") or "degradation_finding",
            "repo": finding.get("repo"),
            "agent": finding.get("agent"),
            "summary": finding.get("summary"),
            "dedup_key": finding.get("id"),
        }
        severity = classify_severity(cfg, "agent_scorer", event)
        route_incident(severity, event, cfg=cfg)

    print(f"Wrote {len(findings)} degradation finding(s) to {artifact}.")
    print("Issue creation is owned by orchestrator.log_analyzer.")


if __name__ == "__main__":
    run()
