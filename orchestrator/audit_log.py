"""Hash-chained append-only audit log with verification and rotation."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestrator.paths import load_config

AUDIT_DIRNAME = "audit"
AUDIT_FILENAME = "audit.jsonl"
MANIFEST_FILENAME = "manifest.json"
ROTATE_BYTES = 10 * 1024 * 1024


def _root_dir(cfg: dict) -> Path:
    configured = str(cfg.get("root_dir") or "").strip()
    repo_root = Path(__file__).resolve().parents[1]
    if configured:
        candidate = Path(configured).expanduser()
        runtime_parent = candidate / "runtime"
        if (
            (candidate.exists() and os.access(candidate, os.W_OK))
            or os.access(candidate.parent, os.W_OK)
            or (runtime_parent.exists() and os.access(runtime_parent, os.W_OK))
            or os.access(runtime_parent.parent, os.W_OK)
        ):
            return candidate
    return repo_root


def audit_dir(cfg: dict) -> Path:
    path = _root_dir(cfg) / "runtime" / AUDIT_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def audit_log_path(cfg: dict) -> Path:
    return audit_dir(cfg) / AUDIT_FILENAME


def audit_manifest_path(cfg: dict) -> Path:
    return audit_dir(cfg) / MANIFEST_FILENAME


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _event_material(ts: str, event_type: str, payload: dict[str, Any]) -> str:
    # Hash the full immutable event body so timestamp and type are protected too.
    return _canonical_json({"ts": ts, "event_type": event_type, "payload": payload})


def _compute_hash(prev_hash: str, ts: str, event_type: str, payload: dict[str, Any]) -> str:
    blob = (prev_hash or "") + _event_material(ts, event_type, payload)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _load_manifest(cfg: dict) -> dict[str, Any]:
    manifest = _read_json(audit_manifest_path(cfg), {"version": 1, "files": []})
    files = manifest.get("files")
    if not isinstance(files, list):
        manifest["files"] = []
    return manifest


def _save_manifest(cfg: dict, manifest: dict[str, Any]) -> None:
    _write_atomic(audit_manifest_path(cfg), json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def _last_manifest_hash(manifest: dict[str, Any]) -> str:
    files = manifest.get("files") or []
    for entry in reversed(files):
        last_hash = str((entry or {}).get("last_hash") or "").strip()
        if last_hash:
            return last_hash
    return ""


def _read_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def _last_record(path: Path) -> dict[str, Any] | None:
    records = _read_records(path)
    return records[-1] if records else None


def _next_rotation_name(audit_dir_path: Path, ts: datetime) -> str:
    stamp = ts.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for idx in range(1, 10000):
        candidate = f"audit-{stamp}-{idx:04d}.jsonl"
        if not (audit_dir_path / candidate).exists():
            return candidate
    raise RuntimeError("unable to allocate unique audit rotation filename")


def _rotate_if_needed(cfg: dict, incoming_line: str) -> None:
    path = audit_log_path(cfg)
    if not path.exists() or path.stat().st_size <= 0:
        return
    incoming_bytes = len(incoming_line.encode("utf-8"))
    if path.stat().st_size + incoming_bytes <= ROTATE_BYTES:
        return

    last = _last_record(path)
    if not last:
        return

    manifest = _load_manifest(cfg)
    audit_path = audit_dir(cfg)
    rotated_name = _next_rotation_name(audit_path, datetime.now(timezone.utc))
    rotated_path = audit_path / rotated_name
    os.replace(path, rotated_path)

    files = list(manifest.get("files") or [])
    if files and files[-1].get("file") == AUDIT_FILENAME:
        files[-1]["file"] = rotated_name
        files[-1]["last_hash"] = str(last.get("hash") or "")
    else:
        files.append({"file": rotated_name, "last_hash": str(last.get("hash") or "")})
    manifest["files"] = files
    _save_manifest(cfg, manifest)


def append_audit_event(cfg: dict, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    payload = dict(payload or {})
    ts = datetime.now(timezone.utc).isoformat()
    manifest = _load_manifest(cfg)
    current = audit_log_path(cfg)
    last = _last_record(current)
    prev_hash = str((last or {}).get("hash") or _last_manifest_hash(manifest) or "")
    record = {
        "ts": ts,
        "event_type": str(event_type),
        "payload": payload,
        "prev_hash": prev_hash,
    }
    record["hash"] = _compute_hash(prev_hash, ts, record["event_type"], payload)
    line = _canonical_json(record) + "\n"

    _rotate_if_needed(cfg, line)
    if current.exists():
        existing = current.read_text(encoding="utf-8")
    else:
        existing = ""
    _write_atomic(current, existing + line)

    manifest = _load_manifest(cfg)
    files = list(manifest.get("files") or [])
    if files and files[-1].get("file") == AUDIT_FILENAME:
        files[-1]["last_hash"] = record["hash"]
    else:
        files.append({"file": AUDIT_FILENAME, "last_hash": record["hash"]})
    manifest["files"] = files
    _save_manifest(cfg, manifest)
    return record


def verify_audit_chain(cfg: dict) -> dict[str, Any]:
    manifest = _load_manifest(cfg)
    audit_path = audit_dir(cfg)
    errors: list[str] = []
    prev_hash = ""
    files_checked = 0
    records_checked = 0
    last_hash = ""
    manifest_files = [entry for entry in manifest.get("files") or [] if isinstance(entry, dict)]

    seen_files: set[str] = set()
    for entry in manifest_files:
        filename = str(entry.get("file") or "").strip()
        expected_last_hash = str(entry.get("last_hash") or "").strip()
        if not filename:
            errors.append("manifest entry missing file name")
            continue
        if filename in seen_files:
            errors.append(f"manifest contains duplicate file entry: {filename}")
            continue
        seen_files.add(filename)
        path = audit_path / filename
        if not path.exists():
            errors.append(f"manifest file missing on disk: {filename}")
            continue

        actual_last_hash = ""
        files_checked += 1
        with path.open("r", encoding="utf-8") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    errors.append(f"{filename}:{line_number} invalid json: {exc}")
                    break
                ts = str(record.get("ts") or "")
                event_type = str(record.get("event_type") or "")
                payload = record.get("payload")
                row_prev = str(record.get("prev_hash") or "")
                row_hash = str(record.get("hash") or "")
                if not isinstance(payload, dict):
                    errors.append(f"{filename}:{line_number} payload must be an object")
                    break
                expected_hash = _compute_hash(prev_hash, ts, event_type, payload)
                if row_prev != prev_hash:
                    errors.append(f"{filename}:{line_number} prev_hash mismatch")
                if row_hash != expected_hash:
                    errors.append(f"{filename}:{line_number} hash mismatch")
                prev_hash = row_hash
                actual_last_hash = row_hash
                records_checked += 1
        if actual_last_hash != expected_last_hash:
            errors.append(
                f"{filename} tail mismatch: manifest={expected_last_hash or '<empty>'} actual={actual_last_hash or '<empty>'}"
            )
        last_hash = actual_last_hash or last_hash

    current = audit_log_path(cfg)
    if current.exists() and current.stat().st_size > 0 and AUDIT_FILENAME not in seen_files:
        errors.append(f"active audit file {AUDIT_FILENAME} is not tracked by manifest")

    return {
        "ok": not errors,
        "files_checked": files_checked,
        "records_checked": records_checked,
        "last_hash": last_hash,
        "errors": errors,
    }


def format_verify_report(report: dict[str, Any]) -> str:
    if report.get("ok"):
        return (
            f"OK: verified {report.get('records_checked', 0)} record(s) across "
            f"{report.get('files_checked', 0)} file(s); tail={report.get('last_hash') or 'none'}"
        )
    head = "; ".join(report.get("errors") or ["unknown verification error"])
    return (
        f"TAMPERED: verified {report.get('records_checked', 0)} record(s) across "
        f"{report.get('files_checked', 0)} file(s); {head}"
    )


def send_tamper_alert(cfg: dict, report: dict[str, Any], *, source: str) -> None:
    token = str(cfg.get("telegram_bot_token", "")).strip()
    chat_id = str(cfg.get("telegram_chat_id", "")).strip()
    if not token or not chat_id:
        return
    text = f"🚨 Audit tampering detected by {source}\n{format_verify_report(report)}"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    subprocess.run(
        [
            "curl",
            "-sS",
            "-X",
            "POST",
            url,
            "-d",
            f"chat_id={chat_id}",
            "--data-urlencode",
            f"text={text}",
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify the hash-chained audit log.")
    parser.add_argument("command", nargs="?", default="verify", choices=["verify"])
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    parser.parse_args(argv)
    cfg = dict(load_config())
    cfg["root_dir"] = str(Path(__file__).resolve().parents[1])
    report = verify_audit_chain(cfg)
    print(format_verify_report(report))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
