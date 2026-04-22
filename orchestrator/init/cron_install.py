from __future__ import annotations

import difflib
import os
import subprocess
import tempfile
import time
from pathlib import Path

from orchestrator.init import ui
from orchestrator.init.state import State, utc_now_iso
from orchestrator.paths import ROOT


BLOCK_BEGIN = "# ── Agent OS (managed by agentos init) — begin ──"
BLOCK_END = "# ── Agent OS (managed by agentos init) — end ──"


def build_managed_block(root: Path = ROOT, path_env: str | None = None) -> str:
    env_path = path_env or os.environ.get("PATH", "")
    lines = [
        BLOCK_BEGIN,
        f"PATH={env_path}",
        "",
        "# Auto-pull latest orchestrator code",
        f"* * * * * {root}/bin/run_autopull.sh >> {root}/runtime/logs/autopull.log 2>&1",
        "",
        "# Core loop: dispatch → execute → merge",
        f"* * * * * {root}/bin/run_dispatcher.sh >> {root}/runtime/logs/dispatcher.log 2>&1",
        f"* * * * * {root}/bin/run_queue.sh >> {root}/runtime/logs/cron.log 2>&1",
        f"*/5 * * * * {root}/bin/run_pr_monitor.sh >> {root}/runtime/logs/pr_monitor.log 2>&1",
        "",
        "# Control plane (REQUIRED — this is how /on /off work; runs even when disabled)",
        f"* * * * * AGENT_OS_IGNORE_DISABLED=1 {root}/bin/run_telegram_control.sh >> {root}/runtime/logs/telegram_control.log 2>&1",
        "",
        "# Self-improvement",
        f"30 6 * * 1 {root}/bin/run_agent_scorer.sh >> {root}/runtime/logs/agent_scorer.log 2>&1",
        f"0 7 * * 1 {root}/bin/run_log_analyzer.sh >> {root}/runtime/logs/log_analyzer.log 2>&1",
        f"0 * * * * {root}/bin/run_backlog_groomer.sh >> {root}/runtime/logs/backlog_groomer.log 2>&1",
        f"0 * * * * {root}/bin/run_strategic_planner.sh >> {root}/runtime/logs/strategic_planner.log 2>&1",
        f"0 9 * * * {root}/bin/run_dependency_watcher.sh >> {root}/runtime/logs/dependency_watcher.log 2>&1",
        "",
        "# Daily digest",
        f"0 8 * * * {root}/bin/run_daily_digest.sh >> {root}/runtime/logs/daily_digest.log 2>&1",
        BLOCK_END,
    ]
    return "\n".join(lines) + "\n"


def strip_managed_block(current: str) -> tuple[str, str | None]:
    if BLOCK_BEGIN not in current:
        return current.rstrip("\n"), None
    before, _, rest = current.partition(BLOCK_BEGIN)
    managed, _, after = rest.partition(BLOCK_END)
    block = BLOCK_BEGIN + managed + BLOCK_END
    remaining = (before.rstrip("\n") + "\n" + after.lstrip("\n")).strip("\n")
    return remaining, block + "\n"


def merge_block(current: str, new_block: str) -> tuple[str, bool]:
    remaining, old_block = strip_managed_block(current)
    if old_block == new_block:
        return (current if current.endswith("\n") else current + "\n"), False
    merged = remaining.strip("\n")
    if merged:
        merged += "\n\n"
    merged += new_block.rstrip("\n") + "\n"
    return merged, True


def _current_crontab() -> str:
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=False, timeout=10)
    if result.returncode != 0:
        return ""
    return result.stdout


def _install_crontab(content: str) -> None:
    with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as handle:
        handle.write(content)
        temp_path = Path(handle.name)
    try:
        subprocess.run(["crontab", str(temp_path)], check=True, timeout=10)
    finally:
        temp_path.unlink(missing_ok=True)


def _wait_for_dispatcher_tick(log_path: Path, timeout_seconds: int = 90) -> bool:
    deadline = time.time() + timeout_seconds
    initial_mtime = log_path.stat().st_mtime if log_path.exists() else 0.0
    while time.time() < deadline:
        if log_path.exists() and log_path.stat().st_mtime > initial_mtime:
            return True
        time.sleep(3)
    return False


def _print_manual_instructions() -> None:
    print(f"Manual cron setup selected. No crontab changes were made.")
    print(f"See: {ROOT / 'CRON.md'}")
    print(f"Use /path/to/agent-os = {ROOT}")
    print("You can re-run `bin/agentos init` later and choose automatic install if you want.")


def run(state: State, *, dry_run: bool = False) -> str:
    print("How should cron be configured?")
    print("  [1] Manual setup via CRON.md (Recommended)")
    print("  [2] Install/update crontab automatically")
    choice = ui.choice("", ["1", "2"], default="1")
    if choice == "1":
        state.mark("cron_setup_mode", "manual")
        _print_manual_instructions()
        return "manual"

    current = _current_crontab()
    new_block = build_managed_block()
    merged, changed = merge_block(current, new_block)
    if not changed and state.get("cron_installed_at"):
        state.mark("cron_setup_mode", "automatic")
        return "automatic"

    if BLOCK_BEGIN in current and changed:
        _, old_block = strip_managed_block(current)
        diff = "".join(
            difflib.unified_diff(
                (old_block or "").splitlines(True),
                new_block.splitlines(True),
                fromfile="current",
                tofile="new",
            )
        ).strip()
        print(diff)
        print("\n  [1] Replace  [2] Skip  [3] Abort")
        choice = ui.choice("", ["1", "2", "3"], default="1")
        if choice == "2":
            state.mark("cron_setup_mode", "manual")
            _print_manual_instructions()
            return "manual"
        if choice == "3":
            raise RuntimeError("Aborted by user")

    if not dry_run:
        _install_crontab(merged)
    state.mark("cron_installed_at", utc_now_iso())
    state.mark("cron_setup_mode", "automatic")

    if dry_run:
        return "automatic"
    log_path = ROOT / "runtime" / "logs" / "dispatcher.log"
    _wait_for_dispatcher_tick(log_path, timeout_seconds=90)
    return "automatic"
