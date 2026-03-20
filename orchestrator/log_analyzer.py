"""Weekly log analyzer.

Reads the last 7 days of runtime metrics and queue logs, combines them with
structured findings from the scorer, synthesizes the top remediation tasks,
and creates deduplicated GitHub issues with bounded evidence and reasoning.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from orchestrator.agent_scorer import (
    WINDOW_DAYS,
    build_degradation_findings,
    findings_path as scorer_findings_path,
    load_recent_metrics,
)
from orchestrator.paths import load_config, runtime_paths

ANALYSIS_MODEL = "haiku"
TOP_N = 3
BLOCKER_WINDOW_DAYS = 1
BLOCKER_THRESHOLD = 3
QUEUE_LOG_EVIDENCE_ID = "queue_log_tail"
METRICS_EVIDENCE_ID = "metrics_window"

ANALYSIS_PROMPT = """You are an AI agent system analyst reviewing operational evidence.
Produce exactly {top_n} distinct remediation tasks that cover the highest-impact problems.
Use the structured findings first, then use the raw queue log only to refine or combine them.
Do not create multiple issues for the same underlying problem. Merge overlapping evidence into one task.

Return ONLY a JSON array (no markdown fences, no commentary) of exactly {top_n} objects.
Each object must have:
  "title": concise GitHub issue title under 70 chars
  "repo": GitHub repo slug, default "{default_repo}"
  "labels": JSON array chosen from ["bug", "enhancement", "agent-os", "prio:high", "prio:normal"]
  "goal": one sentence
  "success_criteria": JSON array of 2-4 short bullets
  "constraints": JSON array of 1-3 short bullets
  "reasoning": one sentence explaining why this is the best single remediation task for the evidence
  "evidence_ids": JSON array of evidence ids from the structured findings section below and/or "{queue_log_evidence_id}" and "{metrics_evidence_id}"

--- Structured findings ---
{structured_findings}

--- Agent metrics (JSONL, last 7 days) ---
{metrics_summary}

--- Queue summary log tail (last 7 days) ---
{log_tail}

Return ONLY the JSON array."""


def _read_log_tail(log_file: Path, max_lines: int = 300) -> str:
    if not log_file.exists():
        return "(no queue log found)"
    lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-max_lines:]) if lines else "(empty log)"


def _metrics_summary(records: list[dict]) -> str:
    if not records:
        return "(no metrics)"
    return "\n".join(json.dumps(r, sort_keys=True) for r in records[-200:])


def _call_haiku(prompt: str) -> str:
    claude_bin = os.environ.get("CLAUDE_BIN", "claude")
    codex_bin = os.environ.get("CODEX_BIN", "codex")
    errors: list[str] = []
    result = subprocess.run(
        [claude_bin, "-p", prompt, "--model", ANALYSIS_MODEL],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    errors.append(f"Claude exit {result.returncode}: {result.stderr[:300]}")

    result = subprocess.run(
        [codex_bin, "exec", "--skip-git-repo-check", prompt],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    errors.append(f"Codex exit {result.returncode}: {(result.stderr or result.stdout)[:300]}")
    raise RuntimeError(" | ".join(errors))


def _parse_issues(text: str) -> list[dict]:
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return json.loads(text)


def _open_issue_exists(repo: str, title: str) -> bool:
    result = subprocess.run(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--search",
            title,
            "--json",
            "title",
            "--limit",
            "20",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    try:
        issues = json.loads(result.stdout or "[]")
        return any(i.get("title", "").strip() == title.strip() for i in issues)
    except Exception:
        return False


def _create_issue(repo: str, title: str, body: str, labels: list[str]) -> str:
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
        [
            "curl",
            "-sS",
            "-X",
            "POST",
            url,
            "-d",
            f"chat_id={chat_id}",
            "--data-urlencode",
            f"text={text}",
        ],
        capture_output=True,
        text=True,
        timeout=20,
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


def build_blocker_findings(records: list[dict], default_repo: str) -> list[dict]:
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=BLOCKER_WINDOW_DAYS)
    for rec in records:
        ts_raw = rec.get("timestamp", "")
        if ts_raw:
            try:
                ts = datetime.fromisoformat(ts_raw)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts < cutoff:
                    continue
            except (TypeError, ValueError):
                continue
        status = str(rec.get("status", "")).strip().lower()
        blocker_code = str(rec.get("blocker_code", "")).strip().lower()
        if status not in {"partial", "blocked"} or not blocker_code or blocker_code == "none":
            continue
        repo = str(rec.get("repo") or rec.get("github_repo") or default_repo).strip() or default_repo
        grouped[(repo, blocker_code)].append(rec)

    findings: list[dict] = []
    for (repo, blocker_code), matches in sorted(grouped.items()):
        if len(matches) < BLOCKER_THRESHOLD:
            continue
        repo_name = repo.rsplit("/", 1)[-1]
        findings.append({
            "id": f"blocker_spike:{repo}:{blocker_code}",
            "source": "log_analyzer",
            "kind": "blocker_spike",
            "repo": repo,
            "title_hint": f"Reduce repeated {blocker_code.replace('_', ' ')} blockers in {repo_name}",
            "summary": (
                f"{len(matches)} recent task outcomes in {repo_name} ended as "
                f"{blocker_code} within the last {BLOCKER_WINDOW_DAYS} day(s)."
            ),
            "blocker_code": blocker_code,
            "count": len(matches),
            "window_days": BLOCKER_WINDOW_DAYS,
            "evidence": [
                f"runtime/metrics/agent_stats.jsonl last {BLOCKER_WINDOW_DAYS} day(s)",
                f"blocked_or_partial_count={len(matches)} for blocker_code={blocker_code}",
            ],
        })
    return findings


def load_scorer_findings(root: Path, records: list[dict]) -> list[dict]:
    artifact = scorer_findings_path(root)
    if artifact.exists():
        try:
            payload = json.loads(artifact.read_text(encoding="utf-8"))
            findings = payload.get("findings", [])
            if isinstance(findings, list):
                return findings
        except Exception:
            pass
    return build_degradation_findings(records)


def collect_structured_findings(root: Path, records: list[dict], default_repo: str) -> list[dict]:
    findings: list[dict] = []
    findings.extend(load_scorer_findings(root, records))
    findings.extend(build_blocker_findings(records, default_repo))
    deduped: dict[str, dict] = {}
    for finding in findings:
        deduped[finding.get("id", json.dumps(finding, sort_keys=True))] = finding
    return list(deduped.values())


def _finding_map(findings: list[dict]) -> dict[str, dict]:
    mapping = {
        METRICS_EVIDENCE_ID: {
            "summary": f"Last {WINDOW_DAYS} days of runtime/metrics/agent_stats.jsonl",
            "source": "runtime/metrics/agent_stats.jsonl",
        },
        QUEUE_LOG_EVIDENCE_ID: {
            "summary": f"Tail of runtime/logs/queue-summary.log over the last {WINDOW_DAYS} days",
            "source": "runtime/logs/queue-summary.log",
        },
    }
    for finding in findings:
        finding_id = finding.get("id")
        if finding_id:
            mapping[finding_id] = finding
    return mapping


def _normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", (title or "").strip().lower())


def dedupe_synthesized_issues(candidates: list[dict]) -> list[dict]:
    deduped: list[dict] = []
    seen_titles: set[str] = set()
    seen_evidence: list[set[str]] = []
    for issue in candidates:
        title_key = _normalize_title(issue.get("title", ""))
        evidence = {str(e).strip() for e in issue.get("evidence_ids", []) if str(e).strip()}
        if not title_key:
            continue
        if title_key in seen_titles:
            continue
        if evidence and any(evidence <= existing or existing <= evidence for existing in seen_evidence):
            continue
        seen_titles.add(title_key)
        if evidence:
            seen_evidence.append(evidence)
        deduped.append(issue)
    return deduped


def build_issue_body(issue: dict, evidence_lookup: dict[str, dict]) -> str:
    goal = str(issue.get("goal", "")).strip()
    success_criteria = [str(item).strip() for item in issue.get("success_criteria", []) if str(item).strip()]
    constraints = [str(item).strip() for item in issue.get("constraints", []) if str(item).strip()]
    reasoning = str(issue.get("reasoning", "")).strip()
    evidence_ids = [str(item).strip() for item in issue.get("evidence_ids", []) if str(item).strip()]

    lines = ["## Goal", goal or "Investigate and remediate the highest-impact operational issue."]
    lines.extend(["", "## Success Criteria"])
    for item in success_criteria or ["Deliver one bounded remediation that addresses the cited evidence."]:
        lines.append(f"- {item}")
    lines.extend(["", "## Constraints"])
    for item in constraints or ["- Prefer minimal diffs"]:
        prefix = "" if item.startswith("- ") else "- "
        lines.append(f"{prefix}{item}")
    lines.extend(["", "## Evidence"])
    for evidence_id in evidence_ids:
        evidence = evidence_lookup.get(evidence_id, {})
        summary = evidence.get("summary") or evidence.get("title_hint") or evidence_id
        source = evidence.get("source", "operational_evidence")
        lines.append(f"- `{evidence_id}` ({source}): {summary}")
    if not evidence_ids:
        lines.append("- No explicit evidence ids provided by analyzer.")
    lines.extend(["", "## Reasoning", reasoning or "Prioritized as the clearest single remediation backed by the cited evidence."])
    return "\n".join(lines)


def synthesize_issues(
    *,
    default_repo: str,
    records: list[dict],
    log_text: str,
    findings: list[dict],
    top_n: int = TOP_N,
) -> list[dict]:
    prompt = ANALYSIS_PROMPT.format(
        top_n=top_n,
        default_repo=default_repo,
        queue_log_evidence_id=QUEUE_LOG_EVIDENCE_ID,
        metrics_evidence_id=METRICS_EVIDENCE_ID,
        structured_findings=json.dumps(findings, indent=2, sort_keys=True),
        metrics_summary=_metrics_summary(records),
        log_tail=log_text,
    )
    raw = _call_haiku(prompt)
    issues = _parse_issues(raw)
    if not isinstance(issues, list):
        raise RuntimeError("Analyzer response was not a JSON list.")
    return dedupe_synthesized_issues(issues)[:top_n]


def run():
    cfg = load_config()
    root = Path(cfg.get("root_dir", ".")).expanduser()
    metrics_file = root / "runtime" / "metrics" / "agent_stats.jsonl"
    paths = runtime_paths(cfg)
    log_file = Path(paths["QUEUE_SUMMARY_LOG"])
    default_repo = _resolve_default_repo(cfg)

    records = load_recent_metrics(metrics_file)
    log_text = _read_log_tail(log_file)
    findings = collect_structured_findings(root, records, default_repo)

    if not records and log_text in ("(no queue log found)", "(empty log)") and not findings:
        print("No data found; skipping analysis.")
        return

    print(
        f"Analyzing {len(records)} metric record(s), {len(findings)} structured finding(s), "
        f"and queue log ({log_file})..."
    )

    try:
        issues = synthesize_issues(
            default_repo=default_repo,
            records=records,
            log_text=log_text,
            findings=findings,
        )
    except Exception as e:
        print(f"Analysis failed: {e}")
        return

    evidence_lookup = _finding_map(findings)
    created_urls: list[str] = []
    skipped: list[str] = []

    for issue in issues:
        title = str(issue.get("title", "")).strip()
        repo = str(issue.get("repo") or default_repo).strip() or default_repo
        labels = [str(label).strip() for label in issue.get("labels", []) if str(label).strip()]

        if not title:
            print("Warning: issue with empty title, skipping")
            continue
        if _open_issue_exists(repo, title):
            print(f"Skipping duplicate: {title!r}")
            skipped.append(title)
            continue

        body = build_issue_body(issue, evidence_lookup)
        try:
            url = _create_issue(repo, title, body, labels)
            print(f"Created: {url}")
            created_urls.append(url)
        except Exception as e:
            print(f"Failed to create issue {title!r}: {e}")
            skipped.append(title)

    summary_lines = [
        "Weekly Log Analyzer complete",
        f"Issues created: {len(created_urls)} | Skipped: {len(skipped)}",
    ]
    for url in created_urls:
        summary_lines.append(f"  {url}")
    summary = "\n".join(summary_lines)
    print(summary)
    _send_telegram(cfg, summary)


if __name__ == "__main__":
    run()
