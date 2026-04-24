from __future__ import annotations

import argparse
import traceback
from pathlib import Path

import yaml

from orchestrator.init import ui
from orchestrator.init import charter as charter_phase
from orchestrator.init import config_emit, cron_install, dialogue, github_scaffold, preflight, telegram_pair, tuning
from orchestrator.init.state import State, delete_state, resumable_states, slugify_repo_name
from orchestrator.paths import ROOT


def _log_error(message: str) -> None:
    log_path = ROOT / "runtime" / "logs" / "init.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(message.rstrip() + "\n")


def _summarize_state(state: State) -> None:
    intake = state.get("intake", {})
    github = state.get("github", {})
    label = github.get("repo_name", "pending repo")
    print(f"Saved unfinished setup: {label}")
    if intake:
        print(f"  Idea: {intake.get('idea', '—')}")
        print(f"  Kind: {intake.get('kind', '—')}")
        print(f"  Stack pref: {intake.get('stack_preference', '—')}")
        print(f"  Success: {intake.get('success_criteria', '—')}")
    if github.get("inputs"):
        print(f"  Repo draft: {github['inputs'].get('owner', '')}/{github['inputs'].get('repo_name', '')}")
    print(f"  State file: {state.path}")


def _decide_resume_action(state: State) -> str:
    _summarize_state(state)
    print("\n  [1] Resume saved setup")
    print("  [2] Re-enter answers from Step 1")
    print("  [3] Discard saved setup and start fresh")
    return ui.choice("", ["1", "2", "3"], default="1")


def _select_resume_state() -> State | None:
    states = resumable_states()
    if not states:
        return None
    if len(states) == 1:
        action = _decide_resume_action(states[0])
        if action == "1":
            return states[0]
        if action == "2":
            states[0].reset()
            return states[0]
        states[0].path.unlink(missing_ok=True)
        return None
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
            selected = states[int(choice) - 1]
            action = _decide_resume_action(selected)
            if action == "1":
                return selected
            if action == "2":
                selected.reset()
                return selected
            selected.path.unlink(missing_ok=True)
            return None
        ui.warn("Choose one of the listed states or 'n'.")


def _print_preflight_results(results: list[preflight.CheckResult]) -> None:
    for result in results:
        if result.ok:
            ui.ok(result.message)
        else:
            ui.fail(f"{result.message} — {result.hint}")


def _confirm_config_merge_mode(existing_cfg: dict | None) -> str:
    """Prompt the operator about preserving an existing config.yaml.

    Returns one of: ``merge`` (default), ``abort``. Operators get a clear
    choice before their existing tuning is touched. Merge mode now preserves
    scalar settings via setdefault, so the only reason to abort is when the
    operator wants to hand-edit the file instead.
    """
    if not existing_cfg:
        return "merge"
    ui.warn("An existing config.yaml was detected.")
    print("  [1] Add this project to the existing config (merge, preserve current settings)")
    print("  [2] Abort — I'll edit config.yaml manually")
    choice = ui.choice("", ["1", "2"], default="1")
    return "merge" if choice == "1" else "abort"


def _final_banner(state: State, telegram: dict[str, str], cron_mode: str) -> None:
    ui.header("Step 8/8 — Done")
    github = state.get("github", {})
    print(f"  Repo:     {github.get('repo_url')}")
    print(f"  Project:  {github.get('project_url')}")
    print(f"  Logs:     {ROOT / 'runtime' / 'logs'}")
    print(f"  Telegram: @{telegram.get('bot_username')} — commands: /on /off /status /jobs /repos /help")
    print(f"\n  Config:   {state.get('config_written_path')}")
    print(f"  State:    {state.path}")
    if cron_mode == "manual":
        print(f"  Cron:     not installed automatically — see {ROOT / 'CRON.md'}")
    else:
        print("  Cron:     installed")
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
        existing_cfg = None
        config_path = ROOT / "config.yaml"
        if config_path.exists():
            existing_cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

        ui.header("Step 1/8 — What are you building?")
        intake = dialogue.run(state.get("intake"))
        if not state.get("intake"):
            state.mark("intake", intake)

        ui.header("Step 2/8 — GitHub repo")
        github = github_scaffold.run(state, intake, dry_run=args.dry_run)

        expected_slug = slugify_repo_name(github["repo_name"])
        expected_path = state.path.parent / f"{expected_slug}.json"
        if state.path != expected_path and not expected_path.exists():
            state.path.rename(expected_path) if state.path.exists() and state.path.name == "_pending.json" else None
            state.path = expected_path
            state.save()

        ui.header("Step 3/8 — Designing the charter and supporting docs")
        charter = charter_phase.run(state, intake, github, dry_run=args.dry_run)

        ui.header("Step 4/8 — Telegram control plane")
        telegram = telegram_pair.run(state, github["repo_full_name"], existing_cfg=existing_cfg, dry_run=args.dry_run)

        ui.header("Step 5/8 — Tuning cadence and thresholds")
        tuning_choices = tuning.run(state, existing_cfg=existing_cfg)

        ui.header("Step 6/8 — Writing config.yaml")
        merge_mode = _confirm_config_merge_mode(existing_cfg)
        if merge_mode == "abort":
            ui.warn("Aborted before config.yaml changes. Your saved init state at "
                    f"{state.path} is preserved; rerun `bin/agentos init` to resume.")
            return 1
        config_path = config_emit.run(state, intake, github, charter, telegram, dry_run=args.dry_run, tuning=tuning_choices)
        ui.ok(f"Wrote {config_path}")
        ui.ok(f"Wrote {state.path}")

        ui.header("Step 7/8 — Cron setup")
        cron_mode = cron_install.run(state, dry_run=args.dry_run)
        state.complete()

        _final_banner(state, telegram, cron_mode)
        return 0
    except Exception as exc:
        _log_error(traceback.format_exc())
        ui.fail(str(exc))
        ui.warn(f"See {ROOT / 'runtime' / 'logs' / 'init.log'} for details.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
