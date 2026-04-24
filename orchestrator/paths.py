import os
from pathlib import Path

import yaml

ROOT = Path(os.environ.get("ORCH_ROOT", Path(__file__).resolve().parents[1]))
CONFIG_ENV_VAR = "AGENT_OS_CONFIG"
DEFAULT_CONFIG_NAME = "config.yaml"


def _config_candidates() -> list[Path]:
    explicit = os.environ.get(CONFIG_ENV_VAR, "").strip()
    if explicit:
        return [Path(explicit).expanduser()]

    return [ROOT / DEFAULT_CONFIG_NAME]


def resolve_config_path() -> Path:
    for candidate in _config_candidates():
        if candidate.exists():
            return candidate
    return _config_candidates()[0]


def load_config():
    config_path = resolve_config_path()
    with config_path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    config_dir = config_path.parent
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
    cfg.setdefault("max_processing_minutes", 30)
    cfg.setdefault("stall_watchdog_interval_minutes", 5)
    cfg.setdefault("automation_mode", "full")
    cfg.setdefault("dashboard_bind_address", "127.0.0.1")
    cfg.setdefault("github_owner", "")
    cfg.setdefault("github_projects", {})
    cfg.setdefault("config_dir", str(config_dir))
    cfg.setdefault("objectives_dir", str(config_dir / "objectives"))
    cfg.setdefault("evidence_dir", str(Path.home() / ".local" / "share" / "agent-os" / "evidence"))
    cfg.setdefault(
        "incident_router",
        {
            "business_timezone": "UTC",
            "business_hours": {"start_hour": 9, "end_hour": 17},
            "digest_hour": 9,
            "tiers": {
                "sev1": {
                    "delivery": "immediate",
                    "dedup_window_minutes": 0,
                    "bypass_kill_switch": True,
                    "handlers": [{"type": "telegram_chat", "snooze_minutes": 0}],
                    "snooze_minutes": 0,
                },
                "sev2": {
                    "delivery": "next_business_hour",
                    "dedup_window_minutes": 60,
                    "bypass_kill_switch": False,
                    "handlers": [{"type": "telegram_chat", "snooze_minutes": 60}],
                    "snooze_minutes": 60,
                },
                "sev3": {
                    "delivery": "regular_digest",
                    "dedup_window_minutes": 240,
                    "bypass_kill_switch": False,
                    "handlers": [{"type": "telegram_chat", "snooze_minutes": 240}],
                    "snooze_minutes": 240,
                },
            },
            "sources": {},
        },
    )
    cfg["_config_path"] = str(config_path)
    cfg["_config_dir"] = str(config_dir)

    from orchestrator.dashboard.auth import validate_dashboard_auth_config
    from orchestrator.tool_registry import validate_tool_registry_or_raise

    cfg.update(validate_dashboard_auth_config(cfg))
    cfg["_tool_registry_status"] = validate_tool_registry_or_raise(cfg)
    return cfg


def runtime_paths(cfg: dict):
    mailbox = Path(cfg["mailbox_dir"])
    logs = Path(cfg["logs_dir"])
    config_path = Path(cfg.get("_config_path") or resolve_config_path())

    paths = {
        "ROOT": ROOT,
        "CONFIG": config_path,
        "MAILBOX": mailbox,
        "INBOX": mailbox / "inbox",
        "PROCESSING": mailbox / "processing",
        "DONE": mailbox / "done",
        "FAILED": mailbox / "failed",
        "BLOCKED": mailbox / "blocked",
        "ESCALATED": mailbox / "escalated",
        "LOGS": logs,
        "PROMPTS": ROOT / "runtime" / "prompts",
        "INCIDENTS": ROOT / "runtime" / "incidents",
        "QUEUE_SUMMARY_LOG": logs / "queue-summary.log",
        "TELEGRAM_ACTIONS": ROOT / "runtime" / "telegram_actions",
        "TELEGRAM_OFFSET": ROOT / "runtime" / "telegram_update_offset.txt",
        "SKIP_SIGNALS": ROOT / "runtime" / "metrics" / "plan_skip_signals.jsonl",
        "APPROVALS": ROOT / "runtime" / "approvals",
        "APPROVALS_RESOLVED": ROOT / "runtime" / "approvals" / "resolved",
    }

    for p in [
        paths["INBOX"],
        paths["PROCESSING"],
        paths["DONE"],
        paths["FAILED"],
        paths["BLOCKED"],
        paths["ESCALATED"],
        paths["LOGS"],
        paths["PROMPTS"],
        paths["INCIDENTS"],
        paths["TELEGRAM_ACTIONS"],
        paths["APPROVALS"],
        paths["APPROVALS_RESOLVED"],
    ]:
        p.mkdir(parents=True, exist_ok=True)

    return paths
