"""Analyze GitHub issues and decompose epics into ordered sub-issues."""
from __future__ import annotations

import json
import os
import subprocess

DECOMPOSE_PROMPT = """You are a task decomposer for an AI coding agent orchestrator.
Given a GitHub issue, decide whether it is an ATOMIC task (single well-defined deliverable)
or an EPIC (multiple independent deliverables that should be worked on separately).

Rules:
- An issue is ATOMIC if it has a single clear goal that can be completed in one work session.
- An issue is EPIC only if it clearly contains 2+ independent deliverables that do NOT
  depend on each other's implementation details to be useful.
- Do NOT split issues that are already well-scoped, even if large.
- Do NOT split issues just because they have multiple success criteria — those may all
  relate to a single deliverable.
- When splitting, create at most 5 sub-issues.
- Each sub-issue must be self-contained and independently deliverable.
- Order sub-issues by logical priority (most foundational first).

Return ONLY valid JSON (no markdown fences, no commentary) with exactly this structure:

For ATOMIC tasks:
{{"type": "atomic"}}

For EPIC tasks:
{{"type": "epic", "sub_issues": [
  {{"title": "Short descriptive title", "body": "## Goal\\n\\nClear goal\\n\\n## Success Criteria\\n\\n- Criterion 1\\n- Criterion 2\\n\\n## Constraints\\n\\n- Prefer minimal diffs"}},
  ...
]}}

---
Issue title: {title}

Issue body:
{body}"""


def decompose_issue(title: str, body: str, model: str | None = None) -> dict | None:
    """Analyze an issue and return decomposition result, or None on failure.

    Returns:
        {{"type": "atomic"}} for single-deliverable issues, or
        {{"type": "epic", "sub_issues": [...]}} for multi-deliverable epics.
        None on any failure (caller should treat as atomic).
    """
    prompt = DECOMPOSE_PROMPT.format(title=title, body=body or "(no body)")
    claude_bin = os.environ.get("CLAUDE_BIN", "claude")
    model = model or os.environ.get("DECOMPOSER_MODEL", "haiku")

    try:
        result = subprocess.run(
            [claude_bin, "-p", prompt, "--model", model],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(f"exit {result.returncode}: {result.stderr[:200]}")

        text = result.stdout.strip()

        # Strip markdown fences if present
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        data = json.loads(text)

        issue_type = data.get("type", "atomic")
        if issue_type != "epic":
            return {"type": "atomic"}

        sub_issues = data.get("sub_issues", [])
        if not sub_issues or not isinstance(sub_issues, list):
            return {"type": "atomic"}

        # Cap at 5 sub-issues
        sub_issues = sub_issues[:5]

        # Validate each sub-issue has title and body
        validated = []
        for si in sub_issues:
            t = str(si.get("title", "")).strip()
            b = str(si.get("body", "")).strip()
            if t and b:
                validated.append({"title": t, "body": b})

        if len(validated) < 2:
            return {"type": "atomic"}

        return {"type": "epic", "sub_issues": validated}

    except Exception as e:
        print(f"Warning: task decomposition failed ({e}), treating as atomic")
        return None


def create_sub_issues(
    repo: str,
    parent_number: int,
    sub_issues: list[dict],
    labels: list[str] | None = None,
) -> list[dict]:
    """Create sub-issues on GitHub, linking them to the parent issue.

    Returns list of created issue dicts with 'number', 'title', 'url' keys.
    """
    from orchestrator.gh_project import gh

    created = []
    for si in sub_issues:
        body = si["body"] + f"\n\nPart of #{parent_number}"
        cmd = [
            "issue", "create", "-R", repo,
            "--title", si["title"],
            "--body", body,
        ]
        if labels:
            cmd += ["--label", ",".join(labels)]

        try:
            raw = gh(cmd)
            # gh issue create prints the URL of the new issue
            url = raw.strip()
            # Extract issue number from URL (e.g. .../issues/42)
            number = int(url.rstrip("/").rsplit("/", 1)[-1])
            created.append({"number": number, "title": si["title"], "url": url})
        except Exception as e:
            print(f"Warning: failed to create sub-issue '{si['title']}': {e}")

    return created


def set_sub_issue_status(
    project_info: dict, item_id: str, status_value: str
):
    """Set a project item's status (used for sending sub-issues to Backlog)."""
    from orchestrator.gh_project import set_item_status

    option_id = project_info["status_options"].get(status_value)
    if project_info["status_field_id"] and option_id:
        set_item_status(
            project_info["project_id"],
            item_id,
            project_info["status_field_id"],
            option_id,
        )
