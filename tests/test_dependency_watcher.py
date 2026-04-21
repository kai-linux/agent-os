from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator import dependency_watcher as dw


def _cfg(tmp_path: Path, repo: Path, overrides: dict | None = None) -> dict:
    cfg = {
        "root_dir": str(tmp_path),
        "dependency_watcher": {"enabled": True, "cadence_days": 7, "max_actions_per_week": 3},
        "github_projects": {
            "proj": {
                "repos": [
                    {
                        "github_repo": "owner/repo",
                        "local_repo": str(repo),
                    }
                ]
            }
        },
    }
    if overrides:
        cfg.update(overrides)
    return cfg


def test_watch_repo_escalates_runtime_vulnerability_from_requirements(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "requirements.txt").write_text("flask==2.0.0\n", encoding="utf-8")

    def fake_run_json_command(cmd: list[str], cwd: Path):
        if cmd[0] == "pip-audit":
            return {
                "dependencies": [
                    {
                        "name": "flask",
                        "version": "2.0.0",
                        "vulns": [
                            {
                                "id": "CVE-2024-0001",
                                "severity": "high",
                                "affected_versions": "<2.0.3",
                                "fix_versions": ["2.0.3"],
                                "description": "Runtime vulnerability",
                                "references": [{"url": "https://advisories.example/flask"}],
                            }
                        ],
                    }
                ]
            }
        return None

    created = []
    monkeypatch.setattr(dw, "_run_json_command", fake_run_json_command)
    monkeypatch.setattr(dw, "_create_high_risk_issue", lambda repo_slug, finding: created.append(finding) or "https://github.com/owner/repo/issues/1")

    result = dw.watch_repo(_cfg(tmp_path, repo), "owner/repo", repo)

    assert result["created_issues"] == ["https://github.com/owner/repo/issues/1"]
    assert result["created_prs"] == []
    assert created[0].dependency == "flask"
    assert created[0].runtime is True
    assert created[0].cve_ids == ["CVE-2024-0001"]
    assert created[0].patched_version == "2.0.3"


def test_watch_repo_opens_pr_for_dev_only_patch_bump(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").write_text(
        """{
  "devDependencies": {
    "eslint": "^8.57.0"
  }
}
""",
        encoding="utf-8",
    )

    def fake_run_json_command(cmd: list[str], cwd: Path):
        if cmd[:2] == ["npm", "outdated"]:
            return {
                "eslint": {
                    "current": "8.57.0",
                    "wanted": "8.57.1",
                    "latest": "8.57.1",
                }
            }
        if cmd[:2] == ["npm", "audit"]:
            return {"vulnerabilities": {}}
        if cmd[0] == "osv-scanner":
            return {"results": []}
        return None

    created = []
    monkeypatch.setattr(dw, "_run_json_command", fake_run_json_command)
    monkeypatch.setattr(dw, "_create_dependency_pr", lambda repo_slug, repo_path, finding: created.append(finding) or "https://github.com/owner/repo/pull/2")

    result = dw.watch_repo(_cfg(tmp_path, repo), "owner/repo", repo)

    assert result["created_prs"] == ["https://github.com/owner/repo/pull/2"]
    assert result["created_issues"] == []
    assert created[0].dependency == "eslint"
    assert created[0].dev_only is True
    assert created[0].update_type == "patch"


def test_watch_repo_returns_clean_for_clean_lockfiles(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").write_text('{"dependencies":{"react":"18.2.0"}}\n', encoding="utf-8")

    def fake_run_json_command(cmd: list[str], cwd: Path):
        if cmd[:2] == ["npm", "outdated"]:
            return {}
        if cmd[:2] == ["npm", "audit"]:
            return {"vulnerabilities": {}}
        if cmd[0] == "osv-scanner":
            return {"results": []}
        return None

    monkeypatch.setattr(dw, "_run_json_command", fake_run_json_command)

    result = dw.watch_repo(_cfg(tmp_path, repo), "owner/repo", repo)

    assert result["created_prs"] == []
    assert result["created_issues"] == []
    assert result["skipped"] == "clean"


def test_watch_repo_skips_dispatcher_only_repos(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _cfg(
        tmp_path,
        repo,
        overrides={
            "github_projects": {
                "proj": {
                    "repos": [
                        {
                            "github_repo": "owner/repo",
                            "local_repo": str(repo),
                            "automation_mode": "dispatcher_only",
                        }
                    ]
                }
            }
        },
    )

    result = dw.watch_repo(cfg, "owner/repo", repo)

    assert result["skipped"] == "dispatcher_only"


def test_watch_repo_caps_actions_per_week(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = _cfg(tmp_path, repo)
    findings = [
        dw.Finding(
            kind="vulnerability",
            ecosystem="python",
            manifest_path="requirements.txt",
            dependency=f"pkg{i}",
            current_version="1.0.0",
            target_version="1.0.1",
            patched_version="1.0.1",
            runtime=True,
            cve_ids=[f"CVE-2024-000{i}"],
            advisory_urls=[f"https://advisories.example/{i}"],
        )
        for i in range(4)
    ]

    monkeypatch.setattr(dw, "scan_repo_dependencies", lambda repo_path: findings)
    created = []
    monkeypatch.setattr(dw, "_create_high_risk_issue", lambda repo_slug, finding: created.append(finding.dependency) or f"https://github.com/owner/repo/issues/{len(created)}")

    result = dw.watch_repo(cfg, "owner/repo", repo)

    assert len(result["created_issues"]) == 3
    assert created == ["pkg0", "pkg1", "pkg2"]


def test_format_finding_body_includes_required_vulnerability_details():
    finding = dw.Finding(
        kind="vulnerability",
        ecosystem="python",
        manifest_path="requirements.txt",
        dependency="flask",
        current_version="2.0.0",
        target_version="2.0.3",
        patched_version="2.0.3",
        runtime=True,
        cve_ids=["CVE-2024-0001"],
        severity="high",
        affected_versions="<2.0.3",
        advisory_urls=["https://advisories.example/flask"],
        summary="Runtime vulnerability",
        scanner="pip-audit",
    )

    body = dw._format_finding_body(finding)

    assert "CVE-2024-0001" in body
    assert "Severity: `high`" in body
    assert "Affected versions: `<2.0.3`" in body
    assert "Patched version: `2.0.3`" in body
    assert "https://advisories.example/flask" in body
