#!/usr/bin/env python3
"""Run real controller fault scenarios, emitting a bounded engineering-eval report."""

import json
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from orchestrator.reliability import SCENARIOS


def main():
    with tempfile.TemporaryDirectory() as directory:
        report = Path(directory) / "result.xml"
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests/test_reliability_scenarios.py",
                "-q",
                "--junitxml=" + str(report),
            ],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=90,
            check=False,
        )
        cases = ET.parse(report).getroot().findall(".//testcase")
        outcomes = {
            case.attrib["name"].removeprefix("test_"): not any(
                case.find(tag) is not None for tag in ("failure", "error", "skipped")
            )
            for case in cases
        }
        results = {name: outcomes.get(name, False) for name in sorted(SCENARIOS)}
        print(
            json.dumps(
                {
                    "samples": len(results),
                    "quality": sum(results.values()) / len(results),
                    "scenarios": results,
                }
            )
        )


if __name__ == "__main__":
    main()
