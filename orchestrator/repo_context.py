"""Shared repo context layers for planner and worker prompts."""
from __future__ import annotations

import re
from pathlib import Path


RESEARCH_ARTIFACT_DEFAULT = "PLANNING_RESEARCH.md"
EXECUTION_RESEARCH_TASK_TYPES = {"architecture", "research", "docs", "design", "content"}
EXECUTION_RESEARCH_HINTS = {
    "strategy", "roadmap", "research", "competitor", "analytics", "conversion",
    "user feedback", "evidence", "pricing", "positioning", "planning",
    "self-improvement", "self improvement", "degradation", "routing", "reliability",
    "observability", "score", "metrics",
}


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


def should_include_research(task_type: str, body: str) -> bool:
    task_type = str(task_type or "").strip().lower()
    if task_type in EXECUTION_RESEARCH_TASK_TYPES:
        return True
    lowered = str(body or "").lower()
    return any(hint in lowered for hint in EXECUTION_RESEARCH_HINTS)


def build_execution_context(repo_path: Path, task_type: str, body: str) -> str:
    """Return high-level layered context for worker prompts, adding research only when relevant."""
    sections = [
        ("Product Goal (README.md)", read_readme_goal(repo_path)),
        ("North Star (NORTH_STAR.md)", read_north_star(repo_path)),
        ("Strategy Context (STRATEGY.md)", read_strategy_context(repo_path)),
        ("Planning Principles (PLANNING_PRINCIPLES.md)", read_planning_principles(repo_path)),
    ]
    if should_include_research(task_type, body):
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
