"""Shared repo context layers for planner and worker prompts."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


EVALUATION_RUBRIC_DEFAULT = "RUBRIC.md"
RESEARCH_ARTIFACT_DEFAULT = "PLANNING_RESEARCH.md"
PRODUCTION_FEEDBACK_ARTIFACT_DEFAULT = "PRODUCTION_FEEDBACK.md"
SIGNALS_ARTIFACT_DEFAULT = "PLANNING_SIGNALS.md"
PRODUCT_INSPECTION_ARTIFACT_DEFAULT = "PRODUCT_INSPECTION.md"
SPRINT_DIRECTIVES_ARTIFACT = "runtime/next_sprint_focus.json"
EXECUTION_RESEARCH_TASK_TYPES = {"architecture", "research", "docs", "design", "content"}
EXECUTION_RESEARCH_HINTS = {
    "strategy", "roadmap", "research", "competitor", "analytics", "conversion",
    "user feedback", "evidence", "pricing", "positioning", "planning",
    "self-improvement", "self improvement", "degradation", "routing", "reliability",
    "observability", "score", "metrics", "incident", "slo", "feedback",
    "inspection", "retention", "activation",
}


_VISIBILITY_VALUES = {"public", "private", "internal"}


def get_repo_visibility(cfg: dict, github_slug: str) -> str:
    """Return declared repo visibility ("public"/"private"/"internal"). Defaults to "public"."""
    for project_cfg in (cfg.get("github_projects") or {}).values():
        if not isinstance(project_cfg, dict):
            continue
        for rc in project_cfg.get("repos", []) or []:
            if rc.get("github_repo") == github_slug:
                raw = str(rc.get("visibility", "")).strip().lower()
                if raw in _VISIBILITY_VALUES:
                    return raw
                return "public"
    return "public"


def visibility_prompt_note(visibility: str) -> str:
    """Return a prompt snippet describing planning implications of the repo's visibility."""
    if visibility == "private":
        return (
            "REPO VISIBILITY: PRIVATE. This repository has no external audience — "
            "README polish, star badges, SEO, public demos, adoption funnels, "
            "and other external-facing credibility work do NOT move any metric "
            "that matters here. Treat adoption/visibility/credibility goals as "
            "non-applicable and do not select or propose that kind of work. "
            "Prioritize internal capability, correctness, reliability, and the "
            "repo's stated internal objectives instead."
        )
    if visibility == "internal":
        return (
            "REPO VISIBILITY: INTERNAL. Audience is limited to internal users — "
            "avoid external-facing adoption work (star badges, SEO, public "
            "credibility). README clarity for internal operators is still useful."
        )
    return ""


def read_readme_goal(repo_path: Path, max_chars: int = 1200) -> str:
    readme = repo_path / "README.md"
    if not readme.exists():
        return "(no README.md found)"
    content = readme.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"##\s+Goal\s*\n(.*?)(?=\n##\s|\Z)", content, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()[:max_chars]
    return content[:max_chars].strip()


def read_north_star(repo_path: Path, max_chars: int = 1800) -> str:
    north_star = repo_path / "NORTH_STAR.md"
    if north_star.exists():
        content = north_star.read_text(encoding="utf-8", errors="replace").strip()
        return content[:max_chars] if content else "(empty NORTH_STAR.md)"
    return (
        "Bootstrap this repo toward stronger autonomy, evidence-driven planning, "
        "and closed-loop self-improvement without sacrificing auditability."
    )


def read_strategy_context(repo_path: Path, max_chars: int = 2200) -> str:
    strategy = repo_path / "STRATEGY.md"
    if not strategy.exists():
        return "(no STRATEGY.md)"
    content = strategy.read_text(encoding="utf-8", errors="replace").strip()
    return content[:max_chars] if content else "(empty STRATEGY.md)"


def read_planning_principles(repo_path: Path, max_chars: int = 1800) -> str:
    principles = repo_path / "PLANNING_PRINCIPLES.md"
    if principles.exists():
        content = principles.read_text(encoding="utf-8", errors="replace").strip()
        return content[:max_chars] if content else "(empty PLANNING_PRINCIPLES.md)"
    return (
        "Prefer work that increases autonomy, evidence-driven planning, "
        "control-plane quality, or unblocks other important work."
    )


def read_evaluation_rubric(repo_path: Path, max_chars: int = 2000) -> str:
    """Read domain-specific evaluation rubric from RUBRIC.md (or custom path).

    The rubric lets each repo declare what 'good' looks like for its domain —
    quality criteria, skills, and evaluation dimensions that planners and
    groomers should use when shaping work beyond generic README/CODEBASE context.
    """
    rubric = repo_path / EVALUATION_RUBRIC_DEFAULT
    if not rubric.exists():
        return ""
    content = rubric.read_text(encoding="utf-8", errors="replace").strip()
    return content[:max_chars] if content else ""


def read_codebase_context(repo_path: Path, max_chars: int = 3000) -> str:
    codebase = repo_path / "CODEBASE.md"
    if not codebase.exists():
        return "(no CODEBASE.md)"
    content = codebase.read_text(encoding="utf-8", errors="replace").strip()
    return content[:max_chars] if content else "(empty CODEBASE.md)"


def read_planning_research_artifact(repo_path: Path, artifact_name: str = RESEARCH_ARTIFACT_DEFAULT, max_chars: int = 2200) -> str:
    artifact = repo_path / artifact_name
    if not artifact.exists():
        return "(no planning research artifact)"
    content = artifact.read_text(encoding="utf-8", errors="replace").strip()
    return content[:max_chars] if content else "(empty planning research artifact)"


def read_planning_signals_artifact(repo_path: Path, artifact_name: str = SIGNALS_ARTIFACT_DEFAULT, max_chars: int = 2600) -> str:
    artifact = repo_path / artifact_name
    if not artifact.exists():
        return "(no planning signals artifact)"
    content = artifact.read_text(encoding="utf-8", errors="replace").strip()
    return content[:max_chars] if content else "(empty planning signals artifact)"


def read_product_inspection_artifact(
    repo_path: Path,
    artifact_name: str = PRODUCT_INSPECTION_ARTIFACT_DEFAULT,
    max_chars: int = 4000,
) -> str:
    artifact = repo_path / artifact_name
    if not artifact.exists():
        return "(no product inspection artifact)"
    content = artifact.read_text(encoding="utf-8", errors="replace").strip()
    return content[:max_chars] if content else "(empty product inspection artifact)"


def read_sprint_directives(
    repo_path: Path,
    artifact_name: str = SPRINT_DIRECTIVES_ARTIFACT,
    max_chars: int = 2000,
) -> str:
    """Format the persisted next-sprint directives sidecar for LLM prompts.

    The strategic planner writes operator-validated "next sprint focus" and
    "risks and gaps" bullets to runtime/next_sprint_focus.json at the end of
    each sprint. Both the backlog groomer and the next planner run should
    treat these as priority drivers so sprint insights actually propagate
    into the next cycle instead of being regenerated from static inputs.
    """
    artifact = repo_path / artifact_name
    if not artifact.exists():
        return "(no sprint directives — run a sprint cycle to generate next_sprint_focus.json)"
    try:
        payload = json.loads(artifact.read_text(encoding="utf-8"))
    except Exception as e:
        return f"(sprint directives unreadable: {e})"

    generated_at = str(payload.get("generated_at") or "unknown")
    headline = str(payload.get("headline") or "").strip()
    risks = [str(item).strip() for item in payload.get("risks_and_gaps") or [] if str(item).strip()]
    focus = [str(item).strip() for item in payload.get("next_sprint_focus") or [] if str(item).strip()]

    if not (headline or risks or focus):
        return "(sprint directives empty)"

    lines = [f"Generated: {generated_at}"]
    if headline:
        lines.append(f"Headline: {headline}")
    if focus:
        lines.append("")
        lines.append("Next Sprint Focus (operator-validated priorities — treat as drivers):")
        for item in focus:
            lines.append(f"- {item}")
    if risks:
        lines.append("")
        lines.append("Risks and Gaps surfaced last sprint (avoid repeating; address where possible):")
        for item in risks:
            lines.append(f"- {item}")
    text = "\n".join(lines)
    return text[:max_chars]


def read_production_feedback_artifact(
    repo_path: Path,
    artifact_name: str = PRODUCTION_FEEDBACK_ARTIFACT_DEFAULT,
    max_chars: int = 3200,
) -> str:
    artifact = repo_path / artifact_name
    if artifact.exists():
        content = artifact.read_text(encoding="utf-8", errors="replace").strip()
        return content[:max_chars] if content else "(empty production feedback artifact)"
    return read_planning_signals_artifact(repo_path, max_chars=max_chars)


def should_include_research(task_type: str, body: str) -> bool:
    task_type = str(task_type or "").strip().lower()
    if task_type in EXECUTION_RESEARCH_TASK_TYPES:
        return True
    lowered = str(body or "").lower()
    return any(hint in lowered for hint in EXECUTION_RESEARCH_HINTS)


def gather_recent_git_state(repo_path: Path, base_branch: str = "main", max_commits: int = 10) -> str:
    """Return recent commit log on the base branch for worker orientation.

    Gives workers visibility into what has recently landed so they can avoid
    conflicts and understand current repo state.  Runs in <200ms.
    """
    try:
        result = subprocess.run(
            ["git", "log", f"origin/{base_branch}", f"--max-count={max_commits}", "--oneline"],
            capture_output=True, text=True, timeout=5, cwd=str(repo_path),
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return "(recent git state unavailable)"


def gather_objective_alignment(repo_path: Path, cfg: dict | None = None, github_slug: str = "") -> str:
    """Return a brief objective alignment summary for worker prompts.

    Workers that understand what metrics the repo is optimising for can make
    better trade-off decisions and avoid missing_context blockers when the
    task rationale isn't self-evident from the issue body alone.
    """
    try:
        from orchestrator.objectives import load_repo_objective, objective_metrics
    except ImportError:
        return ""
    if not cfg:
        return ""
    objective = load_repo_objective(cfg, github_slug, repo_path)
    if not objective:
        return ""
    metrics = objective_metrics(objective)
    if not metrics:
        return ""
    primary = str(objective.get("primary_outcome", "")).strip()
    lines = []
    if primary:
        lines.append(f"Primary outcome: {primary}")
    lines.append("Tracked metrics (id / weight / direction):")
    for m in metrics:
        mid = m.get("id", "?")
        weight = m.get("weight", "?")
        direction = m.get("direction", "?")
        lines.append(f"  - {mid}: weight={weight}, direction={direction}")
    return "\n".join(lines)


def build_execution_context(repo_path: Path, task_type: str, body: str) -> str:
    """Return high-level layered context for worker prompts, adding research only when relevant."""
    sections = [
        ("Product Goal (README.md)", read_readme_goal(repo_path)),
        ("North Star (NORTH_STAR.md)", read_north_star(repo_path)),
        ("Strategy Context (STRATEGY.md)", read_strategy_context(repo_path)),
        ("Planning Principles (PLANNING_PRINCIPLES.md)", read_planning_principles(repo_path)),
    ]
    rubric = read_evaluation_rubric(repo_path)
    if rubric:
        sections.append(("Domain Evaluation Rubric (RUBRIC.md)", rubric))
    if should_include_research(task_type, body):
        sections.append(("Production Feedback (PRODUCTION_FEEDBACK.md)", read_production_feedback_artifact(repo_path)))
        sections.append(("Product Inspection (PRODUCT_INSPECTION.md)", read_product_inspection_artifact(repo_path)))
        sections.append(("Planning Research (PLANNING_RESEARCH.md)", read_planning_research_artifact(repo_path)))

    lines = ["", "", "---", "# Repository Context (read-only)", ""]
    for title, content in sections:
        lines.append(f"## {title}")
        lines.append("")
        lines.append(content)
        lines.append("")
    lines.append("---")
    lines.append("")
    return "\n".join(lines)
