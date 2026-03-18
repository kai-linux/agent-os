"""Unit tests for pure helpers in orchestrator/gh_project.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.pr_monitor import _checks_all_passed, _checks_any_failed, _extract_issue_number


# ---------------------------------------------------------------------------
# _checks_all_passed
# ---------------------------------------------------------------------------

def test_checks_all_passed_empty():
    assert _checks_all_passed([]) is False


def test_checks_all_passed_success():
    checks = [{"state": "completed", "conclusion": "success"}]
    assert _checks_all_passed(checks) is True


def test_checks_all_passed_pending():
    checks = [
        {"state": "completed", "conclusion": "success"},
        {"state": "in_progress", "conclusion": ""},
    ]
    assert _checks_all_passed(checks) is False


def test_checks_all_passed_skipped_neutral():
    checks = [
        {"state": "completed", "conclusion": "skipped"},
        {"state": "completed", "conclusion": "neutral"},
    ]
    assert _checks_all_passed(checks) is True


def test_checks_all_passed_failure():
    checks = [{"state": "completed", "conclusion": "failure"}]
    assert _checks_all_passed(checks) is False


# ---------------------------------------------------------------------------
# _checks_any_failed
# ---------------------------------------------------------------------------

def test_checks_any_failed_none():
    assert _checks_any_failed([]) is False
    assert _checks_any_failed([{"state": "completed", "conclusion": "success"}]) is False


def test_checks_any_failed_one_failure():
    checks = [
        {"state": "completed", "conclusion": "success"},
        {"state": "completed", "conclusion": "failure"},
    ]
    assert _checks_any_failed(checks) is True


def test_checks_any_failed_timed_out():
    assert _checks_any_failed([{"state": "completed", "conclusion": "timed_out"}]) is True


# ---------------------------------------------------------------------------
# _extract_issue_number
# ---------------------------------------------------------------------------

def test_extract_issue_number():
    assert _extract_issue_number("Automated changes for issue #42") == 42
    assert _extract_issue_number("no number here") is None
    assert _extract_issue_number("") is None
    assert _extract_issue_number("closes #7 and #9") == 7
