# Codebase Memory

> Auto-maintained by agent-os. Agents read this before starting work and update it on completion.

## Architecture

(Fill in once the project structure stabilises. Agents will append discoveries below.)

## Key Files

(Agents append important file paths and their purpose here.)

## Known Issues / Gotchas

(Agents append anything surprising or that blocked them.)

## Recent Changes

### 2026-03-18 — [task-20260318-082904-task-weekly-log-analyzer-auto-creates-improvement-] (#10 kai-linux/agent-os)
Implemented `orchestrator/log_analyzer.py` and `bin/run_log_analyzer.sh`. The analyzer reads the last 7 days of `runtime/metrics/agent_stats.jsonl` and `runtime/logs/queue-summary.log`, calls Claude Haiku with a deterministic structured prompt to identify the top 3 failure patterns/bottlenecks, deduplicates against existing open GitHub issues by exact title match, creates one issue per problem using the standard `## Goal / ## Success Criteria / ## Constraints` body format, and posts a summary to Telegram. A system cron fires every Monday at 07:00.

**Files:** `- orchestrator/log_analyzer.py`, `- bin/run_log_analyzer.sh`, `- CODEBASE.md`

**Decisions:**
  - - Reused load_recent_metrics() from agent_scorer.py to avoid duplicating JSONL parsing
  - - Prompt is fully deterministic (requests exact JSON schema, no open-ended fields) to avoid noisy/random issue titles across runs
  - - Deduplication uses gh issue list --search + exact title match to prevent duplicate improvement tasks
  - - Issue body uses standard ## Goal / ## Success Criteria / ## Constraints sections for dispatcher compatibility
  - - Added to system crontab (persistent) rather than session-only CronCreate


### 2026-03-18 — [task-20260318-065704-task-agent-performance-scorer-and-metrics-log] (#9 kai-linux/agent-os)
Added structured metrics logging to queue.py and a new weekly agent performance scorer. After each task completes, `record_metrics()` atomically appends a JSONL record (timestamp, task_id, repo, agent, status, attempt_count, duration_seconds, task_type) to `runtime/metrics/agent_stats.jsonl`, with automatic rotation at 10 MB. A new `orchestrator/agent_scorer.py` module reads the last 7 days of metrics, computes per-agent success rates, and creates a GitHub issue with the title "Agent X degraded (Y% success rate)" for any agent below 60%. `bin/run_agent_scorer.sh` is the cron entry point.

**Files:** `- orchestrator/queue.py`, `- orchestrator/agent_scorer.py`, `- bin/run_agent_scorer.sh`

**Decisions:**
  - - Atomic append implemented as read-existing + write-temp + os.replace() per task spec ("write to temporary file then rename"); handles single-writer safety
  - - record_metrics() is wrapped in try/except in main() so metrics failure never crashes the queue
  - - Rotation renames to .jsonl.1 (overwriting previous rotation); simple single-rotation keeps implementation minimal
  - - agent_scorer.py uses `gh issue create` via subprocess (same pattern as rest of codebase) rather than a new HTTP client
  - - github_repo config key falls back to scanning github_projects entries for a "repo" field if top-level github_repo is not set


### 2026-03-18 — [task-20260318-064404-task-priority-aware-queue-dispatch] (#5 kai-linux/agent-os)
Implemented priority-aware queue dispatch by adding a `priority_score()` helper to `queue.py` that computes score = priority_weight + age_bonus (1 pt/hr), replacing the FIFO `pick_task` with a `max()` selector. The dispatcher writes `priority` into task frontmatter from issue labels (defaulting to `prio:normal`), and the queue logs the priority label, weight, and score when a task is picked up.

**Files:** `- orchestrator/queue.py`, `- orchestrator/github_dispatcher.py`, `- example.config.yaml`

**Decisions:**
  - - Used file mtime as task age proxy (set at dispatch time, stable across queue restarts)
  - - Added cfg=None fallback in pick_task so any call without cfg still works (backward compat)
  - - Priority logged at also_summary=True so it appears in queue summary log, not just per-task log
  - - Weights dict defaults hardcoded in priority_score itself to avoid KeyError if priority_weights missing from config


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


