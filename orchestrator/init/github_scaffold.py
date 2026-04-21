from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from orchestrator.init import ui
from orchestrator.init.state import State, slugify_repo_name


REPO_RE = re.compile(r"^[a-z0-9][a-z0-9-_]{0,99}$")
STOPWORDS = {"a", "an", "the", "for", "with", "and", "or", "to", "of", "my", "me", "app", "site", "tool", "project"}


class GithubError(RuntimeError):
    pass


def run_cmd(cmd: list[str], *, cwd: Path | None = None, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise GithubError(f"{' '.join(cmd)} failed: {stderr}")
    return result


def gh_json(args: list[str], *, cwd: Path | None = None, timeout: int = 30):
    result = run_cmd(["gh", *args], cwd=cwd, timeout=timeout)
    text = result.stdout.strip()
    return json.loads(text) if text else None


def gh_run(args: list[str], *, cwd: Path | None = None, timeout: int = 30) -> str:
    return run_cmd(["gh", *args], cwd=cwd, timeout=timeout).stdout.strip()


def validate_repo_name(name: str) -> str:
    value = name.strip()
    if not REPO_RE.fullmatch(value):
        raise ValueError("Repo name must be lowercase letters, numbers, hyphens, or underscores.")
    return value


def suggest_repo_name(intake: dict[str, str] | None = None) -> str:
    if not intake:
        return "new-project"
    words = re.findall(r"[a-z0-9]+", intake.get("idea", "").lower())
    filtered = [word for word in words if word not in STOPWORDS]
    if not filtered:
        filtered = words
    candidate = "-".join(filtered[:4]).strip("-")
    if not candidate:
        candidate = "new-project"
    candidate = slugify_repo_name(candidate)
    return candidate or "new-project"


def _default_owner() -> str:
    return gh_run(["api", "user", "--jq", ".login"]).strip()


def _ensure_repo_initialized(local_path: Path, repo_name: str) -> None:
    readme = local_path / "README.md"
    gitignore = local_path / ".gitignore"
    if not readme.exists():
        readme.write_text(f"# {repo_name}\n", encoding="utf-8")
    if not gitignore.exists():
        gitignore.write_text(
            "\n".join(
                [
                    ".DS_Store",
                    "__pycache__/",
                    ".venv/",
                    "node_modules/",
                    "dist/",
                    "build/",
                    ".env",
                    "",
                ]
            ),
            encoding="utf-8",
        )
    status = subprocess.run(["git", "status", "--short"], cwd=local_path, capture_output=True, text=True, check=False)
    if not status.stdout.strip():
        return
    run_cmd(["git", "add", "README.md", ".gitignore"], cwd=local_path)
    run_cmd(["git", "commit", "-m", "initial commit"], cwd=local_path)
    run_cmd(["git", "push", "-u", "origin", "main"], cwd=local_path)


def _prompt_repo_inputs(existing: dict | None = None, *, intake: dict[str, str] | None = None) -> dict[str, str]:
    if existing:
        return existing

    print("  [1] Create a new GitHub repo  [2] Use an existing empty repo I already created")
    mode = ui.choice("", ["1", "2"], default="1")

    owner_default = _default_owner()
    owner = ui.prompt("GitHub owner", default=owner_default)
    suggested_name = suggest_repo_name(intake)
    while True:
        repo_name = ui.prompt("Repo name", default=suggested_name)
        try:
            repo_name = validate_repo_name(repo_name)
            break
        except ValueError as exc:
            ui.warn(str(exc))
    visibility = ui.choice("Visibility: [public/private]", ["public", "private"], default="private")
    default_clone = str((Path.home() / "projects" / repo_name).expanduser())
    local_clone_path = str(Path(ui.prompt("Clone to", default=default_clone)).expanduser())
    return {
        "mode": "create" if mode == "1" else "existing",
        "owner": owner.strip(),
        "repo_name": repo_name,
        "visibility": visibility,
        "local_clone_path": local_clone_path,
    }


def _create_or_clone_repo(inputs: dict[str, str]) -> tuple[str, Path]:
    repo_full_name = f"{inputs['owner']}/{inputs['repo_name']}"
    local_path = Path(inputs["local_clone_path"]).expanduser()
    if inputs["mode"] == "create":
        local_path.parent.mkdir(parents=True, exist_ok=True)
        gh_run(
            [
                "repo",
                "create",
                repo_full_name,
                f"--{inputs['visibility']}",
                "--clone",
            ],
            cwd=local_path.parent,
            timeout=120,
        )
        cloned_path = local_path.parent / inputs["repo_name"]
        if cloned_path != local_path and cloned_path.exists() and not local_path.exists():
            cloned_path.rename(local_path)
    else:
        gh_run(["api", f"repos/{repo_full_name}"])
        if not local_path.exists():
            local_path.parent.mkdir(parents=True, exist_ok=True)
            gh_run(["repo", "clone", repo_full_name, str(local_path)], cwd=local_path.parent, timeout=120)
    return repo_full_name, local_path


def _project_by_title(owner: str, title: str) -> dict | None:
    listing = gh_json(["project", "list", "--owner", owner, "--format", "json"], timeout=60)
    projects = listing.get("projects", listing) if isinstance(listing, dict) else listing
    for project in projects or []:
        if project.get("title") == title:
            return project
    return None


def _ensure_project(owner: str, repo_name: str) -> dict[str, str | int]:
    existing = _project_by_title(owner, repo_name)
    if existing:
        return {
            "project_id": existing["id"],
            "project_number": existing["number"],
            "project_url": existing["url"],
        }
    project = gh_json(["project", "create", "--owner", owner, "--title", repo_name, "--format", "json"], timeout=60)
    return {
        "project_id": project["id"],
        "project_number": project["number"],
        "project_url": project["url"],
    }


def _ensure_status_field(owner: str, project_number: int) -> tuple[str, dict[str, str]]:
    fields = gh_json(["project", "field-list", str(project_number), "--owner", owner, "--format", "json"], timeout=60)
    for field in fields.get("fields", fields) if isinstance(fields, dict) else fields:
        if field.get("name") == "Status" and field.get("dataType") == "SINGLE_SELECT":
            options = {opt["name"]: opt["id"] for opt in field.get("options", [])}
            required = ["Ready", "In Progress", "Blocked", "Done"]
            if all(name in options for name in required):
                return field["id"], {name: options[name] for name in required}
    created = gh_json(
        [
            "project",
            "field-create",
            str(project_number),
            "--owner",
            owner,
            "--name",
            "Status",
            "--data-type",
            "SINGLE_SELECT",
            "--single-select-options",
            "Ready,In Progress,Blocked,Done",
            "--format",
            "json",
        ],
        timeout=60,
    )
    options = {opt["name"]: opt["id"] for opt in created.get("options", [])}
    return created["id"], {name: options[name] for name in ["Ready", "In Progress", "Blocked", "Done"]}


def _ensure_label(repo_full_name: str, label: str, description: str, color: str) -> None:
    subprocess.run(
        ["gh", "label", "create", label, "--repo", repo_full_name, "--description", description, "--color", color, "--force"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def run(state: State, intake: dict[str, str] | None = None, *, dry_run: bool = False) -> dict:
    github = state.get("github", {})
    prompt_inputs = _prompt_repo_inputs(github.get("inputs"), intake=intake)
    slug = slugify_repo_name(prompt_inputs["repo_name"])
    expected_path = state.path.parent / f"{slug}.json"
    if state.path.name != expected_path.name and not state.path.exists():
        state.path = expected_path
    state.merge("github.inputs", prompt_inputs)

    if state.get("github.project_number"):
        return state.get("github", {})

    repo_full_name = f"{prompt_inputs['owner']}/{prompt_inputs['repo_name']}"
    local_path = Path(prompt_inputs["local_clone_path"]).expanduser()
    repo_url = f"https://github.com/{repo_full_name}"

    if dry_run:
        project = {"project_id": "PVT_DRYRUN", "project_number": 1, "project_url": f"https://github.com/users/{prompt_inputs['owner']}/projects/1"}
        status_field_id = "PVTSSF_DRYRUN"
        option_ids = {"Ready": "opt_ready", "In Progress": "opt_progress", "Blocked": "opt_blocked", "Done": "opt_done"}
    else:
        repo_full_name, local_path = _create_or_clone_repo(prompt_inputs)
        _ensure_repo_initialized(local_path, prompt_inputs["repo_name"])
        project = _ensure_project(prompt_inputs["owner"], prompt_inputs["repo_name"])
        status_field_id, option_ids = _ensure_status_field(prompt_inputs["owner"], int(project["project_number"]))
        gh_run(["project", "link", str(project["project_number"]), "--owner", prompt_inputs["owner"], "--repo", repo_full_name], timeout=60)
        _ensure_label(repo_full_name, "ready", "Dispatch-ready", "0E8A16")

    github_block = {
        "owner": prompt_inputs["owner"],
        "repo_name": prompt_inputs["repo_name"],
        "repo_full_name": repo_full_name,
        "repo_url": repo_url,
        "local_clone_path": str(local_path),
        "visibility": prompt_inputs["visibility"],
        "project_id": project["project_id"],
        "project_number": int(project["project_number"]),
        "project_url": project["project_url"],
        "status_field_id": status_field_id,
        "status_option_ids": option_ids,
        "inputs": prompt_inputs,
    }
    state.mark("github", github_block)
    return github_block
