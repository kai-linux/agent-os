"""Unit tests for orchestrator/task_decomposer.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.task_decomposer import parse_decomposition, format_sub_issue_body


def test_parse_decomposition_atomic():
    result = parse_decomposition(
        """
        {
          "classification": "atomic",
          "reason": "Already one scoped deliverable.",
          "sub_issues": []
        }
        """
    )
    assert result["classification"] == "atomic"
    assert result["sub_issues"] == []


def test_parse_decomposition_epic_normalizes_defaults():
    result = parse_decomposition(
        """
        {
          "classification": "epic",
          "reason": "Two separate deliverables.",
          "sub_issues": [
            {
              "title": "Add decomposer module",
              "goal": "Create the fast classifier.",
              "success_criteria": ["Returns atomic or epic"],
              "constraints": ["Use Haiku"]
            },
            {
              "title": "Wire dispatcher hook",
              "goal": "Call decomposer before dispatch.",
              "success_criteria": ["Falls back to original issue"],
              "constraints": []
            }
          ]
        }
        """
    )
    assert result["classification"] == "epic"
    assert len(result["sub_issues"]) == 2
    assert result["sub_issues"][0]["constraints"] == ["Use Haiku", "Prefer minimal diffs"]
    assert result["sub_issues"][1]["constraints"] == ["Prefer minimal diffs"]


def test_parse_decomposition_rejects_too_many_subissues():
    text = """
    {
      "classification": "epic",
      "reason": "Too broad.",
      "sub_issues": [
        {"title": "1", "goal": "g1", "success_criteria": ["a"], "constraints": []},
        {"title": "2", "goal": "g2", "success_criteria": ["a"], "constraints": []},
        {"title": "3", "goal": "g3", "success_criteria": ["a"], "constraints": []},
        {"title": "4", "goal": "g4", "success_criteria": ["a"], "constraints": []},
        {"title": "5", "goal": "g5", "success_criteria": ["a"], "constraints": []},
        {"title": "6", "goal": "g6", "success_criteria": ["a"], "constraints": []}
      ]
    }
    """
    try:
        parse_decomposition(text)
    except ValueError as e:
        assert "2-5" in str(e)
    else:
        raise AssertionError("Expected ValueError")


def test_format_sub_issue_body_includes_parent_link():
    body = format_sub_issue_body(42, {
        "goal": "Implement the first slice.",
        "success_criteria": ["Code path exists", "Tests cover behavior"],
        "constraints": ["Prefer minimal diffs"],
        "context": "Reuse existing task formatter patterns.",
    })
    assert body.startswith("Part of #42")
    assert "## Success Criteria" in body
    assert "- Code path exists" in body
    assert "Reuse existing task formatter patterns." in body
