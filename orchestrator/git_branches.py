from __future__ import annotations

import subprocess
from pathlib import Path

_DEFAULT_BRANCH_CACHE: dict[str, str] = {}


def detect_default_branch(repo: Path) -> str | None:
    """Return the remote default branch (e.g. 'main' or 'master').

    Reads `refs/remotes/origin/HEAD`. Returns None if it is not set or git fails.
    Cached per repo path for the lifetime of the process.
    """
    key = str(repo)
    if key in _DEFAULT_BRANCH_CACHE:
        return _DEFAULT_BRANCH_CACHE[key]
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    ref = result.stdout.strip()
    if "/" not in ref:
        return None
    branch = ref.split("/", 1)[1]
    _DEFAULT_BRANCH_CACHE[key] = branch
    return branch


def remote_branch_exists(repo: Path, branch: str) -> bool:
    """Return True if `origin/<branch>` resolves in `repo`."""
    try:
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", f"origin/{branch}"],
            capture_output=True,
            check=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def resolve_base_branch(repo: Path, configured: str | None, fallback: str = "main") -> str:
    """Resolve the base branch to use for a repo.

    Order of precedence:
      1. `configured` if its `origin/<branch>` exists.
      2. Auto-detected default branch from `origin/HEAD`.
      3. `configured` (even if origin ref is missing) so the caller's intent is preserved.
      4. `fallback`.
    """
    if configured and remote_branch_exists(repo, configured):
        return configured
    detected = detect_default_branch(repo)
    if detected:
        return detected
    return configured or fallback
