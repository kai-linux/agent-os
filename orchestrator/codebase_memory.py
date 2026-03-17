"""
Per-repo CODEBASE.md memory.

- read_codebase_context(worktree): returns inject-ready string for the agent prompt
- update_codebase_memory(repo, task_id, result): appends to Recent Changes on main after task completes
"""
from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path


_TEMPLATE = """\
# Codebase Memory

> Auto-maintained by agent-os. Agents read this before starting work and update it on completion.

## Architecture

(Fill in once the project structure stabilises. Agents will append discoveries below.)

## Key Files

(Agents append important file paths and their purpose here.)

## Known Issues / Gotchas

(Agents append anything surprising or that blocked them.)

## Recent Changes

"""


def read_codebase_context(worktree: Path) -> str:
    """Return CODEBASE.md content formatted for prompt injection, or empty string."""
    codebase_md = worktree / "CODEBASE.md"
    if not codebase_md.exists():
        return ""
    content = codebase_md.read_text(encoding="utf-8").strip()
    if not content:
        return ""
    return f"\n\n---\n# Codebase Memory (read-only context)\n\n{content}\n---\n"


def update_codebase_memory(repo: Path, task_id: str, result: dict, meta: dict):
    """Append a summary entry to CODEBASE.md on the repo's main branch."""
    codebase_md = repo / "CODEBASE.md"

    # Initialise file if it doesn't exist
    if not codebase_md.exists():
        codebase_md.write_text(_TEMPLATE, encoding="utf-8")

    content = codebase_md.read_text(encoding="utf-8")

    summary = result.get("summary", "No summary.")
    files = result.get("files_changed") or []
    decisions = result.get("decisions") or []
    issue_num = meta.get("github_issue_number", "")
    repo_name = meta.get("github_repo", repo.name)
    now = datetime.now().strftime("%Y-%m-%d")

    files_str = ", ".join(f"`{f}`" for f in files[:8]) if files else "none"
    decisions_str = "\n".join(f"  - {d}" for d in decisions[:5]) if decisions else "  - none"

    entry = (
        f"\n### {now} — [{task_id}]"
        + (f" (#{issue_num} {repo_name})" if issue_num else "")
        + f"\n{summary}\n\n"
        f"**Files:** {files_str}\n\n"
        f"**Decisions:**\n{decisions_str}\n"
    )

    anchor = "## Recent Changes"
    if anchor in content:
        updated = content.replace(anchor, anchor + "\n" + entry, 1)
    else:
        updated = content + f"\n{anchor}\n{entry}"

    codebase_md.write_text(updated, encoding="utf-8")

    # Commit and push on main
    try:
        base_branch = meta.get("base_branch", "main")
        subprocess.run(
            ["git", "-C", str(repo), "add", "CODEBASE.md"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m",
             f"chore: update CODEBASE.md after {task_id}"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "push", "origin", base_branch],
            check=True, capture_output=True,
        )
        print(f"CODEBASE.md updated and pushed for {repo.name}")
    except subprocess.CalledProcessError as e:
        # Non-fatal — log and continue
        stderr = e.stderr.decode(errors="replace").strip() if e.stderr else ""
        print(f"Warning: failed to commit CODEBASE.md for {repo.name}: {stderr}")
