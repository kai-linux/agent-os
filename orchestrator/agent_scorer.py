"""Weekly agent performance scorer.

Reads runtime/metrics/agent_stats.jsonl, computes per-agent success rates
over the past 7 days, and creates a GitHub issue for any agent below 60%.
"""
from __future__ import annotations

import json
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from orchestrator.paths import load_config


DEGRADED_THRESHOLD = 0.60
WINDOW_DAYS = 7


def _gh(cmd: list[str], *, check: bool = True) -> str:
    result = subprocess.run(["gh", *cmd], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(cmd[:3])}... exit {result.returncode}: {result.stderr.strip()}")
    return result.stdout.strip()


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


def create_degradation_issue(github_repo: str, agent: str, rate: float, total: int, successes: int) -> str:
    """Create a GitHub issue and return its URL."""
    title = issue_title(agent, rate)
    pct = round(rate * 100)
    body = (
        f"## Agent performance alert\n\n"
        f"**Agent:** `{agent}`\n"
        f"**Success rate (last {WINDOW_DAYS} days):** {pct}% ({successes}/{total} tasks)\n"
        f"**Threshold:** {round(DEGRADED_THRESHOLD * 100)}%\n\n"
        f"Please investigate agent `{agent}` for configuration, credential, or quota issues."
    )
    url = _gh(["issue", "create", "--repo", github_repo, "--title", title, "--body", body])
    return url


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

    bad = degraded_agents(rates)
    if not bad:
        print("All agents above threshold. No issues created.")
        return

    github_repo = cfg.get("github_repo") or cfg.get("github_owner", "")
    if not github_repo:
        # Try to derive from github_projects
        projects = cfg.get("github_projects", {})
        for pk, pv in projects.items():
            if isinstance(pv, dict) and pv.get("repo"):
                github_repo = pv["repo"]
                break

    if not github_repo:
        print("Warning: github_repo not configured; skipping issue creation.")
        for agent, rate, total in bad:
            print(f"  DEGRADED: {agent} at {round(rate*100)}%")
        return

    for agent, rate, total in bad:
        successes = rates[agent]["successes"]
        try:
            url = create_degradation_issue(github_repo, agent, rate, total, successes)
            print(f"Created issue for {agent}: {url}")
        except Exception as e:
            print(f"Failed to create issue for {agent}: {e}")


if __name__ == "__main__":
    run()
