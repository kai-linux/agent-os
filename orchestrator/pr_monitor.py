"""PR monitor: polls CI status for agent-created PRs and auto-merges on green."""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from orchestrator.paths import load_config, runtime_paths
from orchestrator.outcome_attribution import (
    append_outcome_record,
    extract_task_id_from_pr_title,
    load_outcome_records,
)
from orchestrator.gh_project import (
    add_issue_comment,
    create_pr_for_branch,
    edit_issue_labels,
    ensure_labels,
    query_project,
    set_item_status,
    gh,
    gh_json,
)
from orchestrator.ci_artifact_validator import (
    validate_ci_artifacts,
    format_validation_log,
)
from orchestrator.privacy import redact_text
from orchestrator.repo_modes import is_dispatcher_only_repo

MAX_MERGE_ATTEMPTS = 3
STATE_FILE_NAME = "pr_monitor_state.json"
_CI_REMEDIATION_RE = re.compile(r"^Fix CI failure on PR #(\d+)$")
_FOLLOWUP_TITLE_RE = re.compile(r"^Follow up partial debug for root issue #(\d+)$")
_ROOT_ISSUE_RE = re.compile(r"^## Root Issue Number\s*\n(\d+)\s*$", re.MULTILINE)
_ROOT_PR_RE = re.compile(r"^## Root PR Number\s*\n(\d+)\s*$", re.MULTILINE)
_ROOT_BRANCH_RE = re.compile(r"^## Root Branch\s*\n(.+?)\s*$", re.MULTILINE)
_BRANCH_RE = re.compile(r"^## Branch\s*\n(.+?)\s*$", re.MULTILINE)
_ORIGINAL_ISSUE_RE = re.compile(r"Original issue:\s+#(\d+)", re.IGNORECASE)

_FORK_PR_CLOSE_MSG = (
    "Closed automatically — this repository does not accept pull requests "
    "from forks. The CI/CD pipeline only processes internal agent branches.\n\n"
    "If you have a contribution, please open an issue instead (note: issues "
    "from external authors are also auto-closed by our automation)."
)


def _close_fork_prs(repos: set[str]):
    """Close open PRs from forks across all configured repos."""
    for repo in repos:
        try:
            prs = gh_json([
                "pr", "list", "-R", repo, "--state", "open",
                "--json", "number,headRefName,isCrossRepository",
            ]) or []
        except Exception:
            continue
        for pr in prs:
            if not pr.get("isCrossRepository", False):
                continue
            try:
                gh([
                    "pr", "close", str(pr["number"]), "-R", repo,
                    "--comment", _FORK_PR_CLOSE_MSG,
                ], check=False)
                print(f"Closed fork PR {repo}#{pr['number']} (branch: {pr.get('headRefName', '?')})")
            except Exception as e:
                print(f"Warning: failed to close fork PR {repo}#{pr['number']}: {e}")


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


def _get_conflicted_files(worktree_path: Path) -> list[str]:
    """Return list of files with unresolved merge conflicts (excluding already-handled ones)."""
    result = subprocess.run(
        ["git", "-C", str(worktree_path), "diff", "--name-only", "--diff-filter=U"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return []
    skip = {"CODEBASE.md", ".agent_result.md"}
    return [f for f in result.stdout.strip().splitlines() if f and f not in skip]


def _try_union_resolve(worktree_path: Path, conflict_files: list[str]) -> bool:
    """Try to resolve conflicts by keeping content from both sides (union merge).

    For each file, strips conflict markers and keeps all lines. Returns True
    only if every file was resolved cleanly.
    """
    marker_ours = re.compile(r"^<{7}\s.*$", re.MULTILINE)
    marker_sep = re.compile(r"^={7}\s*$", re.MULTILINE)
    marker_theirs = re.compile(r"^>{7}\s.*$", re.MULTILINE)

    for filepath in conflict_files:
        full_path = worktree_path / filepath
        if not full_path.exists():
            return False
        try:
            content = full_path.read_text()
        except Exception:
            return False

        # Verify it actually has conflict markers
        if not marker_ours.search(content):
            continue

        # Strip all three marker types, keeping all code from both sides
        resolved = marker_ours.sub("", content)
        resolved = marker_sep.sub("", resolved)
        resolved = marker_theirs.sub("", resolved)

        # Clean up excessive blank lines left by marker removal
        resolved = re.sub(r"\n{3,}", "\n\n", resolved)

        full_path.write_text(resolved)
        subprocess.run(
            ["git", "-C", str(worktree_path), "add", filepath],
            check=True, capture_output=True,
        )
        print(f"  Auto-resolved conflicts in {filepath} (union merge)")

    return True


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
            result = subprocess.run(
                ["git", "-C", str(worktree_path), "rebase", f"origin/{base}"],
                capture_output=True, text=True, env={**subprocess.os.environ, "GIT_EDITOR": "true"},
            )
            # Loop to handle conflicts across multiple rebase steps
            max_steps = 20
            step = 0
            while result.returncode != 0 and step < max_steps:
                step += 1
                # Auto-resolve known safe files
                subprocess.run(["git", "-C", str(worktree_path), "rm", "-f", ".agent_result.md"], capture_output=True)
                subprocess.run(["git", "-C", str(worktree_path), "checkout", "--theirs", "CODEBASE.md"], capture_output=True)

                # For remaining conflicted files, try union merge (keep both sides)
                conflict_files = _get_conflicted_files(worktree_path)
                if conflict_files:
                    resolved = _try_union_resolve(worktree_path, conflict_files)
                    if not resolved:
                        print(f"  Rebase has conflicts that could not be auto-resolved, aborting")
                        subprocess.run(
                            ["git", "-C", str(worktree_path), "rebase", "--abort"],
                            capture_output=True,
                        )
                        return False

                subprocess.run(["git", "-C", str(worktree_path), "add", "-A"], check=True, capture_output=True)
                result = subprocess.run(
                    ["git", "-C", str(worktree_path), "rebase", "--continue"],
                    capture_output=True, text=True,
                    env={**subprocess.os.environ, "GIT_EDITOR": "true"},
                )

            if result.returncode != 0:
                print(f"  Rebase failed after {step} resolution steps, aborting")
                subprocess.run(["git", "-C", str(worktree_path), "rebase", "--abort"], capture_output=True)
                return False

            if step > 0:
                # Validate with tests after conflict resolution
                test_result = subprocess.run(
                    ["python3", "-m", "pytest", "tests/", "-x", "-q", "--tb=no"],
                    capture_output=True, text=True, cwd=str(worktree_path), timeout=120,
                )
                if test_result.returncode != 0:
                    print(f"  Tests failed after auto-resolved rebase, reverting")
                    # Can't abort after rebase completed — reset branch to pre-rebase state
                    subprocess.run(
                        ["git", "-C", str(worktree_path), "reset", "--hard", f"origin/{branch}"],
                        capture_output=True,
                    )
                    return False

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


def _list_agent_branches(repo: str) -> list[str]:
    """List remote branches starting with agent/ for the given repo."""
    try:
        raw = gh(["api", f"repos/{repo}/branches?per_page=100", "--paginate",
                  "--jq", ".[].name"], check=False)
        return [b for b in raw.splitlines() if b.startswith("agent/")]
    except Exception as e:
        print(f"Warning: failed to list branches for {repo}: {e}")
        return []


def _open_pr_branches(repo: str) -> set[str]:
    """Return set of head branch names that already have an open PR."""
    try:
        prs = gh_json([
            "pr", "list", "-R", repo, "--state", "open",
            "--json", "headRefName",
        ]) or []
        return {pr["headRefName"] for pr in prs}
    except Exception:
        return set()


def _branch_has_commits_ahead_of_main(repo: str, branch: str, base: str = "main") -> bool:
    """Return True if the branch has at least one commit not on base."""
    try:
        out = gh(["api", f"repos/{repo}/compare/{base}...{branch}",
                  "--jq", ".ahead_by"], check=False)
        return int(out.strip() or "0") > 0
    except Exception:
        return False


def _find_issue_for_task(repo: str, task_id: str) -> int | None:
    """Search for an open or recently closed issue that was dispatched for this task."""
    if not task_id:
        return None
    try:
        issues = gh_json([
            "issue", "list", "-R", repo, "--state", "all",
            "--search", task_id, "--json", "number,body",
            "--limit", "10",
        ]) or []
    except Exception:
        return None
    for issue in issues:
        body = issue.get("body", "")
        if task_id in body or f"agent/{task_id}" in body:
            return int(issue["number"])
    return None


def _find_merged_pr_for_task(repo: str, task_id: str) -> dict | None:
    if not task_id:
        return None
    title = f"Agent: {task_id}"
    try:
        prs = gh_json([
            "pr", "list", "-R", repo, "--state", "merged",
            "--search", title,
            "--json", "number,title,headRefName,url",
            "--limit", "20",
        ]) or []
    except Exception:
        return None

    for pr in prs:
        if (pr.get("title") or "").strip() == title:
            return pr
    return None


def _create_prs_for_orphan_branches(repos: set[str]):
    """Open PRs for agent branches that have commits but no open PR yet."""
    for repo in sorted(repos):
        try:
            agent_branches = _list_agent_branches(repo)
        except Exception:
            continue
        if not agent_branches:
            continue

        pr_branches = _open_pr_branches(repo)
        orphans = [b for b in agent_branches if b not in pr_branches]
        if not orphans:
            continue

        print(f"{repo}: checking {len(orphans)} agent branch(es) without open PRs")
        for branch in orphans:
            task_id = branch[len("agent/"):]
            if not _branch_has_commits_ahead_of_main(repo, branch):
                # Branch is fully merged into main — delete it
                try:
                    gh(["api", f"repos/{repo}/git/refs/heads/{branch}",
                        "-X", "DELETE"], check=False)
                    print(f"  Deleted stale merged branch {branch}")
                except Exception:
                    pass
                continue
            if _find_merged_pr_for_task(repo, task_id):
                # Has commits ahead but a PR was already merged — also delete
                try:
                    gh(["api", f"repos/{repo}/git/refs/heads/{branch}",
                        "-X", "DELETE"], check=False)
                    print(f"  Deleted branch {branch}: merged PR already exists for task {task_id}")
                except Exception:
                    pass
                continue
            title = f"Agent: {task_id}"
            # Try to find the linked issue number for proper cleanup on merge
            issue_number = _find_issue_for_task(repo, task_id)
            if issue_number:
                body = f"Automated changes for issue #{issue_number}"
            else:
                body = f"Automated changes from agent branch `{branch}`."
            pr_url = create_pr_for_branch(repo, branch, title, body)
            if pr_url:
                print(f"  Opened PR for orphan branch {branch}: {pr_url}")
            else:
                print(f"  Warning: failed to open PR for {branch}")


def _list_agent_prs(repo: str) -> list[dict]:
    """List open PRs whose branch starts with 'agent/' and originates from the same repo (not a fork)."""
    try:
        prs = gh_json([
            "pr", "list", "-R", repo,
            "--state", "open",
            "--json", "number,title,headRefName,baseRefName,isDraft,mergeable,mergeStateStatus,url,body,isCrossRepository",
        ]) or []
    except Exception as e:
        print(f"Warning: failed to list PRs for {repo}: {e}")
        return []
    return [
        pr for pr in prs
        if pr.get("headRefName", "").startswith("agent/")
        and not pr.get("isCrossRepository", False)
    ]


def _get_pr_checks(repo: str, pr_number: int) -> list[dict]:
    """Return CI check results for a PR."""
    try:
        result = subprocess.run(
            ["gh", "pr", "checks", str(pr_number), "-R", repo, "--json", "name,state,bucket,link"],
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


def _failed_checks(checks: list[dict]) -> list[dict]:
    failed = []
    for c in checks:
        state = (c.get("state") or "").upper()
        bucket = (c.get("bucket") or "").lower()
        if bucket == "fail" or state in ("FAILURE", "ERROR", "TIMED_OUT", "ACTION_REQUIRED", "CANCELLED"):
            failed.append(c)
    return failed


def _format_failed_checks(checks: list[dict]) -> str:
    failed = _failed_checks(checks)
    lines = []
    for c in failed:
        state = (c.get("state") or "unknown").lower()
        link = (c.get("link") or "").strip()
        suffix = f" ([link]({link}))" if link else ""
        lines.append(f"- **{c.get('name', 'unknown')}**: `{state}`{suffix}")
    return "\n".join(lines) or "- (no details available)"


def _missing_checks_stub() -> list[dict]:
    return [{
        "name": "required checks missing",
        "state": "ERROR",
        "bucket": "fail",
        "link": "",
    }]


def _find_repo_project(cfg: dict, repo: str) -> tuple[str, dict, dict] | tuple[None, None, None]:
    for project_key, project_cfg in cfg.get("github_projects", {}).items():
        for repo_cfg in project_cfg.get("repos", []):
            if repo_cfg.get("github_repo") == repo:
                return project_key, project_cfg, repo_cfg
    return None, None, None


def _find_open_issue_by_title(repo: str, title: str) -> dict | None:
    return _find_issue_by_title(repo, title, state="open")


def _find_issue_by_title(repo: str, title: str, *, state: str = "open") -> dict | None:
    try:
        issues = gh_json([
            "issue", "list", "-R", repo, "--state", state,
            "--search", title, "--json", "number,title,url,labels",
            "--limit", "20",
        ]) or []
    except Exception:
        return None

    for issue in issues:
        if (issue.get("title") or "").strip() == title.strip():
            return issue
    return None


def _list_followup_debug_issues(repo: str, *, state: str = "open") -> list[dict]:
    try:
        issues = gh_json([
            "issue", "list", "-R", repo, "--state", state,
            "--search", '"Follow up partial debug"',
            "--json", "number,title,body,state,labels,url",
            "--limit", "100",
        ]) or []
    except Exception:
        return []
    return [issue for issue in issues if _FOLLOWUP_TITLE_RE.match(issue.get("title", ""))]


def _extract_followup_root_issue(issue: dict) -> int | None:
    body = issue.get("body", "") or ""
    match = _ROOT_ISSUE_RE.search(body)
    if match:
        return int(match.group(1))
    legacy = _ORIGINAL_ISSUE_RE.search(body)
    if legacy:
        return int(legacy.group(1))
    return None


def _extract_followup_root_pr(issue: dict) -> int | None:
    match = _ROOT_PR_RE.search(issue.get("body", "") or "")
    if match:
        return int(match.group(1))
    return None


def _extract_followup_branch(issue: dict) -> str:
    body = issue.get("body", "") or ""
    for pattern in (_ROOT_BRANCH_RE, _BRANCH_RE):
        match = pattern.search(body)
        if match:
            return match.group(1).strip()
    return ""


def _set_project_issue_status(cfg: dict, repo: str, issue_number: int, status_value: str):
    owner = cfg.get("github_owner", "")
    _project_key, project_cfg, _repo_cfg = _find_repo_project(cfg, repo)
    if not owner or not project_cfg:
        return

    try:
        info = query_project(project_cfg["project_number"], owner)
        option_id = info["status_options"].get(status_value)
        if not info["status_field_id"] or not option_id:
            return
        issue_url_prefix = f"https://github.com/{repo}/issues/{issue_number}"
        for item in info["items"]:
            if item["url"].startswith(issue_url_prefix):
                set_item_status(info["project_id"], item["item_id"], info["status_field_id"], option_id)
                return
    except Exception as e:
        print(f"Warning: failed to set project status {status_value!r} for {repo}#{issue_number}: {e}")


def _mark_issue_done(cfg: dict, repo: str, issue_number: int, *, close_issue: bool, comment: str | None = None):
    _project_key, project_cfg, _repo_cfg = _find_repo_project(cfg, repo)
    edit_issue_labels(
        repo,
        issue_number,
        add=["done"],
        remove=["blocked", "in-progress", "ready", "agent-dispatched"],
    )
    if comment:
        try:
            add_issue_comment(repo, issue_number, comment)
        except Exception as e:
            print(f"Warning: failed to comment on {repo}#{issue_number}: {e}")
    if close_issue:
        try:
            gh(["issue", "close", str(issue_number), "-R", repo], check=False)
        except Exception as e:
            print(f"Warning: failed to close {repo}#{issue_number}: {e}")
    if project_cfg:
        _set_project_issue_status(cfg, repo, issue_number, project_cfg.get("done_value", "Done"))


def _reconcile_issue_board_state(cfg: dict, repo: str, issue: dict):
    issue_number = issue.get("number")
    if not issue_number:
        return
    state = str(issue.get("state", "")).upper()
    labels = {lbl.get("name", "").strip().lower() for lbl in issue.get("labels", []) if isinstance(lbl, dict)}
    if state == "CLOSED" and ("blocked" in labels or "in-progress" in labels or "ready" in labels or "agent-dispatched" in labels):
        _mark_issue_done(
            cfg,
            repo,
            int(issue_number),
            close_issue=False,
            comment=None,
        )


def _cleanup_descendant_followup_issues(cfg: dict, repo: str, *, remediation_issue_number: int | None, pr_number: int, branch: str) -> int:
    closed = 0
    for issue in _list_followup_debug_issues(repo, state="open"):
        followup_branch = _extract_followup_branch(issue)
        followup_root_issue = _extract_followup_root_issue(issue)
        followup_root_pr = _extract_followup_root_pr(issue)

        matches_branch = bool(branch and followup_branch == branch)
        matches_root_issue = remediation_issue_number is not None and followup_root_issue == remediation_issue_number
        matches_root_pr = followup_root_pr == pr_number
        if not (matches_branch or matches_root_issue or matches_root_pr):
            continue

        _mark_issue_done(
            cfg,
            repo,
            int(issue["number"]),
            close_issue=True,
            comment=f"✅ Closed automatically after PR #{pr_number} merged and superseded this follow-up chain.",
        )
        closed += 1
    return closed


def _close_stale_redundant_agent_prs(repo: str) -> int:
    closed = 0
    for pr in _list_agent_prs(repo):
        task_id = extract_task_id_from_pr_title(pr.get("title"))
        if not task_id:
            continue
        merged = _find_merged_pr_for_task(repo, task_id)
        if not merged or merged.get("number") == pr.get("number"):
            continue
        try:
            gh([
                "pr", "close", str(pr["number"]), "-R", repo,
                "--comment", f"Closed automatically as stale automation drift; PR #{merged['number']} already merged for task `{task_id}`.",
            ], check=False)
            closed += 1
        except Exception as e:
            print(f"Warning: failed to close stale PR #{pr['number']} in {repo}: {e}")
    return closed


def _mark_issue_in_progress(cfg: dict, repo: str, issue_number: int, *, reopen_issue: bool, comment: str | None = None):
    _project_key, project_cfg, _repo_cfg = _find_repo_project(cfg, repo)
    if reopen_issue:
        try:
            gh(["issue", "reopen", str(issue_number), "-R", repo], check=False)
        except Exception as e:
            print(f"Warning: failed to reopen {repo}#{issue_number}: {e}")
    edit_issue_labels(
        repo,
        issue_number,
        add=["in-progress", "agent-dispatched"],
        remove=["blocked", "ready", "done"],
    )
    if comment:
        try:
            add_issue_comment(repo, issue_number, comment)
        except Exception as e:
            print(f"Warning: failed to comment on {repo}#{issue_number}: {e}")
    if project_cfg:
        _set_project_issue_status(cfg, repo, issue_number, project_cfg.get("in_progress_value", "In Progress"))


def _mark_issue_ready(cfg: dict, repo: str, issue_number: int, *, reopen_issue: bool, comment: str | None = None):
    _project_key, project_cfg, _repo_cfg = _find_repo_project(cfg, repo)
    if reopen_issue:
        try:
            gh(["issue", "reopen", str(issue_number), "-R", repo], check=False)
        except Exception as e:
            print(f"Warning: failed to reopen {repo}#{issue_number}: {e}")
    edit_issue_labels(
        repo,
        issue_number,
        add=["ready"],
        remove=["blocked", "in-progress", "agent-dispatched", "done"],
    )
    if comment:
        try:
            add_issue_comment(repo, issue_number, comment)
        except Exception as e:
            print(f"Warning: failed to comment on {repo}#{issue_number}: {e}")
    if project_cfg:
        _set_project_issue_status(cfg, repo, issue_number, project_cfg.get("ready_value", "Ready"))


def _cleanup_merged_pr_issues(cfg: dict, repo: str, pr: dict):
    pr_number = pr["number"]
    issue_number = _extract_issue_number(pr.get("body", ""))
    task_id = extract_task_id_from_pr_title(pr.get("title"))
    branch = str(pr.get("headRefName", "")).strip()
    prior_records = load_outcome_records(cfg, repo=repo)
    outcome_check_ids: list[str] = []
    merge_already_logged = False
    for record in reversed(prior_records):
        if record.get("record_type") == "attribution" and record.get("event") == "merged" and record.get("pr_number") == pr_number:
            merge_already_logged = True
            break
        if record.get("record_type") != "attribution":
            continue
        if task_id and record.get("task_id") == task_id:
            outcome_check_ids = list(record.get("outcome_check_ids") or [])
            break
        if record.get("pr_number") == pr_number:
            outcome_check_ids = list(record.get("outcome_check_ids") or [])
            break

    if not merge_already_logged:
        append_outcome_record(
            cfg,
            {
                "record_type": "attribution",
                "event": "merged",
                "repo": repo,
                "task_id": task_id,
                "issue_number": issue_number,
                "pr_number": pr_number,
                "pr_url": pr.get("url"),
                "branch": pr.get("headRefName"),
                "merged_at": pr.get("mergedAt"),
                "outcome_check_ids": outcome_check_ids,
            },
        )

    if issue_number:
        _mark_issue_done(
            cfg,
            repo,
            issue_number,
            close_issue=True,
            comment=f"✅ PR #{pr_number} merged successfully. Clearing blocked state and marking the issue done.",
        )

    remediation_issue_number = None
    remediation_title = f"Fix CI failure on PR #{pr_number}"
    remediation_issue = _find_open_issue_by_title(repo, remediation_title)
    if remediation_issue and remediation_issue.get("number"):
        remediation_issue_number = int(remediation_issue["number"])
        _mark_issue_done(
            cfg,
            repo,
            remediation_issue_number,
            close_issue=True,
            comment=f"✅ Resolved automatically after PR #{pr_number} merged.",
        )
    _cleanup_descendant_followup_issues(
        cfg,
        repo,
        remediation_issue_number=remediation_issue_number,
        pr_number=pr_number,
        branch=branch,
    )


def _reconcile_open_pr_state(cfg: dict, repo: str, pr: dict, checks: list[dict], state: dict) -> bool:
    changed = False
    pr_number = pr["number"]
    pr_url = pr["url"]
    issue_number = _extract_issue_number(pr.get("body", ""))
    remediation_title = f"Fix CI failure on PR #{pr_number}"

    if issue_number:
        _mark_issue_in_progress(
            cfg,
            repo,
            issue_number,
            reopen_issue=True,
        )

    if not _checks_any_failed(checks):
        return changed

    remediation_issue = _find_issue_by_title(repo, remediation_title, state="all")
    if remediation_issue and remediation_issue.get("number"):
        reopen_needed = str(remediation_issue.get("state", "")).upper() != "OPEN"
        if reopen_needed:
            _mark_issue_ready(
                cfg,
                repo,
                int(remediation_issue["number"]),
                reopen_issue=True,
                comment=f"🔁 Reopened automatically because PR #{pr_number} is still failing CI.",
            )
            changed = True
        if pr_url in state and state[pr_url].get("attempts", 0) >= MAX_MERGE_ATTEMPTS:
            state.pop(pr_url, None)
            changed = True
    return changed


def _list_open_ci_remediation_issues(repo: str) -> list[dict]:
    try:
        issues = gh_json([
            "issue", "list", "-R", repo, "--state", "open",
            "--search", '"Fix CI failure on PR #"',
            "--json", "number,title,url",
            "--limit", "100",
        ]) or []
    except Exception:
        return []
    return [issue for issue in issues if _CI_REMEDIATION_RE.match(issue.get("title", ""))]


def _get_pr(repo: str, pr_number: int) -> dict | None:
    try:
        return gh_json([
            "pr", "view", str(pr_number), "-R", repo,
            "--json", "number,url,body,state,mergedAt",
        ])
    except Exception:
        return None


def _cleanup_stale_ci_remediation_issues(cfg: dict, repo: str, state: dict) -> bool:
    changed = False
    try:
        remediation_issues = gh_json([
            "issue", "list", "-R", repo, "--state", "all",
            "--search", '"Fix CI failure on PR #"',
            "--json", "number,title,body,state,labels,url",
            "--limit", "100",
        ]) or []
    except Exception:
        remediation_issues = []
    for issue in remediation_issues:
        if _CI_REMEDIATION_RE.match(issue.get("title", "")):
            _reconcile_issue_board_state(cfg, repo, issue)
    for issue in _list_followup_debug_issues(repo, state="all"):
        _reconcile_issue_board_state(cfg, repo, issue)
    for issue in _list_open_ci_remediation_issues(repo):
        match = _CI_REMEDIATION_RE.match(issue.get("title", ""))
        if not match:
            continue
        pr_number = int(match.group(1))
        pr = _get_pr(repo, pr_number)
        if not pr:
            continue
        if (pr.get("state") or "").upper() == "OPEN" and not pr.get("mergedAt"):
            continue

        print(f"{repo}: cleaning stale CI remediation issue #{issue['number']} for PR #{pr_number}")
        _cleanup_merged_pr_issues(cfg, repo, pr)
        pr_url = pr.get("url")
        if pr_url and pr_url in state:
            state.pop(pr_url, None)
            changed = True
    return changed


def _create_issue(repo: str, title: str, body: str, labels: list[str]) -> str:
    ensure_labels(repo, labels)
    cmd = ["issue", "create", "-R", repo, "--title", title, "--body", body]
    for label in labels:
        cmd += ["--label", label]
    return gh(cmd)


def _set_issue_ready(cfg: dict, repo: str, issue_url: str):
    owner = cfg.get("github_owner", "")
    _project_key, project_cfg, _repo_cfg = _find_repo_project(cfg, repo)
    if not owner or not project_cfg:
        return

    ready_value = project_cfg.get("ready_value", "Ready")
    try:
        raw = gh([
            "project", "item-add", str(project_cfg["project_number"]),
            "--owner", owner,
            "--url", issue_url,
            "--format", "json",
        ], check=False)
        if not raw:
            return
        item_data = json.loads(raw)
        item_id = item_data.get("id")
        if not item_id:
            return

        info = query_project(project_cfg["project_number"], owner)
        option_id = info["status_options"].get(ready_value)
        if info["status_field_id"] and option_id:
            set_item_status(info["project_id"], item_id, info["status_field_id"], option_id)
    except Exception as e:
        print(f"Warning: failed to set remediation issue ready for {repo}: {e}")


def _ensure_ci_remediation_issue(cfg: dict, repo: str, pr: dict, checks: list[dict], linked_issue_number: int | None) -> tuple[str | None, bool]:
    pr_number = pr["number"]
    branch = pr.get("headRefName", "").strip()
    pr_url = pr["url"]
    title = f"Fix CI failure on PR #{pr_number}"
    existing = _find_open_issue_by_title(repo, title)
    if existing:
        return existing.get("url"), False

    # Validate CI artifacts before creating debug task
    validation = validate_ci_artifacts(repo, checks)
    log_line = format_validation_log(validation, task_context=f"PR#{pr_number}")
    print(log_line)
    if not validation.valid:
        print(
            f"Skipping CI remediation issue for PR #{pr_number}: "
            f"{'; '.join(validation.errors)}"
        )
        return None, False

    check_lines = _format_failed_checks(checks)
    linked_issue_line = f"- Original issue: #{linked_issue_number}\n" if linked_issue_number else ""
    artifact_names = ", ".join(a.get("name", "?") for a in validation.artifacts)
    body = f"""## Goal
Repair the failing CI on PR #{pr_number} by updating its existing branch so the current pull request can merge cleanly.

## Success Criteria
- The failed checks on PR #{pr_number} are passing.
- Any required fixes are pushed to branch `{branch}`.
- Document the root cause and the fix in the task result.

## Constraints
- Work only inside this repository.
- Reuse the existing PR branch `{branch}` instead of opening a new feature branch.
- Prefer minimal diffs.

## Task Type
debugging

## Base Branch
{branch}

## Branch
{branch}

## Context
- PR: {pr_url}
{linked_issue_line}- Failed checks:
{check_lines}

## CI Artifacts
- Run ID: {validation.run_id}
- Artifacts: {artifact_names}
- Total size: {validation.total_bytes} bytes
"""
    labels = ["bug", "prio:high", "ready"]
    issue_url = _create_issue(repo, title, body, labels)
    _set_issue_ready(cfg, repo, issue_url)
    return issue_url, True


def _extract_issue_number(pr_body: str) -> int | None:
    m = re.search(r"#(\d+)", pr_body or "")
    return int(m.group(1)) if m else None


def _handle_ci_failure(cfg: dict, repo: str, pr: dict, checks: list[dict], attempt: int):
    issue_number = _extract_issue_number(pr.get("body", ""))
    pr_number = pr["number"]
    pr_url = pr["url"]
    escalated = attempt >= MAX_MERGE_ATTEMPTS

    check_lines = _format_failed_checks(checks)
    remediation_url = None
    remediation_created = False
    try:
        remediation_url, remediation_created = _ensure_ci_remediation_issue(cfg, repo, pr, checks, issue_number)
    except Exception as e:
        print(f"Warning: failed to create CI remediation issue for PR #{pr_number}: {e}")

    comment = f"""## Auto-merge blocked: CI failure

**PR:** {pr_url}
**Attempt:** {attempt}/{MAX_MERGE_ATTEMPTS}
{"**Status:** Escalated — max attempts reached, manual intervention required." if escalated else ""}
{f"**Remediation issue:** {remediation_url}" if remediation_url else ""}

### Failed checks
{redact_text(check_lines)}

### Next step
{"This PR has exceeded the maximum auto-merge attempts. Please review and merge manually." if escalated else "A debugging remediation task has been queued to repair this PR branch automatically."}
"""

    if issue_number:
        try:
            add_issue_comment(repo, issue_number, comment)
        except Exception as e:
            print(f"Warning: failed to post comment on #{issue_number}: {e}")

        try:
            edit_issue_labels(repo, issue_number, add=["in-progress", "agent-dispatched"], remove=["blocked", "ready", "done"])
        except Exception as e:
            print(f"Warning: failed to update labels on #{issue_number}: {e}")

        # Keep source issue in progress while its PR is still active.
        owner = cfg.get("github_owner", "")
        for project_cfg in cfg.get("github_projects", {}).values():
            for repo_cfg in project_cfg.get("repos", []):
                if repo_cfg.get("github_repo") != repo:
                    continue
                in_progress_value = project_cfg.get("in_progress_value", "In Progress")
                try:
                    info = query_project(project_cfg["project_number"], owner)
                    option_id = info["status_options"].get(in_progress_value)
                    if not info["status_field_id"] or not option_id:
                        print(f"Warning: status option '{in_progress_value}' not found in project")
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
                            print(f"Project status set to '{in_progress_value}' for #{issue_number}")
                            break
                    else:
                        print(f"Warning: issue #{issue_number} not found in project")
                except Exception as e:
                    print(f"Warning: failed to update project status: {e}")
                break

    token = str(cfg.get("telegram_bot_token", "")).strip()
    chat_id = str(cfg.get("telegram_chat_id", "")).strip()
    if token and chat_id:
        details = (
            f"⚠️ CI failure\nRepo: {repo}\nPR: {pr_number}\nAttempt: {attempt}/{MAX_MERGE_ATTEMPTS}\n"
            f"Escalated: {'yes' if escalated else 'no'}\n"
            f"Remediation issue: {remediation_url or 'not created'}"
            f"{' (created)' if remediation_created else ''}\n"
            f"Failed checks:\n{check_lines}"
        )
        try:
            subprocess.run(
                [
                    "curl",
                    "-sS",
                    "-X",
                    "POST",
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    "-d",
                    f"chat_id={chat_id}",
                    "--data-urlencode",
                    f"text={details}",
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except Exception as e:
            print(f"Warning: failed to send CI failure telegram for PR #{pr_number}: {e}")


def _try_merge(repo: str, pr_number: int) -> bool:
    """Attempt squash merge. Returns True on success."""
    try:
        gh(["pr", "merge", str(pr_number), "-R", repo, "--squash", "--delete-branch"])
        return True
    except Exception as e:
        print(f"Warning: merge failed for PR #{pr_number} in {repo}: {e}")
        return False


def _purge_stale_inbox_tasks(paths: dict, repo: str, pr: dict):
    """Move inbox tasks for a merged issue to done so they don't run stale."""
    import shutil
    import yaml
    issue_number = _extract_issue_number(pr.get("body", ""))
    if not issue_number:
        return
    inbox = Path(paths["INBOX"])
    done = Path(paths["DONE"])
    for task_file in inbox.glob("*.md"):
        try:
            text = task_file.read_text(encoding="utf-8")
            m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, flags=re.DOTALL)
            if not m:
                continue
            meta = yaml.safe_load(m.group(1)) or {}
            if str(meta.get("github_repo", "")) != repo:
                continue
            if int(meta.get("github_issue_number", 0)) != issue_number:
                continue
            shutil.move(str(task_file), str(done / task_file.name))
            print(f"  Purged stale inbox task {task_file.name} (issue #{issue_number} already merged)")
        except Exception:
            continue


def monitor_prs():
    cfg = load_config()
    paths = runtime_paths(cfg)
    state = _load_state(paths)

    repos: set[str] = set()
    for project_cfg in cfg.get("github_projects", {}).values():
        for repo_cfg in project_cfg.get("repos", []):
            r = repo_cfg.get("github_repo")
            if r and not is_dispatcher_only_repo(cfg, r):
                repos.add(r)

    if not repos:
        print("No repos configured in github_projects. Nothing to monitor.")
        return

    # Housekeeping: close PRs from forks
    _close_fork_prs(repos)

    stale_prs_closed = False
    for repo in sorted(repos):
        if _close_stale_redundant_agent_prs(repo):
            stale_prs_closed = True

    # Open PRs for agent branches that completed but have no PR yet
    _create_prs_for_orphan_branches(repos)

    # Housekeeping: resolve CI remediation issues for PRs that already merged/closed.
    state_changed = False
    for repo in sorted(repos):
        if _cleanup_stale_ci_remediation_issues(cfg, repo, state):
            state_changed = True
    if state_changed or stale_prs_closed:
        _save_state(paths, state)

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
            pr_state = state.setdefault(pr_url, {})
            attempts = pr_state.get("attempts", 0)

            if pr.get("isDraft"):
                print(f"  PR #{pr_number}: draft, skipping")
                continue

            checks = _get_pr_checks(repo, pr_number)
            print(f"  PR #{pr_number} '{pr_title}': {len(checks)} check(s)")
            if checks:
                if pr_state.pop("no_checks_polls", None) is not None:
                    _save_state(paths, state)
            if _reconcile_open_pr_state(cfg, repo, pr, checks, state):
                _save_state(paths, state)

            if not checks:
                no_checks_polls = pr_state.get("no_checks_polls", 0) + 1
                pr_state["no_checks_polls"] = no_checks_polls
                _save_state(paths, state)
                if no_checks_polls < 2:
                    print(f"  PR #{pr_number}: no checks reported yet, will retry next poll")
                    continue

                remediation_issue = _find_open_issue_by_title(repo, f"Fix CI failure on PR #{pr_number}")
                if remediation_issue:
                    print(f"  PR #{pr_number}: missing checks and remediation issue #{remediation_issue['number']} is active")
                    continue

                new_attempts = min(attempts + 1, MAX_MERGE_ATTEMPTS)
                pr_state["attempts"] = new_attempts
                _save_state(paths, state)
                print(f"  PR #{pr_number}: no checks reported after {no_checks_polls} polls (attempt {new_attempts}/{MAX_MERGE_ATTEMPTS})")
                _handle_ci_failure(cfg, repo, pr, _missing_checks_stub(), new_attempts)
                continue

            if _checks_any_failed(checks):
                remediation_issue = _find_open_issue_by_title(repo, f"Fix CI failure on PR #{pr_number}")
                if remediation_issue:
                    print(f"  PR #{pr_number}: CI failed and remediation issue #{remediation_issue['number']} is active")
                    continue
                new_attempts = min(attempts + 1, MAX_MERGE_ATTEMPTS)
                state.setdefault(pr_url, {})["attempts"] = new_attempts
                _save_state(paths, state)
                print(f"  PR #{pr_number}: CI failed (attempt {new_attempts}/{MAX_MERGE_ATTEMPTS})")
                _handle_ci_failure(cfg, repo, pr, checks, new_attempts)
                continue

            if not _checks_all_passed(checks):
                print(f"  PR #{pr_number}: checks pending, will retry next poll")
                continue

            if attempts >= MAX_MERGE_ATTEMPTS:
                print(f"  PR #{pr_number}: max merge attempts reached, skipping")
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
                _cleanup_merged_pr_issues(cfg, repo, pr)
                _purge_stale_inbox_tasks(paths, repo, pr)
            else:
                print(f"  PR #{pr_number}: merge failed")


if __name__ == "__main__":
    monitor_prs()
