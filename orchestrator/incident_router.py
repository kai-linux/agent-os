from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from orchestrator.paths import load_config

VALID_SEVERITIES = ("sev1", "sev2", "sev3")
INCIDENTS_FILENAME = "incidents.jsonl"
DEFAULT_SEV1_CHECKLIST = [
    "Acknowledge the incident and pause risky automation if user-facing impact is ongoing.",
    "Check the linked repo/run context and validate whether the failure is still active.",
    "Follow the runbook or rollback path before retrying automation.",
]


def _now_utc(now: datetime | None = None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _normalize_dt(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def incidents_path(cfg: dict | None = None) -> Path:
    cfg = cfg or load_config()
    root = Path(cfg.get("root_dir", ".")).expanduser()
    path = root / "runtime" / "incidents"
    path.mkdir(parents=True, exist_ok=True)
    return path / INCIDENTS_FILENAME


def _router_cfg(cfg: dict) -> dict[str, Any]:
    router = deepcopy(cfg.get("incident_router") or {})
    router.setdefault("business_timezone", "UTC")
    router.setdefault("business_hours", {"start_hour": 9, "end_hour": 17})
    router.setdefault("digest_hour", 9)
    router.setdefault("tiers", {})
    router.setdefault("sources", {})
    for severity, defaults in {
        "sev1": {
            "delivery": "immediate",
            "dedup_window_minutes": 0,
            "bypass_kill_switch": True,
            "handlers": [{"type": "telegram_chat", "snooze_minutes": 0}],
            "snooze_minutes": 0,
        },
        "sev2": {
            "delivery": "next_business_hour",
            "dedup_window_minutes": 60,
            "bypass_kill_switch": False,
            "handlers": [{"type": "telegram_chat", "snooze_minutes": 60}],
            "snooze_minutes": 60,
        },
        "sev3": {
            "delivery": "regular_digest",
            "dedup_window_minutes": 240,
            "bypass_kill_switch": False,
            "handlers": [{"type": "telegram_chat", "snooze_minutes": 240}],
            "snooze_minutes": 240,
        },
    }.items():
        merged = dict(defaults)
        merged.update(router["tiers"].get(severity) or {})
        merged["handlers"] = _normalize_handlers(merged.get("handlers"), default_snooze=merged.get("snooze_minutes", 0))
        router["tiers"][severity] = merged
    return router


def _normalize_handlers(raw: Any, *, default_snooze: int) -> list[dict[str, Any]]:
    handlers: list[dict[str, Any]] = []
    for item in raw or []:
        if isinstance(item, str):
            handlers.append({"type": item, "snooze_minutes": default_snooze})
            continue
        if not isinstance(item, dict):
            continue
        handler = dict(item)
        handler.setdefault("type", "telegram_chat")
        handler.setdefault("snooze_minutes", default_snooze)
        handlers.append(handler)
    return handlers


def _business_timezone(router_cfg: dict) -> ZoneInfo:
    try:
        return ZoneInfo(str(router_cfg.get("business_timezone") or "UTC"))
    except Exception:
        return ZoneInfo("UTC")


def _business_window(router_cfg: dict) -> tuple[int, int]:
    hours = router_cfg.get("business_hours") or {}
    start_hour = int(hours.get("start_hour", 9) or 9)
    end_hour = int(hours.get("end_hour", 17) or 17)
    return start_hour, end_hour


def _in_business_hours(router_cfg: dict, now: datetime) -> bool:
    local_now = now.astimezone(_business_timezone(router_cfg))
    start_hour, end_hour = _business_window(router_cfg)
    return start_hour <= local_now.hour < end_hour


def _next_business_start(router_cfg: dict, now: datetime) -> datetime:
    zone = _business_timezone(router_cfg)
    local_now = now.astimezone(zone)
    start_hour, end_hour = _business_window(router_cfg)
    if local_now.hour < start_hour:
        candidate = local_now.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    elif local_now.hour >= end_hour:
        candidate = (local_now + timedelta(days=1)).replace(hour=start_hour, minute=0, second=0, microsecond=0)
    else:
        candidate = local_now
    return candidate.astimezone(timezone.utc)


def _next_digest_at(router_cfg: dict, now: datetime) -> datetime:
    zone = _business_timezone(router_cfg)
    local_now = now.astimezone(zone)
    digest_hour = int(router_cfg.get("digest_hour", 9) or 9)
    candidate = local_now.replace(hour=digest_hour, minute=0, second=0, microsecond=0)
    if local_now >= candidate:
        candidate = candidate + timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def _incident_key(source: str, event: dict[str, Any]) -> str:
    key = event.get("dedup_key")
    if key:
        return str(key)
    canonical = {
        "source": source,
        "type": event.get("type"),
        "repo": event.get("repo"),
        "github_repo": event.get("github_repo"),
        "github_issue_number": event.get("github_issue_number"),
        "pr_number": event.get("pr_number"),
        "blocker_code": event.get("blocker_code"),
        "title": event.get("title"),
        "summary": event.get("summary"),
    }
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _load_incidents(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    incidents: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                incidents.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return incidents


def _write_incidents(path: Path, incidents: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(json.dumps(item, sort_keys=True) + "\n" for item in incidents)
    path.write_text(payload, encoding="utf-8")


def list_incidents(cfg: dict | None = None) -> list[dict[str, Any]]:
    return _load_incidents(incidents_path(cfg))


def open_incidents(cfg: dict | None = None, *, severity: str | None = None) -> list[dict[str, Any]]:
    """Return incidents that still require operator action."""
    severity_filter = str(severity or "").strip().lower()
    rows: list[dict[str, Any]] = []
    for incident in list_incidents(cfg):
        if incident.get("resolved_at") or incident.get("deduped_to"):
            continue
        if severity_filter and str(incident.get("sev") or "").lower() != severity_filter:
            continue
        rows.append(incident)
    return rows


def _tier_cfg(cfg: dict, severity: str) -> dict[str, Any]:
    router = _router_cfg(cfg)
    if severity not in VALID_SEVERITIES:
        raise ValueError(f"Unsupported incident severity: {severity}")
    return dict(router["tiers"][severity])


def classify_severity(cfg: dict, source: str, event: dict[str, Any]) -> str:
    router = _router_cfg(cfg)
    source_cfg = router["sources"].get(source) or {}
    for rule in source_cfg.get("rules", []) or []:
        severity = str(rule.get("severity") or "").strip().lower()
        if severity not in VALID_SEVERITIES:
            continue
        matches = True
        for key, expected in rule.items():
            if key == "severity":
                continue
            if event.get(key) != expected:
                matches = False
                break
        if matches:
            return severity
    default_severity = str(source_cfg.get("default_severity") or "sev3").strip().lower()
    return default_severity if default_severity in VALID_SEVERITIES else "sev3"


def _record_matches_window(incident: dict[str, Any], source: str, event_key: str, severity: str, now: datetime, window_minutes: int) -> bool:
    if incident.get("source") != source or incident.get("sev") != severity:
        return False
    if incident.get("resolved_at"):
        return False
    if str(incident.get("event_key") or "") != event_key:
        return False
    created_at = _normalize_dt(incident.get("created_at"))
    if created_at is None:
        return False
    return created_at >= now - timedelta(minutes=max(window_minutes, 0))


def _build_message(incident: dict[str, Any], tier_cfg: dict[str, Any]) -> str:
    event = incident.get("event") or {}
    delivery = tier_cfg.get("delivery", "immediate")
    lines = [
        f"🚨 {incident['sev'].upper()} incident",
        f"Source: {incident['source']}",
        f"Route: {delivery}",
    ]
    if event.get("repo") or event.get("github_repo"):
        lines.append(f"Repo: {event.get('repo') or event.get('github_repo')}")
    if event.get("task_id"):
        lines.append(f"Task: {event['task_id']}")
    if event.get("pr_number"):
        lines.append(f"PR: #{event['pr_number']}")
    if event.get("github_issue_number"):
        lines.append(f"Issue: #{event['github_issue_number']}")
    lines.extend(
        [
            f"Incident: {incident['id']}",
            "",
            str(event.get("summary") or "No summary provided."),
        ]
    )
    details = []
    for key in ("blocker_code", "risk_level", "verdict", "route_reason"):
        if event.get(key):
            details.append(f"{key}: {event[key]}")
    if details:
        lines.extend(["", "Details:"] + [f"- {item}" for item in details])
    if incident["sev"] == "sev1":
        runbook_url = str(event.get("runbook_url") or "").strip()
        checklist = event.get("checklist") or DEFAULT_SEV1_CHECKLIST
        if runbook_url:
            lines.extend(["", f"Runbook: {runbook_url}"])
        lines.extend(["", "Immediate checklist:"] + [f"- {item}" for item in checklist])
    else:
        lines.extend(["", "Commands:", f"/ack {incident['id']}", f"/resolve {incident['id']}"])
    text = "\n".join(lines).strip()
    return text if len(text) <= 4000 else text[:3997] + "..."


def _send_incident(cfg: dict, incident: dict[str, Any], tier_cfg: dict[str, Any], *, logfile: Path | None = None, queue_summary_log: Path | None = None) -> bool:
    from orchestrator.queue import send_telegram

    message = _build_message(incident, tier_cfg)
    reply_markup = incident.get("event", {}).get("reply_markup")
    delivered = False
    for handler in tier_cfg.get("handlers") or []:
        if str(handler.get("type") or "").strip() != "telegram_chat":
            continue
        message_id = send_telegram(
            cfg,
            message,
            logfile,
            queue_summary_log,
            reply_markup=reply_markup,
            chat_id=str(handler.get("chat_id") or "").strip() or None,
            bypass_kill_switch=bool(tier_cfg.get("bypass_kill_switch")),
        )
        if not message_id:
            continue
        incident["message_id"] = message_id
        incident["notified_at"] = _now_utc().isoformat()
        delivered = True
    return delivered


def _incident_due(router_cfg: dict, incident: dict[str, Any], now: datetime) -> bool:
    severity = str(incident.get("sev") or "").lower()
    tier_cfg = dict(router_cfg["tiers"].get(severity) or {})
    delivery = tier_cfg.get("delivery", "immediate")
    created_at = _normalize_dt(incident.get("created_at")) or now
    if delivery == "immediate":
        return True
    if delivery == "next_business_hour":
        return now >= _next_business_start(router_cfg, created_at)
    if delivery == "regular_digest":
        return now >= _next_digest_at(router_cfg, created_at)
    return True


def flush_pending(cfg: dict | None = None, *, now: datetime | None = None, logfile: Path | None = None, queue_summary_log: Path | None = None) -> int:
    cfg = cfg or load_config()
    router_cfg = _router_cfg(cfg)
    current = _now_utc(now)
    path = incidents_path(cfg)
    incidents = _load_incidents(path)
    sent = 0
    changed = False
    for incident in incidents:
        if incident.get("resolved_at") or incident.get("deduped_to") or incident.get("notified_at"):
            continue
        if not _incident_due(router_cfg, incident, current):
            continue
        tier_cfg = dict(router_cfg["tiers"].get(incident.get("sev")) or {})
        if _send_incident(cfg, incident, tier_cfg, logfile=logfile, queue_summary_log=queue_summary_log):
            sent += 1
            changed = True
    if changed:
        _write_incidents(path, incidents)
    return sent


def escalate(
    severity: str,
    event: dict[str, Any],
    *,
    cfg: dict | None = None,
    now: datetime | None = None,
    logfile: Path | None = None,
    queue_summary_log: Path | None = None,
) -> dict[str, Any]:
    cfg = cfg or load_config()
    severity = str(severity or "").strip().lower()
    if severity not in VALID_SEVERITIES:
        raise ValueError(f"Unsupported incident severity: {severity}")
    current = _now_utc(now)
    flush_pending(cfg, now=current, logfile=logfile, queue_summary_log=queue_summary_log)
    router_cfg = _router_cfg(cfg)
    source = str(event.get("source") or "unknown").strip()
    tier_cfg = dict(router_cfg["tiers"][severity])
    event_key = _incident_key(source, event)
    path = incidents_path(cfg)
    incidents = _load_incidents(path)

    incident = {
        "id": uuid4().hex[:12],
        "created_at": current.isoformat(),
        "sev": severity,
        "source": source,
        "event_key": event_key,
        "event": deepcopy(event),
        "ack_at": None,
        "resolved_at": None,
        "route": tier_cfg.get("delivery"),
        "message_id": None,
        "notified_at": None,
    }

    window_minutes = int(tier_cfg.get("dedup_window_minutes", 0) or 0)
    if severity in {"sev2", "sev3"} and window_minutes > 0:
        for existing in reversed(incidents):
            if _record_matches_window(existing, source, event_key, severity, current, window_minutes):
                incident["deduped_to"] = existing["id"]
                break

    incidents.append(incident)
    force_notify = bool(event.get("force_notify"))
    if not incident.get("deduped_to") and (force_notify or _incident_due(router_cfg, incident, current)):
        _send_incident(cfg, incident, tier_cfg, logfile=logfile, queue_summary_log=queue_summary_log)
    _write_incidents(path, incidents)
    return incident


def update_incident_status(
    incident_id: str,
    *,
    action: str,
    cfg: dict | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    cfg = cfg or load_config()
    current = _now_utc(now).isoformat()
    path = incidents_path(cfg)
    incidents = _load_incidents(path)
    updated: dict[str, Any] | None = None
    for incident in incidents:
        if incident.get("id") != incident_id:
            continue
        incident["_already_acknowledged"] = bool(incident.get("ack_at"))
        incident["_already_resolved"] = bool(incident.get("resolved_at"))
        if action == "ack" and not incident.get("ack_at"):
            incident["ack_at"] = current
        elif action == "resolve":
            if not incident.get("ack_at"):
                incident["ack_at"] = current
            if not incident.get("resolved_at"):
                incident["resolved_at"] = current
        updated = incident
        break
    if updated is None:
        return None
    persisted = []
    for incident in incidents:
        clean = dict(incident)
        clean.pop("_already_acknowledged", None)
        clean.pop("_already_resolved", None)
        persisted.append(clean)
    _write_incidents(path, persisted)
    return updated
