# Codebase Memory

> Auto-maintained by agent-os. Agents read this before starting work and update it on completion.

## Architecture

(Fill in once the project structure stabilises. Agents will append discoveries below.)

## Key Files

(Agents append important file paths and their purpose here.)

## Known Issues / Gotchas

(Agents append anything surprising or that blocked them.)

## Recent Changes

### 2026-03-18 — [task-20260318-224806-task-task-decomposer-agent] (#2 kai-linux/agent-os)
Added dispatch-time epic decomposition via `orchestrator/task_decomposer.py` and a narrow hook in `orchestrator/github_dispatcher.py`. Before writing a mailbox task, the dispatcher now asks Claude Haiku for a structured JSON classification; atomic issues continue unchanged, while epic issues are split into 2-5 ordered sub-issues on the same repo. Each child body starts with `Part of #N`, the parent is moved back to Backlog with an `epic` label, the first child is dispatched immediately, and remaining children are left in Backlog. Any decomposition failure falls back to dispatching the original issue unchanged.

**Files:** `- orchestrator/task_decomposer.py`, `- orchestrator/github_dispatcher.py`, `- orchestrator/gh_project.py`, `- tests/test_task_decomposer.py`

**Decisions:**
  - - Reused the existing `task_formatter.py` pattern: deterministic prompt, JSON-only response, fence stripping, and safe fallback on parse or CLI failure
  - - Kept decomposition inside the dispatcher rather than a separate queue stage so no new cron/job wiring was needed
  - - Parent issues are preserved as umbrella epics; child issues inherit non-workflow labels while workflow labels stay controlled by dispatch/project status
  - - Project status updates are best-effort and non-blocking; if they fail, issue creation still succeeds and the original dispatch path is preserved

### 2026-03-18 — [task-20260318-093604-task-auto-backlog-groomer] (#12 kai-linux/agent-os)
Implemented `orchestrator/backlog_groomer.py` and `bin/run_backlog_groomer.sh`. The groomer reads each repo's open issues (via `gh issue list`), last 30 days of `agent_stats.jsonl` completions, CODEBASE.md Known Issues section, and risk flags from `.agent_result.md` files in worktrees. It identifies stale issues (>30 days no activity), Known Issues without linked GitHub issues, and risk flags from recent completions. All data is sent to Claude Haiku with a deterministic prompt to generate 3-5 targeted improvement tasks. Semantic dedup via `difflib.SequenceMatcher` (0.75 threshold) prevents duplicate issues. A system cron fires every Saturday at 20:00.

**Files:** `- orchestrator/backlog_groomer.py`, `- bin/run_backlog_groomer.sh`

**Decisions:**
  - - Reused load_recent_metrics() from agent_scorer.py with 30-day window for completions
  - - Semantic dedup uses difflib.SequenceMatcher (0.75 threshold) rather than exact title match, preventing near-duplicate issues
  - - Deterministic Haiku prompt requests exact JSON schema with ## Goal / ## Success Criteria / ## Constraints body format for dispatcher compatibility
  - - Risk flags scanned from .agent_result.md files in worktrees directory (filtered by 30-day mtime)
  - - CODEBASE.md Known Issues parsed via regex between ## Known Issues heading and next ## heading


### 2026-03-18 — [task-20260318-082904-task-weekly-log-analyzer-auto-creates-improvement-] (#10 kai-linux/agent-os)
Implemented `orchestrator/log_analyzer.py` and `bin/run_log_analyzer.sh`. The analyzer reads the last 7 days of `runtime/metrics/agent_stats.jsonl` and `runtime/logs/queue-summary.log`, calls Claude Haiku with a deterministic structured prompt to identify the top 3 failure patterns/bottlenecks, deduplicates against existing open GitHub issues by exact title match, creates one issue per problem using the standard `## Goal / ## Success Criteria / ## Constraints` body format, and posts a summary to Telegram. A system cron fires every Monday at 07:00.

**Files:** `- orchestrator/log_analyzer.py`, `- bin/run_log_analyzer.sh`, `- CODEBASE.md`

**Decisions:**
  - - Reused load_recent_metrics() from agent_scorer.py to avoid duplicating JSONL parsing
  - - Prompt is fully deterministic (requests exact JSON schema, no open-ended fields) to avoid noisy/random issue titles across runs
  - - Deduplication uses gh issue list --search + exact title match to prevent duplicate improvement tasks
  - - Issue body uses standard ## Goal / ## Success Criteria / ## Constraints sections for dispatcher compatibility
  - - Added to system crontab (persistent) rather than session-only CronCreate
Added `orchestrator/log_analyzer.py` — a weekly analyzer that reads the last 7 days of `runtime/metrics/agent_stats.jsonl` and `runtime/logs/queue-summary.log`, sends both to Claude Haiku with a deterministic prompt to identify the top 3 failure patterns / bottlenecks, deduplicates against open GitHub issues, creates one issue per problem (body follows `## Goal / ## Success Criteria / ## Constraints` template), and posts a summary to Telegram. `bin/run_log_analyzer.sh` is the entry point; a system cron fires every Monday at 07:00.

**Files:** `- orchestrator/log_analyzer.py`, `- bin/run_log_analyzer.sh`

**Decisions:**
  - - Reuses `load_recent_metrics()` from `agent_scorer.py` to avoid duplicating the JSONL parsing logic
  - - Prompt is deterministic (asks for exact JSON schema, no open-ended options) to reduce noise in repeated runs
  - - Deduplication uses `gh issue list --search <title>` and exact-title match; avoids creating duplicate improvement tasks
  - - Issue body uses the standard `## Goal / ## Success Criteria / ## Constraints` sections so the dispatcher can route them normally
  - - Cron added to system crontab (`0 7 * * 1`) rather than relying on session-only CronCreate



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

