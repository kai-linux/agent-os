"""Controller-owned assurance records, sharing the delivery transaction boundary."""

from __future__ import annotations

import hashlib
import json
import math
import time
from contextlib import contextmanager
from uuid import uuid4

from orchestrator.delivery_store import (
    DeliveryConflict,
    DeliveryStore,
    _dump,
    store_path,
)
from orchestrator.privacy import redact_text


class ReliabilityStore:
    def __init__(self, cfg, *, clock=time.time):
        self.delivery = DeliveryStore(store_path(cfg), clock=clock)
        self.clock = clock
        with self.delivery._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS reliability_spans (
                    id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES goals(id),
                    revision INTEGER NOT NULL, parent_id TEXT REFERENCES reliability_spans(id),
                    kind TEXT NOT NULL, started_at REAL NOT NULL, finished_at REAL,
                    state TEXT NOT NULL, attributes TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reliability_usage (
                    receipt_key TEXT PRIMARY KEY, attempt_id TEXT NOT NULL REFERENCES attempts(id),
                    provider TEXT NOT NULL, account TEXT NOT NULL, request_id TEXT NOT NULL,
                    payload TEXT NOT NULL, actor TEXT NOT NULL, observed_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reliability_seals (
                    attempt_id TEXT PRIMARY KEY REFERENCES attempts(id),
                    receipt_keys TEXT NOT NULL, actor TEXT NOT NULL, observed_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reliability_memory (
                    tenant TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
                    source TEXT NOT NULL, actor TEXT NOT NULL, revision INTEGER NOT NULL,
                    expires_at REAL NOT NULL, PRIMARY KEY(tenant,key)
                );
                CREATE TABLE IF NOT EXISTS reliability_audit (
                    id INTEGER PRIMARY KEY, tenant TEXT NOT NULL, actor TEXT NOT NULL,
                    action TEXT NOT NULL, reference TEXT NOT NULL, observed_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reliability_evaluations (
                    id TEXT PRIMARY KEY, profile TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    report TEXT NOT NULL, passed INTEGER NOT NULL, observed_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reliability_approvals (
                    profile TEXT PRIMARY KEY, evaluation_id TEXT NOT NULL
                    REFERENCES reliability_evaluations(id), actor TEXT NOT NULL,
                    reason TEXT NOT NULL, observed_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS reliability_spans_goal ON reliability_spans(goal_id,started_at);
                CREATE INDEX IF NOT EXISTS reliability_evaluations_profile ON reliability_evaluations(profile,observed_at);
            """)

    def audit(self, tenant, actor, action, reference):
        with self.delivery._db() as db:
            self._audit(db, tenant, actor, action, reference)

    def _audit(self, db, tenant, actor, action, reference):
        db.execute(
            "INSERT INTO reliability_audit VALUES(NULL,?,?,?,?,?)",
            (tenant, actor, action, redact_text(str(reference))[:1000], self.clock()),
        )

    def start_span(
        self, goal_id, revision, kind, *, parent=None, attributes=None, key=None
    ):
        if kind not in {"worker", "model", "tool", "action", "verification", "control"}:
            raise ValueError("Unsupported span kind")
        attributes = dict(attributes or {})
        allowed = {
            "attempt_id",
            "action_id",
            "provider",
            "model",
            "request_id",
            "error_type",
            "control",
        }
        if set(attributes) - allowed:
            raise ValueError(
                "Trace attributes must not contain prompts, outputs or credentials"
            )
        attributes = {k: redact_text(str(v))[:300] for k, v in attributes.items()}
        key = key or uuid4().hex
        with self.delivery._db() as db:
            goal = self.delivery._goal(db, goal_id)
            if revision > goal["revision"] or revision < 1:
                raise DeliveryConflict("Invalid trace revision")
            if parent:
                p = db.execute(
                    "SELECT * FROM reliability_spans WHERE id=?", (parent,)
                ).fetchone()
                if not p or (p["goal_id"], p["revision"]) != (goal_id, revision):
                    raise DeliveryConflict(
                        "Trace parent belongs to another goal or revision"
                    )
            db.execute(
                "INSERT INTO reliability_spans VALUES(?,?,?,?,?,?,NULL,'running',?)",
                (key, goal_id, revision, parent, kind, self.clock(), _dump(attributes)),
            )
        return key

    def end_span(self, key, *, error=None):
        with self.delivery._db() as db:
            row = db.execute(
                "SELECT attributes FROM reliability_spans WHERE id=?", (key,)
            ).fetchone()
            if not row:
                return
            attrs = json.loads(row[0])
            if error:
                attrs["error_type"] = type(error).__name__
            db.execute(
                "UPDATE reliability_spans SET finished_at=?,state=?,attributes=? WHERE id=? AND finished_at IS NULL",
                (self.clock(), "error" if error else "ok", _dump(attrs), key),
            )

    @contextmanager
    def span(self, goal, kind, **kwargs):
        key = self.start_span(goal["id"], goal["revision"], kind, **kwargs)
        try:
            yield key
        except BaseException as exc:
            self.end_span(key, error=exc)
            raise
        else:
            self.end_span(key)

    def traces(self, goal_id):
        with self.delivery._db() as db:
            return [
                {
                    **dict(r),
                    "attributes": json.loads(r["attributes"]),
                    "trace_id": hashlib.sha256(
                        f"{r['goal_id']}:{r['revision']}".encode()
                    ).hexdigest()[:32],
                    "span_id": hashlib.sha256(r["id"].encode()).hexdigest()[:16],
                    "parent_span_id": hashlib.sha256(
                        r["parent_id"].encode()
                    ).hexdigest()[:16]
                    if r["parent_id"]
                    else None,
                }
                for r in db.execute(
                    "SELECT * FROM reliability_spans WHERE goal_id=? ORDER BY started_at,id",
                    (goal_id,),
                )
            ]

    def worker_parent(self, goal):
        spans = self.traces(goal["id"])
        return next(
            (
                s["id"]
                for s in reversed(spans)
                if s["kind"] == "worker" and s["revision"] == goal["revision"]
            ),
            None,
        )

    def usage(self, attempt_id, receipt, *, actor):
        """Only trusted billing collectors call this; worker result files never do."""
        required = {
            "provider",
            "account",
            "request_id",
            "model",
            "input_tokens",
            "output_tokens",
            "cost_nano_usd",
            "final",
        }
        if set(receipt) != required:
            raise ValueError("Usage receipt must match the measured-usage schema")
        for field in ("provider", "account", "request_id", "model"):
            if (
                not isinstance(receipt[field], str)
                or not 1 <= len(receipt[field]) <= 200
            ):
                raise ValueError("Usage identity fields must be bounded strings")
        for field in ("input_tokens", "output_tokens", "cost_nano_usd"):
            v = receipt[field]
            if field == "cost_nano_usd" and v is None and receipt["final"] is False:
                continue
            if type(v) is not int or not 0 <= v < 10**15:
                raise ValueError("Measured usage must be nonnegative bounded integers")
        if type(receipt["final"]) is not bool:
            raise ValueError("final must be a boolean")
        key = hashlib.sha256(
            _dump([receipt[k] for k in ("provider", "account", "request_id")]).encode()
        ).hexdigest()
        with self.delivery._db() as db:
            attempt = db.execute(
                "SELECT * FROM attempts WHERE id=?", (attempt_id,)
            ).fetchone()
            if not attempt:
                raise DeliveryConflict("Unknown attempt")
            old = db.execute(
                "SELECT * FROM reliability_usage WHERE receipt_key=?", (key,)
            ).fetchone()
            if old:
                previous = json.loads(old["payload"])
                if old["attempt_id"] != attempt_id:
                    raise DeliveryConflict(
                        "Provider request is already owned by another attempt"
                    )
                if previous == receipt:
                    return key
                if previous["final"]:
                    raise DeliveryConflict("Final billing receipt is immutable")
                if any(
                    previous[k] != receipt[k]
                    for k in ("provider", "account", "request_id", "model")
                ):
                    raise DeliveryConflict("Usage identity changed")
            if db.execute(
                "SELECT 1 FROM reliability_seals WHERE attempt_id=?", (attempt_id,)
            ).fetchone():
                raise DeliveryConflict(
                    "Sealed usage cannot acquire additional requests"
                )
            db.execute(
                "INSERT OR REPLACE INTO reliability_usage VALUES(?,?,?,?,?,?,?,?)",
                (
                    key,
                    attempt_id,
                    receipt["provider"],
                    receipt["account"],
                    receipt["request_id"],
                    _dump(receipt),
                    actor,
                    self.clock(),
                ),
            )
            self._audit(db, attempt["goal_id"], actor, "usage.receipt", key)
        return key

    def seal_usage(self, attempt_id, keys, *, actor):
        """An exact request manifest is required; one observed call is not full coverage."""
        if not isinstance(keys, list) or not keys or len(set(keys)) != len(keys):
            raise ValueError("Supply a nonempty unique provider-request manifest")
        with self.delivery._db() as db:
            attempt = db.execute(
                "SELECT * FROM attempts WHERE id=?", (attempt_id,)
            ).fetchone()
            if not attempt or attempt["state"] == "running":
                raise DeliveryConflict("Only a finished attempt can have sealed usage")
            rows = db.execute(
                "SELECT * FROM reliability_usage WHERE attempt_id=?", (attempt_id,)
            ).fetchall()
            receipts = [json.loads(r["payload"]) for r in rows]
            if set(keys) != {r["receipt_key"] for r in rows} or not all(
                r["final"] for r in receipts
            ):
                raise DeliveryConflict(
                    "Manifest is incomplete or receipts are not final"
                )
            db.execute(
                "INSERT OR IGNORE INTO reliability_seals VALUES(?,?,?,?)",
                (attempt_id, _dump(sorted(keys)), actor, self.clock()),
            )
            db.execute(
                "UPDATE attempts SET cost_usd=? WHERE id=?",
                (sum(r["cost_nano_usd"] for r in receipts) / 1e9, attempt_id),
            )
            self._audit(db, attempt["goal_id"], actor, "usage.seal", attempt_id)

    def put_memory(
        self, tenant, key, value, *, source, actor, ttl_seconds, expected_revision
    ):
        if (
            not key
            or len(key) > 200
            or not source
            or len(source) > 1000
            or len(value.encode()) > 16384
        ):
            raise ValueError("Memory requires bounded content, a key and provenance")
        if (
            isinstance(ttl_seconds, bool)
            or not math.isfinite(ttl_seconds)
            or not 0 < ttl_seconds <= 365 * 86400
        ):
            raise ValueError("Memory retention must be finite and at most one year")
        with self.delivery._db() as db:
            old = db.execute(
                "SELECT revision FROM reliability_memory WHERE tenant=? AND key=?",
                (tenant, key),
            ).fetchone()
            revision = old[0] if old else 0
            if expected_revision != revision:
                raise DeliveryConflict("Memory changed; read it before updating")
            db.execute(
                "INSERT OR REPLACE INTO reliability_memory VALUES(?,?,?,?,?,?,?)",
                (
                    tenant,
                    key,
                    redact_text(value),
                    redact_text(source),
                    actor,
                    revision + 1,
                    self.clock() + ttl_seconds,
                ),
            )
            self._audit(db, tenant, actor, "memory.write", key)
            return revision + 1

    def memory(self, tenant, *, actor):
        with self.delivery._db() as db:
            db.execute(
                "DELETE FROM reliability_memory WHERE expires_at<=?", (self.clock(),)
            )
            self._audit(db, tenant, actor, "memory.read", tenant)
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM reliability_memory WHERE tenant=? ORDER BY key",
                    (tenant,),
                )
            ]

    def delete_memory(self, tenant, key, *, actor):
        with self.delivery._db() as db:
            db.execute("PRAGMA secure_delete=ON")
            db.execute(
                "DELETE FROM reliability_memory WHERE tenant=? AND key=?", (tenant, key)
            )
            self._audit(db, tenant, actor, "memory.delete", key)

    def purge_memory(self):
        with self.delivery._db() as db:
            db.execute("PRAGMA secure_delete=ON")
            count = db.execute(
                "DELETE FROM reliability_memory WHERE expires_at<=?", (self.clock(),)
            ).rowcount
            if count:
                self._audit(db, "controller", "controller", "memory.expire", str(count))

    def evaluation(self, profile, fingerprint, report, passed, *, actor="controller"):
        ident = uuid4().hex
        with self.delivery._db() as db:
            db.execute(
                "INSERT INTO reliability_evaluations VALUES(?,?,?,?,?,?)",
                (ident, profile, fingerprint, _dump(report), int(passed), self.clock()),
            )
            self._audit(db, profile, actor, "evaluation.record", ident)
        return ident

    def evaluations(self, profile):
        with self.delivery._db() as db:
            rows = db.execute(
                "SELECT rowid,* FROM reliability_evaluations WHERE profile=? ORDER BY observed_at DESC,rowid DESC LIMIT 100",
                (profile,),
            ).fetchall()
            approval = db.execute(
                "SELECT * FROM reliability_approvals WHERE profile=?", (profile,)
            ).fetchone()
            baseline = None
            if approval:
                baseline = db.execute(
                    "SELECT * FROM reliability_evaluations WHERE id=?",
                    (approval["evaluation_id"],),
                ).fetchone()
            decode = lambda r: (
                {**dict(r), "report": json.loads(r["report"])} if r else None
            )
            return [decode(r) for r in rows], decode(baseline)

    def approve(self, profile, evaluation_id, *, actor, reason):
        if not reason.strip():
            raise ValueError("Release approval needs a reason")
        with self.delivery._db() as db:
            row = db.execute(
                "SELECT * FROM reliability_evaluations WHERE id=? AND profile=?",
                (evaluation_id, profile),
            ).fetchone()
            if not row or not row["passed"]:
                raise DeliveryConflict("Cannot approve missing or failed evaluations")
            evaluator = db.execute(
                "SELECT actor FROM reliability_audit WHERE action='evaluation.record' AND reference=? ORDER BY id DESC LIMIT 1",
                (evaluation_id,),
            ).fetchone()
            if evaluator and evaluator[0] == actor:
                raise DeliveryConflict(
                    "Release approval requires an identity other than the evaluator"
                )
            db.execute(
                "INSERT OR REPLACE INTO reliability_approvals VALUES(?,?,?,?,?)",
                (profile, evaluation_id, actor, redact_text(reason), self.clock()),
            )
            self._audit(db, profile, actor, "release.approve", evaluation_id)
