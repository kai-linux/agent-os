from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.audit_log import append_audit_event, audit_log_path, verify_audit_chain


def _cfg(tmp_path: Path) -> dict:
    return {"root_dir": str(tmp_path)}


def test_verify_audit_chain_accepts_clean_chain(tmp_path):
    cfg = _cfg(tmp_path)

    append_audit_event(cfg, "test_event", {"value": 1})
    append_audit_event(cfg, "test_event", {"value": 2})
    append_audit_event(cfg, "test_event", {"value": 3})

    report = verify_audit_chain(cfg)

    assert report["ok"] is True
    assert report["records_checked"] == 3
    assert report["files_checked"] == 1


def test_verify_audit_chain_detects_mutated_middle_line(tmp_path):
    cfg = _cfg(tmp_path)

    append_audit_event(cfg, "test_event", {"value": 1})
    append_audit_event(cfg, "test_event", {"value": 2})
    append_audit_event(cfg, "test_event", {"value": 3})

    path = audit_log_path(cfg)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[1]["payload"]["value"] = 999
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")

    report = verify_audit_chain(cfg)

    assert report["ok"] is False
    assert any("hash mismatch" in error for error in report["errors"])


def test_verify_audit_chain_detects_truncated_tail(tmp_path):
    cfg = _cfg(tmp_path)

    append_audit_event(cfg, "test_event", {"value": 1})
    append_audit_event(cfg, "test_event", {"value": 2})
    append_audit_event(cfg, "test_event", {"value": 3})

    path = audit_log_path(cfg)
    rows = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(rows[:-1]) + "\n", encoding="utf-8")

    report = verify_audit_chain(cfg)

    assert report["ok"] is False
    assert any("tail mismatch" in error for error in report["errors"])
