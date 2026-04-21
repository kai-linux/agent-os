from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator import deploy_watchdog as dw


def _cfg(tmp_path: Path, repo: Path) -> dict:
    return {
        "root_dir": str(tmp_path),
        "telegram_chat_id": "123",
        "deploy_watchdog": {
            "enabled": False,
            "window_minutes": 60,
            "error_rate_spike_ratio": 2.0,
            "latency_p95_spike_ratio": 1.5,
        },
        "github_projects": {
            "proj": {
                "repos": [
                    {
                        "github_repo": "owner/repo",
                        "local_repo": str(repo),
                        "deploy_watchdog": {
                            "enabled": True,
                            "window_minutes": 60,
                            "error_rate_spike_ratio": 2.0,
                            "latency_p95_spike_ratio": 1.5,
                        },
                    }
                ]
            }
        },
    }


def _write_merge_record(metrics_dir: Path, merged_at: datetime) -> None:
    metrics_dir.mkdir(parents=True, exist_ok=True)
    (metrics_dir / "outcome_attribution.jsonl").write_text(
        json.dumps(
            {
                "record_type": "attribution",
                "event": "merged",
                "repo": "owner/repo",
                "task_id": "task-123",
                "pr_number": 77,
                "merged_at": merged_at.isoformat(),
                "timestamp": merged_at.isoformat(),
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _write_signals(metrics_dir: Path, records: list[dict]) -> None:
    (metrics_dir / "external_signals.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def test_watch_repo_creates_revert_pr_on_regression(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _cfg(tmp_path, repo)
    metrics_dir = tmp_path / "runtime" / "metrics"
    now = datetime(2026, 4, 21, 10, 0, tzinfo=timezone.utc)
    merged_at = now - timedelta(minutes=20)
    _write_merge_record(metrics_dir, merged_at)
    _write_signals(
        metrics_dir,
        [
            {
                "repo": "owner/repo",
                "ts": (merged_at - timedelta(minutes=10)).isoformat(),
                "title": "baseline",
                "error_rate": 0.10,
                "latency_p95_ms": 100,
            },
            {
                "repo": "owner/repo",
                "ts": (merged_at + timedelta(minutes=5)).isoformat(),
                "title": "spike",
                "error_rate": 0.25,
                "latency_p95_ms": 180,
            },
        ],
    )
    monkeypatch.setattr(dw, "_create_revert_pr", lambda *args, **kwargs: "https://github.com/owner/repo/pull/88")
    monkeypatch.setattr(dw, "send_telegram", lambda *args, **kwargs: 42)

    summaries = dw.watch_repo(cfg, "owner/repo", repo, now=now)

    assert len(summaries) == 1
    summary = summaries[0]
    assert summary["verdict"] == "regressed"
    assert summary["action"] == "revert_pr_created"
    assert summary["revert_pr_url"] == "https://github.com/owner/repo/pull/88"
    assert summary["operator_response"] == "pending"
    action_file = tmp_path / "runtime" / "telegram_actions" / f"{summary['telegram_action_id']}.json"
    stored = json.loads(action_file.read_text(encoding="utf-8"))
    assert stored["approval"] == "pending"
    logged = [json.loads(line) for line in (metrics_dir / "deploy_decisions.jsonl").read_text(encoding="utf-8").splitlines()]
    assert logged[-1]["action"] == "revert_pr_created"


def test_watch_repo_skips_revert_when_signals_are_clean(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _cfg(tmp_path, repo)
    metrics_dir = tmp_path / "runtime" / "metrics"
    now = datetime(2026, 4, 21, 10, 0, tzinfo=timezone.utc)
    merged_at = now - timedelta(minutes=20)
    _write_merge_record(metrics_dir, merged_at)
    _write_signals(
        metrics_dir,
        [
            {
                "repo": "owner/repo",
                "ts": (merged_at - timedelta(minutes=10)).isoformat(),
                "title": "baseline",
                "error_rate": 0.10,
                "latency_p95_ms": 100,
            },
            {
                "repo": "owner/repo",
                "ts": (merged_at + timedelta(minutes=5)).isoformat(),
                "title": "steady",
                "error_rate": 0.15,
                "latency_p95_ms": 120,
            },
        ],
    )
    created = []
    monkeypatch.setattr(dw, "_create_revert_pr", lambda *args, **kwargs: created.append(True) or None)

    summaries = dw.watch_repo(cfg, "owner/repo", repo, now=now)

    assert summaries[0]["verdict"] == "clean"
    assert summaries[0]["action"] == "observed"
    assert summaries[0]["revert_pr_url"] is None
    assert created == []


def test_handle_revert_callback_records_approval(tmp_path, monkeypatch):
    cfg = {"root_dir": str(tmp_path)}
    calls = []
    monkeypatch.setattr(dw, "gh", lambda cmd, check=True: calls.append(cmd) or "")
    action = {
        "action_id": "abcdef123456",
        "repo": "owner/repo",
        "source_pr_number": 77,
        "revert_pr_number": 88,
        "verdict": "regressed",
        "evidence": {"triggered_signals": ["error_rate"]},
    }

    text = dw.handle_revert_callback(cfg, action, "approve")

    assert "Approved revert PR #88" in text
    assert calls and calls[0][:3] == ["pr", "merge", "88"]
    logged = [json.loads(line) for line in (tmp_path / "runtime" / "metrics" / "deploy_decisions.jsonl").read_text(encoding="utf-8").splitlines()]
    assert logged[-1]["operator_response"] == "approved"
