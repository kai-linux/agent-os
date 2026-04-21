from __future__ import annotations

import json

import pytest

from orchestrator.dashboard.auth import (
    LOCALHOST_BIND,
    DashboardUnauthorizedError,
    build_dashboard_auth,
    validate_dashboard_auth_config,
)


def test_validate_dashboard_auth_config_defaults_to_local_bind():
    validated = validate_dashboard_auth_config({})

    assert validated["dashboard_bind_address"] == LOCALHOST_BIND
    assert validated["dashboard_auth_backend"] is None


def test_validate_dashboard_auth_config_rejects_remote_bind_without_auth():
    with pytest.raises(ValueError, match="dashboard_bind_address must remain 127.0.0.1"):
        validate_dashboard_auth_config({"dashboard_bind_address": "0.0.0.0"})


def test_validate_dashboard_auth_config_can_fallback_to_readonly_mode():
    validated = validate_dashboard_auth_config(
        {
            "dashboard_bind_address": "0.0.0.0",
            "dashboard_readonly_fallback": True,
        }
    )

    assert validated["dashboard_bind_address"] == LOCALHOST_BIND
    assert validated["dashboard_auth_backend"] is None
    assert validated["dashboard_readonly_mode"] is True


def test_tailscale_backend_requires_allowed_users():
    with pytest.raises(ValueError, match="dashboard_allowed_users"):
        validate_dashboard_auth_config({"dashboard_auth_backend": "tailscale"})


def test_shared_secret_backend_requires_secret():
    with pytest.raises(ValueError, match="dashboard_shared_secret"):
        validate_dashboard_auth_config({"dashboard_auth_backend": "shared_secret"})


def test_tailscale_backend_allows_allowed_login_and_rejects_others():
    auth = build_dashboard_auth(
        {
            "dashboard_auth_backend": "tailscale",
            "dashboard_allowed_users": ["alice@example.com"],
            "dashboard_bind_address": "100.64.0.1",
        }
    )

    actor = auth.require_write({"Tailscale-User-Login": "alice@example.com"})

    assert actor.actor == "alice@example.com"
    with pytest.raises(DashboardUnauthorizedError):
        auth.require_write({"Tailscale-User-Login": "bob@example.com"})


def test_shared_secret_backend_validates_bearer_token():
    auth = build_dashboard_auth(
        {
            "dashboard_auth_backend": "shared_secret",
            "dashboard_shared_secret": "top-secret",
            "dashboard_bind_address": "0.0.0.0",
        }
    )

    actor = auth.require_write({"Authorization": "Bearer top-secret"})

    assert actor.actor == "shared_secret"
    with pytest.raises(DashboardUnauthorizedError):
        auth.require_write({"Authorization": "Bearer wrong"})


def test_local_reads_allow_unauthenticated_access_but_writes_do_not():
    auth = build_dashboard_auth({})

    assert auth.require_read({}) is None
    with pytest.raises(DashboardUnauthorizedError):
        auth.require_write({})


def test_readonly_fallback_mode_stays_read_only():
    auth = build_dashboard_auth(
        {
            "dashboard_auth_backend": "shared_secret",
            "dashboard_bind_address": "0.0.0.0",
            "dashboard_readonly_fallback": True,
        }
    )

    assert auth.readonly_mode is True
    assert auth.require_read({}) is None
    with pytest.raises(DashboardUnauthorizedError):
        auth.require_write({"Authorization": "Bearer anything"})


def test_remote_reads_require_authentication():
    auth = build_dashboard_auth(
        {
            "dashboard_auth_backend": "shared_secret",
            "dashboard_shared_secret": "top-secret",
            "dashboard_bind_address": "0.0.0.0",
        }
    )

    with pytest.raises(DashboardUnauthorizedError):
        auth.require_read({})


def test_authorize_request_treats_get_as_read_and_post_as_write():
    auth = build_dashboard_auth({})

    assert auth.authorize_request("GET", {}) is None
    with pytest.raises(DashboardUnauthorizedError):
        auth.authorize_request("POST", {})


def test_audit_write_logs_actor_action_payload_and_ts(tmp_path):
    root_dir = tmp_path / "repo-root"
    root_dir.mkdir()
    auth = build_dashboard_auth(
        {
            "root_dir": str(root_dir),
            "dashboard_auth_backend": "tailscale",
            "dashboard_allowed_users": ["alice@example.com"],
            "dashboard_bind_address": "100.64.0.1",
        }
    )
    actor = auth.require_write({"Tailscale-User-Login": "alice@example.com"})

    record = auth.audit_write(
        {"root_dir": str(root_dir)},
        actor,
        action="rerun_job",
        payload={"job": "queue"},
    )

    audit_path = root_dir / "runtime" / "audit" / "audit.jsonl"
    payload = json.loads(audit_path.read_text(encoding="utf-8").strip())["payload"]

    assert record["event_type"] == "dashboard_write"
    assert payload["actor"] == "alice@example.com"
    assert payload["action"] == "rerun_job"
    assert payload["payload"] == {"job": "queue"}
    assert payload["ts"]
