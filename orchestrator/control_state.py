"""Runtime control-state helpers shared by the orchestrator and its bot.

Three flag-file scopes live under ``runtime/state/``:

  - ``disabled``                        global kill-switch (existing)
  - ``repo_disabled/<key>``             skip a single repo in dispatcher
  - ``job_disabled/<job_name>``         skip a single cron entrypoint

Plus surgical YAML editors for the two settings that are commonly toggled
from chat: per-project ``automation_mode`` and per-repo
``sprint_cadence_days`` / ``groomer_cadence_days``. The editors are
line-based on purpose — config.yaml carries lots of inline comments that
PyYAML would destroy on round-trip, and pulling in ruamel.yaml just for
two settings is overkill.
"""

import re
from pathlib import Path

VALID_AUTOMATION_MODES = ("full", "dispatcher_only")


# ---------------------------------------------------------------------------
# Flag-file helpers
# ---------------------------------------------------------------------------

def _state_dir(root: Path) -> Path:
    return root / "runtime" / "state"


def repo_disabled_dir(root: Path) -> Path:
    return _state_dir(root) / "repo_disabled"


def job_disabled_dir(root: Path) -> Path:
    return _state_dir(root) / "job_disabled"


def is_repo_disabled(root: Path, repo_key: str) -> bool:
    return (repo_disabled_dir(root) / _safe(repo_key)).exists()


def set_repo_disabled(root: Path, repo_key: str, disabled: bool):
    flag = repo_disabled_dir(root) / _safe(repo_key)
    if disabled:
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text("", encoding="utf-8")
    elif flag.exists():
        flag.unlink()


def is_job_disabled(root: Path, job_name: str) -> bool:
    return (job_disabled_dir(root) / _safe(job_name)).exists()


def set_job_disabled(root: Path, job_name: str, disabled: bool):
    flag = job_disabled_dir(root) / _safe(job_name)
    if disabled:
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text("", encoding="utf-8")
    elif flag.exists():
        flag.unlink()


def _safe(name: str) -> str:
    """Filesystem-safe slug — the bot accepts user input here so we sanitize."""
    return re.sub(r"[^A-Za-z0-9_.\-]", "_", (name or "").strip())[:128]


# ---------------------------------------------------------------------------
# Config introspection
# ---------------------------------------------------------------------------

def list_repos(cfg: dict) -> list[dict]:
    """Flatten github_projects → list of repo dicts with project context."""
    out = []
    projects = cfg.get("github_projects", {}) or {}
    for project_key, project_cfg in projects.items():
        if not isinstance(project_cfg, dict):
            continue
        mode = str(project_cfg.get("automation_mode", "full")).strip()
        for repo_cfg in project_cfg.get("repos", []) or []:
            if not isinstance(repo_cfg, dict):
                continue
            out.append({
                "key": str(repo_cfg.get("key", "")).strip(),
                "github_repo": str(repo_cfg.get("github_repo", "")).strip(),
                "project_key": project_key,
                "automation_mode": mode,
                "sprint_cadence_days": repo_cfg.get("sprint_cadence_days"),
                "groomer_cadence_days": repo_cfg.get("groomer_cadence_days"),
            })
    return out


def find_repo(cfg: dict, repo_key: str) -> dict | None:
    for r in list_repos(cfg):
        if r["key"] == repo_key:
            return r
    return None


# Known cron jobs (basename of bin/run_*.sh without the run_ prefix and .sh).
# The telegram_control poller is intentionally excluded — disabling it would
# lock the operator out of /on.
KNOWN_JOBS = (
    "autopull",
    "dispatcher",
    "queue",
    "pr_monitor",
    "agent_scorer",
    "log_analyzer",
    "daily_digest",
    "backlog_groomer",
    "strategic_planner",
    "export_github_evidence",
    "product_inspector",
    "health_gate_report",
    "public_dashboard",
    "adoption_report",
)

# Jobs that can never be turned off via /job (would brick the control plane).
PROTECTED_JOBS = frozenset({"telegram_control"})


# ---------------------------------------------------------------------------
# Surgical YAML edits for config.yaml
# ---------------------------------------------------------------------------

def _read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines(keepends=True)


def _write_lines_atomic(path: Path, lines: list[str]):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(lines), encoding="utf-8")
    tmp.replace(path)


def _project_block_range(lines: list[str], project_key: str) -> tuple[int, int]:
    """Return (start_idx, end_idx_exclusive) for a project block.

    Project blocks are nested under ``github_projects:`` at 2-space indent.
    The block ends at the next sibling key or any line less indented than 4.
    """
    in_projects = False
    start = -1
    end = len(lines)
    project_re = re.compile(r"^(  )([A-Za-z0-9_\-]+):\s*$")
    for i, line in enumerate(lines):
        if line.startswith("github_projects:"):
            in_projects = True
            continue
        if not in_projects:
            continue
        m = project_re.match(line)
        if m:
            if start >= 0:
                end = i
                break
            if m.group(2) == project_key:
                start = i
                continue
        # Top-level (0-indent) key terminates github_projects entirely.
        if start >= 0 and line and not line[0].isspace() and line.strip():
            end = i
            break
    if start < 0:
        raise ValueError(f"project not found in config: {project_key}")
    return start, end


def set_project_automation_mode(cfg_path: Path, project_key: str, mode: str) -> None:
    if mode not in VALID_AUTOMATION_MODES:
        raise ValueError(f"invalid mode {mode!r}; expected one of {VALID_AUTOMATION_MODES}")
    lines = _read_lines(cfg_path)
    start, end = _project_block_range(lines, project_key)
    pat = re.compile(r"^(\s*automation_mode:\s*)(\S+)(.*)$")
    for i in range(start, end):
        m = pat.match(lines[i])
        if m:
            lines[i] = f"{m.group(1)}{mode}{m.group(3)}\n" if lines[i].endswith("\n") else f"{m.group(1)}{mode}{m.group(3)}"
            _write_lines_atomic(cfg_path, lines)
            return
    raise ValueError(f"automation_mode line not found inside project {project_key}")


def _repo_entry_range(lines: list[str], project_start: int, project_end: int, repo_key: str) -> tuple[int, int]:
    """Find the line range for a specific repo entry inside a project block.

    Repo entries are list items beginning with ``      - key: "<key>"``.
    """
    list_item_re = re.compile(r"^(\s*)-\s+key:\s*\"?([^\"\s]+)\"?\s*$")
    start = -1
    indent_len = 0
    for i in range(project_start, project_end):
        m = list_item_re.match(lines[i])
        if m and m.group(2) == repo_key:
            start = i
            indent_len = len(m.group(1))
            break
    if start < 0:
        raise ValueError(f"repo entry {repo_key!r} not found in project block")

    end = project_end
    for j in range(start + 1, project_end):
        line = lines[j]
        if not line.strip():
            continue
        # Next list item at same indent OR any sibling at smaller indent ends entry.
        leading = len(line) - len(line.lstrip())
        if leading <= indent_len:
            end = j
            break
    return start, end


def set_repo_cadence(cfg_path: Path, repo_key: str, days: float, project_key: str) -> None:
    if days < 0:
        raise ValueError("days must be >= 0")
    # Store integers as ints (e.g. `1`) and fractional days as floats (e.g. `0.5`)
    # so the YAML stays idiomatic and parses back to the same type.
    days_val = int(days) if float(days).is_integer() else float(days)
    lines = _read_lines(cfg_path)
    p_start, p_end = _project_block_range(lines, project_key)
    r_start, r_end = _repo_entry_range(lines, p_start, p_end, repo_key)
    targets = ("sprint_cadence_days", "groomer_cadence_days")
    pat = re.compile(rf"^(\s*)({'|'.join(targets)}):\s*\S+(.*)$")
    found = 0
    for i in range(r_start, r_end):
        m = pat.match(lines[i])
        if m:
            lines[i] = f"{m.group(1)}{m.group(2)}: {days_val}{m.group(3)}\n" if lines[i].endswith("\n") else f"{m.group(1)}{m.group(2)}: {days_val}{m.group(3)}"
            found += 1
    if not found:
        raise ValueError(f"no cadence keys found for repo {repo_key!r}")
    _write_lines_atomic(cfg_path, lines)
