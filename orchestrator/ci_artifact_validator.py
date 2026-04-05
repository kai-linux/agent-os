"""Validate CI artifacts before dispatching debug tasks.

Checks that error logs, build output, and build metadata uploaded by the CI
workflow are accessible and non-trivial so debug tasks are not dispatched
against incomplete data.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field

_RUN_ID_RE = re.compile(r"/actions/runs/(\d+)")

# Artifact name pattern used by our CI workflow (ci.yml):
#   pr-ci-failure-<job>-<attempt>
_CI_ARTIFACT_NAME_RE = re.compile(r"^pr-ci-failure-")

# Minimum total artifact size (bytes) to consider useful.
# A single log line is ~100 bytes; we expect at least install.log + pytest.log.
MIN_ARTIFACT_BYTES = 256


@dataclass
class ArtifactValidation:
    """Result of CI artifact validation."""
    valid: bool
    run_id: int | None = None
    artifacts: list[dict] = field(default_factory=list)
    total_bytes: int = 0
    errors: list[str] = field(default_factory=list)
    elapsed_ms: float = 0.0


def _extract_run_id_from_checks(checks: list[dict]) -> int | None:
    """Extract the GitHub Actions run ID from check link URLs."""
    for check in checks:
        link = (check.get("link") or "").strip()
        m = _RUN_ID_RE.search(link)
        if m:
            return int(m.group(1))
    return None


def _list_run_artifacts(repo: str, run_id: int) -> list[dict]:
    """List artifacts for a specific workflow run via gh api."""
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{repo}/actions/runs/{run_id}/artifacts",
             "--jq", ".artifacts"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return []
        out = result.stdout.strip()
        return json.loads(out) if out else []
    except Exception:
        return []


def validate_ci_artifacts(
    repo: str,
    checks: list[dict],
    *,
    run_id: int | None = None,
) -> ArtifactValidation:
    """Validate that CI failure artifacts exist and are accessible.

    Returns an ArtifactValidation with ``valid=True`` when at least one
    CI failure artifact exists and meets the minimum size threshold.

    The entire validation is bounded to a single lightweight API call so
    it stays well under the 500 ms dispatch budget.
    """
    start = time.monotonic()
    result = ArtifactValidation(valid=False)

    # Resolve run ID from check links if not provided.
    rid = run_id or _extract_run_id_from_checks(checks)
    result.run_id = rid

    if rid is None:
        result.errors.append(
            "Could not determine workflow run ID from check links. "
            "Re-run the CI workflow so artifacts are uploaded."
        )
        result.elapsed_ms = (time.monotonic() - start) * 1000
        return result

    artifacts = _list_run_artifacts(repo, rid)
    ci_artifacts = [a for a in artifacts if _CI_ARTIFACT_NAME_RE.match(a.get("name", ""))]
    result.artifacts = ci_artifacts

    if not ci_artifacts:
        result.errors.append(
            f"No CI failure artifacts found for run {rid}. "
            f"Expected artifact name matching 'pr-ci-failure-*'. "
            f"Re-run the CI workflow to generate failure artifacts."
        )
        result.elapsed_ms = (time.monotonic() - start) * 1000
        return result

    total = sum(a.get("size_in_bytes", 0) for a in ci_artifacts)
    result.total_bytes = total

    if total < MIN_ARTIFACT_BYTES:
        names = ", ".join(a.get("name", "?") for a in ci_artifacts)
        result.errors.append(
            f"CI artifacts too small ({total} bytes, minimum {MIN_ARTIFACT_BYTES}). "
            f"Artifacts found: {names}. "
            f"The error logs may be empty — re-run the CI workflow."
        )
        result.elapsed_ms = (time.monotonic() - start) * 1000
        return result

    # Check for expired artifacts.
    expired = [a for a in ci_artifacts if a.get("expired", False)]
    if expired:
        names = ", ".join(a.get("name", "?") for a in expired)
        result.errors.append(
            f"CI artifacts have expired: {names}. "
            f"Re-run the CI workflow to generate fresh artifacts."
        )
        result.elapsed_ms = (time.monotonic() - start) * 1000
        return result

    result.valid = True
    result.elapsed_ms = (time.monotonic() - start) * 1000
    return result


def format_validation_log(validation: ArtifactValidation, task_context: str = "") -> str:
    """Format validation result for logging and cross-attempt correlation."""
    status = "PASS" if validation.valid else "FAIL"
    parts = [
        f"[ci-artifact-validation] {status}",
        f"run_id={validation.run_id}",
        f"artifacts={len(validation.artifacts)}",
        f"total_bytes={validation.total_bytes}",
        f"elapsed_ms={validation.elapsed_ms:.0f}",
    ]
    if task_context:
        parts.append(f"context={task_context}")
    if validation.errors:
        parts.append(f"errors={'; '.join(validation.errors)}")
    return " | ".join(parts)
