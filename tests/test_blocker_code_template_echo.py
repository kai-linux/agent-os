"""Regression coverage for the 2026-04-23 blocked-task escalation incident.

eigendark-website#77 escalated because every attempt's `.agent_result.md`
contained the prompt template echoed verbatim — including the literal
instruction text "One line. Required when STATUS is partial or blocked.
Use `none` when STATUS is complete." as the value for `BLOCKER_CODE:`.

Three layered root causes are exercised here:

1. The snapshot extractor (``_extract_task_result_snapshot``) read the raw
   log text after ``BLOCKER_CODE:`` without validating it, so the echoed
   prose flowed into the escalation Telegram and the error-pattern
   aggregator — drowning the real signal.

2. The prompt template itself used prose placeholders ("One line...",
   "- bullet") that look plausibly like content. Fixed by replacing them
   with unambiguous ``<...>`` markers + an explicit "do not copy
   placeholders" rule. This test pins the template text so a future
   refactor can't silently reintroduce the prose.

3. Orphan-branch PRs used "Automated changes for issue #N" — not a
   GitHub closing keyword — so merging the PR left its issue open with
   the ``in-progress`` label and kept the escalation poller firing.
"""
from __future__ import annotations

from orchestrator.github_dispatcher import _sanitize_snapshot_blocker_code


def test_sanitize_known_code_passes_through():
    assert _sanitize_snapshot_blocker_code("missing_context") == "missing_context"
    assert _sanitize_snapshot_blocker_code("MISSING_CONTEXT") == "missing_context"
    assert _sanitize_snapshot_blocker_code("  test_failure  ") == "test_failure"


def test_sanitize_empty_or_none_becomes_none():
    assert _sanitize_snapshot_blocker_code("") == "none"
    assert _sanitize_snapshot_blocker_code("none") == "none"
    assert _sanitize_snapshot_blocker_code("- None") == "none"
    assert _sanitize_snapshot_blocker_code("n/a") == "none"


def test_sanitize_rejects_echoed_template_prose():
    """Agent copied the template literally — must not leak into escalation."""
    echoed = "One line. Required when STATUS is partial or blocked. Use `none` when STATUS is complete."
    assert _sanitize_snapshot_blocker_code(echoed) == "invalid_result_contract"


def test_sanitize_rejects_arbitrary_freeform_text():
    assert _sanitize_snapshot_blocker_code("I got stuck on the tests") == "invalid_result_contract"
    assert _sanitize_snapshot_blocker_code("bullet") == "invalid_result_contract"


def test_sanitize_strips_stray_backticks():
    # Some agents wrap the value in backticks because the rules mention
    # `code` formatting for the enum — the sanitizer should see through that.
    assert _sanitize_snapshot_blocker_code("`missing_context`") == "missing_context"


def test_prompt_template_uses_placeholders_not_prose():
    """Pin the prompt template so the prose-placeholder bug can't regress."""
    from orchestrator import queue
    import inspect

    # The template is embedded inside the source of `build_agent_prompt` or
    # similar; scan queue.py module source for the signature strings.
    source = inspect.getsource(queue)
    # Positive: the new <...> placeholders are present.
    assert "<one of: complete, partial, blocked>" in source
    assert "<one blocker code from the Rules list below, or `none` when STATUS is complete>" in source
    assert "Never copy the <...> placeholders into your answer." in source
    # Negative: the old prose placeholders are gone (these were the ones
    # agents kept echoing).
    assert "One line. Required when STATUS is partial or blocked." not in source
    # The dangling "- bullet\n- bullet" placeholder pair is also gone.
    assert "- bullet\n- bullet" not in source


def test_pr_body_uses_closes_keyword_for_linked_issue():
    """Merged PRs must auto-close their linked issue. The old wording
    ('Automated changes for issue #N') is NOT a closing keyword."""
    import inspect
    from orchestrator import pr_monitor, github_sync

    pr_monitor_src = inspect.getsource(pr_monitor)
    github_sync_src = inspect.getsource(github_sync)
    # Both PR-creation paths must use `Closes #` now.
    assert 'f"Closes #{issue_number}' in pr_monitor_src
    assert 'f"Closes #{issue_number}' in github_sync_src
