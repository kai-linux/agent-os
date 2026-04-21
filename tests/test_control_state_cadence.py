"""Coverage for the surgical YAML editor used by ``/repo cadence``."""
from pathlib import Path

import pytest

from orchestrator import control_state as cs


SAMPLE = """\
github_projects:
  eigendark-phase1:
    project_number: 9
    automation_mode: full
    repos:
      - key: "eigendark"
        github_repo: "kai-linux/eigendark"
        local_repo: "/home/kai/eigendark"
        default_base_branch: master
        sprint_cadence_days: 0.5
        groomer_cadence_days: 1
        plan_size: 5
        visibility: private
      - key: "eigendark-website"
        github_repo: "kai-linux/eigendark-website"
        local_repo: "/home/kai/eigendark-website"
        sprint_cadence_days: 0
        groomer_cadence_days: 0
        plan_size: 5
        visibility: private
"""


def _write(tmp_path: Path) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(SAMPLE, encoding="utf-8")
    return p


def test_set_repo_cadence_drops_explicit_groomer_override(tmp_path):
    cfg = _write(tmp_path)
    cs.set_repo_cadence(cfg, "eigendark", 1, "eigendark-phase1")
    text = cfg.read_text(encoding="utf-8")
    assert "sprint_cadence_days: 1" in text
    assert "groomer_cadence_days" not in text.split('- key: "eigendark-website"')[0]


def test_set_repo_cadence_accepts_fractional_days(tmp_path):
    cfg = _write(tmp_path)
    cs.set_repo_cadence(cfg, "eigendark", 0.5, "eigendark-phase1")
    text = cfg.read_text(encoding="utf-8")
    assert "sprint_cadence_days: 0.5" in text


def test_set_repo_cadence_writes_int_for_whole_numbers(tmp_path):
    cfg = _write(tmp_path)
    cs.set_repo_cadence(cfg, "eigendark", 2.0, "eigendark-phase1")
    text = cfg.read_text(encoding="utf-8")
    assert "sprint_cadence_days: 2\n" in text
    assert "sprint_cadence_days: 2.0" not in text


def test_set_repo_cadence_only_touches_target_repo(tmp_path):
    cfg = _write(tmp_path)
    cs.set_repo_cadence(cfg, "eigendark", 3, "eigendark-phase1")
    text = cfg.read_text(encoding="utf-8")
    website_block = text.split('- key: "eigendark-website"', 1)[1]
    assert "sprint_cadence_days: 0" in website_block
    assert "groomer_cadence_days: 0" in website_block


def test_set_repo_cadence_rejects_negative(tmp_path):
    cfg = _write(tmp_path)
    with pytest.raises(ValueError):
        cs.set_repo_cadence(cfg, "eigendark", -1, "eigendark-phase1")
