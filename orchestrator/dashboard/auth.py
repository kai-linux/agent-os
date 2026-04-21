"""Authentication helpers for the operator dashboard."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from orchestrator.audit_log import append_audit_event

LOCALHOST_BIND = "127.0.0.1"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
TAILSCALE_BACKEND = "tailscale"
SHARED_SECRET_BACKEND = "shared_secret"
SUPPORTED_BACKENDS = frozenset({TAILSCALE_BACKEND, SHARED_SECRET_BACKEND})
READONLY_FALLBACK = "dashboard_readonly_fallback"


class DashboardAuthError(ValueError):
    """Raised when dashboard auth configuration is invalid."""


class DashboardUnauthorizedError(PermissionError):
    """Raised when a dashboard request is not authorized."""


@dataclass(frozen=True)
class DashboardActor:
    actor: str
    backend: str


def dashboard_bind_address(cfg: Mapping[str, Any]) -> str:
    return str(cfg.get("dashboard_bind_address") or LOCALHOST_BIND).strip() or LOCALHOST_BIND


def dashboard_auth_backend(cfg: Mapping[str, Any]) -> str | None:
    raw = str(cfg.get("dashboard_auth_backend") or "").strip().lower()
    return raw or None


def _allowed_users(cfg: Mapping[str, Any]) -> set[str]:
    raw_users = cfg.get("dashboard_allowed_users") or []
    if not isinstance(raw_users, list):
        return set()
    return {str(user).strip() for user in raw_users if str(user).strip()}


def _shared_secret(cfg: Mapping[str, Any]) -> str:
    return str(cfg.get("dashboard_shared_secret") or "").strip()


def _readonly_fallback_enabled(cfg: Mapping[str, Any]) -> bool:
    return bool(cfg.get(READONLY_FALLBACK, False))


def _readonly_fallback_config() -> dict[str, Any]:
    return {
        "dashboard_bind_address": LOCALHOST_BIND,
        "dashboard_auth_backend": None,
        "dashboard_allowed_users": [],
        "dashboard_shared_secret": "",
        "dashboard_readonly_mode": True,
    }


def validate_dashboard_auth_config(cfg: Mapping[str, Any]) -> dict[str, Any]:
    bind_address = dashboard_bind_address(cfg)
    backend = dashboard_auth_backend(cfg)
    allowed_users = sorted(_allowed_users(cfg))
    shared_secret = _shared_secret(cfg)
    readonly_fallback = _readonly_fallback_enabled(cfg)

    if backend and backend not in SUPPORTED_BACKENDS:
        if readonly_fallback:
            return _readonly_fallback_config()
        raise DashboardAuthError(
            f"unsupported dashboard_auth_backend {backend!r}; expected one of {sorted(SUPPORTED_BACKENDS)}"
        )
    if backend == TAILSCALE_BACKEND and not allowed_users:
        if readonly_fallback:
            return _readonly_fallback_config()
        raise DashboardAuthError(
            "dashboard_auth_backend='tailscale' requires dashboard_allowed_users to contain at least one login"
        )
    if backend == SHARED_SECRET_BACKEND and not shared_secret:
        if readonly_fallback:
            return _readonly_fallback_config()
        raise DashboardAuthError(
            "dashboard_auth_backend='shared_secret' requires dashboard_shared_secret to be configured"
        )
    if bind_address != LOCALHOST_BIND and backend is None:
        if readonly_fallback:
            return _readonly_fallback_config()
        raise DashboardAuthError(
            "dashboard_bind_address must remain 127.0.0.1 unless dashboard_auth_backend is configured"
        )

    return {
        "dashboard_bind_address": bind_address,
        "dashboard_auth_backend": backend,
        "dashboard_allowed_users": allowed_users,
        "dashboard_shared_secret": shared_secret,
        "dashboard_readonly_mode": False,
    }


class DashboardAuth:
    def __init__(self, cfg: Mapping[str, Any]):
        self.cfg = dict(validate_dashboard_auth_config(cfg))
        self.bind_address = self.cfg["dashboard_bind_address"]
        self.backend = self.cfg["dashboard_auth_backend"]
        self.allowed_users = set(self.cfg["dashboard_allowed_users"])
        self.shared_secret = self.cfg["dashboard_shared_secret"]
        self.readonly_mode = bool(self.cfg.get("dashboard_readonly_mode", False))

    @property
    def local_reads_allowed_without_auth(self) -> bool:
        return self.bind_address == LOCALHOST_BIND

    def authenticate(self, headers: Mapping[str, Any] | None) -> DashboardActor | None:
        if self.backend == TAILSCALE_BACKEND:
            login = str((headers or {}).get("Tailscale-User-Login") or "").strip()
            if login and login in self.allowed_users:
                return DashboardActor(actor=login, backend=TAILSCALE_BACKEND)
            return None

        if self.backend == SHARED_SECRET_BACKEND:
            header = str((headers or {}).get("Authorization") or "").strip()
            scheme, _, token = header.partition(" ")
            if scheme.lower() != "bearer":
                return None
            token = token.strip()
            if token and token == self.shared_secret:
                return DashboardActor(actor="shared_secret", backend=SHARED_SECRET_BACKEND)
            return None

        return None

    def require_read(self, headers: Mapping[str, Any] | None) -> DashboardActor | None:
        actor = self.authenticate(headers)
        if actor is not None:
            return actor
        if self.local_reads_allowed_without_auth:
            return None
        raise DashboardUnauthorizedError("dashboard read requires authentication")

    def require_write(self, headers: Mapping[str, Any] | None) -> DashboardActor:
        actor = self.authenticate(headers)
        if actor is None:
            raise DashboardUnauthorizedError("dashboard write requires authentication")
        return actor

    def authorize_request(
        self,
        method: str,
        headers: Mapping[str, Any] | None,
    ) -> DashboardActor | None:
        normalized = str(method or "GET").upper()
        if normalized in SAFE_METHODS:
            return self.require_read(headers)
        return self.require_write(headers)

    def audit_write(self, cfg: Mapping[str, Any], actor: DashboardActor, action: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        event_ts = datetime.now(timezone.utc).isoformat()
        return append_audit_event(
            dict(cfg),
            "dashboard_write",
            {
                "actor": actor.actor,
                "action": str(action),
                "payload": dict(payload or {}),
                "ts": event_ts,
                "auth_backend": actor.backend,
            },
        )


def build_dashboard_auth(cfg: Mapping[str, Any]) -> DashboardAuth:
    return DashboardAuth(cfg)
