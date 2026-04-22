from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from orchestrator.paths import ROOT, load_config


def infer_test_command(kind: str, stack_decision: str) -> str:
    text = f"{kind} {stack_decision}".lower()
    if any(token in text for token in ["python", "flask", "quart", "django", "fastapi"]):
        return "pytest -q"
    if any(token in text for token in ["node", "next", "react", "vue", "svelte"]):
        return "npm test"
    if "go" in text:
        return "go test ./..."
    if "unity" in text:
        return "echo 'unity tests not configured yet'"
    return "echo no-tests-yet"


def _ensure_hooks_path() -> None:
    result = subprocess.run(["git", "config", "--get", "core.hooksPath"], capture_output=True, text=True, check=False)
    if result.stdout.strip() == "hooks":
        return
    subprocess.run(["git", "config", "core.hooksPath", "hooks"], check=True)


def _status_values(github: dict[str, Any]) -> dict[str, str]:
    return github.get(
        "status_value_names",
        {"Ready": "Ready", "In Progress": "In Progress", "Blocked": "Blocked", "Done": "Done"},
    )


def _project_entry(github: dict[str, Any]) -> dict[str, Any]:
    local_repo = str(Path(github["local_clone_path"]).expanduser())
    repo_name = github["repo_name"]
    status_values = _status_values(github)
    return {
        repo_name: {
            "project_number": github["project_number"],
            "ready_value": status_values["Ready"],
            "in_progress_value": status_values["In Progress"],
            "blocked_value": status_values["Blocked"],
            "done_value": status_values["Done"],
            "repos": [
                {
                    "key": repo_name,
                    "github_repo": github["repo_full_name"],
                    "path": local_repo,
                    "local_repo": local_repo,
                    "automation_mode": "full",
                }
            ],
        }
    }


def build_config(intake: dict[str, str], github: dict[str, Any], charter: dict[str, Any], telegram: dict[str, str]) -> dict[str, Any]:
    local_repo = str(Path(github["local_clone_path"]).expanduser())
    repo_name = github["repo_name"]
    root = str(ROOT)
    status_values = _status_values(github)
    return {
        "root_dir": root,
        "mailbox_dir": str(ROOT / "runtime" / "mailbox"),
        "logs_dir": str(ROOT / "runtime" / "logs"),
        "worktrees_dir": str(ROOT / "runtime" / "worktrees"),
        "objectives_dir": str(ROOT / "objectives"),
        "evidence_dir": str(Path.home() / ".local" / "share" / "agent-os" / "evidence"),
        "automation_mode": "full",
        "default_agent": "claude",
        "default_task_type": "implementation",
        "max_runtime_minutes": 40,
        "default_base_branch": "main",
        "default_allow_push": True,
        "default_max_attempts": 4,
        "max_parallel_workers": 1,
        "test_timeout_minutes": 5,
        "plan_size": 5,
        "sprint_cadence_days": 7,
        "planner_allow_early_refresh": True,
        "groomer_cadence_days": 3.5,
        "backlog_depth_multiplier": 2,
        "priority_weights": {
            "prio:high": 30,
            "prio:normal": 10,
            "prio:low": 0,
        },
        "allowed_repos": [local_repo],
        "repo_configs": {
            local_repo: {
                "test_command": infer_test_command(intake["kind"], charter["stack_decision"]),
            }
        },
        "agent_fallbacks": {
            "implementation": ["claude"],
            "debugging": ["claude"],
            "architecture": ["claude"],
            "research": ["claude"],
            "docs": ["claude"],
        },
        "planner_agents": ["claude"],
        "agent_timeout_minutes": {"claude": 45},
        "github_owner": github["owner"],
        "github_project_status_field": "Status",
        "github_project_ready_value": status_values["Ready"],
        "github_project_in_progress_value": status_values["In Progress"],
        "github_project_blocked_value": status_values["Blocked"],
        "github_project_done_value": status_values["Done"],
        "github_repos": {repo_name: github["repo_full_name"]},
        "github_projects": _project_entry(github),
        "trusted_authors": [github["owner"]],
        "telegram_bot_token": telegram["token"],
        "telegram_chat_id": str(telegram["chat_id"]),
        "dependency_watcher": {
            "enabled": True,
            "cadence_days": 7,
            "max_actions_per_week": 3,
        },
    }


def merge_config(existing: dict[str, Any], intake: dict[str, str], github: dict[str, Any], charter: dict[str, Any], telegram: dict[str, str]) -> dict[str, Any]:
    merged = dict(existing or {})
    local_repo = str(Path(github["local_clone_path"]).expanduser())
    repo_name = github["repo_name"]
    status_values = _status_values(github)

    merged.setdefault("root_dir", str(ROOT))
    merged.setdefault("mailbox_dir", str(ROOT / "runtime" / "mailbox"))
    merged.setdefault("logs_dir", str(ROOT / "runtime" / "logs"))
    merged.setdefault("worktrees_dir", str(ROOT / "runtime" / "worktrees"))
    merged.setdefault("objectives_dir", str(ROOT / "objectives"))
    merged.setdefault("evidence_dir", str(Path.home() / ".local" / "share" / "agent-os" / "evidence"))
    merged.setdefault("automation_mode", "full")
    merged.setdefault("default_agent", "claude")
    merged.setdefault("default_task_type", "implementation")
    merged.setdefault("max_runtime_minutes", 40)
    merged.setdefault("default_base_branch", "main")
    merged.setdefault("default_allow_push", True)
    merged.setdefault("default_max_attempts", 4)
    merged.setdefault("max_parallel_workers", 1)
    merged.setdefault("test_timeout_minutes", 5)
    merged.setdefault("plan_size", 5)
    merged.setdefault("sprint_cadence_days", 7)
    merged.setdefault("planner_allow_early_refresh", True)
    merged.setdefault("groomer_cadence_days", 3.5)
    merged.setdefault("backlog_depth_multiplier", 2)
    merged.setdefault("priority_weights", {"prio:high": 30, "prio:normal": 10, "prio:low": 0})

    allowed_repos = list(merged.get("allowed_repos") or [])
    if local_repo not in allowed_repos:
        allowed_repos.append(local_repo)
    merged["allowed_repos"] = allowed_repos

    repo_configs = dict(merged.get("repo_configs") or {})
    repo_cfg = dict(repo_configs.get(local_repo) or {})
    repo_cfg.setdefault("test_command", infer_test_command(intake["kind"], charter["stack_decision"]))
    repo_configs[local_repo] = repo_cfg
    merged["repo_configs"] = repo_configs

    merged.setdefault("agent_fallbacks", {
        "implementation": ["claude"],
        "debugging": ["claude"],
        "architecture": ["claude"],
        "research": ["claude"],
        "docs": ["claude"],
    })
    merged.setdefault("planner_agents", ["claude"])
    merged.setdefault("agent_timeout_minutes", {"claude": 45})

    merged.setdefault("github_owner", github["owner"])
    merged.setdefault("github_project_status_field", "Status")
    merged.setdefault("github_project_ready_value", status_values["Ready"])
    merged.setdefault("github_project_in_progress_value", status_values["In Progress"])
    merged.setdefault("github_project_blocked_value", status_values["Blocked"])
    merged.setdefault("github_project_done_value", status_values["Done"])

    github_repos = dict(merged.get("github_repos") or {})
    github_repos[repo_name] = github["repo_full_name"]
    merged["github_repos"] = github_repos

    github_projects = dict(merged.get("github_projects") or {})
    github_projects.update(_project_entry(github))
    merged["github_projects"] = github_projects

    trusted_authors = list(merged.get("trusted_authors") or [])
    if github["owner"] not in trusted_authors:
        trusted_authors.append(github["owner"])
    merged["trusted_authors"] = trusted_authors

    merged["telegram_bot_token"] = telegram["token"]
    merged["telegram_chat_id"] = str(telegram["chat_id"])
    merged.setdefault("dependency_watcher", {"enabled": True, "cadence_days": 7, "max_actions_per_week": 3})
    return merged


def run(state, intake: dict[str, str], github: dict[str, Any], charter: dict[str, Any], telegram: dict[str, str], *, dry_run: bool = False) -> Path:
    config_path = ROOT / "config.yaml"
    _ensure_hooks_path()
    existing_cfg = None
    backup = None
    if not dry_run and config_path.exists():
        existing_cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = config_path.with_name(f"config.yaml.bak.{ts}")
        shutil.move(str(config_path), str(backup))

    payload = merge_config(existing_cfg, intake, github, charter, telegram) if existing_cfg is not None else build_config(intake, github, charter, telegram)
    yaml_text = yaml.safe_dump(payload, sort_keys=False)
    if not dry_run:
        config_path.write_text(yaml_text, encoding="utf-8")
        os.chmod(config_path, 0o600)
    state.mark("config_written_path", str(config_path))
    if backup is not None:
        state.mark("config_backup_path", str(backup))
    if not dry_run:
        os.environ["AGENT_OS_CONFIG"] = str(config_path)
        load_config()
    return config_path
