from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass


@dataclass
class CheckResult:
    ok: bool
    message: str
    hint: str | None = None


class PreflightError(RuntimeError):
    def __init__(self, failures: list[CheckResult]):
        super().__init__("Preflight checks failed")
        self.failures = failures


def _run(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def _check_gh() -> list[CheckResult]:
    results: list[CheckResult] = []
    if not shutil.which("gh"):
        return [CheckResult(False, "gh not found", "Install: https://cli.github.com")]

    auth = _run(["gh", "auth", "status"])
    combined = f"{auth.stdout}\n{auth.stderr}".strip()
    if auth.returncode != 0:
        results.append(CheckResult(False, "gh is not authenticated", "Run: gh auth login"))
        return results

    results.append(CheckResult(True, "gh authenticated"))
    if "project" in combined.lower():
        results.append(CheckResult(True, "gh project scope OK"))
    else:
        results.append(CheckResult(False, "gh project scope missing", "Run: gh auth refresh -s project"))
    return results


def _check_git_identity() -> CheckResult:
    name = _run(["git", "config", "user.name"])
    email = _run(["git", "config", "user.email"])
    if name.returncode == 0 and email.returncode == 0 and name.stdout.strip() and email.stdout.strip():
        return CheckResult(True, f"git identity set ({name.stdout.strip()} <{email.stdout.strip()}>)")
    return CheckResult(
        False,
        "git identity missing",
        'Run: git config --global user.name "You" && git config --global user.email "you@example.com"',
    )


def run() -> list[CheckResult]:
    results: list[CheckResult] = []
    if shutil.which("gh"):
        results.extend(_check_gh())
    else:
        results.append(CheckResult(False, "gh not found", "Install: https://cli.github.com"))

    if sys.version_info >= (3, 10):
        results.append(CheckResult(True, f"python3 {sys.version.split()[0]}"))
    else:
        results.append(CheckResult(False, f"python3 {sys.version.split()[0]}", "Upgrade python3 to 3.10+"))

    if shutil.which("claude"):
        results.append(CheckResult(True, "claude CLI found"))
    else:
        results.append(CheckResult(False, "claude CLI not found", "Install: https://docs.anthropic.com/en/docs/claude-code"))

    if shutil.which("crontab"):
        results.append(CheckResult(True, "crontab available"))
    else:
        results.append(CheckResult(False, "crontab not found", "Install crontab support for your system"))

    results.append(_check_git_identity())

    failures = [item for item in results if not item.ok]
    if failures:
        raise PreflightError(failures)
    return results

