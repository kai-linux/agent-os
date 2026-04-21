# Contributing to Agent OS

Thanks for your interest in contributing. This guide covers the expectations,
workflow, and conventions you need to get a PR merged.

## Getting Started

```bash
git clone https://github.com/kai-linux/agent-os.git
cd agent-os
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Verify your setup:

```bash
pytest tests/ -q
```

For the full setup walkthrough — prerequisites, pre-commit hook, which
modules are safe to run locally, and common troubleshooting — see
[docs/local-development.md](docs/local-development.md).

## Making Changes

1. **Create a branch** from `main`:

   ```bash
   git checkout -b your-branch-name main
   ```

2. **Keep diffs small.** One logical change per PR. Don't bundle unrelated
   fixes or refactors.

3. **Run tests before pushing:**

   ```bash
   pytest tests/ -v
   ```

   CI runs the same command on Python 3.12 / Ubuntu. If tests pass locally,
   they should pass in CI.

4. **Lint check.** CI verifies all `orchestrator/*.py` files compile:

   ```bash
   python -m py_compile orchestrator/your_file.py
   ```

## Commit Conventions

Use [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>: <short description>
```

| Type       | When to use                          | Example                                          |
|------------|--------------------------------------|--------------------------------------------------|
| `feat`     | New functionality                    | `feat: add retry budget to dispatcher`           |
| `fix`      | Bug fix                              | `fix: prevent duplicate follow-up issues`        |
| `docs`     | Documentation only                   | `docs: add deployment troubleshooting section`   |
| `test`     | Adding or updating tests             | `test: add regression test for health gate`      |
| `chore`    | Maintenance, config, CI              | `chore: update CI to Python 3.12`                |
| `refactor` | Code restructuring, no behavior change | `refactor: extract signature matching to module` |

Keep the subject line under 72 characters. Use the body for context when the
"why" isn't obvious from the diff.

## Pull Requests

- **Title** should follow the same commit convention (`feat: ...`, `fix: ...`).
- **Description** should include:
  - What changed and why
  - How to test it
  - Any decisions or tradeoffs worth noting
- Reference related issues with `Closes #N` or `Part of #N`.
- PRs are squash-merged, so your commit history doesn't need to be clean — but
  the PR title and description do.

## Code Review Process

1. Open a PR against `main`.
2. CI must pass (lint + `pytest tests/ -v`).
3. A maintainer reviews the change. Expect feedback on:
   - Correctness and edge cases
   - Diff size (smaller is better)
   - Test coverage for new behavior
   - Whether the change introduces unnecessary complexity
4. Address review comments, then re-request review.
5. Once approved and CI is green, a maintainer squash-merges the PR.

## Filing Issues

Use the [agent task template](.github/ISSUE_TEMPLATE/agent-task.md) when
creating issues. Structure the body with:

```markdown
## Goal
What should be true after this work is done.

## Success Criteria
- Measurable conditions for completion

## Constraints
- Boundaries on the solution
```

This format is required for automated task dispatch.

## Code Style

- Python 3.12+.
- No strict formatter enforced — match the style of surrounding code.
- Prefer clear names over comments. Add a comment only when the *why* is
  non-obvious.
- Don't add error handling for scenarios that can't happen. Trust internal
  code paths; validate at system boundaries.

## Project Layout

```
orchestrator/   # Core automation logic
tests/          # pytest test suite
bin/            # Shell scripts (cron entry points, utilities)
docs/           # Documentation and public-facing content
objectives/     # Repo objective definitions (metrics, weights)
```

## Questions?

Open an issue or start a [GitHub Discussion](https://github.com/kai-linux/agent-os/discussions).
