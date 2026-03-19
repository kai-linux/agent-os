from __future__ import annotations

import re

_SECRET_PATTERNS = [
    re.compile(r"\b\d{8,}:[A-Za-z0-9_-]{20,}\b"),  # Telegram bot token
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]+\b"),
    re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*(?:TOKEN|SECRET|PASSWORD|API_KEY|ACCESS_KEY|PRIVATE_KEY)\b\s*[=:]\s*[^\s]+", re.IGNORECASE),
    re.compile(r"(?im)^\s*(telegram_bot_token|telegram_chat_id|api[_-]?key|secret|password)\s*:\s*.+$"),
    re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----.*?-----END [A-Z ]+PRIVATE KEY-----", re.DOTALL),
]


def redact_text(text: str) -> str:
    redacted = text or ""
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub("[redacted]", redacted)
    return redacted
