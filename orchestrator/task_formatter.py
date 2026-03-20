"""Send raw issue text through an LLM to produce a well-structured task spec."""
from __future__ import annotations

import json
import os
import subprocess

FORMAT_PROMPT = """You are a task formatter for an AI coding agent orchestrator.
Given a raw GitHub issue (which may be poorly formatted notes, a quick one-liner,
or a well-structured spec), extract and structure it into a clean task specification.

Return ONLY valid JSON (no markdown fences, no commentary) with exactly these fields:

{{
  "goal": "Clear one-paragraph objective describing what needs to be done",
  "success_criteria": "- Criterion 1\\n- Criterion 2\\n- Criterion 3",
  "task_type": "implementation",
  "agent_preference": "auto",
  "constraints": "- Constraint 1\\n- Prefer minimal diffs",
  "context": "Any additional context, or None"
}}

Rules:
- goal: expand terse notes into a clear, actionable objective. Keep the original intent.
- success_criteria: infer 2-4 concrete, testable criteria from the goal if not stated.
- task_type: one of implementation, debugging, architecture, research, docs, browser_automation, design, content.
  Infer from the nature of the work.
- agent_preference: "auto" unless the issue explicitly names an agent.
- constraints: always include "Prefer minimal diffs". Add others only if stated or clearly implied.
- context: preserve any useful background info. Write "None" if there is nothing extra.
- Do NOT add scope or features that were not implied by the issue.

---
Issue title: {title}

Issue body:
{body}"""


def format_task(title: str, body: str, model: str | None = None) -> dict | None:
    """Return structured task dict, or None on failure (caller should fall back)."""
    prompt = FORMAT_PROMPT.format(title=title, body=body or "(no body)")
    claude_bin = os.environ.get("CLAUDE_BIN", "claude")
    codex_bin = os.environ.get("CODEX_BIN", "codex")
    model = model or os.environ.get("FORMATTER_MODEL", "haiku")

    try:
        errors = []
        text = ""
        result = subprocess.run(
            [claude_bin, "-p", prompt, "--model", model],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            text = result.stdout.strip()
        else:
            errors.append(f"Claude exit {result.returncode}: {result.stderr[:200]}")
            result = subprocess.run(
                [codex_bin, "exec", "--skip-git-repo-check", prompt],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode == 0:
                text = result.stdout.strip()
            else:
                errors.append(f"Codex exit {result.returncode}: {(result.stderr or result.stdout)[:200]}")
                raise RuntimeError(" | ".join(errors))

        # Strip markdown fences if present
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        data = json.loads(text)

        return {
            "goal": str(data.get("goal", title)).strip(),
            "success_criteria": str(data.get("success_criteria", "")).strip(),
            "task_type": str(data.get("task_type", "implementation")).strip().lower(),
            "agent_preference": str(data.get("agent_preference", "auto")).strip().lower(),
            "constraints": str(data.get("constraints", "- Prefer minimal diffs")).strip(),
            "context": str(data.get("context", "None")).strip(),
        }
    except Exception as e:
        print(f"Warning: LLM formatting failed ({e}), falling back to raw parse")
        return None
