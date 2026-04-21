from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.cost_tracker import build_cost_records, rebuild_cost_records


def test_build_cost_records_aggregates_task_and_repo_costs():
    agent_stats = [
        {
            "timestamp": "2026-04-21T09:00:00+00:00",
            "task_id": "task-1",
            "github_repo": "acme/api",
            "status": "complete",
            "task_type": "implementation",
            "model_attempt_details": [
                {
                    "attempt": 1,
                    "agent": "codex",
                    "provider": "openai",
                    "model": "codex",
                    "input_tokens_estimate": 1000,
                    "output_tokens_estimate": 500,
                    "status": "complete",
                    "blocker_code": "none",
                }
            ],
        },
        {
            "timestamp": "2026-04-21T10:00:00+00:00",
            "task_id": "task-2",
            "github_repo": "acme/api",
            "status": "blocked",
            "task_type": "debugging",
            "model_attempt_details": [
                {
                    "attempt": 1,
                    "agent": "gemini",
                    "provider": "google",
                    "model": "gemini-2.5-flash",
                    "input_tokens_estimate": 2000,
                    "output_tokens_estimate": 1000,
                    "status": "blocked",
                    "blocker_code": "missing_context",
                }
            ],
        },
    ]

    records = build_cost_records(agent_stats, {})
    task_records = [record for record in records if record["record_type"] == "task_cost"]
    repo_summary = next(record for record in records if record["record_type"] == "repo_summary")
    global_summary = next(record for record in records if record["record_type"] == "global_summary")

    assert len(task_records) == 2
    assert task_records[0]["task_cost_usd"] == 0.045
    assert task_records[1]["task_cost_usd"] == 0.0031
    assert task_records[1]["repo_cumulative_cost_usd"] == 0.0481
    assert repo_summary["repo"] == "acme/api"
    assert repo_summary["total_cost_usd"] == 0.0481
    assert global_summary["total_cost_usd"] == 0.0481


def test_rebuild_cost_records_applies_config_price_adjustments(tmp_path):
    metrics_dir = tmp_path / "runtime" / "metrics"
    metrics_dir.mkdir(parents=True)
    stats_path = metrics_dir / "agent_stats.jsonl"
    stats_path.write_text(
        json.dumps(
            {
                "timestamp": "2026-04-21T09:00:00+00:00",
                "task_id": "task-1",
                "github_repo": "acme/api",
                "status": "complete",
                "task_type": "implementation",
                "model_attempt_details": [
                    {
                        "attempt": 1,
                        "agent": "codex",
                        "provider": "openai",
                        "model": "codex",
                        "input_tokens_estimate": 1000,
                        "output_tokens_estimate": 500,
                        "status": "complete",
                        "blocker_code": "none",
                    }
                ],
            }
        ) + "\n",
        encoding="utf-8",
    )

    cfg = {
        "root_dir": str(tmp_path),
        "cost_tracking": {
            "default_price_multiplier": 2.0,
            "provider_multipliers": {"openai": 1.5},
        },
    }
    output_path = rebuild_cost_records(cfg)
    rows = [json.loads(line) for line in output_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    task_record = next(row for row in rows if row["record_type"] == "task_cost")

    assert task_record["task_cost_usd"] == 0.135
