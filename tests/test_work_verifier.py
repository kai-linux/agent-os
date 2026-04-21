from __future__ import annotations

from orchestrator.pr_risk_assessment import RiskAssessment
from orchestrator.work_verifier import (
    _deterministic_pattern_findings,
    verify_pull_request,
)


def test_deterministic_pattern_findings_detects_blocking_antipatterns():
    diff = """diff --git a/app.py b/app.py
+++ b/app.py
@@
+def run():
+    raise NotImplementedError("stub")
+    return None
+# return real_value()
diff --git a/tests/test_app.py b/tests/test_app.py
+++ b/tests/test_app.py
@@
+@pytest.mark.skip(reason="later")
+def test_run():
+    mock_client = Mock()
+    # if ready: return True
+    pass
+    # TODO: finish
"""
    findings = _deterministic_pattern_findings(diff)
    categories = {item.category for item in findings}
    assert "stub" in categories
    assert "bare_return" in categories
    assert "commented_code" in categories
    assert "skipped_test" in categories
    assert "mock" in categories
    assert "todo" in categories


def test_verify_pull_request_blocks_on_stub_without_llm(monkeypatch, tmp_path):
    cfg = {"root_dir": str(tmp_path)}

    monkeypatch.setattr(
        "orchestrator.work_verifier._get_pr_diff",
        lambda repo, pr_number: "diff --git a/app.py b/app.py\n+++ b/app.py\n+raise NotImplementedError()\n",
    )
    monkeypatch.setattr(
        "orchestrator.work_verifier.assess_pr_risk",
        lambda repo, pr_number: RiskAssessment(
            level="low",
            files_changed=1,
            lines_changed=1,
            has_source_changes=True,
            has_test_changes=True,
        ),
    )
    monkeypatch.setattr(
        "orchestrator.work_verifier._fetch_issue_payload",
        lambda repo, issue_number: {"body": "## Success Criteria\n- real implementation exists\n", "labels": [{"name": "codex"}]},
    )
    monkeypatch.setattr(
        "orchestrator.work_verifier._call_independent_judge",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("judge should not run for deterministic block")),
    )

    report = verify_pull_request(
        cfg,
        repo="owner/repo",
        pr_number=12,
        pr_body="Fixes #77",
        worker_agent="codex",
    )

    assert report.verdict == "block"
    assert any(item.category == "stub" for item in report.findings)


def test_verify_pull_request_passes_valid_diff(monkeypatch, tmp_path):
    cfg = {"root_dir": str(tmp_path)}

    monkeypatch.setattr(
        "orchestrator.work_verifier._get_pr_diff",
        lambda repo, pr_number: (
            "diff --git a/app.py b/app.py\n+++ b/app.py\n+def solve():\n+    return 42\n"
            "diff --git a/tests/test_app.py b/tests/test_app.py\n+++ b/tests/test_app.py\n+def test_solve():\n+    assert solve() == 42\n"
        ),
    )
    monkeypatch.setattr(
        "orchestrator.work_verifier.assess_pr_risk",
        lambda repo, pr_number: RiskAssessment(
            level="low",
            files_changed=2,
            lines_changed=8,
            has_source_changes=True,
            has_test_changes=True,
        ),
    )
    monkeypatch.setattr(
        "orchestrator.work_verifier._fetch_issue_payload",
        lambda repo, issue_number: {
            "body": "## Goal\nShip the implementation.\n\n## Success Criteria\n- `solve()` returns 42\n- regression test covers it\n",
            "labels": [{"name": "codex"}],
        },
    )
    monkeypatch.setattr(
        "orchestrator.work_verifier._call_independent_judge",
        lambda cfg, **kwargs: (
            {
                "verdict": "pass",
                "summary": "Diff satisfies the linked issue.",
                "criteria": [
                    {"criterion": "solve() returns 42", "verdict": "pass", "reason": "Implementation added"},
                    {"criterion": "regression test covers it", "verdict": "pass", "reason": "Test added"},
                ],
                "scope_assessment": {"verdict": "match", "reason": "Scope matches the issue"},
                "missing_tests": False,
                "notes": ["criteria covered"],
            },
            "anthropic",
            "claude-sonnet-4",
        ),
    )

    report = verify_pull_request(
        cfg,
        repo="owner/repo",
        pr_number=14,
        pr_body="Implements feature. Fixes #88",
        worker_agent="codex",
    )

    assert report.verdict == "pass"
    assert report.issue_number == 88
    assert len(report.criteria) == 2
    assert report.judge_model_family == "anthropic"
