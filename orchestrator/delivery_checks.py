"""Independent, bounded observations of operator-defined acceptance checks."""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import re
import socket
import ssl
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from orchestrator.gh_project import gh_json

MAX_OBSERVATION_BYTES = 2 * 1024 * 1024


def fetch_public(url: str, *, limit=MAX_OBSERVATION_BYTES):
    """Pin validated DNS addresses; do not follow redirects to unchecked targets."""
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("An unauthenticated HTTP(S) target is required")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    if not addresses or any(
        not ipaddress.ip_address(a[4][0]).is_global for a in addresses
    ):
        raise ValueError(
            "Private, loopback and link-local observations are not permitted"
        )
    sock = socket.create_connection((addresses[0][4][0], port), timeout=10)
    conn = http.client.HTTPConnection(parsed.hostname, port, timeout=10)
    try:
        conn.sock = (
            ssl.create_default_context().wrap_socket(
                sock, server_hostname=parsed.hostname
            )
            if parsed.scheme == "https"
            else sock
        )
        conn.request(
            "GET",
            (parsed.path or "/") + ("?" + parsed.query if parsed.query else ""),
            headers={
                "User-Agent": "Agent-OS-Delivery-Verifier/1.0",
                "Accept-Encoding": "identity",
            },
        )
        response = conn.getresponse()
        data = response.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Observation exceeds the configured read limit")
        return response.status, response.getheader("Content-Type", ""), data
    finally:
        conn.close()
        sock.close()


def _content_checks(check, data):
    if len(data) < int(check.get("min_bytes", 1)):
        return False
    if check.get("sha256") and hashlib.sha256(data).hexdigest() != check["sha256"]:
        return False
    contains = check.get("contains", [])
    if not isinstance(contains, list) or any(not isinstance(s, str) for s in contains):
        raise ValueError("contains must be a list of required strings")
    text = data.decode("utf-8", errors="replace")
    return all(s in text for s in contains)


def observe(check, goal, cfg):
    kind = check["type"]
    if kind == "human":
        return None
    if kind == "merged_pr":
        repo = goal["metadata"].get("github_repo")
        if check.get("repo", repo) != repo:
            raise ValueError(
                "Merged PR must belong to the declared delivery repository"
            )
        number = int(check.get("issue", goal["metadata"]["github_issue_number"]))
        prs = (
            gh_json(
                [
                    "pr",
                    "list",
                    "-R",
                    repo,
                    "--state",
                    "merged",
                    "--search",
                    f"#{number}",
                    "--limit",
                    "100",
                    "--json",
                    "number,url,mergedAt,mergeCommit,body,baseRefName,closingIssuesReferences",
                ]
            )
            or []
        )
        for pr in prs:
            closing = {
                int(item["number"]) for item in pr.get("closingIssuesReferences", [])
            }
            if not closing:
                closing = {
                    int(n)
                    for n in re.findall(
                        r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)\b",
                        pr.get("body", ""),
                    )
                }
            if (
                number in closing
                and pr.get("mergedAt")
                and (pr.get("mergeCommit") or {}).get("oid")
            ):
                expected_base = check.get(
                    "base_branch", cfg.get("default_base_branch", "main")
                )
                if pr.get("baseRefName") != expected_base:
                    continue
                return True, {
                    "type": kind,
                    "url": pr["url"],
                    "commit": pr["mergeCommit"]["oid"],
                    "merged_at": pr["mergedAt"],
                }
        return False, {
            "type": kind,
            "reason": "No merged PR explicitly closes this issue on the target branch",
        }
    if kind == "file":
        workspace = Path(
            goal["metadata"].get("worktree") or goal["metadata"]["workspace"]
        ).resolve()
        relative = str(check.get("path", ""))
        path = (workspace / relative).resolve()
        if not relative or not path.is_relative_to(workspace) or not path.is_file():
            return False, {
                "type": kind,
                "reason": "Expected artifact is missing or outside the workspace",
            }
        with path.open("rb") as handle:
            data = handle.read(MAX_OBSERVATION_BYTES + 1)
        if len(data) > MAX_OBSERVATION_BYTES:
            raise ValueError(
                "Use a dedicated configured verifier for large or binary media"
            )
        passed = _content_checks(check, data)
        detail = {
            "type": kind,
            "path": relative,
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
        }
        if passed:
            # Preserve deliverables before ephemeral worktree cleanup. Do not
            # copy arbitrary workspace files or secrets into public reports.
            output = (
                Path(cfg["root_dir"])
                / "runtime"
                / "delivery"
                / "artifacts"
                / goal["id"]
                / str(goal["revision"])
            )
            output.mkdir(parents=True, exist_ok=True, mode=0o700)
            artifact = output / (
                hashlib.sha256(relative.encode()).hexdigest()[:12] + "-" + path.name
            )
            artifact.write_bytes(data)
            artifact.chmod(0o600)
            detail["artifact"] = str(artifact)
        return passed, detail
    if kind == "url":
        url = str(check.get("url", ""))
        if url not in goal["contract"].get("targets", []):
            raise ValueError("URL evidence must match an explicitly declared target")
        status, content_type, data = fetch_public(url)
        passed = status == int(check.get("status", 200)) and _content_checks(
            check, data
        )
        if check.get("content_type"):
            passed = passed and content_type.startswith(str(check["content_type"]))
        return passed, {
            "type": kind,
            "url": url,
            "status": status,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    if kind == "configured_command":
        name = str(check.get("name", ""))
        command = (cfg.get("delivery_verifiers") or {}).get(name)
        if (
            not isinstance(command, dict)
            or not isinstance(command.get("argv"), list)
            or not command["argv"]
        ):
            raise ValueError("Verifier is not configured by the operator")
        repo = goal["metadata"].get("github_repo")
        if repo not in command.get("repos", []):
            raise ValueError("Verifier is not allowed for this workspace")
        result = subprocess.run(
            command["argv"],
            cwd=goal["metadata"].get("worktree") or goal["metadata"]["workspace"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=min(300, int(command.get("timeout_seconds", 60))),
            check=False,
        )
        return result.returncode == 0, {
            "type": kind,
            "name": name,
            "returncode": result.returncode,
        }
    raise ValueError("Unknown verification method")


def verify_goal(store, ident, cfg):
    goal = store.get(ident)
    if goal["state"] in {"succeeded", "failed", "cancelled", "paused", "backlog"}:
        return goal["state"] == "succeeded"
    if any(
        g["state"]
        in {"succeeded", "failed", "cancelled", "paused", "waiting", "backlog"}
        for g in store.context(ident)["lineage"][1:]
    ):
        return False
    for check in goal["contract"].get("checks", []):
        try:
            observation = observe(check, goal, cfg)
            if observation is None:
                continue
            passed, detail = observation
        except Exception as exc:
            passed, detail = (
                False,
                {"type": check.get("type"), "reason": type(exc).__name__},
            )
        store.record_evidence(
            ident,
            goal["revision"],
            check["id"],
            passed,
            "independent:" + str(check["type"]),
            detail,
        )
    return store.verify(ident)
