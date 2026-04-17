"""Validate CI failure context before dispatching debug tasks.

Primary source is artifacts uploaded by the CI workflow (error logs, build
output, metadata). When those are missing — e.g. third-party checks like
Secret scan that do not upload artifacts — we fall back to fetching the
failed-job logs via the GitHub API so debug tasks still get real context
rather than being silently dropped.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field

_RUN_ID_RE = re.compile(r"/actions/runs/(\d+)")
_JOB_ID_RE = re.compile(r"/job/(\d+)")

# Artifact name pattern used by our CI workflow (ci.yml):
#   pr-ci-failure-<job>-<attempt>
_CI_ARTIFACT_NAME_RE = re.compile(r"^pr-ci-failure-")

# Minimum total artifact size (bytes) to consider useful.
# A single log line is ~100 bytes; we expect at least install.log + pytest.log.
MIN_ARTIFACT_BYTES = 256

# Log-fallback caps.  Logs are embedded verbatim in remediation issue bodies,
# so we keep them small and bias toward the tail where the failure lives.
MAX_LOG_EXCERPT_BYTES = 16000
MAX_LOG_FETCH_BYTES = 200_000  # per job, before truncation
MIN_LOG_EXCERPT_BYTES = 120    # anything smaller is useless as context


@dataclass
class ArtifactValidation:
    """Result of CI failure-context validation."""
    valid: bool
    run_id: int | None = None
    artifacts: list[dict] = field(default_factory=list)
    total_bytes: int = 0
    errors: list[str] = field(default_factory=list)
    elapsed_ms: float = 0.0
    # Log-fallback fields, populated when artifacts are missing but the
    # failed jobs' logs could be fetched instead.
    log_excerpt: str | None = None
    log_jobs: list[str] = field(default_factory=list)
    context_source: str = "artifacts"  # "artifacts" | "job_logs" | "none"


def _extract_run_id_from_checks(checks: list[dict]) -> int | None:
    """Extract the GitHub Actions run ID from check link URLs."""
    for check in checks:
        link = (check.get("link") or "").strip()
        m = _RUN_ID_RE.search(link)
        if m:
            return int(m.group(1))
    return None


def _extract_failed_job_ids(checks: list[dict]) -> list[tuple[str, int]]:
    """Return [(check_name, job_id), ...] for failing checks whose link
    includes a /job/<id> segment."""
    out: list[tuple[str, int]] = []
    for check in checks:
        link = (check.get("link") or "").strip()
        m = _JOB_ID_RE.search(link)
        if m:
            out.append((check.get("name", "?"), int(m.group(1))))
    return out


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


def _fetch_job_log(repo: str, job_id: int, max_bytes: int = MAX_LOG_FETCH_BYTES) -> str | None:
    """Fetch the plain-text log for a specific failed job.

    Uses ``gh api .../jobs/<id>/logs`` which follows the redirect to the
    log download and returns text. Returns None on failure.
    """
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{repo}/actions/jobs/{job_id}/logs"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception:
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    text = result.stdout
    if len(text) > max_bytes:
        text = text[-max_bytes:]
    return text


def _build_log_excerpt(repo: str, checks: list[dict]) -> tuple[str | None, list[str]]:
    """Fetch logs for each failed job and return (excerpt, job_names).

    Tail-biased truncation: each job contributes up to an equal share of
    MAX_LOG_EXCERPT_BYTES, taken from the end of its log.
    """
    failed = _extract_failed_job_ids(checks)
    if not failed:
        return None, []

    per_job_budget = max(MAX_LOG_EXCERPT_BYTES // len(failed), 1024)
    chunks: list[str] = []
    names: list[str] = []
    for name, job_id in failed:
        log = _fetch_job_log(repo, job_id)
        if not log:
            continue
        tail = log[-per_job_budget:] if len(log) > per_job_budget else log
        chunks.append(f"### Job: {name} (id {job_id})\n\n```\n{tail.rstrip()}\n```")
        names.append(name)

    if not chunks:
        return None, []
    excerpt = "\n\n".join(chunks)
    if len(excerpt) < MIN_LOG_EXCERPT_BYTES:
        return None, []
    return excerpt, names


def validate_ci_artifacts(
    repo: str,
    checks: list[dict],
    *,
    run_id: int | None = None,
) -> ArtifactValidation:
    """Validate that CI failure context is available.

    Returns ``valid=True`` when either (a) at least one CI failure artifact
    exists and meets the minimum size, or (b) artifacts are unavailable but
    the failed jobs' logs could be fetched as a fallback. In the fallback
    case ``context_source='job_logs'`` and ``log_excerpt`` is populated.
    """
    start = time.monotonic()
    result = ArtifactValidation(valid=False)

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
    total = sum(a.get("size_in_bytes", 0) for a in ci_artifacts)
    result.total_bytes = total
    expired = [a for a in ci_artifacts if a.get("expired", False)]

    artifact_errors: list[str] = []
    if not ci_artifacts:
        artifact_errors.append(
            f"No CI failure artifacts found for run {rid} "
            f"(expected 'pr-ci-failure-*')."
        )
    elif total < MIN_ARTIFACT_BYTES:
        names = ", ".join(a.get("name", "?") for a in ci_artifacts)
        artifact_errors.append(
            f"CI artifacts too small ({total} bytes, minimum {MIN_ARTIFACT_BYTES}). "
            f"Artifacts found: {names}."
        )
    elif expired:
        names = ", ".join(a.get("name", "?") for a in expired)
        artifact_errors.append(f"CI artifacts have expired: {names}.")
    else:
        result.valid = True
        result.context_source = "artifacts"
        result.elapsed_ms = (time.monotonic() - start) * 1000
        return result

    # Artifacts unusable — try falling back to failed-job logs.
    excerpt, job_names = _build_log_excerpt(repo, checks)
    if excerpt:
        result.valid = True
        result.context_source = "job_logs"
        result.log_excerpt = excerpt
        result.log_jobs = job_names
        result.elapsed_ms = (time.monotonic() - start) * 1000
        return result

    result.errors.extend(artifact_errors)
    result.errors.append(
        "Could not fetch failed-job logs as fallback. Re-run the CI workflow."
    )
    result.context_source = "none"
    result.elapsed_ms = (time.monotonic() - start) * 1000
    return result


def format_validation_log(validation: ArtifactValidation, task_context: str = "") -> str:
    """Format validation result for logging and cross-attempt correlation."""
    status = "PASS" if validation.valid else "FAIL"
    parts = [
        f"[ci-artifact-validation] {status}",
        f"source={validation.context_source}",
        f"run_id={validation.run_id}",
        f"artifacts={len(validation.artifacts)}",
        f"total_bytes={validation.total_bytes}",
        f"elapsed_ms={validation.elapsed_ms:.0f}",
    ]
    if validation.log_jobs:
        parts.append(f"log_jobs={','.join(validation.log_jobs)}")
    if task_context:
        parts.append(f"context={task_context}")
    if validation.errors:
        parts.append(f"errors={'; '.join(validation.errors)}")
    return " | ".join(parts)
