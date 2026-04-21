"""Rolling log of sprint reports + cross-sprint recurring-concern detection.

The planner overwrites ``SPRINT_REPORT.md`` each sprint, so without a separate
log there is no way to see whether the same risk bullet has been flagged 4
sprints running. This module persists each generated report as a JSONL record
and exposes ``find_recurring_concerns`` so the scorer (for findings) and the
planner (for prompt context) can detect meta-sprint patterns and escalate.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


SPRINT_HISTORY_FILENAME = "sprint_reports.jsonl"

_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "of", "to", "is", "in", "on", "for", "with",
    "as", "at", "by", "from", "that", "this", "these", "those", "it", "be",
    "are", "was", "were", "been", "being", "have", "has", "had", "do", "does",
    "did", "not", "no", "so", "if", "but", "than", "then", "will", "would",
    "could", "should", "may", "might", "can", "still", "also", "some", "many",
    "most", "all", "any", "which", "who", "what", "when", "where", "why", "how",
    "more", "less", "other", "another", "such", "into", "over", "under", "its",
    "their", "them", "they", "we", "our", "us", "you", "your",
})

_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z\-_]{2,}")


def sprint_history_path(cfg: dict) -> Path:
    root = Path(cfg.get("root_dir", ".")).expanduser()
    return root / "runtime" / "metrics" / SPRINT_HISTORY_FILENAME


def append_sprint_report(cfg: dict, github_slug: str, report: dict) -> Path | None:
    """Persist one sprint report for a repo. Returns the log path, or None if
    writing is not possible (e.g. no root_dir, permissions error)."""
    path = sprint_history_path(cfg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    record = {
        "repo": github_slug,
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "headline": str(report.get("headline") or "").strip(),
        "movement_summary": str(report.get("movement_summary") or "").strip(),
        "progress_points": [str(x).strip() for x in (report.get("progress_points") or []) if str(x).strip()],
        "risks_and_gaps": [str(x).strip() for x in (report.get("risks_and_gaps") or []) if str(x).strip()],
        "next_sprint_focus": [str(x).strip() for x in (report.get("next_sprint_focus") or []) if str(x).strip()],
        "debug_hypothesis": str(report.get("debug_hypothesis") or "").strip(),
    }
    line = json.dumps(record, sort_keys=True) + "\n"
    # Atomic append: read-then-rewrite via tempfile avoids interleaved partial
    # writes when cron jobs collide.
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(existing + line)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return None
    return path


def load_sprint_history(cfg: dict, github_slug: str, limit: int = 10) -> list[dict]:
    """Return the most recent ``limit`` sprint reports for a repo, oldest first."""
    path = sprint_history_path(cfg)
    if not path.exists():
        return []
    records: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("repo") == github_slug:
                records.append(record)
    return records[-limit:]


def _concern_signature(text: str) -> frozenset[str]:
    """Reduce a risk/concern bullet to a stable set of content words."""
    words = {
        w.lower().rstrip("_-")
        for w in _WORD_RE.findall(text or "")
    }
    return frozenset(w for w in words if w not in _STOPWORDS and len(w) > 2)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    if not intersection:
        return 0.0
    return intersection / len(a | b)


def find_recurring_concerns(
    reports: list[dict],
    *,
    min_repeats: int = 3,
    similarity_threshold: float = 0.45,
) -> list[dict]:
    """Group similar risk/concern bullets across sprints.

    Bullets are grouped by Jaccard similarity on their content-word sets. Any
    group that appears in ``min_repeats`` or more distinct sprints is flagged
    as recurring. Each returned entry includes the number of sprints it
    appeared in, the total sprint count considered, and the latest example
    phrasing (most recent wording is usually the richest).
    """
    if len(reports) < min_repeats:
        return []

    # Flatten all concerns into (sprint_idx, bullet, signature) tuples.
    flat: list[tuple[int, str, frozenset[str]]] = []
    for idx, report in enumerate(reports):
        for bullet in report.get("risks_and_gaps") or []:
            sig = _concern_signature(bullet)
            if sig:
                flat.append((idx, str(bullet).strip(), sig))

    # Simple greedy clustering: each concern joins the first existing group
    # it's similar enough to, else starts a new group.
    clusters: list[dict] = []  # each: {"signatures": [sig,...], "sprints": set, "latest": str, "latest_idx": int}
    for idx, bullet, sig in flat:
        placed = False
        for cluster in clusters:
            if any(_jaccard(sig, other) >= similarity_threshold for other in cluster["signatures"]):
                cluster["signatures"].append(sig)
                cluster["sprints"].add(idx)
                if idx >= cluster["latest_idx"]:
                    cluster["latest"] = bullet
                    cluster["latest_idx"] = idx
                placed = True
                break
        if not placed:
            clusters.append({
                "signatures": [sig],
                "sprints": {idx},
                "latest": bullet,
                "latest_idx": idx,
            })

    recurring: list[dict] = []
    for cluster in clusters:
        if len(cluster["sprints"]) < min_repeats:
            continue
        # Pick a few representative signature words for debugging output.
        all_words: set[str] = set()
        for sig in cluster["signatures"]:
            all_words.update(sig)
        recurring.append({
            "example": cluster["latest"],
            "sprint_count": len(cluster["sprints"]),
            "total_sprints_considered": len(reports),
            "keywords": sorted(all_words)[:8],
        })

    # Highest repeat count first, then most recent.
    recurring.sort(key=lambda r: (-r["sprint_count"], -max((s for s in cluster["sprints"]), default=0)))
    return recurring


def format_recurring_concerns_for_prompt(recurring: list[dict], max_items: int = 5) -> str:
    """Render recurring-concern entries as a compact bullet list for LLM prompts."""
    if not recurring:
        return ""
    lines: list[str] = []
    for entry in recurring[:max_items]:
        lines.append(
            f"- Appeared in {entry['sprint_count']}/{entry['total_sprints_considered']} "
            f"recent sprint reports: {entry['example']}"
        )
    return "\n".join(lines)
