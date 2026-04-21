"""External signal ingestion for planner and groomer evidence pipelines."""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

from orchestrator.trust import is_trusted

EXTERNAL_SIGNALS_FILENAME = "external_signals.jsonl"
EXTERNAL_SIGNAL_STATE_FILENAME = "external_signal_fetch_state.json"
DEFAULT_FETCH_INTERVAL_MINUTES = 30
DEFAULT_MAX_SIGNALS_PER_SOURCE = 20
MAX_TITLE_CHARS = 180
MAX_BODY_CHARS = 1200

_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}
_SEVERITY_ALIASES = {
    "critical": "high",
    "fatal": "high",
    "error": "high",
    "warn": "medium",
    "warning": "medium",
    "info": "low",
    "notice": "low",
}
_HIGH_SEVERITY_HINTS = (
    "outage",
    "down",
    "broken",
    "error",
    "exception",
    "crash",
    "failing",
    "failure",
    "security",
    "incident",
    "regression",
)
_MEDIUM_SEVERITY_HINTS = (
    "bug",
    "issue",
    "support",
    "problem",
    "request",
    "complaint",
    "slow",
)
_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9\-_]{2,}")
_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "have", "has", "had",
    "was", "were", "are", "but", "not", "you", "your", "our", "their", "them",
    "they", "its", "can", "into", "about", "after", "before", "just", "more",
}


def _metrics_dir(cfg: dict) -> Path:
    return Path(cfg.get("root_dir", ".")).expanduser() / "runtime" / "metrics"


def external_signals_path(cfg: dict) -> Path:
    return _metrics_dir(cfg) / EXTERNAL_SIGNALS_FILENAME


def external_signal_state_path(cfg: dict) -> Path:
    return _metrics_dir(cfg) / EXTERNAL_SIGNAL_STATE_FILENAME


def _parse_timestamp(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalize_severity(value: object, *, title: str = "", body: str = "") -> str:
    text = str(value or "").strip().lower()
    if text.isdigit():
        return "high" if int(text) >= 4 else "medium" if int(text) >= 2 else "low"
    text = _SEVERITY_ALIASES.get(text, text)
    if text in _SEVERITY_ORDER:
        return text

    haystack = f"{title}\n{body}".lower()
    if any(hint in haystack for hint in _HIGH_SEVERITY_HINTS):
        return "high"
    if any(hint in haystack for hint in _MEDIUM_SEVERITY_HINTS):
        return "medium"
    return "low"


def _normalize_text(value: object, *, limit: int) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:limit]


def _normalize_url(value: object) -> str:
    return str(value or "").strip()[:500]


def _normalize_record(item: dict, *, source_name: str, kind: str, repo: str) -> dict | None:
    title = _normalize_text(
        item.get("title") or item.get("message") or item.get("subject") or item.get("name"),
        limit=MAX_TITLE_CHARS,
    )
    body = _normalize_text(
        item.get("body")
        or item.get("summary")
        or item.get("content")
        or item.get("description")
        or item.get("culprit")
        or item.get("message")
        or "",
        limit=MAX_BODY_CHARS,
    )
    if not title and not body:
        return None

    ts = (
        _parse_timestamp(
            item.get("ts")
            or item.get("timestamp")
            or item.get("updated_at")
            or item.get("created_at")
            or item.get("last_seen")
            or item.get("published")
        )
        or datetime.now(tz=timezone.utc)
    )
    severity = _normalize_severity(
        item.get("severity") or item.get("level") or item.get("priority"),
        title=title,
        body=body,
    )
    return {
        "source": source_name,
        "kind": kind,
        "severity": severity,
        "title": title or body[:MAX_TITLE_CHARS] or "(untitled signal)",
        "body": body,
        "url": _normalize_url(item.get("url") or item.get("html_url") or item.get("permalink") or item.get("link")),
        "ts": ts.isoformat(),
        "repo": str(item.get("repo") or repo or "").strip(),
    }


def _read_json(url: str, *, headers: dict[str, str] | None = None, timeout: int = 20) -> object:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def _read_text(url: str, *, headers: dict[str, str] | None = None, timeout: int = 20) -> str:
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _source_identity(github_slug: str, source: dict) -> str:
    name = str(source.get("name") or source.get("type") or "source").strip().lower()
    return f"{github_slug}:{name}"


def _load_rate_state(cfg: dict) -> dict:
    path = external_signal_state_path(cfg)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_rate_state(cfg: dict, state: dict) -> None:
    path = external_signal_state_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    updated = json.dumps(state, indent=2, sort_keys=True) + "\n"
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(updated)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _rate_limited(cfg: dict, github_slug: str, source: dict) -> bool:
    interval = int(source.get("fetch_interval_minutes") or cfg.get("fetch_interval_minutes") or DEFAULT_FETCH_INTERVAL_MINUTES)
    if interval <= 0:
        return False
    state = _load_rate_state(cfg)
    last = _parse_timestamp(state.get(_source_identity(github_slug, source)))
    if last is None:
        return False
    return datetime.now(tz=timezone.utc) - last < timedelta(minutes=interval)


def _mark_source_fetch(cfg: dict, github_slug: str, source: dict) -> None:
    state = _load_rate_state(cfg)
    state[_source_identity(github_slug, source)] = datetime.now(tz=timezone.utc).isoformat()
    _write_rate_state(cfg, state)


def _repo_external_signal_config(cfg: dict, github_slug: str) -> dict:
    merged = dict(cfg.get("external_signals") or {})
    for project_cfg in cfg.get("github_projects", {}).values():
        if not isinstance(project_cfg, dict):
            continue
        for repo_cfg in project_cfg.get("repos", []):
            if repo_cfg.get("github_repo") != github_slug:
                continue
            override = repo_cfg.get("external_signals")
            if isinstance(override, dict):
                next_cfg = dict(merged)
                next_cfg.update(override)
                merged = next_cfg
            return merged
    return merged


def _iter_records(payload: object) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("items", "events", "issues", "results", "entries", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _build_headers(source: dict) -> tuple[dict[str, str], str | None]:
    headers = {"Accept": source.get("accept", "application/json")}
    credential_env = str(source.get("credential_env") or source.get("token_env") or "").strip()
    if credential_env:
        token = os.environ.get(credential_env, "").strip()
        if not token:
            return {}, credential_env
        template = str(source.get("auth_header") or "Bearer {token}")
        headers["Authorization"] = template.format(token=token)
    extra_headers = source.get("headers") or {}
    if isinstance(extra_headers, dict):
        for key, value in extra_headers.items():
            if str(key).strip() and value is not None:
                headers[str(key).strip()] = str(value)
    return headers, None


def _fetch_sentry_json(source: dict, github_slug: str) -> list[dict]:
    url = str(source.get("url") or "").strip()
    if not url:
        return []
    headers, missing_env = _build_headers(source)
    if missing_env:
        raise RuntimeError(f"missing credentials env {missing_env}")
    payload = _read_json(url, headers=headers)
    records = []
    repo = str(source.get("repo") or github_slug).strip()
    source_name = str(source.get("name") or "sentry_json").strip()
    for item in _iter_records(payload)[: int(source.get("max_signals", DEFAULT_MAX_SIGNALS_PER_SOURCE))]:
        normalized = _normalize_record(item, source_name=source_name, kind="error", repo=repo)
        if normalized:
            records.append(normalized)
    return records


def _fetch_github_mentions(source: dict, github_slug: str, cfg: dict) -> list[dict]:
    query = str(source.get("query") or f"\"{github_slug}\" type:issue type:pr").strip()
    limit = int(source.get("max_signals") or DEFAULT_MAX_SIGNALS_PER_SOURCE)
    result = subprocess.run(
        [
            "gh",
            "api",
            "search/issues",
            "--method",
            "GET",
            "-f",
            f"q={query}",
            "-f",
            f"per_page={limit}",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[:300]
        raise RuntimeError(f"gh search failed: {detail}")
    payload = json.loads(result.stdout or "{}")
    records = []
    source_name = str(source.get("name") or "github_mentions").strip()
    for item in _iter_records(payload):
        user = item.get("user") or {}
        login = str(user.get("login") or "").strip()
        if is_trusted(login, cfg):
            continue
        body = _normalize_text(item.get("body") or "", limit=MAX_BODY_CHARS)
        normalized = _normalize_record(
            {
                "title": item.get("title"),
                "body": body,
                "url": item.get("html_url"),
                "timestamp": item.get("updated_at") or item.get("created_at"),
                "severity": source.get("default_severity"),
                "repo": github_slug,
            },
            source_name=source_name,
            kind="mention",
            repo=github_slug,
        )
        if normalized:
            records.append(normalized)
    return records


def _xml_text(element: ET.Element | None, tag_names: tuple[str, ...]) -> str:
    if element is None:
        return ""
    for child in list(element):
        local = child.tag.rsplit("}", 1)[-1]
        if local in tag_names and (child.text or "").strip():
            return child.text.strip()
    return ""


def _xml_link(element: ET.Element | None) -> str:
    if element is None:
        return ""
    for child in list(element):
        local = child.tag.rsplit("}", 1)[-1]
        if local != "link":
            continue
        href = child.attrib.get("href")
        if href:
            return href.strip()
        if (child.text or "").strip():
            return child.text.strip()
    return ""


def _fetch_rss_atom(source: dict, github_slug: str) -> list[dict]:
    url = str(source.get("url") or "").strip()
    if not url:
        return []
    headers, missing_env = _build_headers(source)
    if missing_env:
        raise RuntimeError(f"missing credentials env {missing_env}")
    xml_text = _read_text(url, headers=headers)
    root = ET.fromstring(xml_text)
    records = []
    source_name = str(source.get("name") or "rss_atom").strip()
    repo = str(source.get("repo") or github_slug).strip()
    kind = str(source.get("kind") or "mention").strip() or "mention"
    entries = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1] in {"item", "entry"}]
    for entry in entries[: int(source.get("max_signals") or DEFAULT_MAX_SIGNALS_PER_SOURCE)]:
        normalized = _normalize_record(
            {
                "title": _xml_text(entry, ("title",)),
                "body": _xml_text(entry, ("description", "summary", "content")),
                "url": _xml_link(entry),
                "timestamp": _xml_text(entry, ("updated", "published", "pubDate")),
                "severity": source.get("default_severity"),
                "repo": repo,
            },
            source_name=source_name,
            kind=kind,
            repo=repo,
        )
        if normalized:
            records.append(normalized)
    return records


def _append_records(path: Path, records: list[dict]) -> list[dict]:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    seen: set[tuple[str, str, str, str, str]] = set()
    for line in existing.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        seen.add((
            str(item.get("source") or ""),
            str(item.get("url") or ""),
            str(item.get("title") or ""),
            str(item.get("ts") or ""),
            str(item.get("repo") or ""),
        ))

    new_records: list[dict] = []
    for record in records:
        key = (
            str(record.get("source") or ""),
            str(record.get("url") or ""),
            str(record.get("title") or ""),
            str(record.get("ts") or ""),
            str(record.get("repo") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        new_records.append(record)

    if not new_records:
        return []

    payload = existing + "".join(json.dumps(record, sort_keys=True) + "\n" for record in new_records)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return new_records


def run_external_ingester(cfg: dict, github_slug: str, repo_path: Path | None = None) -> list[dict]:
    del repo_path  # Reserved for future repo-local adapters.
    signals_cfg = _repo_external_signal_config(cfg, github_slug)
    if not signals_cfg.get("enabled"):
        return []

    sources = signals_cfg.get("sources") or []
    if not isinstance(sources, list):
        return []

    fetched: list[dict] = []
    for source in sources:
        if not isinstance(source, dict) or not source.get("enabled"):
            continue
        source_repo = str(source.get("repo") or "").strip()
        if source_repo and source_repo != github_slug:
            continue
        if _rate_limited(cfg, github_slug, source):
            continue
        source_type = str(source.get("type") or "").strip().lower()
        try:
            if source_type == "sentry_json":
                fetched.extend(_fetch_sentry_json(source, github_slug))
            elif source_type == "github_mentions":
                fetched.extend(_fetch_github_mentions(source, github_slug, cfg))
            elif source_type in {"rss", "atom", "rss_atom"}:
                fetched.extend(_fetch_rss_atom(source, github_slug))
            else:
                print(f"  Skipping external signal source {source.get('name', '?')}: unsupported type {source_type or '?'}")
                continue
            _mark_source_fetch(cfg, github_slug, source)
        except RuntimeError as exc:
            print(f"  Skipping external signal source {source.get('name', '?')}: {exc}")
        except (json.JSONDecodeError, ET.ParseError, urllib.error.URLError, TimeoutError, subprocess.TimeoutExpired) as exc:
            print(f"  External signal source {source.get('name', '?')} failed: {exc}")
        except Exception as exc:
            print(f"  External signal source {source.get('name', '?')} failed unexpectedly: {exc}")

    if fetched:
        _append_records(external_signals_path(cfg), fetched)
    return fetched


def load_external_signals(cfg: dict, *, repo: str | None = None, window_days: int = 7) -> list[dict]:
    path = external_signals_path(cfg)
    if not path.exists():
        return []
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=max(1, window_days))
    records: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if repo and str(record.get("repo") or "").strip() != repo:
                continue
            ts = _parse_timestamp(record.get("ts"))
            if ts is None or ts < cutoff:
                continue
            record["_ts"] = ts
            records.append(record)
    records.sort(key=lambda item: (_SEVERITY_ORDER.get(str(item.get("severity") or "low"), 9), -item["_ts"].timestamp()))
    return records


def format_external_signals_for_prompt(
    records: list[dict],
    *,
    max_items: int = 6,
    max_chars: int = 1600,
) -> str:
    if not records:
        return "(no recent external signals)"
    ordered = sorted(
        records,
        key=lambda record: (
            _SEVERITY_ORDER.get(str(record.get("severity") or "low"), 9),
            -(_parse_timestamp(record.get("ts")) or datetime.fromtimestamp(0, tz=timezone.utc)).timestamp(),
        ),
    )
    lines: list[str] = []
    for record in ordered[:max_items]:
        title = str(record.get("title") or "(untitled signal)").strip()
        severity = str(record.get("severity") or "low").strip()
        kind = str(record.get("kind") or "signal").strip()
        source = str(record.get("source") or "external").strip()
        body = str(record.get("body") or "").strip()
        snippet = f" — {body[:160]}" if body else ""
        lines.append(f"- [{severity}] {kind} via {source}: {title}{snippet}")
    text = "\n".join(lines)
    return text[:max_chars]


def _signal_signature(text: str) -> tuple[str, ...]:
    words = [w.lower().rstrip("_-") for w in _WORD_RE.findall(text or "")]
    return tuple(sorted({w for w in words if w not in _STOPWORDS})[:6])


def format_external_signal_concerns(records: list[dict], *, max_items: int = 5) -> str:
    clusters: dict[tuple[str, ...], list[dict]] = {}
    for record in records:
        sig = _signal_signature(str(record.get("title") or ""))
        if not sig:
            continue
        clusters.setdefault(sig, []).append(record)
    repeated = [
        items for items in clusters.values()
        if len(items) >= 2
    ]
    if not repeated:
        return ""
    repeated.sort(key=lambda items: (-len(items), _SEVERITY_ORDER.get(str(items[0].get("severity") or "low"), 9)))
    lines = []
    for items in repeated[:max_items]:
        exemplar = items[0]
        lines.append(
            f"- External signals repeated {len(items)} time(s): {exemplar.get('title', '(untitled signal)')}"
        )
    return "\n".join(lines)
