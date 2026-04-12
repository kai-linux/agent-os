# Deployment Guide — Agent OS for Solo Builders

Deploy Agent OS to manage your own GitHub repositories. This guide takes you
from zero to your first autonomously completed task.

**Time estimate:** ~15 minutes for minimal setup, ~30 minutes for full production.

---

## Prerequisites

You need these installed and authenticated before starting:

| Tool | Version | Check |
|---|---|---|
| Git | any recent | `git --version` |
| GitHub CLI | 2.x+ | `gh --version` |
| Python 3 | 3.10+ | `python3 --version` |
| Claude CLI | latest | `claude --version` |

**GitHub CLI must be authenticated** with the `project` scope:

```bash
gh auth login
gh auth refresh -s project    # required for GitHub Projects integration
```

**Optional agents** (extend the fallback chain):
- [Codex CLI](https://github.com/openai/codex) — `codex --version`
- Gemini CLI — `gemini --version`
- DeepSeek via OpenRouter — requires `openRouterApiKey` in DeepSeek config

You only need Claude to get started. Additional agents become fallbacks if
Claude fails on a task.

---

## 1. Clone and Install

```bash
git clone https://github.com/kai-linux/agent-os
cd agent-os
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Verify the test suite passes in your environment:

```bash
pytest -q
```

---

## 2. Create Your Configuration

```bash
cp example.config.yaml config.yaml
```

Edit `config.yaml` with your details. Here is a minimal configuration for a
single Python repository:

### Python Project

```yaml
root_dir: "~/agent-os"
mailbox_dir: "~/agent-os/runtime/mailbox"
logs_dir: "~/agent-os/runtime/logs"
worktrees_dir: "/srv/worktrees"          # isolated execution area
objectives_dir: "~/agent-os/objectives"

allowed_repos:
  - /home/you/my-python-app

default_agent: auto
default_allow_push: true
default_max_attempts: 4
automation_mode: dispatcher_only         # start simple, upgrade to full later

github_owner: "your-github-username"
github_project_number: 1                 # your GitHub Project board number
github_repos:
  my-python-app: "your-github-username/my-python-app"

github_projects:
  my-project:
    repos:
      - github_repo: "your-github-username/my-python-app"
        path: "/home/you/my-python-app"
        automation_mode: dispatcher_only

repo_configs:
  /home/you/my-python-app:
    test_command: "pytest -q"

agent_fallbacks:
  implementation: [claude, codex]
  debugging: [claude, codex]
  docs: [claude]
```

### Node.js Project

Same structure — adjust `test_command` and paths:

```yaml
allowed_repos:
  - /home/you/my-node-app

github_repos:
  my-node-app: "your-github-username/my-node-app"

github_projects:
  my-project:
    repos:
      - github_repo: "your-github-username/my-node-app"
        path: "/home/you/my-node-app"
        automation_mode: dispatcher_only

repo_configs:
  /home/you/my-node-app:
    test_command: "npm test"
```

### Monorepo

For a monorepo with multiple services, register it as one repo but use a
broader test command:

```yaml
allowed_repos:
  - /home/you/my-monorepo

github_repos:
  my-monorepo: "your-github-username/my-monorepo"

github_projects:
  my-project:
    repos:
      - github_repo: "your-github-username/my-monorepo"
        path: "/home/you/my-monorepo"
        automation_mode: dispatcher_only

repo_configs:
  /home/you/my-monorepo:
    test_command: "make test"            # or: cd packages/api && npm test && cd ../web && npm test
```

Agents work in isolated worktrees of the entire repo, so monorepo tasks can
touch any directory. Use the issue body to scope work to specific packages.

---

## 3. Set Up Your GitHub Project Board

Agent OS uses GitHub Projects v2 as its control plane.

1. **Create a project** at `https://github.com/users/YOUR_USERNAME/projects`
2. **Add a Status field** (single select) with these values:
   - `Ready` — dispatcher picks these up
   - `In Progress` — task is executing
   - `Blocked` — task hit a wall
   - `Done` — completed and merged
3. **Link your repository** to the project
4. Note the **project number** from the URL (e.g., `https://github.com/users/you/projects/1` → number is `1`)
5. Set `github_project_number: 1` in your `config.yaml`

---

## 4. Create the Worktrees Directory

Each task runs in an isolated git worktree so agents never collide:

```bash
sudo mkdir -p /srv/worktrees
sudo chown $(whoami) /srv/worktrees
```

Match this path to `worktrees_dir` in your config.

---

## 5. Create Runtime Directories

```bash
mkdir -p ~/agent-os/runtime/{mailbox/inbox,mailbox/done,mailbox/blocked,logs,metrics,prompts,unblock_notes}
```

---

## 6. Dispatch Your First Task

### Create a GitHub Issue

Create an issue on your managed repository using the standard template:

```bash
gh issue create --repo your-github-username/my-python-app \
  --title "Add health check endpoint" \
  --body "$(cat <<'EOF'
## Goal
Add a /health endpoint that returns 200 OK with a JSON body containing
the service version and uptime.

## Success Criteria
- GET /health returns 200 with {"status": "ok", "version": "..."}
- Endpoint is tested
- No authentication required

## Task Type
implementation

## Agent Preference
auto

## Constraints
- Prefer minimal diffs
- Do not modify existing endpoints
EOF
)"
```

### Add the Issue to Your Project

```bash
# Find the issue number from the output above, then:
gh project item-add 1 --owner your-github-username --url https://github.com/your-github-username/my-python-app/issues/1
```

### Set Status to Ready

Use the GitHub web UI to move the issue to **Ready** status, or use the API:

```bash
# Get the project item ID and field/value IDs, then update:
gh api graphql -f query='
  mutation {
    updateProjectV2ItemFieldValue(
      input: {
        projectId: "PROJECT_NODE_ID"
        itemId: "ITEM_NODE_ID"
        fieldId: "STATUS_FIELD_ID"
        value: { singleSelectOptionId: "READY_OPTION_ID" }
      }
    ) { projectV2Item { id } }
  }'
```

The web UI is easier for the first time. Drag the issue to the **Ready** column.

### Run the Dispatcher Manually

For your first task, run the dispatcher directly instead of waiting for cron:

```bash
source .venv/bin/activate
python3 -m orchestrator.github_dispatcher
```

You should see the issue picked up, formatted, and written to
`runtime/mailbox/inbox/`.

### Run the Queue

```bash
python3 -m orchestrator.queue
```

This creates a worktree, dispatches the agent (Claude by default), and waits
for the result. When done, the agent pushes a branch and opens a PR.

### Check the Result

```bash
# See the PR
gh pr list --repo your-github-username/my-python-app

# Check CI status
gh pr checks <PR_NUMBER> --repo your-github-username/my-python-app
```

### Merge (Manual or Automatic)

For your first task, review the PR manually. Once you trust the flow, the
PR monitor handles merging automatically:

```bash
python3 -m orchestrator.pr_monitor
```

---

## 7. Set Up Cron (Production)

Once you've verified the flow works manually, install the cron jobs for
continuous autonomous operation. See [CRON.md](../CRON.md) for the full
reference.

**Minimal cron** — just the core loop (dispatcher + queue + PR monitor):

```cron
# Agent OS — minimal production loop
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

* * * * * /home/you/agent-os/bin/run_dispatcher.sh  >> /home/you/agent-os/runtime/logs/dispatcher.log 2>&1
* * * * * /home/you/agent-os/bin/run_queue.sh       >> /home/you/agent-os/runtime/logs/cron.log 2>&1
*/5 * * * * /home/you/agent-os/bin/run_pr_monitor.sh >> /home/you/agent-os/runtime/logs/pr_monitor.log 2>&1
```

**Full cron** — adds self-improvement, planning, and observability:

```cron
# Agent OS — full autonomous loop
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# Core loop
* * * * * /home/you/agent-os/bin/run_autopull.sh     >> /home/you/agent-os/runtime/logs/autopull.log 2>&1
* * * * * /home/you/agent-os/bin/run_dispatcher.sh   >> /home/you/agent-os/runtime/logs/dispatcher.log 2>&1
* * * * * /home/you/agent-os/bin/run_queue.sh        >> /home/you/agent-os/runtime/logs/cron.log 2>&1
*/5 * * * * /home/you/agent-os/bin/run_pr_monitor.sh >> /home/you/agent-os/runtime/logs/pr_monitor.log 2>&1

# Planning and grooming (hourly — internal cadence gates handle per-repo timing)
0 * * * * /home/you/agent-os/bin/run_strategic_planner.sh  >> /home/you/agent-os/runtime/logs/strategic_planner.log 2>&1
0 * * * * /home/you/agent-os/bin/run_backlog_groomer.sh    >> /home/you/agent-os/runtime/logs/backlog_groomer.log 2>&1

# Weekly self-improvement (Monday)
30 6 * * 1 /home/you/agent-os/bin/run_agent_scorer.sh  >> /home/you/agent-os/runtime/logs/agent_scorer.log 2>&1
0  7 * * 1 /home/you/agent-os/bin/run_log_analyzer.sh  >> /home/you/agent-os/runtime/logs/log_analyzer.log 2>&1

# Daily digest (optional — requires Telegram)
0 8 * * * /home/you/agent-os/bin/run_daily_digest.sh >> /home/you/agent-os/runtime/logs/daily_digest.log 2>&1
```

To use the full cron setup, switch `automation_mode` to `full` in your config.

---

## 8. Upgrade to Full Automation

Once you trust the system with a few manually dispatched tasks:

1. **Switch to full mode** in `config.yaml`:
   ```yaml
   automation_mode: full
   ```

2. **Set sprint cadence**:
   ```yaml
   plan_size: 5
   sprint_cadence_days: 7
   ```

3. **Add a STRATEGY.md** to your managed repo describing priorities

4. **Add objectives** (optional) — create `objectives/my-app.yaml`:
   ```yaml
   repo: your-github-username/my-python-app
   objective: "Ship reliable features and maintain test coverage"
   metrics:
     - id: task_success_rate
       weight: 0.5
       direction: increase
       evidence_source: "file://~/agent-os/runtime/metrics/agent_stats.jsonl"
     - id: test_coverage
       weight: 0.5
       direction: increase
       evidence_source: "file:///home/you/my-python-app/coverage.json"
   ```

The strategic planner will now compose sprint plans, the backlog groomer
will generate improvement issues, and the log analyzer will file fix tickets
from failure patterns — all feeding back into the same autonomous loop.

---

## Telegram Notifications (Optional)

Get real-time task status, escalations, and daily digests on Telegram.

1. Message `@BotFather` on Telegram → `/newbot` → copy the token
2. Send any message to your bot, then get your chat ID:
   ```bash
   curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | python3 -c "import sys,json; print(json.load(sys.stdin)['result'][0]['message']['chat']['id'])"
   ```
3. Add to `config.yaml`:
   ```yaml
   telegram_bot_token: "YOUR_TOKEN"
   telegram_chat_id: "YOUR_CHAT_ID"
   ```

---

## Troubleshooting

### Dispatcher doesn't pick up issues

- **Check the project number** — `github_project_number` in config must match
  your GitHub Project
- **Check the status value** — issue must be in `Ready` status (exact match)
- **Check GitHub auth** — run `gh auth status` and verify the `project` scope
- **Check logs** — `tail runtime/logs/dispatcher.log`

### Agent fails with authentication error

- **Claude** — run `claude --version` and verify it responds
- **Codex** — ensure `OPENAI_API_KEY` is set in the environment
- **DeepSeek** — needs `openRouterApiKey` in its config directory
- **Gemini** — ensure `GOOGLE_API_KEY` is set

Agents that fail auth are automatically skipped in the fallback chain.

### PR is not auto-merged

- **CI must pass** — check `gh pr checks <number>`
- **PR title must start with `Agent:`** — the PR monitor filters on this prefix
- **Merge conflicts** — monitor will attempt auto-rebase; check for
  `CONFLICTING` status with `gh pr view <number> --json mergeStateStatus`
- **Max retries** — after 3 failed merge attempts, the PR is escalated

### Queue shows task as blocked

- **Read the blocker** — `cat runtime/mailbox/blocked/<task>.md` and check
  the `BLOCKER_CODE` field
- **Common codes**:
  - `missing_context` — issue body lacks enough detail for the agent
  - `test_failure` — agent code failed tests; follow-up will retry
  - `environment_failure` — worktree or tool issue
- **Retry** — move the file back to `inbox/` to requeue:
  ```bash
  mv runtime/mailbox/blocked/<task>.md runtime/mailbox/inbox/
  ```

### Worktree errors

- **Permission denied** — ensure `worktrees_dir` is writable: `ls -la /srv/worktrees`
- **Disk full** — worktrees accumulate; clean old ones:
  ```bash
  ls /srv/worktrees/my-repo/
  # Remove completed task worktrees (keep active ones)
  ```
- **Git lock files** — if a task was interrupted:
  ```bash
  find /srv/worktrees -name "*.lock" -delete
  ```

### Cron jobs aren't running

- **Check crontab** — `crontab -l` should show your entries
- **Check PATH** — `bin/common_env.sh` bootstraps common paths, but verify
  `which claude` works from a non-interactive shell
- **Check logs** — each job writes to `runtime/logs/<job>.log`
- **Test manually** — run the script directly:
  ```bash
  bash bin/run_dispatcher.sh
  ```

### Tests fail in the managed repo

- **Verify test command** — the `test_command` in `repo_configs` runs in the
  worktree root, not your project root. Use absolute paths or `cd` if needed:
  ```yaml
  repo_configs:
    /home/you/my-app:
      test_command: "cd src && pytest -q"
  ```

---

## What's Next

- [Architecture deep dive](architecture.md) — system roles, safety mechanisms, observability
- [Execution flow](execution.md) — task dispatch, handoff contract, retry logic
- [Configuration reference](configuration.md) — objectives, evidence, planning research
- [CRON.md](../CRON.md) — complete cron job reference with schedule table
- [Case study](case-study-agent-os.md) — Agent OS managing its own development
