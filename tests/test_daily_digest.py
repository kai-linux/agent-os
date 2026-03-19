from __future__ import annotations

import sys
import tempfile
import textwrap
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.daily_digest import (
    compute_agent_success_rates,
    format_digest_message,
    load_recent_mailbox_entries,
    parse_queue_summary_log,
)


def _write_mailbox_file(directory: Path, name: str, content: str, *, hours_ago: int) -> Path:
    path = directory / name
    path.write_text(content, encoding="utf-8")
    ts = (datetime.now(tz=timezone.utc) - timedelta(hours=hours_ago)).timestamp()
    os.utime(path, (ts, ts))
    return path


def test_load_recent_mailbox_entries_uses_mtime_and_skips_escalation_notes():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        done = root / "done"
        done.mkdir()
        cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=24)

        _write_mailbox_file(
            done,
            "task-recent.md",
            textwrap.dedent(
                """\
                ---
                task_id: task-20260319-080000-daily-digest
                github_issue_number: 8
                ---

                # Goal

                Implement daily digest
                """
            ),
            hours_ago=2,
        )
        _write_mailbox_file(done, "task-old.md", "---\ntask_id: task-old\n---\n", hours_ago=30)
        _write_mailbox_file(done, "task-old-escalation.md", "# Escalation Note\n", hours_ago=1)

        entries = load_recent_mailbox_entries(done, "complete", cutoff)

        assert len(entries) == 1
        assert entries[0]["task_id"] == "task-20260319-080000-daily-digest"
        assert entries[0]["label"] == "#8 Implement daily digest"


def test_parse_queue_summary_log_tracks_agent_per_task():
    with tempfile.TemporaryDirectory() as d:
        log_file = Path(d) / "queue-summary.log"
        log_file.write_text(
            textwrap.dedent(
                """\
                [w0] Processing task: task-1
                Worker status from codex: complete
                Final queue state: done
                [w1] Processing task: task-2
                Worker status from claude: blocked
                Final queue state: escalated
                """
            ),
            encoding="utf-8",
        )

        details = parse_queue_summary_log(log_file)

        assert details["task-1"]["agent"] == "codex"
        assert details["task-1"]["worker_status"] == "complete"
        assert details["task-2"]["agent"] == "claude"
        assert details["task-2"]["queue_state"] == "escalated"


def test_compute_agent_success_rates_counts_completed_vs_non_completed():
    entries = [
        {"task_id": "task-1", "status": "complete"},
        {"task_id": "task-2", "status": "blocked"},
        {"task_id": "task-3", "status": "escalated"},
    ]
    queue_details = {
        "task-1": {"agent": "codex"},
        "task-2": {"agent": "codex"},
        "task-3": {"agent": "claude"},
    }

    rates = compute_agent_success_rates(entries, queue_details)

    assert rates["codex"]["successes"] == 1
    assert rates["codex"]["total"] == 2
    assert rates["codex"]["rate"] == 0.5
    assert rates["claude"]["successes"] == 0
    assert rates["claude"]["total"] == 1


def test_format_digest_message_handles_no_activity():
    message = format_digest_message([], [], [], {}, {"created": 0, "merged": 0}, datetime.now(tz=timezone.utc))
    assert message == "📬 Daily Digest\nℹ️ No activity yesterday."


def test_format_digest_message_stays_compact():
    now = datetime(2026, 3, 19, 8, 0, tzinfo=timezone.utc)
    completed = [{"label": f"#1 task {idx}", "status": "complete"} for idx in range(5)]
    blocked = [{"label": "#2 blocked task", "status": "blocked"}]
    escalated = [{"label": "#3 escalated task", "status": "escalated"}]
    agent_rates = {
        "codex": {"successes": 3, "total": 4, "rate": 0.75},
        "claude": {"successes": 1, "total": 2, "rate": 0.5},
    }

    message = format_digest_message(completed, blocked, escalated, agent_rates, {"created": 2, "merged": 1}, now)

    assert "✅ Completed: 5" in message
    assert "- +2 more" in message
    assert "🔀 PR Activity" in message
    assert len(message.splitlines()) < 40
