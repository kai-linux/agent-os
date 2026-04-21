"""System architect for target-model capability and sensor gap detection."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

from orchestrator.objectives import load_repo_objective, objective_metrics
from orchestrator.scheduler_state import is_due, record_run


DEFAULT_CADENCE_DAYS = 30.0
JOB_NAME = "system_architect"
TARGET_MODEL_FILENAME = "target_operating_model.yaml"
REPORT_FILENAME = "system_architect_report.json"
_SPECIFIC_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.:/-]{2,}")
_METRIC_FILE_RE = re.compile(r'["\']([A-Za-z0-9_-]+\.(?:jsonl|json))["\']')


def system_architect_report_path(cfg: dict) -> Path:
    root = Path(cfg.get("root_dir", ".")).expanduser()
    return root / "runtime" / "metrics" / REPORT_FILENAME


def resolve_system_architect_config(cfg: dict, github_slug: str | None = None) -> dict:
    merged = dict(cfg.get("system_architect") or {})
    merged.setdefault("enabled", True)
    merged.setdefault("cadence_days", DEFAULT_CADENCE_DAYS)
    merged.setdefault("target_model", TARGET_MODEL_FILENAME)

    if github_slug:
        for project_cfg in (cfg.get("github_projects") or {}).values():
            if not isinstance(project_cfg, dict):
                continue
            for repo_cfg in project_cfg.get("repos", []) or []:
                if repo_cfg.get("github_repo") != github_slug:
                    continue
                override = repo_cfg.get("system_architect")
                if isinstance(override, dict):
                    updated = dict(merged)
                    updated.update(override)
                    merged = updated
                return merged
    return merged


def target_operating_model_path(cfg: dict, github_slug: str | None = None) -> Path:
    config = resolve_system_architect_config(cfg, github_slug)
    target = str(config.get("target_model") or TARGET_MODEL_FILENAME).strip() or TARGET_MODEL_FILENAME
    path = Path(target).expanduser()
    if path.is_absolute():
        return path
    root = Path(cfg.get("root_dir", ".")).expanduser()
    return root / path


def _default_repo_slug(cfg: dict) -> str:
    repos = configured_repos(cfg)
    if repos:
        return repos[0][0]
    explicit = str(cfg.get("github_repo") or "").strip()
    if explicit:
        return explicit
    return Path(cfg.get("root_dir", ".")).expanduser().name


def configured_repos(cfg: dict) -> list[tuple[str, Path]]:
    repos: list[tuple[str, Path]] = []
    seen: set[tuple[str, str]] = set()
    for project_cfg in cfg.get("github_projects", {}).values():
        if not isinstance(project_cfg, dict):
            continue
        for repo_cfg in project_cfg.get("repos", []):
            if not isinstance(repo_cfg, dict):
                continue
            github_repo = str(repo_cfg.get("github_repo") or "").strip()
            local_repo = str(repo_cfg.get("path") or repo_cfg.get("local_repo") or "").strip()
            if not github_repo or not local_repo:
                continue
            key = (github_repo, local_repo)
            if key in seen:
                continue
            seen.add(key)
            repos.append((github_repo, Path(local_repo).expanduser()))
    return repos


def _normalize_name(value: object) -> str:
    return str(value or "").strip()


def _stable_slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "unknown"


def _specific_name(value: object) -> str | None:
    text = _normalize_name(value)
    if not text or not _SPECIFIC_NAME_RE.search(text):
        return None
    return text


def _normalize_named_list(values: object) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values or []:
        name = _specific_name(raw)
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def load_target_operating_model(cfg: dict, github_slug: str | None = None) -> tuple[dict, Path]:
    path = target_operating_model_path(cfg, github_slug)
    if not path.exists():
        raise FileNotFoundError(
            f"system architect requires {path.name}; expected at {path}"
        )
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid target operating model at {path}: expected mapping")
    return payload, path


def _discover_roles(root: Path) -> list[str]:
    orchestrator_dir = root / "orchestrator"
    if not orchestrator_dir.exists():
        return []
    excluded = {"__init__", "paths"}
    roles = [
        path.stem
        for path in sorted(orchestrator_dir.glob("*.py"))
        if path.stem not in excluded
    ]
    return _normalize_named_list(roles)


def _discover_jobs(root: Path) -> list[str]:
    jobs: list[str] = []
    for path in sorted((root / "orchestrator").glob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "def run(" in text or "run_" in path.stem:
            jobs.append(path.stem)
    return _normalize_named_list(jobs)


def _discover_agents(cfg: dict) -> list[str]:
    names: list[str] = []
    if cfg.get("default_agent"):
        names.append(str(cfg.get("default_agent")))
    for name in cfg.get("planner_agents") or []:
        names.append(str(name))
    for values in (cfg.get("agent_fallbacks") or {}).values():
        for name in values or []:
            names.append(str(name))
    return _normalize_named_list(names)


def _discover_metric_schemas(root: Path) -> list[str]:
    files: set[str] = set()
    for path in sorted((root / "orchestrator").glob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in _METRIC_FILE_RE.findall(text):
            if match.endswith((".jsonl", ".json")):
                files.add(match)
    files.add(REPORT_FILENAME)
    return sorted(files)


def _discover_signal_channels(cfg: dict, root: Path) -> list[str]:
    signals: set[str] = set()
    for repo_slug, repo_path in configured_repos(cfg):
        objective = load_repo_objective(cfg, repo_slug, repo_path)
        for metric in objective_metrics(objective or {}):
            metric_id = _specific_name(metric.get("id"))
            if metric_id:
                signals.add(metric_id)

    for section, key in (
        ("external_signals", "sources"),
        ("production_feedback", "inputs"),
        ("product_inspection", "targets"),
    ):
        for item in (cfg.get(section) or {}).get(key, []) or []:
            if not isinstance(item, dict):
                continue
            name = _specific_name(item.get("name") or item.get("signal_class") or item.get("type"))
            if name:
                signals.add(name)

    metrics_dir = root / "runtime" / "metrics"
    if metrics_dir.exists():
        for path in sorted(metrics_dir.iterdir()):
            if path.is_file() and path.suffix in {".json", ".jsonl"}:
                signals.add(path.stem)

    for schema in _discover_metric_schemas(root):
        signals.add(Path(schema).stem)

    return sorted(signals)


def enumerate_current_state(cfg: dict) -> dict:
    root = Path(cfg.get("root_dir", ".")).expanduser()
    return {
        "capabilities": {
            "roles": _discover_roles(root),
            "jobs": _discover_jobs(root),
            "agents": _discover_agents(cfg),
        },
        "sensors": {
            "signals": _discover_signal_channels(cfg, root),
            "schemas": _discover_metric_schemas(root),
        },
    }


def _accepted_omission_keys(model: dict) -> set[str]:
    accepted: set[str] = set()
    for item in model.get("accepted_omissions") or []:
        if isinstance(item, str):
            accepted.add(item.strip().lower())
            continue
        if not isinstance(item, dict):
            continue
        kind = _normalize_name(item.get("kind")).lower()
        detail_type = _normalize_name(item.get("detail_type")).lower()
        name = _normalize_name(item.get("name")).lower()
        if kind and detail_type and name:
            accepted.add(f"{kind}:{detail_type}:{name}")
    return accepted


def _target_items(model: dict, section: str, detail_type: str) -> list[str]:
    section_data = model.get(section) or {}
    if not isinstance(section_data, dict):
        return []
    return _normalize_named_list(section_data.get(detail_type))


def _build_gap_finding(
    *,
    repo: str,
    kind: str,
    detail_type: str,
    name: str,
    target_path: Path,
) -> dict:
    repo_name = repo.rsplit("/", 1)[-1]
    if kind == "capability_gap":
        title = f"Add missing {detail_type} `{name}` to agent-os"
        goal = (
            f"Close the operating-model capability gap for `{name}` by adding the missing "
            f"{detail_type} to agent-os or wiring the existing implementation into the active operating model."
        )
        success_criteria = [
            f"`target_operating_model.yaml` entry `{name}` is satisfied by a concrete {detail_type}.",
            f"The {detail_type} is named explicitly in code or config rather than only implied in prose.",
            "The system architect no longer emits this capability gap on the next refresh.",
        ]
        constraints = [
            "Keep the scope to this named capability only; do not bundle unrelated architecture work.",
            "Prefer minimal diffs and reuse existing orchestrator patterns.",
            "If the gap should remain intentionally absent, mark it as accepted_omission instead of adding vague placeholders.",
        ]
        summary = (
            f"The curated operating model expects the {detail_type} `{name}`, but the current agent-os "
            f"state does not expose that capability in the orchestrator/control-plane surface."
        )
    else:
        title = f"Add missing {detail_type} `{name}` to {repo_name}"
        goal = (
            f"Close the operating-model sensor gap for `{name}` by wiring the missing {detail_type} "
            "into the existing measurement/feedback pipeline."
        )
        success_criteria = [
            f"`target_operating_model.yaml` entry `{name}` is backed by a concrete input or schema.",
            "The new sensor flows through the existing scorer/groomer evidence path instead of a parallel detector.",
            "The system architect no longer emits this sensor gap on the next refresh.",
        ]
        constraints = [
            "Propose the missing sensor or schema only; do not duplicate existing detectors.",
            "Keep the signal specific and named, not a generic request for more metrics.",
            "If the gap is intentionally omitted, mark it as accepted_omission instead of suppressing it silently.",
        ]
        summary = (
            f"The curated operating model expects the {detail_type} `{name}`, but the current agent-os "
            "state does not expose a matching sensor/signal/schema."
        )

    return {
        "id": f"{kind}:{detail_type}:{_stable_slug(name)}",
        "source": "system_architect",
        "kind": kind,
        "repo": repo,
        "detail_type": detail_type,
        "name": name,
        "title_hint": title,
        "summary": summary,
        "goal_hint": goal,
        "success_criteria_hint": success_criteria,
        "constraints_hint": constraints,
        "next_steps": [
            f"Inspect the current orchestrator/control-plane surface for where `{name}` should live.",
            "Implement the missing role/sensor with the smallest viable integration into the existing pipeline.",
            "Refresh the system architect report and confirm the named gap disappears.",
        ],
        "reasoning_hint": (
            f"The operator-curated target operating model explicitly names `{name}` as required, so the "
            "absence is a concrete architecture gap rather than a speculative improvement."
        ),
        "evidence": [
            f"target model: {target_path}",
            f"missing {detail_type}: {name}",
        ],
        "metrics": {
            "detail_type": detail_type,
        },
        "operator_approval_required": True,
    }


def _detail_key(detail_type: str) -> str:
    return detail_type[:-1] if detail_type.endswith("s") else detail_type


def evaluate_system_architect(cfg: dict) -> dict:
    repo = _default_repo_slug(cfg)
    model, target_path = load_target_operating_model(cfg, repo)
    current = enumerate_current_state(cfg)
    accepted = _accepted_omission_keys(model)

    findings: list[dict] = []
    omitted: list[dict] = []
    for section, kind, detail_types in (
        ("capabilities", "capability_gap", ("roles", "jobs", "agents")),
        ("sensors", "sensor_gap", ("signals", "schemas")),
    ):
        current_section = current.get(section) or {}
        for detail_type in detail_types:
            detail_key = _detail_key(detail_type)
            current_names = {name.lower() for name in current_section.get(detail_type, [])}
            for target_name in _target_items(model, section, detail_type):
                key = f"{kind}:{detail_key}:{target_name.lower()}"
                if target_name.lower() in current_names:
                    continue
                if key in accepted:
                    omitted.append(
                        {
                            "kind": kind,
                            "detail_type": detail_key,
                            "name": target_name,
                        }
                    )
                    continue
                findings.append(
                    _build_gap_finding(
                        repo=repo,
                        kind=kind,
                        detail_type=detail_key,
                        name=target_name,
                        target_path=target_path,
                    )
                )

    capability_gaps = [f for f in findings if f.get("kind") == "capability_gap"]
    sensor_gaps = [f for f in findings if f.get("kind") == "sensor_gap"]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "repo": repo,
        "target_model_path": str(target_path),
        "cadence_days": float(resolve_system_architect_config(cfg, repo).get("cadence_days") or DEFAULT_CADENCE_DAYS),
        "current_state": current,
        "findings": findings,
        "capability_gaps": capability_gaps,
        "sensor_gaps": sensor_gaps,
        "accepted_omissions": omitted,
    }


def write_system_architect_report(cfg: dict, report: dict) -> Path:
    path = system_architect_report_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def load_system_architect_report(cfg: dict) -> dict | None:
    path = system_architect_report_path(cfg)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def build_system_architect_findings(cfg: dict, now: datetime | None = None) -> list[dict]:
    repo = _default_repo_slug(cfg)
    architect_cfg = resolve_system_architect_config(cfg, repo)
    if not architect_cfg.get("enabled", True):
        return []

    current = now or datetime.now(timezone.utc)
    cadence_days = float(architect_cfg.get("cadence_days") or DEFAULT_CADENCE_DAYS)
    due, _reason = is_due(cfg, JOB_NAME, repo, cadence_hours=cadence_days * 24.0, now=current)
    report = load_system_architect_report(cfg)
    if report is None or due:
        report = evaluate_system_architect(cfg)
        write_system_architect_report(cfg, report)
        record_run(cfg, JOB_NAME, repo, now=current)
    return list(report.get("findings") or [])


def architect_digest_line(cfg: dict) -> str:
    report = load_system_architect_report(cfg)
    if not report:
        return "system architect: no report"
    capability_gaps = len(report.get("capability_gaps") or [])
    sensor_gaps = len(report.get("sensor_gaps") or [])
    accepted = len(report.get("accepted_omissions") or [])
    return (
        f"system architect: {capability_gaps} capability gap(s), "
        f"{sensor_gaps} sensor gap(s), {accepted} accepted omission(s)"
    )


def run() -> None:
    from orchestrator.paths import load_config

    cfg = load_config()
    findings = build_system_architect_findings(cfg)
    report = load_system_architect_report(cfg) or {}
    print(
        f"System architect: {len(findings)} gap finding(s), "
        f"{len(report.get('accepted_omissions') or [])} accepted omission(s)."
    )


if __name__ == "__main__":
    run()
