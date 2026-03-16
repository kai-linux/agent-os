import json
import subprocess
from typing import Optional


def gh(cmd: list[str], *, check: bool = True) -> str:
    result = subprocess.run(["gh", *cmd], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"gh command failed: {' '.join(cmd)}\n{result.stderr}")
    return result.stdout.strip()


def gh_json(cmd: list[str]):
    out = gh(cmd)
    return json.loads(out) if out else None


def list_ready_issues(repo: str, limit: int = 20):
    return gh_json(
        [
            "issue",
            "list",
            "-R",
            repo,
            "--limit",
            str(limit),
            "--search",
            "is:open label:ready",
            "--json",
            "number,title,body,labels,url,updatedAt",
        ]
    ) or []


def get_issue(repo: str, number: int):
    return gh_json(
        [
            "issue",
            "view",
            str(number),
            "-R",
            repo,
            "--json",
            "number,title,body,labels,url,updatedAt,comments",
        ]
    )


def add_issue_comment(repo: str, number: int, body: str):
    gh(["issue", "comment", str(number), "-R", repo, "--body", body])


def edit_issue_labels(repo: str, number: int, add=None, remove=None):
    cmd = ["issue", "edit", str(number), "-R", repo]
    if add:
        cmd += ["--add-label", ",".join(add)]
    if remove:
        cmd += ["--remove-label", ",".join(remove)]
    gh(cmd)


def create_pr_for_branch(repo: str, branch: str, title: str, body: str) -> Optional[str]:
    try:
        out = gh_json(
            [
                "pr",
                "create",
                "-R",
                repo,
                "--head",
                branch,
                "--title",
                title,
                "--body",
                body,
                "--json",
                "url,number",
            ]
        )
        return out["url"] if out else None
    except Exception:
        return None


def field_list(project_number: int, owner: str):
    return gh_json(
        [
            "project",
            "field-list",
            str(project_number),
            "--owner",
            owner,
            "--format",
            "json",
        ]
    ) or []


def item_list(project_number: int, owner: str, query: Optional[str] = None):
    cmd = [
        "project",
        "item-list",
        str(project_number),
        "--owner",
        owner,
        "--format",
        "json",
    ]
    if query:
        cmd += ["--query", query]
    return gh_json(cmd) or []


def find_project_item_for_issue(project_number: int, owner: str, issue_url: str):
    items = item_list(project_number, owner)
    for item in items:
        content = item.get("content") or {}
        if content.get("url") == issue_url:
            return item
    return None


def get_status_field_and_option(project_number: int, owner: str, field_name: str, option_name: str):
    fields = field_list(project_number, owner)
    for field in fields:
        if field.get("name") != field_name:
            continue
        field_id = field.get("id")
        for opt in field.get("options", []):
            if opt.get("name") == option_name:
                return field_id, opt.get("id")
    raise RuntimeError(f"Could not resolve project field '{field_name}' option '{option_name}'")


def set_project_status(project_number: int, owner: str, project_id: str, item_id: str, field_id: str, option_id: str):
    gh(
        [
            "project",
            "item-edit",
            "--id",
            item_id,
            "--project-id",
            project_id,
            "--field-id",
            field_id,
            "--single-select-option-id",
            option_id,
        ]
    )
