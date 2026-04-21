from __future__ import annotations

from orchestrator.commit_signature import (
    AGENT_OS_COMMIT_TRAILER,
    AGENT_OS_REPO_URL,
    with_agent_os_trailer,
)


def test_trailer_is_appended_to_simple_subject():
    out = with_agent_os_trailer("Agent OS: task-123")
    assert out.startswith("Agent OS: task-123")
    assert out.endswith(AGENT_OS_COMMIT_TRAILER)
    assert "\n\n" in out  # blank line separates subject from trailer


def test_trailer_preserves_existing_body():
    msg = "chore: refresh CODEBASE.md\n\nDetailed explanation here."
    out = with_agent_os_trailer(msg)
    assert "Detailed explanation here." in out
    assert out.endswith(AGENT_OS_COMMIT_TRAILER)


def test_trailer_is_idempotent():
    once = with_agent_os_trailer("Agent OS: task-123")
    twice = with_agent_os_trailer(once)
    assert once == twice


def test_trailer_substring_match_prevents_duplication():
    # If somebody hand-crafts a message that already links the repo, don't
    # tack on the trailer again (e.g. custom notice).
    msg = f"note: see {AGENT_OS_REPO_URL} for details"
    assert with_agent_os_trailer(msg) == msg


def test_empty_message_returns_trailer_only():
    assert with_agent_os_trailer("") == AGENT_OS_COMMIT_TRAILER
    assert with_agent_os_trailer("   ") == AGENT_OS_COMMIT_TRAILER


def test_trailer_contains_brand_and_link():
    assert "Agent OS" in AGENT_OS_COMMIT_TRAILER
    assert AGENT_OS_REPO_URL in AGENT_OS_COMMIT_TRAILER
