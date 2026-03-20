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

from orchestrator.paths import load_config


DEGRADED_THRESHOLD = 0.60
WINDOW_DAYS = 7
MIN_TASKS_FOR_DEGRADED_FINDING = 4
FINDINGS_FILENAME = "agent_scorer_findings.json"


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
        findings.append({
            "id": f"agent_degraded:{agent}",
            "source": "agent_scorer",
            "kind": "agent_degraded",
            "title_hint": issue_title(agent, rate),
            "summary": (
                f"{agent} completed {successes} of {total} tasks in the last "
                f"{WINDOW_DAYS} days, below the {round(threshold * 100)}% threshold."
            ),
            "agent": agent,
            "window_days": WINDOW_DAYS,
            "threshold": threshold,
            "metrics": {
                "total": total,
                "successes": successes,
                "rate": rate,
            },
            "evidence": [
                f"runtime/metrics/agent_stats.jsonl last {WINDOW_DAYS} days",
                f"success_rate={round(rate * 100)}% ({successes}/{total})",
            ],
        })
    return findings


def findings_path(root: Path) -> Path:
    return root / "runtime" / "analysis" / FINDINGS_FILENAME


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
    artifact = findings_path(root)
    write_findings(artifact, findings)

    if not findings:
        print(f"All agents above threshold. Wrote empty findings artifact to {artifact}.")
        return

    print(f"Wrote {len(findings)} degradation finding(s) to {artifact}.")
    print("Issue creation is owned by orchestrator.log_analyzer.")


if __name__ == "__main__":
    run()
