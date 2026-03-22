from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.objectives import (
    load_repo_objective,
    objective_feedback_inputs,
    objective_outcome_checks,
    score_objective_snapshots,
)


def test_load_repo_objective_from_default_slug_path(tmp_path):
    objectives_dir = tmp_path / "objectives"
    objectives_dir.mkdir()
    path = objectives_dir / "repo.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "repo": "owner/repo",
                "metrics": [{"id": "traffic", "name": "Traffic", "source": {"type": "file", "path": "/tmp/traffic.json"}}],
            }
        ),
        encoding="utf-8",
    )

    objective = load_repo_objective(
        {"objectives_dir": str(objectives_dir)},
        "owner/repo",
        tmp_path / "repo",
    )

    assert objective["repo"] == "owner/repo"
    assert objective["_objective_path"] == str(path)


def test_objective_translates_to_feedback_inputs_and_outcome_checks():
    objective = {
        "evaluation_window_days": 28,
        "metrics": [
            {
                "id": "conversion",
                "name": "Signup conversion",
                "source": {"type": "file", "path": "/tmp/conversion.json", "signal_class": "analytics"},
                "outcome_check": {"type": "file", "path": "/tmp/conversion-post.json"},
            }
        ],
    }

    inputs = objective_feedback_inputs(objective)
    checks = objective_outcome_checks(objective)

    assert inputs[0]["metric_id"] == "conversion"
    assert checks[0]["id"] == "conversion"
    assert checks[0]["measurement_window_days"] == 28


def test_score_objective_snapshots_weights_recent_business_outcomes():
    now = datetime.now(tz=timezone.utc).isoformat()
    objective = {
        "evaluation_window_days": 28,
        "metrics": [
            {"id": "traffic", "weight": 0.2},
            {"id": "conversion", "weight": 0.8},
        ],
    }
    snapshots = [
        {"record_type": "snapshot", "timestamp": now, "check_id": "traffic", "interpretation": "improved"},
        {"record_type": "snapshot", "timestamp": now, "check_id": "conversion", "interpretation": "regressed"},
    ]

    score = score_objective_snapshots(objective, snapshots)

    assert round(score["score"], 2) == -0.6
    assert score["counts"]["improved"] == 1
    assert score["counts"]["regressed"] == 1
