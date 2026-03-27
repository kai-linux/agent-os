from __future__ import annotations


DEFAULT_AUTOMATION_MODE = "full"
DISPATCHER_ONLY_AUTOMATION_MODE = "dispatcher_only"
VALID_AUTOMATION_MODES = {
    DEFAULT_AUTOMATION_MODE,
    DISPATCHER_ONLY_AUTOMATION_MODE,
}


def _normalize_mode(value: object) -> str:
    mode = str(value or "").strip().lower()
    if mode in VALID_AUTOMATION_MODES:
        return mode
    return DEFAULT_AUTOMATION_MODE


def repo_automation_mode(cfg: dict, github_slug: str) -> str:
    mode = _normalize_mode(cfg.get("automation_mode"))

    for project_cfg in cfg.get("github_projects", {}).values():
        if not isinstance(project_cfg, dict):
            continue
        project_mode = _normalize_mode(project_cfg.get("automation_mode", mode))
        for repo_cfg in project_cfg.get("repos", []):
            if not isinstance(repo_cfg, dict):
                continue
            if repo_cfg.get("github_repo") != github_slug:
                continue
            return _normalize_mode(repo_cfg.get("automation_mode", project_mode))

    return mode


def is_dispatcher_only_repo(cfg: dict, github_slug: str) -> bool:
    return repo_automation_mode(cfg, github_slug) == DISPATCHER_ONLY_AUTOMATION_MODE
