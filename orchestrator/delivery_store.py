"""Durable goal ownership, scheduling gates, evidence and delivery outbox.

The database is the authority for new managed work. Mailbox files are execution
requests and GitHub/Telegram are projections, not independent completion votes.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
STATES = TERMINAL | {"backlog", "ready", "running", "waiting", "verifying", "paused"}
KINDS = {"program", "project", "milestone", "task"}


def validate_contract(contract):
    if not isinstance(contract, dict):
        raise ValueError("Delivery contract must be an object")
    checks = contract.get("checks", [])
    if not isinstance(checks, list) or any(
        not isinstance(c, dict)
        or not isinstance(c.get("id"), str)
        or not c["id"].strip()
        for c in checks
    ):
        raise ValueError("Every acceptance check needs a nonempty string id")
    if len({c["id"] for c in checks}) != len(checks):
        raise ValueError("Acceptance check ids must be unique")
    for field in ("budget_usd", "max_attempts", "max_parallel"):
        value = contract.get(field)
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(f"{field} must be a positive finite number")
        if field != "budget_usd" and value is not None and not isinstance(value, int):
            raise ValueError(f"{field} must be an integer")
    grants = contract.get("allowed_actions", [])
    if not isinstance(grants, list):
        raise ValueError("allowed_actions must be a list")
    for grant in grants:
        if (
            not isinstance(grant, dict)
            or not isinstance(grant.get("capability"), str)
            or not isinstance(grant.get("target"), str)
        ):
            raise ValueError("Action delegation needs a capability and target")
        limit = grant.get("max_calls", 1)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("Action max_calls must be a positive integer")


class DeliveryConflict(ValueError):
    """A stale revision, unmet gate, or conflicting execution request."""

    def __init__(self, message, *, code="conflict"):
        super().__init__(message)
        self.code = code


def _dump(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False)


def goal_id(source: str) -> str:
    return "g-" + hashlib.sha256(source.encode()).hexdigest()[:20]


def store_path(cfg: dict) -> Path:
    return (
        Path(cfg.get("root_dir", ".")).expanduser()
        / "runtime"
        / "delivery"
        / "state.sqlite3"
    )


class DeliveryStore:
    def __init__(self, path: str | Path, *, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS goals (
                    id TEXT PRIMARY KEY, source TEXT UNIQUE NOT NULL,
                    parent_id TEXT REFERENCES goals(id), revision INTEGER NOT NULL,
                    title TEXT NOT NULL, original TEXT NOT NULL, kind TEXT NOT NULL,
                    contract TEXT NOT NULL, metadata TEXT NOT NULL,
                    state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
                    wake_at REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dependencies (
                    goal_id TEXT REFERENCES goals(id), requires_id TEXT REFERENCES goals(id),
                    PRIMARY KEY(goal_id, requires_id)
                );
                CREATE TABLE IF NOT EXISTS revisions (
                    goal_id TEXT REFERENCES goals(id), revision INTEGER NOT NULL,
                    title TEXT NOT NULL, original TEXT NOT NULL, contract TEXT NOT NULL,
                    created_at REAL NOT NULL, PRIMARY KEY(goal_id, revision)
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    id TEXT PRIMARY KEY, goal_id TEXT NOT NULL REFERENCES goals(id),
                    revision INTEGER NOT NULL, worker TEXT NOT NULL,
                    started_at REAL NOT NULL, finished_at REAL, lease_until REAL NOT NULL,
                    state TEXT NOT NULL, reserved_usd REAL NOT NULL,
                    cost_usd REAL, result TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS evidence (
                    goal_id TEXT REFERENCES goals(id), revision INTEGER NOT NULL,
                    check_id TEXT NOT NULL, passed INTEGER NOT NULL,
                    evaluator TEXT NOT NULL, detail TEXT NOT NULL, observed_at REAL NOT NULL,
                    PRIMARY KEY(goal_id, revision, check_id)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, goal_id TEXT REFERENCES goals(id),
                    revision INTEGER NOT NULL, kind TEXT NOT NULL,
                    payload TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    event_id INTEGER REFERENCES events(id), channel TEXT NOT NULL,
                    due_at REAL NOT NULL, lease_until REAL NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0, delivered_at REAL,
                    last_error TEXT NOT NULL DEFAULT '', receipt TEXT,
                    PRIMARY KEY(event_id, channel)
                );
                CREATE TABLE IF NOT EXISTS actions (
                    id TEXT PRIMARY KEY, goal_id TEXT REFERENCES goals(id), revision INTEGER NOT NULL,
                    capability TEXT NOT NULL, target TEXT NOT NULL, request TEXT NOT NULL,
                    state TEXT NOT NULL, receipt TEXT, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS health (
                    component TEXT PRIMARY KEY, observed_at REAL NOT NULL, state TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS goals_parent ON goals(parent_id);
                CREATE INDEX IF NOT EXISTS attempts_goal ON attempts(goal_id);
                CREATE INDEX IF NOT EXISTS outbox_due ON outbox(delivered_at, due_at);
            """)
            db.execute(
                "INSERT OR IGNORE INTO revisions SELECT id,revision,title,original,contract,updated_at FROM goals"
            )
        os.chmod(self.path, 0o600)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            if db.in_transaction:
                db.commit()
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _goal(db, ident):
        row = db.execute("SELECT * FROM goals WHERE id=?", (ident,)).fetchone()
        if row is None:
            raise DeliveryConflict(f"Unknown goal: {ident}")
        item = dict(row)
        for field in ("contract", "metadata"):
            item[field] = json.loads(item[field])
        return item

    @staticmethod
    def _revision(goal, revision):
        if goal["revision"] != revision:
            raise DeliveryConflict("Goal revision changed; old work must not continue")

    def _event(self, db, goal, kind, payload):
        now = self.clock()
        cursor = db.execute(
            "INSERT INTO events(goal_id,revision,kind,payload,created_at) VALUES(?,?,?,?,?)",
            (goal["id"], goal["revision"], kind, _dump(payload), now),
        )
        for channel in ("github", "telegram"):
            db.execute(
                "INSERT INTO outbox(event_id,channel,due_at) VALUES(?,?,?)",
                (cursor.lastrowid, channel, now),
            )
        return cursor.lastrowid

    def _state(self, db, goal, state, reason="", wake_at=None):
        if state not in STATES:
            raise ValueError(f"Invalid goal state: {state}")
        if (
            goal["state"] == state
            and goal["reason"] == reason
            and goal["wake_at"] == wake_at
        ):
            return
        db.execute(
            "UPDATE goals SET state=?,reason=?,wake_at=?,updated_at=? WHERE id=?",
            (state, reason, wake_at, self.clock(), goal["id"]),
        )
        self._event(
            db, goal, "state", {"from": goal["state"], "state": state, "reason": reason}
        )
        goal.update(state=state, reason=reason, wake_at=wake_at)

    @staticmethod
    def _lineage(db, ident):
        found = []
        while ident:
            row = DeliveryStore._goal(db, ident)
            found.append(row)
            ident = row["parent_id"]
        return found

    @staticmethod
    def _descendants(db, ident):
        return [
            row[0]
            for row in db.execute(
                """
            WITH RECURSIVE tree(id) AS (
                SELECT id FROM goals WHERE id=?
                UNION ALL SELECT g.id FROM goals g JOIN tree t ON g.parent_id=t.id
            ) SELECT id FROM tree
        """,
                (ident,),
            )
        ]

    def upsert(
        self,
        source,
        title,
        original,
        *,
        kind="task",
        contract=None,
        metadata=None,
        parent_id=None,
        ready=True,
    ):
        if kind not in KINDS:
            raise ValueError("Goal kind must be program, project, milestone, or task")
        contract = dict(contract or {})
        validate_contract(contract)
        ident = goal_id(source)
        with self._db() as db:
            row = db.execute(
                "SELECT id FROM goals WHERE source=?", (source,)
            ).fetchone()
            if row:
                old = self._goal(db, ident)
                if old["original"] != original or old["contract"] != contract:
                    raise DeliveryConflict(
                        "Changed intent requires an explicit revision, not redispatch"
                    )
                return old
            if parent_id:
                parent = self._goal(db, parent_id)
                if parent["state"] in TERMINAL:
                    raise DeliveryConflict("Cannot add scope to a finished parent")
                metadata = {**(metadata or {}), "parent_revision": parent["revision"]}
            now = self.clock()
            db.execute(
                """INSERT INTO goals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ident,
                    source,
                    parent_id,
                    1,
                    title,
                    original,
                    kind,
                    _dump(contract),
                    _dump(metadata or {}),
                    "ready" if ready else "backlog",
                    "",
                    None,
                    now,
                    now,
                ),
            )
            goal = self._goal(db, ident)
            db.execute(
                "INSERT INTO revisions VALUES(?,?,?,?,?,?)",
                (ident, 1, title, original, _dump(contract), now),
            )
            self._event(db, goal, "created", {"state": goal["state"], "kind": kind})
            return goal

    def get(self, ident):
        with self._db() as db:
            return self._goal(db, ident)

    def list_goals(self):
        with self._db() as db:
            return [
                self._goal(db, r[0])
                for r in db.execute("SELECT id FROM goals ORDER BY created_at,id")
            ]

    def depend(self, ident, requires):
        with self._db() as db:
            goal, other = self._goal(db, ident), self._goal(db, requires)
            if db.execute(
                "SELECT 1 FROM dependencies WHERE goal_id=? AND requires_id=?",
                (ident, requires),
            ).fetchone():
                return
            if goal["state"] in TERMINAL or goal["state"] == "running":
                raise DeliveryConflict(
                    "Cannot change dependencies during or after execution"
                )
            family = {x["id"] for x in self._lineage(db, ident)}
            reverse_family = {x["id"] for x in self._lineage(db, requires)}
            if requires in family or ident in reverse_family:
                raise DeliveryConflict(
                    "A dependency cannot be itself, its parent or its child"
                )
            reachable = {
                r[0]
                for r in db.execute(
                    """
                WITH RECURSIVE edges(a,b) AS (
                    SELECT goal_id,requires_id FROM dependencies
                    UNION SELECT parent_id,id FROM goals WHERE parent_id IS NOT NULL
                ), deps(id) AS (
                    SELECT ? UNION SELECT e.b FROM edges e JOIN deps ON e.a=deps.id
                ) SELECT id FROM deps
            """,
                    (requires,),
                )
            }
            if ident in reachable:
                raise DeliveryConflict("Dependency cycle")
            db.execute(
                "INSERT OR IGNORE INTO dependencies VALUES(?,?)", (ident, other["id"])
            )

    def begin_attempt(
        self, ident, revision, key, worker, *, lease_seconds=2700, reserve_usd=0
    ):
        if not math.isfinite(reserve_usd) or reserve_usd < 0 or lease_seconds <= 0:
            raise ValueError("Invalid attempt reservation")
        with self._db() as db:
            goal = self._goal(db, ident)
            self._revision(goal, revision)
            existing = db.execute(
                "SELECT * FROM attempts WHERE id=?", (key,)
            ).fetchone()
            if existing:
                raise DeliveryConflict(
                    "Attempt already exists; reconcile its result before retrying"
                )
            if goal["state"] != "ready":
                raise DeliveryConflict(f"Goal is {goal['state']}: {goal['reason']}")
            if goal["kind"] != "task":
                raise DeliveryConflict(
                    "Container goals require a delivery plan, not a single coding worker"
                )
            children = db.execute(
                "SELECT 1 FROM goals WHERE parent_id=? AND json_extract(metadata,'$.parent_revision')=? LIMIT 1",
                (ident, revision),
            ).fetchone()
            if children:
                raise DeliveryConflict(
                    "Container goals are managed through their child work"
                )
            pending = db.execute(
                """SELECT g.id FROM dependencies d JOIN goals g ON g.id=d.requires_id
                                    WHERE d.goal_id=? AND g.state!='succeeded'""",
                (ident,),
            ).fetchall()
            if pending:
                raise DeliveryConflict(
                    "Dependencies are not verified: "
                    + ", ".join(r[0] for r in pending),
                    code="dependency",
                )
            for ancestor in self._lineage(db, ident):
                if ancestor["state"] in TERMINAL | {"paused", "waiting", "backlog"}:
                    raise DeliveryConflict(
                        f"Ancestor is {ancestor['state']}: {ancestor['id']}",
                        code="ancestor_paused",
                    )
                if db.execute(
                    "SELECT 1 FROM dependencies d JOIN goals g ON g.id=d.requires_id WHERE d.goal_id=? AND g.state!='succeeded'",
                    (ancestor["id"],),
                ).fetchone():
                    raise DeliveryConflict(
                        "An ancestor has unverified prerequisites", code="dependency"
                    )
                ids = self._descendants(db, ancestor["id"])
                slots = ",".join("?" for _ in ids)
                attempts = db.execute(
                    f"SELECT * FROM attempts WHERE goal_id IN ({slots})", ids
                ).fetchall()
                limit = ancestor["contract"].get(
                    "max_attempts", 8 if ancestor["kind"] == "task" else 64
                )
                if len(attempts) >= limit:
                    raise DeliveryConflict(
                        f"Attempt budget exhausted: {ancestor['id']}"
                    )
                active = sum(a["state"] == "running" for a in attempts)
                if active >= ancestor["contract"].get(
                    "max_parallel", 1 if ancestor["kind"] == "task" else 4
                ):
                    raise DeliveryConflict(
                        f"Execution capacity occupied: {ancestor['id']}",
                        code="capacity",
                    )
                budget = ancestor["contract"].get("budget_usd")
                if budget is not None:
                    if reserve_usd <= 0:
                        raise DeliveryConflict(
                            "A cost reservation is required for budgeted work"
                        )
                    spent = sum(
                        a["cost_usd"]
                        if a["cost_usd"] is not None
                        else a["reserved_usd"]
                        for a in attempts
                    )
                    if spent + reserve_usd > budget:
                        raise DeliveryConflict(
                            f"Cost budget exhausted: {ancestor['id']}"
                        )
                if ancestor["id"] != ident:
                    self._state(db, ancestor, "running", "Managing child delivery")
            now = self.clock()
            db.execute(
                "INSERT INTO attempts(id,goal_id,revision,worker,started_at,lease_until,state,reserved_usd) VALUES(?,?,?,?,?,?,?,?)",
                (
                    key,
                    ident,
                    revision,
                    worker,
                    now,
                    now + lease_seconds,
                    "running",
                    reserve_usd,
                ),
            )
            self._state(db, goal, "running", f"Worker: {worker}")
            return key

    def finish_attempt(self, key, result, *, cost_usd=None):
        if cost_usd is not None and (not math.isfinite(cost_usd) or cost_usd < 0):
            raise ValueError("Invalid recorded cost")
        with self._db() as db:
            row = db.execute("SELECT * FROM attempts WHERE id=?", (key,)).fetchone()
            if not row:
                raise DeliveryConflict("Unknown attempt")
            if row["state"] != "running":
                return
            goal = self._goal(db, row["goal_id"])
            db.execute(
                "UPDATE attempts SET state='finished',finished_at=?,result=?,cost_usd=? WHERE id=?",
                (self.clock(), _dump(result), cost_usd, key),
            )
            if goal["revision"] != row["revision"] or goal["state"] in TERMINAL | {
                "paused"
            }:
                self._event(db, goal, "stale_result", {"attempt": key})
                return
            self._state(
                db, goal, "verifying", "Checking the requested outcome independently"
            )

    def wait(self, ident, revision, reason, *, wake_at=None):
        if not reason.strip():
            raise ValueError("Waiting requires a concrete reason or question")
        with self._db() as db:
            goal = self._goal(db, ident)
            self._revision(goal, revision)
            if goal["state"] in TERMINAL | {"paused"}:
                return
            self._state(db, goal, "waiting", reason, wake_at)

    def control(
        self, ident, action, *, actor, note="", original=None, contract=None, title=None
    ):
        if not actor.strip():
            raise ValueError("An authenticated actor is required")
        if action == "revise":
            if original is None or contract is None:
                raise ValueError(
                    "Revision needs original intent and a complete contract"
                )
            validate_contract(contract)
        with self._db() as db:
            goal = self._goal(db, ident)
            if action in {"cancel", "revise"}:
                if goal["state"] == "succeeded":
                    raise DeliveryConflict(
                        "Create a new goal instead of rewriting an accepted outcome"
                    )
                for child_id in self._descendants(db, ident):
                    child = self._goal(db, child_id)
                    if child_id != ident and child["state"] not in TERMINAL:
                        self._state(
                            db, child, "cancelled", f"Parent {action} by {actor}"
                        )
                if action == "revise":
                    metadata = dict(goal["metadata"])
                    execution_keys = {
                        "worktree",
                        "branch",
                        "task_id",
                        "mailbox_payload",
                        "delivery_plan",
                        "plan_materialized",
                        "prepared_commit",
                        "last_continued_commit",
                        "pending_source",
                        "pr_url",
                        "pr_delivery_pending",
                        "pr_delivery_tries",
                        "pr_retry_at",
                        "planning_attempts",
                    }
                    metadata["prior_execution"] = {
                        key: metadata.pop(key)
                        for key in execution_keys
                        if key in metadata
                    }
                    db.execute(
                        "UPDATE goals SET revision=revision+1,title=?,original=?,contract=?,metadata=? WHERE id=?",
                        (
                            title or goal["title"],
                            original,
                            _dump(contract),
                            _dump(metadata),
                            ident,
                        ),
                    )
                    db.execute("DELETE FROM dependencies WHERE goal_id=?", (ident,))
                    goal = self._goal(db, ident)
                    db.execute(
                        "INSERT INTO revisions VALUES(?,?,?,?,?,?)",
                        (
                            ident,
                            goal["revision"],
                            goal["title"],
                            original,
                            _dump(contract),
                            self.clock(),
                        ),
                    )
                    self._state(
                        db,
                        goal,
                        "ready",
                        "Revised scope; prior children require replanning",
                    )
                else:
                    self._state(db, goal, "cancelled", note or "Cancelled by operator")
            elif action == "pause":
                if goal["state"] in TERMINAL:
                    raise DeliveryConflict("Goal has finished")
                self._state(db, goal, "paused", note or "Paused by operator")
            elif action in {"resume", "answer"}:
                if goal["state"] not in {"waiting", "paused", "backlog"}:
                    raise DeliveryConflict(
                        "Only waiting, paused or backlog goals can resume"
                    )
                if not note.strip():
                    raise ValueError("Explain what changed before resuming")
                self._state(db, goal, "ready", note)
            else:
                raise ValueError("Unknown control action")
            self._event(
                db, goal, "decision", {"actor": actor, "action": action, "note": note}
            )
            return self._goal(db, ident)

    def record_evidence(self, ident, revision, check_id, passed, evaluator, detail):
        with self._db() as db:
            goal = self._goal(db, ident)
            self._revision(goal, revision)
            ids = {c["id"] for c in goal["contract"].get("checks", [])} | {
                "human_acceptance"
            }
            if check_id not in ids:
                raise ValueError("Evidence must address an existing acceptance check")
            db.execute(
                "INSERT OR REPLACE INTO evidence VALUES(?,?,?,?,?,?,?)",
                (
                    ident,
                    revision,
                    check_id,
                    int(bool(passed)),
                    evaluator,
                    _dump(detail),
                    self.clock(),
                ),
            )

    def verify(self, ident):
        with self._db() as db:
            goal = self._goal(db, ident)
            if goal["state"] in TERMINAL | {"paused", "backlog"}:
                return goal["state"] == "succeeded"
            if any(
                g["state"] in TERMINAL | {"paused", "waiting", "backlog"}
                for g in self._lineage(db, ident)[1:]
            ):
                return False
            if db.execute(
                "SELECT 1 FROM attempts WHERE goal_id=? AND state='running'", (ident,)
            ).fetchone():
                return False
            if db.execute(
                """SELECT 1 FROM dependencies d JOIN goals g ON g.id=d.requires_id
                             WHERE d.goal_id=? AND g.state!='succeeded'""",
                (ident,),
            ).fetchone():
                return False
            children = [
                self._goal(db, r[0])
                for r in db.execute("SELECT id FROM goals WHERE parent_id=?", (ident,))
            ]
            children = [
                c
                for c in children
                if c["metadata"].get("parent_revision", 1) == goal["revision"]
            ]
            if any(c["state"] != "succeeded" for c in children):
                return False
            ids = self._descendants(db, ident)
            slots = ",".join("?" for _ in ids)
            if db.execute(
                f"SELECT 1 FROM actions WHERE goal_id IN ({slots}) AND state='uncertain'",
                ids,
            ).fetchone():
                self._state(
                    db,
                    goal,
                    "waiting",
                    "Reconcile uncertain external actions before accepting delivery",
                )
                return False
            evidence = {
                r["check_id"]: r
                for r in db.execute(
                    "SELECT * FROM evidence WHERE goal_id=? AND revision=?",
                    (ident, goal["revision"]),
                )
            }
            checks = goal["contract"].get("checks", [])
            required = {c["id"] for c in checks} or {"human_acceptance"}
            missing = {
                k for k in required if k not in evidence or not evidence[k]["passed"]
            }
            if missing:
                if children or goal["state"] == "verifying":
                    human = {c["id"] for c in checks if c.get("type") == "human"} or (
                        {"human_acceptance"} if not checks else set()
                    )
                    if missing <= human:
                        self._state(
                            db,
                            goal,
                            "waiting",
                            "Human acceptance required: review the deliverable, then /goal accept "
                            + ident
                            + " <reason>",
                        )
                    else:
                        self._state(
                            db,
                            goal,
                            "verifying",
                            "Awaiting acceptance evidence: "
                            + ", ".join(sorted(missing)),
                        )
                return False
            self._state(db, goal, "succeeded", "Acceptance checks verified")
            return True

    def tick(self):
        with self._db() as db:
            now = self.clock()
            for row in db.execute(
                "SELECT * FROM attempts WHERE state='running' AND lease_until<?", (now,)
            ).fetchall():
                db.execute(
                    "UPDATE attempts SET state='lost',finished_at=? WHERE id=?",
                    (now, row["id"]),
                )
                goal = self._goal(db, row["goal_id"])
                if goal["revision"] == row["revision"] and goal["state"] == "running":
                    self._state(
                        db,
                        goal,
                        "waiting",
                        "Worker lease expired; reconcile progress before retrying",
                    )
            for row in db.execute(
                "SELECT id FROM goals WHERE state='waiting' AND wake_at<=?", (now,)
            ).fetchall():
                lineage = self._lineage(db, row[0])
                if all(
                    g["state"] not in TERMINAL | {"paused", "waiting", "backlog"}
                    for g in lineage[1:]
                ):
                    self._state(db, lineage[0], "ready", "Scheduled wait elapsed")

    def prepare_action(self, ident, revision, key, capability, target, request):
        """Reserve one authorized external effect; never automatically repeat uncertainty."""
        with self._db() as db:
            goal = self._goal(db, ident)
            self._revision(goal, revision)
            if any(
                g["state"] in TERMINAL | {"paused", "waiting", "backlog"}
                for g in self._lineage(db, ident)
            ):
                raise DeliveryConflict(
                    "Goal is not authorized to act in its current state"
                )
            for owner in self._lineage(db, ident):
                if owner["parent_id"] and "allowed_actions" not in owner["contract"]:
                    continue
                allowed = owner["contract"].get("allowed_actions", [])
                grant = next(
                    (
                        a
                        for a in allowed
                        if a.get("capability") == capability
                        and a.get("target") == target
                    ),
                    None,
                )
                if not grant:
                    raise DeliveryConflict(
                        "Action and target lack explicit delegation in the goal ancestry"
                    )
                ids = self._descendants(db, owner["id"])
                slots = ",".join("?" for _ in ids)
                count = db.execute(
                    f"SELECT COUNT(*) FROM actions WHERE goal_id IN ({slots}) AND capability=? AND target=? AND id!=?",
                    [*ids, capability, target, key],
                ).fetchone()[0]
                if count >= int(grant.get("max_calls", 1)):
                    raise DeliveryConflict("Delegated action limit reached")
            row = db.execute("SELECT * FROM actions WHERE id=?", (key,)).fetchone()
            if row:
                if (
                    row["goal_id"],
                    row["revision"],
                    row["capability"],
                    row["target"],
                    row["request"],
                ) != (ident, revision, capability, target, _dump(request)):
                    raise DeliveryConflict("Action key reused for different work")
                return dict(row)
            db.execute(
                "INSERT INTO actions VALUES(?,?,?,?,?,?,?,NULL,?)",
                (
                    key,
                    ident,
                    revision,
                    capability,
                    target,
                    _dump(request),
                    "uncertain",
                    self.clock(),
                ),
            )
            return {"id": key, "state": "reserved"}

    def confirm_action(self, key, receipt):
        if not receipt:
            raise ValueError("External actions need a durable receipt")
        with self._db() as db:
            if not db.execute("SELECT 1 FROM actions WHERE id=?", (key,)).fetchone():
                raise DeliveryConflict("Unknown action")
            db.execute(
                "UPDATE actions SET state='confirmed',receipt=?,updated_at=? WHERE id=?",
                (_dump(receipt), self.clock(), key),
            )

    def list_actions(self, ident):
        with self._db() as db:
            self._goal(db, ident)
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id,revision,capability,target,state FROM actions WHERE goal_id=?",
                    (ident,),
                )
            ]

    def claim_outbox(self, *, limit=30):
        with self._db() as db:
            now = self.clock()
            rows = db.execute(
                """SELECT o.*,e.goal_id,e.revision,e.kind,e.payload FROM outbox o
                JOIN events e ON e.id=o.event_id WHERE o.delivered_at IS NULL
                AND o.due_at<=? AND o.lease_until<=? ORDER BY o.event_id LIMIT ?""",
                (now, now, limit),
            ).fetchall()
            for row in rows:
                db.execute(
                    "UPDATE outbox SET lease_until=?,attempts=attempts+1 WHERE event_id=? AND channel=?",
                    (now + 120, row["event_id"], row["channel"]),
                )
            return [dict(r) for r in rows]

    def finish_delivery(self, event_id, channel, *, receipt=None, error=None):
        with self._db() as db:
            if error is None:
                db.execute(
                    "UPDATE outbox SET delivered_at=?,receipt=?,lease_until=0,last_error='' WHERE event_id=? AND channel=?",
                    (self.clock(), _dump(receipt), event_id, channel),
                )
            else:
                row = db.execute(
                    "SELECT attempts FROM outbox WHERE event_id=? AND channel=?",
                    (event_id, channel),
                ).fetchone()
                db.execute(
                    "UPDATE outbox SET due_at=?,lease_until=0,last_error=? WHERE event_id=? AND channel=?",
                    (
                        self.clock() + min(3600, 2 ** min(row[0], 11) * 15),
                        str(error)[:300],
                        event_id,
                        channel,
                    ),
                )

    def snapshot(self):
        with self._db() as db:
            goals = [
                self._goal(db, r[0])
                for r in db.execute("SELECT id FROM goals ORDER BY created_at")
            ]
            return {
                "schema": "agent-os.delivery.v1",
                "generated_at": self.clock(),
                "goals": goals,
                "attempts": [dict(r) for r in db.execute("SELECT * FROM attempts")],
                "evidence": [dict(r) for r in db.execute("SELECT * FROM evidence")],
                "dependencies": [
                    dict(r) for r in db.execute("SELECT * FROM dependencies")
                ],
                "events": [
                    dict(r) for r in db.execute("SELECT * FROM events ORDER BY id")
                ],
                "pending_notifications": db.execute(
                    "SELECT COUNT(*) FROM outbox WHERE delivered_at IS NULL"
                ).fetchone()[0],
                "oldest_pending_notification": db.execute(
                    "SELECT MIN(e.created_at) FROM outbox o JOIN events e ON e.id=o.event_id WHERE o.delivered_at IS NULL"
                ).fetchone()[0],
                "uncertain_actions": db.execute(
                    "SELECT COUNT(*) FROM actions WHERE state='uncertain'"
                ).fetchone()[0],
                "health": [dict(r) for r in db.execute("SELECT * FROM health")],
            }

    def execution_allowed(self, ident, revision):
        with self._db() as db:
            lineage = self._lineage(db, ident)
            self._revision(lineage[0], revision)
            return all(
                g["state"] not in TERMINAL | {"paused", "waiting", "backlog"}
                for g in lineage
            )

    def remember(self, ident, *, actor, note, category="decision"):
        if (
            category not in {"decision", "risk", "preference", "observation"}
            or not actor
            or not note.strip()
        ):
            raise ValueError("Memory needs a category, source actor and nonempty note")
        with self._db() as db:
            goal = self._goal(db, ident)
            self._event(
                db, goal, "memory", {"actor": actor, "category": category, "note": note}
            )

    def context(self, ident):
        with self._db() as db:
            lineage = self._lineage(db, ident)
            ids = [g["id"] for g in lineage]
            slots = ",".join("?" for _ in ids)
            records = db.execute(
                f"SELECT * FROM events WHERE goal_id IN ({slots}) AND kind IN ('decision','memory') ORDER BY id DESC LIMIT 20",
                ids,
            ).fetchall()
            attempts = db.execute(
                "SELECT worker,revision,state,result,finished_at FROM attempts WHERE goal_id=? ORDER BY started_at DESC LIMIT 3",
                (ident,),
            ).fetchall()
            return {
                "lineage": lineage,
                "decisions": [dict(r) for r in records],
                "prior_attempts": [dict(r) for r in attempts],
            }

    def bind_execution(self, ident, revision, metadata):
        with self._db() as db:
            goal = self._goal(db, ident)
            self._revision(goal, revision)
            goal["metadata"].update(metadata)
            db.execute(
                "UPDATE goals SET metadata=? WHERE id=?",
                (_dump(goal["metadata"]), ident),
            )

    def retry(self, ident, revision, reason):
        with self._db() as db:
            goal = self._goal(db, ident)
            self._revision(goal, revision)
            if goal["state"] != "verifying" or not reason.strip():
                raise DeliveryConflict("Only a reconciled attempt may propose a retry")
            if db.execute(
                "SELECT 1 FROM actions WHERE goal_id=? AND state='uncertain'", (ident,)
            ).fetchone():
                raise DeliveryConflict("Uncertain side effects require reconciliation")
            self._state(db, goal, "ready", reason)

    def heartbeat(self, component, state="ok"):
        with self._db() as db:
            db.execute(
                "INSERT OR REPLACE INTO health VALUES(?,?,?)",
                (component, self.clock(), state),
            )

    def await_verification(self, ident, revision, reason):
        with self._db() as db:
            goal = self._goal(db, ident)
            self._revision(goal, revision)
            if goal["state"] not in TERMINAL | {"paused", "backlog"}:
                self._state(db, goal, "verifying", reason)
