# Cron Setup

Add these to your crontab (`crontab -e`). Replace `/path/to/agent-os` with your actual install path.

Make sure `PATH` includes the directories for `gh`, `codex`, `claude`, `gemini`, and `node`.

```cron
# ── Agent OS core loop ──────────────────────────────────────────────────
# Ensure agent CLIs are on PATH (adjust to your node/bin location)
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# Auto-pull latest orchestrator code
* * * * * cd /path/to/agent-os && git pull > /path/to/agent-os/logs/autopull.log 2>&1

# Dispatch ready issues from GitHub Project → mailbox
* * * * * /path/to/agent-os/bin/run_dispatcher.sh >> /path/to/agent-os/runtime/logs/dispatcher.log 2>&1

# Execute queued tasks (supervisor manages parallel workers)
* * * * * /path/to/agent-os/bin/run_queue.sh >> /path/to/agent-os/runtime/logs/cron.log 2>&1

# ── PR auto-merge ───────────────────────────────────────────────────────
# Poll CI status on agent PRs, merge on green, rebase on conflict
*/5 * * * * /path/to/agent-os/bin/run_pr_monitor.sh >> /path/to/agent-os/runtime/logs/pr_monitor.log 2>&1

# ── Weekly self-improvement ─────────────────────────────────────────────
# Analyze failure patterns, file improvement issues (Monday 07:00)
0 7 * * 1 /path/to/agent-os/bin/run_log_analyzer.sh >> /path/to/agent-os/runtime/logs/log_analyzer.log 2>&1

# Score per-agent success rates, flag underperformers (Monday 07:00)
0 7 * * 1 /path/to/agent-os/bin/run_agent_scorer.sh >> /path/to/agent-os/runtime/logs/agent_scorer.log 2>&1

# Groom backlog: prune stale issues, generate improvement tasks (Saturday 20:00)
0 20 * * 6 /path/to/agent-os/bin/run_backlog_groomer.sh >> /path/to/agent-os/runtime/logs/backlog_groomer.log 2>&1

# Strategic planner: generate sprint plan, await Telegram approval, dispatch (Sunday 20:00)
0 20 * * 0 /path/to/agent-os/bin/run_strategic_planner.sh >> /path/to/agent-os/runtime/logs/strategic_planner.log 2>&1
```

## What each job does

| Schedule | Script | Role |
|---|---|---|
| `* * * * *` | `run_dispatcher.sh` | Picks up Ready issues, formats them, writes to mailbox |
| `* * * * *` | `run_queue.sh` | Executes tasks in isolated worktrees, manages agent fallback |
| `*/5 * * * *` | `run_pr_monitor.sh` | CI gate + auto-merge + auto-rebase for agent PRs |
| `0 7 * * 1` | `run_log_analyzer.sh` | Reads failure logs, files fix tickets via Claude Haiku |
| `0 7 * * 1` | `run_agent_scorer.sh` | Computes agent success rates, flags degradation |
| `0 20 * * 6` | `run_backlog_groomer.sh` | Surfaces stale issues, generates improvement tasks (→ Backlog) |
| `0 20 * * 0` | `run_strategic_planner.sh` | Sprint planning, Telegram approval, auto-dispatch (→ Ready) |
