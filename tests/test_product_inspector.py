"""Tests for orchestrator.product_inspector."""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.product_inspector import (
    ARTIFACT_DEFAULT,
    CONTEXT_MAX_CHARS,
    MAX_TARGETS,
    OBSERVATION_CATEGORIES,
    _clean_html,
    _domain_allowed,
    _fetch_target,
    _inspect_target,
    _write_inspection_artifact,
    inspect_product,
    repo_inspection_config,
)


def test_domain_allowed():
    assert _domain_allowed("app.example.com", ["example.com"])
    assert _domain_allowed("example.com", ["example.com"])
    assert not _domain_allowed("evil.com", ["example.com"])
    assert not _domain_allowed("notexample.com", ["example.com"])
    assert not _domain_allowed("", ["example.com"])


def test_clean_html():
    raw = "<html><head><script>alert(1)</script><style>body{}</style></head><body><h1>Hello</h1><p>World</p></body></html>"
    cleaned = _clean_html(raw)
    assert "alert" not in cleaned
    assert "body{}" not in cleaned
    assert "Hello" in cleaned
    assert "World" in cleaned


def test_fetch_target_rejects_http():
    content, error = _fetch_target("http://example.com", ["example.com"], 1000)
    assert content is None
    assert "HTTPS" in error


def test_fetch_target_rejects_unlisted_domain():
    content, error = _fetch_target("https://evil.com/page", ["example.com"], 1000)
    assert content is None
    assert "not in allowed list" in error


def test_inspect_product_disabled():
    cfg = {"product_inspection": {"enabled": False}}
    result = inspect_product(cfg, "owner/repo", Path("/tmp/nonexistent"))
    assert "disabled" in result


def test_inspect_product_no_targets():
    cfg = {"product_inspection": {"enabled": True, "targets": []}}
    result = inspect_product(cfg, "owner/repo", Path("/tmp/nonexistent"))
    assert "no targets" in result


def test_inspect_product_enabled_with_fresh_artifact(tmp_path):
    """When the artifact is fresh, return cached content without fetching."""
    repo = tmp_path / "repo"
    repo.mkdir()
    artifact = repo / ARTIFACT_DEFAULT
    artifact.write_text("# Product Inspection\n\n## Landing page\n- Status: ok\n")

    cfg = {
        "product_inspection": {
            "enabled": True,
            "max_age_hours": 24,
            "allowed_domains": ["example.com"],
            "targets": [{"name": "Landing", "url": "https://example.com"}],
        }
    }
    result = inspect_product(cfg, "owner/repo", repo)
    assert "Landing page" in result


def test_inspect_product_calls_fetch_when_stale(tmp_path):
    """When no artifact exists, should attempt to fetch targets."""
    repo = tmp_path / "repo"
    repo.mkdir()

    cfg = {
        "product_inspection": {
            "enabled": True,
            "max_age_hours": 24,
            "allowed_domains": ["example.com"],
            "targets": [
                {"name": "Landing", "url": "https://example.com", "description": "Homepage"},
            ],
        }
    }

    fake_curl_result = subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout="<html><body><h1>Welcome</h1><p>Our product rocks</p></body></html>",
        stderr="",
    )
    fake_haiku_result = subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout=json.dumps({
            "status": "ok",
            "summary": "Landing page shows product intro.",
            "observations": [
                {
                    "category": "positive_signal",
                    "detail": "Clear headline and CTA present",
                    "severity": "low",
                    "planning_implication": "No action needed",
                }
            ],
        }),
        stderr="",
    )

    call_count = {"n": 0}
    original_run = subprocess.run

    def mock_run(cmd, **kwargs):
        call_count["n"] += 1
        if cmd[0] == "curl":
            return fake_curl_result
        if cmd[0] in ("claude",):
            return fake_haiku_result
        return original_run(cmd, **kwargs)

    with patch("orchestrator.product_inspector.subprocess.run", side_effect=mock_run):
        result = inspect_product(cfg, "owner/repo", repo)

    assert "Landing" in result
    assert "https://example.com" in result
    artifact = repo / ARTIFACT_DEFAULT
    assert artifact.exists()
    assert "Product Inspection" in artifact.read_text()


def test_write_inspection_artifact(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    artifact_path = repo / ARTIFACT_DEFAULT

    results = [
        {
            "name": "Homepage",
            "url": "https://example.com",
            "status": "degraded",
            "summary": "Page loads but CTA is broken.",
            "observations": [
                {
                    "category": "broken_flow",
                    "detail": "Signup button returns 404",
                    "severity": "high",
                    "planning_implication": "Fix signup flow urgently",
                },
            ],
        },
    ]
    _write_inspection_artifact(repo, artifact_path, results, 24.0, {"allowed_domains": ["example.com"]})

    content = artifact_path.read_text()
    assert "Homepage" in content
    assert "broken_flow" in content
    assert "Signup button returns 404" in content
    assert "Refresh after: 24h" in content


def test_repo_inspection_config_per_repo_override():
    cfg = {
        "product_inspection": {"enabled": False, "max_age_hours": 48},
        "github_projects": {
            "proj": {
                "repos": [
                    {
                        "github_repo": "owner/repo1",
                        "product_inspection": {
                            "enabled": True,
                            "allowed_domains": ["app.example.com"],
                            "targets": [{"name": "App", "url": "https://app.example.com"}],
                        },
                    },
                ]
            }
        },
    }
    icfg = repo_inspection_config(cfg, "owner/repo1")
    assert icfg["enabled"] is True
    assert icfg["max_age_hours"] == 48  # inherited from global
    assert icfg["allowed_domains"] == ["app.example.com"]  # overridden


def test_repo_inspection_config_no_override():
    cfg = {
        "product_inspection": {"enabled": True, "max_age_hours": 12},
        "github_projects": {
            "proj": {
                "repos": [
                    {"github_repo": "owner/repo1"},
                ]
            }
        },
    }
    icfg = repo_inspection_config(cfg, "owner/repo1")
    assert icfg["enabled"] is True
    assert icfg["max_age_hours"] == 12


def test_max_targets_cap():
    """Ensure we never inspect more than MAX_TARGETS URLs."""
    cfg = {
        "product_inspection": {
            "enabled": True,
            "max_age_hours": 0,
            "max_targets": 100,  # user tries to set higher than cap
            "allowed_domains": ["example.com"],
            "targets": [
                {"name": f"Target {i}", "url": f"https://example.com/page{i}"}
                for i in range(10)
            ],
        },
    }
    repo = Path("/tmp/test_repo_inspector_cap")
    repo.mkdir(exist_ok=True)

    fetch_calls = {"count": 0}

    def mock_run(cmd, **kwargs):
        if cmd[0] == "curl":
            fetch_calls["count"] += 1
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="<html><body>OK</body></html>", stderr="")
        if cmd[0] in ("claude",):
            return subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout=json.dumps({"status": "ok", "summary": "Fine", "observations": []}),
                stderr="",
            )
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")

    with patch("orchestrator.product_inspector.subprocess.run", side_effect=mock_run):
        inspect_product(cfg, "owner/repo", repo)

    assert fetch_calls["count"] <= MAX_TARGETS

    # cleanup
    (repo / ARTIFACT_DEFAULT).unlink(missing_ok=True)


def test_observation_categories_validated():
    """Invalid observation categories should be filtered out."""
    target = {"name": "Test", "url": "https://example.com", "description": "Test"}

    fake_response = json.dumps({
        "status": "ok",
        "summary": "Test page",
        "observations": [
            {"category": "broken_flow", "detail": "Valid", "severity": "high", "planning_implication": "Fix it"},
            {"category": "invalid_category", "detail": "Invalid", "severity": "low", "planning_implication": "Ignored"},
        ],
    })

    def mock_run(cmd, **kwargs):
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=fake_response, stderr="")

    with patch("orchestrator.product_inspector.subprocess.run", side_effect=mock_run):
        result = _inspect_target(target, "some page content")

    assert len(result["observations"]) == 1
    assert result["observations"][0]["category"] == "broken_flow"
