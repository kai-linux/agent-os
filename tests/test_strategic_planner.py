"""Tests for strategic_planner focus area analysis and configuration."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.strategic_planner import (
    DEFAULT_PLAN_SIZE,
    DEFAULT_SPRINT_CADENCE_DAYS,
    FOCUS_AREA_MARKER,
    PRODUCTION_FEEDBACK_ARTIFACT_DEFAULT,
    RESEARCH_ARTIFACT_DEFAULT,
    SIGNALS_ARTIFACT_DEFAULT,
    _call_sonnet,
    _approval_timeout_hours,
    _allowed_research_file,
    _build_plan_prompt,
    _build_sprint_report,
    _clean_research_text,
    _fallback_sprint_report,
    _has_active_sprint_work,
    _maybe_refresh_backlog_for_early_cycle,
    _create_plan_approval_action,
    _domain_allowed,
    _parse_plan,
    _extract_sprint_entries,
    _format_cadence,
    _format_duration_hours,
    _format_plan_message,
    _gather_cross_repo_context,
    _is_focus_areas_manually_edited,
    _invalidate_pending_action_for_dormant_repo,
    _load_strategy_map,
    _order_repos_by_dependencies,
    _planning_research_context,
    _production_feedback_context,
    _production_priority_signals,
    _planning_signals_context,
    _prioritize_backlog_with_production_feedback,
    _repo_outcome_config,
    _repo_production_feedback_config,
    _recent_outcome_summary,
    _repo_signals_config,
    _read_planning_principles,
    _repo_planner_config,
    _repo_research_config,
    _resolve_repos,
    _apply_production_feedback_to_plan,
    _set_issues_ready,
    _open_issues_summary,
    _planner_allow_early_refresh,
    _strategy_dependencies,
    _summarize_strategy,
    _update_focus_areas_section,
    _analyze_focus_areas,
    _format_sprint_report_message,
    _write_sprint_report_artifact,
)


# ---------------------------------------------------------------------------
# _extract_sprint_entries
# ---------------------------------------------------------------------------

SAMPLE_STRATEGY = textwrap.dedent("""\
    # Strategy — agent-os

    ## Product Vision

    Build an autonomous agent OS.

    ## Current Focus Areas

    <!-- auto-focus-areas -->
    - Improve CI reliability

    ## Sprint History

    ### Sprint 2026-03-19

    **Retrospective:**
    Issues completed:
    - #23: Auto-update focus areas

    **Plan:**
    - [prio:high] Auto-update focus areas: Keep strategy aligned

    ### Sprint 2026-03-12

    **Retrospective:**
    Issues completed:
    - #20: Add backlog groomer

    **Plan:**
    - [prio:high] Backlog groomer: Automate issue creation

    ### Sprint 2026-03-05

    **Plan:**
    - [prio:normal] Queue priority: Age-based scoring
""")


def test_extract_sprint_entries_finds_all():
    entries = _extract_sprint_entries(SAMPLE_STRATEGY)
    assert len(entries) == 3
    assert "Sprint 2026-03-19" in entries[0]
    assert "Sprint 2026-03-12" in entries[1]
    assert "Sprint 2026-03-05" in entries[2]


def test_extract_sprint_entries_empty():
    assert _extract_sprint_entries("# Strategy\n\n## Sprint History\n") == []


def test_extract_sprint_entries_single():
    content = "## Sprint History\n\n### Sprint 2026-03-19\n\n**Plan:**\n- task\n"
    entries = _extract_sprint_entries(content)
    assert len(entries) == 1


# ---------------------------------------------------------------------------
# _is_focus_areas_manually_edited
# ---------------------------------------------------------------------------

def test_auto_marker_present_not_manual():
    content = textwrap.dedent("""\
        ## Current Focus Areas

        <!-- auto-focus-areas -->
        - Theme A
        - Theme B

        ## Sprint History
    """)
    assert _is_focus_areas_manually_edited(content) is False


def test_placeholder_not_manual():
    content = textwrap.dedent("""\
        ## Current Focus Areas

        (Updated each sprint with the key themes being pursued.)

        ## Sprint History
    """)
    assert _is_focus_areas_manually_edited(content) is False


def test_empty_section_not_manual():
    content = "## Current Focus Areas\n\n## Sprint History\n"
    assert _is_focus_areas_manually_edited(content) is False


def test_custom_content_without_marker_is_manual():
    content = textwrap.dedent("""\
        ## Current Focus Areas

        - My custom focus area that I wrote by hand
        - Another hand-written area

        ## Sprint History
    """)
    assert _is_focus_areas_manually_edited(content) is True


def test_no_focus_section():
    content = "## Sprint History\n\n### Sprint 2026-03-19\n"
    assert _is_focus_areas_manually_edited(content) is False


# ---------------------------------------------------------------------------
# _update_focus_areas_section
# ---------------------------------------------------------------------------

def test_update_focus_areas_replaces_auto_content():
    content = textwrap.dedent("""\
        ## Current Focus Areas

        <!-- auto-focus-areas -->
        - Old theme A
        - Old theme B

        ## Sprint History

        ### Sprint 2026-03-19
    """)
    areas = ["New theme X", "New theme Y", "New theme Z"]
    updated = _update_focus_areas_section(content, areas)

    assert "- New theme X" in updated
    assert "- New theme Y" in updated
    assert "- New theme Z" in updated
    assert "- Old theme A" not in updated
    assert FOCUS_AREA_MARKER in updated
    # Sprint history should be preserved
    assert "### Sprint 2026-03-19" in updated


def test_update_focus_areas_replaces_placeholder():
    content = textwrap.dedent("""\
        ## Current Focus Areas

        (Updated each sprint with the key themes being pursued.)

        ## Sprint History
    """)
    areas = ["Theme A", "Theme B", "Theme C"]
    updated = _update_focus_areas_section(content, areas)

    assert "- Theme A" in updated
    assert "(Updated each sprint" not in updated
    assert "## Sprint History" in updated


def test_update_preserves_other_sections():
    content = textwrap.dedent("""\
        # Strategy — test

        ## Product Vision

        Build great software.

        ## Current Focus Areas

        <!-- auto-focus-areas -->
        - Old

        ## Sprint History

        ### Sprint 2026-03-19

        **Plan:**
        - task one
    """)
    updated = _update_focus_areas_section(content, ["New focus"])
    assert "Build great software." in updated
    assert "- task one" in updated
    assert "- New focus" in updated


# ---------------------------------------------------------------------------
# _analyze_focus_areas (mocked LLM)
# ---------------------------------------------------------------------------

def test_analyze_focus_areas_parses_json():
    mock_output = '["CI/CD reliability", "Agent autonomy", "Observability"]'
    with patch("orchestrator.strategic_planner._call_haiku", return_value=mock_output):
        result = _analyze_focus_areas(["sprint 1", "sprint 2", "sprint 3"])
    assert result == ["CI/CD reliability", "Agent autonomy", "Observability"]


def test_analyze_focus_areas_strips_fences():
    mock_output = '```json\n["Theme A", "Theme B", "Theme C"]\n```'
    with patch("orchestrator.strategic_planner._call_haiku", return_value=mock_output):
        result = _analyze_focus_areas(["s1", "s2", "s3"])
    assert result == ["Theme A", "Theme B", "Theme C"]


def test_analyze_focus_areas_caps_at_five():
    mock_output = '["A", "B", "C", "D", "E", "F", "G"]'
    with patch("orchestrator.strategic_planner._call_haiku", return_value=mock_output):
        result = _analyze_focus_areas(["s1", "s2", "s3"])
    assert len(result) == 5


def test_analyze_focus_areas_handles_failure():
    with patch("orchestrator.strategic_planner._call_haiku", side_effect=RuntimeError("fail")):
        result = _analyze_focus_areas(["s1", "s2", "s3"])
    assert result is None


def test_analyze_focus_areas_handles_bad_json():
    with patch("orchestrator.strategic_planner._call_haiku", return_value="not json"):
        result = _analyze_focus_areas(["s1", "s2", "s3"])
    assert result is None


def test_call_sonnet_falls_back_to_codex_when_claude_fails():
    cfg = {}

    def fake_run(cmd, capture_output=True, text=True, timeout=180):
        if os.path.basename(cmd[0]) == "claude":
            return subprocess.CompletedProcess(cmd, 1, "", "You're out of extra usage")
        if os.path.basename(cmd[0]) == "codex":
            return subprocess.CompletedProcess(cmd, 0, '[{"action":"promote","issue_number":39}]', "")
        raise AssertionError(f"Unexpected command: {cmd}")

    with patch("orchestrator.strategic_planner.subprocess.run", side_effect=fake_run):
        raw = _call_sonnet("Return JSON", cfg)

    assert raw == '[{"action":"promote","issue_number":39}]'


def test_call_sonnet_uses_configured_planner_agents():
    cfg = {"planner_agents": ["codex"]}

    def fake_run(cmd, capture_output=True, text=True, timeout=180):
        assert os.path.basename(cmd[0]) == "codex"
        return subprocess.CompletedProcess(cmd, 0, "[]", "")

    with patch("orchestrator.strategic_planner.subprocess.run", side_effect=fake_run):
        raw = _call_sonnet("Return JSON", cfg)

    assert raw == "[]"


def test_parse_plan_supports_explicit_empty_reason():
    plan, reason = _parse_plan('{"empty_reason":"All backlog items are stale or blocked."}')
    assert plan == []
    assert reason == "All backlog items are stale or blocked."


def test_format_cadence_supports_fractional_days():
    assert _format_cadence(0.01) == "every 14m"
    assert _format_cadence(0.5) == "every 12h"
    assert _format_cadence(7) == "every 7d"


def test_approval_timeout_scales_with_cadence():
    assert _approval_timeout_hours(0.01) < 1
    assert _format_duration_hours(_approval_timeout_hours(0.01)) == "12m"
    assert _approval_timeout_hours(7) == 24


def test_format_plan_message_includes_real_cadence_and_buttons_copy():
    text = _format_plan_message(
        [{"priority": "prio:high", "action": "promote", "issue_number": 12, "task_type": "implementation", "title": "Do thing", "rationale": "Because."}],
        "owner/repo",
        0.01,
    )
    assert "📋 Sprint Plan — owner/repo" in text
    assert "Cadence: every 14m" in text
    assert "Tap Approve to apply this plan: move selected backlog issues to Ready." in text
    assert "Auto-skip in 12m if no action." in text


def test_format_plan_message_for_promote_only_plan():
    text = _format_plan_message(
        [{"priority": "prio:high", "action": "promote", "issue_number": 12, "task_type": "implementation", "title": "Do thing", "rationale": "Because."}],
        "owner/repo",
        1,
    )
    assert "Tap Approve to apply this plan: move selected backlog issues to Ready." in text


def test_create_plan_approval_action_uses_dynamic_timeout():
    action = _create_plan_approval_action({"telegram_chat_id": "1"}, "owner/repo", 0.01)
    assert action["timeout_hours"] == _approval_timeout_hours(0.01)


# ---------------------------------------------------------------------------
# Planning research
# ---------------------------------------------------------------------------

def test_repo_research_config_merges_per_repo_override():
    cfg = {
        "planning_research": {
            "enabled": True,
            "max_age_hours": 72,
            "sources": [{"name": "base"}],
        },
        "github_projects": {
            "proj": {
                "repos": [
                    {
                        "github_repo": "owner/repo",
                        "planning_research": {
                            "max_age_hours": 24,
                            "sources": [{"name": "override"}],
                        },
                    }
                ]
            }
        },
    }
    research_cfg = _repo_research_config(cfg, "owner/repo")
    assert research_cfg["enabled"] is True
    assert research_cfg["max_age_hours"] == 24
    assert research_cfg["sources"] == [{"name": "override"}]


def test_domain_allowed_accepts_subdomains():
    assert _domain_allowed("docs.example.com", ["example.com"]) is True
    assert _domain_allowed("evil.com", ["example.com"]) is False


def test_allowed_research_file_allows_repo_and_adjacent_paths(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    inside = _allowed_research_file(repo, "notes.md")
    adjacent = _allowed_research_file(repo, "../shared/notes.md")
    blocked = _allowed_research_file(repo, "../../outside.md")
    assert inside == (repo / "notes.md").resolve()
    assert adjacent == (repo / "../shared/notes.md").resolve()
    assert blocked is None


def test_clean_research_text_strips_html():
    raw = "<html><body><script>bad()</script><h1>Title</h1><p>Hello&nbsp;world</p></body></html>"
    cleaned = _clean_research_text(raw)
    assert "Title" in cleaned
    assert "Hello world" in cleaned
    assert "bad()" not in cleaned


def test_planning_research_context_uses_fresh_artifact(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    artifact = repo / RESEARCH_ARTIFACT_DEFAULT
    artifact.write_text("# Planning Research\n\nFresh artifact\n", encoding="utf-8")
    cfg = {
        "planning_research": {
            "enabled": True,
            "max_age_hours": 72,
            "sources": [{"name": "ignored", "type": "file", "path": "notes.md", "kind": "repo_reference"}],
        }
    }
    context = _planning_research_context(cfg, "owner/repo", repo)
    assert "Fresh artifact" in context


def test_planning_research_context_refreshes_and_writes_artifact(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "notes.md").write_text("Launch week notes for planning.\nNew onboarding flow is missing analytics.\n", encoding="utf-8")
    monkeypatch.setattr(
        "orchestrator.strategic_planner._summarize_research_source",
        lambda source, content: ("Docs show a missing analytics gap.", ["Add analytics before launch."]),
    )
    cfg = {
        "planning_research": {
            "enabled": True,
            "max_age_hours": 0,
            "sources": [
                {
                    "name": "Launch notes",
                    "type": "file",
                    "path": "notes.md",
                    "kind": "repo_reference",
                }
            ],
        }
    }
    context = _planning_research_context(cfg, "owner/repo", repo)
    artifact = repo / RESEARCH_ARTIFACT_DEFAULT
    assert artifact.exists()
    assert "Launch notes" in context
    assert "Add analytics before launch." in context


def test_build_plan_prompt_includes_research_context():
    prompt = _build_plan_prompt(
        plan_size=3,
        strategy_context="strategy",
        readme_goal="goal",
        objectives_context="(no objective configured)",
        north_star="north star",
        planning_principles="north-star rubric",
        evaluation_rubric="",
        codebase_context="codebase",
        production_feedback_context="signals",
        outcome_context="outcomes",
        research_context="# Planning Research\n\nEvidence",
        retrospective="retro",
        git_log="abc123 commit",
        counts={"open": 1, "closed": 2, "blocked": 0},
        metrics_summary="metrics",
        backlog_text="- #1: Task",
        open_issues="(none)",
        cross_repo_context="(single repo)",
    )
    assert "--- Pre-Planning Research (PLANNING_RESEARCH.md) ---" in prompt
    assert "Evidence" in prompt


def test_build_plan_prompt_includes_planning_principles():
    prompt = _build_plan_prompt(
        plan_size=3,
        strategy_context="strategy",
        readme_goal="goal",
        objectives_context="(no objective configured)",
        north_star="north star",
        planning_principles="Prefer autonomy and evidence.",
        evaluation_rubric="",
        codebase_context="codebase",
        production_feedback_context="signals",
        outcome_context="outcomes",
        research_context="research",
        retrospective="retro",
        git_log="abc123 commit",
        counts={"open": 1, "closed": 2, "blocked": 0},
        metrics_summary="metrics",
        backlog_text="- #1: Task",
        open_issues="(none)",
        cross_repo_context="(single repo)",
    )
    assert "--- Stable Planning Principles (PLANNING_PRINCIPLES.md) ---" in prompt
    assert "Prefer autonomy and evidence." in prompt


def test_build_plan_prompt_includes_north_star():
    prompt = _build_plan_prompt(
        plan_size=3,
        strategy_context="strategy",
        readme_goal="goal",
        objectives_context="(no objective configured)",
        north_star="Closed-loop self-improvement.",
        planning_principles="Prefer autonomy and evidence.",
        evaluation_rubric="",
        codebase_context="codebase",
        production_feedback_context="signals",
        outcome_context="outcomes",
        research_context="research",
        retrospective="retro",
        git_log="abc123 commit",
        counts={"open": 1, "closed": 2, "blocked": 0},
        metrics_summary="metrics",
        backlog_text="- #1: Task",
        open_issues="(none)",
        cross_repo_context="(single repo)",
    )
    assert "--- North Star (NORTH_STAR.md) ---" in prompt
    assert "Closed-loop self-improvement." in prompt


def test_repo_signals_config_merges_per_repo_override():
    cfg = {
        "planning_signals": {
            "enabled": True,
            "max_age_hours": 24,
            "inputs": [{"name": "base"}],
        },
        "github_projects": {
            "proj": {
                "repos": [
                    {
                        "github_repo": "owner/repo",
                        "planning_signals": {
                            "max_age_hours": 12,
                            "inputs": [{"name": "override"}],
                        },
                    }
                ]
            }
        },
    }
    signals_cfg = _repo_production_feedback_config(cfg, "owner/repo")
    assert signals_cfg["enabled"] is True
    assert signals_cfg["max_age_hours"] == 12
    assert signals_cfg["inputs"] == [{"name": "override"}]


def test_planning_signals_context_uses_fresh_artifact(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    artifact = repo / PRODUCTION_FEEDBACK_ARTIFACT_DEFAULT
    artifact.write_text("# Production Feedback\n\nFresh signals\n", encoding="utf-8")
    cfg = {
        "production_feedback": {
            "enabled": True,
            "max_age_hours": 24,
            "inputs": [{"name": "ignored", "type": "file", "path": "signals.md", "signal_class": "analytics"}],
        }
    }
    context = _production_feedback_context(cfg, "owner/repo", repo)
    assert "Fresh signals" in context


def test_planning_signals_context_refreshes_and_writes_artifact(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "signals.md").write_text(
        "Activation rate: 32% this week.\nThree users explicitly requested export support.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "orchestrator.strategic_planner._summarize_feedback_input",
        lambda signal, content, freshness_policy: (
            "Activation is low and feedback points to export demand.",
            ["Activation rate 32%", "3 users requested export"],
            ["Prioritize export instrumentation and delivery."],
        ),
    )
    cfg = {
        "production_feedback": {
            "enabled": True,
            "max_age_hours": 0,
            "stale_after_hours": 72,
            "minimum_trust_level": "medium",
            "allowed_privacy_levels": ["public"],
            "inputs": [
                {
                    "name": "Weekly activation snapshot",
                    "type": "file",
                    "path": "signals.md",
                    "signal_class": "analytics",
                    "observed_at": "2026-03-19T08:00:00Z",
                    "provenance": "Derived from public launch-week notes",
                    "trust_level": "high",
                    "trust_note": "Public aggregated metric",
                    "privacy": "public",
                    "privacy_note": "Aggregated counts only",
                }
            ],
        }
    }
    context = _production_feedback_context(cfg, "owner/repo", repo)
    artifact = repo / PRODUCTION_FEEDBACK_ARTIFACT_DEFAULT
    assert artifact.exists()
    assert "Weekly activation snapshot" in context
    assert "Activation rate 32%" in context
    assert "Trust: high (Public aggregated metric)" in context
    assert "Privacy: public (Aggregated counts only)" in context
    assert "Planning Use: included" in context


def test_production_feedback_context_auto_generates_from_runtime_substrate(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    metrics_dir = tmp_path / "runtime" / "metrics"
    metrics_dir.mkdir(parents=True)
    base = datetime.now(tz=timezone.utc) - timedelta(hours=4)
    (metrics_dir / "agent_stats.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": base.isoformat(),
                        "task_id": "task-blocked",
                        "repo": "owner/repo",
                        "agent": "codex",
                        "status": "blocked",
                        "blocker_code": "missing_credentials",
                        "attempt_count": 1,
                    }
                ),
                json.dumps(
                    {
                        "timestamp": (base + timedelta(hours=1)).isoformat(),
                        "task_id": "task-recovered",
                        "repo": "owner/repo",
                        "agent": "claude",
                        "status": "partial",
                        "blocker_code": "environment_failure",
                        "attempt_count": 1,
                    }
                ),
                json.dumps(
                    {
                        "timestamp": (base + timedelta(hours=2)).isoformat(),
                        "task_id": "task-recovered",
                        "repo": "owner/repo",
                        "agent": "claude",
                        "status": "complete",
                        "attempt_count": 2,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (metrics_dir / "outcome_attribution.jsonl").write_text(
        json.dumps(
            {
                "timestamp": (base + timedelta(hours=3)).isoformat(),
                "repo": "owner/repo",
                "interpretation": "improved",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    cfg = {"root_dir": str(tmp_path)}

    context = _production_feedback_context(cfg, "owner/repo", repo)
    artifact = repo / PRODUCTION_FEEDBACK_ARTIFACT_DEFAULT

    assert artifact.exists()
    assert "## Recent Failures" in context
    assert "## Blocked-Task Patterns" in context
    assert "## Repeat-Recovery Signals" in context
    assert "missing_credentials: 1" in context
    assert "completed after retry: 1" in context
    assert "improved: 1" in context


def test_production_feedback_context_writes_no_signals_artifact_when_substrate_empty(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = {"root_dir": str(tmp_path)}

    context = _production_feedback_context(cfg, "owner/repo", repo)
    artifact = repo / PRODUCTION_FEEDBACK_ARTIFACT_DEFAULT

    assert artifact.exists()
    assert "No recent failures were recorded" in context
    assert "No repeated blocked-task pattern was detected" in context
    assert "No repeat-recovery signals were recorded" in context


def test_fallback_sprint_report_extracts_counts_and_next_focus():
    retrospective = textwrap.dedent("""\
        Issues completed:
        - #10: Stabilize queue — completed
        - #11: Add reporting — completed

        PRs merged:
        - PR #12: Agent: improve queue recovery

        Outcome evidence:
        - #10 / PR #12 / reliability: improved — Retry loops recovered.
        - #11 / PR #13 / visibility: inconclusive — No measurable external metric.
    """)
    sprint_summary = textwrap.dedent("""\
        - [prio:high] Add sprint report artifact: Make sprint movement visible
        - [prio:normal] Tighten outcome checks: Reduce inconclusive reporting
    """)

    report = _fallback_sprint_report(
        retrospective,
        sprint_summary,
        readme_goal="Ship reliable autonomous work.",
        north_star="Closed-loop improvement with visible trust signals.",
    )

    assert "2 issue(s)" in report["headline"]
    assert "1 PR(s)" in report["headline"]
    assert any("Shipped execution moved forward" in item for item in report["progress_points"])
    assert any("inconclusive" in item.lower() for item in report["risks_and_gaps"])
    assert report["next_sprint_focus"] == ["Add sprint report artifact", "Tighten outcome checks"]


def test_build_sprint_report_uses_model_output(monkeypatch):
    monkeypatch.setattr(
        "orchestrator.strategic_planner._call_haiku",
        lambda prompt: json.dumps(
            {
                "headline": "Reliability work shipped and planning got more visible.",
                "movement_summary": "The sprint improved execution reliability and clarified strategic reporting.",
                "progress_points": ["Closed high-priority reliability work."],
                "risks_and_gaps": ["Outcome evidence is still sparse."],
                "next_sprint_focus": ["Expand outcome coverage."],
            }
        ),
    )

    report = _build_sprint_report(
        readme_goal="Ship reliable autonomous work.",
        north_star="Closed-loop improvement with visible trust signals.",
        strategy_context="Improve reliability and trust.",
        retrospective="Issues completed:\n- #1: Fix queue",
        sprint_summary="- [prio:high] Add report artifact: Improve visibility",
    )

    assert report["headline"] == "Reliability work shipped and planning got more visible."
    assert report["progress_points"] == ["Closed high-priority reliability work."]


def test_write_sprint_report_artifact_writes_markdown(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    committed = {}

    def _capture_commit(repo_path, relative_path, target_content, commit_message, success_message):
        committed["repo_path"] = repo_path
        committed["relative_path"] = relative_path
        committed["target_content"] = target_content
        committed["commit_message"] = commit_message
        committed["success_message"] = success_message

    monkeypatch.setattr("orchestrator.strategic_planner._commit_repo_markdown_with_retry", _capture_commit)

    _write_sprint_report_artifact(
        repo,
        readme_goal="Ship reliable autonomous work.",
        north_star="Closed-loop improvement with visible trust signals.",
        retrospective="Issues completed:\n- #1: Fix queue",
        sprint_summary="- [prio:high] Add report artifact: Improve visibility",
        report={
            "headline": "Visibility improved.",
            "movement_summary": "The sprint improved oversight of delivered work.",
            "progress_points": ["A direct report artifact is now generated."],
            "risks_and_gaps": ["Outcome measurement remains incomplete."],
            "next_sprint_focus": ["Expand outcome-linked reporting."],
        },
    )

    artifact = repo / "SPRINT_REPORT.md"
    assert artifact.exists()
    content = artifact.read_text(encoding="utf-8")
    assert "# Sprint Report" in content
    assert "## How This Sprint Moved The Repo Forward" in content
    assert "Visibility improved." in content
    assert committed["relative_path"] == Path("SPRINT_REPORT.md")


def test_production_priority_signals_extract_three_fresh_metrics():
    now = datetime.now(tz=timezone.utc).replace(microsecond=0)
    context = textwrap.dedent(f"""\
        # Production Feedback

        - Generated: {now.strftime("%Y-%m-%d %H:%M UTC")}
        - Refresh after: 24h
        - Default stale after: 72h

        ## Recent Failures

        - Observed At: {now.strftime("%Y-%m-%d %H:%M UTC")}
        - Planning Use: included

        ### Key Evidence

        - Blocked outcomes: 3
        - Partial outcomes: 1

        ### Planning Implications

        - Prefer reliability work.

        ## Blocked-Task Patterns

        - Observed At: {now.strftime("%Y-%m-%d %H:%M UTC")}
        - Planning Use: included

        ### Key Evidence

        - missing_credentials: 2

        ### Planning Implications

        - Remove recurring blockers.

        ## Repeat-Recovery Signals

        - Observed At: {now.strftime("%Y-%m-%d %H:%M UTC")}
        - Planning Use: included

        ### Key Evidence

        - completed after retry: 2
        - inconclusive: 1

        ### Planning Implications

        - Tighten measurement loops.
    """)

    signals = _production_priority_signals(context)

    assert [signal["name"] for signal in signals] == [
        "Recent Failures",
        "Blocked-Task Patterns",
        "Repeat-Recovery Signals",
    ]
    assert signals[0]["detail"] == "blocked=3, partial=1"
    assert signals[1]["detail"] == "missing_credentials=2"
    assert "completed after retry=2" in signals[2]["detail"]


def test_production_priority_signals_ignore_stale_sections():
    stale = datetime.now(tz=timezone.utc) - timedelta(days=10)
    context = textwrap.dedent(f"""\
        # Production Feedback

        - Generated: {stale.strftime("%Y-%m-%d %H:%M UTC")}
        - Refresh after: 24h
        - Default stale after: 24h

        ## Recent Failures

        - Observed At: {stale.strftime("%Y-%m-%d %H:%M UTC")}
        - Planning Use: included

        ### Key Evidence

        - Blocked outcomes: 5

        ### Planning Implications

        - Prefer reliability work.
    """)

    assert _production_priority_signals(context) == []


def test_prioritize_backlog_with_production_feedback_reorders_matching_issues():
    now = datetime.now(tz=timezone.utc).replace(microsecond=0)
    context = textwrap.dedent(f"""\
        # Production Feedback

        - Generated: {now.strftime("%Y-%m-%d %H:%M UTC")}
        - Refresh after: 24h
        - Default stale after: 72h

        ## Recent Failures

        - Observed At: {now.strftime("%Y-%m-%d %H:%M UTC")}
        - Planning Use: included

        ### Key Evidence

        - Blocked outcomes: 2
        - Partial outcomes: 1

        ### Planning Implications

        - Prefer reliability work.

        ## Blocked-Task Patterns

        - Observed At: {now.strftime("%Y-%m-%d %H:%M UTC")}
        - Planning Use: included

        ### Key Evidence

        - environment_failure: 2

        ### Planning Implications

        - Remove environment blockers.

        ## Repeat-Recovery Signals

        - Observed At: {now.strftime("%Y-%m-%d %H:%M UTC")}
        - Planning Use: included

        ### Key Evidence

        - completed after retry: 2

        ### Planning Implications

        - Tighten retry loops.
    """)
    backlog = [
        {
            "number": 11,
            "title": "Polish landing page copy",
            "body": "Improve docs and marketing tone.",
            "labels": [{"name": "enhancement"}],
            "createdAt": "2026-03-30T09:00:00Z",
        },
        {
            "number": 12,
            "title": "Fix CI environment failure recovery",
            "body": "Improve retry handling for blocked workflows and runtime failures.",
            "labels": [{"name": "bug"}],
            "createdAt": "2026-03-30T10:00:00Z",
        },
    ]

    ranked = _prioritize_backlog_with_production_feedback(backlog, context)

    assert [issue["number"] for issue in ranked] == [12, 11]
    assert ranked[0]["_production_feedback_score"] > ranked[1].get("_production_feedback_score", 0)
    assert "Recent Failures (blocked=2, partial=1)" in ranked[0]["_production_feedback_reasons"]


def test_apply_production_feedback_to_plan_reorders_and_annotations():
    backlog = [
        {
            "number": 21,
            "title": "Polish landing page copy",
            "body": "Improve docs and marketing tone.",
            "labels": [{"name": "enhancement"}],
            "_production_feedback_score": 0,
            "_production_feedback_reasons": [],
        },
        {
            "number": 22,
            "title": "Add outcome metrics for retry recovery",
            "body": "Track recovery and outcome attribution after retries.",
            "labels": [{"name": "tech-debt"}],
            "_production_feedback_score": 6,
            "_production_feedback_reasons": [
                "Repeat-Recovery Signals (completed after retry=2, inconclusive=1)"
            ],
        },
    ]
    plan = [
        {
            "action": "promote",
            "issue_number": 21,
            "title": "Polish landing page copy",
            "task_type": "docs",
            "priority": "prio:high",
            "rationale": "Improves public clarity.",
        },
        {
            "action": "promote",
            "issue_number": 22,
            "title": "Add outcome metrics for retry recovery",
            "task_type": "implementation",
            "priority": "prio:normal",
            "rationale": "Improves observability.",
        },
    ]

    updated = _apply_production_feedback_to_plan(plan, backlog)

    assert [task["issue_number"] for task in updated] == [22, 21]
    assert updated[0]["priority"] == "prio:high"
    assert "Production signals: Repeat-Recovery Signals" in updated[0]["rationale"]


def test_build_plan_prompt_includes_production_feedback():
    prompt = _build_plan_prompt(
        plan_size=3,
        strategy_context="strategy",
        readme_goal="goal",
        objectives_context="(no objective configured)",
        north_star="north star",
        planning_principles="Prefer autonomy and evidence.",
        evaluation_rubric="",
        codebase_context="codebase",
        production_feedback_context="# Production Feedback\n\nActivation dropped 20%.",
        outcome_context="Outcome evidence here.",
        research_context="research",
        retrospective="retro",
        git_log="abc123 commit",
        counts={"open": 1, "closed": 2, "blocked": 0},
        metrics_summary="metrics",
        backlog_text="- #1: Task",
        open_issues="(none)",
        cross_repo_context="(single repo)",
    )
    assert "--- Production Feedback (PRODUCTION_FEEDBACK.md) ---" in prompt
    assert "--- Recent Outcome Evidence ---" in prompt
    assert "Activation dropped 20%." in prompt


def test_production_feedback_context_guards_stale_low_trust_private_inputs(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "signals.md").write_text("Incident summary with sensitive details.\n", encoding="utf-8")
    monkeypatch.setattr(
        "orchestrator.strategic_planner._summarize_feedback_input",
        lambda signal, content, freshness_policy: (
            "Availability degraded last week.",
            ["2 incidents", "99.1% availability"],
            ["Prioritize reliability work."],
        ),
    )
    cfg = {
        "production_feedback": {
            "enabled": True,
            "max_age_hours": 0,
            "stale_after_hours": 24,
            "minimum_trust_level": "medium",
            "allowed_privacy_levels": ["public"],
            "inputs": [
                {
                    "name": "Weekly incident export",
                    "type": "file",
                    "path": "signals.md",
                    "signal_class": "incident_slo",
                    "observed_at": "2026-03-10T08:00:00Z",
                    "trust_level": "low",
                    "privacy": "restricted",
                }
            ],
        }
    }
    context = _production_feedback_context(cfg, "owner/repo", repo)
    assert "Planning Use: guarded" in context
    assert "Guarded:" in context
    assert "trust below minimum" in context
    assert "privacy level restricted not allowed" in context


def test_repo_outcome_config_includes_external_objective_checks(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    objectives_dir = tmp_path / "objectives"
    objectives_dir.mkdir()
    (objectives_dir / "repo.yaml").write_text(
        textwrap.dedent("""\
            repo: owner/repo
            evaluation_window_days: 28
            metrics:
              - id: conversion
                name: Signup conversion
                source:
                  type: file
                  path: /tmp/conversion-latest.json
                outcome_check:
                  type: file
                  path: /tmp/conversion-post.json
        """),
        encoding="utf-8",
    )
    cfg = {
        "objectives_dir": str(objectives_dir),
        "github_projects": {
            "demo": {
                "repos": [
                    {
                        "github_repo": "owner/repo",
                        "path": str(repo),
                        "outcome_attribution": {"enabled": True, "checks": []},
                    }
                ]
            }
        },
    }

    outcome_cfg = _repo_outcome_config(cfg, "owner/repo")

    assert outcome_cfg["enabled"] is True
    assert outcome_cfg["checks"][0]["id"] == "conversion"


def test_recent_outcome_summary_refreshes_snapshot(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = {
        "root_dir": str(tmp_path),
        "outcome_attribution": {
            "enabled": True,
            "checks": [
                {
                    "id": "activation",
                    "name": "Activation rate",
                    "type": "file",
                    "path": "outcomes.txt",
                    "measurement_window_days": 0,
                    "comparison_window": "Compare 7 days after merge vs 7 days before merge",
                }
            ],
        },
    }
    (repo / "outcomes.txt").write_text("Activation improved from 20% to 28%.", encoding="utf-8")
    metrics_dir = tmp_path / "runtime" / "metrics"
    metrics_dir.mkdir(parents=True)
    (metrics_dir / "outcome_attribution.jsonl").write_text(
        json.dumps(
            {
                "record_type": "attribution",
                "event": "merged",
                "repo": "owner/repo",
                "task_id": "task-123",
                "issue_number": 64,
                "pr_number": 70,
                "merged_at": "2026-03-10T09:00:00+00:00",
                "outcome_check_ids": ["activation"],
                "timestamp": "2026-03-10T09:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "orchestrator.strategic_planner._summarize_outcome_snapshot",
        lambda check, content: ("Activation improved from 20% to 28%.", "improved"),
    )

    summary = _recent_outcome_summary(cfg, "owner/repo", repo, days=30)

    assert "Activation rate: improved" in summary
    logged = (metrics_dir / "outcome_attribution.jsonl").read_text(encoding="utf-8")
    assert '"record_type": "snapshot"' in logged


def test_recent_outcome_summary_marks_missing_checks_inconclusive(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = {
        "root_dir": str(tmp_path),
        "outcome_attribution": {"enabled": True, "checks": []},
    }
    metrics_dir = tmp_path / "runtime" / "metrics"
    metrics_dir.mkdir(parents=True)
    (metrics_dir / "outcome_attribution.jsonl").write_text(
        json.dumps(
            {
                "record_type": "attribution",
                "event": "merged",
                "repo": "owner/repo",
                "task_id": "task-123",
                "issue_number": 64,
                "pr_number": 70,
                "merged_at": "2026-03-10T09:00:00+00:00",
                "outcome_check_ids": [],
                "timestamp": "2026-03-10T09:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    summary = _recent_outcome_summary(cfg, "owner/repo", repo, days=30)

    assert "inconclusive" in summary


# ---------------------------------------------------------------------------
# _repo_planner_config
# ---------------------------------------------------------------------------

def test_repo_planner_config_defaults():
    """Returns defaults when no config is set."""
    plan_size, cadence = _repo_planner_config({}, "owner/repo")
    assert plan_size == DEFAULT_PLAN_SIZE
    assert cadence == DEFAULT_SPRINT_CADENCE_DAYS


def test_repo_planner_config_top_level():
    """Top-level plan_size and sprint_cadence_days are used."""
    cfg = {"plan_size": 8, "sprint_cadence_days": 14}
    plan_size, cadence = _repo_planner_config(cfg, "owner/repo")
    assert plan_size == 8
    assert cadence == 14.0


def test_repo_planner_config_per_repo_override():
    """Per-repo config in github_projects overrides top-level."""
    cfg = {
        "plan_size": 5,
        "sprint_cadence_days": 7,
        "github_projects": {
            "proj1": {
                "repos": [
                    {
                        "github_repo": "owner/repo-a",
                        "plan_size": 10,
                        "sprint_cadence_days": 14,
                    },
                    {"github_repo": "owner/repo-b"},
                ]
            }
        },
    }
    # repo-a gets overrides
    plan_size, cadence = _repo_planner_config(cfg, "owner/repo-a")
    assert plan_size == 10
    assert cadence == 14.0

    # repo-b falls back to top-level
    plan_size, cadence = _repo_planner_config(cfg, "owner/repo-b")
    assert plan_size == 5
    assert cadence == 7.0

    # unknown repo falls back to top-level
    plan_size, cadence = _repo_planner_config(cfg, "owner/other")
    assert plan_size == 5
    assert cadence == 7.0


def test_repo_planner_config_supports_fractional_and_dormant_cadence():
    cfg = {
        "sprint_cadence_days": 1,
        "github_projects": {
            "proj1": {
                "repos": [
                    {"github_repo": "owner/repo-a", "sprint_cadence_days": 0.5},
                    {"github_repo": "owner/repo-b", "sprint_cadence_days": 0},
                ]
            }
        },
    }
    _, cadence_a = _repo_planner_config(cfg, "owner/repo-a")
    _, cadence_b = _repo_planner_config(cfg, "owner/repo-b")
    assert cadence_a == 0.5
    assert cadence_b == 0.0


def test_planner_allow_early_refresh_defaults_true():
    assert _planner_allow_early_refresh({}, "owner/repo") is True


def test_planner_allow_early_refresh_honors_top_level_setting():
    cfg = {"planner_allow_early_refresh": False}
    assert _planner_allow_early_refresh(cfg, "owner/repo") is False


def test_planner_allow_early_refresh_honors_project_and_repo_overrides():
    cfg = {
        "planner_allow_early_refresh": True,
        "github_projects": {
            "proj1": {
                "planner_allow_early_refresh": False,
                "repos": [
                    {"github_repo": "owner/repo-a"},
                    {"github_repo": "owner/repo-b", "planner_allow_early_refresh": True},
                ],
            }
        },
    }
    assert _planner_allow_early_refresh(cfg, "owner/repo-a") is False
    assert _planner_allow_early_refresh(cfg, "owner/repo-b") is True
    assert _planner_allow_early_refresh(cfg, "owner/other") is True


def test_resolve_repos_prefers_explicit_github_projects_local_repo():
    cfg = {
        "github_owner": "kai-linux",
        "allowed_repos": [
            "/home/kai/agent-os",
            "/home/kai/bookgenerator",
        ],
        "github_repos": {
            "writeaibook": "kai-linux/bookgenerator",
            "agent-os": "kai-linux/agent-os",
        },
        "github_projects": {
            "writeaibook": {
                "repos": [
                    {
                        "github_repo": "kai-linux/bookgenerator",
                        "local_repo": "/home/kai/bookgenerator",
                    }
                ]
            }
        },
    }
    repos = dict(_resolve_repos(cfg))
    assert repos["kai-linux/bookgenerator"] == Path("/home/kai/bookgenerator")


def test_open_issues_summary_only_includes_active_issues(monkeypatch):
    raw_issues = [
        {
            "number": 37,
            "title": "Backlog item",
            "labels": [{"name": "prio:high"}],
            "author": {"login": "kai-linux"},
        },
        {
            "number": 38,
            "title": "Active item",
            "labels": [{"name": "in-progress"}],
            "author": {"login": "kai-linux"},
        },
    ]
    monkeypatch.setattr("orchestrator.strategic_planner._gh", lambda *args, **kwargs: json.dumps(raw_issues))
    summary = _open_issues_summary("owner/repo", {"trusted_authors": ["kai-linux"]})
    assert "#38" in summary
    assert "#37" not in summary


def test_has_active_sprint_work_detects_ready_issue(monkeypatch):
    raw_issues = [
        {
            "number": 39,
            "labels": [{"name": "Ready"}],
            "author": {"login": "kai-linux"},
        }
    ]
    monkeypatch.setattr(
        "orchestrator.strategic_planner._gh",
        lambda *args, **kwargs: json.dumps(raw_issues) if args[0][0] == "issue" else "[]",
    )
    active, reason = _has_active_sprint_work("owner/repo", {"trusted_authors": ["kai-linux"]})
    assert active is True
    assert reason == "active issue #39"


def test_has_active_sprint_work_ignores_blocked_issue_with_stale_active_labels(monkeypatch):
    raw_issues = [
        {
            "number": 40,
            "labels": [{"name": "blocked"}, {"name": "in-progress"}, {"name": "agent-dispatched"}],
            "author": {"login": "kai-linux"},
        }
    ]
    monkeypatch.setattr(
        "orchestrator.strategic_planner._gh",
        lambda *args, **kwargs: json.dumps(raw_issues) if args[0][0] == "issue" else "[]",
    )
    active, reason = _has_active_sprint_work("owner/repo", {"trusted_authors": ["kai-linux"]})
    assert active is False
    assert reason == "no active sprint work"


def test_maybe_refresh_backlog_for_early_cycle_runs_groomer_when_due(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr("orchestrator.strategic_planner._backlog_issues", lambda *args, **kwargs: [])
    calls = []
    monkeypatch.setattr(
        "orchestrator.strategic_planner.groom_repo",
        lambda cfg, slug, path: calls.append((slug, path)) or {"status": "created"},
    )
    recorded = []
    monkeypatch.setattr(
        "orchestrator.strategic_planner.record_run",
        lambda cfg, job_name, github_slug: recorded.append((job_name, github_slug)),
    )

    should_plan, reason = _maybe_refresh_backlog_for_early_cycle({}, "owner/repo", repo)

    assert should_plan is True
    assert reason == "early-complete with groomer refresh (created)"
    assert calls == [("owner/repo", repo)]
    assert recorded == [("backlog_groomer", "owner/repo")]


def test_maybe_refresh_backlog_for_early_cycle_uses_existing_backlog(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(
        "orchestrator.strategic_planner._backlog_issues",
        lambda *args, **kwargs: [{"number": 39}, {"number": 40}],
    )
    groom_calls = []
    monkeypatch.setattr(
        "orchestrator.strategic_planner.groom_repo",
        lambda cfg, slug, path: groom_calls.append((slug, path)) or {"status": "created"},
    )

    should_plan, reason = _maybe_refresh_backlog_for_early_cycle({}, "owner/repo", repo)

    assert should_plan is True
    assert reason == "early-complete with existing backlog (2 candidates)"
    assert groom_calls == []


def test_invalidate_pending_action_for_dormant_repo(monkeypatch, tmp_path, capsys):
    actions_dir = tmp_path / "actions"
    actions_dir.mkdir()
    saved = []
    monkeypatch.setattr(
        "orchestrator.strategic_planner.save_telegram_action",
        lambda path, action: saved.append((path, dict(action))),
    )
    action = {"action_id": "abc123", "status": "pending", "repo": "owner/repo"}

    _invalidate_pending_action_for_dormant_repo({"TELEGRAM_ACTIONS": actions_dir}, action, "owner/repo")

    out = capsys.readouterr().out
    assert "Skipping owner/repo: dormant" in out
    assert saved
    saved_path, saved_action = saved[0]
    assert saved_path == actions_dir
    assert saved_action["status"] == "invalid"
    assert saved_action["invalid_reason"] == "repo is dormant"


def test_read_planning_principles_falls_back_to_default(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    text = _read_planning_principles(repo)
    assert "autonomy" in text.lower()
    assert "evidence" in text.lower()


def test_set_issues_ready_adds_new_issue_to_project(monkeypatch):
    cfg = {
        "github_owner": "owner",
        "github_projects": {
            "proj": {
                "project_number": 1,
                "ready_value": "Ready",
                "repos": [{"github_repo": "owner/repo"}],
            }
        },
    }
    infos = iter([
        {
            "project_id": "project-1",
            "status_field_id": "status-field",
            "status_options": {"Ready": "ready-option"},
            "items": [],
        },
        {
            "project_id": "project-1",
            "status_field_id": "status-field",
            "status_options": {"Ready": "ready-option"},
            "items": [{"url": "https://github.com/owner/repo/issues/7", "item_id": "item-7", "number": 7}],
        },
    ])
    monkeypatch.setattr("orchestrator.strategic_planner.query_project", lambda *args, **kwargs: next(infos))
    subprocess_calls = []
    monkeypatch.setattr(
        "orchestrator.strategic_planner.subprocess.run",
        lambda cmd, capture_output=True, text=True: subprocess_calls.append(cmd) or subprocess.CompletedProcess(cmd, 0, "{}", ""),
    )
    status_calls = []
    monkeypatch.setattr("orchestrator.strategic_planner.set_item_status", lambda *args: status_calls.append(args))
    monkeypatch.setattr("orchestrator.strategic_planner.ensure_labels", lambda *args, **kwargs: None)
    monkeypatch.setattr("orchestrator.strategic_planner.edit_issue_labels", lambda *args, **kwargs: None)

    _set_issues_ready(cfg, "owner/repo", ["https://github.com/owner/repo/issues/7"])

    assert any(cmd[:4] == ["gh", "project", "item-add", "1"] for cmd in subprocess_calls)
    assert status_calls == [("project-1", "item-7", "status-field", "ready-option")]


# ---------------------------------------------------------------------------
# _summarize_strategy
# ---------------------------------------------------------------------------

def test_summarize_strategy_extracts_key_sections():
    summary = _summarize_strategy(SAMPLE_STRATEGY)
    assert "Build an autonomous agent OS" in summary
    assert "Improve CI reliability" in summary
    assert "Sprint 2026-03-19" in summary


def test_summarize_strategy_empty():
    assert _summarize_strategy("") == "(no strategy yet)"


def test_summarize_strategy_no_sections():
    content = "# Strategy\n\nSome random text with no standard sections."
    summary = _summarize_strategy(content)
    assert summary == "(no strategy yet)"


def test_summarize_strategy_skips_placeholder_focus():
    content = textwrap.dedent("""\
        ## Product Vision

        Build tools.

        ## Current Focus Areas

        (Updated each sprint with the key themes being pursued.)

        ## Sprint History
    """)
    summary = _summarize_strategy(content)
    assert "Build tools" in summary
    assert "Updated each sprint" not in summary


def test_summarize_strategy_respects_char_limit():
    """Summary is capped at CROSS_REPO_SUMMARY_MAX_CHARS."""
    from orchestrator.strategic_planner import CROSS_REPO_SUMMARY_MAX_CHARS
    long_vision = "X" * 3000
    content = f"## Product Vision\n\n{long_vision}\n\n## Sprint History\n"
    summary = _summarize_strategy(content)
    assert len(summary) <= CROSS_REPO_SUMMARY_MAX_CHARS


# ---------------------------------------------------------------------------
# _gather_cross_repo_context
# ---------------------------------------------------------------------------

def test_gather_cross_repo_context_excludes_current_repo(tmp_path):
    """Cross-repo context should not include the repo being planned."""
    # Create two repo dirs with STRATEGY.md
    repo_a = tmp_path / "repo-a"
    repo_a.mkdir()
    (repo_a / "STRATEGY.md").write_text(
        "## Product Vision\n\nRepo A builds APIs.\n\n## Sprint History\n"
    )
    repo_b = tmp_path / "repo-b"
    repo_b.mkdir()
    (repo_b / "STRATEGY.md").write_text(
        "## Product Vision\n\nRepo B builds UI.\n\n## Sprint History\n"
    )

    repos = [("owner/repo-a", repo_a), ("owner/repo-b", repo_b)]

    # When planning repo-a, context should only include repo-b
    ctx = _gather_cross_repo_context(repos, "owner/repo-a")
    assert "owner/repo-b" in ctx
    assert "Repo B builds UI" in ctx
    assert "owner/repo-a" not in ctx
    assert "Repo A builds APIs" not in ctx


def test_gather_cross_repo_context_single_repo(tmp_path):
    """Returns empty string when there's only one repo."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "STRATEGY.md").write_text("## Product Vision\n\nSolo repo.\n")

    ctx = _gather_cross_repo_context([("owner/repo", repo)], "owner/repo")
    assert ctx == ""


def test_gather_cross_repo_context_missing_strategy(tmp_path):
    """Handles repos without STRATEGY.md gracefully."""
    repo_a = tmp_path / "repo-a"
    repo_a.mkdir()
    # No STRATEGY.md for repo-a

    repo_b = tmp_path / "repo-b"
    repo_b.mkdir()
    (repo_b / "STRATEGY.md").write_text(
        "## Product Vision\n\nRepo B vision.\n\n## Sprint History\n"
    )

    repos = [("owner/repo-a", repo_a), ("owner/repo-b", repo_b)]
    ctx = _gather_cross_repo_context(repos, "owner/repo-a")
    assert "owner/repo-b" in ctx
    assert "Repo B vision" in ctx


def test_gather_cross_repo_context_multiple_siblings(tmp_path):
    """Includes all sibling repos in context."""
    repos = []
    for name in ["repo-a", "repo-b", "repo-c"]:
        d = tmp_path / name
        d.mkdir()
        (d / "STRATEGY.md").write_text(
            f"## Product Vision\n\n{name} vision.\n\n## Sprint History\n"
        )
        repos.append((f"owner/{name}", d))

    ctx = _gather_cross_repo_context(repos, "owner/repo-b")
    assert "owner/repo-a" in ctx
    assert "repo-a vision" in ctx
    assert "owner/repo-c" in ctx
    assert "repo-c vision" in ctx
    assert "owner/repo-b" not in ctx


def test_strategy_dependencies_detects_explicit_repo_links():
    strategy_map = {
        "owner/repo-a": textwrap.dedent("""\
            ## Product Vision

            API service.

            ## Cross-Repo Dependencies

            - owner/repo-b depends on owner/repo-a shipping the auth API.
        """),
        "owner/repo-b": textwrap.dedent("""\
            ## Product Vision

            UI app.

            ## Dependencies

            - Depends on owner/repo-a exposing the auth API.
        """),
        "owner/repo-c": "## Product Vision\n\nBackground jobs.\n",
    }
    deps = _strategy_dependencies(list(strategy_map), strategy_map)
    assert deps["owner/repo-b"] == {"owner/repo-a"}
    assert deps["owner/repo-a"] == set()
    assert deps["owner/repo-c"] == set()


def test_order_repos_by_dependencies_puts_prereqs_first(tmp_path):
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_c = tmp_path / "repo-c"
    for repo in [repo_a, repo_b, repo_c]:
        repo.mkdir()

    repos = [
        ("owner/repo-b", repo_b),
        ("owner/repo-c", repo_c),
        ("owner/repo-a", repo_a),
    ]
    deps = {
        "owner/repo-a": set(),
        "owner/repo-b": {"owner/repo-a"},
        "owner/repo-c": {"owner/repo-b"},
    }
    ordered = _order_repos_by_dependencies(repos, deps)
    assert [slug for slug, _ in ordered] == [
        "owner/repo-a",
        "owner/repo-b",
        "owner/repo-c",
    ]


def test_gather_cross_repo_context_includes_dependency_relationships(tmp_path):
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()
    (repo_a / "STRATEGY.md").write_text(
        "## Product Vision\n\nRepo A builds APIs.\n\n## Sprint History\n"
    )
    (repo_b / "STRATEGY.md").write_text(
        "## Product Vision\n\nRepo B builds UI.\n\n## Dependencies\n\n- Depends on owner/repo-a auth API.\n"
    )
    repos = [("owner/repo-a", repo_a), ("owner/repo-b", repo_b)]
    strategy_map = _load_strategy_map(repos)
    deps = _strategy_dependencies([slug for slug, _ in repos], strategy_map)

    ctx = _gather_cross_repo_context(
        repos,
        "owner/repo-b",
        strategy_map=strategy_map,
        dependencies=deps,
    )

    assert "Prerequisites for this repo: owner/repo-a" in ctx
    assert "owner/repo-a (prerequisite for current repo)" in ctx


# ---------------------------------------------------------------------------
# _format_sprint_report_message
# ---------------------------------------------------------------------------


def test_format_sprint_report_message_basic():
    report = {
        "headline": "Shipped 5 issues and hardened CI recovery.",
        "movement_summary": "The sprint advanced reliability from Level 2 toward Level 3.",
        "progress_points": ["Fixed cascading CI failures", "Added health-gated dispatch"],
        "risks_and_gaps": ["Outcome measurement still inconclusive"],
        "next_sprint_focus": ["Configure external outcome checks", "Close feedback loop"],
    }
    msg = _format_sprint_report_message(report, "owner/repo")
    assert "📊 Sprint Report — owner/repo" in msg
    assert "Shipped 5 issues" in msg
    assert "🧭 Vision Progress" in msg
    assert "Level 2 toward Level 3" in msg
    assert "• Fixed cascading CI failures" in msg
    assert "• Outcome measurement still inconclusive" in msg
    assert "• Configure external outcome checks" in msg


def test_format_sprint_report_message_empty_fields():
    report = {}
    msg = _format_sprint_report_message(report, "owner/repo")
    assert "📊 Sprint Report — owner/repo" in msg
    assert "No headline." in msg
    assert "No movement summary." in msg


def test_build_plan_prompt_includes_evaluation_rubric():
    prompt = _build_plan_prompt(
        plan_size=3,
        strategy_context="strategy",
        readme_goal="goal",
        objectives_context="(no objective configured)",
        north_star="north star",
        planning_principles="Prefer autonomy and evidence.",
        evaluation_rubric="### Execution Reliability\nTasks complete without manual intervention.",
        codebase_context="codebase",
        production_feedback_context="signals",
        outcome_context="outcomes",
        research_context="research",
        retrospective="retro",
        git_log="abc123 commit",
        counts={"open": 1, "closed": 2, "blocked": 0},
        metrics_summary="metrics",
        backlog_text="- #1: Task",
        open_issues="(none)",
        cross_repo_context="(single repo)",
    )
    assert "--- Domain Evaluation Rubric (RUBRIC.md" in prompt
    assert "Execution Reliability" in prompt
    assert "Tasks complete without manual intervention." in prompt


def test_build_plan_prompt_rubric_fallback_when_empty():
    prompt = _build_plan_prompt(
        plan_size=3,
        strategy_context="strategy",
        readme_goal="goal",
        objectives_context="(no objective configured)",
        north_star="north star",
        planning_principles="Prefer autonomy and evidence.",
        evaluation_rubric="",
        codebase_context="codebase",
        production_feedback_context="signals",
        outcome_context="outcomes",
        research_context="research",
        retrospective="retro",
        git_log="abc123 commit",
        counts={"open": 1, "closed": 2, "blocked": 0},
        metrics_summary="metrics",
        backlog_text="- #1: Task",
        open_issues="(none)",
        cross_repo_context="(single repo)",
    )
    assert "no domain rubric defined" in prompt
