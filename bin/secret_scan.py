#!/usr/bin/env python3
"""Fail closed on common credential shapes without echoing secret material."""
from __future__ import annotations

import argparse
import re
import subprocess
import sys

PATTERNS = {
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\b(?:gh[opusr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "Telegram token": re.compile(r"\b[0-9]{9,10}:[A-Za-z0-9_-]{35}\b"),
    "provider API key": re.compile(r"\b(?:sk-(?:or-v1-)?|sk-ant-api03-)[A-Za-z0-9_-]{20,}\b"),
    "bearer token": re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{20,}={0,2}\b"),
}


def added_lines(diff: str):
    current = "unknown"
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
        elif line.startswith("+") and not line.startswith("+++"):
            yield current, line[1:]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--staged", action="store_true")
    parser.add_argument("--base", default="")
    args = parser.parse_args()
    cmd = ["git", "diff", "--no-color", "-U0"]
    if args.staged:
        cmd.append("--cached")
    elif args.base:
        cmd.append(f"{args.base}...HEAD")
    else:
        parser.error("use --staged or --base")
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        print("secret scan could not read the diff; blocking", file=sys.stderr)
        return 2
    findings = []
    for path, line in added_lines(result.stdout):
        for label, pattern in PATTERNS.items():
            if pattern.search(line):
                findings.append((path, label))
    if findings:
        for path, label in sorted(set(findings)):
            print(f"possible {label} in added content: {path} (value redacted)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
