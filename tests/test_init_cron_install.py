from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.init.cron_install import BLOCK_BEGIN, BLOCK_END, build_managed_block, merge_block, strip_managed_block


def test_merge_block_appends_when_missing(tmp_path):
    current = "MAILTO=\n0 1 * * * echo hi\n"
    block = build_managed_block(tmp_path / "root", "/usr/bin")
    merged, changed = merge_block(current, block)

    assert changed is True
    assert "MAILTO=" in merged
    assert BLOCK_BEGIN in merged


def test_merge_block_is_idempotent(tmp_path):
    block = build_managed_block(tmp_path / "root", "/usr/bin")
    merged, changed = merge_block(block, block)

    assert changed is False
    assert merged.count(BLOCK_BEGIN) == 1


def test_strip_managed_block_preserves_other_entries(tmp_path):
    block = build_managed_block(tmp_path / "root", "/usr/bin")
    current = f"MAILTO=\n\n{block}\n0 1 * * * echo hi\n"
    remaining, removed = strip_managed_block(current)

    assert removed is not None
    assert BLOCK_BEGIN not in remaining
    assert BLOCK_END not in remaining
    assert "echo hi" in remaining

