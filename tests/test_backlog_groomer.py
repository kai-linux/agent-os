"""Tests for backlog groomer cadence config."""
from __future__ import annotations

import sys
from pathlib import Path
import os
import subprocess

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.backlog_groomer import (
    _bootstrap_doc_issues,
    _filter_records_for_repo,
    _repo_gap_signals,
    _repo_groomer_cadence_days,
    _resolve_repos,
)
from orchestrator import backlog_groomer as bg


def test_repo_groomer_cadence_defaults_to_sprint_cadence():
    cfg = {"sprint_cadence_days": 2}
    assert _repo_groomer_cadence_days(cfg, "owner/repo") == 2.0


def test_repo_groomer_cadence_uses_top_level_override():
    cfg = {"sprint_cadence_days": 7, "groomer_cadence_days": 0.5}
    assert _repo_groomer_cadence_days(cfg, "owner/repo") == 0.5


def test_repo_groomer_cadence_uses_per_repo_override():
    cfg = {
        "groomer_cadence_days": 7,
        "github_projects": {
            "proj": {
                "repos": [
                    {"github_repo": "owner/repo-a", "groomer_cadence_days": 0},
                    {"github_repo": "owner/repo-b", "sprint_cadence_days": 1.5},
                ]
            }
        },
    }
    assert _repo_groomer_cadence_days(cfg, "owner/repo-a") == 0.0
    assert _repo_groomer_cadence_days(cfg, "owner/repo-b") == 1.5


def test_groom_repo_no_data_status(tmp_path, monkeypatch):
    cfg = {"root_dir": str(tmp_path), "worktrees_dir": str(tmp_path / "worktrees")}
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("## Goal\n\nKeep the repo healthy.\n", encoding="utf-8")
    (repo / "NORTH_STAR.md").write_text("# North Star\n", encoding="utf-8")
    (repo / "STRATEGY.md").write_text("# Strategy\n", encoding="utf-8")
    (repo / "PLANNING_PRINCIPLES.md").write_text("# Planning Principles\n", encoding="utf-8")
    (repo / "CODEBASE.md").write_text("# Codebase\n", encoding="utf-8")
    monkeypatch.setattr(bg, "_list_open_issues", lambda repo, cfg: [])
    monkeypatch.setattr(bg, "load_recent_metrics", lambda *args, **kwargs: [])
    monkeypatch.setattr(bg, "_parse_known_issues", lambda repo_path: [])
    monkeypatch.setattr(bg, "_find_risk_flags", lambda cfg: [])

    result = bg.groom_repo(cfg, "owner/repo", repo)

    assert result["status"] == "no-data"
    assert result["created"] == 0
    assert result["skipped"] == 0


def test_groom_repo_adds_created_issue_to_backlog(tmp_path, monkeypatch):
    cfg = {
        "root_dir": str(tmp_path),
        "worktrees_dir": str(tmp_path / "worktrees"),
        "github_owner": "owner",
        "github_projects": {
            "proj": {
                "project_number": 1,
                "backlog_value": "Backlog",
                "repos": [{"github_repo": "owner/repo", "local_repo": str(tmp_path / "repo")}],
            }
        },
    }
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("## Goal\n\nKeep the repo healthy.\n", encoding="utf-8")
    (repo / "STRATEGY.md").write_text("# Strategy\n", encoding="utf-8")
    (repo / "PLANNING_PRINCIPLES.md").write_text("# Planning Principles\n", encoding="utf-8")
    (repo / "CODEBASE.md").write_text("# Codebase\n", encoding="utf-8")

    monkeypatch.setattr(bg, "_list_open_issues", lambda repo, cfg: [])
    monkeypatch.setattr(bg, "load_recent_metrics", lambda *args, **kwargs: [{"task_id": "t1", "repo": "owner/repo"}])
    monkeypatch.setattr(bg, "_parse_known_issues", lambda repo_path: [])
    monkeypatch.setattr(bg, "_find_risk_flags", lambda cfg: [])
    monkeypatch.setattr(bg, "_call_haiku", lambda prompt: '[{"title":"Fix thing","body":"## Goal\\nX\\n## Success Criteria\\nY\\n## Constraints\\n- Prefer minimal diffs","priority":"prio:high","labels":["bug"]}]')
    monkeypatch.setattr(bg, "_open_issue_exists", lambda repo, title: False)

    created_urls = []
    monkeypatch.setattr(bg, "_create_issue", lambda repo, title, body, labels: created_urls.append("https://github.com/owner/repo/issues/10") or created_urls[-1])
    backlog_urls = []
    monkeypatch.setattr(bg, "_set_issue_backlog", lambda cfg, github_slug, issue_url: backlog_urls.append(issue_url))

    result = bg.groom_repo(cfg, "owner/repo", repo)

    assert result["status"] == "created"
    assert created_urls == ["https://github.com/owner/repo/issues/10"]
    assert backlog_urls == ["https://github.com/owner/repo/issues/10"]


def test_cleanup_stale_issue_when_referenced_pr_merged(monkeypatch):
    closed = []
    done = []

    monkeypatch.setattr(
        bg,
        "_get_pr_state",
        lambda repo, pr_number: {"number": pr_number, "state": "MERGED", "mergedAt": "2026-03-19T18:00:00Z"},
    )
    monkeypatch.setattr(bg, "_set_issue_done", lambda cfg, github_slug, issue_url: done.append((github_slug, issue_url)))
    monkeypatch.setattr(
        bg,
        "gh",
        lambda cmd, check=False: closed.append(cmd) or "",
    )

    cleaned = bg._cleanup_stale_issues(
        {"github_owner": "owner"},
        "owner/repo",
        [{"number": 37, "title": "Diagnose and resolve CI failure blocking PR #34 merge", "url": "https://github.com/owner/repo/issues/37"}],
    )

    assert cleaned == [37]
    assert any(cmd[:3] == ["issue", "close", "37"] for cmd in closed)
    assert done == [("owner/repo", "https://github.com/owner/repo/issues/37")]


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


def test_filter_records_for_repo_uses_repo_slug_and_path(tmp_path):
    repo = tmp_path / "agent-os"
    repo.mkdir()
    records = [
        {"repo": "kai-linux/agent-os", "task_id": "a"},
        {"repo": "agent-os", "task_id": "b"},
        {"repo": str(repo), "task_id": "c"},
        {"repo": "kai-linux/bookgenerator", "task_id": "d"},
    ]
    filtered = _filter_records_for_repo(records, "kai-linux/agent-os", repo)
    assert [r["task_id"] for r in filtered] == ["a", "b", "c"]


def test_repo_gap_signals_detects_missing_strategy(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# Repo\n", encoding="utf-8")

    gaps = _repo_gap_signals(repo, [])

    assert any("STRATEGY.md is missing" in gap for gap in gaps)


def test_bootstrap_doc_issues_for_missing_core_context(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    issues = _bootstrap_doc_issues(repo, [])
    titles = [issue["title"] for issue in issues]

    assert "Bootstrap README.md with repo goal and operator context" in titles
    assert "Bootstrap STRATEGY.md from repo state" in titles
    assert "Bootstrap PLANNING_PRINCIPLES.md for stable planning rules" in titles
    assert "Bootstrap NORTH_STAR.md for long-term direction" in titles
    assert "Bootstrap CODEBASE.md for execution memory" in titles


def test_groom_repo_creates_bootstrap_issues_before_llm(tmp_path, monkeypatch):
    cfg = {
        "root_dir": str(tmp_path),
        "worktrees_dir": str(tmp_path / "worktrees"),
    }
    repo = tmp_path / "repo"
    repo.mkdir()

    monkeypatch.setattr(bg, "_list_open_issues", lambda repo, cfg: [])
    monkeypatch.setattr(bg, "load_recent_metrics", lambda *args, **kwargs: [])
    monkeypatch.setattr(bg, "_parse_known_issues", lambda repo_path: [])
    monkeypatch.setattr(bg, "_find_risk_flags", lambda cfg: [])
    monkeypatch.setattr(bg, "_call_haiku", lambda prompt: (_ for _ in ()).throw(AssertionError("LLM should not be required for bootstrap issues")))
    monkeypatch.setattr(bg, "_open_issue_exists", lambda repo, title: False)

    created_titles = []
    monkeypatch.setattr(
        bg,
        "_create_issue",
        lambda repo, title, body, labels: created_titles.append(title) or f"https://github.com/owner/repo/issues/{len(created_titles)}",
    )
    monkeypatch.setattr(bg, "_set_issue_backlog", lambda cfg, github_slug, issue_url: None)

    result = bg.groom_repo(cfg, "owner/repo", repo)

    assert result["status"] == "created"
    assert created_titles[:5] == [
        "Bootstrap README.md with repo goal and operator context",
        "Bootstrap STRATEGY.md from repo state",
        "Bootstrap PLANNING_PRINCIPLES.md for stable planning rules",
        "Bootstrap NORTH_STAR.md for long-term direction",
        "Bootstrap CODEBASE.md for execution memory",
    ]


def test_groom_repo_prompt_includes_repo_documents(tmp_path, monkeypatch):
    cfg = {
        "root_dir": str(tmp_path),
        "worktrees_dir": str(tmp_path / "worktrees"),
    }
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("## Goal\n\nImprove closed-loop planning.\n", encoding="utf-8")
    (repo / "NORTH_STAR.md").write_text("# North Star\n\nClosed-loop self-improvement.\n", encoding="utf-8")
    (repo / "STRATEGY.md").write_text("## Product Vision\n\nBuild an autonomous agent OS.\n", encoding="utf-8")
    (repo / "PLANNING_PRINCIPLES.md").write_text("Prefer autonomy gains.\n", encoding="utf-8")
    (repo / "CODEBASE.md").write_text("# Codebase\n", encoding="utf-8")
    (repo / "PRODUCTION_FEEDBACK.md").write_text("# Production Feedback\n\nFresh evidence.\n", encoding="utf-8")
    (repo / "PLANNING_RESEARCH.md").write_text("# Planning Research\n\nEvidence.\n", encoding="utf-8")

    monkeypatch.setattr(bg, "_list_open_issues", lambda repo, cfg: [])
    monkeypatch.setattr(bg, "load_recent_metrics", lambda *args, **kwargs: [{"task_id": "t1", "repo": "owner/repo"}])
    monkeypatch.setattr(bg, "_parse_known_issues", lambda repo_path: [])
    monkeypatch.setattr(bg, "_find_risk_flags", lambda cfg: [])
    captured = {}
    def fake_call(prompt):
        captured["prompt"] = prompt
        return "[]"
    monkeypatch.setattr(bg, "_call_haiku", fake_call)
    monkeypatch.setattr(bg, "_open_issue_exists", lambda repo, title: False)

    result = bg.groom_repo(cfg, "owner/repo", repo)

    assert result["status"] == "error"
    prompt = captured["prompt"]
    assert "Improve closed-loop planning." in prompt
    assert "Closed-loop self-improvement." in prompt
    assert "Build an autonomous agent OS." in prompt
    assert "Prefer autonomy gains." in prompt
    assert "Fresh evidence." in prompt
    assert "Evidence." in prompt


def test_run_does_not_send_telegram_for_due_skips_only(tmp_path, monkeypatch):
    cfg = {
        "root_dir": str(tmp_path),
        "github_owner": "owner",
        "github_projects": {
            "proj": {
                "repos": [{"github_repo": "owner/repo", "local_repo": str(tmp_path / "repo")}],
            }
        },
    }
    repo = tmp_path / "repo"
    repo.mkdir()

    monkeypatch.setattr(bg, "load_config", lambda: cfg)
    class _Lock:
        def __enter__(self):
            return True
        def __exit__(self, exc_type, exc, tb):
            return False
    monkeypatch.setattr(bg, "job_lock", lambda cfg, job_name: _Lock())
    monkeypatch.setattr(bg, "_resolve_repos", lambda cfg: [("owner/repo", repo)])
    monkeypatch.setattr(bg, "is_due", lambda *args, **kwargs: (False, "next due in 2.1h"))
    sent = []
    monkeypatch.setattr(bg, "_send_telegram", lambda cfg, text: sent.append(text))

    bg.run()

    assert sent == []


def test_run_sends_telegram_when_repo_created_items(tmp_path, monkeypatch):
    cfg = {
        "root_dir": str(tmp_path),
        "github_owner": "owner",
        "github_projects": {
            "proj": {
                "repos": [{"github_repo": "owner/repo", "local_repo": str(tmp_path / "repo")}],
            }
        },
    }
    repo = tmp_path / "repo"
    repo.mkdir()

    monkeypatch.setattr(bg, "load_config", lambda: cfg)
    class _Lock:
        def __enter__(self):
            return True
        def __exit__(self, exc_type, exc, tb):
            return False
    monkeypatch.setattr(bg, "job_lock", lambda cfg, job_name: _Lock())
    monkeypatch.setattr(bg, "_resolve_repos", lambda cfg: [("owner/repo", repo)])
    monkeypatch.setattr(bg, "is_due", lambda *args, **kwargs: (True, "due"))
    monkeypatch.setattr(bg, "groom_repo", lambda cfg, slug, path: {"status": "created", "created": 1, "skipped": 0, "cleaned": 0})
    monkeypatch.setattr(bg, "record_run", lambda *args, **kwargs: None)
    sent = []
    monkeypatch.setattr(bg, "_send_telegram", lambda cfg, text: sent.append(text))

    bg.run()

    assert len(sent) == 1


def test_call_haiku_falls_back_to_codex_when_claude_fails(monkeypatch):
    def fake_run(cmd, capture_output=True, text=True, timeout=120):
        if os.path.basename(cmd[0]) == "claude":
            return subprocess.CompletedProcess(cmd, 1, "", "quota")
        if os.path.basename(cmd[0]) == "codex":
            return subprocess.CompletedProcess(cmd, 0, '[{"title":"Fix thing"}]', "")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(bg.subprocess, "run", fake_run)

    raw = bg._call_haiku("Return JSON")

    assert raw == '[{"title":"Fix thing"}]'
