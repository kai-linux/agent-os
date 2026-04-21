"""Quality harness planning, fixture capture, and merge-gate helpers."""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import yaml

from orchestrator.gh_project import add_issue_comment, edit_issue_labels, gh_json


FIXED_SUITE_TAXONOMY = (
    "unit",
    "api_contract",
    "browser_e2e",
    "visual_regression",
    "multimodal_eval",
    "llm_judge_eval",
    "bot_conversation_eval",
)
FIELD_FAILURE_LABEL = "qa-fail"
FIELD_FAILURE_PROMPTED_LABEL = "qa-fail-prompted"
FIELD_FAILURE_PROMPT_MARKER = "<!-- quality-harness-field-failure-prompt -->"
QUALITY_HARNESS_FINDINGS_FILENAME = "quality_harness_findings.json"
QA_CAPTURE_PREFIX = "qa-fail-"

_MODALITY_SUITES = {
    "ocr": ["unit", "multimodal_eval"],
    "vision": ["unit", "multimodal_eval", "visual_regression"],
    "api": ["unit", "api_contract"],
    "frontend": ["unit", "browser_e2e", "visual_regression"],
    "bot": ["unit", "bot_conversation_eval"],
    "llm": ["unit", "llm_judge_eval"],
}
_SUITE_KEYWORDS = {
    "unit": ("pytest", "unittest", "spec", "test_"),
    "api_contract": ("openapi", "contract", "schema", "schemathesis", "swagger"),
    "browser_e2e": ("playwright", "cypress", "puppeteer", "selenium"),
    "visual_regression": ("percy", "loki", "argos", "happo", "visual"),
    "multimodal_eval": ("multimodal", "ocr", "vision", "fixture", "image"),
    "llm_judge_eval": ("promptfoo", "ragas", "judge", "eval", "llm"),
    "bot_conversation_eval": ("conversation", "transcript", "telegram", "discord", "slack"),
}
_MODALITY_PATTERNS = {
    "ocr": ("pytesseract", "easyocr", "ocrmypdf", "receipt", "tesseract"),
    "vision": ("opencv", "pillow", "torchvision", "imageio", "cv2"),
    "api": ("fastapi", "flask", "django", "express", "openapi", "graphql"),
    "frontend": ("react", "next", "vite", "vue", "svelte", "tailwind"),
    "bot": ("python-telegram-bot", "telebot", "discord.py", "slack_bolt", "bot"),
    "llm": ("openai", "anthropic", "langchain", "llm", "prompt", "completion"),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def quality_harness_log_path(cfg: dict) -> Path:
    root = Path(cfg.get("root_dir", ".")).expanduser()
    return root / "runtime" / "metrics" / QUALITY_HARNESS_FINDINGS_FILENAME


def resolve_quality_harness_config(cfg: dict, github_slug: str) -> dict:
    merged = dict(cfg.get("quality_harness") or {})
    for project_cfg in (cfg.get("github_projects") or {}).values():
        if not isinstance(project_cfg, dict):
            continue
        for repo_cfg in project_cfg.get("repos", []) or []:
            if repo_cfg.get("github_repo") != github_slug:
                continue
            override = repo_cfg.get("quality_harness")
            if isinstance(override, dict):
                updated = dict(merged)
                updated.update(override)
                merged = updated
            return merged
    return merged


def resolve_repo_local_path(cfg: dict, github_slug: str) -> Path | None:
    for project_cfg in (cfg.get("github_projects") or {}).values():
        if not isinstance(project_cfg, dict):
            continue
        for repo_cfg in project_cfg.get("repos", []) or []:
            if repo_cfg.get("github_repo") != github_slug:
                continue
            local_repo = str(repo_cfg.get("local_repo") or repo_cfg.get("path") or "").strip()
            if local_repo:
                return Path(local_repo).expanduser()
    return None


def quality_harness_enabled(cfg: dict, github_slug: str) -> bool:
    harness_cfg = resolve_quality_harness_config(cfg, github_slug)
    suites = harness_cfg.get("suites") or []
    return bool(harness_cfg.get("enabled")) and bool(suites)


def normalize_suite_list(values) -> list[str]:
    suites: list[str] = []
    for raw in values or []:
        suite = str(raw or "").strip().lower()
        if suite in FIXED_SUITE_TAXONOMY and suite not in suites:
            suites.append(suite)
    return suites


def detect_repo_modalities(repo_path: Path) -> dict:
    files = [p.relative_to(repo_path).as_posix() for p in repo_path.rglob("*") if p.is_file()]
    lowered_files = "\n".join(files).lower()
    package_json = _read_if_exists(repo_path / "package.json")
    pyproject = _read_if_exists(repo_path / "pyproject.toml")
    requirements = _read_if_exists(repo_path / "requirements.txt")
    combined = "\n".join([lowered_files, package_json.lower(), pyproject.lower(), requirements.lower()])

    modalities: list[str] = []
    evidence: dict[str, list[str]] = {}
    for modality, patterns in _MODALITY_PATTERNS.items():
        hits = [p for p in patterns if p in combined]
        if hits:
            modalities.append(modality)
            evidence[modality] = hits[:5]

    entrypoints = []
    for candidate in ("main.py", "app.py", "manage.py", "server.py", "package.json", "README.md"):
        if (repo_path / candidate).exists():
            entrypoints.append(candidate)

    frameworks = sorted({hit for hits in evidence.values() for hit in hits})
    return {
        "modalities": modalities,
        "frameworks": frameworks[:12],
        "entrypoints": entrypoints[:8],
        "evidence": evidence,
        "file_count": len(files),
    }


def recommend_suites(modalities: list[str]) -> list[str]:
    suites: list[str] = ["unit"]
    for modality in modalities:
        for suite in _MODALITY_SUITES.get(modality, []):
            if suite not in suites:
                suites.append(suite)
    return suites


def detect_existing_suite_coverage(repo_path: Path) -> list[str]:
    files = "\n".join(
        p.relative_to(repo_path).as_posix().lower()
        for p in repo_path.rglob("*")
        if p.is_file()
    )
    found: list[str] = []
    for suite, keywords in _SUITE_KEYWORDS.items():
        if any(keyword in files for keyword in keywords):
            found.append(suite)
    if (repo_path / "tests").exists() and "unit" not in found:
        found.append("unit")
    return found


def build_harness_plan(cfg: dict, github_slug: str, repo_path: Path) -> dict:
    harness_cfg = resolve_quality_harness_config(cfg, github_slug)
    repo_scan = detect_repo_modalities(repo_path)
    recommended = recommend_suites(repo_scan["modalities"])
    configured = normalize_suite_list(harness_cfg.get("suites"))
    existing = detect_existing_suite_coverage(repo_path)
    enabled = bool(harness_cfg.get("enabled"))
    coverage_basis = configured or recommended
    coverage_gaps = [suite for suite in coverage_basis if suite not in existing]
    return {
        "finding_type": "harness_plan",
        "timestamp": _now_iso(),
        "repo": github_slug,
        "repo_path": str(repo_path),
        "enabled": enabled,
        "modalities": repo_scan["modalities"],
        "frameworks": repo_scan["frameworks"],
        "entrypoints": repo_scan["entrypoints"],
        "recommended_suites": recommended,
        "configured_suites": configured,
        "existing_suites": existing,
        "coverage_gaps": coverage_gaps,
        "operator_approval_required": not bool(harness_cfg.get("operator_approved")),
        "score_threshold": float(harness_cfg.get("score_threshold", 0.9) or 0.9),
        "taxonomy": list(FIXED_SUITE_TAXONOMY),
    }


def write_harness_plan_finding(cfg: dict, finding: dict) -> Path:
    path = quality_harness_log_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {}
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
    findings = payload.get("findings") or []
    findings = [f for f in findings if f.get("repo") != finding.get("repo")]
    findings.append(finding)
    path.write_text(json.dumps({"findings": findings}, indent=2, sort_keys=True), encoding="utf-8")
    return path


def format_harness_plan_for_prompt(finding: dict) -> str:
    if not finding:
        return "(quality harness disabled)"
    lines = [
        f"Repo modalities: {', '.join(finding.get('modalities') or ['generic'])}",
        f"Recommended suites: {', '.join(finding.get('recommended_suites') or ['unit'])}",
        f"Configured suites: {', '.join(finding.get('configured_suites') or ['(none)'])}",
        f"Coverage gaps: {', '.join(finding.get('coverage_gaps') or ['(none)'])}",
    ]
    if finding.get("operator_approval_required"):
        lines.append(
            "Operator approval is still required for quality-harness implementation work. "
            "Do not auto-file implementation issues for harness skeletons yet."
        )
    return "\n".join(lines)


def issue_targets_quality_harness(issue: dict) -> bool:
    haystack = " ".join(
        [
            str(issue.get("title", "")),
            str(issue.get("body", "")),
            str(issue.get("task_type", "")),
            " ".join(str(x) for x in issue.get("labels", []) or []),
        ]
    ).lower()
    keywords = (
        "quality harness",
        "fixture",
        "regression fixture",
        "eval suite",
        "api_contract",
        "browser_e2e",
        "visual_regression",
        "multimodal_eval",
        "llm_judge_eval",
        "bot_conversation_eval",
    )
    return any(keyword in haystack for keyword in keywords)


def find_labeled_field_failures(repo: str) -> list[dict]:
    try:
        issues = gh_json(
            [
                "issue", "list", "-R", repo, "--state", "open",
                "--label", FIELD_FAILURE_LABEL,
                "--json", "number,title,body,labels",
                "--limit", "20",
            ]
        ) or []
    except Exception:
        return []
    filtered = []
    for issue in issues:
        labels = {
            str(label.get("name") if isinstance(label, dict) else label).strip().lower()
            for label in (issue.get("labels") or [])
        }
        if FIELD_FAILURE_PROMPTED_LABEL in labels:
            continue
        filtered.append(issue)
    return filtered


def build_field_failure_prompt(repo: str, issue: dict) -> str:
    return (
        f"🧪 Field failure captured\n"
        f"Repo: {repo}\n"
        f"Issue: #{issue.get('number')}\n"
        f"Title: {issue.get('title', '')}\n\n"
        f"Reply with `/qa-fail {repo} <suite> <fixture_id> {issue.get('number')}` to store a verified regression fixture."
    )


def mark_field_failure_prompted(repo: str, issue_number: int):
    add_issue_comment(
        repo,
        issue_number,
        (
            f"{FIELD_FAILURE_PROMPT_MARKER}\n"
            "Operator prompt sent for quality-harness capture. Reply via Telegram with "
            "`/qa-fail <repo> <suite> <fixture_id> <issue_number>` followed by INPUT / EXPECTED_OUTPUT."
        ),
    )
    try:
        edit_issue_labels(repo, issue_number, add=[FIELD_FAILURE_PROMPTED_LABEL], remove=[])
    except Exception:
        pass


def create_pending_qa_action(
    actions_dir: Path,
    *,
    chat_id: str,
    github_repo: str,
    suite: str,
    fixture_id: str,
    issue_number: int | None,
) -> Path:
    action = {
        "action_id": f"{QA_CAPTURE_PREFIX}{chat_id}",
        "type": "qa_fail_capture",
        "chat_id": str(chat_id),
        "github_repo": github_repo,
        "suite": suite,
        "fixture_id": fixture_id,
        "issue_number": issue_number,
        "created_at": _now_iso(),
    }
    path = actions_dir / f"{action['action_id']}.json"
    path.write_text(json.dumps(action, indent=2, sort_keys=True), encoding="utf-8")
    return path


def load_pending_qa_action(actions_dir: Path, chat_id: str) -> dict | None:
    path = actions_dir / f"{QA_CAPTURE_PREFIX}{chat_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def clear_pending_qa_action(actions_dir: Path, chat_id: str):
    path = actions_dir / f"{QA_CAPTURE_PREFIX}{chat_id}.json"
    if path.exists():
        path.unlink()


def parse_qa_failure_response(text: str) -> dict:
    sections = {}
    current = None
    for raw_line in str(text or "").splitlines():
        line = raw_line.rstrip()
        header = re.match(r"^(INPUT|EXPECTED_OUTPUT|VERIFIED|NOTES):\s*(.*)$", line.strip(), re.IGNORECASE)
        if header:
            current = header.group(1).upper()
            sections[current] = [header.group(2)] if header.group(2) else []
            continue
        if current:
            sections[current].append(line)
    parsed = {key.lower(): "\n".join(value).strip() for key, value in sections.items()}
    verified = parsed.get("verified", "").strip().lower() in {"yes", "true", "verified", "y"}
    return {
        "input": parsed.get("input", "").strip(),
        "expected_output": parsed.get("expected_output", "").strip(),
        "notes": parsed.get("notes", "").strip(),
        "verified": verified,
    }


def write_field_failure_fixture(
    repo_path: Path,
    *,
    suite: str,
    fixture_id: str,
    payload: dict,
    github_repo: str,
    issue_number: int | None = None,
) -> Path:
    verified = bool(payload.get("verified")) and bool(payload.get("expected_output"))
    base = repo_path / "tests" / "fixtures"
    if verified:
        fixture_dir = base / suite / fixture_id
    else:
        fixture_dir = base / "unverified" / suite / fixture_id
    fixture_dir.mkdir(parents=True, exist_ok=True)

    input_path = fixture_dir / "input.txt"
    input_path.write_text(str(payload.get("input", "")).strip() + "\n", encoding="utf-8")

    manifest = {
        "id": fixture_id,
        "suite": suite,
        "verified": verified,
        "source": "field_failure",
        "captured_at": _now_iso(),
        "github_repo": github_repo,
        "github_issue_number": issue_number,
        "input_file": input_path.name,
    }
    expected = str(payload.get("expected_output", "")).strip()
    if expected:
        expected_path = fixture_dir / "expected_output.txt"
        expected_path.write_text(expected + "\n", encoding="utf-8")
        manifest["expected_output_file"] = expected_path.name
    notes = str(payload.get("notes", "")).strip()
    if notes:
        (fixture_dir / "notes.txt").write_text(notes + "\n", encoding="utf-8")
        manifest["notes_file"] = "notes.txt"

    manifest_path = fixture_dir / "manifest.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return manifest_path


def validate_fixture_manifest(manifest_path: Path) -> tuple[bool, str | None]:
    try:
        data = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        return False, f"{manifest_path.parent.name}: unreadable manifest ({exc})"

    suite = str(data.get("suite", "")).strip().lower()
    if suite not in FIXED_SUITE_TAXONOMY:
        return False, f"{manifest_path.parent.name}: invalid suite"

    input_file = manifest_path.parent / str(data.get("input_file", "")).strip()
    if not input_file.exists():
        return False, f"{manifest_path.parent.name}: missing input_file"

    if data.get("verified"):
        expected_file = manifest_path.parent / str(data.get("expected_output_file", "")).strip()
        if not expected_file.exists():
            return False, f"{manifest_path.parent.name}: missing expected_output_file"
    return True, None


def _suite_fixture_manifests(repo_path: Path, suite: str) -> list[Path]:
    suite_dir = repo_path / "tests" / "fixtures" / suite
    if not suite_dir.exists():
        return []
    return sorted(suite_dir.glob("*/manifest.yaml"))


def _run_suite_command(repo_path: Path, command: str) -> dict:
    proc = subprocess.run(
        command,
        shell=True,
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        timeout=300,
    )
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    if stdout:
        try:
            payload = json.loads(stdout)
            if isinstance(payload, dict) and "score" in payload:
                return {
                    "score": float(payload.get("score", 0.0)),
                    "failing_fixtures": list(payload.get("failing_fixtures") or []),
                    "detail": payload.get("detail") or stderr or stdout[:200],
                }
        except Exception:
            pass
    return {
        "score": 1.0 if proc.returncode == 0 else 0.0,
        "failing_fixtures": [] if proc.returncode == 0 else [stderr or stdout or "suite command failed"],
        "detail": stderr or stdout[:200],
    }


def evaluate_quality_harness(repo_path: Path, harness_cfg: dict) -> dict:
    suites = normalize_suite_list(harness_cfg.get("suites"))
    threshold = float(harness_cfg.get("score_threshold", 0.9) or 0.9)
    commands = harness_cfg.get("suite_commands") or {}
    suite_results = []
    failing: list[str] = []
    for suite in suites:
        command = str(commands.get(suite, "")).strip()
        if command:
            result = _run_suite_command(repo_path, command)
        else:
            manifests = _suite_fixture_manifests(repo_path, suite)
            if not manifests:
                result = {
                    "score": 0.0,
                    "failing_fixtures": [f"{suite}:no_verified_fixtures"],
                    "detail": "No verified fixture manifests found.",
                }
            else:
                errors = [err for ok, err in (validate_fixture_manifest(p) for p in manifests) if not ok and err]
                result = {
                    "score": 1.0 if not errors else 0.0,
                    "failing_fixtures": errors,
                    "detail": "manifest validation",
                }
        suite_results.append({"suite": suite, **result})
        failing.extend(result.get("failing_fixtures") or [])

    score = round(sum(r["score"] for r in suite_results) / len(suite_results), 4) if suite_results else 1.0
    return {
        "enabled": bool(suites),
        "score": score,
        "threshold": threshold,
        "suite_results": suite_results,
        "failing_fixtures": failing,
        "passed": score >= threshold and not failing,
    }


def pr_deletes_fixtures(repo: str, pr_number: int) -> list[str]:
    try:
        files = gh_json(
            [
                "api",
                f"repos/{repo}/pulls/{pr_number}/files?per_page=100",
            ]
        ) or []
    except Exception:
        return []
    deleted = []
    for entry in files:
        if str(entry.get("status", "")).lower() != "removed":
            continue
        filename = str(entry.get("filename", "")).strip()
        if filename.startswith("tests/fixtures/"):
            deleted.append(filename)
    return deleted


def append_quality_harness_eval_record(cfg: dict, record: dict) -> Path:
    path = quality_harness_log_path(cfg).with_name("quality_harness_eval.jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"timestamp": _now_iso(), **record}, sort_keys=True) + "\n"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(existing + line)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return path


def _read_if_exists(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")
