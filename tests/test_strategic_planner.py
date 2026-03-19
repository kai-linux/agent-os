"""Tests for strategic_planner focus area analysis and configuration."""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.strategic_planner import (
    DEFAULT_PLAN_SIZE,
    DEFAULT_SPRINT_CADENCE_DAYS,
    FOCUS_AREA_MARKER,
    _extract_sprint_entries,
    _is_focus_areas_manually_edited,
    _repo_planner_config,
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
    assert cadence == 14


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
    assert cadence == 14

    # repo-b falls back to top-level
    plan_size, cadence = _repo_planner_config(cfg, "owner/repo-b")
    assert plan_size == 5
    assert cadence == 7

    # unknown repo falls back to top-level
    plan_size, cadence = _repo_planner_config(cfg, "owner/other")
    assert plan_size == 5
    assert cadence == 7
