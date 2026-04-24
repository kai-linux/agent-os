from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.tool_registry import resolve_tools_for


def _cfg() -> dict:
    return {
        "tool_registry": {
            "mcp_servers": {
                "linear_mcp": {
                    "title": "Linear MCP",
                    "package": "@acme/mcp-linear",
                    "version": "1.2.3",
                    "sha256": "a" * 64,
                    "env": {"LINEAR_API_KEY": "${LINEAR_API_KEY}"},
                    "task_permissions": {
                        "groomer": ["issues:read", "issues:write"],
                        "research": ["issues:read"],
                    },
                }
            },
            "http_apis": {
                "receipt_ocr_api": {
                    "title": "Receipt OCR",
                    "base_url": "https://ocr.example.com/v1",
                    "credential_env": "OCR_API_KEY",
                    "task_permissions": {
                        "quality_harness": ["ocr:extract"],
                        "implementation": ["ocr:extract"],
                    },
                }
            },
        },
        "github_projects": {
            "proj": {
                "repos": [
                    {"key": "demo", "github_repo": "owner/repo", "enabled_tools": ["linear_mcp", "receipt_ocr_api"]},
                    {"key": "legacy", "github_repo": "owner/legacy"},
                ]
            }
        },
    }


def test_resolve_tools_for_repo_and_task_type_narrows_bundle():
    bundle = resolve_tools_for("demo", "groomer", _cfg())

    assert bundle["default_toolset_allowed"] is False
    assert [tool["id"] for tool in bundle["mcp_servers"]] == ["linear_mcp"]
    assert bundle["http_apis"] == []


def test_resolve_tools_for_quality_harness_does_not_leak_groomer_scope():
    bundle = resolve_tools_for("owner/repo", "quality_harness", _cfg())

    assert [tool["id"] for tool in bundle["http_apis"]] == ["receipt_ocr_api"]
    assert bundle["mcp_servers"] == []


def test_resolve_tools_for_repo_without_enabled_tools_falls_back_to_default_toolset():
    bundle = resolve_tools_for("legacy", "implementation", _cfg())

    assert bundle["default_toolset_allowed"] is True
    assert bundle["all_tools"] == []

