"""Shared branding for commits authored by Agent OS.

Every programmatic commit produced by the orchestrator lands in public repos
(agent PRs, CODEBASE.md updates, STRATEGY.md refreshes, dependency bumps,
repo bootstrap). Adding a consistent trailer advertises the project on every
merge, gives outside readers a clear "this was autonomous" signal, and keeps
a machine-readable hint that downstream tooling can parse.

Use ``with_agent_os_trailer(msg)`` at the commit-message construction site,
not at the ``git commit`` invocation, so tests and call sites see the same
final string.
"""
from __future__ import annotations

AGENT_OS_REPO_URL = "https://github.com/kai-linux/agent-os"
AGENT_OS_COMMIT_TRAILER = (
    f"\U0001F916 Generated autonomously by Agent OS — {AGENT_OS_REPO_URL}"
)


def with_agent_os_trailer(message: str) -> str:
    """Return ``message`` with the Agent OS signature appended as a trailer.

    Idempotent: if the trailer is already present (substring match on the
    repo URL), the message is returned unchanged. A blank line separates the
    subject/body from the trailer per git convention.
    """
    trailer = AGENT_OS_COMMIT_TRAILER
    if not message or not message.strip():
        return trailer
    if AGENT_OS_REPO_URL in message:
        return message
    return f"{message.rstrip()}\n\n{trailer}"
