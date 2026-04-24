"""File-backed human approvals inbox under runtime/approvals/."""
from __future__ import annotations

import argparse
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from orchestrator.paths import ROOT, load_config

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)

DEFAULT_EXPIRY_HOURS = {
    "sprint_plan": 24.0,
    "system_architect": 72.0,
    "high_risk_pr": 48.0,
    "repeat_blocker": 48.0,
    "budget_override": 48.0,
}

AUTO_EXPIRY_DEFAULTS = {
    "sprint_plan": ("skip", "Auto-expired at approval deadline; defaulted to skip."),
    "system_architect": ("skip", "Auto-expired at approval deadline; defaulted to skip."),
    "high_risk_pr": ("hold", "Auto-expired at approval deadline; defaulted to hold."),
    "repeat_blocker": ("hold", "Auto-expired at approval deadline; defaulted to hold."),
    "budget_override": ("hold", "Auto-expired at approval deadline; defaulted to hold."),
}


def _approvals_dirs(cfg: dict) -> tuple[Path, Path]:
    root = Path(cfg.get("root_dir") or ROOT).expanduser()
    pending = root / "runtime" / "approvals"
    resolved = pending / "resolved"
    pending.mkdir(parents=True, exist_ok=True)
    resolved.mkdir(parents=True, exist_ok=True)
    return pending, resolved


def _approval_path(base: Path, approval_id: str) -> Path:
    return base / f"approval-{approval_id}.md"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def build_action_url(chat_id: str | None, message_id: int | str | None) -> str:
    chat = str(chat_id or "").strip()
    if not chat or message_id in (None, ""):
        return ""
    msg = str(message_id).strip()
    if chat.startswith("-100") and chat[4:].isdigit():
        return f"https://t.me/c/{chat[4:]}/{msg}"
    return f"telegram://message?chat_id={chat}&message_id={msg}"


def _render(record: dict[str, Any]) -> str:
    frontmatter = yaml.safe_dump(record, sort_keys=False, allow_unicode=False).strip()
    context = record.get("context") or {}
    context_yaml = yaml.safe_dump(context, sort_keys=False, allow_unicode=False).rstrip()
    lines = [
        "---",
        frontmatter,
        "---",
        f"# Approval {record['id']}",
        "",
        "Edit `decision` / `reason` in the frontmatter, then run `bin/aos-approvals resolve <id>`.",
        "",
        "## Context",
        "```yaml",
        context_yaml or "{}",
        "```",
        "",
    ]
    return "\n".join(lines)


def _read_record(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise ValueError(f"Approval file missing frontmatter: {path}")
    payload = yaml.safe_load(match.group(1)) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"Approval frontmatter must be a mapping: {path}")
    payload["body"] = match.group(2)
    payload["_path"] = str(path)
    return payload


def _write_record(path: Path, record: dict[str, Any]) -> None:
    serializable = dict(record)
    serializable.pop("body", None)
    serializable.pop("_path", None)
    path.write_text(_render(serializable), encoding="utf-8")


def _iter_records(cfg: dict, *, include_resolved: bool = False) -> list[dict[str, Any]]:
    pending_dir, resolved_dir = _approvals_dirs(cfg)
    roots = [pending_dir]
    if include_resolved:
        roots.append(resolved_dir)
    records: list[dict[str, Any]] = []
    for root in roots:
        for path in sorted(root.glob("approval-*.md")):
            try:
                records.append(_read_record(path))
            except Exception:
                continue
    return records


def get(cfg: dict, approval_id: str) -> dict[str, Any] | None:
    pending_dir, resolved_dir = _approvals_dirs(cfg)
    for root in (pending_dir, resolved_dir):
        path = _approval_path(root, approval_id)
        if path.exists():
            return _read_record(path)
    return None


def request(
    cfg: dict,
    *,
    kind: str,
    context: dict[str, Any],
    approval_id: str | None = None,
    expires_at: str | None = None,
    expiry_hours: float | None = None,
    action_url: str = "",
    telegram_message_id: int | None = None,
) -> dict[str, Any]:
    expire_pending(cfg)
    pending_dir, _ = _approvals_dirs(cfg)
    dedup_key = str((context or {}).get("dedup_key") or "").strip()
    if dedup_key:
        for record in _iter_records(cfg):
            if record.get("status") != "pending":
                continue
            if record.get("kind") != kind:
                continue
            current_key = str((record.get("context") or {}).get("dedup_key") or "").strip()
            if current_key == dedup_key:
                return record

    approval_id = approval_id or uuid4().hex[:12]
    created_at = _now()
    expiry_dt = _parse_dt(expires_at)
    if expiry_dt is None:
        ttl_hours = float(expiry_hours if expiry_hours is not None else DEFAULT_EXPIRY_HOURS.get(kind, 48.0))
        expiry_dt = created_at + timedelta(hours=ttl_hours)
    record = {
        "id": approval_id,
        "kind": kind,
        "status": "pending",
        "created_at": _iso(created_at),
        "expires_at": _iso(expiry_dt),
        "decision": "pending",
        "reason": "",
        "action_url": action_url or "",
        "telegram_message_id": telegram_message_id,
        "context": context or {},
    }
    _write_record(_approval_path(pending_dir, approval_id), record)
    return record


def resolve(cfg: dict, approval_id: str, decision: str, reason: str) -> dict[str, Any]:
    pending_dir, resolved_dir = _approvals_dirs(cfg)
    pending_path = _approval_path(pending_dir, approval_id)
    resolved_path = _approval_path(resolved_dir, approval_id)
    if pending_path.exists():
        record = _read_record(pending_path)
        source_path = pending_path
    elif resolved_path.exists():
        record = _read_record(resolved_path)
        source_path = resolved_path
    else:
        raise FileNotFoundError(f"Approval not found: {approval_id}")

    record["status"] = "resolved"
    record["decision"] = str(decision or "").strip()
    record["reason"] = str(reason or "").strip()
    record["resolved_at"] = _iso(_now())
    _write_record(resolved_path, record)
    if source_path != resolved_path and source_path.exists():
        source_path.unlink()
    return _read_record(resolved_path)


def update(cfg: dict, approval_id: str, **fields: Any) -> dict[str, Any]:
    pending_dir, resolved_dir = _approvals_dirs(cfg)
    for root in (pending_dir, resolved_dir):
        path = _approval_path(root, approval_id)
        if not path.exists():
            continue
        record = _read_record(path)
        record.update(fields)
        _write_record(path, record)
        return _read_record(path)
    raise FileNotFoundError(f"Approval not found: {approval_id}")


def list_pending(
    cfg: dict,
    *,
    kind: str | None = None,
    repo: str | None = None,
) -> list[dict[str, Any]]:
    expire_pending(cfg)
    records = [record for record in _iter_records(cfg) if record.get("status") == "pending"]
    if kind:
        records = [record for record in records if record.get("kind") == kind]
    if repo:
        records = [record for record in records if str((record.get("context") or {}).get("repo") or "").strip() == repo]
    records.sort(key=lambda item: (str(item.get("expires_at") or ""), str(item.get("id") or "")))
    return records


def list_resolved(
    cfg: dict,
    *,
    kind: str | None = None,
) -> list[dict[str, Any]]:
    records = [record for record in _iter_records(cfg, include_resolved=True) if record.get("status") == "resolved"]
    if kind:
        records = [record for record in records if record.get("kind") == kind]
    records.sort(key=lambda item: (str(item.get("resolved_at") or ""), str(item.get("id") or "")))
    return records


def expire_pending(cfg: dict, *, now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or _now()
    expired: list[dict[str, Any]] = []
    for record in _iter_records(cfg):
        if record.get("status") != "pending":
            continue
        expires_at = _parse_dt(record.get("expires_at"))
        if expires_at is None or expires_at > now:
            continue
        decision, default_reason = AUTO_EXPIRY_DEFAULTS.get(
            str(record.get("kind") or ""),
            ("hold", "Auto-expired at approval deadline; defaulted to hold."),
        )
        reason = default_reason
        if record.get("reason"):
            reason = str(record.get("reason"))
        resolved = resolve(cfg, str(record.get("id")), decision, reason)
        resolved["expired_at"] = _iso(now)
        _write_record(Path(resolved["_path"]), resolved)
        expired.append(resolved)
    return expired


def _cmd_list(args: argparse.Namespace) -> int:
    cfg = load_config()
    rows = list_pending(cfg, kind=args.kind, repo=args.repo)
    if not rows:
        print("No pending approvals.")
        return 0
    for row in rows:
        repo = str((row.get("context") or {}).get("repo") or "-")
        print(f"{row['id']}\t{row['kind']}\t{repo}\t{row['expires_at']}")
    return 0


def _cmd_resolve(args: argparse.Namespace) -> int:
    cfg = load_config()
    record = get(cfg, args.approval_id)
    if record is None:
        raise SystemExit(f"Approval not found: {args.approval_id}")
    decision = args.decision or str(record.get("decision") or "").strip()
    reason = args.reason if args.reason is not None else str(record.get("reason") or "").strip()
    if not decision or decision == "pending":
        raise SystemExit("Resolution requires a non-pending decision (via flags or edited frontmatter).")
    resolved = resolve(cfg, args.approval_id, decision, reason)
    print(f"Resolved {resolved['id']} as {resolved['decision']}.")
    return 0


def _cmd_expire(_args: argparse.Namespace) -> int:
    cfg = load_config()
    expired = expire_pending(cfg)
    print(f"Expired {len(expired)} approval(s).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aos-approvals")
    sub = parser.add_subparsers(dest="command", required=True)

    list_parser = sub.add_parser("list")
    list_parser.add_argument("--kind")
    list_parser.add_argument("--repo")
    list_parser.set_defaults(func=_cmd_list)

    resolve_parser = sub.add_parser("resolve")
    resolve_parser.add_argument("approval_id")
    resolve_parser.add_argument("--decision")
    resolve_parser.add_argument("--reason")
    resolve_parser.set_defaults(func=_cmd_resolve)

    expire_parser = sub.add_parser("expire")
    expire_parser.set_defaults(func=_cmd_expire)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
