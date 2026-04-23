from __future__ import annotations

from pathlib import Path

from orchestrator.semantic_dedup import DedupCandidate, SemanticDeduper, collect_dedup_candidates


def test_simhash_fallback_detects_same_meaning_different_wording(tmp_path):
    cfg = {
        "root_dir": str(tmp_path),
        "semantic_dedup": {
            "fallback": "always_simhash",
            "threshold": 0.82,
        },
    }
    deduper = SemanticDeduper(cfg, "owner/repo", tmp_path)

    match = deduper.find_duplicate(
        "Dedup duplicate groomer issues",
        "Suppress redundant backlog items before filing.",
        [
            DedupCandidate(
                title="Deduplicate duplicate grooming tickets",
                body="Avoid filing redundant backlog work.",
                number=42,
                source="open_issue",
            )
        ],
    )

    assert match is not None
    assert match.similarity >= 0.82
    assert match.candidate.number == 42
    assert match.backend == "simhash"


def test_collect_dedup_candidates_includes_active_branch_origin(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    prompts = tmp_path / "runtime" / "prompts"
    prompts.mkdir(parents=True)
    task_id = "task-20260423-070711-phase-1-semantic-dedup-at-groom-time"
    (prompts / f"{task_id}.txt").write_text(
        "\n".join(
            [
                "github_repo: owner/repo",
                "github_issue_number: 306",
                "github_issue_title: 'Phase 1: Semantic dedup at groom time'",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "orchestrator.semantic_dedup._active_agent_branches",
        lambda repo_path: [f"agent/{task_id}"],
    )
    monkeypatch.setattr("orchestrator.semantic_dedup.list_recently_closed_issues", lambda *args, **kwargs: [])
    monkeypatch.setattr("orchestrator.semantic_dedup._gh_issue_view", lambda repo, number: {})

    candidates = collect_dedup_candidates(
        {"root_dir": str(tmp_path)},
        "owner/repo",
        repo,
        [{"number": 1, "title": "Open backlog item", "body": "", "url": ""}],
    )

    branch_candidate = next(c for c in candidates if c.source == "active_branch")
    assert branch_candidate.number == 306
    assert branch_candidate.title == "Phase 1: Semantic dedup at groom time"
