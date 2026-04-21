"""Send raw issue text through an LLM to produce a well-structured task spec."""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
import subprocess
import re

FORMAT_PROMPT = """You are a task formatter for an AI coding agent orchestrator.
Given a raw GitHub issue (which may be poorly formatted notes, a quick one-liner,
or a well-structured spec), extract and structure it into a clean task specification.

Return ONLY valid JSON (no markdown fences, no commentary) with exactly these fields:

{{
  "goal": "Clear one-paragraph objective describing what needs to be done",
  "success_criteria": "- Criterion 1\\n- Criterion 2\\n- Criterion 3",
  "task_type": "implementation",
  "agent_preference": "auto",
  "constraints": "- Constraint 1\\n- Prefer minimal diffs",
  "context": "Any additional context, or None"
}}

Rules:
- goal: expand terse notes into a clear, actionable objective. Keep the original intent.
- success_criteria: infer 2-4 concrete, testable criteria from the goal if not stated.
- task_type: one of implementation, debugging, architecture, research, docs, browser_automation, design, content.
  Infer from the nature of the work.
- agent_preference: "auto" unless the issue explicitly names an agent.
- constraints: always include "Prefer minimal diffs". Add others only if stated or clearly implied.
- context: preserve any useful background info. Write "None" if there is nothing extra.
- Do NOT add scope or features that were not implied by the issue.

---
Issue title: {title}

Issue body:
{body}"""


def format_task(title: str, body: str, model: str | None = None) -> dict | None:
    """Return structured task dict, or None on failure (caller should fall back)."""
    prompt = FORMAT_PROMPT.format(title=title, body=body or "(no body)")
    claude_bin = os.environ.get("CLAUDE_BIN", "claude")
    codex_bin = os.environ.get("CODEX_BIN", "codex")
    model = model or os.environ.get("FORMATTER_MODEL", "haiku")

    try:
        errors = []
        text = ""
        result = subprocess.run(
            [claude_bin, "-p", prompt, "--model", model],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            text = result.stdout.strip()
        else:
            errors.append(f"Claude exit {result.returncode}: {result.stderr[:200]}")
            result = subprocess.run(
                [codex_bin, "exec", "--skip-git-repo-check", prompt],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode == 0:
                text = result.stdout.strip()
            else:
                errors.append(f"Codex exit {result.returncode}: {(result.stderr or result.stdout)[:200]}")
                raise RuntimeError(" | ".join(errors))

        # Strip markdown fences if present
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        data = json.loads(text)

        return {
            "goal": str(data.get("goal", title)).strip(),
            "success_criteria": str(data.get("success_criteria", "")).strip(),
            "task_type": str(data.get("task_type", "implementation")).strip().lower(),
            "agent_preference": str(data.get("agent_preference", "auto")).strip().lower(),
            "constraints": str(data.get("constraints", "- Prefer minimal diffs")).strip(),
            "context": str(data.get("context", "None")).strip(),
        }
    except Exception as e:
        print(f"Warning: LLM formatting failed ({e}), falling back to raw parse")
        return None


GOAL_ANCESTRY_MAX_CHARS = 500
GOAL_ANCESTRY_SUMMARY_MAX_CHARS = 320


def _truncate(text: str, limit: int) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _load_objective_goal_context(cfg: dict, repo_path: Path, github_slug: str) -> dict:
    try:
        from orchestrator.objectives import load_repo_objective
    except Exception:
        return {}
    objective = load_repo_objective(cfg or {}, github_slug, repo_path)
    if not objective:
        return {}
    objective_path = Path(str(objective.get("_objective_path") or repo_path.name))
    objective_id = str(objective.get("id") or objective_path.stem).strip()
    objective_summary = str(
        objective.get("primary_outcome")
        or objective.get("product_name")
        or ""
    ).strip()
    return {
        "objective_id": objective_id,
        "objective_summary": objective_summary,
    }


def _load_sprint_goal_context(repo_path: Path) -> dict:
    try:
        from orchestrator.repo_context import SPRINT_DIRECTIVES_ARTIFACT
    except Exception:
        return {}
    artifact = repo_path / SPRINT_DIRECTIVES_ARTIFACT
    if not artifact.exists():
        return {}
    try:
        payload = json.loads(artifact.read_text(encoding="utf-8"))
    except Exception:
        return {}
    generated_at = str(payload.get("generated_at") or "").strip()
    sprint_id = ""
    if generated_at:
        try:
            sprint_id = f"sprint-{datetime.fromisoformat(generated_at).date().isoformat()}"
        except ValueError:
            sprint_id = f"sprint-{generated_at[:10]}".strip("-")
    headline = str(payload.get("headline") or "").strip()
    focus = [
        str(item).strip()
        for item in (payload.get("next_sprint_focus") or [])
        if str(item).strip()
    ]
    sprint_summary = headline or (focus[0] if focus else "")
    return {
        "sprint_id": sprint_id,
        "sprint_summary": sprint_summary,
    }


def _compose_parent_goal_summary(
    objective_id: str,
    objective_summary: str,
    sprint_id: str,
    sprint_summary: str,
    parent_issue: str,
    issue_title: str,
) -> str:
    parts: list[str] = []
    if objective_id or objective_summary:
        label = f"Objective {objective_id}".strip()
        if objective_summary:
            label = f"{label}: {objective_summary}" if label else objective_summary
        parts.append(label)
    if sprint_id or sprint_summary:
        label = f"Sprint {sprint_id}".strip()
        if sprint_summary:
            label = f"{label}: {sprint_summary}" if label else sprint_summary
        parts.append(label)
    if parent_issue or issue_title:
        label = f"Issue {parent_issue}".strip()
        if issue_title:
            label = f"{label}: {issue_title}" if label else issue_title
        parts.append(label)
    return _truncate(" -> ".join(part for part in parts if part), GOAL_ANCESTRY_SUMMARY_MAX_CHARS)


def resolve_goal_ancestry(
    *,
    cfg: dict,
    repo_path: Path,
    github_slug: str,
    issue: dict | None = None,
    existing: dict | None = None,
) -> dict:
    existing = dict(existing or {})
    objective_ctx = _load_objective_goal_context(cfg or {}, repo_path, github_slug)
    sprint_ctx = _load_sprint_goal_context(repo_path)

    issue_number = None
    issue_title = ""
    issue_url = ""
    if issue:
        issue_number = issue.get("number")
        issue_title = str(issue.get("title") or "").strip()
        issue_url = str(issue.get("url") or "").strip()

    parent_issue = str(existing.get("parent_issue") or "").strip()
    if not parent_issue and issue_number:
        parent_issue = f"{github_slug}#{issue_number}" if github_slug else f"#{issue_number}"

    ancestry = {
        "objective_id": str(existing.get("objective_id") or objective_ctx.get("objective_id") or "").strip(),
        "sprint_id": str(existing.get("sprint_id") or sprint_ctx.get("sprint_id") or "").strip(),
        "parent_issue": parent_issue,
        "parent_goal_summary": str(existing.get("parent_goal_summary") or "").strip(),
        "objective_summary": str(objective_ctx.get("objective_summary") or "").strip(),
        "sprint_summary": str(sprint_ctx.get("sprint_summary") or "").strip(),
        "issue_url": issue_url,
    }
    if not ancestry["parent_goal_summary"]:
        ancestry["parent_goal_summary"] = _compose_parent_goal_summary(
            ancestry["objective_id"],
            ancestry["objective_summary"],
            ancestry["sprint_id"],
            ancestry["sprint_summary"],
            ancestry["parent_issue"],
            issue_title,
        )
    if ancestry["parent_goal_summary"]:
        ancestry["parent_goal_summary"] = _truncate(
            ancestry["parent_goal_summary"], GOAL_ANCESTRY_SUMMARY_MAX_CHARS
        )
    return ancestry


def format_goal_ancestry_block(ancestry: dict, max_chars: int = GOAL_ANCESTRY_MAX_CHARS) -> str:
    objective_id = str(ancestry.get("objective_id") or "").strip()
    sprint_id = str(ancestry.get("sprint_id") or "").strip()
    parent_issue = str(ancestry.get("parent_issue") or "").strip()
    parent_goal_summary = str(ancestry.get("parent_goal_summary") or "").strip()
    issue_url = str(
        ancestry.get("issue_url")
        or ancestry.get("github_issue_url")
        or ""
    ).strip()

    lines = ["## Goal Ancestry"]
    if objective_id:
        lines.append(f"- Objective: `{objective_id}`")
    if sprint_id:
        lines.append(f"- Sprint: `{sprint_id}`")
    if parent_issue:
        parent_ref = f"[{parent_issue}]({issue_url})" if issue_url else f"`{parent_issue}`"
        lines.append(f"- Parent issue: {parent_ref}")
    if not any((objective_id, sprint_id, parent_issue, parent_goal_summary)):
        return ""

    base = "\n".join(lines)
    if not parent_goal_summary:
        return base[:max_chars]

    remaining = max_chars - len(base) - len("\n- Summary: ")
    if remaining <= 0:
        return base[:max_chars]
    lines.append(f"- Summary: {_truncate(parent_goal_summary, remaining)}")
    return "\n".join(lines)


def append_goal_ancestry_sections(body: str, ancestry: dict) -> str:
    additions: list[str] = []
    section_map = {
        "objective_id": "Objective ID",
        "sprint_id": "Sprint ID",
        "parent_issue": "Parent Issue",
        "parent_goal_summary": "Parent Goal Summary",
    }
    text = str(body or "").rstrip()
    for key, heading in section_map.items():
        value = str(ancestry.get(key) or "").strip()
        if not value:
            continue
        if re.search(rf"^##\s+{re.escape(heading)}\s*$", text, flags=re.MULTILINE):
            continue
        additions.append(f"## {heading}\n{value}")
    if not additions:
        return text + ("\n" if text else "")
    return text + "\n\n" + "\n\n".join(additions) + "\n"
