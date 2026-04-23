"""Generate a public-safe reliability dashboard from existing repo artifacts."""
from __future__ import annotations

import json
import re
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

from orchestrator.agent_scorer import (
    _compute_escalation_rate,
    _compute_merge_cycle_hours,
    compute_success_rates,
    load_recent_metrics,
)
from orchestrator.budgets import budget_snapshot
from orchestrator.paths import load_config


WINDOW_DAYS = 14
PUBLIC_DASHBOARD_DIR = Path("docs") / "reliability"
METRICS_JSON = "metrics.json"
MARKDOWN_FILE = "README.md"
HTML_FILE = "index.html"

_KEY_METRIC_RE = re.compile(r"^- (?P<label>[^:]+): (?P<value>.+)$")
_RATE_WITH_COUNTS_RE = re.compile(r"(?P<pct>\d+)% \((?P<success>\d+)/(?P<total>\d+)\)")
_COUNT_RE = re.compile(r"(?P<count>\d+)$")
_TIME_RE = re.compile(r"(?P<mean>[0-9]+(?:\.[0-9]+)?)h")


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _format_pct(rate: float | None) -> str:
    if rate is None:
        return "n/a"
    return f"{round(rate * 100)}%"


def _format_count_line(count: int | None, total: int | None) -> str:
    if count is None or total is None:
        return "n/a"
    return f"{count}/{total}"


def _read_production_feedback(feedback_path: Path) -> dict:
    parsed = {
        "available": False,
        "generated_at": None,
        "overall_success_rate": None,
        "overall_success_counts": None,
        "escalation_rate": None,
        "escalation_counts": None,
        "mean_completion_hours": None,
        "per_agent": [],
        "top_blockers": [],
    }
    if not feedback_path.exists():
        return parsed

    section = None
    parsed["available"] = True
    for raw_line in feedback_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("Auto-generated:"):
            parsed["generated_at"] = line.split(":", 1)[1].strip()
            continue
        if line.startswith("## "):
            section = line[3:].strip().lower()
            continue
        if not line.startswith("- "):
            continue
        match = _KEY_METRIC_RE.match(line)
        if not match:
            continue
        label = match.group("label").strip().lower()
        value = match.group("value").strip()
        if section == "key metrics":
            rate_match = _RATE_WITH_COUNTS_RE.search(value)
            if label == "overall success rate" and rate_match:
                parsed["overall_success_rate"] = int(rate_match.group("pct")) / 100.0
                parsed["overall_success_counts"] = {
                    "successes": int(rate_match.group("success")),
                    "total": int(rate_match.group("total")),
                }
            elif label == "escalation rate" and rate_match:
                parsed["escalation_rate"] = int(rate_match.group("pct")) / 100.0
                parsed["escalation_counts"] = {
                    "escalated": int(rate_match.group("success")),
                    "total": int(rate_match.group("total")),
                }
            elif label == "mean completion time":
                time_match = _TIME_RE.search(value)
                if time_match:
                    parsed["mean_completion_hours"] = float(time_match.group("mean"))
        elif section == "per-agent performance":
            rate_match = _RATE_WITH_COUNTS_RE.search(value)
            if rate_match:
                parsed["per_agent"].append({
                    "agent": match.group("label").strip(),
                    "rate": int(rate_match.group("pct")) / 100.0,
                    "successes": int(rate_match.group("success")),
                    "total": int(rate_match.group("total")),
                })
        elif section == "top blocker codes":
            count_match = _COUNT_RE.search(value)
            if count_match:
                parsed["top_blockers"].append({
                    "code": match.group("label").strip(),
                    "count": int(count_match.group("count")),
                })

    return parsed


def _daily_series(records: list[dict], today: datetime) -> list[dict]:
    start_date = (today - timedelta(days=WINDOW_DAYS - 1)).date()
    daily: dict[str, dict] = {}
    for offset in range(WINDOW_DAYS):
        day = start_date + timedelta(days=offset)
        daily[day.isoformat()] = {"date": day.isoformat(), "total": 0, "successes": 0, "blocked": 0}

    for rec in records:
        ts = _parse_iso(rec.get("timestamp"))
        if not ts:
            continue
        key = ts.astimezone(timezone.utc).date().isoformat()
        if key not in daily:
            continue
        daily[key]["total"] += 1
        if rec.get("status") == "complete":
            daily[key]["successes"] += 1
        if rec.get("status") == "blocked":
            daily[key]["blocked"] += 1

    series = list(daily.values())
    for day in series:
        total = day["total"]
        day["success_rate"] = round(day["successes"] / total, 4) if total else None
        day["escalation_rate"] = round(day["blocked"] / total, 4) if total else None
    return series


def _trend_summary(records: list[dict], today: datetime) -> dict:
    current_cutoff = today - timedelta(days=7)
    current = [rec for rec in records if (_parse_iso(rec.get("timestamp")) or today) >= current_cutoff]
    prior = [
        rec for rec in records
        if (ts := _parse_iso(rec.get("timestamp"))) and (today - timedelta(days=14)) <= ts < current_cutoff
    ]

    current_rates = compute_success_rates(current)
    prior_rates = compute_success_rates(prior)
    cur_total = sum(item["total"] for item in current_rates.values())
    cur_successes = sum(item["successes"] for item in current_rates.values())
    prev_total = sum(item["total"] for item in prior_rates.values())
    prev_successes = sum(item["successes"] for item in prior_rates.values())
    cur_rate = cur_successes / cur_total if cur_total else None
    prev_rate = prev_successes / prev_total if prev_total else None

    cur_time = _compute_merge_cycle_hours(current)
    prev_time = _compute_merge_cycle_hours(prior)
    cur_escalation = _compute_escalation_rate(current)
    prev_escalation = _compute_escalation_rate(prior)

    return {
        "current_week": {
            "success_rate": cur_rate,
            "successes": cur_successes,
            "total": cur_total,
            "mean_completion_hours": cur_time["mean"],
            "escalation_rate": cur_escalation["rate"] if cur_total else None,
        },
        "prior_week": {
            "success_rate": prev_rate,
            "successes": prev_successes,
            "total": prev_total,
            "mean_completion_hours": prev_time["mean"],
            "escalation_rate": prev_escalation["rate"] if prev_total else None,
        },
    }


def _fetch_github_metrics(repo_slug: str) -> dict:
    result: dict = {"stars": None, "forks": None, "open_issues": None}
    try:
        proc = subprocess.run(
            ["gh", "api", f"repos/{repo_slug}",
             "--jq", "{stars: .stargazers_count, forks: .forks_count, open_issues: .open_issues_count}"],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode == 0:
            data = json.loads(proc.stdout.strip())
            result.update(data)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        pass
    return result


def _detect_repo_slug(root: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        )
        url = proc.stdout.strip()
        m = re.search(r"github\.com[:/](.+?)(?:\.git)?$", url)
        if m:
            return m.group(1)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def build_dashboard_snapshot(root: Path) -> dict:
    metrics_file = root / "runtime" / "metrics" / "agent_stats.jsonl"
    feedback_path = root / "PRODUCTION_FEEDBACK.md"
    today = datetime.now(tz=timezone.utc)
    records = load_recent_metrics(metrics_file, window_days=WINDOW_DAYS)
    feedback = _read_production_feedback(feedback_path)

    repo_slug = _detect_repo_slug(root)
    github_metrics = _fetch_github_metrics(repo_slug) if repo_slug else {"stars": None, "forks": None, "open_issues": None}

    rates = compute_success_rates(records)
    cycle = _compute_merge_cycle_hours(records)
    escalation = _compute_escalation_rate(records)

    total = sum(item["total"] for item in rates.values())
    successes = sum(item["successes"] for item in rates.values())
    overall_rate = successes / total if total else None

    blocker_counts = Counter(
        str(rec.get("blocker_code") or "").strip()
        for rec in records
        if str(rec.get("blocker_code") or "").strip() and str(rec.get("blocker_code") or "").strip() != "none"
    )

    latest_metric = max((_parse_iso(rec.get("timestamp")) for rec in records), default=None)
    per_agent = [
        {
            "agent": agent,
            "rate": stats["rate"],
            "successes": stats["successes"],
            "total": stats["total"],
        }
        for agent, stats in sorted(rates.items(), key=lambda item: (-item[1]["rate"], -item[1]["total"], item[0]))
    ]
    if not per_agent and feedback["per_agent"]:
        per_agent = feedback["per_agent"]

    top_blockers = [
        {"code": code, "count": count}
        for code, count in blocker_counts.most_common(5)
    ]
    if not top_blockers and feedback["top_blockers"]:
        top_blockers = feedback["top_blockers"]

    snapshot = {
        "generated_at": today.isoformat(),
        "window_days": WINDOW_DAYS,
        "sources": {
            "agent_stats": {
                "path": str(metrics_file.relative_to(root)),
                "available": metrics_file.exists(),
                "records": len(records),
                "latest_timestamp": latest_metric.isoformat() if latest_metric else None,
            },
            "production_feedback": {
                "path": str(feedback_path.relative_to(root)),
                "available": feedback["available"],
                "generated_at": feedback["generated_at"],
            },
        },
        "summary": {
            "task_success_rate": {
                "rate": overall_rate if overall_rate is not None else feedback["overall_success_rate"],
                "successes": successes if total else (feedback["overall_success_counts"] or {}).get("successes"),
                "total": total if total else (feedback["overall_success_counts"] or {}).get("total"),
            },
            "mean_completion_time_hours": cycle["mean"] if cycle["mean"] is not None else feedback["mean_completion_hours"],
            "escalation_rate": {
                "rate": escalation["rate"] if total else feedback["escalation_rate"],
                "escalated": escalation["escalated"] if total else (feedback["escalation_counts"] or {}).get("escalated"),
                "total": escalation["total"] if total else (feedback["escalation_counts"] or {}).get("total"),
            },
        },
        "per_agent": per_agent,
        "top_blockers": top_blockers,
        "rolling_14_day": _daily_series(records, today),
        "momentum": _trend_summary(records, today),
        "github": {
            "repo": repo_slug,
            "stars": github_metrics.get("stars"),
            "forks": github_metrics.get("forks"),
            "open_issues": github_metrics.get("open_issues"),
        },
        "budgets": _budget_snapshot_safe(root, [item["agent"] for item in per_agent]),
    }
    return snapshot


def _budget_snapshot_safe(root: Path, agents: list[str]) -> dict:
    """Best-effort budget snapshot — returns an empty record when config is absent."""
    try:
        cfg = load_config()
        cfg.setdefault("root_dir", str(root))
    except Exception:
        return {"month_key": None, "enabled": False, "per_agent": [], "total_spend_usd": 0.0}
    return budget_snapshot(cfg, agents=agents)


def _text_bar(value: float | None, width: int = 12) -> str:
    if value is None:
        return "." * width
    filled = max(0, min(width, round(value * width)))
    return "#" * filled + "." * (width - filled)


def render_markdown(snapshot: dict) -> str:
    success = snapshot["summary"]["task_success_rate"]
    escalation = snapshot["summary"]["escalation_rate"]
    momentum = snapshot["momentum"]
    no_data = (
        snapshot["sources"]["agent_stats"]["records"] == 0
        and not snapshot["sources"]["production_feedback"]["available"]
    )
    lines = [
        "# Reliability Dashboard",
        "",
        f"Updated: {snapshot['generated_at']}",
        f"Window: rolling {snapshot['window_days']} days",
        f"Sources: `{snapshot['sources']['agent_stats']['path']}` + `{snapshot['sources']['production_feedback']['path']}`",
        "",
    ]
    if no_data:
        lines.extend([
            "> No public-safe metrics snapshot is available in this checkout yet.",
            "> Run `bin/run_public_dashboard.sh` in the live repo environment to publish the first refresh.",
            "",
        ])
    gh = snapshot.get("github", {})
    lines.extend([
        "| Metric | Value |",
        "|---|---|",
        f"| Task success rate | {_format_pct(success['rate'])} ({_format_count_line(success['successes'], success['total'])}) |",
        f"| Mean completion time | {snapshot['summary']['mean_completion_time_hours']:.1f}h |" if snapshot["summary"]["mean_completion_time_hours"] is not None else "| Mean completion time | n/a |",
        f"| Escalation rate | {_format_pct(escalation['rate'])} ({_format_count_line(escalation['escalated'], escalation['total'])}) |",
        f"| GitHub stars | {gh['stars'] if gh.get('stars') is not None else 'n/a'} |",
        f"| GitHub forks | {gh['forks'] if gh.get('forks') is not None else 'n/a'} |",
        "",
        "## 14-Day Momentum",
        "",
        "| Period | Success | Mean time | Escalation |",
        "|---|---|---|---|",
        f"| Last 7 days | {_format_pct(momentum['current_week']['success_rate'])} ({_format_count_line(momentum['current_week']['successes'], momentum['current_week']['total'])}) | "
        + (f"{momentum['current_week']['mean_completion_hours']:.1f}h" if momentum["current_week"]["mean_completion_hours"] is not None else "n/a")
        + " | "
        + _format_pct(momentum["current_week"]["escalation_rate"])
        + " |",
        f"| Prior 7 days | {_format_pct(momentum['prior_week']['success_rate'])} ({_format_count_line(momentum['prior_week']['successes'], momentum['prior_week']['total'])}) | "
        + (f"{momentum['prior_week']['mean_completion_hours']:.1f}h" if momentum["prior_week"]["mean_completion_hours"] is not None else "n/a")
        + " | "
        + _format_pct(momentum["prior_week"]["escalation_rate"])
        + " |",
        "",
        "## Daily Trend",
        "",
        "| Date | Success | Escalation | Volume |",
        "|---|---|---|---|",
    ])
    for day in snapshot["rolling_14_day"]:
        lines.append(
            f"| {day['date']} | `{_text_bar(day['success_rate'])}` { _format_pct(day['success_rate']) } | "
            f"`{_text_bar(day['escalation_rate'])}` { _format_pct(day['escalation_rate']) } | {day['total']} |"
        )

    lines.extend(["", "## Per-Agent Breakdown", "", "| Agent | Success | Volume |", "|---|---|---|"])
    if snapshot["per_agent"]:
        for item in snapshot["per_agent"]:
            lines.append(
                f"| {item['agent']} | {_format_pct(item['rate'])} ({item['successes']}/{item['total']}) | {item['total']} |"
            )
    else:
        lines.append("| No recent data | n/a | 0 |")

    lines.extend(["", "## Top Blocker Categories", ""])
    if snapshot["top_blockers"]:
        for item in snapshot["top_blockers"]:
            lines.append(f"- `{item['code']}`: {item['count']}")
    else:
        lines.append("- No blocker categories in the current window.")

    budgets = snapshot.get("budgets") or {}
    if budgets.get("enabled") and budgets.get("per_agent"):
        lines.extend([
            "",
            f"## Monthly Spend — {budgets.get('month_key', 'n/a')}",
            "",
            "| Agent | Spend | Hard-stop | Remaining | Status |",
            "|---|---|---|---|---|",
        ])
        for item in budgets["per_agent"]:
            hard = item.get("hard_stop_usd")
            remaining = item.get("remaining_usd")
            status = "hard-stopped" if item.get("hard_stopped") else "ok"
            lines.append(
                f"| {item['agent']} "
                f"| ${item['spend_usd']:.2f} "
                f"| {'unlimited' if hard is None else f'${float(hard):.2f}'} "
                f"| {'n/a' if remaining is None else f'${float(remaining):.2f}'} "
                f"| {status} |"
            )

    lines.extend([
        "",
        "## Notes",
        "",
        "- Public-safe aggregates only: no task bodies, escalation notes, or operator-sensitive logs.",
        "- This page prefers live `agent_stats.jsonl` aggregates and falls back to `PRODUCTION_FEEDBACK.md` when needed.",
    ])
    return "\n".join(lines) + "\n"


def render_html(snapshot: dict) -> str:
    success = snapshot["summary"]["task_success_rate"]
    escalation = snapshot["summary"]["escalation_rate"]
    mean_hours = snapshot["summary"]["mean_completion_time_hours"]
    no_data = (
        snapshot["sources"]["agent_stats"]["records"] == 0
        and not snapshot["sources"]["production_feedback"]["available"]
    )

    def metric_card(title: str, value: str, detail: str) -> str:
        return (
            '<section class="card">'
            f"<p class=\"eyebrow\">{escape(title)}</p>"
            f"<p class=\"metric\">{escape(value)}</p>"
            f"<p class=\"detail\">{escape(detail)}</p>"
            "</section>"
        )

    gh = snapshot.get("github", {})
    cards = "\n".join([
        metric_card(
            "Task success rate",
            _format_pct(success["rate"]),
            _format_count_line(success["successes"], success["total"]),
        ),
        metric_card(
            "Mean completion time",
            f"{mean_hours:.1f}h" if mean_hours is not None else "n/a",
            "completed tasks only",
        ),
        metric_card(
            "Escalation rate",
            _format_pct(escalation["rate"]),
            _format_count_line(escalation["escalated"], escalation["total"]),
        ),
        metric_card(
            "GitHub stars",
            str(gh.get("stars")) if gh.get("stars") is not None else "n/a",
            "stargazers",
        ),
        metric_card(
            "GitHub forks",
            str(gh.get("forks")) if gh.get("forks") is not None else "n/a",
            "forks",
        ),
    ])

    trend_rows = []
    for day in snapshot["rolling_14_day"]:
        trend_rows.append(
            "<tr>"
            f"<td>{escape(day['date'])}</td>"
            f"<td><code>{escape(_text_bar(day['success_rate']))}</code> {escape(_format_pct(day['success_rate']))}</td>"
            f"<td><code>{escape(_text_bar(day['escalation_rate']))}</code> {escape(_format_pct(day['escalation_rate']))}</td>"
            f"<td>{day['total']}</td>"
            "</tr>"
        )

    agent_rows = []
    for item in snapshot["per_agent"]:
        agent_rows.append(
            "<tr>"
            f"<td>{escape(item['agent'])}</td>"
            f"<td>{escape(_format_pct(item['rate']))}</td>"
            f"<td>{item['successes']}/{item['total']}</td>"
            "</tr>"
        )
    if not agent_rows:
        agent_rows.append("<tr><td>No recent data</td><td>n/a</td><td>0/0</td></tr>")

    blocker_items = "".join(
        f"<li><code>{escape(item['code'])}</code> <span>{item['count']}</span></li>"
        for item in snapshot["top_blockers"]
    ) or "<li>No blocker categories in the current window.</li>"

    momentum = snapshot["momentum"]
    momentum_rows = [
        (
            "Last 7 days",
            momentum["current_week"]["success_rate"],
            momentum["current_week"]["successes"],
            momentum["current_week"]["total"],
            momentum["current_week"]["mean_completion_hours"],
            momentum["current_week"]["escalation_rate"],
        ),
        (
            "Prior 7 days",
            momentum["prior_week"]["success_rate"],
            momentum["prior_week"]["successes"],
            momentum["prior_week"]["total"],
            momentum["prior_week"]["mean_completion_hours"],
            momentum["prior_week"]["escalation_rate"],
        ),
    ]
    momentum_html = "".join(
        "<tr>"
        f"<td>{label}</td>"
        f"<td>{escape(_format_pct(rate))} ({escape(_format_count_line(successes, total))})</td>"
        f"<td>{f'{mean_hours_row:.1f}h' if mean_hours_row is not None else 'n/a'}</td>"
        f"<td>{escape(_format_pct(escalation_rate))}</td>"
        "</tr>"
        for label, rate, successes, total, mean_hours_row, escalation_rate in momentum_rows
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Agent OS Reliability Dashboard</title>
  <style>
    :root {{
      --bg: #f4efe6;
      --panel: #fffdf8;
      --ink: #172121;
      --muted: #5c6b73;
      --line: #d8cfc2;
      --accent: #0f766e;
      --accent-soft: #d8f3ef;
      --warn: #9a3412;
      --warn-soft: #ffedd5;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Georgia, "Times New Roman", serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(15,118,110,0.10), transparent 30%),
        linear-gradient(180deg, #f6f1e7 0%, #efe7d8 100%);
    }}
    main {{ max-width: 1120px; margin: 0 auto; padding: 48px 20px 72px; }}
    h1, h2 {{ font-weight: 700; letter-spacing: -0.02em; }}
    p {{ line-height: 1.5; }}
    .lede {{ max-width: 760px; color: var(--muted); margin-bottom: 28px; }}
    .meta {{ color: var(--muted); font-size: 0.95rem; margin-bottom: 28px; }}
    .cards {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 16px;
      margin: 24px 0 36px;
    }}
    .card, .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 18px;
      box-shadow: 0 14px 40px rgba(23, 33, 33, 0.06);
    }}
    .eyebrow {{
      margin: 0 0 8px;
      font-size: 0.85rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
    }}
    .metric {{
      margin: 0;
      font-size: clamp(2rem, 4vw, 2.8rem);
      color: var(--accent);
    }}
    .detail {{ margin: 6px 0 0; color: var(--muted); }}
    .grid {{
      display: grid;
      grid-template-columns: 1.3fr 1fr;
      gap: 18px;
      margin-top: 18px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.95rem;
    }}
    th, td {{
      padding: 10px 0;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
    }}
    th {{ color: var(--muted); font-weight: 600; }}
    code {{
      font-family: "SFMono-Regular", Consolas, "Liberation Mono", monospace;
      background: #f6f3ee;
      padding: 2px 4px;
      border-radius: 4px;
    }}
    ul {{
      list-style: none;
      padding: 0;
      margin: 0;
    }}
    li {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      padding: 10px 0;
      border-bottom: 1px solid var(--line);
    }}
    .callout {{
      margin-top: 18px;
      padding: 14px 16px;
      border-radius: 14px;
      background: var(--accent-soft);
      color: var(--ink);
    }}
    .footer-note {{
      margin-top: 24px;
      color: var(--muted);
      font-size: 0.92rem;
    }}
    @media (max-width: 820px) {{
      .grid {{ grid-template-columns: 1fr; }}
      main {{ padding-top: 32px; }}
    }}
  </style>
</head>
<body>
  <main>
    <h1>Agent OS Reliability Dashboard</h1>
    <p class="lede">Public, aggregate reliability metrics for technical builders evaluating whether Agent OS ships useful work consistently.</p>
    <p class="meta">Updated {escape(snapshot['generated_at'])} | Rolling {snapshot['window_days']}-day window | Sources: {escape(snapshot['sources']['agent_stats']['path'])} + {escape(snapshot['sources']['production_feedback']['path'])}</p>
    <div class="cards">
      {cards}
    </div>
    {"<p class=\"callout\">No public-safe metrics snapshot is available in this checkout yet. Run <code>bin/run_public_dashboard.sh</code> in the live repo environment to publish the first refresh.</p>" if no_data else ""}
    <section class="panel">
      <h2>14-Day Momentum</h2>
      <table>
        <thead>
          <tr><th>Period</th><th>Success</th><th>Mean time</th><th>Escalation</th></tr>
        </thead>
        <tbody>
          {momentum_html}
        </tbody>
      </table>
      <p class="callout">This view intentionally stays aggregate-only: no task bodies, escalation notes, internal logs, or operator-sensitive details.</p>
    </section>
    <div class="grid">
      <section class="panel">
        <h2>Daily Trend</h2>
        <table>
          <thead>
            <tr><th>Date</th><th>Success</th><th>Escalation</th><th>Volume</th></tr>
          </thead>
          <tbody>
            {''.join(trend_rows)}
          </tbody>
        </table>
      </section>
      <section class="panel">
        <h2>Per-Agent Breakdown</h2>
        <table>
          <thead>
            <tr><th>Agent</th><th>Success</th><th>Completed</th></tr>
          </thead>
          <tbody>
            {''.join(agent_rows)}
          </tbody>
        </table>
      </section>
    </div>
    <div class="grid">
      <section class="panel">
        <h2>Top Blocker Categories</h2>
        <ul>{blocker_items}</ul>
      </section>
      <section class="panel">
        <h2>Source Freshness</h2>
        <table>
          <tbody>
            <tr><th>agent_stats.jsonl</th><td>{snapshot['sources']['agent_stats']['records']} records</td></tr>
            <tr><th>Latest runtime metric</th><td>{escape(snapshot['sources']['agent_stats']['latest_timestamp'] or 'n/a')}</td></tr>
            <tr><th>PRODUCTION_FEEDBACK.md</th><td>{escape(snapshot['sources']['production_feedback']['generated_at'] or 'n/a')}</td></tr>
          </tbody>
        </table>
      </section>
    </div>
    <p class="footer-note">GitHub-friendly fallback: <a href="./README.md">README.md</a> | Raw snapshot: <a href="./metrics.json">metrics.json</a></p>
  </main>
</body>
</html>
"""


def write_dashboard(root: Path) -> dict:
    snapshot = build_dashboard_snapshot(root)
    output_dir = root / PUBLIC_DASHBOARD_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / METRICS_JSON).write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output_dir / MARKDOWN_FILE).write_text(render_markdown(snapshot), encoding="utf-8")
    (output_dir / HTML_FILE).write_text(render_html(snapshot), encoding="utf-8")
    return snapshot


def run() -> None:
    root = Path.cwd()
    snapshot = write_dashboard(root)
    print(
        "Wrote public reliability dashboard to "
        f"{(root / PUBLIC_DASHBOARD_DIR / HTML_FILE)} "
        f"using {snapshot['sources']['agent_stats']['records']} metric record(s)."
    )


if __name__ == "__main__":
    run()
