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

# Deploy watchdog: inspect recent merges against production telemetry and queue revert PRs for operator approval
*/10 * * * * /path/to/agent-os/bin/run_deploy_watchdog.sh >> /path/to/agent-os/runtime/logs/deploy_watchdog.log 2>&1

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

# ── Adoption monitoring ────────────────────────────────────────────────
# Weekly adoption funnel report with traffic/conversion analysis (Monday 07:30)
30 7 * * 1 /path/to/agent-os/bin/run_adoption_report.sh >> /path/to/agent-os/runtime/logs/adoption_report.log 2>&1

# ── Evidence and product inspection ────────────────────────────────────
# Export GitHub stars/forks evidence every 6 hours (one invocation per managed repo)
0 */6 * * * /path/to/agent-os/bin/export_github_evidence.sh owner/repo >> /path/to/agent-os/runtime/logs/evidence_export.log 2>&1

# Export the public reliability dashboard snapshot daily from runtime metrics
15 6 * * * /path/to/agent-os/bin/run_public_dashboard.sh >> /path/to/agent-os/runtime/logs/public_dashboard.log 2>&1

# Live product inspection: fetch configured public surfaces and refresh PRODUCT_INSPECTION.md (daily 06:00)
0 6 * * * /path/to/agent-os/bin/run_product_inspector.sh >> /path/to/agent-os/runtime/logs/product_inspector.log 2>&1
```

Each wrapper emits a timestamp banner like `[2026-03-30T12:34:56+0200] queue start` to stderr before running, so the existing `>> ... 2>&1` redirection captures one timestamped entry per cron invocation.

## What each job does

| Schedule | Script | Role |
|---|---|---|
| `* * * * *` | `run_autopull.sh` | Fast-forwards the orchestrator checkout so cron always runs the latest code |
| `* * * * *` | `run_dispatcher.sh` | Picks up Ready issues, formats them, writes to mailbox |
| `* * * * *` | `run_queue.sh` | Executes tasks in isolated worktrees, manages agent fallback |
| `*/5 * * * *` | `run_pr_monitor.sh` | CI gate + auto-merge + auto-rebase for agent PRs |
| `*/10 * * * *` | `run_deploy_watchdog.sh` | Watches recent merges against external telemetry and opens operator-gated revert PRs on production regression |
| `30 6 * * 1` | `run_agent_scorer.sh` | Computes agent success rates, flags degradation |
| `0 7 * * 1` | `run_log_analyzer.sh` | Reads failure logs + scorer findings, files fix tickets via Claude Haiku |
| `0 8 * * *` | `run_daily_digest.sh` | Summarizes the last 24h of completions, blockers, escalations, agent success, and PR activity to Telegram |
| `0 * * * *` | `run_backlog_groomer.sh` | Per-repo cadence gate in config decides when each repo is groomed |
| `0 * * * *` | `run_strategic_planner.sh` | Per-repo cadence gate in config decides when each repo is planned |
| `30 7 * * 1` | `run_adoption_report.sh` | Generates weekly adoption funnel report with traffic, referral, and conversion analysis; sends Telegram summary |
| `0 */6 * * *` | `export_github_evidence.sh` | Snapshots GitHub stars/forks/traffic for the tracked objective metrics (one invocation per managed repo) |
| `15 6 * * *` | `run_public_dashboard.sh` | Writes the public reliability dashboard snapshot from `PRODUCTION_FEEDBACK.md` and `runtime/metrics/agent_stats.jsonl` |
| `0 6 * * *` | `run_product_inspector.sh` | Fetches configured public product surfaces and refreshes `PRODUCT_INSPECTION.md` for the planner/groomer |
