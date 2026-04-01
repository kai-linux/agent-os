from __future__ import annotations

import re


CI_SIGNATURE_SECTION_RE = re.compile(r"^## CI Failure Signature\s*\n(.+?)\s*$", re.MULTILINE)
_REMAINING_FAILURE_RE = re.compile(r"^## Remaining Failure\s*\n(.*?)(?=^##\s+|\Z)", re.MULTILINE | re.DOTALL)
_CONTEXT_RE = re.compile(r"^## Context\s*\n(.*?)(?=^##\s+|\Z)", re.MULTILINE | re.DOTALL)
_FAILED_CHECK_RE = re.compile(r"^\s*-\s+\*\*(.+?)\*\*:", re.MULTILINE)
_FILE_LINE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r'File "([^"]+)", line (\d+)', re.IGNORECASE),
    re.compile(r"\b((?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.[A-Za-z0-9_]+):(\d+)\b"),
)
_ERROR_TYPE_RE = re.compile(r"\b([A-Z][A-Za-z0-9_]*(?:Error|Exception|Failure))\b")
_STACK_FRAME_RE = re.compile(r"\bin ([A-Za-z_][A-Za-z0-9_]*)\b")


def extract_ci_failure_signature(title: str, body: str, summary: str = "") -> str | None:
    explicit = extract_signature_from_body(body)
    if explicit:
        return explicit

    text = "\n".join(part for part in [summary, _extract_failure_text(body), _extract_context_text(body), title] if part).strip()
    if not text:
        return None

    error_type = _extract_error_type(text)
    location = _extract_code_location(text)
    stack_frame = _extract_stack_frame(text)
    checks = _extract_failed_checks(text)

    anchors = [part for part in [error_type, location, stack_frame] if part]
    if len(anchors) < 2:
        return None

    parts = []
    if checks:
        parts.append(f"checks={','.join(checks)}")
    if error_type:
        parts.append(f"error={error_type}")
    if location:
        parts.append(f"location={location}")
    if stack_frame:
        parts.append(f"frame={stack_frame}")
    return " | ".join(parts)


def extract_signature_from_body(body: str) -> str | None:
    match = CI_SIGNATURE_SECTION_RE.search(body or "")
    if not match:
        return None
    return match.group(1).strip() or None


def format_signature_section(signature: str | None) -> str:
    value = str(signature or "").strip()
    if not value:
        return ""
    return f"\n## CI Failure Signature\n{value}\n"


def _extract_failure_text(body: str) -> str:
    match = _REMAINING_FAILURE_RE.search(body or "")
    if match:
        return match.group(1).strip()
    return ""


def _extract_context_text(body: str) -> str:
    match = _CONTEXT_RE.search(body or "")
    if match:
        return match.group(1).strip()
    return ""


def _extract_failed_checks(text: str) -> list[str]:
    seen: set[str] = set()
    checks: list[str] = []
    for raw in _FAILED_CHECK_RE.findall(text or ""):
        normalized = raw.strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        checks.append(normalized)
    return checks


def _extract_error_type(text: str) -> str | None:
    match = _ERROR_TYPE_RE.search(text or "")
    if not match:
        return None
    return match.group(1)


def _extract_code_location(text: str) -> str | None:
    for pattern in _FILE_LINE_PATTERNS:
        match = pattern.search(text or "")
        if not match:
            continue
        return f"{match.group(1)}:{match.group(2)}"
    return None


def _extract_stack_frame(text: str) -> str | None:
    match = _STACK_FRAME_RE.search(text or "")
    if not match:
        return None
    return match.group(1)
