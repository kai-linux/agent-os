"""Regression coverage for the post-rebase test-runner detection.

Incident 2026-04-23: ``_rebase_pr_onto_main`` hardcoded
``python3 -m pytest tests/`` for post-rebase validation. eigendark-website
(Next.js) and liminalconsultants (static HTML) have no ``tests/``
directory, so pytest exited non-zero on every conflicting PR. The
rebase code interpreted that as "tests failed", reset the branch, and
left PRs #88 / #95 / #98 stuck for hours with no path to merge.
"""
from __future__ import annotations

import json
from pathlib import Path

from orchestrator.pr_monitor import _detect_post_rebase_test_command


def test_python_repo_uses_pytest(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    cmd = _detect_post_rebase_test_command(tmp_path)
    assert cmd == ["python3", "-m", "pytest", "tests/", "-x", "-q", "--tb=no"]


def test_js_repo_with_test_script_uses_npm(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "x", "scripts": {"test": "jest"}})
    )
    cmd = _detect_post_rebase_test_command(tmp_path)
    assert cmd is not None
    assert cmd[0] == "npm"
    assert "test" in cmd


def test_js_repo_without_test_script_returns_none(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"name": "x", "scripts": {"build": "next build"}}))
    assert _detect_post_rebase_test_command(tmp_path) is None


def test_static_content_repo_returns_none(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<html></html>")
    (tmp_path / "sitemap.xml").write_text("<urlset/>")
    assert _detect_post_rebase_test_command(tmp_path) is None


def test_python_without_pyproject_or_setup_returns_none(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    assert _detect_post_rebase_test_command(tmp_path) is None
