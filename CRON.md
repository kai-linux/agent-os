# Cron Setup

Add these to your crontab (`crontab -e`). Replace `/path/to/agent-os` with your actual install path.

Make sure `PATH` includes the directories for `gh`, `codex`, `claude`, `gemini`, and `node`.

```cron
# ── Agent OS core loop ──────────────────────────────────────────────────
# Ensure agent CLIs are on PATH (adjust to your node/bin location)
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# Auto-pull latest orchestrator code
* * * * * /path/to/agent-os/bin/run_autopull.sh >> /path/to/agent-os/logs/autopull.log 2>&1

# Dispatch ready issues from GitHub Project → mailbox
* * * * * /path/to/agent-os/bin/run_dispatcher.sh >> /path/to/agent-os/runtime/logs/dispatcher.log 2>&1

# Execute queued tasks (supervisor manages parallel workers)
* * * * * /path/to/agent-os/bin/run_queue.sh >> /path/to/agent-os/runtime/logs/cron.log 2>&1

# ── PR auto-merge ───────────────────────────────────────────────────────
# Poll CI status on agent PRs, merge on green, rebase on conflict
*/5 * * * * /path/to/agent-os/bin/run_pr_monitor.sh >> /path/to/agent-os/runtime/logs/pr_monitor.log 2>&1

# ── Weekly self-improvement ─────────────────────────────────────────────
# Score per-agent success rates, flag underperformers (Monday 06:30)
# MUST run before log_analyzer so findings are ready to consume
30 6 * * 1 /path/to/agent-os/bin/run_agent_scorer.sh >> /path/to/agent-os/runtime/logs/agent_scorer.log 2>&1

# Analyze failure patterns + scorer findings, file improvement issues (Monday 07:00)
0 7 * * 1 /path/to/agent-os/bin/run_log_analyzer.sh >> /path/to/agent-os/runtime/logs/log_analyzer.log 2>&1

# Daily digest to Telegram (every day at 08:00)
0 8 * * * /path/to/agent-os/bin/run_daily_digest.sh >> /path/to/agent-os/runtime/logs/daily_digest.log 2>&1

# Groom backlog: safe to run frequently; per-repo cadence comes from config.
# Important: cron frequency is the upper bound. If you want ~15 min cadence,
# cron must also run at least every 15 min. The bin/ scripts bootstrap PATH
# for common local CLI installs, so cron entries do not need provider-specific
# PATH overrides in normal setups.
0 * * * * /path/to/agent-os/bin/run_backlog_groomer.sh >> /path/to/agent-os/runtime/logs/backlog_groomer.log 2>&1

# Strategic planner: safe to run frequently; per-repo cadence comes from config.
# Important: cron frequency is the upper bound. If you want ~15 min cadence,
# cron must also run at least every 15 min.
0 * * * * /path/to/agent-os/bin/run_strategic_planner.sh >> /path/to/agent-os/runtime/logs/strategic_planner.log 2>&1
```

Each wrapper emits a timestamp banner like `[2026-03-30T12:34:56+0200] queue start` to stderr before running, so the existing `>> ... 2>&1` redirection captures one timestamped entry per cron invocation.

## What each job does

| Schedule | Script | Role |
|---|---|---|
| `* * * * *` | `run_dispatcher.sh` | Picks up Ready issues, formats them, writes to mailbox |
| `* * * * *` | `run_queue.sh` | Executes tasks in isolated worktrees, manages agent fallback |
| `*/5 * * * *` | `run_pr_monitor.sh` | CI gate + auto-merge + auto-rebase for agent PRs |
| `30 6 * * 1` | `run_agent_scorer.sh` | Computes agent success rates, flags degradation |
| `0 7 * * 1` | `run_log_analyzer.sh` | Reads failure logs + scorer findings, files fix tickets via Claude Haiku |
| `0 8 * * *` | `run_daily_digest.sh` | Summarizes the last 24h of completions, blockers, escalations, agent success, and PR activity to Telegram |
| `0 * * * *` | `run_backlog_groomer.sh` | Per-repo cadence gate in config decides when each repo is groomed |
| `0 * * * *` | `run_strategic_planner.sh` | Per-repo cadence gate in config decides when each repo is planned |
