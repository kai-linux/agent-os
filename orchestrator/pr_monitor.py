"""PR monitor: polls CI status for agent-created PRs and auto-merges on green."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from orchestrator.paths import load_config, runtime_paths
from orchestrator.gh_project import (
    add_issue_comment,
    edit_issue_labels,
    query_project,
    set_item_status,
    gh,
    gh_json,
)

MAX_MERGE_ATTEMPTS = 3
STATE_FILE_NAME = "pr_monitor_state.json"


def _load_state(paths: dict) -> dict:
    state_file = Path(paths["LOGS"]) / STATE_FILE_NAME
    if state_file.exists():
        try:
            return json.loads(state_file.read_text())
        except Exception:
            return {}
    return {}


def _save_state(paths: dict, state: dict):
    state_file = Path(paths["LOGS"]) / STATE_FILE_NAME
    state_file.write_text(json.dumps(state, indent=2))


def _rebase_pr_onto_main(repo: str, pr: dict) -> bool:
    """Rebase a conflicting agent PR branch onto main and force-push. Returns True on success."""
    branch = pr.get("headRefName", "")
    base = pr.get("baseRefName", "main")
    if not branch:
        return False
    try:
        worktree_path = Path("/tmp") / f"rebase-{branch.replace('/', '-')}"
        # Use a temporary worktree so we don't disturb the caller's working directory
        repo_path = None
        from orchestrator.paths import load_config
        cfg = load_config()
        for pk, pcfg in cfg.get("github_projects", {}).items():
            for rcfg in pcfg.get("repos", []):
                if rcfg["github_repo"] == repo:
                    repo_path = Path(rcfg["local_repo"])
                    break
        if not repo_path:
            return False

        # Fetch latest
        subprocess.run(["git", "-C", str(repo_path), "fetch", "origin"], check=True, capture_output=True)

        # Create temp worktree on the PR branch
        if worktree_path.exists():
            subprocess.run(["git", "-C", str(repo_path), "worktree", "remove", "--force", str(worktree_path)], capture_output=True)
        subprocess.run(
            ["git", "-C", str(repo_path), "worktree", "add", str(worktree_path), f"origin/{branch}"],
            check=True, capture_output=True,
        )

        try:
            # Rebase onto origin/main, auto-resolving known conflict files
            env = {"GIT_EDITOR": "true", "HOME": str(Path.home()), "PATH": subprocess.os.environ.get("PATH", "")}
            result = subprocess.run(
                ["git", "-C", str(worktree_path), "rebase", f"origin/{base}"],
                capture_output=True, text=True, env={**subprocess.os.environ, "GIT_EDITOR": "true"},
            )
            if result.returncode != 0:
                # Auto-resolve: drop .agent_result.md (deleted on main), keep union for CODEBASE.md
                subprocess.run(["git", "-C", str(worktree_path), "rm", "-f", ".agent_result.md"], capture_output=True)
                subprocess.run(["git", "-C", str(worktree_path), "checkout", "--theirs", "CODEBASE.md"], capture_output=True)
                subprocess.run(["git", "-C", str(worktree_path), "add", "-A"], check=True, capture_output=True)
                subprocess.run(
                    ["git", "-C", str(worktree_path), "rebase", "--continue"],
                    check=True, capture_output=True,
                    env={**subprocess.os.environ, "GIT_EDITOR": "true"},
                )

            # Force-push rebased branch
            subprocess.run(
                ["git", "-C", str(worktree_path), "push", "origin", f"HEAD:{branch}", "--force-with-lease"],
                check=True, capture_output=True,
            )
            return True
        finally:
            subprocess.run(
                ["git", "-C", str(repo_path), "worktree", "remove", "--force", str(worktree_path)],
                capture_output=True,
            )
    except Exception as e:
        print(f"  Rebase error for {branch}: {e}")
        return False


def _list_agent_prs(repo: str) -> list[dict]:
    """List open PRs whose branch starts with 'agent/' (all orchestrator-created PRs)."""
    try:
        prs = gh_json([
            "pr", "list", "-R", repo,
            "--state", "open",
            "--json", "number,title,headRefName,baseRefName,isDraft,mergeable,mergeStateStatus,url,body",
        ]) or []
    except Exception as e:
        print(f"Warning: failed to list PRs for {repo}: {e}")
        return []
    return [pr for pr in prs if pr.get("headRefName", "").startswith("agent/")]


def _get_pr_checks(repo: str, pr_number: int) -> list[dict]:
    """Return CI check results for a PR."""
    try:
        result = subprocess.run(
            ["gh", "pr", "checks", str(pr_number), "-R", repo, "--json", "name,state,bucket"],
            capture_output=True, text=True,
        )
        # gh pr checks may return non-zero when checks are failing but still output valid JSON
        out = result.stdout.strip()
        return json.loads(out) if out else []
    except Exception as e:
        print(f"Warning: failed to get checks for PR #{pr_number} in {repo}: {e}")
        return []


def _checks_all_passed(checks: list[dict]) -> bool:
    """Return True only when every check has a terminal passing state."""
    if not checks:
        return False  # no checks means uncertain — don't auto-merge
    for c in checks:
        state = (c.get("state") or "").upper()
        bucket = (c.get("bucket") or "").lower()
        if state in ("PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED"):
            return False
        if bucket == "fail":
            return False
        if state not in ("SUCCESS", "NEUTRAL", "SKIPPED"):
            return False
    return True


def _checks_any_failed(checks: list[dict]) -> bool:
    """Return True when at least one check has a terminal failing state."""
    for c in checks:
        state = (c.get("state") or "").upper()
        bucket = (c.get("bucket") or "").lower()
        if bucket == "fail":
            return True
        if state in ("FAILURE", "ERROR", "TIMED_OUT", "ACTION_REQUIRED", "CANCELLED"):
            return True
    return False


def _extract_issue_number(pr_body: str) -> int | None:
    m = re.search(r"#(\d+)", pr_body or "")
    return int(m.group(1)) if m else None


def _handle_ci_failure(cfg: dict, repo: str, pr: dict, checks: list[dict], attempt: int):
    issue_number = _extract_issue_number(pr.get("body", ""))
    pr_number = pr["number"]
    pr_url = pr["url"]
    escalated = attempt >= MAX_MERGE_ATTEMPTS

    failed_checks = [
        c for c in checks
        if (c.get("conclusion") or "").lower()
        in ("failure", "error", "timed_out", "action_required", "cancelled")
    ]
    check_lines = "\n".join(
        f"- **{c['name']}**: `{c.get('conclusion', 'unknown')}`" for c in failed_checks
    ) or "- (no details available)"

    comment = f"""## Auto-merge blocked: CI failure

**PR:** {pr_url}
**Attempt:** {attempt}/{MAX_MERGE_ATTEMPTS}
{"**Status:** Escalated — max attempts reached, manual intervention required." if escalated else ""}

### Failed checks
{check_lines}

### Next step
{"This PR has exceeded the maximum auto-merge attempts. Please review and merge manually." if escalated else "The orchestrator will retry once CI passes."}
"""

    if issue_number:
        try:
            add_issue_comment(repo, issue_number, comment)
        except Exception as e:
            print(f"Warning: failed to post comment on #{issue_number}: {e}")

        try:
            edit_issue_labels(repo, issue_number, add=["blocked"], remove=["in-progress", "ready"])
        except Exception as e:
            print(f"Warning: failed to update labels on #{issue_number}: {e}")

        # Set project Status → Blocked
        owner = cfg.get("github_owner", "")
        for project_cfg in cfg.get("github_projects", {}).values():
            for repo_cfg in project_cfg.get("repos", []):
                if repo_cfg.get("github_repo") != repo:
                    continue
                blocked_value = project_cfg.get("blocked_value", "Blocked")
                try:
                    info = query_project(project_cfg["project_number"], owner)
                    option_id = info["status_options"].get(blocked_value)
                    if not info["status_field_id"] or not option_id:
                        print(f"Warning: status option '{blocked_value}' not found in project")
                        break
                    issue_url_prefix = f"https://github.com/{repo}/issues/{issue_number}"
                    for item in info["items"]:
                        if item["url"].startswith(issue_url_prefix):
                            set_item_status(
                                info["project_id"],
                                item["item_id"],
                                info["status_field_id"],
                                option_id,
                            )
                            print(f"Project status set to '{blocked_value}' for #{issue_number}")
                            break
                    else:
                        print(f"Warning: issue #{issue_number} not found in project")
                except Exception as e:
                    print(f"Warning: failed to update project status: {e}")
                break


def _try_merge(repo: str, pr_number: int) -> bool:
    """Attempt squash merge. Returns True on success."""
    try:
        gh(["pr", "merge", str(pr_number), "-R", repo, "--squash", "--delete-branch"])
        return True
    except Exception as e:
        print(f"Warning: merge failed for PR #{pr_number} in {repo}: {e}")
        return False


def monitor_prs():
    cfg = load_config()
    paths = runtime_paths(cfg)
    state = _load_state(paths)

    repos: set[str] = set()
    for project_cfg in cfg.get("github_projects", {}).values():
        for repo_cfg in project_cfg.get("repos", []):
            r = repo_cfg.get("github_repo")
            if r:
                repos.add(r)

    if not repos:
        print("No repos configured in github_projects. Nothing to monitor.")
        return

    for repo in sorted(repos):
        prs = _list_agent_prs(repo)
        if not prs:
            print(f"{repo}: no open agent PRs")
            continue
        print(f"{repo}: found {len(prs)} agent PR(s)")

        for pr in prs:
            pr_url = pr["url"]
            pr_number = pr["number"]
            pr_title = pr["title"]
            attempts = state.get(pr_url, {}).get("attempts", 0)

            if pr.get("isDraft"):
                print(f"  PR #{pr_number}: draft, skipping")
                continue

            if attempts >= MAX_MERGE_ATTEMPTS:
                print(f"  PR #{pr_number}: max merge attempts reached, skipping")
                continue

            checks = _get_pr_checks(repo, pr_number)
            print(f"  PR #{pr_number} '{pr_title}': {len(checks)} check(s)")

            if _checks_any_failed(checks):
                new_attempts = attempts + 1
                state.setdefault(pr_url, {})["attempts"] = new_attempts
                _save_state(paths, state)
                print(f"  PR #{pr_number}: CI failed (attempt {new_attempts}/{MAX_MERGE_ATTEMPTS})")
                _handle_ci_failure(cfg, repo, pr, checks, new_attempts)
                continue

            if not _checks_all_passed(checks):
                print(f"  PR #{pr_number}: checks pending, will retry next poll")
                continue

            mergeable = (pr.get("mergeable") or "").upper()
            if mergeable == "CONFLICTING":
                print(f"  PR #{pr_number}: has merge conflicts, attempting auto-rebase...")
                if _rebase_pr_onto_main(repo, pr):
                    print(f"  PR #{pr_number}: rebased successfully, will merge next poll")
                else:
                    print(f"  PR #{pr_number}: rebase failed, skipping")
                continue

            new_attempts = attempts + 1
            state.setdefault(pr_url, {})["attempts"] = new_attempts
            _save_state(paths, state)
            print(f"  PR #{pr_number}: all checks passed, merging (attempt {new_attempts}/{MAX_MERGE_ATTEMPTS})")

            if _try_merge(repo, pr_number):
                print(f"  PR #{pr_number}: merged successfully")
                state.pop(pr_url, None)
                _save_state(paths, state)
            else:
                print(f"  PR #{pr_number}: merge failed")


if __name__ == "__main__":
    monitor_prs()
