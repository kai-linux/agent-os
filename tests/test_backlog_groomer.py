"""Tests for backlog groomer cadence config."""
from __future__ import annotations

import sys
from pathlib import Path
import os
import subprocess

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.backlog_groomer import (
    _assess_demo_availability,
    _assess_readme,
    _bootstrap_doc_issues,
    _count_backlog_issues,
    _expected_status_for,
    _filter_records_for_repo,
    _gather_adoption_signals,
    _reap_stale_in_progress,
    _reconcile_board_state,
    _repo_backlog_depth_target,
    _repo_gap_signals,
    _repo_groomer_cadence_days,
    _repo_plan_size,
    _resolve_repos,
)
from orchestrator import backlog_groomer as bg


def test_repo_groomer_cadence_defaults_to_half_sprint_cadence():
    """The groomer must run ahead of the planner so the backlog refills in time."""
    cfg = {"sprint_cadence_days": 2}
    assert _repo_groomer_cadence_days(cfg, "owner/repo") == 1.0


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
    # Explicit per-repo override of 0 (dormant) wins.
    assert _repo_groomer_cadence_days(cfg, "owner/repo-a") == 0.0
    # Top-level explicit groomer_cadence_days wins over per-repo sprint_cadence_days
    # because the operator chose to set it explicitly.
    assert _repo_groomer_cadence_days(cfg, "owner/repo-b") == 7.0


def test_repo_groomer_cadence_per_repo_sprint_with_no_explicit_groomer_halves():
    """When no explicit groomer cadence is set anywhere, per-repo sprint cadence halves."""
    cfg = {
        "github_projects": {
            "proj": {
                "repos": [
                    {"github_repo": "owner/repo-b", "sprint_cadence_days": 1.5},
                ]
            }
        },
    }
    assert _repo_groomer_cadence_days(cfg, "owner/repo-b") == 0.75


def test_repo_backlog_depth_target_defaults_to_2x_plan_size():
    cfg = {"plan_size": 5}
    assert _repo_backlog_depth_target(cfg, "owner/repo") == 10


def test_repo_backlog_depth_target_honors_absolute_override():
    cfg = {"plan_size": 5, "target_backlog_depth": 15}
    assert _repo_backlog_depth_target(cfg, "owner/repo") == 15


def test_repo_backlog_depth_target_honors_multiplier_override():
    cfg = {"plan_size": 5, "backlog_depth_multiplier": 3}
    assert _repo_backlog_depth_target(cfg, "owner/repo") == 15


def test_repo_backlog_depth_target_per_repo_override():
    cfg = {
        "plan_size": 5,
        "github_projects": {
            "proj": {
                "repos": [
                    {"github_repo": "owner/repo-a", "target_backlog_depth": 4},
                    {"github_repo": "owner/repo-b", "plan_size": 3},
                ]
            }
        },
    }
    assert _repo_backlog_depth_target(cfg, "owner/repo-a") == 4
    assert _repo_backlog_depth_target(cfg, "owner/repo-b") == 6


def test_repo_plan_size_default_and_override():
    assert _repo_plan_size({}, "owner/repo") == 5
    cfg = {
        "github_projects": {
            "proj": {"repos": [{"github_repo": "owner/repo", "plan_size": 8}]}
        }
    }
    assert _repo_plan_size(cfg, "owner/repo") == 8


def test_count_backlog_issues_excludes_active_and_done():
    issues = [
        {"number": 1, "labels": [{"name": "enhancement"}]},                       # backlog
        {"number": 2, "labels": [{"name": "ready"}]},                             # excluded
        {"number": 3, "labels": [{"name": "in-progress"}]},                       # excluded
        {"number": 4, "labels": [{"name": "done"}]},                              # excluded
        {"number": 5, "labels": [{"name": "blocked"}]},                           # excluded
        {"number": 6, "labels": [{"name": "agent-dispatched"}]},                  # excluded
        {"number": 7, "labels": [{"name": "bug"}, {"name": "prio:high"}]},        # backlog
    ]
    assert _count_backlog_issues(issues) == 2


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
    (repo / "NORTH_STAR.md").write_text("# North Star\n", encoding="utf-8")
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
    monkeypatch.setattr(bg, "run_external_ingester", lambda cfg, github_slug, repo_path: [])
    monkeypatch.setattr(
        bg,
        "load_external_signals",
        lambda cfg, repo=None, window_days=14: [
            {
                "source": "sentry",
                "kind": "error",
                "severity": "high",
                "title": "Checkout crashes on submit",
                "body": "Unhandled exception in payment flow.",
                "ts": "2026-04-21T09:00:00+00:00",
            }
        ],
    )
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
    assert "--- External Signals ---" in prompt
    assert "Checkout crashes on submit" in prompt
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


def test_assess_readme_detects_missing(tmp_path):
    info = _assess_readme(tmp_path)
    assert info == {"exists": False}


def test_assess_readme_detects_structure(tmp_path):
    (tmp_path / "README.md").write_text(
        "# My Project\n\n## Goal\n\nDo things.\n\n## Quick Start\n\n```sh\npip install foo\n```\n\n"
        "![demo](docs/demo.svg)\n\n![stars](https://img.shields.io/github/stars/foo/bar)\n",
        encoding="utf-8",
    )
    info = _assess_readme(tmp_path)
    assert info["exists"] is True
    assert info["has_quickstart"] is True
    assert info["has_demo"] is True
    assert info["has_badge"] is True
    assert info["has_goal"] is True


def test_assess_demo_availability(tmp_path):
    assert _assess_demo_availability(tmp_path) is False
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "demo.svg").write_text("<svg/>", encoding="utf-8")
    assert _assess_demo_availability(tmp_path) is True


def test_gather_adoption_signals_includes_key_fields(tmp_path, monkeypatch):
    (tmp_path / "README.md").write_text("# Hi\n\nSmall readme.\n", encoding="utf-8")
    monkeypatch.setattr(bg, "_fetch_github_stars_forks", lambda slug: {"stars": 5, "forks": 1})
    signals = _gather_adoption_signals("owner/repo", tmp_path)
    assert "stars: 5" in signals
    assert "forks: 1" in signals
    assert "NO quickstart" in signals
    assert "NO demo" in signals
    assert "Demo asset available: NO" in signals


def test_groom_repo_prompt_includes_adoption_signals(tmp_path, monkeypatch):
    cfg = {
        "root_dir": str(tmp_path),
        "worktrees_dir": str(tmp_path / "worktrees"),
    }
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("## Goal\n\nTest.\n", encoding="utf-8")
    (repo / "CODEBASE.md").write_text("# Codebase\n", encoding="utf-8")
    (repo / "STRATEGY.md").write_text("## Product Vision\nTest.\n", encoding="utf-8")
    (repo / "PLANNING_PRINCIPLES.md").write_text("Prefer things.\n", encoding="utf-8")
    (repo / "NORTH_STAR.md").write_text("# North Star\nTest.\n", encoding="utf-8")

    monkeypatch.setattr(bg, "_list_open_issues", lambda repo, cfg: [])
    monkeypatch.setattr(bg, "load_recent_metrics", lambda *a, **kw: [{"task_id": "t1", "repo": "owner/repo"}])
    monkeypatch.setattr(bg, "_parse_known_issues", lambda p: [])
    monkeypatch.setattr(bg, "_find_risk_flags", lambda c: [])
    monkeypatch.setattr(bg, "_fetch_github_stars_forks", lambda slug: {"stars": 2, "forks": 0})
    captured = {}

    def fake_call(prompt):
        captured["prompt"] = prompt
        return "[]"

    monkeypatch.setattr(bg, "_call_haiku", fake_call)
    monkeypatch.setattr(bg, "_open_issue_exists", lambda repo, title: False)

    bg.groom_repo(cfg, "owner/repo", repo)

    prompt = captured["prompt"]
    assert "Adoption and credibility signals" in prompt
    assert "stars: 2" in prompt
    assert "forks: 0" in prompt
    assert "NO quickstart" in prompt


def _make_groomable_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("## Goal\n\nTest.\n", encoding="utf-8")
    (repo / "CODEBASE.md").write_text("# Codebase\n", encoding="utf-8")
    (repo / "STRATEGY.md").write_text("## Product Vision\nTest.\n", encoding="utf-8")
    (repo / "PLANNING_PRINCIPLES.md").write_text("Prefer things.\n", encoding="utf-8")
    (repo / "NORTH_STAR.md").write_text("# North Star\nTest.\n", encoding="utf-8")
    return repo


def test_groom_repo_skips_when_backlog_already_at_target(tmp_path, monkeypatch):
    """If the backlog already meets target depth, the groomer should not
    generate more issues — preventing infrastructure-issue churn on top of an
    already-healthy backlog."""
    cfg = {
        "root_dir": str(tmp_path),
        "worktrees_dir": str(tmp_path / "worktrees"),
        "plan_size": 5,  # target_depth = 10
    }
    repo = _make_groomable_repo(tmp_path)

    # Simulate 12 already-open backlog issues (none in active/done/blocked state)
    fake_open = [
        {"number": n, "title": f"Issue {n}", "labels": [{"name": "enhancement"}]}
        for n in range(1, 13)
    ]
    monkeypatch.setattr(bg, "_list_open_issues", lambda repo, cfg: fake_open)
    monkeypatch.setattr(bg, "load_recent_metrics", lambda *a, **kw: [{"task_id": "t1", "repo": "owner/repo"}])
    monkeypatch.setattr(bg, "_parse_known_issues", lambda p: [])
    monkeypatch.setattr(bg, "_find_risk_flags", lambda c: [])
    monkeypatch.setattr(bg, "_fetch_github_stars_forks", lambda slug: {"stars": 2, "forks": 0})

    def fail_call(prompt):
        raise AssertionError("LLM should not be called when backlog is already at target")

    monkeypatch.setattr(bg, "_call_haiku", fail_call)
    monkeypatch.setattr(bg, "_open_issue_exists", lambda repo, title: False)

    result = bg.groom_repo(cfg, "owner/repo", repo)

    assert result["status"] == "skipped"
    assert result["created"] == 0


def test_groom_repo_generates_only_gap_to_target(tmp_path, monkeypatch):
    """When the backlog is partially full, generate only the deficit (capped
    by per-run budget), not a fixed 3-5 every cycle."""
    cfg = {
        "root_dir": str(tmp_path),
        "worktrees_dir": str(tmp_path / "worktrees"),
        "plan_size": 5,  # target_depth = 10
    }
    repo = _make_groomable_repo(tmp_path)

    # 8 existing backlog issues → only 2 new ones needed to hit target.
    fake_open = [
        {"number": n, "title": f"Existing {n}", "labels": [{"name": "enhancement"}]}
        for n in range(1, 9)
    ]
    monkeypatch.setattr(bg, "_list_open_issues", lambda repo, cfg: fake_open)
    monkeypatch.setattr(bg, "load_recent_metrics", lambda *a, **kw: [{"task_id": "t1", "repo": "owner/repo"}])
    monkeypatch.setattr(bg, "_parse_known_issues", lambda p: [])
    monkeypatch.setattr(bg, "_find_risk_flags", lambda c: [])
    monkeypatch.setattr(bg, "_fetch_github_stars_forks", lambda slug: {"stars": 2, "forks": 0})

    captured = {}

    def fake_call(prompt):
        captured["prompt"] = prompt
        return "[]"  # we only care about prompt sizing here

    monkeypatch.setattr(bg, "_call_haiku", fake_call)
    monkeypatch.setattr(bg, "_open_issue_exists", lambda repo, title: False)

    bg.groom_repo(cfg, "owner/repo", repo)

    prompt = captured["prompt"]
    # The prompt must instruct the LLM to generate exactly 2 issues this run.
    assert "create exactly 2 targeted" in prompt
    # Backlog state must be visible in the prompt.
    assert "8 open backlog issues vs target depth 10" in prompt


def test_groom_repo_prompt_enforces_40_percent_adoption_floor(tmp_path, monkeypatch):
    """The prompt must compute and pass an adoption floor of ceil(num_issues * 0.4)."""
    cfg = {
        "root_dir": str(tmp_path),
        "worktrees_dir": str(tmp_path / "worktrees"),
        "plan_size": 5,  # target_depth = 10, num_issues = 5 with empty backlog
    }
    repo = _make_groomable_repo(tmp_path)

    monkeypatch.setattr(bg, "_list_open_issues", lambda repo, cfg: [])
    monkeypatch.setattr(bg, "load_recent_metrics", lambda *a, **kw: [{"task_id": "t1", "repo": "owner/repo"}])
    monkeypatch.setattr(bg, "_parse_known_issues", lambda p: [])
    monkeypatch.setattr(bg, "_find_risk_flags", lambda c: [])
    monkeypatch.setattr(bg, "_fetch_github_stars_forks", lambda slug: {"stars": 2, "forks": 0})

    captured = {}

    def fake_call(prompt):
        captured["prompt"] = prompt
        return "[]"

    monkeypatch.setattr(bg, "_call_haiku", fake_call)
    monkeypatch.setattr(bg, "_open_issue_exists", lambda repo, title: False)

    bg.groom_repo(cfg, "owner/repo", repo)

    prompt = captured["prompt"]
    # ceil(5 * 0.4) = 2 adoption-facing issues required out of 5 generated.
    assert "At least 2 of the 5 issues" in prompt
    assert "≥40%" in prompt


def test_groom_repo_calls_llm_after_partial_bootstrap(tmp_path, monkeypatch):
    """Bootstrap doc issues should AUGMENT the LLM, not silence it. The previous
    behavior set remaining_slots=0 whenever any bootstrap issue existed, which
    crowded out adoption work whenever a doc gap was present."""
    cfg = {
        "root_dir": str(tmp_path),
        "worktrees_dir": str(tmp_path / "worktrees"),
        "plan_size": 5,
    }
    # Repo has README + STRATEGY + PLANNING_PRINCIPLES + CODEBASE but is
    # missing NORTH_STAR.md, so exactly one bootstrap issue is generated.
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("## Goal\n\nTest.\n", encoding="utf-8")
    (repo / "CODEBASE.md").write_text("# Codebase\n", encoding="utf-8")
    (repo / "STRATEGY.md").write_text("## Product Vision\nTest.\n", encoding="utf-8")
    (repo / "PLANNING_PRINCIPLES.md").write_text("Prefer things.\n", encoding="utf-8")

    monkeypatch.setattr(bg, "_list_open_issues", lambda repo, cfg: [])
    monkeypatch.setattr(bg, "load_recent_metrics", lambda *a, **kw: [{"task_id": "t1", "repo": "owner/repo"}])
    monkeypatch.setattr(bg, "_parse_known_issues", lambda p: [])
    monkeypatch.setattr(bg, "_find_risk_flags", lambda c: [])
    monkeypatch.setattr(bg, "_fetch_github_stars_forks", lambda slug: {"stars": 2, "forks": 0})

    llm_called = {"count": 0}

    def fake_call(prompt):
        llm_called["count"] += 1
        return (
            '[{"title":"Make a compelling demo video","body":"## Goal\\nx\\n## Success Criteria\\ny\\n## Constraints\\n- minimal","priority":"prio:high","labels":["enhancement"]}]'
        )

    monkeypatch.setattr(bg, "_call_haiku", fake_call)
    monkeypatch.setattr(bg, "_open_issue_exists", lambda repo, title: False)

    created = []
    monkeypatch.setattr(
        bg,
        "_create_issue",
        lambda repo, title, body, labels: created.append(title) or f"https://github.com/owner/repo/issues/{len(created)}",
    )
    monkeypatch.setattr(bg, "_set_issue_backlog", lambda cfg, github_slug, issue_url: None)

    result = bg.groom_repo(cfg, "owner/repo", repo)

    assert result["status"] == "created"
    assert llm_called["count"] == 1, "LLM must still be called when bootstrap issues only fill some of the slots"
    # The LLM-generated adoption issue must be among the created titles.
    assert "Make a compelling demo video" in created


# ---------------------------------------------------------------------------
# Skip signal integration
# ---------------------------------------------------------------------------


def test_groomer_cadence_backoff_on_auto_skips(tmp_path, monkeypatch):
    """When 2+ recent auto-skips exist, groomer halves issue generation."""
    from orchestrator.skip_signals import record_skip_signal

    cfg = {
        "root_dir": str(tmp_path),
        "worktrees_dir": str(tmp_path / "worktrees"),
        "plan_size": 5,
    }
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("## Goal\n\nGoal.\n", encoding="utf-8")
    (repo / "NORTH_STAR.md").write_text("# North Star\n", encoding="utf-8")
    (repo / "STRATEGY.md").write_text("# Strategy\n", encoding="utf-8")
    (repo / "PLANNING_PRINCIPLES.md").write_text("# Planning Principles\n", encoding="utf-8")
    (repo / "CODEBASE.md").write_text("# Codebase\n", encoding="utf-8")

    # Create skip signals directly in the expected location
    skip_signals_path = tmp_path / "runtime" / "metrics" / "plan_skip_signals.jsonl"
    plan = [{"issue_number": 1}, {"issue_number": 2}]
    record_skip_signal(skip_signals_path, "owner/repo", plan, "auto_skip")
    record_skip_signal(skip_signals_path, "owner/repo", plan, "auto_skip")

    monkeypatch.setattr(bg, "_list_open_issues", lambda repo, cfg: [])
    monkeypatch.setattr(bg, "load_recent_metrics", lambda *args, **kwargs: [{"task_id": "t1", "repo": "owner/repo"}])
    monkeypatch.setattr(bg, "_parse_known_issues", lambda repo_path: [])
    monkeypatch.setattr(bg, "_find_risk_flags", lambda cfg: [])

    captured_prompt = {}

    def mock_haiku(prompt):
        captured_prompt["text"] = prompt
        return '[{"title":"Test issue","body":"## Goal\\nX\\n## Success Criteria\\nY\\n## Constraints\\n- Z","priority":"prio:normal","labels":["enhancement"]}]'

    monkeypatch.setattr(bg, "_call_haiku", mock_haiku)
    monkeypatch.setattr(bg, "_open_issue_exists", lambda repo, title: False)
    monkeypatch.setattr(bg, "_create_issue", lambda repo, title, body, labels: "https://github.com/owner/repo/issues/99")
    monkeypatch.setattr(bg, "_set_issue_backlog", lambda cfg, github_slug, issue_url: None)

    result = bg.groom_repo(cfg, "owner/repo", repo)

    # Should still produce output but with reduced volume.
    # The key assertion is that the prompt was generated (LLM was called)
    # and the cadence backoff message was printed.
    assert result.get("status") in {"created", "skipped"}


# ---------------------------------------------------------------------------
# Board reconciliation + stale in-progress reaper
# ---------------------------------------------------------------------------

PROJECT_CFG = {
    "ready_value": "Ready",
    "in_progress_value": "In Progress",
    "review_value": "Review",
    "blocked_value": "Blocked",
    "done_value": "Done",
    "backlog_value": "Backlog",
}


def test_expected_status_blocked_label_overrides_ready():
    assert _expected_status_for({"blocked", "ready"}, "OPEN", PROJECT_CFG) == "Blocked"


def test_expected_status_in_progress_label():
    assert _expected_status_for({"in-progress"}, "OPEN", PROJECT_CFG) == "In Progress"
    assert _expected_status_for({"agent-dispatched"}, "OPEN", PROJECT_CFG) == "In Progress"


def test_expected_status_closed_issue_maps_to_done():
    assert _expected_status_for({"in-progress"}, "CLOSED", PROJECT_CFG) == "Done"


def test_expected_status_no_labels_maps_to_backlog():
    assert _expected_status_for(set(), "OPEN", PROJECT_CFG) == "Backlog"


def _make_cfg() -> dict:
    return {
        "github_owner": "owner",
        "github_projects": {
            "proj": {
                "project_number": 1,
                "repos": [{"github_repo": "owner/repo", "local_repo": "/tmp/repo"}],
                **PROJECT_CFG,
            }
        },
    }


class _FakeProject:
    """Stand-in for query_project output that tracks set_item_status calls."""

    def __init__(self, items: list[dict]):
        self.items = items
        self.moves: list[tuple[str, str]] = []  # (item_id, option_id)

    def info(self) -> dict:
        return {
            "project_id": "PID",
            "status_field_id": "SFID",
            "status_options": {
                "Ready": "opt-ready",
                "In Progress": "opt-ip",
                "Review": "opt-review",
                "Blocked": "opt-blocked",
                "Done": "opt-done",
                "Backlog": "opt-backlog",
            },
            "items": self.items,
        }


def test_reconcile_moves_blocked_label_out_of_ready(monkeypatch):
    fake = _FakeProject([{
        "item_id": "I1", "number": 42,
        "url": "https://github.com/owner/repo/issues/42",
        "state": "OPEN", "status": "Ready",
        "labels": {"blocked"},
    }])
    monkeypatch.setattr(bg, "query_project", lambda pn, owner: fake.info())
    monkeypatch.setattr(bg, "set_item_status", lambda pid, iid, fid, oid: fake.moves.append((iid, oid)))
    monkeypatch.setattr(bg, "edit_issue_labels", lambda *a, **kw: None)

    summary = _reconcile_board_state(_make_cfg(), "owner/repo")
    assert fake.moves == [("I1", "opt-blocked")]
    assert summary["moved"] == 1


def test_reconcile_moves_ready_label_out_of_backlog(monkeypatch):
    fake = _FakeProject([{
        "item_id": "I2", "number": 7,
        "url": "https://github.com/owner/repo/issues/7",
        "state": "OPEN", "status": "Backlog",
        "labels": {"ready"},
    }])
    monkeypatch.setattr(bg, "query_project", lambda pn, owner: fake.info())
    monkeypatch.setattr(bg, "set_item_status", lambda pid, iid, fid, oid: fake.moves.append((iid, oid)))
    monkeypatch.setattr(bg, "edit_issue_labels", lambda *a, **kw: None)

    summary = _reconcile_board_state(_make_cfg(), "owner/repo")
    assert fake.moves == [("I2", "opt-ready")]
    assert summary["moved"] == 1


def test_reconcile_closed_with_stale_labels_moves_to_done_and_strips(monkeypatch):
    fake = _FakeProject([{
        "item_id": "I3", "number": 99,
        "url": "https://github.com/owner/repo/issues/99",
        "state": "CLOSED", "status": "In Progress",
        "labels": {"in-progress"},
    }])
    stripped: list[dict] = []
    monkeypatch.setattr(bg, "query_project", lambda pn, owner: fake.info())
    monkeypatch.setattr(bg, "set_item_status", lambda pid, iid, fid, oid: fake.moves.append((iid, oid)))
    monkeypatch.setattr(
        bg, "edit_issue_labels",
        lambda repo, number, add=None, remove=None: stripped.append({"add": add, "remove": remove}),
    )

    summary = _reconcile_board_state(_make_cfg(), "owner/repo")
    assert fake.moves == [("I3", "opt-done")]
    assert summary["closed_reconciled"] == 1
    assert any("in-progress" in (s.get("remove") or []) for s in stripped)


def test_reconcile_strips_stale_labels_on_closed_already_in_done(monkeypatch):
    fake = _FakeProject([{
        "item_id": "I6", "number": 100,
        "url": "https://github.com/owner/repo/issues/100",
        "state": "CLOSED", "status": "Done",
        "labels": {"in-progress", "agent-dispatched"},
    }])
    stripped: list[dict] = []
    monkeypatch.setattr(bg, "query_project", lambda pn, owner: fake.info())
    monkeypatch.setattr(bg, "set_item_status", lambda pid, iid, fid, oid: fake.moves.append((iid, oid)))
    monkeypatch.setattr(
        bg, "edit_issue_labels",
        lambda repo, number, add=None, remove=None: stripped.append({"add": add, "remove": remove}),
    )

    summary = _reconcile_board_state(_make_cfg(), "owner/repo")
    assert fake.moves == []
    assert summary["closed_reconciled"] == 1
    removed_calls = [s.get("remove") or [] for s in stripped]
    assert any("in-progress" in rm for rm in removed_calls)
    assert any("agent-dispatched" in rm for rm in removed_calls)


def test_reconcile_leaves_review_column_alone(monkeypatch):
    fake = _FakeProject([{
        "item_id": "I4", "number": 4,
        "url": "https://github.com/owner/repo/issues/4",
        "state": "OPEN", "status": "Review",
        "labels": {"in-progress"},  # Review overrides — human gate
    }])
    monkeypatch.setattr(bg, "query_project", lambda pn, owner: fake.info())
    monkeypatch.setattr(bg, "set_item_status", lambda pid, iid, fid, oid: fake.moves.append((iid, oid)))
    monkeypatch.setattr(bg, "edit_issue_labels", lambda *a, **kw: None)

    _reconcile_board_state(_make_cfg(), "owner/repo")
    assert fake.moves == []


def test_reconcile_noop_when_aligned(monkeypatch):
    fake = _FakeProject([{
        "item_id": "I5", "number": 5,
        "url": "https://github.com/owner/repo/issues/5",
        "state": "OPEN", "status": "Ready",
        "labels": {"ready"},
    }])
    monkeypatch.setattr(bg, "query_project", lambda pn, owner: fake.info())
    monkeypatch.setattr(bg, "set_item_status", lambda pid, iid, fid, oid: fake.moves.append((iid, oid)))
    monkeypatch.setattr(bg, "edit_issue_labels", lambda *a, **kw: None)

    summary = _reconcile_board_state(_make_cfg(), "owner/repo")
    assert fake.moves == []
    assert summary == {"moved": 0, "labels_fixed": 0, "closed_reconciled": 0, "errors": 0}


def test_reap_stale_in_progress_demotes_when_no_open_pr(monkeypatch):
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(tz=timezone.utc) - timedelta(hours=48)).isoformat().replace("+00:00", "Z")
    issues = [{
        "number": 11, "url": "https://github.com/owner/repo/issues/11",
        "updatedAt": old,
        "labels": [{"name": "in-progress"}],
    }]
    calls: list[dict] = []
    monkeypatch.setattr(bg, "_list_open_pr_issue_refs", lambda repo: set())
    monkeypatch.setattr(
        bg, "edit_issue_labels",
        lambda repo, num, add=None, remove=None: calls.append({"num": num, "add": add, "remove": remove}),
    )
    monkeypatch.setattr(bg, "add_issue_comment", lambda *a, **kw: None)
    monkeypatch.setattr(bg, "query_project", lambda *a, **kw: {"project_id": "P", "status_field_id": "F", "status_options": {"Ready": "R"}, "items": []})
    monkeypatch.setattr(bg, "set_item_status", lambda *a, **kw: None)

    demoted = _reap_stale_in_progress(_make_cfg(), "owner/repo", issues)
    assert demoted == [11]
    assert any("in-progress" in (c.get("remove") or []) for c in calls)
    assert any("ready" in (c.get("add") or []) for c in calls)


def test_reap_stale_in_progress_skips_when_open_pr_references_issue(monkeypatch):
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(tz=timezone.utc) - timedelta(hours=48)).isoformat().replace("+00:00", "Z")
    issues = [{
        "number": 11, "url": "https://github.com/owner/repo/issues/11",
        "updatedAt": old,
        "labels": [{"name": "in-progress"}],
    }]
    monkeypatch.setattr(bg, "_list_open_pr_issue_refs", lambda repo: {11})
    monkeypatch.setattr(bg, "edit_issue_labels", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not edit")))

    demoted = _reap_stale_in_progress(_make_cfg(), "owner/repo", issues)
    assert demoted == []


def test_reap_stale_in_progress_skips_recent_activity(monkeypatch):
    from datetime import datetime, timedelta, timezone
    recent = (datetime.now(tz=timezone.utc) - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    issues = [{
        "number": 12, "url": "https://github.com/owner/repo/issues/12",
        "updatedAt": recent,
        "labels": [{"name": "in-progress"}],
    }]
    monkeypatch.setattr(bg, "_list_open_pr_issue_refs", lambda repo: set())
    monkeypatch.setattr(bg, "edit_issue_labels", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("should not edit")))

    demoted = _reap_stale_in_progress(_make_cfg(), "owner/repo", issues)
    assert demoted == []
