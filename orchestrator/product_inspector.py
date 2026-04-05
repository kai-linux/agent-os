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
MAX_TARGETS = 4
MAX_TARGET_CHARS = 6000
DEFAULT_MAX_AGE_HOURS = 24
FETCH_TIMEOUT_SECONDS = 20
CONTEXT_MAX_CHARS = 4000
FOCUS_AREA_MODEL = "haiku"

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


def _fetch_target(url: str, allowed_domains: list[str], max_chars: int) -> tuple[str | None, str | None]:
    """Fetch a single target URL. Returns (content, error)."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        return None, "only HTTPS URLs are allowed"
    if not _domain_allowed(parsed.hostname or "", allowed_domains):
        return None, f"domain not in allowed list: {parsed.hostname or '?'}"
    try:
        result = subprocess.run(
            ["curl", "-LfsS", "--max-time", str(FETCH_TIMEOUT_SECONDS), url],
            capture_output=True,
            text=True,
            timeout=FETCH_TIMEOUT_SECONDS + 5,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return None, f"fetch error: {e}"
    if result.returncode != 0:
        return None, f"fetch failed (exit {result.returncode}): {result.stderr.strip()[:200]}"
    cleaned = _clean_html(result.stdout)
    return cleaned[:max_chars], None


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


def _write_inspection_artifact(
    repo_path: Path,
    artifact_path: Path,
    results: list[dict],
    refresh_hours: float,
    inspection_cfg: dict,
):
    """Write structured inspection results to the artifact file."""
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
    if not results:
        lines.extend([
            "## Findings",
            "",
            "(no inspection targets were reachable or configured)",
            "",
        ])
    for entry in results:
        lines.extend([
            f"## {entry['name']}",
            "",
            f"- URL: {entry['url']}",
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


def inspect_product(cfg: dict, github_slug: str, repo_path: Path) -> str:
    """Run live-product inspection and return artifact content for planning.

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
    refresh_hours = float(inspection_cfg.get("max_age_hours", DEFAULT_MAX_AGE_HOURS))
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

        content, error = _fetch_target(url, allowed_domains, max_chars)
        if error:
            print(f"  Skipping inspection target {name}: {error}")
            results.append({
                "name": name,
                "url": url,
                "status": "error",
                "summary": f"Could not fetch: {error}",
                "observations": [],
            })
            continue

        inspection = _inspect_target(target, content or "")
        results.append({
            "name": name,
            "url": url,
            **inspection,
        })

    _write_inspection_artifact(repo_path, artifact_path, results, refresh_hours, inspection_cfg)
    content = artifact_path.read_text(encoding="utf-8", errors="replace").strip()
    return content[:CONTEXT_MAX_CHARS] if content else "(empty product inspection artifact)"
