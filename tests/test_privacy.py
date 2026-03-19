import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.privacy import (
    redact_text,
)


def test_redact_text_hides_telegram_token():
    text = "telegram_bot_token: 123456789:ABCDEFGHIJKLMNOPQRSTUV"
    assert "[redacted]" in redact_text(text)
    assert "ABCDEFGHIJKLMNOPQRSTUV" not in redact_text(text)


def test_redact_text_hides_bearer_token():
    text = "Authorization: Bearer abcdefghijklmnopqrstuvwxyz.123456"
    redacted = redact_text(text)
    assert "Bearer" not in redacted
    assert "[redacted]" in redacted


def test_redact_text_hides_env_style_secret_assignment():
    text = "OPENAI_API_KEY=super-secret-value"
    redacted = redact_text(text)
    assert "super-secret-value" not in redacted
    assert "[redacted]" in redacted


def test_redact_text_preserves_non_secret_operational_context():
    text = "DeepSeek provider 'openrouter' failed with exit code 1."
    assert redact_text(text) == text
