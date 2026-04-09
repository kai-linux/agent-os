"""Live-product inspection for evidence-driven planning.

Examines configured live product URLs and extracts structured observations
(broken flows, missing content, weak UX signals, regressions) suitable for
sprint planning and backlog shaping.

This module is opt-in per repo via the ``product_inspection`` config section.
It produces a durable ``PRODUCT_INSPECTION.md`` artifact that the strategic
planner and backlog groomer can reference.

Guardrails:
- Only HTTPS URLs on explicitly allowed domains are fetched
- At most ``max_targets`` URLs per inspection cycle (hard cap: 4)
- Each fetch is capped at ``max_target_chars`` characters
- Rate-limited to one refresh per ``max_age_hours`` window
- No authentication: only public, unauthenticated surfaces are inspected
- No form submission, no JavaScript execution, no stateful browsing
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ARTIFACT_DEFAULT = "PRODUCT_INSPECTION.md"
FAILURE_HISTORY_FILE = ".product_inspection_failures.json"
MAX_TARGETS = 4
MAX_TARGET_CHARS = 6000
DEFAULT_MAX_AGE_HOURS = 24
FETCH_TIMEOUT_SECONDS = 20
CONTEXT_MAX_CHARS = 4000
FOCUS_AREA_MODEL = "haiku"
CONSECUTIVE_FAILURE_THRESHOLD = 3  # N consecutive failures → low_confidence

# Only these observation categories are emitted
OBSERVATION_CATEGORIES = frozenset({
    "broken_flow",
    "missing_content",
    "weak_ux_signal",
    "regression",
    "positive_signal",
})


def _domain_allowed(hostname: str, allowed_domains: list[str]) -> bool:
    hostname = (hostname or "").strip().lower()
    for domain in allowed_domains:
        domain = domain.strip().lower()
        if not domain:
            continue
        if hostname == domain or hostname.endswith("." + domain):
            return True
    return False


def _clean_html(raw: str) -> str:
    """Strip HTML tags and collapse whitespace for LLM consumption."""
    text = re.sub(r"<script[^>]*>.*?</script>", "", raw, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _fetch_target(url: str, allowed_domains: list[str], max_chars: int) -> dict:
    """Fetch a single target URL. Returns provenance dict with content/error."""
    ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        return {"content": None, "error": "only HTTPS URLs are allowed",
                "fetch_timestamp": ts, "http_status": None, "response_bytes": 0}
    if not _domain_allowed(parsed.hostname or "", allowed_domains):
        return {"content": None, "error": f"domain not in allowed list: {parsed.hostname or '?'}",
                "fetch_timestamp": ts, "http_status": None, "response_bytes": 0}
    try:
        result = subprocess.run(
            ["curl", "-LfsS", "-w", "\n%{http_code}", "--max-time", str(FETCH_TIMEOUT_SECONDS), url],
            capture_output=True,
            text=True,
            timeout=FETCH_TIMEOUT_SECONDS + 5,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"content": None, "error": f"fetch error: {e}",
                "fetch_timestamp": ts, "http_status": None, "response_bytes": 0}
    # Extract HTTP status from curl -w output
    raw_out = result.stdout
    http_status = None
    if raw_out:
        lines = raw_out.rsplit("\n", 1)
        if len(lines) == 2 and lines[1].strip().isdigit():
            http_status = int(lines[1].strip())
            raw_out = lines[0]
    response_bytes = len(raw_out.encode("utf-8", errors="replace"))
    if result.returncode != 0:
        return {"content": None,
                "error": f"fetch failed (exit {result.returncode}): {result.stderr.strip()[:200]}",
                "fetch_timestamp": ts, "http_status": http_status, "response_bytes": response_bytes}
    cleaned = _clean_html(raw_out)
    return {"content": cleaned[:max_chars], "error": None,
            "fetch_timestamp": ts, "http_status": http_status, "response_bytes": response_bytes}


_INSPECTION_PROMPT = """\
You are inspecting a live product surface for a software sprint planner.

Examine the page content below and extract structured observations that would
help a planning system decide what to fix, improve, or investigate next.

Focus ONLY on what is observable in the provided text. Do not speculate about
backend behavior. Do not invent issues that are not evident from the content.

Target name: {name}
Target URL: {url}
Target description: {description}

Page content (text-only snapshot):
{content}

Return ONLY JSON with this schema:
{{
  "status": "ok" or "degraded" or "error",
  "summary": "2-3 sentence factual summary of what the page shows",
  "observations": [
    {{
      "category": one of "broken_flow", "missing_content", "weak_ux_signal", "regression", "positive_signal",
      "detail": "one sentence describing the observation",
      "severity": "high" or "medium" or "low",
      "planning_implication": "one sentence on what to do about it"
    }}
  ]
}}

If the page content looks normal with no issues, return status "ok" with an
empty observations array and a summary of what the page shows.
"""


def _inspect_target(target: dict, content: str) -> dict:
    """Send page content to Haiku for structured observation extraction."""
    prompt = _INSPECTION_PROMPT.format(
        name=target.get("name", "Unnamed target"),
        url=target.get("url", "(unknown)"),
        description=target.get("description", "Product surface"),
        content=content[:MAX_TARGET_CHARS],
    )
    claude_bin = os.environ.get("CLAUDE_BIN", "claude")
    try:
        result = subprocess.run(
            [claude_bin, "-p", prompt, "--model", FOCUS_AREA_MODEL],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            return {"status": "error", "summary": f"LLM call failed (exit {result.returncode})", "observations": []}
        text = result.stdout.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        data = json.loads(text)
        # Validate observations
        observations = []
        for obs in data.get("observations", []):
            if isinstance(obs, dict) and obs.get("category") in OBSERVATION_CATEGORIES:
                observations.append({
                    "category": obs["category"],
                    "detail": str(obs.get("detail", "")).strip()[:300],
                    "severity": obs.get("severity", "medium") if obs.get("severity") in ("high", "medium", "low") else "medium",
                    "planning_implication": str(obs.get("planning_implication", "")).strip()[:300],
                })
        return {
            "status": str(data.get("status", "ok")).strip(),
            "summary": str(data.get("summary", "")).strip()[:500],
            "observations": observations[:10],
        }
    except Exception as e:
        return {"status": "error", "summary": f"Inspection failed: {e}", "observations": []}


def _load_failure_history(repo_path: Path) -> dict:
    """Load consecutive failure counts per target URL."""
    path = repo_path / FAILURE_HISTORY_FILE
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_failure_history(repo_path: Path, history: dict) -> None:
    """Persist consecutive failure counts per target URL."""
    path = repo_path / FAILURE_HISTORY_FILE
    try:
        path.write_text(json.dumps(history, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def _update_failure_history(repo_path: Path, results: list[dict]) -> dict:
    """Update failure history and return confidence map {url: "ok"|"low_confidence"}."""
    history = _load_failure_history(repo_path)
    confidence: dict[str, str] = {}
    for entry in results:
        url = entry.get("url", "")
        if entry.get("status") == "error":
            history[url] = history.get(url, 0) + 1
        else:
            history[url] = 0
        if history.get(url, 0) >= CONSECUTIVE_FAILURE_THRESHOLD:
            confidence[url] = "low_confidence"
        else:
            confidence[url] = "ok"
    _save_failure_history(repo_path, history)
    return confidence


def _write_inspection_artifact(
    repo_path: Path,
    artifact_path: Path,
    results: list[dict],
    refresh_hours: float,
    inspection_cfg: dict,
    confidence_map: dict | None = None,
):
    """Write structured inspection results to the artifact file."""
    confidence_map = confidence_map or {}
    generated = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    allowed_domains = ", ".join(
        str(d).strip() for d in inspection_cfg.get("allowed_domains", []) if str(d).strip()
    ) or "(none configured)"
    lines = [
        "# Product Inspection",
        "",
        f"- Generated: {generated}",
        f"- Refresh after: {refresh_hours:g}h",
        f"- Allowed domains: {allowed_domains}",
        "- Scope: public unauthenticated product surfaces only",
        "- Method: deterministic text-only fetch + structured LLM observation extraction",
        "",
    ]

    # --- Coverage boundary ---
    inspected = [e["name"] for e in results if e.get("status") != "error"]
    uninspected_fetch = [e["name"] for e in results if e.get("status") == "error"]
    lines.extend([
        "## Coverage Boundary",
        "",
        "### Inspected surfaces (unauthenticated)",
    ])
    if inspected:
        for name in inspected:
            lines.append(f"- {name}")
    else:
        lines.append("- (none)")
    lines.append("")
    lines.append("### Uninspected surfaces")
    if uninspected_fetch:
        for name in uninspected_fetch:
            lines.append(f"- {name} (fetch failed)")
    lines.append("- All authenticated flows (out of scope — no credentials)")
    lines.append("- JavaScript-rendered content (text-only fetch)")
    lines.append("")

    if not results:
        lines.extend([
            "## Findings",
            "",
            "(no inspection targets were reachable or configured)",
            "",
        ])
    for entry in results:
        url = entry.get("url", "")
        confidence = confidence_map.get(url, "ok")
        # Provenance header
        lines.extend([
            f"## {entry['name']}",
            "",
            f"- Source URL: {url}",
            f"- Fetch timestamp: {entry.get('fetch_timestamp', 'unknown')}",
            f"- HTTP status: {entry.get('http_status') or 'N/A'}",
            f"- Response size: {entry.get('response_bytes', 0)} bytes",
            f"- Extraction confidence: {entry.get('extraction_confidence', 'normal')}",
        ])
        if entry.get("http_status") and entry["http_status"] != 200:
            lines.append(f"- ⚠ Non-200 response ({entry['http_status']})")
        if confidence == "low_confidence":
            lines.append(f"- ⚠ LOW CONFIDENCE — {CONSECUTIVE_FAILURE_THRESHOLD}+ consecutive fetch failures; target may be un-observable")
        lines.extend([
            f"- Status: {entry['status']}",
            "",
            "### Summary",
            "",
            entry.get("summary", "(no summary)"),
            "",
        ])
        observations = entry.get("observations", [])
        if observations:
            lines.extend(["### Observations", ""])
            for obs in observations:
                lines.append(
                    f"- **[{obs['severity'].upper()}]** ({obs['category']}) "
                    f"{obs['detail']}"
                )
                if obs.get("planning_implication"):
                    lines.append(f"  - Planning implication: {obs['planning_implication']}")
            lines.append("")
        else:
            lines.extend(["### Observations", "", "(no issues detected)", ""])
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def repo_inspection_config(cfg: dict, github_slug: str) -> dict:
    """Return merged product inspection config for a repo."""
    inspection_cfg = dict(cfg.get("product_inspection") or {})
    for project_cfg in cfg.get("github_projects", {}).values():
        if not isinstance(project_cfg, dict):
            continue
        for repo_cfg in project_cfg.get("repos", []):
            if repo_cfg.get("github_repo") != github_slug:
                continue
            override = repo_cfg.get("product_inspection")
            if isinstance(override, dict):
                merged = dict(inspection_cfg)
                merged.update(override)
                inspection_cfg = merged
            return inspection_cfg
    return inspection_cfg


def inspect_product(cfg: dict, github_slug: str, repo_path: Path, cadence_hours: float = 0) -> str:
    """Run live-product inspection and return artifact content for planning.

    Args:
        cadence_hours: If >0, use this as the staleness window instead of the
            configured max_age_hours.  Allows the planner to align freshness
            with its sprint cadence rather than a fixed N-hour window.

    Returns the inspection artifact text (capped at CONTEXT_MAX_CHARS),
    or a short placeholder if inspection is disabled or has no targets.
    """
    inspection_cfg = repo_inspection_config(cfg, github_slug)
    if not inspection_cfg.get("enabled"):
        return "(product inspection disabled)"

    targets = inspection_cfg.get("targets", [])
    if not isinstance(targets, list) or not targets:
        return "(product inspection enabled but no targets configured)"

    artifact_name = str(
        inspection_cfg.get("artifact_file", ARTIFACT_DEFAULT)
    ).strip() or ARTIFACT_DEFAULT
    configured_hours = float(inspection_cfg.get("max_age_hours", DEFAULT_MAX_AGE_HOURS))
    # Prefer planner cadence when provided; fall back to configured window
    refresh_hours = cadence_hours if cadence_hours > 0 else configured_hours
    max_targets = max(1, min(int(inspection_cfg.get("max_targets", MAX_TARGETS)), MAX_TARGETS))
    max_chars = int(inspection_cfg.get("max_target_chars", MAX_TARGET_CHARS))
    allowed_domains = [
        str(d).strip() for d in inspection_cfg.get("allowed_domains", [])
        if isinstance(d, str) and d.strip()
    ]

    artifact_path = (repo_path / artifact_name).resolve()
    repo_root = repo_path.resolve()
    if repo_root == artifact_path or repo_root not in artifact_path.parents:
        return "(product inspection artifact path invalid)"

    # Check freshness — skip if artifact is fresh enough
    if artifact_path.exists():
        age_seconds = time.time() - artifact_path.stat().st_mtime
        if age_seconds <= refresh_hours * 3600:
            content = artifact_path.read_text(encoding="utf-8", errors="replace").strip()
            return content[:CONTEXT_MAX_CHARS] if content else "(empty product inspection artifact)"

    results: list[dict] = []
    for target in targets[:max_targets]:
        if not isinstance(target, dict):
            continue
        url = str(target.get("url", "")).strip()
        name = str(target.get("name", "Unnamed target")).strip()
        if not url:
            print(f"  Skipping inspection target with no URL: {name}")
            continue

        fetch = _fetch_target(url, allowed_domains, max_chars)
        if fetch["error"]:
            print(f"  Skipping inspection target {name}: {fetch['error']}")
            results.append({
                "name": name,
                "url": url,
                "status": "error",
                "summary": f"Could not fetch: {fetch['error']}",
                "observations": [],
                "fetch_timestamp": fetch["fetch_timestamp"],
                "http_status": fetch["http_status"],
                "response_bytes": fetch["response_bytes"],
                "extraction_confidence": "none",
            })
            continue

        inspection = _inspect_target(target, fetch["content"] or "")
        results.append({
            "name": name,
            "url": url,
            **inspection,
            "fetch_timestamp": fetch["fetch_timestamp"],
            "http_status": fetch["http_status"],
            "response_bytes": fetch["response_bytes"],
            "extraction_confidence": "normal" if fetch["http_status"] == 200 else "degraded",
        })

    # Track consecutive failures and derive confidence
    confidence_map = _update_failure_history(repo_path, results)

    _write_inspection_artifact(repo_path, artifact_path, results, refresh_hours,
                               inspection_cfg, confidence_map)
    content = artifact_path.read_text(encoding="utf-8", errors="replace").strip()
    return content[:CONTEXT_MAX_CHARS] if content else "(empty product inspection artifact)"
