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
import time
from pathlib import Path

from .models import Task, TaskStatus, Verdict, VerdictKind

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
"""


class Store:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
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
            " ('in_progress','validating','revising','pending')"
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

    def finish_attempt(self, attempt_id: int, output: str,
                       tokens_in: int, tokens_out: int, cost_usd: float) -> None:
        self._conn.execute(
            "UPDATE attempts SET finished_at=?, output=?, tokens_in=?,"
            " tokens_out=?, cost_usd=? WHERE id=?",
            (time.time(), output, tokens_in, tokens_out, cost_usd, attempt_id))
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
        self._conn.execute(
            "INSERT INTO memory (tier, key, value, approved, created_at)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(tier, key) DO UPDATE SET value=excluded.value,"
            " approved=excluded.approved",
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
