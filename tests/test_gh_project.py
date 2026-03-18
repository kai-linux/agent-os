"""Unit tests for pure helpers in orchestrator/pr_monitor.py"""
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
    checks = [{"state": "SUCCESS", "bucket": "pass"}]
    assert _checks_all_passed(checks) is True


def test_checks_all_passed_pending():
    checks = [
        {"state": "SUCCESS", "bucket": "pass"},
        {"state": "PENDING", "bucket": ""},
    ]
    assert _checks_all_passed(checks) is False


def test_checks_all_passed_skipped_neutral():
    checks = [
        {"state": "SKIPPED", "bucket": "pass"},
        {"state": "NEUTRAL", "bucket": "pass"},
    ]
    assert _checks_all_passed(checks) is True


def test_checks_all_passed_failure():
    checks = [{"state": "FAILURE", "bucket": "fail"}]
    assert _checks_all_passed(checks) is False


# ---------------------------------------------------------------------------
# _checks_any_failed
# ---------------------------------------------------------------------------

def test_checks_any_failed_none():
    assert _checks_any_failed([]) is False
    assert _checks_any_failed([{"state": "SUCCESS", "bucket": "pass"}]) is False


def test_checks_any_failed_one_failure():
    checks = [
        {"state": "SUCCESS", "bucket": "pass"},
        {"state": "FAILURE", "bucket": "fail"},
    ]
    assert _checks_any_failed(checks) is True


def test_checks_any_failed_bucket_fail():
    assert _checks_any_failed([{"state": "ERROR", "bucket": "fail"}]) is True


# ---------------------------------------------------------------------------
# _extract_issue_number
# ---------------------------------------------------------------------------

def test_extract_issue_number():
    assert _extract_issue_number("Automated changes for issue #42") == 42
    assert _extract_issue_number("no number here") is None
    assert _extract_issue_number("") is None
    assert _extract_issue_number("closes #7 and #9") == 7
