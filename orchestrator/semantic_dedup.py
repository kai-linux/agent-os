"""Semantic duplicate detection for groom-time issue creation.

The primary path uses a local sentence-transformers model with a SQLite cache.
When that dependency or model cannot be loaded, the module falls back to a
deterministic SimHash-style vector so grooming can continue without a remote
embedding service.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_THRESHOLD = 0.82
DEFAULT_RECENTLY_CLOSED_DAYS = 90
BODY_CHARS = 500
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_TASK_RE = re.compile(r"(task-\d{8}-\d{6}[-a-z0-9]*)", re.IGNORECASE)


@dataclass(frozen=True)
class DedupCandidate:
    title: str
    body: str = ""
    number: int | None = None
    url: str = ""
    state: str = "open"
    source: str = "open_issue"
    branch: str = ""


@dataclass(frozen=True)
class DedupMatch:
    candidate: DedupCandidate
    similarity: float
    backend: str


def issue_text(title: str, body: str = "") -> str:
    title = (title or "").strip()
    body_prefix = (body or "").strip()[:BODY_CHARS]
    # Title carries the groomer's intent; weighting it avoids long templates in
    # issue bodies drowning out short same-meaning titles.
    return f"{title}\n{title}\n\n{body_prefix}".strip()


def _repo_semantic_cfg(cfg: dict, github_slug: str) -> dict:
    merged = dict(cfg.get("semantic_dedup") or {})
    for project in (cfg.get("github_projects") or {}).values():
        if not isinstance(project, dict):
            continue
        project_cfg = project.get("semantic_dedup")
        if isinstance(project_cfg, dict):
            merged.update(project_cfg)
        for repo_cfg in project.get("repos", []) or []:
            if repo_cfg.get("github_repo") != github_slug:
                continue
            repo_semantic = repo_cfg.get("semantic_dedup")
            if isinstance(repo_semantic, dict):
                merged.update(repo_semantic)
            if "semantic_dedup_threshold" in repo_cfg:
                merged["threshold"] = repo_cfg["semantic_dedup_threshold"]
            return merged
    return merged


def semantic_dedup_enabled(cfg: dict, github_slug: str) -> bool:
    return bool(_repo_semantic_cfg(cfg, github_slug).get("enabled", True))


def semantic_threshold(cfg: dict, github_slug: str) -> float:
    value = _repo_semantic_cfg(cfg, github_slug).get("threshold", DEFAULT_THRESHOLD)
    try:
        return float(value)
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLD


def recently_closed_days(cfg: dict, github_slug: str) -> int:
    value = _repo_semantic_cfg(cfg, github_slug).get("recently_closed_days", DEFAULT_RECENTLY_CLOSED_DAYS)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return DEFAULT_RECENTLY_CLOSED_DAYS


class _SQLiteVectorCache:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _init(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS embeddings "
                "(cache_key TEXT PRIMARY KEY, model TEXT NOT NULL, text_hash TEXT NOT NULL, vector TEXT NOT NULL)"
            )

    def get(self, model: str, text: str) -> list[float] | None:
        text_hash = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
        cache_key = f"{model}:{text_hash}"
        with sqlite3.connect(self.path) as conn:
            row = conn.execute("SELECT vector FROM embeddings WHERE cache_key = ?", (cache_key,)).fetchone()
        if not row:
            return None
        try:
            return [float(v) for v in json.loads(row[0])]
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    def set(self, model: str, text: str, vector: list[float]) -> None:
        text_hash = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
        cache_key = f"{model}:{text_hash}"
        payload = json.dumps([float(v) for v in vector], separators=(",", ":"))
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO embeddings(cache_key, model, text_hash, vector) VALUES (?, ?, ?, ?)",
                (cache_key, model, text_hash, payload),
            )


class _SentenceTransformerBackend:
    name = "sentence-transformers"

    def __init__(self, model_name: str, cache: _SQLiteVectorCache):
        from sentence_transformers import SentenceTransformer  # type: ignore

        self.model_name = model_name
        self.cache = cache
        self.model = SentenceTransformer(model_name)

    def embed(self, text: str) -> list[float]:
        cached = self.cache.get(self.model_name, text)
        if cached is not None:
            return cached
        vector = self.model.encode(text, normalize_embeddings=True)
        if hasattr(vector, "tolist"):
            vector = vector.tolist()
        result = [float(v) for v in vector]
        self.cache.set(self.model_name, text, result)
        return result


class _SimHashBackend:
    name = "simhash"
    model_name = "simhash-v1"

    _SYNONYMS = {
        "adoption": "growth",
        "activation": "onboarding",
        "auth": "authentication",
        "authenticate": "authentication",
        "budget": "cost",
        "ci": "test",
        "dedupe": "deduplication",
        "dedup": "deduplication",
        "deduplicate": "deduplication",
        "deduplicated": "deduplication",
        "duplicate": "deduplication",
        "duplicates": "deduplication",
        "fail": "failure",
        "failed": "failure",
        "failing": "failure",
        "flake": "failure",
        "flakes": "failure",
        "groom": "groomer",
        "grooming": "groomer",
        "issue": "ticket",
        "issues": "ticket",
        "scorer": "signal",
        "semantic": "meaning",
        "spend": "cost",
        "tests": "test",
    }

    def __init__(self, cache: _SQLiteVectorCache | None = None):
        self.cache = cache

    def embed(self, text: str) -> list[float]:
        if self.cache:
            cached = self.cache.get(self.model_name, text)
            if cached is not None:
                return cached
        buckets = [0.0] * 256
        tokens = self._tokens(text)
        for feature, weight in self._features(tokens):
            digest = hashlib.sha256(feature.encode("utf-8", errors="replace")).digest()
            idx = int.from_bytes(digest[:4], "big") % len(buckets)
            buckets[idx] += weight
        vector = buckets
        if self.cache:
            self.cache.set(self.model_name, text, vector)
        return vector

    @classmethod
    def _tokens(cls, text: str) -> list[str]:
        tokens = []
        for token in _TOKEN_RE.findall((text or "").lower()):
            normalized = cls._SYNONYMS.get(token, token)
            if len(normalized) > 4 and normalized.endswith("ing"):
                normalized = normalized[:-3]
            elif len(normalized) > 3 and normalized.endswith("ed"):
                normalized = normalized[:-2]
            elif len(normalized) > 3 and normalized.endswith("s"):
                normalized = normalized[:-1]
            tokens.append(normalized)
        return tokens

    @staticmethod
    def _features(tokens: list[str]) -> Iterable[tuple[str, float]]:
        for token in tokens:
            yield token, 1.0
        for left, right in zip(tokens, tokens[1:]):
            yield f"{left}:{right}", 0.55


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if not norm_a or not norm_b:
        return 0.0
    return dot / (norm_a * norm_b)


class SemanticDeduper:
    def __init__(self, cfg: dict, github_slug: str, repo_path: Path | None = None):
        self.cfg = cfg
        self.github_slug = github_slug
        self.repo_path = repo_path
        semantic_cfg = _repo_semantic_cfg(cfg, github_slug)
        self.threshold = semantic_threshold(cfg, github_slug)
        root = Path(cfg.get("root_dir", ".")).expanduser()
        cache_path = Path(semantic_cfg.get("cache_path") or root / "runtime" / "semantic_dedup" / "embeddings.sqlite")
        self.cache = _SQLiteVectorCache(cache_path)
        self.backend = self._load_backend(semantic_cfg)

    def _load_backend(self, semantic_cfg: dict):
        model_name = str(semantic_cfg.get("model") or DEFAULT_MODEL)
        fallback = str(semantic_cfg.get("fallback") or "simhash").lower()
        if fallback != "always_simhash":
            try:
                return _SentenceTransformerBackend(model_name, self.cache)
            except Exception:
                pass
        return _SimHashBackend(self.cache)

    @property
    def backend_name(self) -> str:
        return self.backend.name

    def embed(self, title: str, body: str = "") -> list[float]:
        return self.backend.embed(issue_text(title, body))

    def score(self, title: str, body: str, candidate: DedupCandidate) -> float:
        return cosine_similarity(self.embed(title, body), self.embed(candidate.title, candidate.body))

    def find_duplicate(
        self,
        title: str,
        body: str,
        candidates: Iterable[DedupCandidate],
    ) -> DedupMatch | None:
        best: DedupMatch | None = None
        probe = self.embed(title, body)
        for candidate in candidates:
            if not candidate.title:
                continue
            score = cosine_similarity(probe, self.embed(candidate.title, candidate.body))
            if best is None or score > best.similarity:
                best = DedupMatch(candidate=candidate, similarity=score, backend=self.backend_name)
        if best and best.similarity >= self.threshold:
            return best
        return None


def _gh_json(cmd: list[str]) -> list[dict]:
    try:
        result = subprocess.run(["gh", *cmd], capture_output=True, text=True)
    except FileNotFoundError:
        return []
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _trusted(issue: dict, cfg: dict) -> bool:
    try:
        from orchestrator.trust import is_trusted

        return is_trusted((issue.get("author") or {}).get("login", ""), cfg)
    except Exception:
        return True


def list_recently_closed_issues(repo: str, cfg: dict, days: int = DEFAULT_RECENTLY_CLOSED_DAYS) -> list[DedupCandidate]:
    if days <= 0:
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    issues = _gh_json([
        "issue", "list", "--repo", repo, "--state", "closed",
        "--json", "number,title,body,closedAt,url,author",
        "--limit", "100",
    ])
    candidates: list[DedupCandidate] = []
    for issue in issues:
        if not _trusted(issue, cfg):
            continue
        closed_at = str(issue.get("closedAt") or "")
        try:
            closed_dt = datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        if closed_dt < cutoff:
            continue
        candidates.append(_candidate_from_issue(issue, source="recently_closed_issue", state="closed"))
    return candidates


def _candidate_from_issue(issue: dict, *, source: str, state: str | None = None) -> DedupCandidate:
    number = issue.get("number")
    try:
        number = int(number) if number is not None else None
    except (TypeError, ValueError):
        number = None
    return DedupCandidate(
        title=str(issue.get("title") or ""),
        body=str(issue.get("body") or ""),
        number=number,
        url=str(issue.get("url") or ""),
        state=str(state or issue.get("state") or "open").lower(),
        source=source,
    )


def issue_candidates_from_open(open_issues: Iterable[dict]) -> list[DedupCandidate]:
    return [_candidate_from_issue(issue, source="open_issue", state="open") for issue in open_issues]


def list_active_branch_issue_candidates(cfg: dict, repo: str, repo_path: Path) -> list[DedupCandidate]:
    branches = _active_agent_branches(repo_path)
    if not branches:
        return []
    root = Path(cfg.get("root_dir", ".")).expanduser()
    prompts_dir = root / "runtime" / "prompts"
    candidates: list[DedupCandidate] = []
    seen: set[str] = set()
    for branch in branches:
        task_id = _task_id_from_branch(branch)
        number, title = _origin_from_prompt(prompts_dir, task_id, repo)
        body = ""
        url = ""
        if number is not None:
            issue = _gh_issue_view(repo, number)
            if issue:
                title = str(issue.get("title") or title or "")
                body = str(issue.get("body") or "")
                url = str(issue.get("url") or "")
        if not title:
            title = _title_from_branch(branch)
        key = f"{number}:{title}:{branch}"
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            DedupCandidate(
                title=title,
                body=body,
                number=number,
                url=url,
                state="active_branch",
                source="active_branch",
                branch=branch,
            )
        )
    return candidates


def _active_agent_branches(repo_path: Path) -> list[str]:
    branches: list[str] = []
    for args in (
        ["branch", "--list", "agent/task-*", "--format", "%(refname:short)"],
        ["branch", "-r", "--list", "origin/agent/task-*", "--format", "%(refname:short)"],
    ):
        try:
            result = subprocess.run(["git", "-C", str(repo_path), *args], capture_output=True, text=True)
        except FileNotFoundError:
            continue
        if result.returncode != 0:
            continue
        branches.extend(line.strip() for line in result.stdout.splitlines() if line.strip())
    return sorted(set(branches))


def _task_id_from_branch(branch: str) -> str:
    match = _TASK_RE.search(branch or "")
    return match.group(1) if match else ""


def _origin_from_prompt(prompts_dir: Path, task_id: str, repo: str) -> tuple[int | None, str]:
    if not task_id:
        return None, ""
    prompt_path = prompts_dir / f"{task_id}.txt"
    if not prompt_path.exists():
        return None, ""
    text = prompt_path.read_text(encoding="utf-8", errors="replace")
    repo_match = re.search(r"^github_repo:\s*(\S+)\s*$", text, re.MULTILINE)
    if repo_match and repo_match.group(1).strip() != repo:
        return None, ""
    number = None
    number_match = re.search(r"^github_issue_number:\s*(\d+)\s*$", text, re.MULTILINE)
    if number_match:
        number = int(number_match.group(1))
    title = ""
    title_match = re.search(r"^github_issue_title:\s*['\"]?(.+?)['\"]?\s*$", text, re.MULTILINE)
    if title_match:
        title = title_match.group(1).strip()
    return number, title


def _gh_issue_view(repo: str, number: int) -> dict:
    try:
        result = subprocess.run(
            ["gh", "issue", "view", str(number), "--repo", repo, "--json", "number,title,body,url"],
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return {}
    if result.returncode != 0 or not result.stdout.strip():
        return {}
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _title_from_branch(branch: str) -> str:
    branch = branch.replace("origin/", "")
    tail = branch.split("/", 1)[-1]
    tail = _TASK_RE.sub("", tail).strip("-")
    return tail.replace("-", " ").strip()


def collect_dedup_candidates(
    cfg: dict,
    github_slug: str,
    repo_path: Path,
    open_issues: Iterable[dict],
) -> list[DedupCandidate]:
    candidates = issue_candidates_from_open(open_issues)
    candidates.extend(list_recently_closed_issues(github_slug, cfg, recently_closed_days(cfg, github_slug)))
    candidates.extend(list_active_branch_issue_candidates(cfg, github_slug, repo_path))
    seen: set[tuple[str, int | None, str]] = set()
    unique: list[DedupCandidate] = []
    for candidate in candidates:
        key = (candidate.source, candidate.number, candidate.title.lower().strip())
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
    return unique
