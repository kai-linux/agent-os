from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.repo_modes import is_dispatcher_only_repo, repo_automation_mode


def test_repo_automation_mode_defaults_to_full():
    assert repo_automation_mode({}, "owner/repo") == "full"
    assert is_dispatcher_only_repo({}, "owner/repo") is False


def test_repo_automation_mode_prefers_repo_override():
    cfg = {
        "automation_mode": "full",
        "github_projects": {
            "proj": {
                "automation_mode": "full",
                "repos": [
                    {"github_repo": "owner/repo-a", "automation_mode": "dispatcher_only"},
                    {"github_repo": "owner/repo-b"},
                ],
            }
        },
    }

    assert repo_automation_mode(cfg, "owner/repo-a") == "dispatcher_only"
    assert repo_automation_mode(cfg, "owner/repo-b") == "full"
    assert is_dispatcher_only_repo(cfg, "owner/repo-a") is True
