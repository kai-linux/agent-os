from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from orchestrator.init import ui
from orchestrator.init.github_scaffold import gh_json, gh_run
from orchestrator.init.state import State


class CharterError(RuntimeError):
    pass


def build_prompt(intake: dict[str, str]) -> str:
    return f"""You are the architect agent for Agent OS, bootstrapping a new project.

The operator just answered these questions:

IDEA:
{intake["idea"]}

KIND OF PROJECT:
{intake["kind"]}  (one of: web, mobile, game, api, cli, desktop, other)

STACK PREFERENCE:
{intake["stack_preference"]}  (may be "auto")

USER + SUCCESS CRITERIA:
{intake["success_criteria"]}

Your job: propose a stack and the first 3-5 seed issues that will take this project from empty repo to first deployable vertical slice. Optimize for:
- Solo-operator friendliness (no heavy infra)
- Thin first slice over complete scaffolding (so the stack choice is revisable)
- Issues the existing Agent OS agents (claude/codex/gemini/deepseek) can complete in 10-40 minutes each

The operator may be non-technical, unsure, or relying on defaults. You must handle vague input gracefully:
- If the project kind is unclear or set to `other`, infer the most plausible product shape from the idea and success criteria.
- If the request is underspecified, make the minimum necessary assumptions and say what you inferred in the rationale.
- Prefer boring, easy-to-run stacks over ambitious ones when the operator is undecided.
- Distinguish between a static site, interactive web app, mobile app, game, API, or multimodal AI product when the idea implies one.
- Default to a simple web-first product only when another modality is not clearly required.

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
"""


def strip_code_fences(text: str) -> str:
    value = text.strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    return value


def parse_charter_response(text: str) -> dict[str, Any]:
    cleaned = strip_code_fences(text)
    payload = json.loads(cleaned)
    validate_charter_payload(payload)
    return payload


def validate_charter_payload(payload: dict[str, Any]) -> None:
    if not payload.get("stack_decision"):
        raise CharterError("Missing stack_decision")
    if not payload.get("north_star_md"):
        raise CharterError("Missing north_star_md")
    issues = payload.get("seed_issues")
    if not isinstance(issues, list) or not 3 <= len(issues) <= 5:
        raise CharterError("seed_issues must contain 3-5 issues")
    for issue in issues:
        if issue.get("priority") not in {"prio:high", "prio:normal", "prio:low"}:
            raise CharterError(f"Invalid priority: {issue.get('priority')}")
        for field in ("title", "goal", "success_criteria", "constraints"):
            if not issue.get(field):
                raise CharterError(f"Missing issue field: {field}")


def call_claude(prompt: str) -> str:
    result = subprocess.run(
        ["claude", "-p", prompt],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise CharterError(f"claude call failed: {stderr}")
    return result.stdout.strip()


def _edit_payload(payload: dict[str, Any]) -> dict[str, Any]:
    editor = os.environ.get("EDITOR", "vi")
    with tempfile.NamedTemporaryFile("w+", suffix=".json", delete=False, encoding="utf-8") as handle:
        temp_path = Path(handle.name)
        json.dump(payload, handle, indent=2)
        handle.write("\n")
    try:
        subprocess.run([editor, str(temp_path)], check=False)
        edited = json.loads(temp_path.read_text(encoding="utf-8"))
        validate_charter_payload(edited)
        return edited
    finally:
        temp_path.unlink(missing_ok=True)


def _preview(payload: dict[str, Any]) -> None:
    print("Proposed plan:")
    print(f"  Stack: {payload['stack_decision']}")
    print(f"  Rationale: {payload['stack_rationale']}")
    print("  Seed issues:")
    for idx, issue in enumerate(payload["seed_issues"], start=1):
        print(f"    #{idx}  {issue['title']}")


def _confirm_payload(initial_payload: dict[str, Any], intake: dict[str, str]) -> dict[str, Any]:
    payload = initial_payload
    regen_count = 0
    while True:
        _preview(payload)
        print("\n  [1] Looks good, proceed  [2] Regenerate  [3] Edit manually  [4] Abort")
        choice = ui.choice("", ["1", "2", "3", "4"], default="1")
        if choice == "1":
            return payload
        if choice == "2":
            if regen_count >= 1:
                ui.warn("Automatic regeneration is capped at one retry.")
                continue
            prompt = build_prompt(intake) + f"\nThe last attempt proposed this stack: {payload['stack_decision']}. Try a meaningfully different angle."
            payload = parse_charter_response(call_claude(prompt))
            regen_count += 1
            continue
        if choice == "3":
            payload = _edit_payload(payload)
            continue
        raise CharterError("Aborted by user")


def _build_issue_body(issue: dict[str, Any]) -> str:
    sc = "\n".join(f"- {item}" for item in issue["success_criteria"])
    constraints = "\n".join(f"- {item}" for item in issue["constraints"])
    return f"## Goal\n\n{issue['goal']}\n\n## Success Criteria\n\n{sc}\n\n## Constraints\n\n{constraints}\n"


def _ensure_priority_labels(repo_full_name: str) -> None:
    labels = {
        "prio:high": ("B60205", "High priority"),
        "prio:normal": ("FBCA04", "Normal priority"),
        "prio:low": ("0E8A16", "Low priority"),
    }
    for label, (color, description) in labels.items():
        subprocess.run(
            ["gh", "label", "create", label, "--repo", repo_full_name, "--description", description, "--color", color, "--force"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )


def _commit_charter(local_path: Path, markdown_body: str, *, dry_run: bool) -> str:
    target = local_path / "NORTH_STAR.md"
    target.write_text(markdown_body.rstrip() + "\n", encoding="utf-8")
    if dry_run:
        return "DRYRUN"
    subprocess.run(["git", "add", "NORTH_STAR.md"], cwd=local_path, check=True)
    subprocess.run(["git", "commit", "-m", "NORTH_STAR: scaffold charter and seed backlog"], cwd=local_path, check=True)
    subprocess.run(["git", "push"], cwd=local_path, check=True)
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=local_path, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _create_seed_issues(payload: dict[str, Any], github: dict[str, Any], *, dry_run: bool) -> list[dict[str, Any]]:
    repo_full_name = github["repo_full_name"]
    _ensure_priority_labels(repo_full_name)
    created: list[dict[str, Any]] = []
    if dry_run:
        for idx, issue in enumerate(payload["seed_issues"], start=1):
            created.append({"number": idx, "title": issue["title"], "item_id": f"PVTI_DRYRUN_{idx}"})
        return created

    for issue in payload["seed_issues"]:
        issue_url = gh_run(
            [
                "issue",
                "create",
                "--repo",
                repo_full_name,
                "--title",
                issue["title"],
                "--body",
                _build_issue_body(issue),
                "--label",
                "ready",
                "--label",
                issue["priority"],
            ],
            timeout=60,
        ).strip()
        issue_number = int(issue_url.rstrip("/").rsplit("/", 1)[-1])
        item_json = gh_json(
            [
                "project",
                "item-add",
                str(github["project_number"]),
                "--owner",
                github["owner"],
                "--url",
                issue_url,
                "--format",
                "json",
            ],
            timeout=60,
        )
        gh_run(
            [
                "project",
                "item-edit",
                "--id",
                item_json["id"],
                "--project-id",
                github["project_id"],
                "--field-id",
                github["status_field_id"],
                "--single-select-option-id",
                github["status_option_ids"]["Ready"],
            ],
            timeout=60,
        )
        created.append({"number": issue_number, "title": issue["title"], "item_id": item_json["id"]})
    return created


def run(state: State, intake: dict[str, str], github: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
    if state.get("charter.committed_sha") and state.get("issues_created"):
        return state.get("charter", {})

    charter_data = state.get("charter")
    if not charter_data or "seed_issues" not in charter_data:
        ui.info("Asking claude to propose a stack and draft the first issues...")
        payload = parse_charter_response(call_claude(build_prompt(intake)))
        payload = _confirm_payload(payload, intake)
        state.mark("charter", payload)
        charter_data = payload

    local_path = Path(github["local_clone_path"]).expanduser()
    if not state.get("charter.committed_sha"):
        sha = _commit_charter(local_path, charter_data["north_star_md"], dry_run=dry_run)
        state.merge("charter", {"committed_sha": sha})

    if not state.get("issues_created"):
        created = _create_seed_issues(charter_data, github, dry_run=dry_run)
        state.mark("issues_created", created)

    return state.get("charter", {})
