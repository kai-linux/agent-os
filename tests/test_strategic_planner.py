"""Tests for strategic_planner focus area analysis and configuration."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.strategic_planner import (
    DEFAULT_PLAN_SIZE,
    DEFAULT_SPRINT_CADENCE_DAYS,
    FOCUS_AREA_MARKER,
    RESEARCH_ARTIFACT_DEFAULT,
    _call_sonnet,
    _approval_timeout_hours,
    _allowed_research_file,
    _build_plan_prompt,
    _clean_research_text,
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
    _load_strategy_map,
    _order_repos_by_dependencies,
    _planning_research_context,
    _repo_planner_config,
    _repo_research_config,
    _resolve_repos,
    _set_issues_ready,
    _open_issues_summary,
    _strategy_dependencies,
    _summarize_strategy,
    _update_focus_areas_section,
    _analyze_focus_areas,
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
        codebase_context="codebase",
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


def test_maybe_refresh_backlog_for_early_cycle_runs_groomer_when_due(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr("orchestrator.strategic_planner.is_due", lambda *args, **kwargs: (True, "due"))
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
