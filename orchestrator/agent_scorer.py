"""Weekly agent performance scorer.

Reads runtime/metrics/agent_stats.jsonl, computes per-agent success rates
over the past 7 days, and emits structured degradation findings for the
log analyzer's evidence-synthesis flow.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from orchestrator.objectives import load_repo_objective, objective_metrics, score_objective_snapshots
from orchestrator.outcome_attribution import load_outcome_records
from orchestrator.paths import load_config


DEGRADED_THRESHOLD = 0.60
WINDOW_DAYS = 7
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


def compute_success_rates(records: list[dict]) -> dict[str, dict]:
    """Return {agent: {total, successes, rate}} for agents with >= 1 task."""
    counts: dict[str, dict] = defaultdict(lambda: {"total": 0, "successes": 0})
    for rec in records:
        agent = rec.get("agent", "unknown")
        counts[agent]["total"] += 1
        if rec.get("status") == "complete":
            counts[agent]["successes"] += 1
    return {
        agent: {**v, "rate": v["successes"] / v["total"] if v["total"] else 0.0}
        for agent, v in counts.items()
    }


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
) -> list[dict]:
    rates = compute_success_rates(records)
    findings: list[dict] = []
    for agent, rate, total in degraded_agents(rates, threshold):
        if total < min_total:
            continue
        successes = rates[agent]["successes"]
        agent_records = [rec for rec in records if rec.get("agent") == agent]
        failures = [rec for rec in agent_records if rec.get("status") != "complete"]
        target_repo = _pick_target_repo(agent_records, failures)
        repo_records = [rec for rec in agent_records if _repo_slug(rec) == target_repo] if target_repo else agent_records
        repo_name = _repo_display_name(target_repo, repo_records)
        cause, cause_context = _classify_degradation_cause(
            agent=agent,
            agent_rate=rate,
            records=records,
            agent_records=agent_records,
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
            "id": f"agent_remediation:{agent}:{target_repo or 'unknown'}:{cause}",
            "source": "agent_scorer",
            "kind": "agent_remediation",
            "title_hint": _finding_title(agent, cause, repo_name),
            "summary": (
                f"{agent} completed {successes} of {total} tasks in the last "
                f"{WINDOW_DAYS} days in {repo_name}, below the {round(threshold * 100)}% threshold; "
                f"likely cause: {cause.replace('_', ' ')}."
            ),
            "agent": agent,
            "repo": target_repo,
            "repo_name": repo_name,
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


def write_findings(path: Path, findings: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "window_days": WINDOW_DAYS,
        "threshold": DEGRADED_THRESHOLD,
        "findings": findings,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run():
    cfg = load_config()
    root = Path(cfg.get("root_dir", ".")).expanduser()
    metrics_file = root / "runtime" / "metrics" / "agent_stats.jsonl"

    records = load_recent_metrics(metrics_file)
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
    artifact = findings_path(root)
    write_findings(artifact, findings)

    if not findings:
        print(f"All agents above threshold. Wrote empty findings artifact to {artifact}.")
        return

    print(f"Wrote {len(findings)} degradation finding(s) to {artifact}.")
    print("Issue creation is owned by orchestrator.log_analyzer.")


if __name__ == "__main__":
    run()
