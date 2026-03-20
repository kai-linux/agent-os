"""Weekly log analyzer.

Reads the last 7 days of runtime/metrics/agent_stats.jsonl and
runtime/logs/queue-summary.log, uses Claude Haiku to identify the top 3
failure patterns / performance bottlenecks, and creates one GitHub issue per
identified problem (deduplicated against open issues).  Posts a summary to
Telegram.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from orchestrator.paths import load_config, runtime_paths
from orchestrator.agent_scorer import load_recent_metrics

WINDOW_DAYS = 7
ANALYSIS_MODEL = "haiku"
TOP_N = 3

# Deterministic prompt — no open-ended creative instructions so the model
# sticks to extracting facts rather than inventing issues.
ANALYSIS_PROMPT = """You are an AI agent system analyst reviewing operational logs.
Analyze the data below and identify exactly {top_n} distinct, high-impact problems to fix.
Focus on: repeated failures/blockers, slow task types, and degraded agents.

Return ONLY a JSON array (no markdown fences, no commentary) of exactly {top_n} objects.
Each object must have:
  "title"  - concise GitHub issue title under 70 chars
  "body"   - 3-5 sentence description with root cause and suggested fix, using the sections:
             ## Goal\\n...\\n## Success Criteria\\n...\\n## Constraints\\n- Prefer minimal diffs
  "repo"   - GitHub repo slug, default: "{default_repo}"
  "labels" - JSON array of label strings (choose from: bug, enhancement, agent-os)

--- Agent metrics (JSONL, last 7 days) ---
{metrics_summary}

--- Queue summary log tail (last 7 days) ---
{log_tail}

Return ONLY the JSON array."""


def _read_log_tail(log_file: Path, window_days: int = WINDOW_DAYS, max_lines: int = 300) -> str:
    """Return the tail of the queue-summary log, limited to recent content."""
    if not log_file.exists():
        return "(no queue log found)"
    lines = []
    with log_file.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            lines.append(line.rstrip())
    return "\n".join(lines[-max_lines:]) if lines else "(empty log)"


def _metrics_summary(records: list[dict]) -> str:
    """Compact JSONL for the prompt (capped to avoid token overflow)."""
    if not records:
        return "(no metrics)"
    return "\n".join(json.dumps(r) for r in records[-200:])


def _call_haiku(prompt: str) -> str:
    claude_bin = os.environ.get("CLAUDE_BIN", "claude")
    codex_bin = os.environ.get("CODEX_BIN", "codex")
    errors: list[str] = []
    result = subprocess.run(
        [claude_bin, "-p", prompt, "--model", ANALYSIS_MODEL],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    errors.append(f"Claude exit {result.returncode}: {result.stderr[:300]}")

    result = subprocess.run(
        [codex_bin, "exec", "--skip-git-repo-check", prompt],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    errors.append(f"Codex exit {result.returncode}: {(result.stderr or result.stdout)[:300]}")
    raise RuntimeError(" | ".join(errors))


def _parse_issues(text: str) -> list[dict]:
    """Parse JSON array from Claude response, stripping markdown fences if present."""
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(text)


def _open_issue_exists(repo: str, title: str) -> bool:
    """Return True if an open issue with this exact title already exists."""
    result = subprocess.run(
        ["gh", "issue", "list", "--repo", repo, "--state", "open",
         "--search", title, "--json", "title", "--limit", "20"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return False
    try:
        issues = json.loads(result.stdout or "[]")
        return any(i.get("title", "").strip() == title.strip() for i in issues)
    except Exception:
        return False


def _create_issue(repo: str, title: str, body: str, labels: list[str]) -> str:
    """Create a GitHub issue and return its URL."""
    cmd = ["gh", "issue", "create", "--repo", repo, "--title", title, "--body", body]
    for label in labels:
        cmd += ["--label", label]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"gh issue create failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _send_telegram(cfg: dict, text: str):
    token = str(cfg.get("telegram_bot_token", "")).strip()
    chat_id = str(cfg.get("telegram_chat_id", "")).strip()
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    subprocess.run(
        ["curl", "-sS", "-X", "POST", url,
         "-d", f"chat_id={chat_id}",
         "--data-urlencode", f"text={text}"],
        capture_output=True, text=True, timeout=20,
    )


def _resolve_default_repo(cfg: dict) -> str:
    repo = cfg.get("github_repo", "")
    if repo:
        return repo
    for pv in cfg.get("github_projects", {}).values():
        if not isinstance(pv, dict):
            continue
        for rc in pv.get("repos", []):
            r = rc.get("github_repo", "")
            if r:
                return r
    owner = cfg.get("github_owner", "")
    return f"{owner}/agent-os" if owner else "kai-linux/agent-os"


def run():
    cfg = load_config()
    root = Path(cfg.get("root_dir", ".")).expanduser()
    metrics_file = root / "runtime" / "metrics" / "agent_stats.jsonl"
    paths = runtime_paths(cfg)
    log_file = Path(paths["QUEUE_SUMMARY_LOG"])

    default_repo = _resolve_default_repo(cfg)

    records = load_recent_metrics(metrics_file)
    metrics_text = _metrics_summary(records)
    log_text = _read_log_tail(log_file)

    if not records and log_text in ("(no queue log found)", "(empty log)"):
        print("No data found; skipping analysis.")
        return

    print(f"Analyzing {len(records)} metric record(s) and queue log ({log_file})...")

    prompt = ANALYSIS_PROMPT.format(
        top_n=TOP_N,
        default_repo=default_repo,
        metrics_summary=metrics_text,
        log_tail=log_text,
    )

    try:
        raw = _call_haiku(prompt)
    except Exception as e:
        print(f"Analysis failed: {e}")
        return

    try:
        issues = _parse_issues(raw)
    except Exception as e:
        print(f"Failed to parse Haiku response: {e}\nRaw output:\n{raw[:500]}")
        return

    if not isinstance(issues, list):
        print(f"Unexpected response format (not a list):\n{raw[:300]}")
        return

    created_urls: list[str] = []
    skipped: list[str] = []

    for issue in issues[:TOP_N]:
        title = (issue.get("title") or "").strip()
        body = (issue.get("body") or "").strip()
        repo = (issue.get("repo") or default_repo).strip()
        labels = [str(l) for l in issue.get("labels", []) if l]

        if not title:
            print("Warning: issue with empty title, skipping")
            continue

        if _open_issue_exists(repo, title):
            print(f"Skipping duplicate: {title!r}")
            skipped.append(title)
            continue

        try:
            url = _create_issue(repo, title, body, labels)
            print(f"Created: {url}")
            created_urls.append(url)
        except Exception as e:
            print(f"Failed to create issue {title!r}: {e}")
            skipped.append(title)

    summary_lines = [
        "Weekly Log Analyzer complete",
        f"Issues created: {len(created_urls)} | Skipped (duplicate): {len(skipped)}",
    ]
    for url in created_urls:
        summary_lines.append(f"  {url}")
    summary = "\n".join(summary_lines)
    print(summary)
    _send_telegram(cfg, summary)


if __name__ == "__main__":
    run()
