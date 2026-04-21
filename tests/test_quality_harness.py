from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator import backlog_groomer as bg
from orchestrator import pr_monitor as pm
from orchestrator import queue as q
from orchestrator.quality_harness import (
    build_harness_plan,
    evaluate_quality_harness,
    parse_qa_failure_response,
    write_field_failure_fixture,
)


def test_build_harness_plan_detects_ocr_and_recommends_multimodal(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "requirements.txt").write_text("pytesseract\npillow\n", encoding="utf-8")
    (repo / "receipt_ocr.py").write_text("import pytesseract\n", encoding="utf-8")
    (repo / "tests").mkdir()

    finding = build_harness_plan(
        {"quality_harness": {"enabled": True, "suites": ["unit", "multimodal_eval"]}},
        "owner/repo",
        repo,
    )

    assert "ocr" in finding["modalities"]
    assert "multimodal_eval" in finding["recommended_suites"]
    assert finding["operator_approval_required"] is True


def test_write_field_failure_fixture_uses_verified_and_unverified_paths(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    verified = write_field_failure_fixture(
        repo,
        suite="multimodal_eval",
        fixture_id="receipt-total",
        payload={"input": "bad", "expected_output": "good", "verified": True},
        github_repo="owner/repo",
        issue_number=12,
    )
    unverified = write_field_failure_fixture(
        repo,
        suite="multimodal_eval",
        fixture_id="receipt-missing-expected",
        payload={"input": "bad", "verified": False},
        github_repo="owner/repo",
        issue_number=13,
    )

    assert verified.relative_to(repo).as_posix() == "tests/fixtures/multimodal_eval/receipt-total/manifest.yaml"
    assert unverified.relative_to(repo).as_posix() == "tests/fixtures/unverified/multimodal_eval/receipt-missing-expected/manifest.yaml"


def test_parse_qa_failure_response_extracts_sections():
    parsed = parse_qa_failure_response(
        "INPUT:\nfoo\nEXPECTED_OUTPUT:\nbar\nVERIFIED: yes\nNOTES:\nregression"
    )
    assert parsed == {
        "input": "foo",
        "expected_output": "bar",
        "notes": "regression",
        "verified": True,
    }


def test_evaluate_quality_harness_validates_fixture_manifests(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    write_field_failure_fixture(
        repo,
        suite="multimodal_eval",
        fixture_id="receipt-total",
        payload={"input": "bad", "expected_output": "good", "verified": True},
        github_repo="owner/repo",
        issue_number=12,
    )

    result = evaluate_quality_harness(
        repo,
        {"suites": ["multimodal_eval"], "score_threshold": 0.9},
    )

    assert result["passed"] is True
    assert result["score"] == 1.0


def test_queue_qa_fail_command_and_reply_write_fixture(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = {
        "TELEGRAM_ACTIONS": tmp_path / "actions",
    }
    paths["TELEGRAM_ACTIONS"].mkdir()
    cfg = {
        "github_projects": {
            "proj": {
                "repos": [{"github_repo": "owner/repo", "local_repo": str(repo)}],
            }
        }
    }

    reply = q._handle_qa_fail_command(cfg, paths, chat_id="1", args=["owner/repo", "multimodal_eval", "receipt-total", "17"])
    assert "Capture ready" in reply

    saved = q._handle_pending_qa_reply(
        cfg,
        paths,
        "1",
        "INPUT:\nwrong total\nEXPECTED_OUTPUT:\n42.00\nVERIFIED: yes\n",
    )
    assert "tests/fixtures/multimodal_eval/receipt-total/manifest.yaml" in saved
    assert (repo / "tests" / "fixtures" / "multimodal_eval" / "receipt-total" / "manifest.yaml").exists()


def test_pr_quality_harness_gate_blocks_deleted_fixtures(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = {
        "quality_harness": {"enabled": True, "suites": ["unit"], "score_threshold": 0.9},
        "github_projects": {"proj": {"repos": [{"github_repo": "owner/repo", "local_repo": str(repo)}]}},
        "root_dir": str(tmp_path),
    }

    monkeypatch.setattr(pm, "pr_deletes_fixtures", lambda repo, pr_number: ["tests/fixtures/unit/x/manifest.yaml"])

    ok, reason = pm._quality_harness_gate(cfg, "owner/repo", 5)
    assert ok is False
    assert "fixture deletions" in reason


def test_groom_repo_filters_unapproved_quality_harness_implementation(tmp_path, monkeypatch):
    cfg = {
        "root_dir": str(tmp_path),
        "worktrees_dir": str(tmp_path / "worktrees"),
        "quality_harness": {"enabled": True, "suites": ["multimodal_eval"], "operator_approved": False},
    }
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("## Goal\n\nKeep the repo healthy.\n", encoding="utf-8")
    (repo / "NORTH_STAR.md").write_text("# North Star\n", encoding="utf-8")
    (repo / "STRATEGY.md").write_text("# Strategy\n", encoding="utf-8")
    (repo / "PLANNING_PRINCIPLES.md").write_text("# Planning Principles\n", encoding="utf-8")
    (repo / "CODEBASE.md").write_text("# Codebase\n", encoding="utf-8")
    (repo / "requirements.txt").write_text("pytesseract\n", encoding="utf-8")

    monkeypatch.setattr(bg, "_list_open_issues", lambda repo, cfg: [])
    monkeypatch.setattr(bg, "load_recent_metrics", lambda *args, **kwargs: [{"task_id": "t1", "repo": "owner/repo"}])
    monkeypatch.setattr(bg, "_parse_known_issues", lambda repo_path: [])
    monkeypatch.setattr(bg, "_find_risk_flags", lambda cfg: [])
    monkeypatch.setattr(
        bg,
        "_call_haiku",
        lambda prompt: '[{"title":"Add quality harness multimodal eval","body":"## Goal\\nAdd quality harness skeleton\\n## Success Criteria\\n- multimodal_eval exists\\n## Constraints\\n- Prefer minimal diffs","task_type":"implementation","priority":"prio:high","labels":["enhancement"]}]',
    )
    monkeypatch.setattr(bg, "_open_issue_exists", lambda repo, title: False)

    created = []
    monkeypatch.setattr(bg, "_create_issue", lambda repo, title, body, labels: created.append(title) or "url")
    monkeypatch.setattr(bg, "_set_issue_backlog", lambda cfg, github_slug, issue_url: None)

    result = bg.groom_repo(cfg, "owner/repo", repo)

    assert result["created"] == 0
    assert created == []
