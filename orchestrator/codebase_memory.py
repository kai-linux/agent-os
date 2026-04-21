"""
Per-repo CODEBASE.md memory.

- read_codebase_context(worktree): returns inject-ready string for the agent prompt
- update_codebase_memory(repo, task_id, result): appends to Recent Changes on main after task completes
"""
from __future__ import annotations

import re
import subprocess
from datetime import datetime
from pathlib import Path

from orchestrator.commit_signature import with_agent_os_trailer


# The on-disk CODEBASE.md grows unbounded as an audit trail, but prompts must
# stay well under the 100 KB argv ceiling. When injecting into a prompt we keep
# the curated header sections (Architecture / Key Files / Known Issues) in full
# and cap the auto-accumulated "Recent Changes" tail to the N newest entries.
RECENT_CHANGES_MAX_ENTRIES = 15

_RECENT_CHANGES_RE = re.compile(
    r"(^|\n)(## Recent Changes\s*\n)(.*)\Z",
    re.DOTALL,
)


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


def _truncate_recent_changes(content: str, max_entries: int = RECENT_CHANGES_MAX_ENTRIES) -> str:
    """Keep only the newest ``max_entries`` items under ``## Recent Changes``.

    Entries are appended newest-first by ``update_codebase_memory`` (prepended
    after the anchor), so slicing the first N ``### `` headers preserves
    freshness. The header sections above ``## Recent Changes`` are left intact.
    """
    m = _RECENT_CHANGES_RE.search(content)
    if not m:
        return content
    prefix = content[:m.start(2)]
    header = m.group(2)
    tail = m.group(3)

    entries = re.split(r"(?m)^(?=### )", tail)
    leading = entries[0] if entries else ""
    items = entries[1:]
    if len(items) <= max_entries:
        return content
    kept = "".join(items[:max_entries])
    note = (
        f"\n_(Truncated for prompt budget — showing {max_entries} most recent of "
        f"{len(items)} entries. Full history lives in CODEBASE.md on disk.)_\n"
    )
    return f"{prefix}{header}{leading}{kept}{note}"


def read_codebase_context(worktree: Path) -> str:
    """Return CODEBASE.md content formatted for prompt injection, or empty string."""
    codebase_md = worktree / "CODEBASE.md"
    if not codebase_md.exists():
        return ""
    content = codebase_md.read_text(encoding="utf-8").strip()
    if not content:
        return ""
    content = _truncate_recent_changes(content)
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

    # Commit and push on main, with pull-and-retry to handle concurrent workers
    base_branch = meta.get("base_branch", "main")
    try:
        for attempt in range(3):
            try:
                subprocess.run(
                    ["git", "-C", str(repo), "add", "CODEBASE.md"],
                    check=True, capture_output=True,
                )
                # Nothing staged means another worker already wrote the same content
                result = subprocess.run(
                    ["git", "-C", str(repo), "diff", "--cached", "--quiet"],
                    capture_output=True,
                )
                if result.returncode == 0:
                    print(f"CODEBASE.md unchanged for {repo.name}, skipping commit")
                    return
                subprocess.run(
                    ["git", "-C", str(repo), "commit", "-m",
                     with_agent_os_trailer(f"chore: update CODEBASE.md after {task_id}")],
                    check=True, capture_output=True,
                )
                subprocess.run(
                    ["git", "-C", str(repo), "push", "origin", base_branch],
                    check=True, capture_output=True,
                )
                print(f"CODEBASE.md updated and pushed for {repo.name}")
                return
            except subprocess.CalledProcessError as e:
                stderr = e.stderr.decode(errors="replace").strip() if e.stderr else ""
                if "rejected" in stderr or "non-fast-forward" in stderr:
                    # Another worker pushed first — pull, re-apply our entry, retry
                    subprocess.run(
                        ["git", "-C", str(repo), "pull", "--rebase", "origin", base_branch],
                        capture_output=True,
                    )
                    # Re-read and re-apply after rebase
                    content = codebase_md.read_text(encoding="utf-8")
                    if task_id not in content:
                        updated2 = content.replace(anchor, anchor + "\n" + entry, 1) if anchor in content else content + f"\n{anchor}\n{entry}"
                        codebase_md.write_text(updated2, encoding="utf-8")
                    continue
                raise
        print(f"Warning: gave up updating CODEBASE.md for {repo.name} after 3 attempts")
    except Exception as e:
        print(f"Warning: failed to commit CODEBASE.md for {repo.name}: {e}")
