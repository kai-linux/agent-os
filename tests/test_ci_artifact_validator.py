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


def _patch_validator(artifacts_stdout: str, *, job_log: str | None = None, artifact_rc: int = 0):
    """Patch subprocess.run so artifact-list calls and job-log calls behave independently."""
    def fake_run(cmd, *args, **kwargs):
        if len(cmd) >= 3 and cmd[0] == "gh" and cmd[1] == "api":
            path = cmd[2]
            if "/artifacts" in path:
                return mock.Mock(stdout=artifacts_stdout, stderr="", returncode=artifact_rc)
            if "/logs" in path:
                if job_log is None:
                    return mock.Mock(stdout="", stderr="not found", returncode=1)
                return mock.Mock(stdout=job_log, stderr="", returncode=0)
        return mock.Mock(stdout="", stderr="", returncode=1)
    return mock.patch("orchestrator.ci_artifact_validator.subprocess.run", side_effect=fake_run)


def test_validate_fails_no_artifacts_and_no_logs():
    with _patch_validator("[]"):
        result = validate_ci_artifacts("owner/repo", [], run_id=100)
    assert result.valid is False
    assert result.context_source == "none"
    assert any("No CI failure artifacts found" in e for e in result.errors)


def test_validate_fails_too_small_and_no_logs():
    artifacts = [
        {"name": "pr-ci-failure-test-1", "size_in_bytes": 10, "expired": False},
    ]
    with _patch_validator(json.dumps(artifacts)):
        result = validate_ci_artifacts("owner/repo", [], run_id=100)
    assert result.valid is False
    assert result.context_source == "none"
    assert any("too small" in e for e in result.errors)


def test_validate_fails_expired_and_no_logs():
    artifacts = [
        {"name": "pr-ci-failure-test-1", "size_in_bytes": 5000, "expired": True},
    ]
    with _patch_validator(json.dumps(artifacts)):
        result = validate_ci_artifacts("owner/repo", [], run_id=100)
    assert result.valid is False
    assert result.context_source == "none"
    assert any("expired" in e for e in result.errors)


def test_validate_ignores_non_ci_artifacts():
    artifacts = [
        {"name": "some-other-artifact", "size_in_bytes": 5000, "expired": False},
    ]
    with _patch_validator(json.dumps(artifacts)):
        result = validate_ci_artifacts("owner/repo", [], run_id=100)
    assert result.valid is False
    assert any("No CI failure artifacts found" in e for e in result.errors)


def test_validate_extracts_run_id_from_checks():
    checks = [{"name": "test", "state": "FAILURE",
                "link": "https://github.com/o/r/actions/runs/999/job/1"}]
    artifacts = [
        {"name": "pr-ci-failure-test-1", "size_in_bytes": 1000, "expired": False},
    ]
    with _patch_validator(json.dumps(artifacts)):
        result = validate_ci_artifacts("owner/repo", checks)
    assert result.valid is True
    assert result.run_id == 999
    assert result.context_source == "artifacts"


def test_validate_api_failure_returns_no_artifacts():
    with _patch_validator("", artifact_rc=1):
        result = validate_ci_artifacts("owner/repo", [], run_id=100)
    assert result.valid is False
    assert result.context_source == "none"


def test_validate_falls_back_to_job_logs_when_artifacts_missing():
    checks = [{"name": "Secret scan", "state": "FAILURE",
                "link": "https://github.com/o/r/actions/runs/999/job/12345"}]
    log_body = "starting secret scan...\n" * 20 + "ERROR: leaked token at config.py:42\n"
    with _patch_validator("[]", job_log=log_body):
        result = validate_ci_artifacts("owner/repo", checks)
    assert result.valid is True
    assert result.context_source == "job_logs"
    assert result.log_excerpt is not None
    assert "ERROR: leaked token" in result.log_excerpt
    assert result.log_jobs == ["Secret scan"]


def test_validate_log_fallback_truncates_tail():
    from orchestrator.ci_artifact_validator import MAX_LOG_EXCERPT_BYTES
    checks = [{"name": "Secret scan", "state": "FAILURE",
                "link": "https://github.com/o/r/actions/runs/999/job/12345"}]
    huge_log = ("noise\n" * 50_000) + "FINAL_ERROR_MARKER\n"
    with _patch_validator("[]", job_log=huge_log):
        result = validate_ci_artifacts("owner/repo", checks)
    assert result.valid is True
    assert "FINAL_ERROR_MARKER" in result.log_excerpt
    assert len(result.log_excerpt) <= MAX_LOG_EXCERPT_BYTES + 200  # +fence overhead


def test_validate_log_fallback_fails_when_logs_unavailable():
    checks = [{"name": "Secret scan", "state": "FAILURE",
                "link": "https://github.com/o/r/actions/runs/999/job/12345"}]
    with _patch_validator("[]", job_log=None):
        result = validate_ci_artifacts("owner/repo", checks)
    assert result.valid is False
    assert result.context_source == "none"
    assert any("Could not fetch failed-job logs" in e for e in result.errors)


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
