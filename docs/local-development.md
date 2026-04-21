# Local Development Setup

Get a working Agent OS checkout that can run tests, import orchestrator
modules, and exercise control-plane code paths locally — without needing
GitHub credentials, Claude credentials, or a writable `/srv/worktrees`.

This guide is the shared baseline linked from
[CONTRIBUTING.md](../CONTRIBUTING.md) and `FORK_GUIDE.md`.

> **Running Agent OS against a real repo** (dispatch, execute, merge) is a
> separate setup — see [deployment-guide.md](deployment-guide.md). This doc
> gets you to "I can edit code and run the test suite."

---

## Prerequisites

| Tool     | Version    | Why                                  | Check                |
|----------|------------|--------------------------------------|----------------------|
| Python   | 3.10+      | Orchestrator + pytest                | `python3 --version`  |
| Git      | any recent | Clone, worktrees, pre-commit hook    | `git --version`      |
| GitHub CLI | 2.x+     | Only if running dispatcher/PR paths  | `gh --version`       |

Claude / Codex / Gemini / DeepSeek CLIs are only required for actual agent
execution. You do **not** need them to develop, run tests, or change
orchestrator code.

---

## 1. Clone and install

```bash
git clone https://github.com/kai-linux/agent-os
cd agent-os
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

`requirements.txt` is three packages: `PyYAML`, `pytest`, `duckduckgo-search`.
Install takes under a minute on a fresh venv.

`.venv/`, `config.yaml`, `objectives/*.yaml` (except `example.yaml`),
`runtime/`, and `*.log` are in `.gitignore` — your local config and runtime
artifacts never end up in a commit.

---

## 2. Run the test suite

This is the verification step. If this command passes, your setup is good.

```bash
pytest tests/ -q
```

Expected: all tests pass in under a minute. Example:

```
........................................................................ [ 13%]
...
539 passed in 26.67s
```

CI runs the same command on Python 3.12 / Ubuntu. If tests pass locally, they
should pass in CI.

`make test` is a shortcut wrapper that runs `pytest tests/ -q` against the
repo's `.venv`.

### Run one test file

```bash
pytest tests/test_queue.py -q                    # one file
pytest tests/test_queue.py::test_specific -q     # one test
pytest tests/ -q -k "dispatcher"                 # pattern match
```

### Compile check

CI also verifies every `orchestrator/*.py` imports cleanly. Locally:

```bash
python3 -m py_compile orchestrator/*.py
```

---

## 3. Enable the pre-commit hook

The repo ships a `hooks/pre-commit` guard that blocks commits touching
`config.yaml`, `objectives/<repo>.yaml` (except `example.yaml`), or any staged
diff matching a Telegram bot-token shape. Enable once per clone:

```bash
git config core.hooksPath hooks
```

This is strongly recommended — it prevents you from accidentally committing a
local config or a real secret.

---

## 4. Import orchestrator modules

Orchestrator is a regular Python package. From the repo root with the venv
active:

```bash
python3 -c "import orchestrator; print(orchestrator.__file__)"
python3 -c "from orchestrator.queue import get_agent_chain; print(get_agent_chain)"
```

Use this for quick REPL exploration of control-plane code paths during
development.

---

## 5. What NOT to run locally (without reading first)

These entrypoints have side effects against real GitHub / filesystem state.
Safe to run once you have a `config.yaml` pointing at a test repo, but do not
run blindly on a fresh clone:

| Module                             | What it does                                    |
|------------------------------------|-------------------------------------------------|
| `orchestrator.github_dispatcher`   | Reads `Status: Ready` issues, writes mailbox    |
| `orchestrator.queue`               | Creates worktrees, invokes agents, pushes branches |
| `orchestrator.pr_monitor`          | Rebases, merges, comments on real PRs           |
| `orchestrator.backlog_groomer`     | Files real GitHub issues against configured repos |
| `orchestrator.strategic_planner`   | Writes to `STRATEGY.md`, files planning issues  |

For a zero-risk end-to-end run against a scratch repo, use `./demo.sh` — it
generates a temporary config under `/tmp`, creates one issue, runs one task,
and leaves cleanup hints at the end.

---

## 6. Repo layout

```
orchestrator/   # Core automation logic (one file = one concern)
tests/          # pytest test suite (38 files, 539+ tests)
bin/            # Shell wrappers for cron entrypoints + utilities
hooks/          # Pre-commit secret guard
docs/           # This guide, deployment-guide.md, architecture.md, ...
objectives/     # Repo objective definitions (example.yaml only is tracked)
example.config.yaml   # Annotated full config reference
```

Further reading:
- [CONTRIBUTING.md](../CONTRIBUTING.md) — branch, commit, and PR conventions
- [docs/architecture.md](architecture.md) — how the pieces fit together
- [docs/execution.md](execution.md) — task execution flow + `.agent_result.md` contract
- [docs/configuration.md](configuration.md) — full config reference

---

## Troubleshooting

**`pytest` not found.** Activate the venv: `source .venv/bin/activate`. Or
run `.venv/bin/python3 -m pytest tests/ -q` without activation.

**Some tests fail on a fresh clone.** Make sure you are on `main`, the venv
is active, and you installed the *current* `requirements.txt` (rerun
`pip install -r requirements.txt`). Tests should be deterministic; no
network, GitHub, or Claude credentials are required.

**`make test` fails with "python3: command not found".** The Makefile
target uses `.venv/bin/python3` directly — create the venv first
(`python3 -m venv .venv && pip install -r requirements.txt`).

**Pre-commit hook blocks a legitimate commit.** The hook rejects any staged
change to `config.yaml` or a real `objectives/<repo>.yaml`. If you really
need to modify the tracked `example.config.yaml` or `objectives/example.yaml`,
those are explicitly allowed. For unusual cases, temporarily unset the hook
path — but re-enable it before your next commit.
