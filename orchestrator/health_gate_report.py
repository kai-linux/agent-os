"""Health gate validation report generator.

Reads agent_stats.jsonl and health_gate_decisions.jsonl to produce a
monitoring report that tracks gate efficacy, decision patterns, and
actual task outcomes. Output is written to PRODUCTION_FEEDBACK.md-compatible
sections and optionally sent to Telegram.
"""
from __future__ import annotations

import json
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from orchestrator.agent_scorer import (
    ADAPTIVE_HEALTH_THRESHOLD,
    ADAPTIVE_HEALTH_WINDOW_DAYS,
    GATE_DECISIONS_FILENAME,
    compute_success_rates,
    load_recent_metrics,
)
from orchestrator.paths import load_config


REPORT_WINDOW_DAYS = 7
REPORT_FILENAME = "HEALTH_GATE_REPORT.md"


def _load_gate_decisions(metrics_dir: Path, window_days: int = REPORT_WINDOW_DAYS) -> list[dict]:
    """Load gate decision records within the reporting window."""
    log_path = metrics_dir / GATE_DECISIONS_FILENAME
    if not log_path.exists():
        return []
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=window_days)
    records = []
    with log_path.open(encoding="utf-8") as f:
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


def _compute_baseline(records: list[dict]) -> dict[str, dict]:
    """Compute per-agent baseline metrics from all-time records."""
    return compute_success_rates(records)


def _compute_window_rates(records: list[dict], window_days: int) -> dict[str, dict]:
    """Compute per-agent success rates within a time window."""
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=window_days)
    windowed = []
    for rec in records:
        ts_raw = rec.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_raw)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        if ts >= cutoff:
            windowed.append(rec)
    return compute_success_rates(windowed)


def _compute_task_type_rates(records: list[dict], task_type: str, window_days: int) -> dict[str, dict]:
    """Compute per-agent success rates for one task type within the report window."""
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=window_days)
    windowed = []
    for rec in records:
        ts_raw = rec.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_raw)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        if ts >= cutoff:
            windowed.append(rec)
    return compute_success_rates(windowed, task_type=task_type)


def _count_blocker_codes(records: list[dict], window_days: int) -> dict[str, int]:
    """Count blocker codes within a time window."""
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=window_days)
    codes: dict[str, int] = defaultdict(int)
    for rec in records:
        ts_raw = rec.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_raw)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        if ts >= cutoff:
            bc = rec.get("blocker_code", "")
            if bc:
                codes[bc] += 1
    return dict(codes)


def _gate_decision_summary(decisions: list[dict]) -> dict:
    """Summarize gate decisions: total invocations, agents skipped, contexts."""
    total = len(decisions)
    skipped_counts: dict[str, int] = defaultdict(int)
    contexts: dict[str, int] = defaultdict(int)
    for d in decisions:
        for agent in d.get("skipped", {}):
            skipped_counts[agent] += 1
        ctx = d.get("context", "unknown")
        contexts[ctx.split()[0] if ctx else "unknown"] += 1
    return {
        "total_invocations": total,
        "agents_skipped": dict(skipped_counts),
        "by_source": dict(contexts),
    }


def generate_report(cfg: dict | None = None) -> str:
    """Generate the health gate validation report as markdown."""
    if cfg is None:
        cfg = load_config()

    root_dir = Path(cfg.get("root_dir", ".")).expanduser()
    metrics_dir = root_dir / "runtime" / "metrics"
    metrics_file = metrics_dir / "agent_stats.jsonl"

    # Load all records for baseline
    all_records = load_recent_metrics(metrics_file, window_days=365)
    window_records = load_recent_metrics(metrics_file, window_days=REPORT_WINDOW_DAYS)

    baseline_rates = _compute_baseline(all_records)
    window_rates = _compute_window_rates(all_records, REPORT_WINDOW_DAYS)
    debug_window_rates = _compute_task_type_rates(all_records, "debugging", REPORT_WINDOW_DAYS)
    blocker_codes_all = _count_blocker_codes(all_records, window_days=365)
    blocker_codes_window = _count_blocker_codes(all_records, REPORT_WINDOW_DAYS)

    # Gate decisions
    decisions = _load_gate_decisions(metrics_dir, REPORT_WINDOW_DAYS)
    gate_summary = _gate_decision_summary(decisions)

    # Build report
    now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# Health Gate Validation Report",
        f"",
        f"Generated: {now}",
        f"Window: {REPORT_WINDOW_DAYS} days | Gate: >{int(ADAPTIVE_HEALTH_THRESHOLD * 100)}% threshold over {ADAPTIVE_HEALTH_WINDOW_DAYS}d",
        f"",
        f"## Baseline Metrics (All Time)",
        f"",
    ]

    total_all = sum(v["total"] for v in baseline_rates.values())
    complete_all = sum(v["successes"] for v in baseline_rates.values())
    overall_rate = complete_all / total_all * 100 if total_all else 0
    lines.append(f"| Agent | Tasks | Successes | Rate |")
    lines.append(f"|-------|------:|----------:|-----:|")
    for agent in sorted(baseline_rates):
        s = baseline_rates[agent]
        lines.append(f"| {agent} | {s['total']} | {s['successes']} | {s['rate']*100:.1f}% |")
    lines.append(f"| **Overall** | **{total_all}** | **{complete_all}** | **{overall_rate:.1f}%** |")
    lines.append("")

    # Window metrics
    lines.append(f"## Last {REPORT_WINDOW_DAYS} Days")
    lines.append(f"")
    total_w = sum(v["total"] for v in window_rates.values())
    complete_w = sum(v["successes"] for v in window_rates.values())
    overall_w = complete_w / total_w * 100 if total_w else 0
    lines.append(f"| Agent | Tasks | Successes | Rate |")
    lines.append(f"|-------|------:|----------:|-----:|")
    for agent in sorted(window_rates):
        s = window_rates[agent]
        lines.append(f"| {agent} | {s['total']} | {s['successes']} | {s['rate']*100:.1f}% |")
    lines.append(f"| **Overall** | **{total_w}** | **{complete_w}** | **{overall_w:.1f}%** |")
    lines.append("")
    lines.append(f"## Last {REPORT_WINDOW_DAYS} Days Debugging Path")
    lines.append(f"")
    debug_total = sum(v["total"] for v in debug_window_rates.values())
    debug_complete = sum(v["successes"] for v in debug_window_rates.values())
    debug_rate = debug_complete / debug_total * 100 if debug_total else 0
    if debug_total:
        lines.append(f"| Agent | Debugging Tasks | Successes | Rate |")
        lines.append(f"|-------|----------------:|----------:|-----:|")
        for agent in sorted(debug_window_rates):
            s = debug_window_rates[agent]
            lines.append(f"| {agent} | {s['total']} | {s['successes']} | {s['rate']*100:.1f}% |")
        lines.append(f"| **Overall** | **{debug_total}** | **{debug_complete}** | **{debug_rate:.1f}%** |")
    else:
        lines.append("No debugging-task metrics in this window.")
    lines.append("")

    # Gate decision analysis
    lines.append(f"## Gate Decisions ({REPORT_WINDOW_DAYS}d)")
    lines.append(f"")
    if gate_summary["total_invocations"] == 0:
        lines.append("No gate decisions logged in this window.")
        lines.append("")
        lines.append("This may indicate:")
        lines.append("- No degraded agents were encountered during dispatch")
        lines.append("- Gate decisions occurred before logging was enabled")
    else:
        lines.append(f"Total gate invocations: {gate_summary['total_invocations']}")
        lines.append("")
        if gate_summary["agents_skipped"]:
            lines.append("Agents skipped by gate:")
            for agent, count in sorted(gate_summary["agents_skipped"].items(), key=lambda x: -x[1]):
                rate_info = baseline_rates.get(agent, {})
                rate_str = f"{rate_info.get('rate', 0)*100:.0f}%" if rate_info else "N/A"
                lines.append(f"- **{agent}**: skipped {count} time(s) (baseline rate: {rate_str})")
        lines.append("")
        if gate_summary["by_source"]:
            lines.append("By source:")
            for source, count in sorted(gate_summary["by_source"].items()):
                lines.append(f"- {source}: {count}")
    lines.append("")

    # Blocker code analysis
    lines.append(f"## Blocker Codes")
    lines.append(f"")
    fe_all = blocker_codes_all.get("fallback_exhausted", 0)
    fe_window = blocker_codes_window.get("fallback_exhausted", 0)
    lines.append(f"| Code | All Time | Last {REPORT_WINDOW_DAYS}d |")
    lines.append(f"|------|--------:|---------:|")
    all_codes = sorted(set(list(blocker_codes_all.keys()) + list(blocker_codes_window.keys())))
    for code in all_codes:
        lines.append(f"| {code} | {blocker_codes_all.get(code, 0)} | {blocker_codes_window.get(code, 0)} |")
    lines.append("")
    lines.append(f"**fallback_exhausted trend**: {fe_all} all-time -> {fe_window} last {REPORT_WINDOW_DAYS}d")
    lines.append("")

    # Validation status
    lines.append("## Validation Status")
    lines.append("")

    # Check if gate is routing away from agents below threshold
    gated_agents = []
    for agent, stats in baseline_rates.items():
        if stats["rate"] <= ADAPTIVE_HEALTH_THRESHOLD and stats["total"] > 0:
            gated_agents.append((agent, stats["rate"]))

    if gated_agents:
        lines.append("Agents below gate threshold (all-time):")
        for agent, rate in gated_agents:
            w_stats = window_rates.get(agent, {})
            w_tasks = w_stats.get("total", 0)
            if w_tasks == 0:
                lines.append(f"- {agent}: {rate*100:.0f}% baseline, **0 tasks in window** (gate effective)")
            else:
                lines.append(f"- {agent}: {rate*100:.0f}% baseline, {w_tasks} tasks in window (gate may not be active)")
    else:
        lines.append("No agents currently below the gate threshold.")
    lines.append("")

    # False positive check
    lines.append("## False Positive Analysis")
    lines.append("")
    false_positives = []
    for agent, stats in baseline_rates.items():
        w_stats = window_rates.get(agent, {})
        if (
            stats["rate"] <= ADAPTIVE_HEALTH_THRESHOLD
            and w_stats.get("rate", 0) > ADAPTIVE_HEALTH_THRESHOLD
            and w_stats.get("total", 0) >= 3
        ):
            false_positives.append(agent)

    if false_positives:
        lines.append("Potential false positives (gated agents that improved in window):")
        for agent in false_positives:
            lines.append(f"- {agent}: may warrant threshold review")
    else:
        lines.append("No false positive gate triggers detected.")
    lines.append("")

    # Threshold recommendation
    lines.append("## Threshold Status")
    lines.append("")
    lines.append(f"Current threshold: {ADAPTIVE_HEALTH_THRESHOLD*100:.0f}% over {ADAPTIVE_HEALTH_WINDOW_DAYS}d")
    lines.append("Recommendation: No threshold change until 3+ days of gate decision data collected.")
    lines.append("")

    return "\n".join(lines)


def write_report(cfg: dict | None = None) -> Path:
    """Generate and write the report to the repo root."""
    if cfg is None:
        cfg = load_config()
    report = generate_report(cfg)
    root_dir = Path(cfg.get("root_dir", ".")).expanduser()
    report_path = root_dir / REPORT_FILENAME
    report_path.write_text(report, encoding="utf-8")
    return report_path


def send_telegram_summary(cfg: dict | None = None) -> None:
    """Send a compact health gate summary to Telegram."""
    if cfg is None:
        cfg = load_config()
    token = cfg.get("telegram_bot_token", "")
    chat_id = cfg.get("telegram_chat_id", "")
    if not token or not chat_id:
        return

    root_dir = Path(cfg.get("root_dir", ".")).expanduser()
    metrics_file = root_dir / "runtime" / "metrics" / "agent_stats.jsonl"
    all_records = load_recent_metrics(metrics_file, window_days=365)
    window_records = load_recent_metrics(metrics_file, window_days=REPORT_WINDOW_DAYS)

    baseline = _compute_baseline(all_records)
    window = _compute_window_rates(all_records, REPORT_WINDOW_DAYS)
    decisions = _load_gate_decisions(root_dir / "runtime" / "metrics", REPORT_WINDOW_DAYS)

    total_all = sum(v["total"] for v in baseline.values())
    complete_all = sum(v["successes"] for v in baseline.values())
    total_w = sum(v["total"] for v in window.values())
    complete_w = sum(v["successes"] for v in window.values())

    msg_lines = [
        "Health Gate Report",
        f"Overall: {complete_all}/{total_all} ({complete_all/total_all*100:.0f}%)" if total_all else "Overall: no data",
        f"Last {REPORT_WINDOW_DAYS}d: {complete_w}/{total_w} ({complete_w/total_w*100:.0f}%)" if total_w else f"Last {REPORT_WINDOW_DAYS}d: no data",
        f"Gate decisions: {len(decisions)}",
    ]

    # Skipped agents
    skipped: dict[str, int] = defaultdict(int)
    for d in decisions:
        for agent in d.get("skipped", {}):
            skipped[agent] += 1
    if skipped:
        msg_lines.append("Gated: " + ", ".join(f"{a}({c}x)" for a, c in sorted(skipped.items())))

    msg = "\n".join(msg_lines)
    try:
        subprocess.run(
            [
                "curl", "-s", "-X", "POST",
                f"https://api.telegram.org/bot{token}/sendMessage",
                "-d", f"chat_id={chat_id}",
                "-d", f"text={msg}",
            ],
            capture_output=True,
            timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass


def main():
    cfg = load_config()
    path = write_report(cfg)
    print(f"Report written to {path}")
    send_telegram_summary(cfg)


if __name__ == "__main__":
    main()
