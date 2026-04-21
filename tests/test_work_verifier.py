from __future__ import annotations

import subprocess

from orchestrator.pr_risk_assessment import RiskAssessment
from orchestrator.work_verifier import (
    _call_independent_judge,
    _deterministic_pattern_findings,
    verify_pull_request,
)


def test_call_independent_judge_does_not_treat_literal_json_as_format_placeholders(monkeypatch):
    """Regression: the judge prompt contains literal JSON (``{"verdict": ...}``).
    Running it through ``str.format`` mis-parses the ``{`` as a placeholder and
    raises ``KeyError: '\\n  "verdict"'``, which crashed pr_monitor mid-merge
    on 2026-04-21 and burned PR #32's auto-merge attempts until MAX hit."""

    captured: dict = {}

    def fake_run(argv, *args, **kwargs):
        prompt = argv[-1] if argv and isinstance(argv[-1], str) else ""
        for piece in argv:
            if isinstance(piece, str) and piece.startswith("You are the Agent OS work verifier"):
                prompt = piece
                break
        captured["prompt"] = prompt
        return subprocess.CompletedProcess(
            argv, 0,
            stdout='{"verdict":"pass","summary":"ok","criteria":[],"scope_assessment":{"verdict":"match","reason":"ok"},"missing_tests":false,"notes":[]}',
            stderr="",
        )

    monkeypatch.setattr("orchestrator.work_verifier.subprocess.run", fake_run)
    monkeypatch.setattr(
        "orchestrator.work_verifier._judge_command",
        lambda *a, **kw: (["claude"], "anthropic", "stub-model"),
    )

    payload, family, model = _call_independent_judge(
        {},
        worker_agent="claude",
        issue_body="Issue with literal {curly} braces inside body.",
        diff_text='+x = {"k": 1}\n+y = {} ',
    )
    assert payload["verdict"] == "pass"
    # The literal JSON schema survived into the final prompt text intact.
    assert '"verdict": "pass|block|uncertain"' in captured["prompt"]
    assert "Issue body:" in captured["prompt"]
    assert "literal {curly} braces" in captured["prompt"]


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
