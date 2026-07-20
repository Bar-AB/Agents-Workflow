"""SQLite store — the single source of truth (spec §2).

Every agent and (later) the Phase-2 UI reads and writes here. Design notes:

- `events` is an immutable, append-only audit trail (spec §11): every prompt,
  output, verdict, and human decision lands here with a timestamp.
- `attempts` carries per-invocation metrics (tokens, wall time, cost) — the
  data source for the visualization layer (spec §6, §8).
- Schema is deliberately boring SQL so a later move to Postgres is a
  connection-string change, not a rewrite (spec §9).
- The loop is resumable (spec §11): all state needed to continue lives here.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from .models import Task, TaskStatus, TestResult, Verdict

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    goal TEXT NOT NULL,
    acceptance_criteria TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    risk_level INTEGER NOT NULL DEFAULT 1,
    revision_count INTEGER NOT NULL DEFAULT 0,
    worker_role TEXT NOT NULL DEFAULT 'worker',
    validator_role TEXT NOT NULL DEFAULT 'validator',
    output TEXT NOT NULL DEFAULT '',
    escalation_reason TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    kind TEXT NOT NULL,               -- 'worker' | 'validator'
    agent_role TEXT NOT NULL,
    model TEXT NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    output TEXT NOT NULL DEFAULT '',
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS verdicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    attempt_id INTEGER REFERENCES attempts(id),
    kind TEXT NOT NULL,               -- approve | revise | escalate
    confidence REAL NOT NULL,
    reasoning TEXT NOT NULL DEFAULT '',
    tests_passed INTEGER,             -- NULL = n/a
    created_at REAL NOT NULL
);

-- Append-only audit log. Never UPDATE or DELETE rows here.
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}'
);

-- Two-tier memory (spec §7): tier = 'project' | 'loop'.
-- Writes are human-auditable via the events log; `approved` gates reads.
CREATE TABLE IF NOT EXISTS memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tier TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    hit_count INTEGER NOT NULL DEFAULT 0,
    approved INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    UNIQUE(tier, key)
);

-- Real, executed test results (spec §5). Authoritative over the validator's
-- self-reported TESTS: field.
CREATE TABLE IF NOT EXISTS test_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    attempt_id INTEGER REFERENCES attempts(id),
    status TEXT NOT NULL,             -- pass | fail | na | error
    exit_code INTEGER,
    summary TEXT NOT NULL DEFAULT '',
    stdout_tail TEXT NOT NULL DEFAULT '',
    duration_s REAL NOT NULL DEFAULT 0.0,
    created_at REAL NOT NULL
);
"""


class _LockedConnection:
    """Serializes access to one sqlite3 connection.

    The Phase-2 dashboard reads this store from HTTP request threads while the
    loop writes from the main thread, so the connection is opened with
    `check_same_thread=False`. That alone is not enough — it only disables the
    ownership check — so every statement goes through this lock. Wrapping the
    connection rather than each method means no call site can forget to lock.
    """

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._lock = threading.RLock()

    def execute(self, sql: str, params: tuple = ()) -> "_LockedCursor":
        with self._lock:
            cur = self._conn.execute(sql, params)
            # Materialize under the lock: rows read later, off-lock, would
            # race another thread's use of the same connection.
            rows = cur.fetchall() if cur.description else []
            return _LockedCursor(cur.lastrowid, rows)

    def executescript(self, sql: str) -> None:
        with self._lock:
            self._conn.executescript(sql)

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class _LockedCursor:
    """Cursor view over rows already fetched under the connection lock."""

    def __init__(self, lastrowid: int | None, rows: list[sqlite3.Row]):
        self.lastrowid = lastrowid
        self._rows = rows

    def fetchone(self) -> sqlite3.Row | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[sqlite3.Row]:
        return self._rows


class Store:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        raw = sqlite3.connect(self.db_path, check_same_thread=False)
        raw.row_factory = sqlite3.Row
        self._conn = _LockedConnection(raw)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- tasks ---------------------------------------------------------------

    def add_task(self, task: Task) -> int:
        now = time.time()
        cur = self._conn.execute(
            "INSERT INTO tasks (title, goal, acceptance_criteria, status,"
            " risk_level, worker_role, validator_role, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (task.title, task.goal, task.acceptance_criteria,
             task.status.value, task.risk_level, task.worker_role,
             task.validator_role, now, now),
        )
        task.id = cur.lastrowid
        self.log_event(task.id, "task_defined", {
            "title": task.title, "goal": task.goal,
            "acceptance_criteria": task.acceptance_criteria,
            "risk_level": task.risk_level,
        })
        self._conn.commit()
        return task.id

    def get_task(self, task_id: int) -> Task | None:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return self._row_to_task(row) if row else None

    def next_pending_task(self) -> Task | None:
        """Next actionable task (sequential loop). Resumable: in-flight
        statuses are picked up before untouched pending ones."""
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE status IN"
            " ('in_progress','testing','validating','revising','pending')"
            " ORDER BY CASE status WHEN 'pending' THEN 1 ELSE 0 END, id"
            " LIMIT 1").fetchone()
        return self._row_to_task(row) if row else None

    def list_tasks(self) -> list[Task]:
        rows = self._conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()
        return [self._row_to_task(r) for r in rows]

    def update_task(self, task: Task) -> None:
        self._conn.execute(
            "UPDATE tasks SET status=?, revision_count=?, output=?,"
            " escalation_reason=?, updated_at=? WHERE id=?",
            (task.status.value, task.revision_count, task.output,
             task.escalation_reason, time.time(), task.id),
        )
        self._conn.commit()

    def set_status(self, task: Task, status: TaskStatus, reason: str = "") -> None:
        task.status = status
        if reason:
            task.escalation_reason = reason
        self.update_task(task)
        self.log_event(task.id, f"status:{status.value}",
                       {"reason": reason} if reason else {})

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> Task:
        return Task(
            id=row["id"], title=row["title"], goal=row["goal"],
            acceptance_criteria=row["acceptance_criteria"],
            status=TaskStatus(row["status"]), risk_level=row["risk_level"],
            revision_count=row["revision_count"],
            worker_role=row["worker_role"],
            validator_role=row["validator_role"], output=row["output"],
            escalation_reason=row["escalation_reason"],
        )

    # -- attempts / metrics --------------------------------------------------

    def start_attempt(self, task_id: int, kind: str, role: str, model: str) -> int:
        cur = self._conn.execute(
            "INSERT INTO attempts (task_id, kind, agent_role, model, started_at)"
            " VALUES (?,?,?,?,?)", (task_id, kind, role, model, time.time()))
        self._conn.commit()
        return cur.lastrowid

    def finish_attempt(self, attempt_id: int, output: str, tokens_in: int,
                       tokens_out: int, cost_usd: float,
                       model: str | None = None) -> None:
        """Record the result of an attempt.

        `model` is what actually served the request, which can differ from what
        the registry asked for (a mock backend, or a provider substituting a
        model). Cost is derived from the serving model, so the row stores that
        one — otherwise the per-model rollup attributes spend to a model that
        never ran.
        """
        if model:
            self._conn.execute(
                "UPDATE attempts SET finished_at=?, output=?, tokens_in=?,"
                " tokens_out=?, cost_usd=?, model=? WHERE id=?",
                (time.time(), output, tokens_in, tokens_out, cost_usd, model,
                 attempt_id))
        else:
            self._conn.execute(
                "UPDATE attempts SET finished_at=?, output=?, tokens_in=?,"
                " tokens_out=?, cost_usd=? WHERE id=?",
                (time.time(), output, tokens_in, tokens_out, cost_usd,
                 attempt_id))
        self._conn.commit()

    def task_spend(self, task_id: int) -> tuple[int, float]:
        """Total (tokens, cost_usd) across all attempts — for budget caps."""
        row = self._conn.execute(
            "SELECT COALESCE(SUM(tokens_in+tokens_out),0) AS toks,"
            " COALESCE(SUM(cost_usd),0.0) AS cost"
            " FROM attempts WHERE task_id=?", (task_id,)).fetchone()
        return int(row["toks"]), float(row["cost"])

    def task_metrics(self, task_id: int) -> dict:
        toks, cost = self.task_spend(task_id)
        row = self._conn.execute(
            "SELECT COUNT(*) AS n,"
            " COALESCE(SUM(finished_at-started_at),0) AS wall"
            " FROM attempts WHERE task_id=? AND finished_at IS NOT NULL",
            (task_id,)).fetchone()
        verdicts = self._conn.execute(
            "SELECT kind, confidence, tests_passed FROM verdicts"
            " WHERE task_id=? ORDER BY id", (task_id,)).fetchall()
        return {
            "tokens": toks, "cost_usd": round(cost, 6),
            "attempts": row["n"], "wall_seconds": round(row["wall"], 3),
            "verdicts": [dict(v) for v in verdicts],
        }

    # -- verdicts ------------------------------------------------------------

    def add_verdict(self, task_id: int, attempt_id: int | None, v: Verdict) -> int:
        cur = self._conn.execute(
            "INSERT INTO verdicts (task_id, attempt_id, kind, confidence,"
            " reasoning, tests_passed, created_at) VALUES (?,?,?,?,?,?,?)",
            (task_id, attempt_id, v.kind.value, v.confidence, v.reasoning,
             None if v.tests_passed is None else int(v.tests_passed),
             time.time()))
        self._conn.commit()
        self.log_event(task_id, "verdict", {
            "kind": v.kind.value, "confidence": v.confidence,
            "tests_passed": v.tests_passed})
        return cur.lastrowid

    # -- audit log -----------------------------------------------------------

    def log_event(self, task_id: int | None, kind: str, payload: dict) -> None:
        self._conn.execute(
            "INSERT INTO events (task_id, ts, kind, payload) VALUES (?,?,?,?)",
            (task_id, time.time(), kind, json.dumps(payload)))
        self._conn.commit()

    def events(self, task_id: int | None = None) -> list[dict]:
        if task_id is None:
            rows = self._conn.execute(
                "SELECT * FROM events ORDER BY id").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE task_id=? ORDER BY id",
                (task_id,)).fetchall()
        return [dict(r) | {"payload": json.loads(r["payload"])} for r in rows]

    # -- memory (spec §7) ----------------------------------------------------

    def memory_write(self, tier: str, key: str, value: str,
                     approved: bool = False) -> None:
        # Approval is sticky: rewriting an already-approved fact must not
        # silently revoke it (that would make approved memory quietly
        # unreadable). Only an explicit approve=True can raise the flag.
        self._conn.execute(
            "INSERT INTO memory (tier, key, value, approved, created_at)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(tier, key) DO UPDATE SET value=excluded.value,"
            " approved=MAX(memory.approved, excluded.approved)",
            (tier, key, value, int(approved), time.time()))
        self._conn.commit()
        self.log_event(None, "memory_write",
                       {"tier": tier, "key": key, "approved": approved})

    def memory_read(self, tier: str, key: str,
                    approved_only: bool = True) -> str | None:
        q = "SELECT value FROM memory WHERE tier=? AND key=?"
        if approved_only:
            q += " AND approved=1"
        row = self._conn.execute(q, (tier, key)).fetchone()
        if row:
            self._conn.execute(
                "UPDATE memory SET hit_count=hit_count+1 WHERE tier=? AND key=?",
                (tier, key))
            self._conn.commit()
        return row["value"] if row else None

    def memory_list(self, tier: str | None = None,
                    approved_only: bool = False) -> list[dict]:
        q = "SELECT * FROM memory"
        params: list = []
        where = []
        if tier:
            where.append("tier=?")
            params.append(tier)
        if approved_only:
            where.append("approved=1")
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY tier, key"
        return [dict(r) for r in self._conn.execute(q, tuple(params)).fetchall()]

    def memory_set_approved(self, mem_id: int, approved: bool) -> None:
        row = self._conn.execute(
            "SELECT tier, key FROM memory WHERE id=?", (mem_id,)).fetchone()
        if row is None:
            raise KeyError(f"No memory row {mem_id}")
        self._conn.execute("UPDATE memory SET approved=? WHERE id=?",
                           (int(approved), mem_id))
        self._conn.commit()
        self.log_event(None, "memory_approved" if approved else "memory_revoked",
                       {"tier": row["tier"], "key": row["key"]})

    def memory_delete(self, mem_id: int) -> None:
        row = self._conn.execute(
            "SELECT tier, key FROM memory WHERE id=?", (mem_id,)).fetchone()
        if row is None:
            raise KeyError(f"No memory row {mem_id}")
        self._conn.execute("DELETE FROM memory WHERE id=?", (mem_id,))
        self._conn.commit()
        self.log_event(None, "memory_deleted",
                       {"tier": row["tier"], "key": row["key"]})

    # -- executed test results (spec §5) -------------------------------------

    def add_test_run(self, task_id: int, attempt_id: int | None,
                     result: TestResult) -> int:
        cur = self._conn.execute(
            "INSERT INTO test_runs (task_id, attempt_id, status, exit_code,"
            " summary, stdout_tail, duration_s, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (task_id, attempt_id, result.status, result.exit_code,
             result.summary, result.stdout_tail, result.duration_s,
             time.time()))
        self._conn.commit()
        self.log_event(task_id, "test_run", {
            "status": result.status, "exit_code": result.exit_code,
            "summary": result.summary, "duration_s": result.duration_s})
        return cur.lastrowid

    def test_runs(self, task_id: int) -> list[dict]:
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM test_runs WHERE task_id=? ORDER BY id",
            (task_id,)).fetchall()]

    # -- change feed for the Phase-2 dashboard --------------------------------

    def events_since(self, event_id: int, limit: int = 500) -> list[dict]:
        """Audit-log rows after `event_id`. The append-only log doubles as the
        dashboard's change feed: monotonic ids make SSE resumable by cursor."""
        rows = self._conn.execute(
            "SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?",
            (event_id, limit)).fetchall()
        return [dict(r) | {"payload": json.loads(r["payload"])} for r in rows]

    def latest_event_id(self) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS m FROM events").fetchone()
        return int(row["m"])

    def run_metrics(self) -> dict:
        """Run-level rollup across all tasks (spec §6)."""
        totals = self._conn.execute(
            "SELECT COALESCE(SUM(tokens_in),0) AS tin,"
            " COALESCE(SUM(tokens_out),0) AS tout,"
            " COALESCE(SUM(cost_usd),0.0) AS cost,"
            " COUNT(*) AS attempts,"
            " COALESCE(SUM(finished_at-started_at),0) AS wall"
            " FROM attempts WHERE finished_at IS NOT NULL").fetchone()
        by_status = {r["status"]: r["n"] for r in self._conn.execute(
            "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
        ).fetchall()}
        by_model = [dict(r) for r in self._conn.execute(
            "SELECT model, COUNT(*) AS attempts,"
            " COALESCE(SUM(tokens_in),0) AS tokens_in,"
            " COALESCE(SUM(tokens_out),0) AS tokens_out,"
            " COALESCE(SUM(cost_usd),0.0) AS cost_usd"
            " FROM attempts GROUP BY model ORDER BY cost_usd DESC").fetchall()]
        revisions = self._conn.execute(
            "SELECT COALESCE(SUM(revision_count),0) AS r FROM tasks").fetchone()
        return {
            "tokens_in": int(totals["tin"]), "tokens_out": int(totals["tout"]),
            "tokens": int(totals["tin"]) + int(totals["tout"]),
            "cost_usd": round(float(totals["cost"]), 6),
            "attempts": int(totals["attempts"]),
            "wall_seconds": round(float(totals["wall"]), 3),
            "revisions": int(revisions["r"]),
            "tasks_by_status": by_status,
            "by_model": by_model,
        }
