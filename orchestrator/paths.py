from pathlib import Path
import os
import yaml

ROOT = Path(os.environ.get("ORCH_ROOT", Path(__file__).resolve().parents[1]))
CONFIG_PATH = ROOT / "config.yaml"


def load_config():
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    cfg.setdefault("root_dir", str(ROOT))
    cfg.setdefault("mailbox_dir", str(ROOT / "runtime" / "mailbox"))
    cfg.setdefault("logs_dir", str(ROOT / "runtime" / "logs"))
    cfg.setdefault("worktrees_dir", "/srv/worktrees")
    cfg.setdefault("allowed_repos", [])
    cfg.setdefault("default_agent", "auto")
    cfg.setdefault("default_task_type", "implementation")
    cfg.setdefault("max_runtime_minutes", 40)
    cfg.setdefault("default_base_branch", "main")
    cfg.setdefault("default_allow_push", True)
    cfg.setdefault("default_max_attempts", 4)
    cfg.setdefault("github_owner", "")
    cfg.setdefault("github_project_number", 0)
    cfg.setdefault("github_project_status_field", "Status")
    cfg.setdefault("github_project_ready_value", "Ready")
    cfg.setdefault("github_project_in_progress_value", "In Progress")
    cfg.setdefault("github_project_review_value", "Review")
    cfg.setdefault("github_project_blocked_value", "Blocked")
    cfg.setdefault("github_project_done_value", "Done")
    cfg.setdefault("github_repos", {})

    return cfg


def runtime_paths(cfg: dict):
    mailbox = Path(cfg["mailbox_dir"])
    logs = Path(cfg["logs_dir"])

    paths = {
        "ROOT": ROOT,
        "CONFIG": CONFIG_PATH,
        "MAILBOX": mailbox,
        "INBOX": mailbox / "inbox",
        "PROCESSING": mailbox / "processing",
        "DONE": mailbox / "done",
        "FAILED": mailbox / "failed",
        "BLOCKED": mailbox / "blocked",
        "ESCALATED": mailbox / "escalated",
        "LOGS": logs,
        "QUEUE_SUMMARY_LOG": logs / "queue-summary.log",
    }

    for p in [
        paths["INBOX"],
        paths["PROCESSING"],
        paths["DONE"],
        paths["FAILED"],
        paths["BLOCKED"],
        paths["ESCALATED"],
        paths["LOGS"],
    ]:
        p.mkdir(parents=True, exist_ok=True)

    return paths
