"""Curated tool registry resolution and fail-closed startup validation."""
from __future__ import annotations

import os
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import yaml


DEFAULT_VERIFIED_PACKAGES_FILE = "verified_packages.yaml"
DEFAULT_LIBRARY_CATALOG_FILE = "library_catalog.yaml"
_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_ENV_REF_RE = re.compile(r"^\$\{([A-Z][A-Z0-9_]*)\}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


def _config_dir(cfg: dict) -> Path:
    return Path(cfg.get("_config_dir") or cfg.get("config_dir") or ".").expanduser()


def _resolve_registry_path(cfg: dict, raw_path: str | None, default_name: str) -> Path:
    path = Path(str(raw_path or default_name)).expanduser()
    if path.is_absolute():
        return path
    return _config_dir(cfg) / path


def verified_packages_path(cfg: dict) -> Path:
    registry = cfg.get("tool_registry") or {}
    return _resolve_registry_path(cfg, registry.get("verified_packages_file"), DEFAULT_VERIFIED_PACKAGES_FILE)


def library_catalog_path(cfg: dict) -> Path:
    registry = cfg.get("tool_registry") or {}
    return _resolve_registry_path(cfg, registry.get("library_catalog_file"), DEFAULT_LIBRARY_CATALOG_FILE)


def _load_yaml_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data if isinstance(data, dict) else {}


def _verified_package_index(cfg: dict) -> dict[tuple[str, str, str], dict[str, Any]]:
    payload = _load_yaml_file(verified_packages_path(cfg))
    entries = payload.get("packages") or []
    index: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in entries:
        if not isinstance(item, dict):
            continue
        ecosystem = str(item.get("ecosystem") or "npm").strip().lower()
        package = str(item.get("package") or item.get("name") or "").strip()
        version = str(item.get("version") or "").strip()
        if ecosystem and package and version:
            index[(ecosystem, package, version)] = item
    return index


def load_library_catalog(cfg: dict) -> list[dict[str, Any]]:
    payload = _load_yaml_file(library_catalog_path(cfg))
    entries = payload.get("libraries") or []
    return [item for item in entries if isinstance(item, dict)]


def _iter_repo_cfgs(cfg: dict) -> list[dict[str, Any]]:
    repos: list[dict[str, Any]] = []
    for project_cfg in (cfg.get("github_projects") or {}).values():
        if not isinstance(project_cfg, dict):
            continue
        for repo_cfg in project_cfg.get("repos", []) or []:
            if isinstance(repo_cfg, dict):
                repos.append(repo_cfg)
    return repos


def _repo_matches(repo_cfg: dict[str, Any], repo_key: str) -> bool:
    normalized = str(repo_key or "").strip()
    if not normalized:
        return False
    candidates = {
        str(repo_cfg.get("key") or "").strip(),
        str(repo_cfg.get("github_repo") or "").strip(),
        str(repo_cfg.get("local_repo") or repo_cfg.get("path") or "").strip(),
        Path(str(repo_cfg.get("local_repo") or repo_cfg.get("path") or ".")).name,
    }
    candidates.discard("")
    return normalized in candidates


def _find_repo_cfg(cfg: dict, repo_key: str) -> dict[str, Any] | None:
    for repo_cfg in _iter_repo_cfgs(cfg):
        if _repo_matches(repo_cfg, repo_key):
            return repo_cfg
    return None


def _normalize_env_ref(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("credential env var reference is empty")
    match = _ENV_REF_RE.fullmatch(raw)
    if match:
        return match.group(1)
    if _ENV_NAME_RE.fullmatch(raw):
        return raw
    raise ValueError(
        f"registry credentials must reference environment variables only; got {raw!r}"
    )


def _tool_env_refs(tool: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    credential_env = tool.get("credential_env")
    if credential_env:
        refs.append(_normalize_env_ref(credential_env))
    env_block = tool.get("env") or {}
    if isinstance(env_block, dict):
        for value in env_block.values():
            refs.append(_normalize_env_ref(value))
    deduped: list[str] = []
    for ref in refs:
        if ref not in deduped:
            deduped.append(ref)
    return deduped


def _task_permissions(tool: dict[str, Any]) -> dict[str, list[str]]:
    raw = tool.get("task_permissions") or {}
    if not isinstance(raw, dict):
        return {}
    resolved: dict[str, list[str]] = {}
    for task_type, permissions in raw.items():
        if isinstance(permissions, list):
            values = [str(item).strip() for item in permissions if str(item).strip()]
        elif permissions in (None, ""):
            values = []
        else:
            values = [str(permissions).strip()]
        resolved[str(task_type).strip().lower()] = values
    return resolved


def _normalize_tool_record(tool_id: str, tool_type: str, tool: dict[str, Any], task_type: str) -> dict[str, Any] | None:
    permissions = _task_permissions(tool).get(str(task_type or "").strip().lower(), [])
    if not permissions:
        return None
    return {
        "id": tool_id,
        "type": tool_type,
        "title": str(tool.get("title") or tool_id).strip(),
        "description": str(tool.get("description") or "").strip(),
        "permissions": permissions,
        "credential_envs": _tool_env_refs(tool),
        "package": str(tool.get("package") or "").strip(),
        "version": str(tool.get("version") or "").strip(),
        "sha256": str(tool.get("sha256") or "").strip(),
        "base_url": str(tool.get("base_url") or "").strip(),
        "transport": str(tool.get("transport") or "stdio").strip(),
        "command": list(tool.get("command") or []),
    }


def resolve_tools_for(repo_key: str, task_type: str, cfg: dict | None = None) -> dict[str, Any]:
    cfg = cfg or {}
    registry = cfg.get("tool_registry") or {}
    repo_cfg = _find_repo_cfg(cfg, repo_key)
    enabled_tools = None
    if repo_cfg is not None and "enabled_tools" in repo_cfg:
        raw_enabled = repo_cfg.get("enabled_tools") or []
        enabled_tools = [str(item).strip() for item in raw_enabled if str(item).strip()]

    if enabled_tools is None:
        return {
            "repo_key": repo_key,
            "task_type": str(task_type or "").strip().lower(),
            "default_toolset_allowed": True,
            "enabled_tool_ids": [],
            "mcp_servers": [],
            "http_apis": [],
            "all_tools": [],
        }

    mcp_servers = registry.get("mcp_servers") or {}
    http_apis = registry.get("http_apis") or {}
    unknown = [tool_id for tool_id in enabled_tools if tool_id not in mcp_servers and tool_id not in http_apis]
    if unknown:
        raise ValueError(f"repo {repo_key!r} enables unknown curated tool(s): {', '.join(sorted(unknown))}")

    resolved_mcp: list[dict[str, Any]] = []
    resolved_http: list[dict[str, Any]] = []
    normalized_task_type = str(task_type or "").strip().lower()
    for tool_id in enabled_tools:
        if tool_id in mcp_servers:
            record = _normalize_tool_record(tool_id, "mcp", mcp_servers[tool_id], normalized_task_type)
            if record:
                resolved_mcp.append(record)
        elif tool_id in http_apis:
            record = _normalize_tool_record(tool_id, "http", http_apis[tool_id], normalized_task_type)
            if record:
                resolved_http.append(record)

    return {
        "repo_key": repo_key,
        "task_type": normalized_task_type,
        "default_toolset_allowed": False,
        "enabled_tool_ids": enabled_tools,
        "mcp_servers": resolved_mcp,
        "http_apis": resolved_http,
        "all_tools": [*resolved_mcp, *resolved_http],
    }


def format_tool_bundle_for_prompt(bundle: dict[str, Any]) -> str:
    if bundle.get("default_toolset_allowed", True):
        return "Curated tool registry: no per-repo override; adapter default toolset remains in effect."
    tools = bundle.get("all_tools") or []
    if not tools:
        return (
            "Curated tool registry: this repo is opt-in, but no curated tool is scoped to "
            f"task_type={bundle.get('task_type')!r}."
        )
    lines = ["Curated tool registry for this repo/task type:"]
    for tool in tools:
        detail = f"{tool['id']} [{tool['type']}] perms={','.join(tool.get('permissions') or [])}"
        if tool.get("base_url"):
            detail += f" base_url={tool['base_url']}"
        if tool.get("package") and tool.get("version"):
            detail += f" package={tool['package']}@{tool['version']}"
        lines.append(f"- {detail}")
    return "\n".join(lines)


def _enabled_tool_records(cfg: dict) -> list[tuple[str, str, dict[str, Any]]]:
    registry = cfg.get("tool_registry") or {}
    mcp_servers = registry.get("mcp_servers") or {}
    http_apis = registry.get("http_apis") or {}
    seen: set[tuple[str, str]] = set()
    resolved: list[tuple[str, str, dict[str, Any]]] = []
    for repo_cfg in _iter_repo_cfgs(cfg):
        enabled_tools = repo_cfg.get("enabled_tools")
        if enabled_tools is None:
            continue
        for tool_id in enabled_tools or []:
            tool_key = str(tool_id).strip()
            if not tool_key:
                continue
            if tool_key in mcp_servers:
                key = ("mcp", tool_key)
                if key not in seen:
                    seen.add(key)
                    resolved.append((key[0], tool_key, mcp_servers[tool_key]))
            elif tool_key in http_apis:
                key = ("http", tool_key)
                if key not in seen:
                    seen.add(key)
                    resolved.append((key[0], tool_key, http_apis[tool_key]))
            else:
                raise ValueError(f"enabled_tools references unknown curated tool {tool_key!r}")
    return resolved


def _validate_mcp_package(tool_id: str, tool: dict[str, Any], verified_index: dict[tuple[str, str, str], dict[str, Any]]) -> None:
    package = str(tool.get("package") or "").strip()
    version = str(tool.get("version") or "").strip()
    sha256 = str(tool.get("sha256") or "").strip().lower()
    if not package:
        raise ValueError(f"tool_registry.mcp_servers.{tool_id} must declare package")
    if not version:
        raise ValueError(f"tool_registry.mcp_servers.{tool_id} must declare pinned version")
    if version.lower() == "latest" or "@latest" in package.lower():
        raise ValueError(f"tool_registry.mcp_servers.{tool_id} uses banned @latest pin")
    if not _SHA256_RE.fullmatch(sha256):
        raise ValueError(f"tool_registry.mcp_servers.{tool_id} must declare sha256")
    verified = verified_index.get(("npm", package, version))
    if not verified:
        raise ValueError(
            f"tool_registry.mcp_servers.{tool_id} is not present in the curated verified_packages registry"
        )
    expected = str(verified.get("sha256") or "").strip().lower()
    if sha256 != expected:
        raise ValueError(
            f"tool_registry.mcp_servers.{tool_id} sha256 mismatch for {package}@{version}: "
            f"expected {expected}, got {sha256}"
        )


def _validate_task_permissions(tool_type: str, tool_id: str, tool: dict[str, Any]) -> None:
    permissions = _task_permissions(tool)
    if not permissions:
        raise ValueError(f"tool_registry.{tool_type}.{tool_id} must declare task_permissions")


def _validate_tool_envs(tool_type: str, tool_id: str, tool: dict[str, Any]) -> None:
    for env_name in _tool_env_refs(tool):
        if not os.environ.get(env_name):
            raise ValueError(
                f"tool_registry.{tool_type}.{tool_id} requires environment variable {env_name}"
            )


def validate_tool_registry_config(cfg: dict) -> dict[str, Any]:
    registry = cfg.get("tool_registry") or {}
    mcp_servers = registry.get("mcp_servers") or {}
    http_apis = registry.get("http_apis") or {}
    if not mcp_servers and not http_apis:
        return {
            "registered_mcp": 0,
            "registered_http": 0,
            "enabled_tools": 0,
            "verified_mcp": 0,
            "status": "inactive",
        }

    verified_index = _verified_package_index(cfg)
    enabled_records = _enabled_tool_records(cfg)
    verified_mcp = 0
    for tool_type, tool_id, tool in enabled_records:
        label = "http_apis" if tool_type == "http" else "mcp_servers"
        _validate_task_permissions(label, tool_id, tool)
        _validate_tool_envs(label, tool_id, tool)
        if tool_type == "mcp":
            _validate_mcp_package(tool_id, tool, verified_index)
            verified_mcp += 1

    return {
        "registered_mcp": len(mcp_servers),
        "registered_http": len(http_apis),
        "enabled_tools": len(enabled_records),
        "verified_mcp": verified_mcp,
        "status": "verified" if enabled_records else "configured",
    }


def _notify_registry_failure(cfg: dict, message: str) -> None:
    token = str(cfg.get("telegram_bot_token") or "").strip()
    chat_id = str(cfg.get("telegram_chat_id") or "").strip()
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": f"🧰 Tool registry pre-flight failed\n{message}"}
    ).encode("utf-8")
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=payload), timeout=10):
            pass
    except Exception:
        pass


def validate_tool_registry_or_raise(cfg: dict) -> dict[str, Any]:
    try:
        return validate_tool_registry_config(cfg)
    except Exception as exc:
        _notify_registry_failure(cfg, str(exc))
        raise


def registry_status_line(cfg: dict) -> str:
    status = cfg.get("_tool_registry_status") or {}
    registered_mcp = int(status.get("registered_mcp") or 0)
    registered_http = int(status.get("registered_http") or 0)
    if registered_mcp == 0 and registered_http == 0:
        return "tool registry: inactive"
    enabled = int(status.get("enabled_tools") or 0)
    verified = int(status.get("verified_mcp") or 0)
    state = str(status.get("status") or "configured")
    return (
        f"tool registry: mcp={registered_mcp}, http={registered_http}, "
        f"enabled={enabled}, verified_mcp={verified}, preflight={state}"
    )
