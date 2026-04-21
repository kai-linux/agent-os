# Specification: `agentos init` — Guided Bootstrap From Empty

**Status:** Specification for implementation.
**Audience:** The agent / engineer who will pick this up and finish it end-to-end.
**Author context:** Written by an assistant after exploring the repo and a short design dialogue with the operator. This doc is the single source of truth for the feature — do not re-derive design decisions from scratch.

---

## 1. Goal

Add a single command, `agentos init`, that walks a new user through every prerequisite to take Agent OS from "freshly cloned orchestrator + a product idea" to "fully autonomous loop running on cron, Telegram control plane live, first PR on its way." No manual YAML editing, no GitHub Projects web UI clicking, no cron math, no issue templating.

**Definition of done for the user:** after running `agentos init` once and answering the prompts, within ~2 minutes cron ticks execute, a dispatcher picks up a seeded "Ready" issue from the new project board, claude opens a PR on the new repo, and a Telegram message arrives confirming the loop is live.

## 2. Why this exists

Today, Agent OS is an *orchestration layer for an already-existing repo with an already-configured GitHub Project*. The `example.config.yaml` is 360+ lines; `CRON.md` assumes the user knows crontab; Telegram setup is undocumented beyond "paste token here." A solo builder with a product idea bounces before the loop ever runs. `agentos init` closes that gap.

## 3. Hard prerequisites (block on missing)

These are **required** — the script must verify and halt with an actionable install hint if any are absent:

- `gh` CLI, authenticated (`gh auth status`), with `project` scope (`gh auth refresh -s project`)
- `python3` (3.10+) with the repo's venv usable (`.venv/bin/python3` if present; else prompt to create)
- `claude` CLI on PATH
- `crontab` available (macOS and Linux ship it; confirm `command -v crontab`)
- Git configured with `user.name` and `user.email` (required for commits to the new repo)

**Do not make any of these optional.** The operator confirmed: "crontab is not optional," "telegram is the only control plane, so it is required," "gh is obviously another requirement."

## 4. Non-goals for v1

- Do not support non-GitHub code hosts.
- Do not support non-Telegram control planes.
- Do not generate a full multi-repo `github_projects` config; init handles exactly one repo, one project.
- Do not invent new fallback-agent chains; use the default `[claude]`-only fallback (user can add codex/gemini/deepseek later).
- Do not merge into an existing multi-repo `config.yaml` — if one exists at the target path, back it up to `config.yaml.bak.<ts>` and write a fresh single-repo config, warning the user.
- Do not install the `gh`, `claude`, `python3`, or `crontab` binaries automatically — only detect and instruct.

## 5. User journey (terminal transcript mock-up)

This is the canonical experience. Code should match this closely. The `▸` markers are `info()` lines; `✓` are `ok()`; `!` are `warn()`; `✗` are `fail()` (same palette as `demo.sh`).

```
$ agentos init

═══ Agent OS — guided setup ═══

▸ Checking prerequisites...
  ✓ gh (authenticated as kai-linux, project scope OK)
  ✓ python3 3.11.7
  ✓ claude CLI found
  ✓ crontab available
  ✓ git identity set (kai-linux <kai@...>)

═══ Step 1/7 — What are you building? ═══

What do you want to build?
> A habit tracker that asks me one question per day and plots streaks.

What kind of thing is it?
  [1] web app      [2] mobile app   [3] game
  [4] API          [5] CLI          [6] desktop app
  [7] other
> 1

Any stack preference? ("auto" lets the agent decide)
> auto

Who is the user, and what does success look like in one sentence?
> Me personally. Success = I use it every day for 30 days without it crashing.

═══ Step 2/7 — GitHub repo ═══

  [1] Create a new GitHub repo  [2] Use an existing empty repo I already created
> 1

Repo name (on github.com/kai-linux/...): habit-tracker
Visibility: [public/private] > private
Clone to: [~/projects/habit-tracker]
> 

  ✓ Created github.com/kai-linux/habit-tracker
  ✓ Cloned to /Users/kai/projects/habit-tracker
  ✓ Created GitHub Project #14 "habit-tracker"
  ✓ Added Status field with options: Ready, In Progress, Blocked, Done
  ✓ Linked project to repo
  ✓ Created "ready" label

═══ Step 3/7 — Designing the charter ═══

▸ Asking claude to propose a stack and draft the first 5 issues...
  (this takes 30-90 seconds)

Proposed plan:
  Stack: Python (Quart) + SQLite + HTMX + Tailwind CDN
  Rationale: solo-operator stack, zero build step, deploys to any VPS.
  Seed issues:
    #1  Scaffold Quart app skeleton with /health endpoint
    #2  Add SQLite schema + daily-question model
    #3  Build question-of-the-day page (HTMX)
    #4  Build streak visualization page
    #5  Deploy config: Dockerfile + fly.toml

  [1] Looks good, proceed  [2] Regenerate  [3] Edit manually  [4] Abort
> 1

  ✓ Wrote NORTH_STAR.md, committed, pushed
  ✓ Created issue #1 on GitHub, added to project, status=Ready
  ✓ Created issue #2 ... status=Ready
  ✓ Created issue #3 ... status=Ready
  ✓ Created issue #4 ... status=Ready
  ✓ Created issue #5 ... status=Ready

═══ Step 4/7 — Telegram control plane ═══

Agent OS uses Telegram for the daily digest, escalations, and the /on /off /status commands. Let's set it up.

  1. Open Telegram, message @BotFather, send /newbot, pick a name.
  2. Paste the token below.

Bot token: > 1234567890:AA...redacted

▸ Verifying token...
  ✓ Token valid: @kai_habittracker_bot

  3. Now open a chat with @kai_habittracker_bot and send /start (or any message).

Press Enter when you've sent the message. > 

▸ Polling getUpdates...
  ✓ Found chat: Kai Libicher (id=987654321)

▸ Sending test message...
  ✓ Test message delivered.

═══ Step 5/7 — Writing config.yaml ═══

  ✓ Wrote /Users/kai/agent-os/config.yaml
  ✓ Wrote /Users/kai/agent-os/runtime/init_state/habit-tracker.json

═══ Step 6/7 — Installing cron ═══

  ✓ Read existing crontab (12 lines, no Agent OS entries found)
  ✓ Appended Agent OS block (11 jobs)
  ✓ Installed updated crontab

▸ Waiting up to 90s for first dispatcher tick...
  ✓ dispatcher.log updated at 14:32:05 — loop is live.

═══ Step 7/7 — Done ═══

  Repo:     https://github.com/kai-linux/habit-tracker
  Project:  https://github.com/users/kai-linux/projects/14
  Logs:     ~/agent-os/runtime/logs/
  Telegram: @kai_habittracker_bot — commands: /on /off /status /jobs /repos /help

  Expected first PR:     ~3-5 minutes from now
  Expected Telegram ping: within 1 minute (dispatcher announce)

  To pause the whole system:  bin/agentos off
  To re-run init for another repo:  bin/agentos init
```

## 6. Implementation layout

### 6.1 New files

```
orchestrator/init/
├── __init__.py
├── __main__.py          # python3 -m orchestrator.init
├── state.py             # read/write runtime/init_state/<slug>.json
├── preflight.py         # Phase 0: checks
├── dialogue.py          # Phase 1: product intake
├── github_scaffold.py   # Phase 2: repo + project + label
├── charter.py           # Phase 3: claude call, NORTH_STAR, seed issues
├── telegram_pair.py     # Phase 4: bot token + chat_id pairing
├── config_emit.py       # Phase 5: write config.yaml
├── cron_install.py      # Phase 6: merge crontab
└── ui.py                # shared colored-output helpers (info/ok/warn/fail/header)
```

**Why a package, not a single file:** each phase has distinct external dependencies (gh / claude / telegram api / crontab) and is independently testable. A single 800-line file will be un-fun to maintain.

### 6.2 Modified files

- `bin/agentos` — add `init` subcommand. The bash wrapper does minimal work: cd to repo root, ensure venv, exec `python3 -m orchestrator.init`. Keep `on|off|status` behavior unchanged.
- `README.md` — add **Option C: bootstrap from scratch** under "Get Started" pointing at `agentos init`. Keep Options A (demo) and B (production) intact.
- `docs/configuration.md` — add a short section near the top: "If you haven't set up Agent OS yet, run `agentos init` — it configures everything documented below."

### 6.3 Test files

```
tests/test_init_state.py        # state file read/write + idempotency markers
tests/test_init_config_emit.py  # generated config.yaml validates against config loader
tests/test_init_cron_install.py # crontab merge: no dupes, preserves user lines, idempotent
tests/test_init_charter.py      # mock claude subprocess, test JSON parsing + code-fence stripping
```

Do not test the interactive dialogue, gh calls, or Telegram API directly — those are integration points; mock at the subprocess boundary only where it's fast to do so.

## 7. Phase-by-phase behavior

Every phase must be **idempotent and re-entrant**. If the user ctrl-c's partway through, re-running `agentos init` must pick up at the first incomplete phase. The state file (§8) is how phases know whether to skip.

**Universal pattern for every phase:**

1. Read state file. If this phase's marker is set, print `✓ already done (<marker>)` and return.
2. Do the work.
3. On success, update state file atomically (write to `*.tmp`, `os.replace`).
4. On failure, print a specific recovery hint and exit non-zero. Do NOT roll back previous phases.

### Phase 0 — Preflight (`preflight.py`)

Check each prerequisite. For each, print ✓ or ✗ with one-line hint:

| Check | Command | On fail, print |
|---|---|---|
| gh installed | `shutil.which("gh")` | `Install: https://cli.github.com` |
| gh authenticated | `gh auth status` | `Run: gh auth login` |
| gh project scope | parse `gh auth status` for `project` scope | `Run: gh auth refresh -s project` |
| python3 >= 3.10 | `sys.version_info` | `Upgrade python3` |
| claude on PATH | `shutil.which("claude")` | `Install: https://docs.anthropic.com/en/docs/claude-code` |
| crontab on PATH | `shutil.which("crontab")` | `crontab is standard on macOS/Linux — install if missing` |
| git identity | `git config user.name && user.email` | `Run: git config --global user.name "You" && git config --global user.email "you@x"` |

If any check fails, print all failures and exit 1. Do not prompt "fix and continue" — user re-runs.

### Phase 1 — Product intake (`dialogue.py`)

Four questions, plain `input()`. Validate:
- Q1 (idea): non-empty, >= 10 chars.
- Q2 (kind): int 1–7. Map to a string label (`web`, `mobile`, `game`, `api`, `cli`, `desktop`, `other`).
- Q3 (stack): free text; accept literal `auto`.
- Q4 (user + success): non-empty, >= 10 chars.

Return a dict:
```python
{"idea": str, "kind": str, "stack_preference": str, "success_criteria": str}
```

Persist the raw answers in state file under key `"intake"` (so regenerations in Phase 3 don't re-ask).

### Phase 2 — GitHub scaffolding (`github_scaffold.py`)

Sub-steps in order:

1. **Choose mode:** new repo vs bind to existing empty. Default: new.
2. **Gather info:**
   - `repo_name` (validate: `^[a-z0-9][a-z0-9-_]*$`, 1–100 chars, lowercase hyphenated)
   - `visibility` (public|private, default private)
   - `local_clone_path` (default `~/projects/<repo_name>`)
   - `github_owner` — derive from `gh api user --jq .login`, confirm with user.
3. **Create repo** (if new mode):
   ```
   gh repo create <owner>/<repo_name> --<visibility> --clone --add-readme=false
   ```
   If "existing empty" mode, verify: `gh api repos/<owner>/<repo_name>` returns a repo, and `gh api repos/<owner>/<repo_name>/commits` returns 409 (empty). Clone if not already cloned locally.
4. **Make initial commit** if repo is empty: write a minimal `.gitignore` (python + node + OS noise) and a one-line `README.md` (`# <repo_name>`), `git add`, `git commit -m "initial commit"`, `git push origin main`. This is required because GitHub Projects can't have "Status=Ready" items without the repo being initialized.
5. **Create project:**
   ```
   gh project create --owner <owner> --title <repo_name> --format json
   ```
   Capture `id` (node id), `number`, `url`.
6. **Check/create Status field:**
   ```
   gh project field-list <number> --owner <owner> --format json
   ```
   If a field named `Status` with `dataType=SINGLE_SELECT` already exists, capture its id and options. If missing:
   ```
   gh project field-create <number> --owner <owner> \
     --name "Status" --data-type SINGLE_SELECT \
     --single-select-options "Ready,In Progress,Blocked,Done" --format json
   ```
   Capture the field id and the 4 option ids by name.
   **Critical:** the option names must exactly match `Ready`, `In Progress`, `Blocked`, `Done` — these are the defaults referenced in `example.config.yaml:268-272` and the dispatcher / planner / pr_monitor all read them.
7. **Link project to repo:**
   ```
   gh project link <number> --owner <owner> --repo <owner>/<repo_name>
   ```
8. **Create `ready` label** on the repo (lowercase; used as a fallback trigger by the dispatcher):
   ```
   gh label create ready --color 0E8A16 --description "Dispatch-ready" \
     --repo <owner>/<repo_name>
   ```
   Idempotent: ignore "already exists" errors.

Persist in state: `repo_full_name`, `local_clone_path`, `project_id`, `project_number`, `project_url`, `status_field_id`, `status_option_ids` (dict by name).

### Phase 3 — Charter + seed issues (`charter.py`)

Build the prompt and call `claude -p` (not `--dangerously-skip-permissions`; this call generates content only, it should not touch the filesystem). Timeout 300s.

**Prompt template** (string, substitute the `{…}` placeholders):

```
You are the architect agent for Agent OS, bootstrapping a new project.

The operator just answered these questions:

IDEA:
{idea}

KIND OF PROJECT:
{kind}  (one of: web, mobile, game, api, cli, desktop, other)

STACK PREFERENCE:
{stack_preference}  (may be "auto")

USER + SUCCESS CRITERIA:
{success_criteria}

Your job: propose a stack and the first 3-5 seed issues that will take this project from empty repo to first deployable vertical slice. Optimize for:
- Solo-operator friendliness (no heavy infra)
- Thin first slice over complete scaffolding (so the stack choice is revisable)
- Issues the existing Agent OS agents (claude/codex/gemini/deepseek) can complete in 10-40 minutes each

Output EXACTLY one JSON object, no code fences, no prose before or after. Schema:

{{
  "stack_decision": "<one-line stack summary>",
  "stack_rationale": "<2-4 sentence rationale>",
  "north_star_md": "<full markdown body for NORTH_STAR.md — include: one-paragraph mission, stack + rationale, out-of-scope list, definition of first vertical slice>",
  "seed_issues": [
    {{
      "title": "<imperative, <= 70 chars>",
      "priority": "prio:high|prio:normal|prio:low",
      "goal": "<one paragraph>",
      "success_criteria": ["<bullet 1>", "<bullet 2>", "..."],
      "constraints": ["<bullet 1>", "..."]
    }}
  ]
}}

Rules:
- First issue MUST be a minimal skeleton that runs (e.g. "hello world" level) for the chosen stack. This is the "revisable stack choice" anchor.
- Each issue must be independently completable — no "depends on issue #2" language.
- success_criteria bullets must be observable (file exists, command returns 0, endpoint returns 200, etc.)
- 3-5 issues total. No more.
```

**Response handling:**
- Read stdout, strip leading/trailing whitespace.
- If response is wrapped in ```json … ``` or ``` … ```, strip fences before `json.loads`.
- If `json.loads` fails, show the raw response and let the user pick: regenerate / abort / edit manually.
- Validate shape: `stack_decision` and `north_star_md` non-empty, `seed_issues` length 3–5, each issue has all fields, `priority` in the allowed set.

**Preview + confirm UI:**
Display the plan in the mock-up's format. Offer 4 options:
1. Looks good, proceed
2. Regenerate (call claude again with an added "The last attempt proposed X; the operator wants something different. Try a different angle." instruction — use 1 retry max before forcing user to pick 3 or 4)
3. Edit manually (open `$EDITOR` with the parsed JSON; re-validate on save)
4. Abort

**On "Looks good":**
- Write `NORTH_STAR.md` into the clone, add + commit (`NORTH_STAR: scaffold charter and seed backlog`), push.
- For each seed issue:
  - Build issue body: `## Goal\n\n<goal>\n\n## Success Criteria\n\n- <bullet>\n- <bullet>\n\n## Constraints\n\n- <bullet>`.
  - `gh issue create --repo <full_name> --title <title> --body <body> --label ready --label <priority>` (create the priority labels first if missing).
  - `gh project item-add <number> --owner <owner> --url <issue_url> --format json` → capture item `id`.
  - `gh project item-edit --id <item_id> --project-id <project_id> --field-id <status_field_id> --single-select-option-id <ready_option_id>` to set Status=Ready.
- Persist `issues_created: [{number, title, item_id}, ...]` in state.

### Phase 4 — Telegram pairing (`telegram_pair.py`)

Explain and walk the user through:

1. Print instructions: "Message @BotFather on Telegram, send `/newbot`, pick any name and username."
2. Prompt for bot token. Validate shape with regex `^\d+:[A-Za-z0-9_-]{30,}$`. Do not log the token beyond masked last-4.
3. Call `GET https://api.telegram.org/bot<TOKEN>/getMe` to verify. Capture bot `username`. On 401, reprompt (up to 3 tries).
4. Print: "Now open a chat with @<username> and send any message. Press Enter when done."
5. After Enter, poll `GET https://api.telegram.org/bot<TOKEN>/getUpdates?timeout=30` in a loop, up to 60s total. Extract unique `result[].message.chat.id` and `result[].message.chat.type` / `first_name` / `title`.
   - If 1 chat: use it.
   - If >1 chat: show list and let user pick.
   - If 0 after 60s: print troubleshooting (common cause: user started bot but didn't send message; or bot privacy settings) and re-poll once.
6. Send a test message:
   ```
   POST https://api.telegram.org/bot<TOKEN>/sendMessage
   {"chat_id": <id>, "text": "✅ Agent OS control plane linked to <repo_full_name>. You can now /on /off /status from this chat."}
   ```
7. Persist: `telegram_bot_token`, `telegram_chat_id`, `telegram_bot_username` in state **and** in memory for Phase 5 — do not re-read from state in Phase 5 in case of privacy rules.

Use `urllib.request` (stdlib) — do not add a `requests` dependency.

### Phase 5 — Config emission (`config_emit.py`)

Build the YAML in Python (dict → `yaml.safe_dump(sort_keys=False)`). Target path: `<agent-os root>/config.yaml`.

**Before writing config:** ensure the repo-local pre-commit hook guard is active so `config.yaml` cannot be committed accidentally. Check:
```
git config --get core.hooksPath
```
If it does not equal `hooks`, run:
```
git config core.hooksPath hooks
```
This is a repo-local setting inside the Agent OS checkout, not a global git config change.

**If a `config.yaml` already exists:** move it to `config.yaml.bak.<YYYYMMDD-HHMMSS>` and warn.

**Required config shape** (fill with state + reasonable defaults):

```yaml
root_dir: "<agent-os root, expanded ~>"
mailbox_dir: "<root>/runtime/mailbox"
logs_dir: "<root>/runtime/logs"
worktrees_dir: "<root>/runtime/worktrees"     # default; let user override via prompt
objectives_dir: "<root>/objectives"
evidence_dir: "~/.local/share/agent-os/evidence"

automation_mode: full
default_agent: claude
default_task_type: implementation
max_runtime_minutes: 40
default_base_branch: main
default_allow_push: true
default_max_attempts: 4
max_parallel_workers: 1
test_timeout_minutes: 5

plan_size: 5
sprint_cadence_days: 7
planner_allow_early_refresh: true
groomer_cadence_days: 3.5
backlog_depth_multiplier: 2

priority_weights:
  prio:high: 30
  prio:normal: 10
  prio:low: 0

allowed_repos:
  - <local_clone_path>

repo_configs:
  <local_clone_path>:
    test_command: "<inferred from stack: pytest / npm test / go test ./... — default: echo no-tests>"

agent_fallbacks:
  implementation: [claude]
  debugging: [claude]
  architecture: [claude]
  research: [claude]
  docs: [claude]

planner_agents: [claude]

agent_timeout_minutes:
  claude: 45

github_owner: "<owner>"
github_project_status_field: "Status"
github_project_ready_value: "Ready"
github_project_in_progress_value: "In Progress"
github_project_blocked_value: "Blocked"
github_project_done_value: "Done"

github_projects:
  <repo_name>:
    project_number: <number>
    repos:
      - github_repo: "<repo_full_name>"
        path: "<local_clone_path>"
        local_repo: "<local_clone_path>"
        automation_mode: full

trusted_authors:
  - "<owner>"

telegram_bot_token: "<token>"
telegram_chat_id: "<chat_id>"

dependency_watcher:
  enabled: true
  cadence_days: 7
  max_actions_per_week: 3
```

**Do not emit** `planning_research`, `product_inspection`, `production_feedback`, `external_signals`, `outcome_attribution`, `quality_harness`, `cost_tracking`, `blocker_regression_alerts`. These are advanced features; leaving them unset uses the code's defaults. Keep v1 config minimal.

**Stack → test_command inference** (best-effort; default to `"echo no-tests-yet"`):
- kind=web + stack mentions `python` / `flask` / `quart` / `django` / `fastapi` → `pytest -q`
- kind=web + stack mentions `node` / `next` / `react` / `vue` / `svelte` → `npm test`
- kind=api + python → `pytest -q`
- kind=cli + python → `pytest -q`
- kind=cli + go → `go test ./...`
- kind=game + unity → `echo 'unity tests not configured yet'`
- otherwise → `echo no-tests-yet`

### Phase 6 — Crontab install (`cron_install.py`)

**Read current crontab:**
```
crontab -l 2>/dev/null
```
(non-zero exit is fine — empty crontab.)

**Detect existing Agent OS block:** scan for the marker line `# ── Agent OS (managed by agentos init) ──`. If present:
- If block is unchanged: print `✓ already installed` and return.
- If block differs: offer diff + "replace / skip / abort".

**Compose the block** (use the exact jobs below; substitute `<ROOT>` with the agent-os root and `<PATH>` with the PATH the current shell sees — including any nvm / brew prefixes, detected via `os.environ["PATH"]`):

```cron
# ── Agent OS (managed by agentos init) — begin ──
PATH=<PATH>

# Auto-pull latest orchestrator code
* * * * * <ROOT>/bin/run_autopull.sh >> <ROOT>/runtime/logs/autopull.log 2>&1

# Core loop: dispatch → execute → merge
* * * * * <ROOT>/bin/run_dispatcher.sh >> <ROOT>/runtime/logs/dispatcher.log 2>&1
* * * * * <ROOT>/bin/run_queue.sh >> <ROOT>/runtime/logs/cron.log 2>&1
*/5 * * * * <ROOT>/bin/run_pr_monitor.sh >> <ROOT>/runtime/logs/pr_monitor.log 2>&1

# Control plane (REQUIRED — this is how /on /off work; runs even when disabled)
* * * * * AGENT_OS_IGNORE_DISABLED=1 <ROOT>/bin/run_telegram_control.sh >> <ROOT>/runtime/logs/telegram_control.log 2>&1

# Self-improvement
30 6 * * 1 <ROOT>/bin/run_agent_scorer.sh >> <ROOT>/runtime/logs/agent_scorer.log 2>&1
0 7 * * 1 <ROOT>/bin/run_log_analyzer.sh >> <ROOT>/runtime/logs/log_analyzer.log 2>&1
0 * * * * <ROOT>/bin/run_backlog_groomer.sh >> <ROOT>/runtime/logs/backlog_groomer.log 2>&1
0 * * * * <ROOT>/bin/run_strategic_planner.sh >> <ROOT>/runtime/logs/strategic_planner.log 2>&1

# Daily digest
0 8 * * * <ROOT>/bin/run_daily_digest.sh >> <ROOT>/runtime/logs/daily_digest.log 2>&1
# ── Agent OS (managed by agentos init) — end ──
```

**Install:**
Write existing crontab (minus any previously-managed block) + new block to a temp file; `crontab <tempfile>`; verify by reading back.

**Verify first tick:** tail `runtime/logs/dispatcher.log` — wait up to 90 seconds for any write. If nothing appears, warn but do not fail — cron may simply be slow on the system. Print the log path so user can check manually.

### Phase 7 — Done banner

Print the summary box from §5. Specifically include:
- Repo URL, project URL, Telegram bot @handle
- Canonical paths: config.yaml, runtime/logs/, state file
- Pause command: `bin/agentos off`
- Re-run command: `bin/agentos init` (for adding another project)

## 8. State file format

Path: `<agent-os root>/runtime/init_state/<repo_name>.json`.

Atomic write: serialize, write to `<path>.tmp`, `os.replace`. Never partial-write.

```json
{
  "schema_version": 1,
  "started_at": "2026-04-21T12:00:00Z",
  "completed_at": null,
  "intake": {
    "idea": "...",
    "kind": "web",
    "stack_preference": "auto",
    "success_criteria": "..."
  },
  "github": {
    "owner": "kai-linux",
    "repo_name": "habit-tracker",
    "repo_full_name": "kai-linux/habit-tracker",
    "repo_url": "https://github.com/kai-linux/habit-tracker",
    "local_clone_path": "/Users/kai/projects/habit-tracker",
    "visibility": "private",
    "project_id": "PVT_abc",
    "project_number": 14,
    "project_url": "https://github.com/users/kai-linux/projects/14",
    "status_field_id": "PVTSSF_xyz",
    "status_option_ids": {
      "Ready": "opt_r",
      "In Progress": "opt_i",
      "Blocked": "opt_b",
      "Done": "opt_d"
    }
  },
  "charter": {
    "stack_decision": "...",
    "committed_sha": "abc123"
  },
  "issues_created": [
    {"number": 1, "title": "...", "item_id": "PVTI_..."},
    ...
  ],
  "telegram": {
    "bot_username": "kai_habittracker_bot",
    "chat_id": "987654321",
    "verified_at": "2026-04-21T12:05:00Z"
  },
  "config_written_path": "/Users/kai/agent-os/config.yaml",
  "cron_installed_at": "2026-04-21T12:06:00Z"
}
```

**Do NOT persist `telegram_bot_token` in the state file.** Token lives only in `config.yaml`, which the repo's `hooks/pre-commit` already guards. State file may leak (temp dir, logs) — tokens must not.

**Idempotency markers are key presence, not value:**
- `github.project_number` set → skip project creation
- `github.status_field_id` set → skip status field creation
- `charter.committed_sha` set → skip charter write
- `issues_created` non-empty → skip issue creation (or offer: keep existing / wipe and regenerate)
- `telegram.chat_id` set → skip telegram pairing (offer: reuse / redo)
- `cron_installed_at` set → skip cron install (offer: reinstall / skip)

## 9. Error handling conventions

- **Every external command gets a timeout.** `gh` calls: 30s each. `claude`: 300s. Telegram HTTP: 30s. `crontab`: 10s.
- **On failure, print three things:** what we were trying to do, the raw error from the tool, and the single most likely fix. Do not print a stack trace to the user (log to `runtime/logs/init.log` instead).
- **Never leave partial GitHub state:** if repo creation succeeds but project creation fails, the state file records repo_created=true; re-run will see it and skip repo but retry project. Do not attempt to delete partial resources.
- **Network failures** (telegram API, gh API): allow one silent retry after 2s, then surface the error.

## 10. Security

- Token, chat_id, and any secrets go in `config.yaml` only. `hooks/pre-commit` already blocks `config.yaml` commits — verify the hook is installed (`git config core.hooksPath` returns `hooks`); if not, install it before writing the config.
- Installing the hook guard means setting the repo-local config: `git config core.hooksPath hooks`. Do not modify the user's global git config.
- Mask the bot token in terminal output (show last 4 chars only).
- Do not log the token to `runtime/logs/init.log`.
- State file is `chmod 600` on write (same for `config.yaml`).

## 11. Key CLI commands reference

Collected here so the implementer doesn't re-derive. All are `gh` CLI v2.40+.

| Purpose | Command |
|---|---|
| Whoami | `gh api user --jq .login` |
| Auth scopes | `gh auth status` (parse for `project`) |
| Refresh scope | `gh auth refresh -s project` |
| Create repo | `gh repo create <owner>/<name> --public\|--private --clone` |
| Check repo exists | `gh api repos/<owner>/<name>` |
| Check repo empty | `gh api repos/<owner>/<name>/commits` (404 = empty) |
| Create project | `gh project create --owner <owner> --title <title> --format json` |
| List project fields | `gh project field-list <number> --owner <owner> --format json` |
| Create single-select field | `gh project field-create <number> --owner <owner> --name Status --data-type SINGLE_SELECT --single-select-options "Ready,In Progress,Blocked,Done" --format json` |
| Link project to repo | `gh project link <number> --owner <owner> --repo <owner>/<name>` |
| Create label | `gh label create ready --color 0E8A16 --repo <owner>/<name>` |
| Create issue | `gh issue create --repo <owner>/<name> --title <t> --body <b> --label ready --label prio:normal` |
| Add issue to project | `gh project item-add <number> --owner <owner> --url <issue_url> --format json` |
| Set Status field | `gh project item-edit --id <item_id> --project-id <project_id> --field-id <field_id> --single-select-option-id <opt_id>` |

Telegram HTTP endpoints (all `GET` except sendMessage):

| Purpose | URL |
|---|---|
| Verify token | `https://api.telegram.org/bot<TOKEN>/getMe` |
| Poll messages | `https://api.telegram.org/bot<TOKEN>/getUpdates?timeout=30` |
| Send message | `POST https://api.telegram.org/bot<TOKEN>/sendMessage` body `{"chat_id":..., "text":...}` |

## 12. Testing checklist (manual, before shipping)

Run this on a fresh account / repo before declaring done:

1. ✓ `agentos init` with `auto` stack on a fake-new-repo produces a working config, committed NORTH_STAR.md, 5 seed issues on a project board with Status=Ready, a valid crontab block, a successful Telegram test message.
2. ✓ Ctrl-C during Phase 3 (charter), re-run — Phase 0, 1, 2 skip with `✓ already done`; Phase 3 resumes.
3. ✓ Ctrl-C during Phase 6 (cron), re-run — only cron install re-runs.
4. ✓ Run twice for the same repo name — second run detects and offers reuse/abort, does not double-create.
5. ✓ Run twice for different repo names — second run warns that v1 is single-repo-only, backs up the existing `config.yaml`, and writes a fresh config for the new repo only. The old repo/project/state remain on GitHub and on disk; only the active local config is replaced.
6. ✓ Within 5 minutes of `init` completing on a clean system, at least one PR is opened by an agent on the new repo.
7. ✓ `bin/agentos off` + `bin/agentos on` still work unchanged.
8. ✓ `/status` in Telegram returns the correct ON/OFF state.
9. ✓ `test_init_config_emit.py` confirms the emitted config loads cleanly through whatever function the rest of the orchestrator uses (`orchestrator.<module>.load_config` — find it, reuse it).

## 13. Open decisions (already made — do not re-ask)

- **Default repo mode:** create new. (Existing-repo mode is the secondary branch.)
- **Telegram pairing:** walk through @BotFather in-terminal. Do not assume prior bot setup.
- **Multi-repo support in v1:** out of scope. Single repo per init. If config already exists, back it up.
- **Config overwrite semantics:** when init is run for a different repo, back up the existing `config.yaml` and replace it with a fresh single-repo config. Do not merge.
- **Fallback agents:** claude-only in the emitted config. Operator can widen later.
- **Charter regeneration:** max 1 auto-retry; then user picks edit / abort.
- **State file location:** `runtime/init_state/<repo_name>.json`. Not in `.gitignore` — add an entry for `runtime/init_state/` to `.gitignore` as part of the implementation (runtime artifacts don't belong in git).
- **Hook installation semantics:** set `git config core.hooksPath hooks` in the Agent OS repo before writing `config.yaml` if it is not already set.

## 14. File-level checklist for the implementer

Tick these off as you go:

- [ ] `orchestrator/init/__init__.py` (empty)
- [ ] `orchestrator/init/__main__.py` — top-level flow, calls phases in order, handles global `--help`, `--reset <repo>` (wipe state for a given repo), `--dry-run`
- [ ] `orchestrator/init/ui.py` — `info/ok/warn/fail/header/prompt/choice/password` helpers
- [ ] `orchestrator/init/state.py` — `State` class with `load(slug)`, `save()`, `mark(key, value)`, `has(key)`, atomic write
- [ ] `orchestrator/init/preflight.py` — `run() -> None`, raises `PreflightError` on any fail
- [ ] `orchestrator/init/dialogue.py` — `run(state) -> Intake`
- [ ] `orchestrator/init/github_scaffold.py` — `run(state, intake) -> GithubScaffold`
- [ ] `orchestrator/init/charter.py` — `run(state, intake, scaffold) -> Charter`, includes `call_claude(prompt)` and `create_seed_issues(charter, scaffold)`
- [ ] `orchestrator/init/telegram_pair.py` — `run(state) -> TelegramCreds`
- [ ] `orchestrator/init/config_emit.py` — `run(state, intake, scaffold, telegram) -> Path`
- [ ] `orchestrator/init/cron_install.py` — `run(state) -> None`, includes `read_current()`, `merge_block(current, new_block)`, `install(merged)`
- [ ] `bin/agentos` — add `init` subcommand that execs `python3 -m orchestrator.init`
- [ ] `tests/test_init_state.py`
- [ ] `tests/test_init_config_emit.py`
- [ ] `tests/test_init_cron_install.py`
- [ ] `tests/test_init_charter.py`
- [ ] `.gitignore` — add `runtime/init_state/`
- [ ] `README.md` — add **Option C: bootstrap from scratch** section (~8 lines)
- [ ] `docs/configuration.md` — top-note pointing at `agentos init`

## 15. Style

- Follow the existing codebase conventions (PyYAML, subprocess.run with `check=False` + explicit return code handling, f-strings, 4-space indent, type hints where they add value).
- Comments: only where the *why* is non-obvious (see operator's CLAUDE.md preferences at `~/.claude/CLAUDE.md`).
- No emojis in code or output except the check/cross/arrow glyphs already used in `demo.sh` (`▸ ✓ ! ✗`).
- Match `demo.sh`'s color palette for visual consistency.

## 16. When in doubt

- **Prefer re-runability over cleverness.** A phase that can safely run 100 times is worth more than one that's 50 lines shorter.
- **Prefer halting with a clear message over silently continuing.** Users can re-run — they can't un-corrupt their crontab.
- **Prefer the user's existing patterns over new abstractions.** Look at `demo.sh`, `bin/agentos`, `bin/run_*.sh` for how the operator likes scripts to feel. Match that.
