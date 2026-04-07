"""Skip signal store for sprint plan skip/auto-skip persistence.

Records when sprint plans are skipped (explicitly or via auto-skip timeout)
so the planner and groomer can read these signals on the next cycle and avoid
regenerating identical plan compositions.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

SKIP_SIGNAL_MAX_AGE_DAYS = 30
EXPLICIT_SKIP_PENALTY_WEIGHT = 3.0
AUTO_SKIP_PENALTY_WEIGHT = 1.0
SKIP_PENALTY_HALF_LIFE_DAYS = 7.0


def plan_fingerprint(plan: list[dict]) -> str:
    """Stable fingerprint: sorted issue numbers joined by comma."""
    nums = sorted(t.get("issue_number", 0) for t in plan if t.get("issue_number"))
    return ",".join(str(n) for n in nums)


def plan_issue_set(plan: list[dict]) -> set[int]:
    return {t["issue_number"] for t in plan if t.get("issue_number")}


def record_skip_signal(
    skip_signals_path: Path,
    repo: str,
    plan: list[dict],
    skip_type: str,
) -> None:
    """Append a skip event to the JSONL signal store."""
    fingerprint = plan_fingerprint(plan)
    issues = sorted(plan_issue_set(plan))
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "repo": repo,
        "skip_type": skip_type,  # "explicit" or "auto_skip"
        "fingerprint": fingerprint,
        "issues": issues,
    }
    skip_signals_path.parent.mkdir(parents=True, exist_ok=True)
    with open(skip_signals_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")


def load_skip_signals(
    skip_signals_path: Path,
    repo: str,
    max_age_days: float = SKIP_SIGNAL_MAX_AGE_DAYS,
) -> list[dict]:
    """Load recent skip signals for a repo."""
    if not skip_signals_path.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    signals: list[dict] = []
    try:
        for line in skip_signals_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("repo") != repo:
                continue
            ts_str = rec.get("timestamp", "")
            try:
                ts = datetime.fromisoformat(ts_str)
            except (ValueError, TypeError):
                continue
            if ts >= cutoff:
                signals.append(rec)
    except Exception:
        pass
    return signals


def skip_penalty_for_issue(issue_number: int, signals: list[dict]) -> float:
    """Compute a decaying anti-repeat penalty for an issue based on skip history.

    Explicit skips carry more weight than auto-skips. Penalties decay with a
    configurable half-life so issues can resurface when context changes.
    """
    now = datetime.now(timezone.utc)
    total = 0.0
    for sig in signals:
        if issue_number not in sig.get("issues", []):
            continue
        weight = (
            EXPLICIT_SKIP_PENALTY_WEIGHT
            if sig.get("skip_type") == "explicit"
            else AUTO_SKIP_PENALTY_WEIGHT
        )
        try:
            ts = datetime.fromisoformat(sig["timestamp"])
        except (ValueError, TypeError):
            continue
        age_days = max(0.0, (now - ts).total_seconds() / 86400.0)
        decay = 0.5 ** (age_days / SKIP_PENALTY_HALF_LIFE_DAYS)
        total += weight * decay
    return total


def plan_diff_line(current_plan: list[dict], signals: list[dict]) -> str:
    """Return a human-readable diff line comparing current plan to recent skipped plans."""
    if not signals:
        return ""
    current_set = plan_issue_set(current_plan)
    current_fp = plan_fingerprint(current_plan)
    # Compare against most recent skip
    latest = signals[-1]
    prev_set = set(latest.get("issues", []))
    prev_fp = latest.get("fingerprint", "")
    if not prev_set:
        return ""
    if current_set == prev_set:
        # Same issues, different order
        cur_nums = [t.get("issue_number") for t in current_plan if t.get("issue_number")]
        prev_nums = latest.get("issues", [])
        swaps = []
        for i, (a, b) in enumerate(zip(cur_nums, prev_nums)):
            if a != b:
                swaps.append(f"#{a}\u2194#{b}")
        if swaps:
            return f"Reordered: {', '.join(swaps[:3])}"
        return "No change from previous skipped plan"
    added = current_set - prev_set
    removed = prev_set - current_set
    parts = []
    if added:
        parts.append("+" + ",".join(f"#{n}" for n in sorted(added)))
    if removed:
        parts.append("-" + ",".join(f"#{n}" for n in sorted(removed)))
    return f"Changed from last skip: {' '.join(parts)}" if parts else ""


def skip_context_for_prompt(signals: list[dict]) -> str:
    """Build a prompt section describing recent skip history."""
    if not signals:
        return ""
    lines = ["Recent skip history (avoid repeating identical compositions):"]
    # Group by fingerprint
    seen_fps: dict[str, list[dict]] = {}
    for sig in signals:
        fp = sig.get("fingerprint", "")
        seen_fps.setdefault(fp, []).append(sig)
    for fp, sigs in list(seen_fps.items())[-5:]:
        issues = sigs[0].get("issues", [])
        skip_types = [s.get("skip_type", "?") for s in sigs]
        explicit_count = skip_types.count("explicit")
        auto_count = skip_types.count("auto_skip")
        parts = []
        if explicit_count:
            parts.append(f"{explicit_count}x explicit skip")
        if auto_count:
            parts.append(f"{auto_count}x auto-skip")
        lines.append(
            f"- Issues [{', '.join(f'#{n}' for n in issues)}] skipped {', '.join(parts)}"
        )
    lines.append(
        "If the same composition was explicitly skipped, choose different issues or change priority order. "
        "Auto-skips are weaker signals (user may be away); moderate reuse is acceptable."
    )
    return "\n".join(lines)
