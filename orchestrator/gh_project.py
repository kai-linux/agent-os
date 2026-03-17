import json
import subprocess
from typing import Optional


def gh(cmd: list[str], *, check: bool = True) -> str:
    result = subprocess.run(["gh", *cmd], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(cmd[:3])}... exit {result.returncode}: {result.stderr.strip()}")
    return result.stdout.strip()


def gh_json(cmd: list[str]):
    out = gh(cmd)
    return json.loads(out) if out else None


# ---------------------------------------------------------------------------
# GraphQL-based project query – single call returns project ID, status field
# with all options, and every item with its current Status value.
# ---------------------------------------------------------------------------

_PROJECT_QUERY = """
query($owner: String!, $number: Int!) {{
  {owner_type}(login: $owner) {{
    projectV2(number: $number) {{
      id
      fields(first: 20) {{
        nodes {{
          ... on ProjectV2SingleSelectField {{
            id
            name
            options {{ id name }}
          }}
        }}
      }}
      items(first: 100) {{
        nodes {{
          id
          fieldValueByName(name: "Status") {{
            ... on ProjectV2ItemFieldSingleSelectValue {{
              name
            }}
          }}
          content {{
            ... on Issue {{
              number
              title
              body
              url
              state
              labels(first: 20) {{
                nodes {{ name }}
              }}
              repository {{
                nameWithOwner
              }}
            }}
          }}
        }}
      }}
    }}
  }}
}}
"""


def query_project(project_number: int, owner: str) -> dict:
    """Fetch project metadata + all items in one GraphQL call.

    Returns dict with keys:
        project_id, status_field_id, status_options (name->id),
        items (list of dicts with item_id, status, number, title, body, url, state, labels, repo)
    """
    last_error = None
    for owner_type in ["user", "organization"]:
        query = _PROJECT_QUERY.format(owner_type=owner_type)
        try:
            raw = gh([
                "api", "graphql",
                "-f", f"query={query}",
                "-F", f"owner={owner}",
                "-F", f"number={project_number}",
            ])
            data = json.loads(raw)
            # Check for GraphQL errors
            if "errors" in data:
                last_error = data["errors"]
                continue
            project_data = data["data"][owner_type]["projectV2"]
            if project_data is None:
                last_error = f"{owner_type} '{owner}' has no projectV2 #{project_number}"
                continue
            break
        except Exception as e:
            last_error = e
            continue
    else:
        raise RuntimeError(f"Could not query project {project_number} for owner {owner}: {last_error}")

    # Find the "Status" field among all fields
    field_info = {}
    for f in (project_data.get("fields") or {}).get("nodes", []):
        if f.get("name") == "Status":
            field_info = f
            break
    status_options = {opt["name"]: opt["id"] for opt in field_info.get("options", [])}

    items = []
    for node in project_data["items"]["nodes"]:
        content = node.get("content")
        if not content or not content.get("number"):
            continue  # skip drafts / PRs without number

        fv = node.get("fieldValueByName")
        status_name = fv.get("name") if fv else None

        label_nodes = (content.get("labels") or {}).get("nodes", [])
        labels = {lbl["name"].lower() for lbl in label_nodes}

        items.append({
            "item_id": node["id"],
            "status": status_name,
            "number": content["number"],
            "title": content.get("title", ""),
            "body": content.get("body", ""),
            "url": content["url"],
            "state": content.get("state", "OPEN"),
            "labels": labels,
            "repo": content.get("repository", {}).get("nameWithOwner", ""),
        })

    return {
        "project_id": project_data["id"],
        "status_field_id": field_info.get("id"),
        "status_options": status_options,
        "items": items,
    }


def get_ready_items(project_number: int, owner: str, ready_value: str = "Ready") -> tuple[dict, list[dict]]:
    """Return (project_info, ready_items) where ready_items have Status=ready_value and are OPEN."""
    info = query_project(project_number, owner)
    ready = [
        item for item in info["items"]
        if item["status"] == ready_value and item["state"] == "OPEN"
    ]
    return info, ready


def set_item_status(project_id: str, item_id: str, field_id: str, option_id: str):
    """Update a project item's Status field using IDs obtained from query_project."""
    gh([
        "project", "item-edit",
        "--id", item_id,
        "--project-id", project_id,
        "--field-id", field_id,
        "--single-select-option-id", option_id,
    ])


# ---------------------------------------------------------------------------
# Issue helpers (unchanged)
# ---------------------------------------------------------------------------

def list_ready_issues(repo: str, limit: int = 20):
    return gh_json([
        "issue", "list", "-R", repo,
        "--limit", str(limit),
        "--search", "is:open label:ready",
        "--json", "number,title,body,labels,url,updatedAt",
    ]) or []


def get_issue(repo: str, number: int):
    return gh_json([
        "issue", "view", str(number), "-R", repo,
        "--json", "number,title,body,labels,url,updatedAt,comments",
    ])


def add_issue_comment(repo: str, number: int, body: str):
    gh(["issue", "comment", str(number), "-R", repo, "--body", body])


def ensure_labels(repo: str, labels: list[str]):
    """Create labels on the repo if they don't already exist."""
    for label in labels:
        gh(["label", "create", label, "-R", repo, "--force"], check=False)


def edit_issue_labels(repo: str, number: int, add=None, remove=None):
    if add:
        ensure_labels(repo, add)
    cmd = ["issue", "edit", str(number), "-R", repo]
    if add:
        cmd += ["--add-label", ",".join(add)]
    if remove:
        cmd += ["--remove-label", ",".join(remove)]
    try:
        gh(cmd)
    except Exception as e:
        print(f"Warning: label update failed for #{number}: {e}")


def create_pr_for_branch(repo: str, branch: str, title: str, body: str) -> Optional[str]:
    try:
        out = gh_json([
            "pr", "create", "-R", repo,
            "--head", branch,
            "--title", title,
            "--body", body,
            "--json", "url,number",
        ])
        return out["url"] if out else None
    except Exception:
        return None
