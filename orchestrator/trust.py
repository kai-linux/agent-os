"""Trusted-author filtering for prompt injection defense.

Only issues created by trusted authors (repo owner + system bot) are ingested
into LLM prompts or dispatched to agents. External issues are visible on GitHub
but never enter the automation pipeline.
"""
from __future__ import annotations

_DEFAULT_TRUSTED = {"kai-linux"}


def trusted_authors(cfg: dict) -> set[str]:
    """Return the set of GitHub logins whose issues may be processed."""
    extra = cfg.get("trusted_authors", [])
    authors = set(_DEFAULT_TRUSTED)
    for a in extra:
        if isinstance(a, str) and a.strip():
            authors.add(a.strip().lower())
    return authors


def is_trusted(login: str | None, cfg: dict) -> bool:
    """Check if a GitHub login is trusted."""
    if not login:
        return False
    return login.strip().lower() in trusted_authors(cfg)
