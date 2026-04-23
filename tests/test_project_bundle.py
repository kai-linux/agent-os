from __future__ import annotations

import hashlib
import json
import tarfile
from pathlib import Path

import yaml

from orchestrator.project_bundle import export_bundle, import_bundle
from orchestrator.queue import parse_task


def _read_bundle(path: Path) -> dict[str, bytes]:
    with tarfile.open(path, "r:gz") as tar:
        return {member.name: tar.extractfile(member).read() for member in tar.getmembers() if member.isfile()}


def test_export_is_deterministic_and_redacts_secrets_and_raw_metrics(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "objectives").mkdir()
    (repo / "runtime" / "metrics").mkdir(parents=True)
    (repo / "runtime" / "mailbox" / "inbox").mkdir(parents=True)
    (repo / "runtime" / "worktrees").mkdir(parents=True)
    (repo / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "root_dir": str(repo),
                "github_projects": {"demo": {"local_repo": str(repo), "github_repo": "owner/repo"}},
                "API_TOKEN": "secret-token",
                "nested": {"PRIVATE_KEY": "secret-key"},
            }
        ),
        encoding="utf-8",
    )
    (repo / "CODEBASE.md").write_text(f"Local path: {repo}/runtime/prompts/task.txt\n", encoding="utf-8")
    (repo / "STRATEGY.md").write_text("# Strategy\n", encoding="utf-8")
    (repo / "objectives" / "demo.yaml").write_text("id: demo\n", encoding="utf-8")
    (repo / ".env").write_text("API_TOKEN=must-not-export\n", encoding="utf-8")
    (repo / "runtime" / "mailbox" / "inbox" / "task.md").write_text("prompt contents\n", encoding="utf-8")
    (repo / "runtime" / "worktrees" / "state.txt").write_text("worktree\n", encoding="utf-8")
    (repo / "runtime" / "logs").mkdir()
    (repo / "runtime" / "logs" / "queue.log").write_text("log\n", encoding="utf-8")
    records = [
        {"status": "complete", "task_type": "implementation", "duration_seconds": 30, "prompt": "raw prompt"},
        {"status": "blocked", "task_type": "debugging", "duration_seconds": 90, "body": "raw body"},
    ]
    (repo / "runtime" / "metrics" / "agent_stats.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n",
        encoding="utf-8",
    )

    out1 = tmp_path / "bundle1.tar.gz"
    out2 = tmp_path / "bundle2.tar.gz"
    export_bundle(repo, out1)
    export_bundle(repo, out2)

    assert hashlib.sha256(out1.read_bytes()).hexdigest() == hashlib.sha256(out2.read_bytes()).hexdigest()
    entries = _read_bundle(out1)
    names = set(entries)
    assert "bundle/MANIFEST.yaml" in names
    assert "bundle/SECRETS.md" in names
    assert "bundle/config.yaml" in names
    assert "bundle/objectives/demo.yaml" in names
    assert "bundle/runtime/metrics/summary.yaml" in names
    assert not any(".env" in name or "runtime/mailbox" in name or "runtime/worktrees" in name or name.endswith(".log") for name in names)

    blob = b"\n".join(entries.values())
    assert b"secret-token" not in blob
    assert b"secret-key" not in blob
    assert b"raw prompt" not in blob
    assert b"raw body" not in blob
    assert str(repo).encode() not in blob
    assert b"__AGENT_OS_SECRET_CONFIG_YAML_API_TOKEN__" in blob
    assert b"__HOST_PATH_REMOVED__" in blob

    summary = yaml.safe_load(entries["bundle/runtime/metrics/summary.yaml"])
    assert summary["task_counts"]["total"] == 2
    assert summary["success_rate"]["rate"] == 0.5
    assert summary["completion_time"]["mean_seconds"] == 60


def test_import_restores_bundle_validates_config_and_writes_noop_dispatch(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "objectives").mkdir()
    (source / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "root_dir": str(source),
                "mailbox_dir": str(source / "runtime" / "mailbox"),
                "logs_dir": str(source / "runtime" / "logs"),
                "github_projects": {"demo": {"local_repo": str(source), "github_repo": "owner/repo"}},
                "API_TOKEN": "old-secret",
            }
        ),
        encoding="utf-8",
    )
    (source / "CODEBASE.md").write_text("# Codebase\n", encoding="utf-8")
    (source / "STRATEGY.md").write_text("# Strategy\n", encoding="utf-8")
    (source / "objectives" / "demo.yaml").write_text("id: demo\n", encoding="utf-8")
    bundle = tmp_path / "bundle.tar.gz"
    export_bundle(source, bundle)

    target = tmp_path / "target"
    result = import_bundle(
        bundle,
        target,
        secrets={"CONFIG_YAML_API_TOKEN": "new-secret"},
        prompt=False,
        smoke_dispatch=True,
    )

    assert "config.yaml" in result["files"]
    cfg = yaml.safe_load((target / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["API_TOKEN"] == "new-secret"
    assert cfg["root_dir"] == str(target)
    assert cfg["mailbox_dir"] == str(target / "runtime" / "mailbox")
    assert cfg["github_projects"]["demo"]["local_repo"] == str(target)
    assert (target / "CODEBASE.md").exists()
    assert (target / "objectives" / "demo.yaml").exists()

    smoke_task = Path(result["smoke_task"])
    meta, body = parse_task(smoke_task)
    assert meta["task_id"] == "task-import-smoke-no-op"
    assert meta["repo"] == str(target)
    assert "No-op import smoke task" in body
