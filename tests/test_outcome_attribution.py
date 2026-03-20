from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.outcome_attribution import (
    append_outcome_record,
    load_outcome_records,
    parse_outcome_check_ids,
)


def test_parse_outcome_check_ids_handles_bullets_and_csv():
    parsed = parse_outcome_check_ids("- activation_rate\nsignup_completion, retention_7d\nnone")
    assert parsed == ["activation_rate", "signup_completion", "retention_7d"]


def test_append_outcome_record_and_load_round_trip(tmp_path):
    cfg = {"root_dir": str(tmp_path)}
    append_outcome_record(
        cfg,
        {
            "record_type": "attribution",
            "event": "pr_opened",
            "repo": "owner/repo",
            "task_id": "task-123",
            "issue_number": 64,
            "pr_number": 70,
            "outcome_check_ids": ["activation_rate"],
        },
    )

    records = load_outcome_records(cfg, repo="owner/repo")

    assert len(records) == 1
    assert records[0]["task_id"] == "task-123"
    assert records[0]["outcome_check_ids"] == ["activation_rate"]
    raw = (tmp_path / "runtime" / "metrics" / "outcome_attribution.jsonl").read_text(encoding="utf-8")
    assert json.loads(raw.splitlines()[0])["pr_number"] == 70
