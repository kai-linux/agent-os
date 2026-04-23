"""Per-agent monthly token/cost budget tracking and hard-stop enforcement.

Complements ``cost_tracker`` by writing per-task cost events that are fast to
aggregate per calendar-month-UTC, and exposing helpers the dispatch path uses
to filter agents that have exceeded their monthly hard-stop budget.

Design notes:
- Missing ``budgets`` config is treated as unlimited — dispatch never blocks
  on a missing config, and we log a one-time warning so operators notice.
- Soft-warn and hard-stop Telegram alerts fire at most once per
  (month, agent, threshold) by recording dedup state in ``budget_alerts.jsonl``.
- The calendar window is UTC so the reset boundary is stable across operators.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from orchestrator.cost_tracker import _attempt_cost

COST_EVENTS_FILENAME = "cost_events.jsonl"
BUDGET_ALERTS_FILENAME = "budget_alerts.jsonl"
_MISSING_WARNING_FIRED = {"once": False}


def current_month_key(now: datetime | None = None) -> str:
    """Return YYYY-MM in UTC for the given or current timestamp."""
    moment = now or datetime.now(tz=timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m")


def _metrics_dir(cfg: dict) -> Path:
    return Path(cfg.get("root_dir", ".")).expanduser() / "runtime" / "metrics"


def _budgets_cfg(cfg: dict | None) -> dict:
    return dict(((cfg or {}).get("budgets") or {}))


def budget_for_agent(cfg: dict, agent: str) -> dict | None:
    """Resolve per-agent budget entry, falling back to the ``default`` entry.

    Returns None when no budget applies to the agent. Unset thresholds are
    normalized to None so callers can distinguish "no soft-warn" from 0 USD.
    """
    section = _budgets_cfg(cfg)
    if not section:
        return None
    per_agent = section.get("per_agent") or {}
    entry = per_agent.get(agent)
    if entry is None:
        entry = section.get("default")
    if not entry:
        return None
    soft = entry.get("soft_warn_usd")
    hard = entry.get("hard_stop_usd")
    if soft is None and hard is None:
        return None
    return {
        "soft_warn_usd": float(soft) if soft is not None else None,
        "hard_stop_usd": float(hard) if hard is not None else None,
    }


def _iter_cost_events(path: Path, month_key: str):
    if not path.exists():
        return
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            rec_month = str(record.get("month_key") or "")
            if rec_month:
                if rec_month != month_key:
                    continue
            else:
                ts_raw = str(record.get("timestamp", ""))
                try:
                    ts_dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if ts_dt.tzinfo is None:
                    ts_dt = ts_dt.replace(tzinfo=timezone.utc)
                if ts_dt.astimezone(timezone.utc).strftime("%Y-%m") != month_key:
                    continue
            yield record


def monthly_spend_by_agent(cfg: dict, month_key: str | None = None) -> dict[str, float]:
    """Return {agent: usd_spent} aggregated from cost_events.jsonl for the month."""
    month_key = month_key or current_month_key()
    path = _metrics_dir(cfg) / COST_EVENTS_FILENAME
    totals: dict[str, float] = {}
    for rec in _iter_cost_events(path, month_key):
        agent = str(rec.get("agent") or "unknown")
        try:
            usd = float(rec.get("usd_estimate") or 0.0)
        except (TypeError, ValueError):
            usd = 0.0
        totals[agent] = round(totals.get(agent, 0.0) + usd, 6)
    return totals


def remaining_budget(cfg: dict, agent: str, *, month_key: str | None = None, spend: float | None = None) -> dict | None:
    """Return a structured remaining-budget record for dashboards, or None."""
    budget = budget_for_agent(cfg, agent)
    if budget is None:
        return None
    if spend is None:
        spend = monthly_spend_by_agent(cfg, month_key).get(agent, 0.0)
    hard = budget.get("hard_stop_usd")
    remaining = None if hard is None else max(0.0, round(float(hard) - float(spend), 6))
    return {
        "agent": agent,
        "month_key": month_key or current_month_key(),
        "spend_usd": round(float(spend), 6),
        "soft_warn_usd": budget.get("soft_warn_usd"),
        "hard_stop_usd": hard,
        "remaining_usd": remaining,
        "hard_stopped": bool(hard is not None and float(spend) >= float(hard)),
    }


def is_hard_stopped(cfg: dict, agent: str, *, month_key: str | None = None, spend: float | None = None) -> bool:
    record = remaining_budget(cfg, agent, month_key=month_key, spend=spend)
    return bool(record and record["hard_stopped"])


def filter_budget_compliant_agents(
    agents: list[str],
    cfg: dict,
    *,
    month_key: str | None = None,
) -> tuple[list[str], dict[str, dict]]:
    """Drop agents over their monthly hard-stop.

    Missing ``budgets`` config returns the input list unchanged so dispatch is
    never blocked by a missing config. The second return value maps each
    skipped agent to its remaining-budget record for logging/alerting.
    """
    section = _budgets_cfg(cfg)
    if not section:
        return list(agents), {}
    month_key = month_key or current_month_key()
    spend_by_agent = monthly_spend_by_agent(cfg, month_key)
    passing: list[str] = []
    skipped: dict[str, dict] = {}
    for agent in agents:
        record = remaining_budget(cfg, agent, month_key=month_key, spend=spend_by_agent.get(agent, 0.0))
        if record and record["hard_stopped"]:
            skipped[agent] = record
        else:
            passing.append(agent)
    return passing, skipped


def record_cost_events(
    cfg: dict,
    *,
    task_id: str,
    timestamp: str,
    github_repo: str | None,
    model_attempt_details: list[dict],
) -> Path | None:
    """Append one cost_events.jsonl entry per per-attempt cost for this task.

    Each entry captures the fields required for monthly aggregation:
    agent, task_id, input_tokens, output_tokens, usd_estimate, timestamp.
    Returns the path written, or None when there is nothing to write.
    """
    if not model_attempt_details:
        return None
    metrics_dir = _metrics_dir(cfg)
    metrics_dir.mkdir(parents=True, exist_ok=True)
    path = metrics_dir / COST_EVENTS_FILENAME
    month_key = _month_key_from_timestamp(timestamp)
    lines: list[str] = []
    for attempt in model_attempt_details:
        costed = _attempt_cost(attempt, cfg)
        payload = {
            "timestamp": timestamp,
            "month_key": month_key,
            "task_id": task_id,
            "github_repo": github_repo or "",
            "agent": costed.get("agent", "unknown"),
            "provider": costed.get("provider", "unknown"),
            "model": costed.get("model", "unknown"),
            "attempt": costed.get("attempt", 0),
            "input_tokens": int(attempt.get("input_tokens_estimate") or 0),
            "output_tokens": int(attempt.get("output_tokens_estimate") or 0),
            "usd_estimate": round(float(costed.get("cost_usd", 0.0)), 6),
            "status": costed.get("status", "unknown"),
            "blocker_code": costed.get("blocker_code", "none") or "none",
        }
        lines.append(json.dumps(payload, sort_keys=True) + "\n")
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    fd, tmp_path = tempfile.mkstemp(dir=metrics_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(existing + "".join(lines))
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return path


def _month_key_from_timestamp(timestamp: str) -> str:
    try:
        ts_dt = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return current_month_key()
    if ts_dt.tzinfo is None:
        ts_dt = ts_dt.replace(tzinfo=timezone.utc)
    return ts_dt.astimezone(timezone.utc).strftime("%Y-%m")


def _alert_state_path(cfg: dict) -> Path:
    return _metrics_dir(cfg) / BUDGET_ALERTS_FILENAME


def _load_fired_alert_keys(cfg: dict) -> set[str]:
    path = _alert_state_path(cfg)
    if not path.exists():
        return set()
    keys: set[str] = set()
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                key = rec.get("key")
                if key:
                    keys.add(str(key))
    except OSError:
        return set()
    return keys


def _record_alert_fired(cfg: dict, key: str, payload: dict) -> None:
    metrics_dir = _metrics_dir(cfg)
    metrics_dir.mkdir(parents=True, exist_ok=True)
    record = {"key": key, "fired_at": datetime.now(tz=timezone.utc).isoformat(), **payload}
    with (metrics_dir / BUDGET_ALERTS_FILENAME).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def check_budget_alerts(
    cfg: dict,
    send_telegram_fn: Callable[[dict, str], object] | None,
    *,
    month_key: str | None = None,
    logger: Callable[[str], None] | None = None,
) -> list[dict]:
    """Fire soft-warn and hard-stop Telegram alerts once per (month, agent, threshold).

    ``send_telegram_fn`` is dependency-injected so tests can assert delivery
    without reaching into queue.send_telegram's network path.
    """
    section = _budgets_cfg(cfg)
    if not section:
        return []
    month_key = month_key or current_month_key()
    spend_by_agent = monthly_spend_by_agent(cfg, month_key)
    fired_keys = _load_fired_alert_keys(cfg)
    fired: list[dict] = []
    for agent, spend in sorted(spend_by_agent.items()):
        budget = budget_for_agent(cfg, agent)
        if not budget:
            continue
        for threshold_name, limit in (
            ("soft_warn", budget.get("soft_warn_usd")),
            ("hard_stop", budget.get("hard_stop_usd")),
        ):
            if limit is None:
                continue
            if float(spend) < float(limit):
                continue
            key = f"{month_key}:{agent}:{threshold_name}"
            if key in fired_keys:
                continue
            hard_limit = budget.get("hard_stop_usd")
            if threshold_name == "hard_stop":
                text = (
                    "🛑 Budget hard-stop\n"
                    f"Agent: {agent}\n"
                    f"Month: {month_key}\n"
                    f"Spend: ${float(spend):.2f} / ${float(limit):.2f}\n"
                    "Agent removed from healthy-agent selection for the rest of the calendar month."
                )
            else:
                hard_line = (
                    f"Hard-stop at ${float(hard_limit):.2f} will auto-pause dispatch to this agent."
                    if hard_limit is not None
                    else "No hard-stop configured; monitor manually."
                )
                text = (
                    "⚠️ Budget soft-warn\n"
                    f"Agent: {agent}\n"
                    f"Month: {month_key}\n"
                    f"Spend: ${float(spend):.2f} / soft-warn ${float(limit):.2f}\n"
                    + hard_line
                )
            if send_telegram_fn is not None:
                try:
                    send_telegram_fn(cfg, text)
                except Exception as exc:  # pragma: no cover — network best-effort
                    if logger:
                        logger(f"Budget alert send failed: {exc}")
            _record_alert_fired(cfg, key, {
                "agent": agent,
                "month_key": month_key,
                "threshold": threshold_name,
                "spend_usd": round(float(spend), 6),
                "limit_usd": float(limit),
            })
            fired_keys.add(key)
            fired.append({
                "agent": agent,
                "threshold": threshold_name,
                "spend_usd": round(float(spend), 6),
                "limit_usd": float(limit),
            })
    return fired


def warn_if_budgets_missing(cfg: dict, logger: Callable[[str], None] | None = None) -> None:
    """Log a one-time warning when no `budgets` section is configured."""
    if _MISSING_WARNING_FIRED["once"]:
        return
    if _budgets_cfg(cfg):
        return
    if logger:
        logger(
            "Budget enforcement disabled: no `budgets` config section defined; "
            "treating all agents as unlimited."
        )
    _MISSING_WARNING_FIRED["once"] = True


def reset_missing_warning_for_tests() -> None:
    """Test helper — resets the one-shot missing-config warning latch."""
    _MISSING_WARNING_FIRED["once"] = False


def budget_snapshot(cfg: dict, *, month_key: str | None = None, agents: list[str] | None = None) -> dict:
    """Dashboard-facing snapshot of monthly spend and remaining budget per agent."""
    month_key = month_key or current_month_key()
    spend_by_agent = monthly_spend_by_agent(cfg, month_key)
    section = _budgets_cfg(cfg)
    known_agents = set(spend_by_agent.keys())
    if agents:
        known_agents.update(agents)
    known_agents.update((section.get("per_agent") or {}).keys())
    per_agent: list[dict] = []
    for agent in sorted(known_agents):
        spend = spend_by_agent.get(agent, 0.0)
        record = remaining_budget(cfg, agent, month_key=month_key, spend=spend)
        if record is None:
            per_agent.append({
                "agent": agent,
                "month_key": month_key,
                "spend_usd": round(float(spend), 6),
                "soft_warn_usd": None,
                "hard_stop_usd": None,
                "remaining_usd": None,
                "hard_stopped": False,
            })
        else:
            per_agent.append(record)
    return {
        "month_key": month_key,
        "enabled": bool(section),
        "per_agent": per_agent,
        "total_spend_usd": round(sum(spend_by_agent.values()), 6),
    }
