from __future__ import annotations

import argparse
import getpass
import gzip
import io
import json
import os
import re
import tarfile
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

import yaml


SCHEMA_VERSION = 1
BUNDLE_ROOT = "bundle"
SECRET_KEY_RE = re.compile(r".*_(TOKEN|KEY|SECRET)$")
SECRET_PLACEHOLDER_PREFIX = "__AGENT_OS_SECRET_"
HOST_PATH_PLACEHOLDER = "__HOST_PATH_REMOVED__"

PORTABLE_FILES = [
    "config.yaml",
    "CODEBASE.md",
    "STRATEGY.md",
    "NORTH_STAR.md",
    "PLANNING_PRINCIPLES.md",
    "RUBRIC.md",
    "target_operating_model.yaml",
]
PORTABLE_DIRS = [
    "objectives",
    "skills",
    ".codex/skills",
]
LOSSY_NOT_CARRIED = [
    ".env files and local shell secrets",
    "runtime/mailbox live queue state",
    "runtime/worktrees git worktrees",
    "running cron jobs or systemd timers",
    "raw runtime JSONL metrics and prompt/log contents",
    "*.log files",
]


class BundleError(RuntimeError):
    pass


def _normalise_rel(path: Path) -> str:
    return path.as_posix().lstrip("/")


def _is_excluded(rel: str) -> bool:
    parts = rel.split("/")
    return (
        rel == ".env"
        or rel.endswith("/.env")
        or rel.endswith(".log")
        or parts[:2] == ["runtime", "mailbox"]
        or parts[:2] == ["runtime", "worktrees"]
    )


def _placeholder_for(path: str, key: str) -> str:
    token = re.sub(r"[^A-Z0-9_]", "_", f"{path}_{key}".upper()).strip("_")
    return f"{SECRET_PLACEHOLDER_PREFIX}{token}__"


def _looks_absolute_path(value: str) -> bool:
    return value.startswith("/") and not value.startswith("//")


def _scrub_absolute_paths(text: str, repo: Path) -> str:
    repo_text = str(repo.resolve())
    text = text.replace(repo_text, ".")
    # Keep URLs intact by avoiding slashes preceded by a URL scheme colon.
    return re.sub(
        r"(?<![:\w])/(?:[A-Za-z0-9._@+-]+/)+[A-Za-z0-9._@+-]*",
        HOST_PATH_PLACEHOLDER,
        text,
    )


def _redact_value(value: Any, rel_path: str, key_path: list[str], secrets: list[dict]) -> Any:
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, child in value.items():
            key_text = str(key)
            if SECRET_KEY_RE.fullmatch(key_text.upper()) and child not in (None, ""):
                placeholder = _placeholder_for(rel_path, "_".join(key_path + [key_text]))
                secrets.append(
                    {
                        "file": rel_path,
                        "key": ".".join(key_path + [key_text]),
                        "placeholder": placeholder,
                    }
                )
                redacted[key] = placeholder
            else:
                redacted[key] = _redact_value(child, rel_path, key_path + [key_text], secrets)
        return redacted
    if isinstance(value, list):
        return [_redact_value(item, rel_path, key_path, secrets) for item in value]
    if isinstance(value, str) and _looks_absolute_path(value):
        return HOST_PATH_PLACEHOLDER
    return value


def _read_yaml_redacted(path: Path, rel_path: str, secrets: list[dict]) -> bytes:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        text = _scrub_absolute_paths(path.read_text(encoding="utf-8"), path.parent)
        return text.encode("utf-8")
    redacted = _redact_value(data, rel_path, [], secrets)
    return yaml.safe_dump(redacted, sort_keys=True).encode("utf-8")


def _read_text_redacted(path: Path, repo: Path) -> bytes:
    text = path.read_text(encoding="utf-8")
    return _scrub_absolute_paths(text, repo).encode("utf-8")


def _iter_dir_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


def _load_agent_stats(repo: Path) -> list[dict]:
    metrics_path = repo / "runtime" / "metrics" / "agent_stats.jsonl"
    if not metrics_path.exists():
        return []
    records = []
    for line in metrics_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            records.append(rec)
    return records


def build_metrics_summary(repo: Path) -> dict:
    records = _load_agent_stats(repo)
    total = len(records)
    statuses = Counter(str(r.get("status", "unknown")) for r in records)
    task_types = Counter(str(r.get("task_type", "unknown")) for r in records)
    durations = [
        float(r["duration_seconds"])
        for r in records
        if isinstance(r.get("duration_seconds"), (int, float)) and float(r["duration_seconds"]) >= 0
    ]
    successes = statuses.get("complete", 0)
    return {
        "source": "runtime/metrics/agent_stats.jsonl",
        "privacy_note": "Aggregated task metrics only; raw JSONL, prompts, and logs are not exported.",
        "task_counts": {
            "total": total,
            "by_status": dict(sorted(statuses.items())),
            "by_task_type": dict(sorted(task_types.items())),
        },
        "success_rate": {
            "successes": successes,
            "total": total,
            "rate": round(successes / total, 4) if total else None,
        },
        "completion_time": {
            "mean_seconds": round(sum(durations) / len(durations), 2) if durations else None,
            "sample_size": len(durations),
        },
    }


def _secrets_markdown(secrets: list[dict]) -> str:
    lines = [
        "# Bundle Secrets",
        "",
        "The exporter replaced matching `*_TOKEN`, `*_KEY`, and `*_SECRET` values with placeholders.",
        "Provide these values during `aos-import` via prompts, `--secret NAME=value`, or matching environment variables.",
        "",
    ]
    if not secrets:
        lines.append("No secrets were found in exported configuration files.")
    else:
        for secret in secrets:
            name = secret["placeholder"].removeprefix(SECRET_PLACEHOLDER_PREFIX).removesuffix("__")
            lines.append(f"- `{name}` in `{secret['file']}` at `{secret['key']}` -> `{secret['placeholder']}`")
    lines.extend(
        [
            "",
            "## Not Carried Over",
            "",
            *[f"- {item}" for item in LOSSY_NOT_CARRIED],
            "",
        ]
    )
    return "\n".join(lines)


def _manifest(entries: dict[str, bytes], source_repo: Path, secrets: list[dict]) -> bytes:
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "created_by": "agent-os aos-export",
        "source_repo_name": source_repo.name,
        "contents": sorted(entries.keys()),
        "secrets": [
            {
                "file": s["file"],
                "key": s["key"],
                "placeholder": s["placeholder"],
            }
            for s in secrets
        ],
        "not_carried_over": LOSSY_NOT_CARRIED,
    }
    return yaml.safe_dump(manifest, sort_keys=True).encode("utf-8")


def _add_tar_bytes(tar: tarfile.TarFile, arcname: str, data: bytes) -> None:
    info = tarfile.TarInfo(arcname)
    info.size = len(data)
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mode = 0o644
    tar.addfile(info, io.BytesIO(data))


def write_deterministic_tar_gz(out_path: Path, entries: dict[str, bytes]) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for rel in sorted(entries):
            _add_tar_bytes(tar, f"{BUNDLE_ROOT}/{rel}", entries[rel])
    with out_path.open("wb") as f:
        with gzip.GzipFile(filename="", mode="wb", fileobj=f, mtime=0) as gz:
            gz.write(raw.getvalue())


def export_bundle(repo: Path, out_path: Path) -> Path:
    repo = repo.resolve()
    if not repo.is_dir():
        raise BundleError(f"Repository does not exist: {repo}")

    entries: dict[str, bytes] = {}
    secrets: list[dict] = []
    for rel in PORTABLE_FILES:
        path = repo / rel
        if not path.exists() or not path.is_file() or _is_excluded(rel):
            continue
        if path.suffix in {".yaml", ".yml"} or path.name == "config.yaml":
            entries[rel] = _read_yaml_redacted(path, rel, secrets)
        else:
            entries[rel] = _read_text_redacted(path, repo)

    for rel_dir in PORTABLE_DIRS:
        root = repo / rel_dir
        if not root.is_dir():
            continue
        for path in _iter_dir_files(root):
            rel = _normalise_rel(path.relative_to(repo))
            if _is_excluded(rel):
                continue
            if path.suffix in {".yaml", ".yml"}:
                entries[rel] = _read_yaml_redacted(path, rel, secrets)
            else:
                entries[rel] = _read_text_redacted(path, repo)

    entries["runtime/metrics/summary.yaml"] = yaml.safe_dump(
        build_metrics_summary(repo),
        sort_keys=True,
    ).encode("utf-8")
    entries["SECRETS.md"] = _secrets_markdown(secrets).encode("utf-8")
    entries["MANIFEST.yaml"] = _manifest(entries, repo, secrets)

    write_deterministic_tar_gz(out_path, entries)
    return out_path


def _safe_extract(bundle_path: Path, dest: Path) -> Path:
    dest_root = dest.resolve()
    with tarfile.open(bundle_path, "r:gz") as tar:
        members = tar.getmembers()
        for member in members:
            target = (dest / member.name).resolve()
            try:
                target.relative_to(dest_root)
            except ValueError as exc:
                raise BundleError(f"Unsafe bundle member path: {member.name}") from exc
            if member.islnk() or member.issym():
                raise BundleError(f"Refusing link in bundle: {member.name}")
        tar.extractall(dest, members, filter="data")
    bundle_root = dest / BUNDLE_ROOT
    if not bundle_root.is_dir():
        raise BundleError("Bundle is missing bundle/ root directory")
    return bundle_root


def _load_manifest(bundle_root: Path) -> dict:
    manifest_path = bundle_root / "MANIFEST.yaml"
    if not manifest_path.exists():
        raise BundleError("Bundle is missing MANIFEST.yaml")
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise BundleError(f"Unsupported bundle schema_version: {manifest.get('schema_version')}")
    return manifest


def _parse_secret_args(values: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise BundleError(f"Invalid --secret value, expected NAME=value: {value}")
        key, secret = value.split("=", 1)
        parsed[key.strip().upper()] = secret
    return parsed


def _resolve_secret_values(manifest: dict, provided: dict[str, str], prompt: bool) -> dict[str, str]:
    values: dict[str, str] = {}
    for secret in manifest.get("secrets", []):
        placeholder = str(secret.get("placeholder", ""))
        name = placeholder.removeprefix(SECRET_PLACEHOLDER_PREFIX).removesuffix("__").upper()
        env_name = f"AGENT_OS_SECRET_{name}"
        if name in provided:
            values[placeholder] = provided[name]
        elif env_name in os.environ:
            values[placeholder] = os.environ[env_name]
        elif prompt:
            values[placeholder] = getpass.getpass(f"Value for {name}: ")
    return values


def _replace_placeholders(text: str, secret_values: dict[str, str]) -> str:
    for placeholder, value in secret_values.items():
        text = text.replace(placeholder, value)
    return text


def _validate_manifest_content_path(rel: Any) -> str:
    if not isinstance(rel, str) or not rel:
        raise BundleError(f"Unsafe manifest content path: {rel!r}")
    path = Path(rel)
    if path.is_absolute() or ".." in path.parts:
        raise BundleError(f"Unsafe manifest content path: {rel}")
    return rel


def _copy_bundle_files(bundle_root: Path, repo: Path, manifest: dict, secret_values: dict[str, str], force: bool) -> list[str]:
    copied: list[str] = []
    contents = [
        _validate_manifest_content_path(p)
        for p in manifest.get("contents", [])
        if p not in {"MANIFEST.yaml", "SECRETS.md", "runtime/metrics/summary.yaml"}
    ]
    for rel in sorted(contents):
        if _is_excluded(rel):
            continue
        src = bundle_root / rel
        if not src.exists() or not src.is_file():
            continue
        dest = repo / rel
        if dest.exists() and not force:
            raise BundleError(f"Refusing to overwrite existing file without --force: {dest}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        text = src.read_text(encoding="utf-8")
        dest.write_text(_replace_placeholders(text, secret_values), encoding="utf-8")
        copied.append(rel)
    return copied


def _restore_config_paths(config_path: Path, repo: Path) -> None:
    if not config_path.exists():
        return
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(cfg, dict):
        return

    defaults = {
        "root_dir": str(repo),
        "config_dir": str(repo),
        "objectives_dir": str(repo / "objectives"),
        "mailbox_dir": str(repo / "runtime" / "mailbox"),
        "logs_dir": str(repo / "runtime" / "logs"),
        "evidence_dir": str(repo / "runtime" / "evidence"),
    }
    for key, value in defaults.items():
        if cfg.get(key) in (None, "", HOST_PATH_PLACEHOLDER, "."):
            cfg[key] = value

    def rewrite_repo_paths(value: Any) -> Any:
        if isinstance(value, dict):
            out = {}
            for key, child in value.items():
                if str(key) in {"local_repo", "repo"} and child in (HOST_PATH_PLACEHOLDER, "."):
                    out[key] = str(repo)
                else:
                    out[key] = rewrite_repo_paths(child)
            return out
        if isinstance(value, list):
            return [rewrite_repo_paths(item) for item in value]
        return value

    cfg = rewrite_repo_paths(cfg)
    config_path.write_text(yaml.safe_dump(cfg, sort_keys=True), encoding="utf-8")


def validate_imported_config(config_path: Path) -> dict:
    from orchestrator.paths import load_config

    old = os.environ.get("AGENT_OS_CONFIG")
    os.environ["AGENT_OS_CONFIG"] = str(config_path)
    try:
        return load_config()
    finally:
        if old is None:
            os.environ.pop("AGENT_OS_CONFIG", None)
        else:
            os.environ["AGENT_OS_CONFIG"] = old


def write_noop_dispatch_task(repo: Path) -> Path:
    inbox = repo / "runtime" / "mailbox" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    task_id = "task-import-smoke-no-op"
    task_path = inbox / f"{task_id}.md"
    meta = {
        "task_id": task_id,
        "repo": str(repo),
        "agent": "auto",
        "task_type": "implementation",
        "branch": "agent/import-smoke-no-op",
        "base_branch": "main",
        "allow_push": False,
        "attempt": 1,
        "max_attempts": 1,
        "max_runtime_minutes": 5,
        "model_attempts": [],
        "priority": "prio:low",
        "context_complete": True,
    }
    body = "# Goal\n\nNo-op import smoke task.\n\n# Success Criteria\n\n- Dispatch metadata parses.\n"
    task_path.write_text(f"---\n{yaml.safe_dump(meta, sort_keys=False)}---\n\n{body}", encoding="utf-8")
    return task_path


def import_bundle(
    bundle_path: Path,
    repo: Path,
    *,
    secrets: dict[str, str] | None = None,
    force: bool = False,
    prompt: bool = True,
    smoke_dispatch: bool = False,
) -> dict:
    bundle_path = bundle_path.resolve()
    repo = repo.resolve()
    repo.mkdir(parents=True, exist_ok=True)
    if any(repo.iterdir()) and not force:
        raise BundleError(f"Target repo is not empty; use --force to allow collisions: {repo}")

    with tempfile.TemporaryDirectory(prefix="agent-os-bundle-") as tmp:
        bundle_root = _safe_extract(bundle_path, Path(tmp))
        manifest = _load_manifest(bundle_root)
        secret_values = _resolve_secret_values(manifest, secrets or {}, prompt)
        copied = _copy_bundle_files(bundle_root, repo, manifest, secret_values, force=force)

    _restore_config_paths(repo / "config.yaml", repo)
    if (repo / "config.yaml").exists():
        validate_imported_config(repo / "config.yaml")
    (repo / "runtime" / "mailbox" / "inbox").mkdir(parents=True, exist_ok=True)
    (repo / "runtime" / "logs").mkdir(parents=True, exist_ok=True)
    smoke_task = write_noop_dispatch_task(repo) if smoke_dispatch else None
    return {
        "repo": str(repo),
        "files": copied,
        "smoke_task": str(smoke_task) if smoke_task else None,
    }


def export_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export a portable agent-os project bundle.")
    parser.add_argument("repo", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        out = export_bundle(args.repo, args.out)
    except BundleError as exc:
        parser.exit(1, f"aos-export: {exc}\n")
    print(out)
    return 0


def import_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import a portable agent-os project bundle.")
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--non-interactive", action="store_true", help="Do not prompt for missing secret values.")
    parser.add_argument("--secret", action="append", default=[], help="Secret replacement as NAME=value.")
    parser.add_argument("--smoke-dispatch", action="store_true", help="Create and parse a no-op dispatch task after import.")
    args = parser.parse_args(argv)
    try:
        result = import_bundle(
            args.bundle,
            args.repo,
            secrets=_parse_secret_args(args.secret),
            force=args.force,
            prompt=not args.non_interactive,
            smoke_dispatch=args.smoke_dispatch,
        )
    except BundleError as exc:
        parser.exit(1, f"aos-import: {exc}\n")
    print(yaml.safe_dump(result, sort_keys=True).strip())
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else os.sys.argv[1:])
    if not argv or argv[0] in {"-h", "--help"}:
        print("usage: python -m orchestrator.project_bundle {export|import} ...")
        return 0 if argv else 2
    command = argv.pop(0)
    if command == "export":
        return export_main(argv)
    if command == "import":
        return import_main(argv)
    print(f"unknown command: {command}", file=os.sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
