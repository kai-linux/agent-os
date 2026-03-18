"""Fast issue decomposer for dispatch-time epic splitting."""
from __future__ import annotations

import json
import os
import subprocess

DECOMPOSE_MODEL = "haiku"
MAX_SUBISSUES = 5

DECOMPOSE_PROMPT = """You are a task decomposer for an AI coding agent orchestrator.
Decide whether the GitHub issue below is atomic or an epic.

Return ONLY valid JSON (no markdown fences, no commentary) with exactly this shape:

{{
  "classification": "atomic" | "epic",
  "reason": "one sentence",
  "sub_issues": [
    {{
      "title": "Short sub-issue title",
      "goal": "One paragraph objective",
      "success_criteria": ["criterion 1", "criterion 2"],
      "constraints": ["constraint 1", "Prefer minimal diffs"],
      "context": "Optional context or None"
    }}
  ]
}}

Rules:
- Return "atomic" if the issue is already well-defined, tightly scoped, or represents one deliverable.
- Return "epic" only when the work clearly contains multiple independent deliverables that should become ordered sub-issues.
- For "epic", produce 2 to {max_subissues} ordered sub-issues. Never produce more than {max_subissues}.
- Each sub-issue must be atomic, implementation-ready, and executable in sequence.
- Preserve the original intent. Do not invent new product scope.
- Keep titles under 80 characters.
- Always include "Prefer minimal diffs" in each sub-issue constraints.
- For "atomic", return an empty sub_issues array.

---
Issue title: {title}

Issue body:
{body}"""


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    return text


def _normalize_lines(value, default: str) -> list[str]:
    if isinstance(value, str):
        items = [value.strip()] if value.strip() else []
    elif isinstance(value, list):
        items = [str(item).strip() for item in value if str(item).strip()]
    else:
        items = []
    if default not in items:
        items.append(default)
    return items


def _normalize_sub_issue(item: dict) -> dict:
    context = str(item.get("context", "None")).strip() or "None"
    return {
        "title": str(item.get("title", "")).strip(),
        "goal": str(item.get("goal", "")).strip(),
        "success_criteria": _normalize_lines(item.get("success_criteria"), "Match the stated goal"),
        "constraints": _normalize_lines(item.get("constraints"), "Prefer minimal diffs"),
        "context": context,
    }


def parse_decomposition(text: str) -> dict:
    data = json.loads(_strip_fences(text))
    classification = str(data.get("classification", "atomic")).strip().lower()
    if classification not in {"atomic", "epic"}:
        raise ValueError(f"Invalid classification: {classification!r}")

    raw_sub_issues = data.get("sub_issues") or []
    if classification == "atomic":
        return {"classification": "atomic", "reason": str(data.get("reason", "")).strip(), "sub_issues": []}

    if not isinstance(raw_sub_issues, list):
        raise ValueError("sub_issues must be a list")

    sub_issues = [_normalize_sub_issue(item or {}) for item in raw_sub_issues]
    if not (2 <= len(sub_issues) <= MAX_SUBISSUES):
        raise ValueError(f"Epic must have 2-{MAX_SUBISSUES} sub-issues")
    for sub_issue in sub_issues:
        if not sub_issue["title"] or not sub_issue["goal"]:
            raise ValueError("Each sub-issue needs title and goal")

    return {
        "classification": "epic",
        "reason": str(data.get("reason", "")).strip(),
        "sub_issues": sub_issues,
    }


def decompose_task(title: str, body: str, model: str | None = None) -> dict | None:
    """Return decomposition plan or None on failure."""
    prompt = DECOMPOSE_PROMPT.format(
        max_subissues=MAX_SUBISSUES,
        title=title,
        body=body or "(no body)",
    )
    claude_bin = os.environ.get("CLAUDE_BIN", "claude")
    model = model or os.environ.get("DECOMPOSER_MODEL", DECOMPOSE_MODEL)

    try:
        result = subprocess.run(
            [claude_bin, "-p", prompt, "--model", model],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(f"exit {result.returncode}: {result.stderr[:200]}")
        return parse_decomposition(result.stdout)
    except Exception as e:
        print(f"Warning: task decomposition failed ({e}), dispatching original issue")
        return None


def format_sub_issue_body(parent_issue_number: int, sub_issue: dict) -> str:
    criteria = "\n".join(f"- {line}" for line in sub_issue["success_criteria"])
    constraints = "\n".join(f"- {line}" for line in sub_issue["constraints"])
    context = sub_issue.get("context", "None") or "None"
    return f"""Part of #{parent_issue_number}

## Goal

{sub_issue["goal"]}

## Success Criteria

{criteria}

## Constraints

{constraints}

## Context

{context}
"""
