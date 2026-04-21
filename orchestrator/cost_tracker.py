"""Approximate API cost tracking from agent_stats.jsonl.

Reads task-level metrics plus per-attempt metadata from runtime/metrics/agent_stats.jsonl
and rewrites runtime/metrics/cost_records.jsonl with task cost records and repo/global
summary rows. Prices are intentionally hard-coded and easy to update.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from orchestrator.paths import load_config


COST_RECORDS_FILENAME = "cost_records.jsonl"
AGENT_STATS_FILENAME = "agent_stats.jsonl"
TOKEN_ESTIMATION_METHOD = "chars_div_4"
PRICING_VERSION = "foundation_2026_04"

# Approximate baseline prices in USD per 1M tokens.
# Update this table when provider pricing changes.
PRICING_CATALOG = {
    "claude-sonnet-4": {"provider": "anthropic", "input_per_million_tokens": 3.00, "output_per_million_tokens": 15.00},
    "claude-opus-4": {"provider": "anthropic", "input_per_million_tokens": 15.00, "output_per_million_tokens": 75.00},
    "gemini-2.5-flash": {"provider": "google", "input_per_million_tokens": 0.30, "output_per_million_tokens": 2.50},
    "deepseek/deepseek-v3.2": {"provider": "deepseek", "input_per_million_tokens": 0.27, "output_per_million_tokens": 1.10},
    "codex": {"provider": "openai", "input_per_million_tokens": 15.00, "output_per_million_tokens": 60.00},
}

MODEL_ALIASES = {
    "claude": "claude-sonnet-4",
    "sonnet": "claude-sonnet-4",
    "claude-sonnet-4": "claude-sonnet-4",
    "opus": "claude-opus-4",
    "claude-opus-4": "claude-opus-4",
    "gemini": "gemini-2.5-flash",
    "gemini-2.5-flash": "gemini-2.5-flash",
    "deepseek": "deepseek/deepseek-v3.2",
    "deepseek/deepseek-v3.2": "deepseek/deepseek-v3.2",
    "codex": "codex",
}

DEFAULT_AGENT_MODELS = {
    "claude": "claude-sonnet-4",
    "opus": "claude-opus-4",
    "gemini": "gemini-2.5-flash",
    "deepseek": "deepseek/deepseek-v3.2",
    "codex": "codex",
}

DEFAULT_AGENT_PROVIDERS = {
    "claude": "anthropic",
    "opus": "anthropic",
    "gemini": "google",
    "deepseek": "deepseek",
    "codex": "openai",
}


def estimate_text_tokens(text: str) -> int:
    """Very rough token estimate for governance-grade spend tracking."""
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def _cost_tracking_cfg(cfg: dict | None) -> dict:
    section = ((cfg or {}).get("cost_tracking") or {}).copy()
    agent_models = {**DEFAULT_AGENT_MODELS, **(section.get("agent_models") or {})}
    provider_multipliers = {"anthropic": 1.0, "google": 1.0, "deepseek": 1.0, "openai": 1.0}
    provider_multipliers.update(section.get("provider_multipliers") or {})
    section["agent_models"] = agent_models
    section["provider_multipliers"] = provider_multipliers
    section.setdefault("default_price_multiplier", 1.0)
    section.setdefault("model_overrides", {})
    return section


def resolve_attempt_model(agent: str, cfg: dict | None) -> str:
    section = _cost_tracking_cfg(cfg)
    if agent == "gemini":
        return str(os.environ.get("GEMINI_MODEL", "")).strip() or str(section["agent_models"].get(agent, DEFAULT_AGENT_MODELS["gemini"]))
    if agent == "deepseek":
        return str(os.environ.get("DEEPSEEK_OPENROUTER_MODEL", "")).strip() or str(section["agent_models"].get(agent, DEFAULT_AGENT_MODELS["deepseek"]))
    return str(section["agent_models"].get(agent, agent or "unknown")).strip() or "unknown"


def resolve_attempt_provider(agent: str, cfg: dict | None) -> str:
    section = _cost_tracking_cfg(cfg)
    provider_overrides = section.get("provider_overrides") or {}
    return str(provider_overrides.get(agent) or DEFAULT_AGENT_PROVIDERS.get(agent) or "unknown")


def _canonical_model_name(model: str | None, agent: str | None, cfg: dict | None) -> str:
    raw = str(model or "").strip().lower()
    if raw:
        return MODEL_ALIASES.get(raw, raw)
    return MODEL_ALIASES.get(str(agent or "").strip().lower(), str(agent or "unknown").strip().lower())


def _pricing_for_attempt(attempt: dict, cfg: dict) -> tuple[str, dict]:
    section = _cost_tracking_cfg(cfg)
    canonical_model = _canonical_model_name(attempt.get("model"), attempt.get("agent"), cfg)
    pricing = dict(PRICING_CATALOG.get(canonical_model, PRICING_CATALOG.get("codex")))
    override = (section.get("model_overrides") or {}).get(canonical_model) or {}
    pricing.update(override)
    pricing["provider"] = str(override.get("provider") or pricing.get("provider") or attempt.get("provider") or "unknown")
    return canonical_model, pricing


def _attempt_cost(attempt: dict, cfg: dict) -> dict:
    section = _cost_tracking_cfg(cfg)
    canonical_model, pricing = _pricing_for_attempt(attempt, cfg)
    provider = str(attempt.get("provider") or pricing.get("provider") or "unknown")
    input_tokens = int(attempt.get("input_tokens_estimate") or 0)
    output_tokens = int(attempt.get("output_tokens_estimate") or 0)
    provider_multiplier = float(section["provider_multipliers"].get(provider, 1.0))
    default_multiplier = float(section.get("default_price_multiplier", 1.0))
    input_rate = float(pricing["input_per_million_tokens"])
    output_rate = float(pricing["output_per_million_tokens"])
    cost_usd = (
        (input_tokens / 1_000_000.0) * input_rate +
        (output_tokens / 1_000_000.0) * output_rate
    ) * provider_multiplier * default_multiplier
    return {
        "attempt": int(attempt.get("attempt") or 0),
        "agent": attempt.get("agent", "unknown"),
        "provider": provider,
        "model": canonical_model,
        "status": attempt.get("status", "unknown"),
        "blocker_code": attempt.get("blocker_code", "none") or "none",
        "input_tokens_estimate": input_tokens,
        "output_tokens_estimate": output_tokens,
        "cost_usd": round(cost_usd, 6),
    }


def _load_agent_stats(metrics_dir: Path) -> list[dict]:
    stats_path = metrics_dir / AGENT_STATS_FILENAME
    if not stats_path.exists():
        return []
    records: list[dict] = []
    with stats_path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            records.append(record)
    records.sort(key=lambda record: str(record.get("timestamp", "")))
    return records


def build_cost_records(agent_stats: list[dict], cfg: dict) -> list[dict]:
    task_records: list[dict] = []
    repo_totals: dict[str, float] = {}
    repo_attempts: dict[str, int] = {}
    repo_tasks: dict[str, int] = {}
    global_total = 0.0

    for stat in agent_stats:
        attempts = list(stat.get("model_attempt_details") or [])
        costed_attempts = [_attempt_cost(attempt, cfg) for attempt in attempts]
        task_cost = round(sum(item["cost_usd"] for item in costed_attempts), 6)
        repo_key = str(stat.get("github_repo") or stat.get("repo") or "unknown")
        repo_totals[repo_key] = round(repo_totals.get(repo_key, 0.0) + task_cost, 6)
        repo_attempts[repo_key] = repo_attempts.get(repo_key, 0) + len(costed_attempts)
        repo_tasks[repo_key] = repo_tasks.get(repo_key, 0) + 1
        global_total = round(global_total + task_cost, 6)
        task_records.append({
            "record_type": "task_cost",
            "pricing_version": PRICING_VERSION,
            "token_estimation_method": TOKEN_ESTIMATION_METHOD,
            "timestamp": stat.get("timestamp"),
            "task_id": stat.get("task_id", "unknown"),
            "repo": repo_key,
            "status": stat.get("status", "unknown"),
            "task_type": stat.get("task_type", "unknown"),
            "attempt_count": len(costed_attempts),
            "task_cost_usd": task_cost,
            "repo_cumulative_cost_usd": repo_totals[repo_key],
            "global_cumulative_cost_usd": global_total,
            "attempt_costs": costed_attempts,
        })

    summary_records = [
        {
            "record_type": "repo_summary",
            "pricing_version": PRICING_VERSION,
            "repo": repo,
            "task_count": repo_tasks.get(repo, 0),
            "attempt_count": repo_attempts.get(repo, 0),
            "total_cost_usd": round(total, 6),
        }
        for repo, total in sorted(repo_totals.items())
    ]
    summary_records.append({
        "record_type": "global_summary",
        "pricing_version": PRICING_VERSION,
        "repo_count": len(repo_totals),
        "task_count": len(task_records),
        "total_cost_usd": round(global_total, 6),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    })
    return task_records + summary_records


def write_cost_records(metrics_dir: Path, records: list[dict]) -> Path:
    metrics_dir.mkdir(parents=True, exist_ok=True)
    output_path = metrics_dir / COST_RECORDS_FILENAME
    payload = "".join(json.dumps(record, sort_keys=True) + "\n" for record in records)
    fd, tmp_path = tempfile.mkstemp(dir=metrics_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp_path, output_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return output_path


def rebuild_cost_records(cfg: dict) -> Path:
    root = Path(cfg.get("root_dir", ".")).expanduser()
    metrics_dir = root / "runtime" / "metrics"
    records = build_cost_records(_load_agent_stats(metrics_dir), cfg)
    return write_cost_records(metrics_dir, records)


def main() -> int:
    cfg = load_config()
    output_path = rebuild_cost_records(cfg)
    print(f"Wrote {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
