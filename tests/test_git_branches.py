from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator import git_branches
from orchestrator.git_branches import (
    detect_default_branch,
    remote_branch_exists,
    resolve_base_branch,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _make_origin_with_default(tmp_path: Path, default_branch: str) -> tuple[Path, Path]:
    """Create a bare 'origin' repo and a clone whose origin/HEAD tracks default_branch."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-b", default_branch, str(origin)], check=True, capture_output=True)

    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "-b", default_branch, str(seed)], check=True, capture_output=True)
    _git(seed, "config", "user.email", "test@example.com")
    _git(seed, "config", "user.name", "Test")
    (seed / "README.md").write_text("hello", encoding="utf-8")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-m", "init")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "origin", default_branch)

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(origin), str(clone)], check=True, capture_output=True)
    return origin, clone


def setup_function(_):
    git_branches._DEFAULT_BRANCH_CACHE.clear()


def test_detect_default_branch_master(tmp_path):
    _, clone = _make_origin_with_default(tmp_path, "master")
    assert detect_default_branch(clone) == "master"


def test_detect_default_branch_main(tmp_path):
    _, clone = _make_origin_with_default(tmp_path, "main")
    assert detect_default_branch(clone) == "main"


def test_detect_default_branch_caches(tmp_path):
    _, clone = _make_origin_with_default(tmp_path, "master")
    assert detect_default_branch(clone) == "master"
    # Break origin/HEAD; cached value should still be returned.
    subprocess.run(
        ["git", "-C", str(clone), "symbolic-ref", "--delete", "refs/remotes/origin/HEAD"],
        check=True, capture_output=True,
    )
    assert detect_default_branch(clone) == "master"


def test_detect_default_branch_returns_none_for_non_repo(tmp_path):
    assert detect_default_branch(tmp_path) is None


def test_remote_branch_exists(tmp_path):
    _, clone = _make_origin_with_default(tmp_path, "master")
    assert remote_branch_exists(clone, "master") is True
    assert remote_branch_exists(clone, "main") is False


def test_resolve_base_branch_prefers_existing_configured(tmp_path):
    _, clone = _make_origin_with_default(tmp_path, "main")
    assert resolve_base_branch(clone, "main", "main") == "main"


def test_resolve_base_branch_falls_back_to_detected(tmp_path):
    # Repo's actual default is master, but caller configured 'main'.
    _, clone = _make_origin_with_default(tmp_path, "master")
    assert resolve_base_branch(clone, "main", "main") == "master"


def test_resolve_base_branch_uses_fallback_when_no_origin(tmp_path):
    # Plain repo, no origin remote, no configured value.
    repo = tmp_path / "lonely"
    subprocess.run(["git", "init", "-b", "trunk", str(repo)], check=True, capture_output=True)
    assert resolve_base_branch(repo, None, "main") == "main"
