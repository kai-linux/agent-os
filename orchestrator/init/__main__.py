from __future__ import annotations

import argparse
import traceback
from pathlib import Path

from orchestrator.init import ui
from orchestrator.init import charter as charter_phase
from orchestrator.init import config_emit, cron_install, dialogue, github_scaffold, preflight, telegram_pair
from orchestrator.init.state import State, delete_state, resumable_states, slugify_repo_name
from orchestrator.paths import ROOT


def _log_error(message: str) -> None:
    log_path = ROOT / "runtime" / "logs" / "init.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(message.rstrip() + "\n")


def _select_resume_state() -> State | None:
    states = resumable_states()
    if not states:
        return None
    if len(states) == 1:
        ui.info(f"Resuming unfinished init for {states[0].get('github.repo_name', 'pending repo')}")
        return states[0]
    print("Unfinished init runs found:")
    for idx, state in enumerate(states, start=1):
        label = state.get("github.repo_name", state.path.stem)
        print(f"  [{idx}] {label}")
    print("  [n] Start a new init")
    while True:
        choice = ui.prompt("")
        if choice.lower() == "n":
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(states):
            return states[int(choice) - 1]
        ui.warn("Choose one of the listed states or 'n'.")


def _print_preflight_results(results: list[preflight.CheckResult]) -> None:
    for result in results:
        if result.ok:
            ui.ok(result.message)
        else:
            ui.fail(f"{result.message} — {result.hint}")


def _final_banner(state: State, telegram: dict[str, str]) -> None:
    ui.header("Step 7/7 — Done")
    github = state.get("github", {})
    print(f"  Repo:     {github.get('repo_url')}")
    print(f"  Project:  {github.get('project_url')}")
    print(f"  Logs:     {ROOT / 'runtime' / 'logs'}")
    print(f"  Telegram: @{telegram.get('bot_username')} — commands: /on /off /status /jobs /repos /help")
    print(f"\n  Config:   {state.get('config_written_path')}")
    print(f"  State:    {state.path}")
    print("\n  Expected first PR:     ~3-5 minutes from now")
    print("  Expected Telegram ping: within 1 minute")
    print("\n  To pause the whole system:  bin/agentos off")
    print("  To re-run init for another repo:  bin/agentos init")


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m orchestrator.init", description="Guided bootstrap for Agent OS")
    parser.add_argument("--dry-run", action="store_true", help="Preview actions without creating external resources")
    parser.add_argument("--reset", metavar="REPO", help="Delete saved init state for a repo slug/name and exit")
    args = parser.parse_args()

    if args.reset:
        path = delete_state(slugify_repo_name(args.reset))
        print(f"Deleted init state: {path}")
        return 0

    ui.header("Agent OS — guided setup")
    ui.info("Checking prerequisites...")
    try:
        results = preflight.run()
        _print_preflight_results(results)
    except preflight.PreflightError as exc:
        _print_preflight_results(exc.failures)
        return 1

    state = _select_resume_state() or State.for_slug("_pending")
    try:
        ui.header("Step 1/7 — What are you building?")
        intake = dialogue.run(state.get("intake"))
        if not state.get("intake"):
            state.mark("intake", intake)

        ui.header("Step 2/7 — GitHub repo")
        github = github_scaffold.run(state, intake, dry_run=args.dry_run)

        expected_slug = slugify_repo_name(github["repo_name"])
        expected_path = state.path.parent / f"{expected_slug}.json"
        if state.path != expected_path and not expected_path.exists():
            state.path.rename(expected_path) if state.path.exists() and state.path.name == "_pending.json" else None
            state.path = expected_path
            state.save()

        ui.header("Step 3/7 — Designing the charter")
        charter = charter_phase.run(state, intake, github, dry_run=args.dry_run)

        ui.header("Step 4/7 — Telegram control plane")
        telegram = telegram_pair.run(state, github["repo_full_name"], dry_run=args.dry_run)

        ui.header("Step 5/7 — Writing config.yaml")
        config_path = config_emit.run(state, intake, github, charter, telegram, dry_run=args.dry_run)
        ui.ok(f"Wrote {config_path}")
        ui.ok(f"Wrote {state.path}")

        ui.header("Step 6/7 — Installing cron")
        cron_install.run(state, dry_run=args.dry_run)
        state.complete()

        _final_banner(state, telegram)
        return 0
    except Exception as exc:
        _log_error(traceback.format_exc())
        ui.fail(str(exc))
        ui.warn(f"See {ROOT / 'runtime' / 'logs' / 'init.log'} for details.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
