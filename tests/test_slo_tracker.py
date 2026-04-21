from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.slo_tracker import build_slo_state_records, rebuild_slo_state


def test_build_slo_state_records_excludes_transient_blockers_and_maps_repo_slug(tmp_path):
    repo_dir = tmp_path / "agent-os"
    repo_dir.mkdir()
    (tmp_path / "slos").mkdir()
    (tmp_path / "slos" / "agent-os.yaml").write_text(
        """
slos:
  - id: success_rate
    target: 0.9
    window_days: 30
""".strip(),
        encoding="utf-8",
    )

    metrics_dir = tmp_path / "runtime" / "metrics"
    metrics_dir.mkdir(parents=True)
    rows = [
        {"timestamp": "2026-04-20T10:00:00+00:00", "github_repo": "kai-linux/agent-os", "status": "complete", "blocker_code": "none"},
        {"timestamp": "2026-04-20T11:00:00+00:00", "github_repo": "kai-linux/agent-os", "status": "complete", "blocker_code": "none"},
        {"timestamp": "2026-04-20T12:00:00+00:00", "github_repo": "kai-linux/agent-os", "status": "blocked", "blocker_code": "missing_context"},
        {"timestamp": "2026-04-20T13:00:00+00:00", "github_repo": "kai-linux/agent-os", "status": "blocked", "blocker_code": "quota_limited"},
    ]
    (metrics_dir / "agent_stats.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )

    cfg = {
        "root_dir": str(tmp_path),
        "github_projects": {
            "default": {
                "repos": [
                    {"github_repo": "kai-linux/agent-os", "path": str(repo_dir)},
                ]
            }
        },
    }

    records = build_slo_state_records(
        cfg,
        now=datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc),
    )

    assert len(records) == 1
    assert records[0]["repo"] == "kai-linux/agent-os"
    assert records[0]["slo_id"] == "success_rate"
    assert records[0]["current"] == 0.6667
    assert records[0]["budget_remaining_pct"] == 0.0
    assert records[0]["burn_rate"] == 3.3333


def test_build_slo_state_records_computes_merge_cycle_p95_from_outcome_attribution(tmp_path):
    (tmp_path / "slos").mkdir()
    (tmp_path / "slos" / "repo.yaml").write_text(
        """
slos:
  - id: merge_cycle_p95
    target: 4h
    window_days: 30
""".strip(),
        encoding="utf-8",
    )

    metrics_dir = tmp_path / "runtime" / "metrics"
    metrics_dir.mkdir(parents=True)
    outcome_rows = [
        {"record_type": "attribution", "event": "pr_opened", "repo": "owner/repo", "pr_number": 1, "timestamp": "2026-04-10T08:00:00+00:00"},
        {"record_type": "attribution", "event": "merged", "repo": "owner/repo", "pr_number": 1, "merged_at": "2026-04-10T10:00:00+00:00", "timestamp": "2026-04-10T10:00:00+00:00"},
        {"record_type": "attribution", "event": "pr_opened", "repo": "owner/repo", "pr_number": 2, "timestamp": "2026-04-11T08:00:00+00:00"},
        {"record_type": "attribution", "event": "merged", "repo": "owner/repo", "pr_number": 2, "merged_at": "2026-04-11T13:00:00+00:00", "timestamp": "2026-04-11T13:00:00+00:00"},
        {"record_type": "attribution", "event": "pr_opened", "repo": "owner/repo", "pr_number": 3, "timestamp": "2026-04-12T08:00:00+00:00"},
        {"record_type": "attribution", "event": "merged", "repo": "owner/repo", "pr_number": 3, "merged_at": "2026-04-12T18:00:00+00:00", "timestamp": "2026-04-12T18:00:00+00:00"},
    ]
    (metrics_dir / "outcome_attribution.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in outcome_rows),
        encoding="utf-8",
    )

    cfg = {"root_dir": str(tmp_path), "github_projects": {"default": {"repos": [{"github_repo": "owner/repo", "path": str(tmp_path / "repo")}]}}}
    records = build_slo_state_records(
        cfg,
        now=datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc),
    )

    assert len(records) == 1
    assert records[0]["repo"] == "owner/repo"
    assert records[0]["slo_id"] == "merge_cycle_p95"
    assert records[0]["target"] == 4.0
    assert records[0]["current"] == 10.0
    assert records[0]["budget_remaining_pct"] == 0.0
    assert records[0]["burn_rate"] == 2.5


def test_rebuild_slo_state_writes_standardized_jsonl_file(tmp_path):
    (tmp_path / "slos").mkdir()
    (tmp_path / "slos" / "repo.yaml").write_text(
        """
success_rate:
  target: 0.9
  window_days: 30
""".strip(),
        encoding="utf-8",
    )
    metrics_dir = tmp_path / "runtime" / "metrics"
    metrics_dir.mkdir(parents=True)
    (metrics_dir / "agent_stats.jsonl").write_text(
        json.dumps(
            {
                "timestamp": "2026-04-20T10:00:00+00:00",
                "github_repo": "repo",
                "status": "complete",
                "blocker_code": "none",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    output = rebuild_slo_state(
        {"root_dir": str(tmp_path)},
        now=datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc),
    )

    rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert output.name == "slo_state.jsonl"
    assert rows == [
        {
            "budget_remaining_pct": 100.0,
            "burn_rate": 0.0,
            "current": 1.0,
            "repo": "repo",
            "slo_id": "success_rate",
            "target": 0.9,
            "ts": "2026-04-21T12:00:00+00:00",
        }
    ]


def test_rebuild_slo_state_is_empty_when_no_repo_opted_in(tmp_path):
    output = rebuild_slo_state(
        {"root_dir": str(tmp_path)},
        now=datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc),
    )

    assert output.read_text(encoding="utf-8") == ""
