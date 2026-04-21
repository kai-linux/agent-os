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


def build_config(intake: dict[str, str], github: dict[str, Any], charter: dict[str, Any], telegram: dict[str, str]) -> dict[str, Any]:
    local_repo = str(Path(github["local_clone_path"]).expanduser())
    repo_name = github["repo_name"]
    root = str(ROOT)
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
        "github_project_ready_value": "Ready",
        "github_project_in_progress_value": "In Progress",
        "github_project_blocked_value": "Blocked",
        "github_project_done_value": "Done",
        "github_repos": {repo_name: github["repo_full_name"]},
        "github_projects": {
            repo_name: {
                "project_number": github["project_number"],
                "repos": [
                    {
                        "github_repo": github["repo_full_name"],
                        "path": local_repo,
                        "local_repo": local_repo,
                        "automation_mode": "full",
                    }
                ],
            }
        },
        "trusted_authors": [github["owner"]],
        "telegram_bot_token": telegram["token"],
        "telegram_chat_id": str(telegram["chat_id"]),
        "dependency_watcher": {
            "enabled": True,
            "cadence_days": 7,
            "max_actions_per_week": 3,
        },
    }


def run(state, intake: dict[str, str], github: dict[str, Any], charter: dict[str, Any], telegram: dict[str, str], *, dry_run: bool = False) -> Path:
    config_path = ROOT / "config.yaml"
    _ensure_hooks_path()
    if not dry_run and config_path.exists():
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = config_path.with_name(f"config.yaml.bak.{ts}")
        shutil.move(str(config_path), str(backup))

    payload = build_config(intake, github, charter, telegram)
    yaml_text = yaml.safe_dump(payload, sort_keys=False)
    if not dry_run:
        config_path.write_text(yaml_text, encoding="utf-8")
        os.chmod(config_path, 0o600)
    state.mark("config_written_path", str(config_path))
    if not dry_run:
        os.environ["AGENT_OS_CONFIG"] = str(config_path)
        load_config()
    return config_path

