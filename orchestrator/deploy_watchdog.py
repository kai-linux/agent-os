"""Post-merge deploy watchdog that opens revert PRs on production regressions."""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from orchestrator.audit_log import append_audit_event
from orchestrator.external_ingester import load_external_signals
from orchestrator.gh_project import create_pr_for_branch, ensure_labels, gh, gh_json
from orchestrator.incident_router import classify_severity, escalate as route_incident
from orchestrator.outcome_attribution import load_outcome_records
from orchestrator.paths import load_config
from orchestrator.queue import save_telegram_action
from orchestrator.repo_modes import is_dispatcher_only_repo
from orchestrator.scheduler_state import job_lock

WATCHDOG_JOB_NAME = "deploy_watchdog"
DECISIONS_LOG_FILENAME = "deploy_decisions.jsonl"
REVERT_PR_LABELS = ["bug", "task:debugging", "prio:high"]
DEFAULT_WINDOW_MINUTES = 60
DEFAULT_ERROR_RATE_SPIKE_RATIO = 2.0
DEFAULT_LATENCY_P95_SPIKE_RATIO = 1.5
SIGNAL_ACTION_TTL_HOURS = 48


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timestamp(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _metrics_dir(cfg: dict) -> Path:
    return Path(cfg.get("root_dir", ".")).expanduser() / "runtime" / "metrics"


def deploy_decisions_path(cfg: dict) -> Path:
    return _metrics_dir(cfg) / DECISIONS_LOG_FILENAME


def _telegram_actions_dir(cfg: dict) -> Path:
    path = Path(cfg.get("root_dir", ".")).expanduser() / "runtime" / "telegram_actions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def append_deploy_decision(cfg: dict, record: dict[str, Any]) -> Path:
    path = deploy_decisions_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(record)
    payload.setdefault("timestamp", _now_utc().isoformat())
    line = json.dumps(payload, sort_keys=True) + "\n"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(existing + line)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return path


def load_deploy_decisions(
    cfg: dict,
    *,
    repo: str | None = None,
    source_pr_number: int | None = None,
) -> list[dict[str, Any]]:
    path = deploy_decisions_path(cfg)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if repo and row.get("repo") != repo:
                continue
            if source_pr_number is not None and row.get("source_pr_number") != source_pr_number:
                continue
            rows.append(row)
    return rows


def resolve_deploy_watchdog_config(cfg: dict, github_slug: str) -> dict[str, Any]:
    merged = dict(cfg.get("deploy_watchdog") or {})
    for project_cfg in (cfg.get("github_projects") or {}).values():
        if not isinstance(project_cfg, dict):
            continue
        for repo_cfg in project_cfg.get("repos", []) or []:
            if repo_cfg.get("github_repo") != github_slug:
                continue
            override = repo_cfg.get("deploy_watchdog")
            if isinstance(override, dict):
                updated = dict(merged)
                updated.update(override)
                merged = updated
            return merged
    return merged


def _resolve_repos(cfg: dict) -> list[tuple[str, Path]]:
    repos: list[tuple[str, Path]] = []
    seen: set[tuple[str, str]] = set()
    for project_cfg in (cfg.get("github_projects") or {}).values():
        if not isinstance(project_cfg, dict):
            continue
        for repo_cfg in project_cfg.get("repos", []) or []:
            github_slug = str(repo_cfg.get("github_repo") or "").strip()
            local_repo = str(repo_cfg.get("local_repo") or repo_cfg.get("path") or "").strip()
            if not github_slug or not local_repo:
                continue
            key = (github_slug, local_repo)
            if key in seen:
                continue
            seen.add(key)
            repos.append((github_slug, Path(local_repo).expanduser()))
    return repos


def _recent_merged_records(
    cfg: dict,
    github_slug: str,
    *,
    now: datetime,
    window_minutes: int,
) -> list[dict[str, Any]]:
    cutoff = now - timedelta(minutes=max(window_minutes, 1))
    records: list[dict[str, Any]] = []
    for record in load_outcome_records(cfg, repo=github_slug):
        if record.get("record_type") != "attribution" or record.get("event") != "merged":
            continue
        merged_at = _parse_timestamp(record.get("merged_at") or record.get("timestamp"))
        if merged_at is None or merged_at < cutoff or merged_at > now:
            continue
        copied = dict(record)
        copied["_merged_at"] = merged_at
        records.append(copied)
    records.sort(key=lambda item: item["_merged_at"])
    return records


def _lookup_path(data: object, candidates: list[tuple[str, ...]]) -> float | None:
    for path in candidates:
        current = data
        for key in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)
        if current is None:
            continue
        try:
            return float(current)
        except (TypeError, ValueError):
            continue
    return None


def _extract_signal_metrics(signal: dict[str, Any]) -> dict[str, float | None]:
    metric_paths = {
        "error_rate": [
            ("error_rate",),
            ("metrics", "error_rate"),
            ("telemetry", "error_rate"),
            ("measurements", "error_rate"),
        ],
        "baseline_error_rate": [
            ("baseline_error_rate",),
            ("metrics", "baseline_error_rate"),
            ("telemetry", "baseline_error_rate"),
            ("measurements", "baseline_error_rate"),
        ],
        "latency_p95_ms": [
            ("latency_p95_ms",),
            ("latency_p95",),
            ("p95_latency_ms",),
            ("metrics", "latency_p95_ms"),
            ("metrics", "latency_p95"),
            ("telemetry", "latency_p95_ms"),
            ("measurements", "latency_p95_ms"),
        ],
        "baseline_latency_p95_ms": [
            ("baseline_latency_p95_ms",),
            ("baseline_latency_p95",),
            ("metrics", "baseline_latency_p95_ms"),
            ("metrics", "baseline_latency_p95"),
            ("telemetry", "baseline_latency_p95_ms"),
            ("measurements", "baseline_latency_p95_ms"),
        ],
    }
    return {name: _lookup_path(signal, paths) for name, paths in metric_paths.items()}


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _max_value(values: list[float]) -> float | None:
    if not values:
        return None
    return max(values)


def _signal_window(
    cfg: dict,
    github_slug: str,
    merged_at: datetime,
    window_minutes: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    raw_signals = load_external_signals(cfg, repo=github_slug, window_days=max(1, int((window_minutes * 2) / 1440) + 1))
    start = merged_at
    end = merged_at + timedelta(minutes=window_minutes)
    baseline_start = merged_at - timedelta(minutes=window_minutes)
    post_merge: list[dict[str, Any]] = []
    baseline: list[dict[str, Any]] = []
    for signal in raw_signals:
        ts = _parse_timestamp(signal.get("ts") or signal.get("timestamp"))
        if ts is None:
            continue
        enriched = dict(signal)
        enriched["_ts"] = ts
        enriched["_metrics"] = _extract_signal_metrics(signal)
        if start <= ts <= end:
            post_merge.append(enriched)
        elif baseline_start <= ts < start:
            baseline.append(enriched)
    post_merge.sort(key=lambda item: item["_ts"])
    baseline.sort(key=lambda item: item["_ts"])
    return baseline, post_merge


def _build_verdict(
    merge_record: dict[str, Any],
    baseline_signals: list[dict[str, Any]],
    post_merge_signals: list[dict[str, Any]],
    *,
    error_rate_threshold: float,
    latency_threshold: float,
) -> tuple[str, dict[str, Any]]:
    baseline_error_values = [
        metric
        for signal in baseline_signals
        if (metric := signal["_metrics"].get("error_rate")) is not None
    ]
    baseline_latency_values = [
        metric
        for signal in baseline_signals
        if (metric := signal["_metrics"].get("latency_p95_ms")) is not None
    ]
    signal_baseline_error_values = [
        metric
        for signal in post_merge_signals
        if (metric := signal["_metrics"].get("baseline_error_rate")) is not None
    ]
    signal_baseline_latency_values = [
        metric
        for signal in post_merge_signals
        if (metric := signal["_metrics"].get("baseline_latency_p95_ms")) is not None
    ]
    current_error_values = [
        metric
        for signal in post_merge_signals
        if (metric := signal["_metrics"].get("error_rate")) is not None
    ]
    current_latency_values = [
        metric
        for signal in post_merge_signals
        if (metric := signal["_metrics"].get("latency_p95_ms")) is not None
    ]

    baseline_error = _mean(signal_baseline_error_values) or _mean(baseline_error_values)
    baseline_latency = _mean(signal_baseline_latency_values) or _mean(baseline_latency_values)
    current_error = _max_value(current_error_values)
    current_latency = _max_value(current_latency_values)

    error_ratio = None
    if baseline_error and current_error is not None and baseline_error > 0:
        error_ratio = current_error / baseline_error
    latency_ratio = None
    if baseline_latency and current_latency is not None and baseline_latency > 0:
        latency_ratio = current_latency / baseline_latency

    triggered = []
    if error_ratio is not None and error_ratio >= error_rate_threshold:
        triggered.append("error_rate")
    if latency_ratio is not None and latency_ratio >= latency_threshold:
        triggered.append("latency_p95")

    evidence = {
        "merge_timestamp": merge_record["_merged_at"].isoformat(),
        "baseline_signal_count": len(baseline_signals),
        "post_merge_signal_count": len(post_merge_signals),
        "error_rate": {
            "baseline": baseline_error,
            "current": current_error,
            "ratio": error_ratio,
            "threshold_ratio": error_rate_threshold,
        },
        "latency_p95_ms": {
            "baseline": baseline_latency,
            "current": current_latency,
            "ratio": latency_ratio,
            "threshold_ratio": latency_threshold,
        },
        "triggered_signals": triggered,
        "signals": [
            {
                "title": signal.get("title"),
                "source": signal.get("source"),
                "ts": signal["_ts"].isoformat(),
                "severity": signal.get("severity"),
                "url": signal.get("url"),
                "metrics": signal["_metrics"],
            }
            for signal in post_merge_signals[:10]
        ],
    }
    if triggered:
        return "regressed", evidence
    if post_merge_signals:
        return "clean", evidence
    return "no_signals", evidence


def _safe_branch_part(value: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", value.lower()).strip("-")[:48] or "revert"


def _git(cmd: list[str], cwd: Path) -> None:
    subprocess.run(["git", *cmd], cwd=str(cwd), capture_output=True, text=True, check=True)


def _repo_clean(repo_path: Path) -> bool:
    result = subprocess.run(["git", "status", "--porcelain"], cwd=str(repo_path), capture_output=True, text=True)
    return result.returncode == 0 and not result.stdout.strip()


def _extract_pr_number(pr_url: str | None) -> int | None:
    match = re.search(r"/pull/(\d+)$", str(pr_url or ""))
    return int(match.group(1)) if match else None


def _open_revert_pr_exists(repo: str, source_pr_number: int) -> str | None:
    title = f"revert: rollback PR #{source_pr_number} after production regression"
    prs = gh_json(
        [
            "pr",
            "list",
            "-R",
            repo,
            "--state",
            "open",
            "--search",
            title,
            "--json",
            "title,url",
            "--limit",
            "20",
        ]
    ) or []
    for pr in prs:
        if str(pr.get("title") or "").strip() == title:
            return str(pr.get("url") or "").strip() or None
    return None


def _fetch_pr_merge_details(repo: str, pr_number: int) -> dict[str, Any] | None:
    return gh_json(
        [
            "pr",
            "view",
            str(pr_number),
            "-R",
            repo,
            "--json",
            "number,title,url,baseRefName,mergeCommit",
        ]
    )


def _create_revert_pr(
    repo: str,
    repo_path: Path,
    source_pr_number: int,
    evidence: dict[str, Any],
) -> str | None:
    existing = _open_revert_pr_exists(repo, source_pr_number)
    if existing:
        return existing
    if not repo_path.exists() or not _repo_clean(repo_path):
        raise RuntimeError(f"Local repo not clean or unavailable: {repo_path}")

    details = _fetch_pr_merge_details(repo, source_pr_number) or {}
    merge_commit = ((details.get("mergeCommit") or {}).get("oid") or "").strip()
    base_branch = str(details.get("baseRefName") or "main").strip() or "main"
    if not merge_commit:
        raise RuntimeError(f"PR #{source_pr_number} has no merge commit available for revert.")

    branch = f"deploy-watchdog/revert-pr-{source_pr_number}-{_safe_branch_part(merge_commit[:8])}"
    original_branch = (
        subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        or base_branch
    )
    try:
        _git(["fetch", "origin", base_branch], repo_path)
        _git(["checkout", base_branch], repo_path)
        _git(["reset", "--hard", f"origin/{base_branch}"], repo_path)
        _git(["checkout", "-B", branch], repo_path)
        revert = subprocess.run(
            ["git", "revert", "-m", "1", merge_commit, "--no-edit"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
        )
        if revert.returncode != 0:
            subprocess.run(["git", "revert", "--abort"], cwd=str(repo_path), capture_output=True, text=True)
            raise RuntimeError(revert.stderr.strip() or revert.stdout.strip() or "git revert failed")
        _git(["push", "origin", branch, "--force-with-lease"], repo_path)
        pr_title = f"revert: rollback PR #{source_pr_number} after production regression"
        pr_body = (
            "Deploy watchdog detected a post-merge production regression and prepared a revert for operator review.\n\n"
            f"- Source PR: #{source_pr_number}\n"
            f"- Merge commit: `{merge_commit}`\n"
            f"- Error-rate ratio: {_fmt_ratio((evidence.get('error_rate') or {}).get('ratio'))}\n"
            f"- Latency p95 ratio: {_fmt_ratio((evidence.get('latency_p95_ms') or {}).get('ratio'))}\n"
            "- This PR must not be auto-merged; Telegram operator approval is required.\n"
        )
        pr_url = create_pr_for_branch(repo, branch, pr_title, pr_body)
        pr_number = _extract_pr_number(pr_url)
        if pr_number is not None:
            ensure_labels(repo, REVERT_PR_LABELS)
            gh(["pr", "edit", str(pr_number), "-R", repo, "--add-label", ",".join(REVERT_PR_LABELS)], check=False)
        return pr_url
    finally:
        subprocess.run(["git", "checkout", original_branch], cwd=str(repo_path), capture_output=True, text=True)


def _fmt_ratio(value: object) -> str:
    try:
        if value is None:
            return "n/a"
        return f"{float(value):.2f}x"
    except (TypeError, ValueError):
        return "n/a"


def _create_revert_action(
    cfg: dict,
    repo: str,
    source_pr_number: int,
    revert_pr_url: str,
    verdict: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    now = _now_utc()
    revert_pr_number = _extract_pr_number(revert_pr_url)
    action_id = __import__("uuid").uuid4().hex[:12]
    return {
        "action_id": action_id,
        "type": "deploy_watchdog_revert",
        "status": "pending",
        "approval": "pending",
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=SIGNAL_ACTION_TTL_HOURS)).isoformat(),
        "chat_id": str(cfg.get("telegram_chat_id", "")).strip(),
        "message_id": None,
        "repo": repo,
        "source_pr_number": source_pr_number,
        "revert_pr_number": revert_pr_number,
        "revert_pr_url": revert_pr_url,
        "verdict": verdict,
        "evidence": evidence,
    }


def revert_reply_markup(action_id: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [[
            {"text": "/approve-revert", "callback_data": f"rvt:{action_id}:approve"},
            {"text": "/cancel-revert", "callback_data": f"rvt:{action_id}:cancel"},
        ]]
    }


def _send_revert_telegram(cfg: dict, action: dict[str, Any]) -> dict[str, Any]:
    evidence = action.get("evidence") or {}
    event = {
        "source": "deploy_watchdog",
        "type": "deploy_regression",
        "repo": action.get("repo"),
        "pr_number": action.get("source_pr_number"),
        "revert_pr_url": action.get("revert_pr_url"),
        "verdict": action.get("verdict"),
        "summary": (
            f"Deploy regression after PR #{action.get('source_pr_number')}: revert PR {action.get('revert_pr_url')} ready. "
            f"error_rate={_fmt_ratio(((evidence.get('error_rate') or {}).get('ratio')))}, "
            f"latency_p95={_fmt_ratio(((evidence.get('latency_p95_ms') or {}).get('ratio')))}."
        ),
        "dedup_key": f"deploy-regression:{action.get('repo')}:{action.get('source_pr_number')}",
        "reply_markup": revert_reply_markup(action["action_id"]),
    }
    severity = classify_severity(cfg, "deploy_watchdog", event)
    return route_incident(severity, event, cfg=cfg)


def _already_processed(cfg: dict, repo: str, source_pr_number: int) -> bool:
    rows = load_deploy_decisions(cfg, repo=repo, source_pr_number=source_pr_number)
    for row in reversed(rows):
        if row.get("operator_response") in {"pending", "approved", "canceled"}:
            return True
        if row.get("action") in {"revert_pr_created", "revert_pr_exists"}:
            return True
    return False


def evaluate_merge(
    cfg: dict,
    repo: str,
    repo_path: Path,
    merge_record: dict[str, Any],
    *,
    window_minutes: int,
    error_rate_threshold: float,
    latency_threshold: float,
) -> dict[str, Any]:
    baseline_signals, post_merge_signals = _signal_window(cfg, repo, merge_record["_merged_at"], window_minutes)
    verdict, evidence = _build_verdict(
        merge_record,
        baseline_signals,
        post_merge_signals,
        error_rate_threshold=error_rate_threshold,
        latency_threshold=latency_threshold,
    )
    result = {
        "repo": repo,
        "source_pr_number": merge_record.get("pr_number"),
        "task_id": merge_record.get("task_id"),
        "verdict": verdict,
        "evidence": evidence,
        "revert_pr_url": None,
        "operator_response": "none",
        "action": "observed",
    }
    if verdict != "regressed":
        append_deploy_decision(cfg, result)
        return result

    source_pr_number = int(merge_record.get("pr_number") or 0)
    if source_pr_number <= 0:
        result["action"] = "missing_pr_number"
        result["operator_response"] = "none"
        append_deploy_decision(cfg, result)
        return result
    if _already_processed(cfg, repo, source_pr_number):
        result["action"] = "already_pending"
        result["operator_response"] = "pending"
        append_deploy_decision(cfg, result)
        return result

    revert_pr_url = _create_revert_pr(repo, repo_path, source_pr_number, evidence)
    result["revert_pr_url"] = revert_pr_url
    result["operator_response"] = "pending"
    result["action"] = "revert_pr_created" if revert_pr_url else "revert_pr_failed"
    action_id = None
    if revert_pr_url:
        action = _create_revert_action(cfg, repo, source_pr_number, revert_pr_url, verdict, evidence)
        actions_dir = _telegram_actions_dir(cfg)
        save_telegram_action(actions_dir, action)
        incident = _send_revert_telegram(cfg, action)
        if incident.get("message_id"):
            action["message_id"] = incident["message_id"]
            save_telegram_action(actions_dir, action)
        action_id = action["action_id"]
    if action_id:
        result["telegram_action_id"] = action_id
        append_audit_event(
            cfg,
            "autonomous_pr_opened",
            {
                "source": "deploy_watchdog",
                "repo": repo,
                "pr_url": revert_pr_url,
                "source_pr_number": source_pr_number,
                "kind": "revert",
                "telegram_action_id": action_id,
            },
        )
    append_deploy_decision(cfg, result)
    return result


def watch_repo(cfg: dict, github_slug: str, repo_path: Path, *, now: datetime | None = None) -> list[dict[str, Any]]:
    current = now or _now_utc()
    watcher_cfg = resolve_deploy_watchdog_config(cfg, github_slug)
    if not watcher_cfg.get("enabled"):
        return [{"repo": github_slug, "verdict": "skipped", "action": "disabled"}]
    if is_dispatcher_only_repo(cfg, github_slug):
        return [{"repo": github_slug, "verdict": "skipped", "action": "dispatcher_only"}]
    if not repo_path.exists():
        return [{"repo": github_slug, "verdict": "skipped", "action": "missing_repo"}]

    window_minutes = int(watcher_cfg.get("window_minutes") or DEFAULT_WINDOW_MINUTES)
    error_rate_threshold = float(
        watcher_cfg.get("error_rate_spike_ratio") or DEFAULT_ERROR_RATE_SPIKE_RATIO
    )
    latency_threshold = float(
        watcher_cfg.get("latency_p95_spike_ratio") or DEFAULT_LATENCY_P95_SPIKE_RATIO
    )
    merges = _recent_merged_records(cfg, github_slug, now=current, window_minutes=window_minutes)
    if not merges:
        return [{"repo": github_slug, "verdict": "skipped", "action": "no_recent_merges"}]
    return [
        evaluate_merge(
            cfg,
            github_slug,
            repo_path,
            merge_record,
            window_minutes=window_minutes,
            error_rate_threshold=error_rate_threshold,
            latency_threshold=latency_threshold,
        )
        for merge_record in merges
    ]


def run_deploy_watchdog(cfg: dict | None = None, *, now: datetime | None = None) -> list[dict[str, Any]]:
    cfg = cfg or load_config()
    summaries: list[dict[str, Any]] = []
    with job_lock(cfg, WATCHDOG_JOB_NAME) as acquired:
        if not acquired:
            return [{"repo": "*", "verdict": "skipped", "action": "locked"}]
        for github_slug, repo_path in _resolve_repos(cfg):
            summaries.extend(watch_repo(cfg, github_slug, repo_path, now=now))
    return summaries


def handle_revert_callback(
    cfg: dict,
    action: dict[str, Any],
    operation: str,
    logfile: Path | None = None,
    queue_summary_log: Path | None = None,
) -> str:
    repo = str(action.get("repo") or "").strip()
    revert_pr_number = int(action.get("revert_pr_number") or 0)
    if not repo or revert_pr_number <= 0:
        raise RuntimeError("Deploy watchdog action is missing revert PR metadata.")
    if operation == "approve":
        gh(["pr", "merge", str(revert_pr_number), "-R", repo, "--squash", "--delete-branch"])
        response = "approved"
        human_text = f"Approved revert PR #{revert_pr_number} for {repo}."
        append_audit_event(
            cfg,
            "autonomous_pr_merged",
            {
                "source": "deploy_watchdog",
                "repo": repo,
                "pr_number": revert_pr_number,
                "source_pr_number": action.get("source_pr_number"),
                "merge_method": "squash",
                "kind": "revert",
            },
        )
    elif operation == "cancel":
        gh(
            [
                "pr",
                "close",
                str(revert_pr_number),
                "-R",
                repo,
                "--delete-branch",
                "--comment",
                "Closed by deploy-watchdog operator decision from Telegram.",
            ]
        )
        response = "canceled"
        human_text = f"Canceled revert PR #{revert_pr_number} for {repo}."
    else:
        raise RuntimeError(f"Unsupported revert operation: {operation}")

    append_deploy_decision(
        cfg,
        {
            "repo": repo,
            "source_pr_number": action.get("source_pr_number"),
            "revert_pr_number": revert_pr_number,
            "verdict": action.get("verdict"),
            "evidence": action.get("evidence"),
            "action": "operator_response",
            "operator_response": response,
            "telegram_action_id": action.get("action_id"),
        },
    )
    return human_text


def main() -> int:
    summaries = run_deploy_watchdog()
    for summary in summaries:
        repo = summary.get("repo")
        print(
            f"{repo}: verdict={summary.get('verdict')}, "
            f"action={summary.get('action')}, "
            f"revert_pr={summary.get('revert_pr_url') or 'none'}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
