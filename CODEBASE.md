# Codebase Memory

> Auto-maintained by agent-os. Agents read this before starting work and update it on completion.

## Architecture

(Fill in once the project structure stabilises. Agents will append discoveries below.)

## Key Files

(Agents append important file paths and their purpose here.)

## Known Issues / Gotchas

(Agents append anything surprising or that blocked them.)

## Recent Changes

### 2026-03-17 — [task-20260317-221804-task-pr-auto-merge-on-green-ci] (#3 kai-linux/agent-os)
Implemented `orchestrator/pr_monitor.py` — a new module that lists open PRs with "Agent:" title prefix across all configured repos, checks CI status via `gh pr checks`, auto-merges on green using `gh pr merge --squash`, and posts a structured failure comment + adds "blocked" label + sets project Status=Blocked on CI failure. Merge attempts are tracked in a JSON state file (`runtime/logs/pr_monitor_state.json`); each PR is retried at most 3 times before being escalated. Also added `bin/run_pr_monitor.sh` as the entry-point for a cron job running every 5 minutes.

**Files:** `- orchestrator/pr_monitor.py`, `- bin/run_pr_monitor.sh`

**Decisions:**
  - - Used a JSON state file (not PR labels) to track merge attempts — avoids extra GitHub API calls and label clutter
  - - gh pr checks returns non-zero exit on failing checks but still outputs valid JSON; captured both outcomes via subprocess.run without check=True
  - - Pending checks (state: pending/queued/in_progress) cause the PR to be skipped until the next poll rather than counted as a failure
  - - CONFLICTING mergeable state skips without incrementing attempt counter (not a CI issue)
  - - Draft PRs are skipped silently without incrementing attempt counter


### 2026-03-17 — [task-20260317-214604-task-test-runner-agent-role] (#4 kai-linux/agent-os)
Implemented a test runner role by adding a `run_tests()` function to `orchestrator/queue.py` that executes a configured test command in the worktree after the agent finishes (just before `parse_agent_result` reads the result file). Test results are appended to `.agent_result.md`'s TESTS_RUN section; if tests fail and the agent reported `complete`, the status is overridden to `partial` and BLOCKERS are updated. Documented `test_command` and `test_timeout_minutes` in `example.config.yaml` with per-repo `repo_configs` support.

**Files:** `- orchestrator/queue.py`, `- example.config.yaml`

**Decisions:**
  - - Used `repo_configs` dict in config.yaml keyed by resolved repo path (falling back to repo.name) for per-repo test commands — minimal change, no new files
  - - Shell=True for test_command to support compound commands like `cd src && pytest`
  - - Test runner modifies `.agent_result.md` before `parse_agent_result()` reads it — queue routing sees the final overridden status directly, no routing logic changes needed
  - - Failures (subprocess errors, timeouts) caught and recorded as ERROR/TIMEOUT labels, never crash the queue


### 2026-03-17 — [task-20260317-213404-task-parallel-queue-workers] (#1 kai-linux/agent-os)
Implemented parallel queue worker execution by creating a thin `orchestrator/supervisor.py` that spawns up to `max_parallel_workers` independent queue.py processes. Per-repo file locking (`fcntl.flock`) was added to `queue.py` so that no two workers can access the same repository simultaneously. Workers are identified by a `QUEUE_WORKER_ID` environment variable that appears in logs. Single-worker mode (`max_parallel_workers=1`) is unchanged in behavior.

**Files:** `- example.config.yaml`, `- orchestrator/supervisor.py`, `- orchestrator/queue.py`, `- bin/run_queue.sh`

**Decisions:**
  - - Thin supervisor (new file) over refactoring queue.py to minimize diff and risk
  - - Per-repo lock uses /tmp lock files with fcntl (no new dependencies, same mechanism as global queue lock)
  - - Locked-repo workers return task to inbox and exit; supervisor respawns workers, so tasks eventually run when repo is free
  - - Global flock in run_queue.sh kept on supervisor to prevent duplicate supervisor instances
  - - Worker IDs (w0, w1, ...) are sequential integers from supervisor lifetime counter


