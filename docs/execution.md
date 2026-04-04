# How Tasks Execute

## 1. Dispatch

A human (or the system) creates a GitHub Issue. It can be a polished spec or a one-line note — the dispatcher's LLM formatter will restructure it into a proper task with goal, success criteria, constraints, and agent preference.

Set the Project status to **Ready** (or add the `ready` label — either triggers dispatch).

If you want only this manual `Ready -> dispatch -> PR` flow for a repo, set that repo's `automation_mode` to `dispatcher_only` in config.

## 2. Execution

The queue engine:
- Creates an **isolated git worktree** — each task gets its own branch in `/srv/worktrees/<repo>/<task-id>`, so agents never collide
- Selects the best agent based on **task type and priority** (implementation → Codex first, debugging → Claude first)
- Injects **CODEBASE.md** — the repo's accumulated memory from all prior agent work
- Runs the agent with a structured prompt including prior attempt history (if this is a retry)
- Parses the **`.agent_result.md`** handoff contract

## 3. Handoff

Every agent must write `.agent_result.md` before exiting — the universal contract:

```
STATUS: complete | partial | blocked
BLOCKER_CODE: none | missing_context | missing_credentials | environment_failure | dependency_blocked | quota_limited | runner_failure | timeout | test_failure | manual_intervention_required | fallback_exhausted | invalid_result_contract
SUMMARY: ...
DONE: ...
BLOCKERS: ...
NEXT_STEP: ...
FILES_CHANGED: ...
TESTS_RUN: ...
DECISIONS: ...
RISKS: ...
ATTEMPTED_APPROACHES: ...
MANUAL_STEPS: ...
```

This file is what makes multi-agent collaboration work. When an agent is blocked, the next agent in the fallback chain receives everything the first one tried, what failed, and what to do next. No context is lost between handoffs.

`BLOCKER_CODE` is required whenever `STATUS` is `partial` or `blocked`; `complete` outcomes should use `none`.

`MANUAL_STEPS` (cron entries, config changes, secrets) are surfaced back to GitHub for agent continuity, with secret values redacted.

## 4. Review & Merge

`pr_monitor.py` polls every 5 minutes:
- **CI green** → squash-merge, delete branch, close issue, move board to Done
- **Merge conflict** → auto-rebase onto main, force-push with lease, retry next poll
- **CI failure** → comment on issue with failed checks, redacting any detected secret material, label as blocked, retry up to 3 times, then escalate

## 5. Retry & Escalation

If a task returns `partial` or `blocked`:
- A **follow-up task** is created automatically with full prior context
- The next agent in the **fallback chain** takes over (e.g., Codex failed → Claude tries)
- After `max_attempts` (default 4), the system **escalates** — writes a structured note and stops

The system never thrashes. It tries, it hands off, it escalates. Like a real team.
