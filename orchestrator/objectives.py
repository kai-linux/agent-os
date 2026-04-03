from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml


DEFAULT_INTERPRETATION_SCORES = {
    "improved": 1.0,
    "unchanged": 0.0,
    "regressed": -1.0,
    "inconclusive": -0.35,
}


def _slug_variants(github_slug: str, repo_path: Path) -> list[str]:
    repo_name = repo_path.name.strip()
    variants: list[str] = []
    for value in [repo_name]:
        value = str(value or "").strip()
        if value and value not in variants:
            variants.append(value)
    return variants


def _objective_candidates(cfg: dict, github_slug: str, repo_path: Path) -> list[Path]:
    candidates: list[Path] = []
    repo_objectives = cfg.get("repo_objectives") or {}
    mapped = repo_objectives.get(github_slug) or repo_objectives.get(repo_path.name)
    if mapped:
        candidates.append(Path(str(mapped)).expanduser())

    objectives_dir = Path(str(cfg.get("objectives_dir") or "")).expanduser()
    if objectives_dir:
        for variant in _slug_variants(github_slug, repo_path):
            candidates.append(objectives_dir / f"{variant}.yaml")
            candidates.append(objectives_dir / f"{variant}.yml")
    return candidates


def load_repo_objective(cfg: dict, github_slug: str, repo_path: Path) -> dict:
    for candidate in _objective_candidates(cfg, github_slug, repo_path):
        if not candidate.exists():
            continue
        with candidate.open("r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        if isinstance(data, dict):
            payload = dict(data)
            payload["_objective_path"] = str(candidate)
            return payload
    return {}


def objective_metrics(objective: dict) -> list[dict]:
    metrics = objective.get("metrics")
    if not isinstance(metrics, list):
        return []
    return [metric for metric in metrics if isinstance(metric, dict) and str(metric.get("id", "")).strip()]


def objective_feedback_inputs(objective: dict) -> list[dict]:
    inputs: list[dict] = []
    for metric in objective_metrics(objective):
        source = metric.get("source")
        if not isinstance(source, dict):
            continue
        source_type = str(source.get("type") or "").strip().lower()
        if source_type not in {"web", "file"}:
            continue
        payload = dict(source)
        payload.setdefault("name", str(metric.get("name") or metric.get("id") or "Objective metric").strip())
        payload.setdefault("signal_class", str(source.get("signal_class") or "analytics").strip().lower())
        payload.setdefault("provenance", f"Objective metric `{metric.get('id')}` from external objective config")
        payload.setdefault(
            "trust_note",
            f"Primary business metric for objective weighting ({metric.get('direction', 'increase')}).",
        )
        payload.setdefault("privacy", "internal")
        payload.setdefault("privacy_note", "Business metric snapshot sourced from external evidence storage.")
        payload.setdefault("metric_id", str(metric.get("id")).strip())
        inputs.append(payload)
    return inputs


def objective_outcome_checks(objective: dict) -> list[dict]:
    checks: list[dict] = []
    for metric in objective_metrics(objective):
        outcome_check = metric.get("outcome_check")
        if not isinstance(outcome_check, dict):
            continue
        source = metric.get("source") if isinstance(metric.get("source"), dict) else {}
        source_type = str(outcome_check.get("type") or source.get("type") or "").strip().lower()
        if source_type not in {"web", "file"}:
            continue
        payload = dict(outcome_check)
        payload.setdefault("id", str(metric.get("id")).strip())
        payload.setdefault("name", str(metric.get("name") or metric.get("id") or "Objective metric").strip())
        payload.setdefault("type", source_type)
        if source.get("url") and not payload.get("url"):
            payload["url"] = source["url"]
        if source.get("path") and not payload.get("path"):
            payload["path"] = source["path"]
        payload.setdefault(
            "comparison_window",
            f"Compare the latest business metric snapshot for `{metric.get('id')}` against the prior measurement window.",
        )
        payload.setdefault("measurement_window_days", int(objective.get("evaluation_window_days", 28)))
        checks.append(payload)
    return checks


def objective_score_window_days(objective: dict) -> int:
    return max(1, int(objective.get("evaluation_window_days", 28)))


def objective_interpretation_scores(objective: dict) -> dict[str, float]:
    scores = dict(DEFAULT_INTERPRETATION_SCORES)
    configured = objective.get("interpretation_scores")
    if isinstance(configured, dict):
        for key, value in configured.items():
            if key in scores:
                try:
                    scores[key] = float(value)
                except (TypeError, ValueError):
                    continue
    return scores


def objective_metric_weights(objective: dict) -> dict[str, float]:
    metrics = objective_metrics(objective)
    if not metrics:
        return {}
    raw = {}
    for metric in metrics:
        metric_id = str(metric.get("id")).strip()
        try:
            raw[metric_id] = max(0.0, float(metric.get("weight", 1.0)))
        except (TypeError, ValueError):
            raw[metric_id] = 1.0
    total = sum(raw.values())
    if total <= 0:
        even = 1.0 / len(raw)
        return {key: even for key in raw}
    return {key: value / total for key, value in raw.items()}


def build_objective_scorecard_section(objective: dict, snapshots: list[dict]) -> dict | None:
    if not objective:
        return None
    score = score_objective_snapshots(objective, snapshots)
    metric_names = {
        str(metric.get("id")).strip(): str(metric.get("name") or metric.get("id")).strip()
        for metric in objective_metrics(objective)
    }
    score_label = f"{score['score']:+.2f}"
    primary = str(objective.get("primary_outcome") or objective.get("north_star") or "business growth").strip()
    evidence = [
        f"Weighted objective score over last {score['window_days']}d: {score_label}",
        f"Improved snapshots: {score['counts']['improved']}",
        f"Regressed snapshots: {score['counts']['regressed']}",
        f"Inconclusive snapshots: {score['counts']['inconclusive']}",
    ]
    for metric_id, component in score["components"][:4]:
        evidence.append(
            f"{metric_names.get(metric_id, metric_id)}: {component['score']:+.2f} from {component['count']} snapshot(s)"
        )

    if score["score"] < 0:
        implications = [
            f"Bias the sprint toward work expected to improve `{primary}` rather than repo-local throughput.",
            "Prefer experiments and fixes attached to measurable business metrics over internal polish.",
        ]
    else:
        implications = [
            f"Continue compounding work that improves `{primary}` while avoiding unmeasured backlog churn.",
            "Attach outcome checks to shipped work so future scoring stays tied to business metrics.",
        ]

    return {
        "name": "Business Objective Scorecard",
        "signal_class": "business_objective",
        "location": objective.get("_objective_path", "(external objective config)"),
        "observed_at": score["latest_observed_at"],
        "freshness": score["freshness"],
        "freshness_policy": f"rolling {score['window_days']}d weighted business outcome window",
        "provenance": "external repo objective config and outcome attribution snapshots",
        "trust_note": "Objective weights and business metrics are defined outside repo-controlled state.",
        "privacy_note": "Derived business-metric summary only; raw analytics credentials stay external.",
        "trust_level": "high",
        "privacy_level": "internal",
        "planning_use": "included",
        "summary": (
            f"The current repo objective is `{primary}`. Recent outcome snapshots produce a weighted business score "
            f"of {score_label}, summarizing whether shipped work moved the prioritized product metrics."
        ),
        "key_evidence": evidence,
        "planning_implications": implications,
        "guardrail_note": "included in planning context",
    }


def score_objective_snapshots(objective: dict, snapshots: list[dict]) -> dict:
    window_days = objective_score_window_days(objective)
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=window_days)
    weights = objective_metric_weights(objective)
    scores = objective_interpretation_scores(objective)
    components: dict[str, dict] = {}
    counts = {key: 0 for key in DEFAULT_INTERPRETATION_SCORES}
    latest_observed_at: datetime | None = None

    for record in snapshots:
        if record.get("record_type") != "snapshot":
            continue
        raw_timestamp = str(record.get("timestamp") or "").strip()
        if raw_timestamp.endswith("Z"):
            raw_timestamp = raw_timestamp[:-1] + "+00:00"
        try:
            observed_at = datetime.fromisoformat(raw_timestamp)
        except ValueError:
            continue
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)
        observed_at = observed_at.astimezone(timezone.utc)
        if observed_at < cutoff:
            continue
        interpretation = str(record.get("interpretation") or "inconclusive").strip().lower()
        if interpretation not in scores:
            interpretation = "inconclusive"
        counts[interpretation] = counts.get(interpretation, 0) + 1
        if latest_observed_at is None or observed_at > latest_observed_at:
            latest_observed_at = observed_at
        check_id = str(record.get("check_id") or "").strip().lower()
        if not check_id or check_id not in weights:
            continue
        component = components.setdefault(check_id, {"sum": 0.0, "count": 0})
        component["sum"] += scores[interpretation]
        component["count"] += 1

    weighted_score = 0.0
    ranked_components: list[tuple[str, dict]] = []
    for metric_id, weight in weights.items():
        component = components.get(metric_id, {"sum": 0.0, "count": 0})
        avg = component["sum"] / component["count"] if component["count"] else scores["inconclusive"]
        weighted_score += weight * avg
        ranked_components.append((metric_id, {"score": avg, "count": component["count"], "weight": weight}))
    ranked_components.sort(key=lambda item: item[1]["score"])

    freshness = "unknown"
    latest_text = "unspecified"
    if latest_observed_at is not None:
        age_hours = max(0.0, (datetime.now(tz=timezone.utc) - latest_observed_at).total_seconds() / 3600.0)
        if age_hours < 1:
            freshness = "<1h old"
        elif age_hours < 24:
            freshness = f"{round(age_hours)}h old"
        else:
            freshness = f"{round(age_hours / 24, 1):g}d old"
        latest_text = latest_observed_at.strftime("%Y-%m-%d %H:%M UTC")

    return {
        "score": weighted_score,
        "counts": counts,
        "components": ranked_components,
        "window_days": window_days,
        "latest_observed_at": latest_text,
        "freshness": freshness,
    }


def format_objective_for_prompt(objective: dict, max_chars: int = 2000) -> str:
    """Format the repo objective as a readable prompt section.

    Returns a human-readable summary of the primary outcome, metrics,
    and their weights so LLM consumers (planner, groomer) understand
    what the repo is optimizing for.
    """
    if not objective:
        return "(no objective configured for this repo)"

    lines: list[str] = []
    primary = str(objective.get("primary_outcome") or "").strip()
    if primary:
        lines.append(f"Primary outcome: {primary}")

    eval_window = objective.get("evaluation_window_days")
    if eval_window:
        lines.append(f"Evaluation window: {eval_window} days")

    metrics = objective_metrics(objective)
    if metrics:
        lines.append("")
        lines.append("Tracked metrics (what the system is measured on):")
        weights = objective_metric_weights(objective)
        for m in metrics:
            mid = str(m.get("id", "")).strip()
            name = str(m.get("name") or mid).strip()
            direction = str(m.get("direction") or "increase").strip()
            weight_pct = weights.get(mid, 0) * 100
            lines.append(f"  - {name} (id={mid}, direction={direction}, weight={weight_pct:.0f}%)")

    interp = objective.get("interpretation_scores")
    if isinstance(interp, dict):
        lines.append("")
        lines.append("Scoring: " + ", ".join(f"{k}={v}" for k, v in interp.items()))

    result = "\n".join(lines)
    return result[:max_chars] if len(result) > max_chars else result
