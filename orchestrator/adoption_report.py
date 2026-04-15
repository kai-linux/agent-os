"""Weekly adoption funnel monitoring and impact report generator.

Reads GitHub traffic data (views, clones, referrers, paths) and star/fork
history, correlates traffic sources with conversion, identifies bottlenecks,
and produces an actionable 1-page weekly report.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from orchestrator.paths import load_config

REPORT_DIR = Path("docs") / "adoption-reports"
REPORT_FILE = "WEEKLY_ADOPTION_REPORT.md"


def _evidence_dir(cfg: dict, repo_name: str) -> Path:
    base = Path(cfg.get("evidence_dir", "~/.local/share/agent-os/evidence")).expanduser()
    return base / repo_name


def _read_jsonl(path: Path, max_age_days: int | None = None) -> list[dict]:
    if not path.exists():
        return []
    cutoff = None
    if max_age_days:
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=max_age_days)
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            if cutoff:
                ts = rec.get("timestamp") or rec.get("snapshot_at", "")
                if ts and ts < cutoff.isoformat():
                    continue
            records.append(rec)
        except json.JSONDecodeError:
            continue
    return records


def _fetch_live_traffic(repo: str) -> dict:
    result = {
        "views": {"count": 0, "uniques": 0, "daily": []},
        "clones": {"count": 0, "uniques": 0, "daily": []},
        "referrers": [],
        "paths": [],
    }
    try:
        proc = subprocess.run(
            ["gh", "api", f"repos/{repo}/traffic/views"],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode == 0:
            data = json.loads(proc.stdout)
            result["views"] = {
                "count": data.get("count", 0),
                "uniques": data.get("uniques", 0),
                "daily": [
                    {"date": v["timestamp"][:10], "count": v["count"], "uniques": v["uniques"]}
                    for v in data.get("views", [])
                ],
            }
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        pass

    try:
        proc = subprocess.run(
            ["gh", "api", f"repos/{repo}/traffic/clones"],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode == 0:
            data = json.loads(proc.stdout)
            result["clones"] = {
                "count": data.get("count", 0),
                "uniques": data.get("uniques", 0),
                "daily": [
                    {"date": c["timestamp"][:10], "count": c["count"], "uniques": c["uniques"]}
                    for c in data.get("clones", [])
                ],
            }
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        pass

    try:
        proc = subprocess.run(
            ["gh", "api", f"repos/{repo}/traffic/popular/referrers"],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode == 0:
            result["referrers"] = json.loads(proc.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        pass

    try:
        proc = subprocess.run(
            ["gh", "api", f"repos/{repo}/traffic/popular/paths"],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode == 0:
            result["paths"] = json.loads(proc.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        pass

    return result


def _fetch_repo_stats(repo: str) -> dict:
    try:
        proc = subprocess.run(
            ["gh", "api", f"repos/{repo}",
             "--jq", "{stars: .stargazers_count, forks: .forks_count, watchers: .subscribers_count}"],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode == 0:
            return json.loads(proc.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        pass
    return {"stars": 0, "forks": 0, "watchers": 0}


def _star_growth(metrics_history: list[dict]) -> dict:
    if len(metrics_history) < 2:
        return {"start": 0, "end": 0, "delta": 0, "first_date": "n/a", "last_date": "n/a"}
    first = metrics_history[0]
    last = metrics_history[-1]
    return {
        "start": first.get("stars", 0),
        "end": last.get("stars", 0),
        "delta": last.get("stars", 0) - first.get("stars", 0),
        "first_date": (first.get("timestamp", ""))[:10],
        "last_date": (last.get("timestamp", ""))[:10],
    }


def _fork_growth(metrics_history: list[dict]) -> dict:
    if len(metrics_history) < 2:
        return {"start": 0, "end": 0, "delta": 0}
    return {
        "start": metrics_history[0].get("forks", 0),
        "end": metrics_history[-1].get("forks", 0),
        "delta": metrics_history[-1].get("forks", 0) - metrics_history[0].get("forks", 0),
    }


def _identify_bottlenecks(traffic: dict, repo_stats: dict) -> list[dict]:
    bottlenecks = []
    unique_visitors = traffic["views"]["uniques"]
    stars = repo_stats.get("stars", 0)

    if unique_visitors < 10:
        bottlenecks.append({
            "stage": "Top of funnel (traffic)",
            "severity": "critical",
            "finding": f"Only {unique_visitors} unique visitors in 14 days. "
                       "All downstream conversion optimization is moot without traffic.",
            "recommendation": "Prioritize distribution: post case study to dev.to, HN, "
                              "r/programming, r/SideProject. Each platform needs tailored framing.",
        })

    referrers = traffic.get("referrers", [])
    external_referrers = [r for r in referrers if r.get("referrer", "") != "github.com"]
    if not external_referrers:
        bottlenecks.append({
            "stage": "Referral diversity",
            "severity": "high",
            "finding": "Zero external referral sources. All traffic comes from github.com internal navigation.",
            "recommendation": "Content distribution is the #1 lever. "
                              "Cross-post to communities where technical builders already browse.",
        })

    if unique_visitors > 0 and stars > 0:
        conversion = stars / unique_visitors
        if conversion < 0.05:
            bottlenecks.append({
                "stage": "Visitor-to-star conversion",
                "severity": "medium",
                "finding": f"Visitor-to-star conversion rate: {conversion:.1%} "
                           f"({stars} stars / {unique_visitors} visitors). "
                           "Below 5% benchmark for dev tools.",
                "recommendation": "Improve README first impression: clear value prop in 10 seconds, "
                                  "demo GIF above fold, prominent star CTA.",
            })

    clone_uniques = traffic["clones"]["uniques"]
    clone_total = traffic["clones"]["count"]
    if clone_uniques > 20 and clone_total / max(clone_uniques, 1) > 3:
        bottlenecks.append({
            "stage": "Clone traffic (noise)",
            "severity": "info",
            "finding": f"{clone_total} clones from {clone_uniques} unique cloners — "
                       "likely dominated by automated worktree operations, not real users.",
            "recommendation": "Do not use clone metrics as adoption signal. "
                              "Focus on unique visitors and stars.",
        })

    if not bottlenecks:
        bottlenecks.append({
            "stage": "General",
            "severity": "info",
            "finding": "No critical bottlenecks identified. Continue monitoring.",
            "recommendation": "Maintain current distribution cadence.",
        })

    return bottlenecks


def _render_daily_table(daily: list[dict], label: str) -> list[str]:
    if not daily:
        return [f"No daily {label} data available."]
    lines = [
        f"| Date | {label} | Unique |",
        "|---|---|---|",
    ]
    for d in daily:
        lines.append(f"| {d['date']} | {d['count']} | {d['uniques']} |")
    return lines


def generate_report(repo: str = "kai-linux/agent-os") -> str:
    cfg = load_config()
    repo_name = repo.split("/")[-1]
    evi_dir = _evidence_dir(cfg, repo_name)

    metrics_history = _read_jsonl(evi_dir / "github_metrics_history.jsonl", max_age_days=30)
    traffic = _fetch_live_traffic(repo)
    repo_stats = _fetch_repo_stats(repo)
    star_trend = _star_growth(metrics_history)
    fork_trend = _fork_growth(metrics_history)
    bottlenecks = _identify_bottlenecks(traffic, repo_stats)

    now = datetime.now(tz=timezone.utc)
    referrers = traffic.get("referrers", [])
    paths = traffic.get("paths", [])

    lines = [
        "# Weekly Adoption Report",
        "",
        f"**Repo:** {repo}",
        f"**Generated:** {now.strftime('%Y-%m-%d %H:%M UTC')}",
        f"**Window:** 14-day rolling (GitHub traffic retention limit)",
        "",
        "---",
        "",
        "## Baseline Metrics",
        "",
        "| Metric | Current | 14-day delta |",
        "|---|---|---|",
        f"| Stars | {repo_stats.get('stars', 0)} | {star_trend['delta']:+d} |",
        f"| Forks | {repo_stats.get('forks', 0)} | {fork_trend['delta']:+d} |",
        f"| Unique visitors (14d) | {traffic['views']['uniques']} | — |",
        f"| Total views (14d) | {traffic['views']['count']} | — |",
        f"| Unique cloners (14d) | {traffic['clones']['uniques']} | — |",
        "",
        "## Top Referral Channels",
        "",
    ]

    if referrers:
        lines.extend([
            "| Source | Views | Unique visitors | Est. conversion |",
            "|---|---|---|---|",
        ])
        for ref in referrers:
            source = ref.get("referrer", "unknown")
            count = ref.get("count", 0)
            uniques = ref.get("uniques", 0)
            conv = "n/a"
            if uniques > 0 and repo_stats.get("stars", 0) > 0:
                conv = f"~{repo_stats['stars'] / uniques:.0%} (rough)"
            lines.append(f"| {source} | {count} | {uniques} | {conv} |")
    else:
        lines.append("No referral sources detected in the current window.")

    lines.extend([
        "",
        "## Popular Pages",
        "",
    ])
    if paths:
        lines.extend([
            "| Page | Views | Unique |",
            "|---|---|---|",
        ])
        for p in paths[:8]:
            lines.append(f"| `{p.get('path', '')}` | {p.get('count', 0)} | {p.get('uniques', 0)} |")
    else:
        lines.append("No page view data available.")

    lines.extend([
        "",
        "## Daily Views Breakdown",
        "",
    ])
    lines.extend(_render_daily_table(traffic["views"]["daily"], "Views"))

    lines.extend([
        "",
        "## Star & Fork Growth Trend",
        "",
        f"- Stars: {star_trend['start']} → {star_trend['end']} "
        f"({star_trend['delta']:+d}) over {star_trend['first_date']} to {star_trend['last_date']}",
        f"- Forks: {fork_trend['start']} → {fork_trend['end']} ({fork_trend['delta']:+d})",
        "",
    ])

    if len(metrics_history) >= 4:
        mid = len(metrics_history) // 2
        first_half_stars = metrics_history[mid].get("stars", 0) - metrics_history[0].get("stars", 0)
        second_half_stars = metrics_history[-1].get("stars", 0) - metrics_history[mid].get("stars", 0)
        trend_word = "accelerating" if second_half_stars > first_half_stars else (
            "decelerating" if second_half_stars < first_half_stars else "steady"
        )
        lines.append(f"- Trend: {trend_word} (first half: +{first_half_stars}, second half: +{second_half_stars})")
        lines.append("")

    lines.extend([
        "## Conversion Bottlenecks",
        "",
    ])
    for i, b in enumerate(bottlenecks, 1):
        lines.extend([
            f"### {i}. {b['stage']} [{b['severity']}]",
            "",
            f"**Finding:** {b['finding']}",
            "",
            f"**Recommendation:** {b['recommendation']}",
            "",
        ])

    lines.extend([
        "## Optimization Priorities (Next Week)",
        "",
        "1. **Distribution first**: traffic is the binding constraint. "
        "No README/conversion optimization matters without visitors.",
        "2. **Track referrer attribution**: after posting to external platforms, "
        "monitor which sources drive the highest visitor→star conversion rate.",
        "3. **Measure**: re-run this report next week to compare baseline vs. post-distribution metrics.",
        "",
        "---",
        "",
        "*Generated by `orchestrator/adoption_report.py` from native GitHub analytics.*",
        "",
    ])

    return "\n".join(lines)


def _build_telegram_summary(repo: str, traffic: dict, repo_stats: dict,
                            bottlenecks: list[dict]) -> str:
    lines = [
        "📊 Weekly Adoption Report",
        f"Stars: {repo_stats.get('stars', 0)} | Forks: {repo_stats.get('forks', 0)}",
        f"Visitors (14d): {traffic['views']['uniques']} unique / {traffic['views']['count']} total",
    ]
    referrers = traffic.get("referrers", [])
    if referrers:
        top = referrers[0]
        lines.append(f"Top referrer: {top.get('referrer', '?')} ({top.get('uniques', 0)} unique)")
    top_bottleneck = bottlenecks[0] if bottlenecks else None
    if top_bottleneck:
        lines.append(f"#1 bottleneck: {top_bottleneck['stage']} ({top_bottleneck['severity']})")
    return "\n".join(lines)


def write_report(root: Path, repo: str = "kai-linux/agent-os") -> Path:
    report_content = generate_report(repo)
    output_dir = root / REPORT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(tz=timezone.utc)
    dated_name = f"adoption-report-{now.strftime('%Y-%m-%d')}.md"
    dated_path = output_dir / dated_name
    latest_path = output_dir / REPORT_FILE

    dated_path.write_text(report_content, encoding="utf-8")
    latest_path.write_text(report_content, encoding="utf-8")

    return dated_path


def run() -> None:
    cfg = load_config()
    root = Path.cwd()
    repo = "kai-linux/agent-os"

    for project in cfg.get("github_projects", {}).values():
        for r in project.get("repos", []):
            if r.get("repo"):
                repo = r["repo"]
                break

    report_path = write_report(root, repo)
    print(f"Wrote adoption report to {report_path}")

    traffic = _fetch_live_traffic(repo)
    repo_stats = _fetch_repo_stats(repo)
    bottlenecks = _identify_bottlenecks(traffic, repo_stats)
    summary = _build_telegram_summary(repo, traffic, repo_stats, bottlenecks)

    try:
        from orchestrator.queue import send_telegram
        send_telegram(cfg, summary)
        print("Sent Telegram summary.")
    except Exception:
        print("Telegram send skipped (not configured or failed).")


if __name__ == "__main__":
    run()
