"""Regression tests for codebase_memory injection-size controls.

The E2BIG credit-burn escalations on 2026-04-21 (issues #246, #247) were
traced to CODEBASE.md injection consuming ~110 KB of the 100 KB prompt
budget: the on-disk file accumulates one Recent Changes entry per completed
task forever, and ``read_codebase_context`` inlined the whole thing. The
on-disk file stays intact as an audit trail, but the injected view caps
``## Recent Changes`` to the newest N entries.
"""
from __future__ import annotations

from pathlib import Path

from orchestrator.codebase_memory import (
    RECENT_CHANGES_MAX_ENTRIES,
    _truncate_recent_changes,
    read_codebase_context,
)


def _make_codebase_md(tmp_path: Path, num_entries: int) -> Path:
    entries = []
    for i in range(num_entries):
        entries.append(
            f"### 2026-04-{(i % 28) + 1:02d} — [task-{i:04d}] (#{i} example/repo)\n"
            f"Summary for entry {i}.\n\n"
            "**Files:** `- a.py`, `- b.py`\n\n"
            "**Decisions:**\n  - decision\n"
        )
    content = (
        "# Codebase Memory\n\n"
        "## Architecture\n\nsome architecture notes\n\n"
        "## Key Files\n\nkey files here\n\n"
        "## Known Issues / Gotchas\n\n"
        "### A permanent gotcha\nthis stays in full\n\n"
        "## Recent Changes\n\n" + "\n".join(entries)
    )
    path = tmp_path / "CODEBASE.md"
    path.write_text(content, encoding="utf-8")
    return path


def test_truncate_keeps_only_newest_entries():
    body = (
        "# header\n\n"
        "## Architecture\narch\n\n"
        "## Recent Changes\n\n"
        + "\n".join(f"### entry-{i}\nbody {i}\n" for i in range(50))
    )
    out = _truncate_recent_changes(body, max_entries=5)
    assert "### entry-0\n" in out
    assert "### entry-4\n" in out
    # Oldest entries dropped (entries 5..49)
    assert "### entry-5\n" not in out
    assert "### entry-49\n" not in out
    assert "Truncated for prompt budget" in out


def test_truncate_noop_when_under_cap():
    body = (
        "## Recent Changes\n\n"
        + "\n".join(f"### entry-{i}\nx\n" for i in range(3))
    )
    out = _truncate_recent_changes(body, max_entries=15)
    assert out == body


def test_truncate_preserves_header_sections():
    body = (
        "# Codebase Memory\n\n"
        "## Architecture\n\nimportant arch notes\n\n"
        "## Known Issues / Gotchas\n\n### Gotcha One\ndetails\n\n"
        "## Recent Changes\n\n"
        + "\n".join(f"### entry-{i}\n" for i in range(30))
    )
    out = _truncate_recent_changes(body, max_entries=5)
    assert "important arch notes" in out
    assert "### Gotcha One" in out
    assert "details" in out


def test_truncate_noop_without_recent_changes_section():
    body = "## Architecture\n\nno recent changes section here\n"
    assert _truncate_recent_changes(body) == body


def test_read_codebase_context_truncates_large_file(tmp_path):
    _make_codebase_md(tmp_path, num_entries=RECENT_CHANGES_MAX_ENTRIES * 4)
    ctx = read_codebase_context(tmp_path)
    # Header framing present
    assert "# Codebase Memory (read-only context)" in ctx
    # Truncation marker present when we exceed the cap
    assert "Truncated for prompt budget" in ctx
    # Budget check: the injected context must stay well under the argv ceiling
    assert len(ctx) < 60_000


def test_read_codebase_context_empty_when_missing(tmp_path):
    assert read_codebase_context(tmp_path) == ""
