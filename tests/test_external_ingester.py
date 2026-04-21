from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import orchestrator.external_ingester as ext


def test_run_external_ingester_writes_normalized_records(tmp_path, monkeypatch):
    cfg = {
        "root_dir": str(tmp_path),
        "external_signals": {
            "enabled": True,
            "sources": [
                {
                    "name": "Sentry incidents",
                    "enabled": True,
                    "type": "sentry_json",
                    "repo": "owner/repo",
                    "url": "https://ops.example.com/feed.json",
                },
                {
                    "name": "Support feed",
                    "enabled": True,
                    "type": "rss",
                    "repo": "owner/repo",
                    "url": "https://status.example.com/feed.xml",
                    "kind": "support",
                },
            ],
        },
    }

    monkeypatch.setattr(
        ext,
        "_read_json",
        lambda url, headers=None, timeout=20: {
            "items": [
                {
                    "title": "Payments failing for checkout",
                    "message": "500s from payment provider",
                    "permalink": "https://ops.example.com/issues/1",
                    "timestamp": "2026-04-21T08:00:00Z",
                    "level": "error",
                }
            ]
        },
    )
    monkeypatch.setattr(
        ext,
        "_read_text",
        lambda url, headers=None, timeout=20: """\
            <rss><channel><item>
              <title>Customer support backlog rising</title>
              <description>Multiple users report delayed responses.</description>
              <link>https://status.example.com/tickets/9</link>
              <pubDate>2026-04-21T09:00:00Z</pubDate>
            </item></channel></rss>
        """,
    )

    ext.run_external_ingester(cfg, "owner/repo")

    path = tmp_path / "runtime" / "metrics" / ext.EXTERNAL_SIGNALS_FILENAME
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 2
    assert rows[0]["repo"] == "owner/repo"
    assert {"source", "kind", "severity", "title", "body", "url", "ts", "repo"} <= set(rows[0])
    assert any(row["kind"] == "error" and row["severity"] == "high" for row in rows)
    assert any(row["kind"] == "support" for row in rows)


def test_run_external_ingester_rate_limits_sources(tmp_path, monkeypatch):
    cfg = {
        "root_dir": str(tmp_path),
        "external_signals": {
            "enabled": True,
            "fetch_interval_minutes": 30,
            "sources": [
                {
                    "name": "Untrusted GitHub mentions",
                    "enabled": True,
                    "type": "github_mentions",
                    "repo": "owner/repo",
                    "query": "\"owner/repo\" type:issue",
                }
            ],
        },
    }

    calls = {"count": 0}

    def fake_run(cmd, capture_output, text, timeout):
        calls["count"] += 1
        payload = {
            "items": [
                {
                    "title": "owner/repo is broken after latest deploy",
                    "body": "The release regressed login.",
                    "html_url": "https://github.com/example/other/issues/1",
                    "updated_at": "2026-04-21T10:00:00Z",
                    "user": {"login": "external-user"},
                }
            ]
        }
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(ext.subprocess, "run", fake_run)

    ext.run_external_ingester(cfg, "owner/repo")
    ext.run_external_ingester(cfg, "owner/repo")

    assert calls["count"] == 1


def test_format_external_signals_for_prompt_orders_high_severity_first():
    records = [
        {
            "source": "support",
            "kind": "support",
            "severity": "medium",
            "title": "Customers ask for audit logs",
            "body": "Feature request from support queue.",
            "ts": "2026-04-21T10:00:00+00:00",
        },
        {
            "source": "sentry",
            "kind": "error",
            "severity": "high",
            "title": "Checkout crashes on submit",
            "body": "Unhandled exception in payment flow.",
            "ts": "2026-04-21T09:00:00+00:00",
        },
    ]

    text = ext.format_external_signals_for_prompt(records)

    assert text.splitlines()[0].startswith("- [high] error via sentry")
