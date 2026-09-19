"""Delivery coordinator and compatibility boundary for GitHub/mailbox workers."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from uuid import uuid4

from orchestrator.delivery_checks import verify_goal
from orchestrator.delivery_store import (
    TERMINAL,
    DeliveryConflict,
    DeliveryStore,
    store_path,
)
from orchestrator.gh_project import gh, gh_json, query_project, set_item_status
from orchestrator.privacy import redact_text


def managed_store(cfg, meta):
    return DeliveryStore(store_path(cfg)) if meta.get("goal_id") else None


def begin_worker(cfg, meta, worker, agent, timeout_minutes, worktree):
    store = managed_store(cfg, meta)
    if store is None:
        from orchestrator.reliability import enforced
        if enforced(cfg):
            raise DeliveryConflict("Unmanaged legacy work cannot bypass enforced delivery controls")
        return None
    ident, revision = meta["goal_id"], int(meta["goal_revision"])
    from orchestrator.reliability import execution_gate
    execution_gate(cfg, store.get(ident))
    store.bind_execution(
        ident,
        revision,
        {
            "worktree": str(worktree),
            "branch": meta["branch"],
            "task_id": meta["task_id"],
        },
    )
    key = meta["task_id"] + ":" + agent + ":" + uuid4().hex[:12]
    store.begin_attempt(
        ident,
        revision,
        key,
        worker + ":" + agent,
        lease_seconds=timeout_minutes * 60 + 60,
        reserve_usd=float(cfg.get("delivery_attempt_reservation_usd", 0)),
    )
    from orchestrator.reliability_store import ReliabilityStore
    ReliabilityStore(cfg).start_span(ident, revision, "worker", key="attempt:" + key,
                                    attributes={"attempt_id": key})
    meta["delivery_attempt_id"] = key
    meta.pop("usage_receipts", None)
    meta.pop("usage_complete", None)
    return key


def finish_worker(cfg, meta, key, result, *, retry=False):
    if not key:
        return
    store = managed_store(cfg, meta)
    store.finish_attempt(
        key,
        {
            "status": result.get("status"),
            "blocker_code": result.get("blocker_code"),
            "summary": redact_text(result.get("summary", "")),
        },
    )
    from orchestrator.reliability_store import ReliabilityStore
    ReliabilityStore(cfg).end_span("attempt:" + key, error=None if result.get("status") == "complete" else RuntimeError())
    if meta.get("delivery_attempt_id") == key and meta.get("usage_complete") and meta.get("usage_receipts"):
        try:
            ReliabilityStore(cfg).seal_usage(key, meta["usage_receipts"], actor="qualified-worker-collector")
        except DeliveryConflict:
            store.heartbeat("usage_reconciliation", "Provider receipts remain provisional; costs are unknown")
    if retry and store.get(meta["goal_id"])["state"] == "verifying":
        store.retry(
            meta["goal_id"],
            int(meta["goal_revision"]),
            "Different available provider after "
            + str(result.get("blocker_code", "worker failure")),
        )


def recover_verified_outcome(cfg, meta, result):
    store = managed_store(cfg, meta)
    if store and verify_goal(store, meta["goal_id"], cfg):
        return {
            **result,
            "status": "complete",
            "blocker_code": "none",
            "delivery_state": "succeeded",
            "summary": "Outcome independently verified despite the worker result. "
            + str(result.get("summary", "")),
        }
    return None


def run_monitored(argv, cwd, logfile, *, timeout_seconds, store, ident, revision, cfg=None):
    """Fence stale work and stop the actual process group on pause/cancellation."""
    started = time.monotonic()
    environment = None
    from orchestrator.reliability import enforced, execution_gate, profile, profile_for
    if cfg and enforced(cfg):
        from orchestrator.worker_isolation import sandbox_command
        goal = store.get(ident)
        execution_gate(cfg, goal)
        policy = profile(cfg, profile_for(cfg, goal))["sandbox"]
        argv, environment = sandbox_command(argv, cwd, policy, readonly=[argv[0], argv[-1]], controller_root=cfg.get("root_dir"))
    next_gate = started + 5
    with Path(logfile).open("a", encoding="utf-8") as output:
        os.chmod(logfile, 0o600)
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=environment,
        )
        try:
            while proc.poll() is None:
                if cfg and enforced(cfg) and time.monotonic() >= next_gate:
                    execution_gate(cfg, store.get(ident))
                    next_gate = time.monotonic() + 5
                if not store.execution_allowed(ident, revision):
                    raise DeliveryConflict(
                        "Goal paused or cancelled while worker was running",
                        code="ancestor_paused",
                    )
                if time.monotonic() - started > timeout_seconds:
                    raise subprocess.TimeoutExpired(argv, timeout_seconds)
                time.sleep(0.25)
            if proc.returncode:
                with Path(logfile).open("rb") as handle:
                    handle.seek(max(0, Path(logfile).stat().st_size - 8192))
                    tail = redact_text(handle.read().decode("utf-8", errors="replace"))
                raise subprocess.CalledProcessError(
                    proc.returncode, argv, output=tail, stderr=tail
                )
        finally:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def settle_result(cfg, meta, result):
    store = managed_store(cfg, meta)
    if not store:
        return result
    ident = meta["goal_id"]
    goal = store.get(ident)
    if goal["revision"] != int(meta["goal_revision"]) or goal["state"] in {
        "cancelled",
        "paused",
    }:
        return {
            **result,
            "status": "blocked",
            "delivery_state": goal["state"],
            "blocker_code": "manual_intervention_required",
        }
    verified = verify_goal(store, ident, cfg)
    goal = store.get(ident)
    if verified:
        return {
            **result,
            "status": "complete",
            "blocker_code": "none",
            "delivery_state": "succeeded",
            "summary": "Outcome independently verified. "
            + str(result.get("summary", "")),
        }
    if result.get("status") == "complete" and goal["state"] == "verifying":
        repairable = {
            c["id"]
            for c in goal["contract"].get("checks", [])
            if c.get("type") in {"file", "configured_command"}
        }
        failed = [
            e["check_id"]
            for e in store.snapshot()["evidence"]
            if e["goal_id"] == ident
            and e["revision"] == goal["revision"]
            and not e["passed"]
            and e["check_id"] in repairable
        ]
        if failed:
            repairs = int(goal["metadata"].get("acceptance_repairs", 0))
            reason = "Independent acceptance failed: " + ", ".join(sorted(failed))
            store.remember(
                ident, actor="independent-verifier", note=reason, category="observation"
            )
            if repairs < 2:
                store.bind_execution(
                    ident, goal["revision"], {"acceptance_repairs": repairs + 1}
                )
                store.retry(
                    ident,
                    goal["revision"],
                    reason + "; correct the deliverable without changing the contract",
                )
            else:
                store.wait(
                    ident,
                    goal["revision"],
                    reason
                    + "; two correction attempts did not satisfy the checks. Review the deliverable, capability and contract before resuming",
                )
            return {
                **result,
                "status": "partial",
                "blocker_code": "acceptance_failed",
                "delivery_state": store.get(ident)["state"],
            }
    if result.get("status") != "complete":
        code = result.get("blocker_code", "verification_required")
        reason = (
            (result.get("unblock_notes") or {}).get("next_action")
            or result.get("next_step")
            or result.get("summary")
            or "Inspect the attempt and reconcile progress"
        )
        progress = goal["metadata"].get("prepared_commit")
        if (
            code
            not in {
                "missing_context",
                "missing_credentials",
                "manual_intervention_required",
                "environment_failure",
                "quota_limited",
                "fallback_exhausted",
            }
            and progress
            and progress != goal["metadata"].get("last_continued_commit")
            and goal["state"] == "verifying"
        ):
            store.bind_execution(
                ident, goal["revision"], {"last_continued_commit": progress}
            )
            store.retry(
                ident,
                goal["revision"],
                "New committed progress; continue the original outcome within its remaining budget",
            )
        else:
            wake_at = (
                time.time() + 3600
                if code in {"quota_limited", "fallback_exhausted"}
                else None
            )
            store.wait(ident, goal["revision"], f"{code}: {reason}", wake_at=wake_at)
    elif not goal["contract"].get("checks"):
        store.wait(
            ident,
            goal["revision"],
            "Human acceptance required: no outcome checks were specified. Review the deliverable, then /goal accept "
            + ident,
        )
    goal = store.get(ident)
    return {**result, "delivery_state": goal["state"]}


def _project_github(cfg, goal):
    meta, state = goal["metadata"], goal["state"]
    repo, number = meta.get("github_repo"), meta.get("github_issue_number")
    if not repo or not number:
        return {"skipped": "no linked issue"}
    issue = (
        gh_json(
            [
                "issue",
                "view",
                str(number),
                "-R",
                repo,
                "--json",
                "state,stateReason,labels,comments",
            ]
        )
        or {}
    )
    # Never reopen an issue. A merged PR may close it before deployment checks
    # pass; verification/cancellation still need their own board projection.
    if issue.get("state") == "CLOSED" and state in {"running", "ready", "backlog"}:
        return {"skipped": "externally closed"}
    label_for = {
        "ready": "ready",
        "backlog": "backlog",
        "running": "in-progress",
        "waiting": "blocked",
        "paused": "blocked",
        "verifying": "verification-required",
        "succeeded": "done",
        "failed": "blocked",
        "cancelled": "cancelled",
    }
    desired = {label_for[state]}
    if (
        state in {"waiting", "paused"}
        and goal.get("wake_at") is None
        and not goal["reason"].startswith("dependency:")
    ):
        desired.add("human-required")
    for label in sorted(desired):
        gh(["label", "create", label, "-R", repo, "--force"])
    present = {l["name"] for l in issue.get("labels", [])}
    managed = set(label_for.values()) | {"agent-dispatched", "human-required"}
    cmd = [
        "issue",
        "edit",
        str(number),
        "-R",
        repo,
        "--add-label",
        ",".join(sorted(desired)),
    ]
    remove = sorted((present & managed) - desired)
    if remove:
        cmd += ["--remove-label", ",".join(remove)]
    gh(cmd)
    if state == "succeeded" and issue.get("state") != "CLOSED":
        gh(["issue", "close", str(number), "-R", repo, "--reason", "completed"])
    if state == "cancelled" and issue.get("state") != "CLOSED":
        gh(["issue", "close", str(number), "-R", repo, "--reason", "not planned"])
    marker = f"<!-- agent-os-delivery:{goal['id']} -->"
    body = (
        marker
        + f"\n## Delivery status\n\nGoal: `{goal['id']}` revision {goal['revision']}\n\nState: **{state}**\n\n{redact_text(goal['reason'])}"
    )
    previous = next(
        (c for c in issue.get("comments", []) if c.get("body", "").startswith(marker)),
        None,
    )
    if previous:
        if previous.get("body") != body:
            comment_id = str(previous["url"]).rsplit("issuecomment-", 1)[-1]
            gh(
                [
                    "api",
                    "--method",
                    "PATCH",
                    f"repos/{repo}/issues/comments/{comment_id}",
                    "-f",
                    "body=" + body,
                ]
            )
    else:
        gh(["issue", "comment", str(number), "-R", repo, "--body", body])
    pcfg = (cfg.get("github_projects") or {}).get(meta.get("github_project_key"))
    if pcfg:
        info = query_project(pcfg["project_number"], cfg["github_owner"])
        status = {
            "ready": pcfg.get("ready_value", "Ready"),
            "backlog": pcfg.get("backlog_value", "Backlog"),
            "running": pcfg.get("in_progress_value", "In Progress"),
            "succeeded": pcfg.get("done_value", "Done"),
            "verifying": pcfg.get("review_value", "In Review"),
            "cancelled": pcfg.get("done_value", "Done"),
        }.get(state, pcfg.get("blocked_value", "Blocked"))
        option = info["status_options"].get(status) or info["status_options"].get(
            pcfg.get("blocked_value", "Blocked")
        )
        item = next(
            (i for i in info["items"] if i.get("url") == meta.get("github_issue_url")),
            None,
        )
        if not option or not item:
            raise DeliveryConflict(
                "Linked project item/status missing; issue updated but board delivery is pending"
            )
        set_item_status(
            info["project_id"], item["item_id"], info["status_field_id"], option
        )
    return {"state": state, "issue": number}


def flush_outbox(cfg, *, sender=None, projector=None):
    store = DeliveryStore(store_path(cfg))
    rows = store.claim_outbox()
    groups = {}
    for row in rows:
        groups.setdefault((row["goal_id"], row["channel"]), []).append(row)
    for (ident, channel), events in groups.items():
        goal = store.get(ident)
        try:
            meaningful = [e for e in events if e["kind"] in {"state", "created"}]
            if not meaningful:
                receipt = {"skipped": "internal evidence or decision event"}
            elif channel == "github":
                receipt = (projector or _project_github)(cfg, goal)
            elif not cfg.get("telegram_bot_token") or not cfg.get("telegram_chat_id"):
                receipt = {"skipped": "telegram not configured"}
            else:
                if sender is None:
                    from orchestrator.queue import send_telegram

                    sender = lambda text: send_telegram(cfg, text, None)
                event_id = meaningful[-1]["event_id"]
                receipt = sender(
                    redact_text(
                        f"Delivery: {goal['state']}\n{goal['title']}\nGoal: {ident} / revision {goal['revision']}\n"
                        f"{goal['reason']}\n{goal['metadata'].get('github_issue_url', '')}\nEvent: {event_id}"
                    )
                )
                if not receipt:
                    raise DeliveryConflict("Telegram did not acknowledge delivery")
            for event in events:
                store.finish_delivery(event["event_id"], channel, receipt=receipt)
        except Exception as exc:
            for event in events:
                store.finish_delivery(
                    event["event_id"], channel, error=type(exc).__name__
                )


def tick(cfg, *, publish=True):
    from orchestrator.scheduler_state import job_lock

    with job_lock(cfg, "delivery_coordinator") as acquired:
        if acquired:
            _tick(cfg, publish=publish)


def _tick(cfg, *, publish=True):
    """Run from existing queue/dispatcher cadence, including when no tasks are ready."""
    store = DeliveryStore(store_path(cfg))
    store.tick()
    goals = store.list_goals()
    snapshot = store.snapshot()
    if publish:
        from orchestrator.github_sync import create_pr_for_branch

        for goal in goals:
            meta = goal["metadata"]
            if (
                goal["state"] not in {"waiting", "verifying"}
                or not meta.get("pr_delivery_pending")
                or time.time() < meta.get("pr_retry_at", 0)
            ):
                continue
            tries = int(meta.get("pr_delivery_tries", 0))
            if tries >= 5:
                store.wait(
                    goal["id"],
                    goal["revision"],
                    "GitHub PR delivery failed five times; repair GitHub access before resuming",
                )
                continue
            store.bind_execution(
                goal["id"],
                goal["revision"],
                {
                    "pr_delivery_tries": tries + 1,
                    "pr_retry_at": time.time() + 60 * 2**tries,
                },
            )
            try:
                url = create_pr_for_branch(
                    meta["github_repo"],
                    meta["branch"],
                    f"Agent: {meta['task_id']}",
                    f"Closes #{meta['github_issue_number']}\n\nGoal: {goal['id']} revision {goal['revision']}",
                )
                if not url:
                    raise DeliveryConflict("PR not acknowledged")
                store.bind_execution(
                    goal["id"],
                    goal["revision"],
                    {"pr_delivery_pending": False, "pr_url": url},
                )
                store.await_verification(
                    goal["id"],
                    goal["revision"],
                    "Prepared work has a PR; awaiting verified delivery",
                )
            except Exception:
                store.wait(
                    goal["id"],
                    goal["revision"],
                    "PR delivery pending; retrying the GitHub handoff without rerunning a worker",
                )
        goals = store.list_goals()
    if publish:
        last_check = next(
            (
                h["observed_at"]
                for h in snapshot["health"]
                if h["component"] == "source_reconciliation"
            ),
            0,
        )
        if time.time() - last_check >= 60:
            source_errors = 0
            for goal in goals:
                meta = goal["metadata"]
                if (
                    goal["state"] in TERMINAL
                    or not meta.get("github_repo")
                    or not meta.get("github_issue_number")
                ):
                    continue
                try:
                    issue = gh_json(
                        [
                            "issue",
                            "view",
                            str(meta["github_issue_number"]),
                            "-R",
                            meta["github_repo"],
                            "--json",
                            "title,body,state,stateReason,labels,number",
                        ]
                    )
                    if not issue or "state" not in issue:
                        raise DeliveryConflict("Issue source could not be read")
                    if issue["state"] == "CLOSED":
                        if not verify_goal(store, goal["id"], cfg):
                            from orchestrator.delivery_checks import observe

                            merged = False
                            if issue.get("stateReason") != "NOT_PLANNED" and (
                                meta.get("pr_url") or meta.get("branch")
                            ):
                                merged = observe({"type": "merged_pr"}, goal, cfg)[0]
                            if merged:
                                if goal["state"] not in {
                                    "verifying",
                                    "waiting",
                                    "paused",
                                }:
                                    store.await_verification(
                                        goal["id"],
                                        goal["revision"],
                                        "PR merged; other outcome checks remain outstanding",
                                    )
                            else:
                                store.control(
                                    goal["id"],
                                    "cancel",
                                    actor="github",
                                    note="Issue closed externally; stopped without reopening or claiming verified success",
                                )
                    elif (
                        issue["title"] + "\n\n" + str(issue.get("body") or "")
                        != goal["original"]
                    ):
                        store.bind_execution(
                            goal["id"], goal["revision"], {"pending_source": issue}
                        )
                        if goal["state"] != "paused" or not meta.get("pending_source"):
                            store.control(
                                goal["id"],
                                "pause",
                                actor="github",
                                note="Source intent changed; /goal revise "
                                + goal["id"]
                                + " to adopt a new scope revision",
                            )
                except Exception:
                    source_errors += 1
            store.heartbeat(
                "source_reconciliation",
                "ok" if not source_errors else f"{source_errors} source reads failed",
            )
            goals = store.list_goals()
    by_id = {g["id"]: g for g in goals}
    for goal in goals:
        if goal["state"] == "waiting" and goal["reason"].startswith("ancestor_paused:"):
            ancestors = store.context(goal["id"])["lineage"][1:]
            if ancestors and all(
                a["state"] not in TERMINAL | {"paused", "waiting", "backlog"}
                for a in ancestors
            ):
                store.control(
                    goal["id"],
                    "resume",
                    actor="coordinator",
                    note="Parent scope is authorized to continue",
                )
        if goal["state"] == "waiting" and goal["reason"].startswith("dependency:"):
            lineage = {g["id"] for g in store.context(goal["id"])["lineage"]}
            requirements = [
                d["requires_id"]
                for d in snapshot["dependencies"]
                if d["goal_id"] in lineage
            ]
            if requirements and all(
                by_id[k]["state"] == "succeeded" for k in requirements
            ):
                store.control(
                    goal["id"],
                    "resume",
                    actor="coordinator",
                    note="All prerequisite outcomes independently verified",
                )
    # Leaves first; parents only complete after their own combined acceptance.
    depth = {g["id"]: len(store.context(g["id"])["lineage"]) for g in goals}
    for goal in sorted(goals, key=lambda g: depth[g["id"]], reverse=True):
        if goal["state"] in {"verifying", "waiting"} or (
            goal["kind"] != "task"
            and goal["metadata"].get("plan_materialized")
            and goal["state"] in {"ready", "running", "waiting"}
        ):
            verify_goal(store, goal["id"], cfg)
    # A wake/answer reuses the same goal and task. Never manufacture a new goal
    # from the failed worker's next step or reset the lineage budget.
    mailbox = Path(
        cfg.get("mailbox_dir", Path(cfg.get("root_dir", ".")) / "runtime" / "mailbox")
    )
    if (mailbox / "blocked").exists():
        import yaml

        from orchestrator.queue import parse_task

        for path in sorted((mailbox / "blocked").glob("*.md")):
            try:
                meta, body = parse_task(path)
            except (ValueError, OSError):
                continue
            if not meta.get("goal_id"):
                continue
            goal = store.get(meta["goal_id"])
            if (
                goal["revision"] == meta.get("goal_revision")
                and goal["state"] == "succeeded"
            ):
                destination = mailbox / "done" / path.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                if not destination.exists():
                    path.rename(destination)
                continue
            if (
                goal["revision"] == meta.get("goal_revision")
                and goal["state"] == "ready"
                and store.execution_allowed(goal["id"], goal["revision"])
            ):
                meta["model_attempts"] = []
                target = mailbox / "inbox" / path.name
                if not target.exists():
                    path.write_text(
                        "---\n"
                        + yaml.safe_dump(meta, sort_keys=False)
                        + "---\n\n"
                        + body
                    )
                    target.parent.mkdir(parents=True, exist_ok=True)
                    path.rename(target)
    for goal in store.list_goals():
        meta = goal["metadata"]
        if goal["state"] != "ready" or not meta.get("mailbox_payload"):
            continue
        name = meta["task_id"] + ".md"
        if any(
            (mailbox / state / name).exists()
            for state in (
                "inbox",
                "processing",
                "blocked",
                "done",
                "failed",
                "escalated",
            )
        ):
            continue
        destination = mailbox / "inbox" / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with destination.open("x", encoding="utf-8") as handle:
                handle.write(meta["mailbox_payload"])
        except FileExistsError:
            pass
    if publish:
        flush_outbox(cfg)
    from orchestrator.reliability import monitor
    monitor(cfg)
    store.heartbeat("delivery_coordinator")


def command(cfg, args, *, actor):
    from orchestrator.reliability import authorize, tenant_for
    store = DeliveryStore(store_path(cfg))
    if not args or args[0] == "list":
        return (
            "\n".join(
                f"{g['id']} {g['kind']} {g['state']}: {g['title']}"
                for g in store.list_goals() if _visible_goal(cfg, g, actor)
            )
            or "No managed goals yet."
        )
    if len(args) < 2:
        return "Usage: /goal status|pause|resume|cancel|revise|answer|accept|risk|actions|receipt <goal_id> [reason]"
    action, ident, note = args[0], args[1], " ".join(args[2:])
    if action == "adopt":
        from orchestrator.delivery_contract import register_issue

        repo, number = ident.rsplit("#", 1)
        authorize(cfg, tenant_for(cfg, {"metadata": {"github_repo": repo}}), actor, "control")
        location = next(
            (
                (pk, r)
                for pk, p in cfg.get("github_projects", {}).items()
                for r in p.get("repos", [])
                if r["github_repo"] == repo
            ),
            None,
        )
        if location is None:
            raise DeliveryConflict("Only configured workspaces can be adopted")
        issue = gh_json(
            [
                "issue",
                "view",
                str(int(number)),
                "-R",
                repo,
                "--json",
                "number,title,body,url,state,labels",
            ]
        )
        if not issue or not issue.get("state"):
            raise DeliveryConflict("Cannot read the source issue")
        goal = register_issue(
            cfg,
            location[0],
            location[1],
            issue,
            note or "implementation",
            ready=issue["state"] == "CLOSED",
        )
        if issue["state"] == "CLOSED" and not goal["metadata"].get("mailbox_payload"):
            store.bind_execution(
                goal["id"], goal["revision"], {"historical_import": True}
            )
        store.remember(
            goal["id"],
            actor=actor,
            note="Adopted an existing issue; legacy completion and cost claims are not imported as evidence",
        )
        if issue["state"] == "CLOSED" and not verify_goal(store, goal["id"], cfg):
            store.control(
                goal["id"],
                "cancel",
                actor=actor,
                note="Closed legacy issue lacks independent acceptance proof; not reopened or counted as verified success",
            )
        goal = store.get(goal["id"])
        return f"{goal['id']}: {goal['state']}\n{goal['reason']}"
    goal = store.get(ident)
    authorize(cfg, tenant_for(cfg, goal), actor,
              "read" if action in {"status", "actions"} else action if action in {"accept", "receipt"} else "control")
    from orchestrator.reliability_store import ReliabilityStore
    ReliabilityStore(cfg).audit(tenant_for(cfg, goal), actor, "goal." + action, ident)
    if action == "status":
        children = [g for g in store.list_goals() if g["parent_id"] == ident]
        return (
            f"{goal['kind']}: {goal['title']}\n{ident} revision {goal['revision']}: {goal['state']}\n"
            f"{goal['reason']}\nChildren: {sum(g['state'] == 'succeeded' for g in children)}/{len(children)} verified\n"
            + "\n".join(f"{g['id']} {g['state']}: {g['title']}" for g in children)
        )
    if action == "accept":
        if not note.strip():
            raise ValueError(
                "Acceptance needs a reason describing the verified outcome"
            )
        checks = goal["contract"].get("checks", [])
        human = [c["id"] for c in checks if c["type"] == "human"] or (
            ["human_acceptance"] if not checks else []
        )
        if not human:
            raise DeliveryConflict(
                "This goal requires independent checks; human acceptance cannot bypass them"
            )
        for key in human:
            store.record_evidence(
                ident, goal["revision"], key, True, "human:" + actor, {"note": note}
            )
        store.verify(ident)
        store.remember(ident, actor=actor, note=note)
    elif action == "risk":
        store.remember(ident, actor=actor, note=note, category="risk")
    elif action == "actions":
        return json.dumps(store.list_actions(ident), indent=2)
    elif action == "receipt":
        if len(args) < 4 or not " ".join(args[3:]).strip():
            raise ValueError(
                "Usage: /goal receipt <goal_id> <action_id> <verified external receipt reference>"
            )
        if args[2] not in {a["id"] for a in store.list_actions(ident)}:
            raise DeliveryConflict("Action does not belong to this goal")
        reference = " ".join(args[3:])
        store.confirm_action(args[2], {"verified_by": actor, "reference": reference})
        store.remember(
            ident,
            actor=actor,
            note="External action reconciled: " + args[2] + " " + reference,
        )
    elif action == "revise":
        from orchestrator.delivery_contract import issue_contract

        pending = goal["metadata"].get("pending_source")
        if not pending:
            raise DeliveryConflict(
                "Edit the linked issue first; the coordinator will pause it for scope revision"
            )
        pending_with_kind = {
            **pending,
            "labels": [*pending.get("labels", []), {"name": "task:" + goal["kind"]}],
        }
        kind, contract = issue_contract(
            pending_with_kind,
            {"github_repo": goal["metadata"]["github_repo"]},
            goal["metadata"]["task_type"],
        )
        if contract.get("parent") or contract.get("depends_on") or kind != goal["kind"]:
            raise DeliveryConflict(
                "Changing hierarchy requires a new linked goal; this revision may change scope and checks only"
            )
        store.control(
            ident,
            "revise",
            actor=actor,
            note=note,
            original=pending["title"] + "\n\n" + str(pending.get("body") or ""),
            contract=contract,
            title=pending["title"],
        )
    else:
        store.control(ident, action, actor=actor, note=note)
    current = store.get(ident)
    return f"{ident}: {current['state']}\n{current['reason']}"


def _visible_goal(cfg, goal, actor):
    from orchestrator.reliability import authorize, tenant_for
    try:
        authorize(cfg, tenant_for(cfg, goal), actor, "read")
        return True
    except DeliveryConflict:
        return False


def main():
    from orchestrator.paths import load_config

    parser = argparse.ArgumentParser(
        description="Manage persistent programs, projects and goals"
    )
    parser.add_argument("args", nargs="*")
    parser.add_argument("--snapshot", action="store_true")
    parser.add_argument("--tick", action="store_true")
    opts = parser.parse_args()
    cfg = load_config()
    if opts.snapshot or opts.tick:
        from orchestrator.reliability import authorize
        authorize(cfg, "controller", "local:" + str(os.getuid()), "admin")
    if opts.snapshot:
        from orchestrator.delivery_metrics import operational_snapshot

        print(json.dumps(operational_snapshot(cfg), indent=2))
    elif opts.tick:
        tick(cfg)
    else:
        print(command(cfg, opts.args, actor="local:" + str(os.getuid())))


if __name__ == "__main__":
    main()
