"""Tests for orchestrator.ci_artifact_validator."""
from __future__ import annotations

import json
from unittest import mock

import pytest

from orchestrator.ci_artifact_validator import (
    ArtifactValidation,
    validate_ci_artifacts,
    format_validation_log,
    _extract_run_id_from_checks,
)


# ---------------------------------------------------------------------------
# _extract_run_id_from_checks
# ---------------------------------------------------------------------------

def test_extract_run_id_from_check_link():
    checks = [
        {"name": "test", "state": "FAILURE", "link": "https://github.com/owner/repo/actions/runs/12345/job/67890"},
    ]
    assert _extract_run_id_from_checks(checks) == 12345


def test_extract_run_id_no_link():
    assert _extract_run_id_from_checks([{"name": "test", "state": "FAILURE", "link": ""}]) is None
    assert _extract_run_id_from_checks([]) is None


# ---------------------------------------------------------------------------
# validate_ci_artifacts
# ---------------------------------------------------------------------------

def _mock_run(artifacts_json: str, returncode: int = 0):
    """Return a mock subprocess.run result."""
    return mock.Mock(stdout=artifacts_json, stderr="", returncode=returncode)


def test_validate_passes_with_good_artifacts():
    artifacts = [
        {"name": "pr-ci-failure-test-1", "size_in_bytes": 5000, "expired": False},
    ]
    with mock.patch("orchestrator.ci_artifact_validator.subprocess.run",
                    return_value=_mock_run(json.dumps(artifacts))):
        result = validate_ci_artifacts("owner/repo", [], run_id=100)
    assert result.valid is True
    assert result.run_id == 100
    assert len(result.artifacts) == 1
    assert result.total_bytes == 5000
    assert not result.errors
    assert result.elapsed_ms >= 0


def test_validate_fails_no_run_id():
    result = validate_ci_artifacts("owner/repo", [])
    assert result.valid is False
    assert "Could not determine workflow run ID" in result.errors[0]


def test_validate_fails_no_artifacts():
    with mock.patch("orchestrator.ci_artifact_validator.subprocess.run",
                    return_value=_mock_run("[]")):
        result = validate_ci_artifacts("owner/repo", [], run_id=100)
    assert result.valid is False
    assert "No CI failure artifacts found" in result.errors[0]


def test_validate_fails_too_small():
    artifacts = [
        {"name": "pr-ci-failure-test-1", "size_in_bytes": 10, "expired": False},
    ]
    with mock.patch("orchestrator.ci_artifact_validator.subprocess.run",
                    return_value=_mock_run(json.dumps(artifacts))):
        result = validate_ci_artifacts("owner/repo", [], run_id=100)
    assert result.valid is False
    assert "too small" in result.errors[0]


def test_validate_fails_expired():
    artifacts = [
        {"name": "pr-ci-failure-test-1", "size_in_bytes": 5000, "expired": True},
    ]
    with mock.patch("orchestrator.ci_artifact_validator.subprocess.run",
                    return_value=_mock_run(json.dumps(artifacts))):
        result = validate_ci_artifacts("owner/repo", [], run_id=100)
    assert result.valid is False
    assert "expired" in result.errors[0]


def test_validate_ignores_non_ci_artifacts():
    artifacts = [
        {"name": "some-other-artifact", "size_in_bytes": 5000, "expired": False},
    ]
    with mock.patch("orchestrator.ci_artifact_validator.subprocess.run",
                    return_value=_mock_run(json.dumps(artifacts))):
        result = validate_ci_artifacts("owner/repo", [], run_id=100)
    assert result.valid is False
    assert "No CI failure artifacts found" in result.errors[0]


def test_validate_extracts_run_id_from_checks():
    checks = [{"name": "test", "state": "FAILURE",
                "link": "https://github.com/o/r/actions/runs/999/job/1"}]
    artifacts = [
        {"name": "pr-ci-failure-test-1", "size_in_bytes": 1000, "expired": False},
    ]
    with mock.patch("orchestrator.ci_artifact_validator.subprocess.run",
                    return_value=_mock_run(json.dumps(artifacts))):
        result = validate_ci_artifacts("owner/repo", checks)
    assert result.valid is True
    assert result.run_id == 999


def test_validate_api_failure_returns_no_artifacts():
    with mock.patch("orchestrator.ci_artifact_validator.subprocess.run",
                    return_value=_mock_run("", returncode=1)):
        result = validate_ci_artifacts("owner/repo", [], run_id=100)
    assert result.valid is False
    assert "No CI failure artifacts found" in result.errors[0]


# ---------------------------------------------------------------------------
# format_validation_log
# ---------------------------------------------------------------------------

def test_format_log_pass():
    v = ArtifactValidation(valid=True, run_id=42, total_bytes=5000, elapsed_ms=12.3)
    log = format_validation_log(v, task_context="PR#5")
    assert "[ci-artifact-validation] PASS" in log
    assert "run_id=42" in log
    assert "context=PR#5" in log


def test_format_log_fail():
    v = ArtifactValidation(valid=False, run_id=None, errors=["no run id"])
    log = format_validation_log(v)
    assert "[ci-artifact-validation] FAIL" in log
    assert "no run id" in log
