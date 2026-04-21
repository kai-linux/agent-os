"""Dependency and CVE watcher with bounded auto-remediation."""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orchestrator.commit_signature import with_agent_os_trailer
from orchestrator.gh_project import create_pr_for_branch, ensure_labels, gh, gh_json
from orchestrator.paths import load_config
from orchestrator.repo_modes import is_dispatcher_only_repo
from orchestrator.scheduler_state import is_due, job_lock, record_run

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python <3.11 fallback
    tomllib = None


WATCHER_JOB_NAME = "dependency_watcher"
DEFAULT_CADENCE_DAYS = 7.0
DEFAULT_MAX_ACTIONS_PER_WEEK = 3
LOW_RISK_PR_LABELS = ["task:implementation", "prio:normal", "tech-debt"]
HIGH_RISK_ISSUE_LABELS = ["bug", "task:debugging", "prio:high"]
SUPPORTED_LOCKFILES = (
    "requirements.txt",
    "pyproject.toml",
    "package.json",
    "package-lock.json",
    "go.sum",
)
VERSION_RE = re.compile(r"\d+")


@dataclass
class Manifest:
    ecosystem: str
    path: Path


@dataclass
class Finding:
    kind: str
    ecosystem: str
    manifest_path: str
    dependency: str
    current_version: str = ""
    target_version: str = ""
    update_type: str = "unknown"
    dev_only: bool = False
    runtime: bool = True
    cve_ids: list[str] = field(default_factory=list)
    severity: str = ""
    affected_versions: str = ""
    patched_version: str = ""
    advisory_urls: list[str] = field(default_factory=list)
    summary: str = ""
    scanner: str = ""


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _watcher_state_path(cfg: dict) -> Path:
    root = Path(cfg.get("root_dir", ".")).expanduser()
    path = root / "runtime" / "state" / "dependency_watcher_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_watcher_state(cfg: dict) -> dict[str, Any]:
    path = _watcher_state_path(cfg)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_watcher_state(cfg: dict, state: dict[str, Any]) -> None:
    _watcher_state_path(cfg).write_text(
        json.dumps(state, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _week_key(now: datetime | None = None) -> str:
    current = now or _now_utc()
    iso = current.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _repo_watcher_config(cfg: dict, github_slug: str) -> dict[str, Any]:
    merged = dict(cfg.get("dependency_watcher") or {})
    merged.setdefault("enabled", True)
    merged.setdefault("cadence_days", DEFAULT_CADENCE_DAYS)
    merged.setdefault("max_actions_per_week", DEFAULT_MAX_ACTIONS_PER_WEEK)

    for project_cfg in (cfg.get("github_projects") or {}).values():
        if not isinstance(project_cfg, dict):
            continue
        for repo_cfg in project_cfg.get("repos", []) or []:
            if repo_cfg.get("github_repo") != github_slug:
                continue
            override = repo_cfg.get("dependency_watcher")
            if isinstance(override, dict):
                updated = dict(merged)
                updated.update(override)
                merged = updated
            return merged
    return merged


def _action_budget_remaining(cfg: dict, github_slug: str, max_actions: int, now: datetime | None = None) -> int:
    current_week = _week_key(now)
    repo_state = _load_watcher_state(cfg).get(github_slug) or {}
    if repo_state.get("week") != current_week:
        return max_actions
    return max(0, int(max_actions) - int(repo_state.get("actions_created", 0)))


def _record_created_action(cfg: dict, github_slug: str, now: datetime | None = None) -> None:
    current_week = _week_key(now)
    state = _load_watcher_state(cfg)
    repo_state = state.get(github_slug) or {}
    if repo_state.get("week") != current_week:
        repo_state = {"week": current_week, "actions_created": 0}
    repo_state["actions_created"] = int(repo_state.get("actions_created", 0)) + 1
    state[github_slug] = repo_state
    _save_watcher_state(cfg, state)


def _resolve_repos(cfg: dict) -> list[tuple[str, Path]]:
    repos: list[tuple[str, Path]] = []
    seen: set[tuple[str, str]] = set()
    for project_cfg in (cfg.get("github_projects") or {}).values():
        if not isinstance(project_cfg, dict):
            continue
        for repo_cfg in project_cfg.get("repos", []) or []:
            github_slug = str(repo_cfg.get("github_repo") or "").strip()
            local_repo = str(repo_cfg.get("local_repo") or repo_cfg.get("path") or "").strip()
            if not github_slug or not local_repo:
                continue
            key = (github_slug, local_repo)
            if key in seen:
                continue
            seen.add(key)
            repos.append((github_slug, Path(local_repo).expanduser()))
    return repos


def _run_json_command(cmd: list[str], cwd: Path) -> dict[str, Any] | list[Any] | None:
    result = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True)
    if result.returncode != 0:
        return None
    stdout = (result.stdout or "").strip()
    if not stdout:
        return None
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return None


def _safe_read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _safe_read_toml(path: Path) -> dict[str, Any]:
    if tomllib is None:
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def detect_manifests(repo_path: Path) -> list[Manifest]:
    manifests: list[Manifest] = []
    for name in SUPPORTED_LOCKFILES:
        path = repo_path / name
        if not path.exists():
            continue
        if name in {"requirements.txt", "pyproject.toml"}:
            ecosystem = "python"
        elif name in {"package.json", "package-lock.json"}:
            ecosystem = "npm"
        elif name == "go.sum":
            ecosystem = "go"
        else:
            continue
        manifests.append(Manifest(ecosystem=ecosystem, path=path))
    deduped: list[Manifest] = []
    seen: set[tuple[str, str]] = set()
    for manifest in manifests:
        key = (manifest.ecosystem, manifest.path.name)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(manifest)
    return deduped


def _npm_dependency_sections(repo_path: Path) -> tuple[dict[str, str], dict[str, str]]:
    data = _safe_read_json(repo_path / "package.json")
    return (
        dict(data.get("dependencies") or {}),
        dict(data.get("devDependencies") or {}),
    )


def _python_dev_dependencies(repo_path: Path) -> set[str]:
    path = repo_path / "pyproject.toml"
    data = _safe_read_toml(path)
    dev_names: set[str] = set()

    project_optional = ((data.get("project") or {}).get("optional-dependencies") or {})
    for group_name, deps in project_optional.items():
        if "dev" not in str(group_name).lower():
            continue
        for dep in deps or []:
            name = str(dep).split()[0].split("[")[0].split("=")[0].strip()
            if name:
                dev_names.add(name.lower())

    poetry_groups = (((data.get("tool") or {}).get("poetry") or {}).get("group") or {})
    dev_group = poetry_groups.get("dev") or {}
    for name in ((dev_group.get("dependencies") or {}).keys()):
        dev_names.add(str(name).lower())

    return dev_names


def _version_key(version: str) -> tuple[int, ...]:
    parts = [int(p) for p in VERSION_RE.findall(version or "")]
    return tuple(parts or [0])


def _update_type(current_version: str, target_version: str) -> str:
    current = list(_version_key(current_version))
    target = list(_version_key(target_version))
    if not current or not target:
        return "unknown"
    while len(current) < 3:
        current.append(0)
    while len(target) < 3:
        target.append(0)
    if target[0] != current[0]:
        return "major"
    if target[1] != current[1]:
        return "minor"
    if target[2] != current[2]:
        return "patch"
    return "same"


def _normalize_cves(raw_id: str, aliases: list[str] | None = None) -> list[str]:
    values = [str(raw_id or "").strip(), *(str(a).strip() for a in aliases or [])]
    cves = [value for value in values if value.upper().startswith("CVE-")]
    return sorted(dict.fromkeys(cves))


def _collect_urls(*values: Any) -> list[str]:
    urls: list[str] = []
    for value in values:
        if isinstance(value, str) and value.startswith("http"):
            urls.append(value)
        elif isinstance(value, dict):
            urls.extend(_collect_urls(*value.values()))
        elif isinstance(value, list):
            urls.extend(_collect_urls(*value))
    deduped: list[str] = []
    for url in urls:
        if url not in deduped:
            deduped.append(url)
    return deduped


def _scan_npm_outdated(repo_path: Path, manifest: Manifest) -> list[Finding]:
    data = _run_json_command(["npm", "outdated", "--json"], repo_path)
    if not isinstance(data, dict):
        return []
    runtime_deps, dev_deps = _npm_dependency_sections(repo_path)
    findings: list[Finding] = []
    for dependency, info in data.items():
        if not isinstance(info, dict):
            continue
        current_version = str(info.get("current") or "")
        target_version = str(info.get("latest") or info.get("wanted") or "")
        dev_only = dependency in dev_deps and dependency not in runtime_deps
        findings.append(
            Finding(
                kind="outdated",
                ecosystem=manifest.ecosystem,
                manifest_path=manifest.path.name,
                dependency=dependency,
                current_version=current_version,
                target_version=target_version,
                update_type=_update_type(current_version, target_version),
                dev_only=dev_only,
                runtime=not dev_only,
                scanner="npm outdated",
                summary=f"{dependency} is outdated ({current_version} -> {target_version}).",
            )
        )
    return findings


def _scan_npm_audit(repo_path: Path, manifest: Manifest) -> list[Finding]:
    data = _run_json_command(["npm", "audit", "--json"], repo_path)
    if not isinstance(data, dict):
        return []
    runtime_deps, dev_deps = _npm_dependency_sections(repo_path)
    vulnerabilities = data.get("vulnerabilities") or {}
    findings: list[Finding] = []
    for dependency, info in vulnerabilities.items():
        if not isinstance(info, dict):
            continue
        via = info.get("via") or []
        advisories = [entry for entry in via if isinstance(entry, dict)]
        fix_available = info.get("fixAvailable")
        target_version = ""
        if isinstance(fix_available, dict):
            target_version = str(fix_available.get("version") or "")
        elif isinstance(fix_available, bool):
            target_version = ""
        current_version = ""
        if dependency in runtime_deps:
            current_version = str(runtime_deps.get(dependency) or "")
        elif dependency in dev_deps:
            current_version = str(dev_deps.get(dependency) or "")
        cves: list[str] = []
        urls: list[str] = []
        for advisory in advisories:
            cves.extend(_normalize_cves(str(advisory.get("source") or ""), advisory.get("cves") or advisory.get("aliases") or []))
            urls.extend(_collect_urls(advisory))
        severity = str(info.get("severity") or "").lower()
        dev_only = dependency in dev_deps and dependency not in runtime_deps
        findings.append(
            Finding(
                kind="vulnerability",
                ecosystem=manifest.ecosystem,
                manifest_path=manifest.path.name,
                dependency=dependency,
                current_version=current_version,
                target_version=target_version,
                update_type=_update_type(current_version, target_version) if target_version else "unknown",
                dev_only=dev_only,
                runtime=not dev_only,
                cve_ids=sorted(dict.fromkeys(cves)),
                severity=severity,
                affected_versions=str(info.get("range") or ""),
                patched_version=target_version,
                advisory_urls=sorted(dict.fromkeys(urls)),
                summary=str(info.get("title") or info.get("name") or f"Vulnerability in {dependency}"),
                scanner="npm audit",
            )
        )
    return findings


def _scan_pip_audit(repo_path: Path, manifest: Manifest) -> list[Finding]:
    if manifest.path.name == "requirements.txt":
        cmd = ["pip-audit", "-r", str(manifest.path), "--format", "json"]
    else:
        cmd = ["pip-audit", "--path", str(repo_path), "--format", "json"]
    data = _run_json_command(cmd, repo_path)
    if not isinstance(data, dict):
        return []
    dev_names = _python_dev_dependencies(repo_path)
    findings: list[Finding] = []
    for dep in data.get("dependencies") or []:
        if not isinstance(dep, dict):
            continue
        name = str(dep.get("name") or "")
        version = str(dep.get("version") or "")
        for vuln in dep.get("vulns") or []:
            if not isinstance(vuln, dict):
                continue
            fix_versions = [str(v) for v in (vuln.get("fix_versions") or []) if str(v).strip()]
            findings.append(
                Finding(
                    kind="vulnerability",
                    ecosystem=manifest.ecosystem,
                    manifest_path=manifest.path.name,
                    dependency=name,
                    current_version=version,
                    target_version=fix_versions[0] if fix_versions else "",
                    update_type=_update_type(version, fix_versions[0]) if fix_versions else "unknown",
                    dev_only=name.lower() in dev_names,
                    runtime=name.lower() not in dev_names,
                    cve_ids=_normalize_cves(str(vuln.get("id") or ""), vuln.get("aliases") or []),
                    severity=str(vuln.get("severity") or "").lower(),
                    affected_versions=str(vuln.get("affected_versions") or ""),
                    patched_version=fix_versions[0] if fix_versions else "",
                    advisory_urls=_collect_urls(vuln),
                    summary=str(vuln.get("description") or vuln.get("summary") or f"Vulnerability in {name}"),
                    scanner="pip-audit",
                )
            )
    return findings


def _extract_osv_fixed_version(vulnerability: dict[str, Any]) -> str:
    for affected in vulnerability.get("affected") or []:
        for rng in affected.get("ranges") or []:
            for event in rng.get("events") or []:
                fixed = str(event.get("fixed") or "").strip()
                if fixed:
                    return fixed
    return ""


def _scan_osv_scanner(repo_path: Path, manifest: Manifest) -> list[Finding]:
    data = _run_json_command(["osv-scanner", "--lockfile", str(manifest.path), "--format", "json"], repo_path)
    if not isinstance(data, dict):
        return []
    runtime_deps, dev_deps = _npm_dependency_sections(repo_path) if manifest.ecosystem == "npm" else ({}, {})
    dev_names = _python_dev_dependencies(repo_path) if manifest.ecosystem == "python" else set()
    findings: list[Finding] = []
    for result in data.get("results") or []:
        for package in result.get("packages") or []:
            name = str((package.get("package") or {}).get("name") or package.get("name") or "")
            version = str(package.get("version") or "")
            for vulnerability in package.get("vulnerabilities") or []:
                aliases = vulnerability.get("aliases") or []
                fixed_version = _extract_osv_fixed_version(vulnerability)
                advisory_urls = _collect_urls(
                    vulnerability.get("references") or [],
                    vulnerability.get("database_specific") or {},
                    vulnerability.get("severity") or [],
                )
                if manifest.ecosystem == "npm":
                    dev_only = name in dev_deps and name not in runtime_deps
                else:
                    dev_only = name.lower() in dev_names
                findings.append(
                    Finding(
                        kind="vulnerability",
                        ecosystem=manifest.ecosystem,
                        manifest_path=manifest.path.name,
                        dependency=name,
                        current_version=version,
                        target_version=fixed_version,
                        update_type=_update_type(version, fixed_version) if fixed_version else "unknown",
                        dev_only=dev_only,
                        runtime=not dev_only,
                        cve_ids=_normalize_cves(str(vulnerability.get("id") or ""), aliases),
                        severity=str(vulnerability.get("database_specific", {}).get("severity") or "").lower(),
                        affected_versions=str(vulnerability.get("affected_versions") or ""),
                        patched_version=fixed_version,
                        advisory_urls=advisory_urls,
                        summary=str(vulnerability.get("summary") or vulnerability.get("details") or f"Vulnerability in {name}"),
                        scanner="osv-scanner",
                    )
                )
    return findings


def _scan_go_vulnerabilities(repo_path: Path, manifest: Manifest) -> list[Finding]:
    return _scan_osv_scanner(repo_path, manifest)


def scan_repo_dependencies(repo_path: Path) -> list[Finding]:
    findings: list[Finding] = []
    for manifest in detect_manifests(repo_path):
        if manifest.ecosystem == "npm" and manifest.path.name == "package.json":
            findings.extend(_scan_npm_outdated(repo_path, manifest))
            findings.extend(_scan_npm_audit(repo_path, manifest))
            findings.extend(_scan_osv_scanner(repo_path, manifest))
        elif manifest.ecosystem == "python":
            findings.extend(_scan_pip_audit(repo_path, manifest))
            findings.extend(_scan_osv_scanner(repo_path, manifest))
        elif manifest.ecosystem == "go":
            findings.extend(_scan_go_vulnerabilities(repo_path, manifest))

    merged: dict[tuple[str, str, str, str], Finding] = {}
    for finding in findings:
        key = (finding.kind, finding.manifest_path, finding.dependency.lower(), finding.current_version)
        existing = merged.get(key)
        if existing is None:
            merged[key] = finding
            continue
        existing.cve_ids = sorted(dict.fromkeys([*existing.cve_ids, *finding.cve_ids]))
        existing.advisory_urls = sorted(dict.fromkeys([*existing.advisory_urls, *finding.advisory_urls]))
        existing.summary = existing.summary or finding.summary
        existing.scanner = ", ".join(sorted(dict.fromkeys([p for p in [existing.scanner, finding.scanner] if p])))
        if not existing.target_version and finding.target_version:
            existing.target_version = finding.target_version
            existing.patched_version = finding.patched_version or finding.target_version
            existing.update_type = finding.update_type
        if not existing.severity and finding.severity:
            existing.severity = finding.severity
    return list(merged.values())


def _is_high_risk(finding: Finding) -> bool:
    if finding.kind == "vulnerability":
        return True
    return finding.update_type == "major"


def _is_low_risk_autobump_candidate(finding: Finding) -> bool:
    return (
        finding.kind == "outdated"
        and finding.dev_only
        and finding.update_type == "patch"
        and not finding.cve_ids
    )


def _sanitize_branch_part(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip().lower()).strip("-")
    return cleaned[:40] or "dependency"


def _replace_version_spec(spec: str, target_version: str) -> str | None:
    raw = str(spec or "").strip()
    if not raw:
        return target_version
    if raw.startswith(("workspace:", "file:", "git+", "github:", "http:", "https:")):
        return None
    prefix = ""
    match = re.match(r"^([~^<>]=?|=)", raw)
    if match:
        prefix = match.group(1)
    return f"{prefix}{target_version}"


def _repo_clean(repo_path: Path) -> bool:
    result = subprocess.run(["git", "status", "--porcelain"], cwd=str(repo_path), capture_output=True, text=True)
    return result.returncode == 0 and not result.stdout.strip()


def _git(cmd: list[str], cwd: Path) -> None:
    subprocess.run(["git", *cmd], cwd=str(cwd), capture_output=True, text=True, check=True)


def _apply_npm_package_json_update(repo_path: Path, dependency: str, target_version: str) -> list[str]:
    package_json_path = repo_path / "package.json"
    data = _safe_read_json(package_json_path)
    changed_files: list[str] = []
    for section in ("devDependencies", "dependencies"):
        deps = data.get(section)
        if not isinstance(deps, dict) or dependency not in deps:
            continue
        updated = _replace_version_spec(str(deps[dependency]), target_version)
        if not updated:
            return []
        deps[dependency] = updated
        package_json_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        changed_files.append("package.json")
        break

    package_lock = repo_path / "package-lock.json"
    if package_lock.exists():
        result = subprocess.run(
            ["npm", "install", "--package-lock-only", f"{dependency}@{target_version}"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and package_lock.exists():
            changed_files.append("package-lock.json")
    return changed_files


def _create_dependency_pr(repo: str, repo_path: Path, finding: Finding) -> str | None:
    if finding.manifest_path != "package.json":
        return None
    if not _repo_clean(repo_path):
        return None

    branch = f"dependency-watcher/{_sanitize_branch_part(finding.dependency)}-{_sanitize_branch_part(finding.target_version)}"
    base_branch_result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        check=True,
    )
    original_branch = (base_branch_result.stdout or "").strip() or "main"

    try:
        _git(["checkout", "-B", branch], repo_path)
        changed_files = _apply_npm_package_json_update(repo_path, finding.dependency, finding.target_version)
        if not changed_files:
            _git(["checkout", original_branch], repo_path)
            return None
        _git(["add", *changed_files], repo_path)
        _git(
            ["commit", "-m",
             with_agent_os_trailer(f"chore(deps): bump {finding.dependency} to {finding.target_version}")],
            repo_path,
        )
        _git(["push", "origin", branch, "--force-with-lease"], repo_path)
        pr_body = (
            f"Automated low-risk dependency bump.\n\n"
            f"- Dependency: `{finding.dependency}`\n"
            f"- Manifest: `{finding.manifest_path}`\n"
            f"- Update: `{finding.current_version}` -> `{finding.target_version}`\n"
            f"- Risk classification: dev-only patch bump with no detected CVE\n"
            f"- Vulnerability advisories: none detected\n"
        )
        pr_url = create_pr_for_branch(
            repo,
            branch,
            f"chore(deps): bump {finding.dependency} to {finding.target_version}",
            pr_body,
        )
        if pr_url:
            pr_number = _extract_pr_number(pr_url)
            if pr_number is not None:
                ensure_labels(repo, LOW_RISK_PR_LABELS)
                gh(["pr", "edit", str(pr_number), "-R", repo, "--add-label", ",".join(LOW_RISK_PR_LABELS)], check=False)
        return pr_url
    finally:
        subprocess.run(["git", "checkout", original_branch], cwd=str(repo_path), capture_output=True, text=True)


def _extract_pr_number(pr_url: str) -> int | None:
    match = re.search(r"/pull/(\d+)$", pr_url or "")
    return int(match.group(1)) if match else None


def _issue_title_for_finding(finding: Finding) -> str:
    if finding.kind == "vulnerability":
        ident = ", ".join(finding.cve_ids) if finding.cve_ids else "security advisory"
        return f"Escalate dependency vulnerability in {finding.dependency} ({ident})"
    return f"Review major dependency bump for {finding.dependency} ({finding.current_version} -> {finding.target_version})"


def _open_issue_exists(repo: str, title: str) -> bool:
    issues = gh_json(
        ["issue", "list", "--repo", repo, "--state", "open", "--search", title, "--json", "title", "--limit", "20"]
    ) or []
    return any(str(issue.get("title") or "").strip() == title.strip() for issue in issues)


def _format_finding_body(finding: Finding) -> str:
    advisory_lines = "\n".join(f"- {url}" for url in finding.advisory_urls) or "- none available from scanner output"
    cve_line = ", ".join(finding.cve_ids) if finding.cve_ids else "none recorded"
    severity = finding.severity or "unknown"
    patched = finding.patched_version or finding.target_version or "unknown"
    goal = (
        f"Investigate and remediate the high-risk dependency finding for `{finding.dependency}` in `{finding.manifest_path}`."
        if finding.kind == "vulnerability"
        else f"Review the major-version dependency bump for `{finding.dependency}` before any automated update."
    )
    criteria = [
        f"Verify impact of `{finding.dependency}` on runtime behavior and compatibility.",
        f"Confirm the safe patched or target version (current: `{finding.current_version or 'unknown'}`, target: `{patched}`).",
        "Record operator-facing remediation notes and any required test coverage before merge.",
    ]
    constraints = [
        "Prefer minimal diffs.",
        "Do not auto-merge dependency PRs.",
        "Use the advisory URLs below for operator verification.",
    ]
    return (
        "## Goal\n"
        f"{goal}\n\n"
        "## Success Criteria\n"
        + "\n".join(f"- {item}" for item in criteria)
        + "\n\n## Constraints\n"
        + "\n".join(f"- {item}" for item in constraints)
        + "\n\n## Dependency Finding\n"
        f"- Dependency: `{finding.dependency}`\n"
        f"- Ecosystem: `{finding.ecosystem}`\n"
        f"- Manifest: `{finding.manifest_path}`\n"
        f"- Scanner: `{finding.scanner or 'unknown'}`\n"
        f"- CVE IDs: {cve_line}\n"
        f"- Severity: `{severity}`\n"
        f"- Affected versions: `{finding.affected_versions or finding.current_version or 'unknown'}`\n"
        f"- Patched version: `{patched}`\n"
        f"- Runtime dependency: `{str(finding.runtime).lower()}`\n"
        f"- Dev-only dependency: `{str(finding.dev_only).lower()}`\n"
        f"- Summary: {finding.summary or 'No additional summary from scanner output.'}\n"
        "\n## Advisory URLs\n"
        f"{advisory_lines}\n"
    )


def _create_high_risk_issue(repo: str, finding: Finding) -> str | None:
    title = _issue_title_for_finding(finding)
    if _open_issue_exists(repo, title):
        return None
    ensure_labels(repo, HIGH_RISK_ISSUE_LABELS)
    result = gh(
        ["issue", "create", "--repo", repo, "--title", title, "--body", _format_finding_body(finding), *sum((["--label", label] for label in HIGH_RISK_ISSUE_LABELS), [])],
        check=False,
    )
    return result.strip() or None


def watch_repo(cfg: dict, github_slug: str, repo_path: Path, now: datetime | None = None) -> dict[str, Any]:
    current = now or _now_utc()
    watcher_cfg = _repo_watcher_config(cfg, github_slug)
    if not watcher_cfg.get("enabled", True):
        return {"repo": github_slug, "created_prs": [], "created_issues": [], "skipped": "disabled"}
    if is_dispatcher_only_repo(cfg, github_slug):
        return {"repo": github_slug, "created_prs": [], "created_issues": [], "skipped": "dispatcher_only"}
    if not repo_path.exists():
        return {"repo": github_slug, "created_prs": [], "created_issues": [], "skipped": "missing_repo"}

    findings = scan_repo_dependencies(repo_path)
    if not findings:
        return {"repo": github_slug, "created_prs": [], "created_issues": [], "skipped": "clean"}

    remaining_budget = _action_budget_remaining(cfg, github_slug, int(watcher_cfg.get("max_actions_per_week", DEFAULT_MAX_ACTIONS_PER_WEEK)), current)
    created_prs: list[str] = []
    created_issues: list[str] = []

    high_risk = [finding for finding in findings if _is_high_risk(finding)]
    low_risk = [finding for finding in findings if _is_low_risk_autobump_candidate(finding)]

    for finding in high_risk:
        if remaining_budget <= 0:
            break
        issue_url = _create_high_risk_issue(github_slug, finding)
        if issue_url:
            created_issues.append(issue_url)
            _record_created_action(cfg, github_slug, current)
            remaining_budget -= 1

    for finding in low_risk:
        if remaining_budget <= 0:
            break
        pr_url = _create_dependency_pr(github_slug, repo_path, finding)
        if pr_url:
            created_prs.append(pr_url)
            _record_created_action(cfg, github_slug, current)
            remaining_budget -= 1

    return {
        "repo": github_slug,
        "created_prs": created_prs,
        "created_issues": created_issues,
        "finding_count": len(findings),
        "skipped": None if (created_prs or created_issues) else "no_actionable_findings",
    }


def run_dependency_watcher(cfg: dict | None = None, now: datetime | None = None) -> list[dict[str, Any]]:
    cfg = cfg or load_config()
    current = now or _now_utc()
    summaries: list[dict[str, Any]] = []
    with job_lock(cfg, WATCHER_JOB_NAME) as acquired:
        if not acquired:
            return [{"repo": "*", "created_prs": [], "created_issues": [], "skipped": "locked"}]
        for github_slug, repo_path in _resolve_repos(cfg):
            repo_cfg = _repo_watcher_config(cfg, github_slug)
            cadence_days = float(repo_cfg.get("cadence_days", DEFAULT_CADENCE_DAYS) or DEFAULT_CADENCE_DAYS)
            due, reason = is_due(cfg, WATCHER_JOB_NAME, github_slug, cadence_hours=cadence_days * 24.0, now=current)
            if not due:
                summaries.append({"repo": github_slug, "created_prs": [], "created_issues": [], "skipped": reason})
                continue
            summary = watch_repo(cfg, github_slug, repo_path, now=current)
            summaries.append(summary)
            record_run(cfg, WATCHER_JOB_NAME, github_slug, now=current)
    return summaries


def main() -> int:
    summaries = run_dependency_watcher()
    for summary in summaries:
        repo = summary.get("repo")
        print(
            f"{repo}: prs={len(summary.get('created_prs') or [])}, "
            f"issues={len(summary.get('created_issues') or [])}, "
            f"status={summary.get('skipped') or 'ran'}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
