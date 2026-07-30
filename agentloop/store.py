"""SQLite store — the single source of truth (spec §2).

Every agent and (later) the Phase-2 UI reads and writes here. Design notes:

- `events` is an immutable, append-only audit trail (spec §11): every prompt,
  output, verdict, and human decision lands here with a timestamp.
- `attempts` carries per-invocation metrics (tokens, wall time, cost) — the
  data source for the visualization layer (spec §6, §8).
- Schema is deliberately boring SQL so a later move to Postgres is a
  connection-string change, not a rewrite (spec §9).
- The loop is resumable (spec §11): all state needed to continue lives here.
- A state change and its audit event commit together via `transaction()`, so a
  crash never leaves the row without its event (or an event without its row).
  Rollback discards only uncommitted writes — committed `events` are never
  touched, so the log stays append-only.
- `claim_next_task` hands a task to exactly one worker (atomic select+claim via
  a `claimed_by` lease column), the prerequisite for parallel workers; the
  sequential loop uses one stable worker id and is behaviorally unchanged.
- `task_deps` holds the planner's task graph. Being blocked is a *predicate*
  evaluated inside the claim (`_UNBLOCKED`), not a status: a task waiting on an
  unfinished dependency, or on an unapproved plan, stays ordinary `pending` and
  is simply never handed out. Nothing has to be un-set when the blocker clears,
  and a human can never mistake waiting work for finished work.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
from pathlib import Path

from .models import Task, TaskStatus, TestResult, Verdict

# How many candidates a claim will try before giving up. Each retry means
# another process won that row, so this only bounds a pathological live-lock;
# in practice the loser's next look finds a different task or nothing.
_CLAIM_ATTEMPTS = 100

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
    -- Mid-run human control signal (spec: pause/resume/abort), read at each
    -- loop iteration boundary. Persisted so a human in another process (CLI or
    -- dashboard) can steer a running loop, and a paused task survives restart.
    control TEXT NOT NULL DEFAULT 'run',   -- 'run' | 'pause' | 'abort'
    -- Which worker holds this task. NULL = unclaimed. Set atomically by
    -- claim_next_task so exactly one worker runs a task (parallel-worker
    -- prerequisite); a worker only resumes in-flight tasks it owns.
    claimed_by TEXT,
    -- 'task' | 'plan'. A 'plan' row is the container a planner run decomposed:
    -- it owns the planner's attempts and the plan's approval flag, and is
    -- excluded from claiming so a goal statement is never executed as work.
    kind TEXT NOT NULL DEFAULT 'task',
    -- The plan that produced this task; NULL for hand-defined tasks.
    plan_id INTEGER REFERENCES tasks(id),
    -- Meaningful on 'plan' rows only: has a human signed this plan off? A
    -- planner generating tasks *is* task definition, which humans stay in the
    -- loop for, so children of an unapproved plan are not claimable.
    plan_approved INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

-- Task graph edges (spec: planner). One row per "task_id waits on
-- depends_on_id". A separate table rather than a column because a task can
-- have many dependencies; the primary key makes re-adding an edge idempotent.
-- Acyclicity is enforced in `add_dependency`, not by the schema: a cycle is a
-- permanent deadlock, so the graph must never be able to hold one.
CREATE TABLE IF NOT EXISTS task_deps (
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    depends_on_id INTEGER NOT NULL REFERENCES tasks(id),
    created_at REAL NOT NULL,
    PRIMARY KEY (task_id, depends_on_id)
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
    tokens_in INTEGER NOT NULL DEFAULT 0,      -- new input only
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,  -- prompt-cache writes
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,      -- prompt-cache reads
    cost_usd REAL NOT NULL DEFAULT 0.0         -- includes cache cost
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
    -- Pinned approved facts sort first and get a reserved slice of the prompt
    -- budget, so a fact that must always be present is not dropped by the
    -- alphabetical tail-off once the injection cap is reached.
    pinned INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    -- When this fact was last read out. `hit_count` says how often, never how
    -- recently; a fact hot a year ago and cold since looks identical without it.
    last_used_at REAL,
    UNIQUE(tier, key)
);

-- Which tasks a memory fact was actually relevant to. `hit_count` is the
-- denormalized size of this set, and promotion's claim -- "relevant to N
-- *tasks*" -- is only true because the set is keyed on the task. Counting
-- injections instead counted worker + validator + one revision as three, so a
-- single task promoted a fact on its own at the default threshold.
CREATE TABLE IF NOT EXISTS memory_hits (
    memory_id INTEGER NOT NULL REFERENCES memory(id) ON DELETE CASCADE,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    ts REAL NOT NULL,
    UNIQUE(memory_id, task_id)
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

-- Validator calibration harness results (spec: eval). One row per harness run;
-- the per-fixture detail and the summary (agreement, confusion matrix,
-- calibration table) are stored as JSON so the schema stays boring.
CREATE TABLE IF NOT EXISTS eval_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    runner TEXT NOT NULL,
    n_fixtures INTEGER NOT NULL,
    agreement REAL NOT NULL,
    summary TEXT NOT NULL DEFAULT '{}',
    detail TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL
);
"""


# How much of a displaced memory value a merge event keeps. Enough to recognise
# and recover what was overwritten; not a second copy of the store.
_MAX_EVENT_VALUE_CHARS = 400


class TransactionAborted(RuntimeError):
    """A transaction was rolled back because an inner one failed, and that
    failure never reached the caller. Raised at the outermost boundary so a
    swallowed inner error cannot be mistaken for a group that committed."""


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
        # >0 while a transaction() is open (reentrant). Inner commit() calls
        # are deferred to the outermost transaction's single commit, so a
        # state change and its audit event land together or not at all.
        self._txn_depth = 0
        # Set when any transaction in the current nest fails. The rollback and
        # the decision not to commit both belong to the *outermost* boundary:
        # see `transaction`.
        self._txn_aborted = False

    def execute(self, sql: str, params: tuple = ()) -> _LockedCursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            # Materialize under the lock: rows read later, off-lock, would
            # race another thread's use of the same connection.
            rows = cur.fetchall() if cur.description else []
            return _LockedCursor(cur.lastrowid, rows, cur.rowcount)

    def write(self, sql: str, params: tuple = ()) -> _LockedCursor:
        """A write that commits, with the lock held across both steps.

        `execute()` then `commit()` as two calls takes the lock *twice*, and in
        the gap between them another thread can enter `transaction()`, fail, and
        `rollback()` — on this same shared connection, destroying the
        uncommitted row the first thread just wrote while its `execute()`
        returned perfectly normally. That is silent data loss (a worker's output,
        an attempt's token counts) and it becomes reachable the moment more than
        one worker runs. Every unbatched write goes through here so the window
        does not exist; inside a `transaction()` this joins it and defers the
        commit as before.
        """
        with self.transaction():
            return self.execute(sql, params)

    def executescript(self, sql: str) -> None:
        with self._lock:
            self._conn.executescript(sql)

    def commit(self) -> None:
        with self._lock:
            # Inside a transaction, defer to the outermost boundary; otherwise
            # commit eagerly (the store's per-operation default).
            if self._txn_depth == 0:
                self._conn.commit()

    @contextlib.contextmanager
    def transaction(self):
        """Group writes into one commit. The lock is held for the whole block
        (SQL is already serialized), inner `commit()` calls become no-ops, and
        an exception rolls the whole group back — so no partial state, and the
        append-only `events` log never diverges from the row it describes.

        Reentrant: a helper that opens its own transaction while inside one
        joins the outer transaction rather than committing early.

        A failure anywhere in the nest aborts the whole nest, but the rollback
        itself happens once, at the outermost boundary. Zeroing the depth on the
        error path instead of decrementing it meant an enclosing block that
        *caught* an inner transaction's error went on to decrement to -1, after
        which every `commit()` saw a non-zero depth and silently stopped
        committing: writes that returned normally and never landed. So the
        depth is decremented on both paths, and a swallowed inner failure is
        raised as `TransactionAborted` at the outermost exit rather than
        committing the surviving half of a group — the half that landed would
        otherwise look like a complete group nobody questioned."""
        with self._lock:
            self._txn_depth += 1
            try:
                yield
            except BaseException:
                self._txn_aborted = True
                raise
            finally:
                self._txn_depth -= 1
                outermost = self._txn_depth == 0
                aborted = self._txn_aborted
                if outermost:
                    self._txn_aborted = False
                    if aborted:
                        # Rollback only discards uncommitted writes; it never
                        # touches committed `events` rows, so the append-only
                        # rule holds.
                        self._conn.rollback()
                    else:
                        self._conn.commit()
            # Only reached when this block exited normally: an inner failure was
            # caught by the code between here and there. The writes are gone, so
            # say so rather than returning as if they landed.
            if outermost and aborted:
                raise TransactionAborted(
                    "an inner transaction failed and its error was swallowed; "
                    "the whole transaction was rolled back"
                )

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class _LockedCursor:
    """Cursor view over rows already fetched under the connection lock."""

    def __init__(
        self, lastrowid: int | None, rows: list[sqlite3.Row], rowcount: int = -1
    ):
        self.lastrowid = lastrowid
        # How many rows the statement actually changed — the signal a
        # conditional UPDATE uses to tell "I won this race" from "someone else
        # already did".
        self.rowcount = rowcount
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
        # Which tables predate this open, captured *before* the schema script
        # creates the missing ones: a table that is new here was written by an
        # older build of agentloop, and `_migrate` sometimes has to reconcile
        # what that build left behind, not just add a column.
        existing = self._tables()
        self._conn.executescript(_SCHEMA)
        self._migrate(existing)
        self._conn.commit()

    def _tables(self) -> set[str]:
        return {
            r["name"]
            for r in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

    def _migrate(self, existing: set[str] | None = None) -> None:
        """Bring a database written by an older build up to date.

        `CREATE TABLE IF NOT EXISTS` never alters an existing table, so a store
        opened against an older db is missing the newer columns. Add them
        idempotently; SQLite ignores the change once the column exists.

        `existing` is the set of tables that were present before this open, used
        by `_reconcile_memory_hits` to recognise a database written before hits
        were counted per task.
        """
        existing = self._tables() if existing is None else existing
        additions = [
            ("attempts", "cache_creation_tokens", "INTEGER NOT NULL DEFAULT 0"),
            ("attempts", "cache_read_tokens", "INTEGER NOT NULL DEFAULT 0"),
            ("tasks", "control", "TEXT NOT NULL DEFAULT 'run'"),
            ("tasks", "claimed_by", "TEXT"),
            ("tasks", "kind", "TEXT NOT NULL DEFAULT 'task'"),
            # No REFERENCES on the added column: SQLite's ALTER TABLE ADD COLUMN
            # rejects a foreign key with a non-NULL default and cannot add one
            # retroactively. The constraint is in _SCHEMA for fresh dbs; older
            # dbs get the column, and plan_id is only ever written from a task
            # id this store just inserted.
            ("tasks", "plan_id", "INTEGER"),
            ("tasks", "plan_approved", "INTEGER NOT NULL DEFAULT 0"),
            ("memory", "pinned", "INTEGER NOT NULL DEFAULT 0"),
            ("memory", "last_used_at", "REAL"),
        ]
        for table, column, decl in additions:
            cols = {
                r["name"]
                for r in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if column not in cols:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        if "memory" in existing and "memory_hits" not in existing:
            self._reconcile_memory_hits()

    def _reconcile_memory_hits(self) -> None:
        """Repair what the pre-`memory_hits` build of agentloop left behind.

        Two things, both one-shot, both only for a database that already had a
        `memory` table before `memory_hits` existed.

        **The old `hit_count` is a different unit.** It counted *prompts*, so
        every project fact sitting at or above the threshold would promote on
        its first relevant read after the upgrade — precisely the "one ordinary
        task promoted the whole project tier" failure this build exists to
        remove, preserved intact for every existing database. There is nothing
        to backfill from (the per-task evidence was never recorded), so the
        counters restart at zero against the table that now defines them. That
        is also what keeps `hit_count` equal to the size of a fact's
        `memory_hits` set, which is the invariant the rest of the code reads it
        as.

        **Duplicate keys.** The old promotion copied rather than moved, so a
        database can hold both `project/k` and `loop/k`. Both are injected, so
        one real fact is evicted from a cap that is not full, and revoking one
        leaves the other approved. They are merged here rather than being left
        to clear themselves the next time each fact happens to get hot again.

        The merge runs with `origin='migration'`, which differs from the live
        promotion in two ways, both because this is a repair of somebody's
        existing database rather than a fact winning on merit: an **approved**
        loop value outranks an unapproved project value (otherwise opening the
        database would replace vetted content with the rewrite that un-approved
        it), and the audit event is `memory_duplicates_merged` rather than
        `memory_promoted` (nothing was promoted here — the old build did that
        already, and reusing the event would make the feed report a promotion
        every time a database is opened).
        """
        with self.transaction():
            reset = self._conn.execute(
                "UPDATE memory SET hit_count=0 WHERE hit_count<>0"
            )
            if reset.rowcount:
                self.log_event(
                    None, "memory_hit_counts_reset", {"rows": int(reset.rowcount)}
                )
            dupes = self._conn.execute(
                "SELECT p.id AS project_id, l.id AS loop_id FROM memory p"
                " JOIN memory l ON l.key = p.key AND l.tier='loop'"
                " WHERE p.tier='project'"
            ).fetchall()
            for row in dupes:
                self._merge_into_loop(
                    int(row["project_id"]), int(row["loop_id"]), origin="migration"
                )

    def close(self) -> None:
        self._conn.close()

    def transaction(self):
        """Commit a state change and its audit event as one unit. Use around
        paired writes so a crash can't leave the row without its event (or the
        reverse). Rollback never deletes committed `events` — the log stays
        append-only. See `_LockedConnection.transaction`."""
        return self._conn.transaction()

    # -- tasks ---------------------------------------------------------------

    def add_task(self, task: Task) -> int:
        now = time.time()
        with self.transaction():
            cur = self._conn.execute(
                "INSERT INTO tasks (title, goal, acceptance_criteria, status,"
                " risk_level, worker_role, validator_role, kind, plan_id,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    task.title,
                    task.goal,
                    task.acceptance_criteria,
                    task.status.value,
                    task.risk_level,
                    task.worker_role,
                    task.validator_role,
                    task.kind,
                    task.plan_id,
                    now,
                    now,
                ),
            )
            task.id = cur.lastrowid
            self.log_event(
                task.id,
                "task_defined",
                {
                    "title": task.title,
                    "goal": task.goal,
                    "acceptance_criteria": task.acceptance_criteria,
                    "risk_level": task.risk_level,
                    "kind": task.kind,
                    "plan_id": task.plan_id,
                },
            )
        return task.id

    def get_task(self, task_id: int) -> Task | None:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        return self._row_to_task(row) if row else None

    # Why a task may not be worked yet, as SQL. Two blockers, both *predicates*
    # rather than statuses: a blocked task is ordinary `pending` that simply
    # isn't claimable, so nothing has to be un-set when the blocker clears and a
    # human can never mistake waiting work for finished work.
    #
    #   1. an unfinished dependency — every edge must point at a DONE task;
    #   2. an unapproved plan — a planner generating tasks is task definition,
    #      which humans stay in the loop for.
    _UNBLOCKED = (
        " AND t.kind='task'"
        " AND NOT EXISTS (SELECT 1 FROM task_deps d JOIN tasks dep"
        "   ON dep.id = d.depends_on_id"
        "   WHERE d.task_id = t.id AND dep.status <> 'done')"
        " AND (t.plan_id IS NULL OR EXISTS (SELECT 1 FROM tasks p"
        "   WHERE p.id = t.plan_id AND p.plan_approved = 1))"
    )
    # In-flight before untouched, then by id: a crashed run resumes what it was
    # already doing before starting anything new.
    _ACTIONABLE_ORDER = " ORDER BY CASE t.status WHEN 'pending' THEN 1 ELSE 0 END, t.id"

    def next_pending_task(self) -> Task | None:
        """Next actionable task (read-only peek; the loop uses the claiming
        form). Resumable: in-flight statuses are picked up before untouched
        pending ones, and blocked tasks are skipped exactly as they are for a
        claim, so this never reports work the loop would refuse to start."""
        row = self._conn.execute(
            "SELECT * FROM tasks t WHERE t.status IN"
            " ('in_progress','testing','validating','revising','pending')"
            + self._UNBLOCKED
            + self._ACTIONABLE_ORDER
            + " LIMIT 1"
        ).fetchone()
        return self._row_to_task(row) if row else None

    def claim_next_task(self, worker_id: str) -> Task | None:
        """Atomically hand the next actionable task to exactly one worker.

        Select and claim happen in a single transaction, so two workers racing
        for one pending task can never both win (parallel-worker prerequisite).
        A pending task is flipped to `in_progress` and stamped `claimed_by`; an
        in-flight task is resumable only by the worker that already owns it, so
        a crashed run resumes without another worker stealing it. With a single
        stable `worker_id` this reproduces `next_pending_task`'s ordering
        (in-flight before pending), so the sequential loop is unchanged.

        Blocked tasks are invisible to the claim (see `_UNBLOCKED`): a task
        whose dependencies are unfinished, or whose plan is unapproved, is never
        handed out. Because the check runs inside the claiming transaction it is
        evaluated against committed state, so two workers finishing two
        dependencies at once cannot both conclude a shared dependent is still
        blocked — the later claim sees the earlier commit.

        The claim is a **compare-and-swap**, not a plain UPDATE. Within one
        process the connection lock alone would be enough, but two `agentloop
        run` processes on the same database each hold their own connection and
        their own lock: Python's sqlite3 does not open a write transaction for a
        `SELECT`, so both could read the same pending row and both claim it. The
        `WHERE status='pending' AND claimed_by IS NULL` guard makes the loser's
        UPDATE match zero rows, and it retries on the next candidate instead.
        """
        for _ in range(_CLAIM_ATTEMPTS):
            with self.transaction():
                row = self._conn.execute(
                    "SELECT * FROM tasks t WHERE ("
                    " t.status='pending'"
                    " OR (t.status IN ('in_progress','testing','validating',"
                    "'revising') AND t.claimed_by=?))"
                    + self._UNBLOCKED
                    + self._ACTIONABLE_ORDER
                    + " LIMIT 1",
                    (worker_id,),
                ).fetchone()
                if row is None:
                    return None
                task = self._row_to_task(row)
                if task.status != TaskStatus.PENDING:
                    return task  # resuming in-flight work this worker owns
                cur = self._conn.execute(
                    "UPDATE tasks SET status='in_progress', claimed_by=?,"
                    " updated_at=? WHERE id=? AND status='pending'"
                    " AND claimed_by IS NULL",
                    (worker_id, time.time(), task.id),
                )
                if cur.rowcount == 1:
                    task.status = TaskStatus.IN_PROGRESS
                    task.claimed_by = worker_id
                    self.log_event(task.id, "task_claimed", {"worker": worker_id})
                    return task
                # Lost the race to another process; fall through and look again.
        return None

    def stranded_claims(self, prefix: str, active_worker_ids: list[str]) -> list[Task]:
        """In-flight tasks held by a claim id in this loop's id-space that no
        currently-active worker owns.

        Shrinking a worker pool retires claim ids, and `claim_next_task` only
        re-offers in-flight work to its exact owner — so those tasks stop being
        visible to anybody. This finds them; deciding what to do about it is the
        caller's, because a retired id is indistinguishable from a live second
        process using the same id-space."""
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE claimed_by IS NOT NULL"
            " AND status IN ('in_progress','testing','validating','revising')"
            " AND (claimed_by = ? OR claimed_by LIKE ?) ORDER BY id",
            (prefix, f"{prefix}-%"),
        ).fetchall()
        active = set(active_worker_ids)
        return [
            t
            for t in (self._row_to_task(r) for r in rows)
            if t.claimed_by not in active
        ]

    def list_tasks(self) -> list[Task]:
        rows = self._conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()
        return [self._row_to_task(r) for r in rows]

    def update_task(self, task: Task) -> None:
        # Deliberately does NOT write the `control` column: the loop holds a
        # task object loaded at the start of the run, and writing its stale
        # `control` here would clobber a pause/abort a human set concurrently
        # from another process. `set_control` is the sole writer of control.
        self._conn.write(
            "UPDATE tasks SET status=?, revision_count=?, output=?,"
            " escalation_reason=?, updated_at=? WHERE id=?",
            (
                task.status.value,
                task.revision_count,
                task.output,
                task.escalation_reason,
                time.time(),
                task.id,
            ),
        )

    def set_control(self, task_id: int, control: str) -> None:
        """Write the mid-run control signal (run/pause/abort). The sole writer
        of the control column, so a human in another process can steer a
        running loop without racing the loop's own task writes."""
        with self.transaction():
            self._conn.execute(
                "UPDATE tasks SET control=?, updated_at=? WHERE id=?",
                (control, time.time(), task_id),
            )
            self.log_event(task_id, f"control:{control}", {})

    def get_control(self, task_id: int) -> str:
        """Read the control signal fresh from the store — the loop re-reads this
        each iteration so a cross-process pause/abort is seen promptly."""
        row = self._conn.execute(
            "SELECT control FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        return row["control"] if row else "run"

    def set_status(self, task: Task, status: TaskStatus, reason: str = "") -> None:
        task.status = status
        if reason:
            task.escalation_reason = reason
        with self.transaction():  # row change + its event: one commit
            self.update_task(task)
            self.log_event(
                task.id, f"status:{status.value}", {"reason": reason} if reason else {}
            )

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> Task:
        return Task(
            id=row["id"],
            title=row["title"],
            goal=row["goal"],
            acceptance_criteria=row["acceptance_criteria"],
            status=TaskStatus(row["status"]),
            risk_level=row["risk_level"],
            revision_count=row["revision_count"],
            worker_role=row["worker_role"],
            validator_role=row["validator_role"],
            output=row["output"],
            escalation_reason=row["escalation_reason"],
            control=row["control"],
            claimed_by=row["claimed_by"],
            kind=row["kind"],
            plan_id=row["plan_id"],
        )

    # -- task graph (spec: planner) -------------------------------------------

    def add_dependency(self, task_id: int, depends_on_id: int) -> None:
        """Record that `task_id` waits on `depends_on_id`.

        Rejects any edge that would close a cycle, including a self-edge. A
        cycle is not a slow graph, it is a permanent deadlock: every task in it
        waits on another, so none is ever claimable and the loop drains to a
        silent stall. Refusing the edge keeps "the graph the loop drains is a
        DAG" an invariant of the store rather than a hope about the planner.
        """
        if task_id == depends_on_id:
            raise ValueError(f"Task {task_id} cannot depend on itself (cycle)")
        with self.transaction():
            # The new edge closes a cycle iff `task_id` is already reachable
            # from `depends_on_id` by following existing edges.
            if self._reaches(depends_on_id, task_id):
                raise ValueError(
                    f"Dependency {task_id} -> {depends_on_id} would create a cycle"
                )
            # ON CONFLICT DO NOTHING rather than INSERT OR IGNORE: identical
            # semantics, but it is the standard form Postgres also understands,
            # and the store already uses this idiom in `memory_write`.
            self._conn.execute(
                "INSERT INTO task_deps (task_id, depends_on_id, created_at)"
                " VALUES (?,?,?) ON CONFLICT DO NOTHING",
                (task_id, depends_on_id, time.time()),
            )
            self.log_event(task_id, "task_dependency", {"depends_on": depends_on_id})

    def _reaches(self, start: int, target: int) -> bool:
        """Is `target` reachable from `start` along dependency edges? Iterative
        so a deep graph can't blow the Python stack, and `seen` makes it
        terminate even if a cycle somehow already exists in an older db."""
        seen: set[int] = set()
        frontier = [start]
        while frontier:
            node = frontier.pop()
            if node == target:
                return True
            if node in seen:
                continue
            seen.add(node)
            frontier.extend(self.dependencies(node))
        return False

    def dependencies(self, task_id: int) -> list[int]:
        """Task ids this task waits on."""
        return [
            int(r["depends_on_id"])
            for r in self._conn.execute(
                "SELECT depends_on_id FROM task_deps WHERE task_id=?"
                " ORDER BY depends_on_id",
                (task_id,),
            ).fetchall()
        ]

    def dependents(self, task_id: int) -> list[int]:
        """Task ids waiting on this task."""
        return [
            int(r["task_id"])
            for r in self._conn.execute(
                "SELECT task_id FROM task_deps WHERE depends_on_id=? ORDER BY task_id",
                (task_id,),
            ).fetchall()
        ]

    def plan_tasks(self, plan_id: int) -> list[Task]:
        """The tasks one plan produced, in creation order."""
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE plan_id=? ORDER BY id", (plan_id,)
        ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def set_plan_approved(self, plan_id: int, approved: bool) -> None:
        """Sign a plan off (or withdraw it). The children are ordinary pending
        rows; this flag is what makes them claimable, so approval is one write
        rather than a status sweep that could half-apply."""
        with self.transaction():
            self._conn.execute(
                "UPDATE tasks SET plan_approved=?, updated_at=? WHERE id=?",
                (int(approved), time.time(), plan_id),
            )
            self.log_event(
                plan_id, "plan_approved" if approved else "plan_unapproved", {}
            )

    def is_plan_approved(self, plan_id: int) -> bool:
        row = self._conn.execute(
            "SELECT plan_approved FROM tasks WHERE id=?", (plan_id,)
        ).fetchone()
        return bool(row["plan_approved"]) if row else False

    # -- attempts / metrics --------------------------------------------------

    def start_attempt(self, task_id: int, kind: str, role: str, model: str) -> int:
        cur = self._conn.write(
            "INSERT INTO attempts (task_id, kind, agent_role, model, started_at)"
            " VALUES (?,?,?,?,?)",
            (task_id, kind, role, model, time.time()),
        )
        return cur.lastrowid

    def finish_attempt(
        self,
        attempt_id: int,
        output: str,
        tokens_in: int,
        tokens_out: int,
        cost_usd: float,
        model: str | None = None,
        cache_creation_tokens: int = 0,
        cache_read_tokens: int = 0,
    ) -> None:
        """Record the result of an attempt.

        `model` is what actually served the request, which can differ from what
        the registry asked for (a mock backend, or a provider substituting a
        model). Cost is derived from the serving model, so the row stores that
        one — otherwise the per-model rollup attributes spend to a model that
        never ran. `cost_usd` already includes cache cost; the cache token
        columns keep the breakdown auditable and feed the token budget cap.
        """
        base = (
            "UPDATE attempts SET finished_at=?, output=?, tokens_in=?,"
            " tokens_out=?, cache_creation_tokens=?, cache_read_tokens=?,"
            " cost_usd=?"
        )
        vals = [
            time.time(),
            output,
            tokens_in,
            tokens_out,
            cache_creation_tokens,
            cache_read_tokens,
            cost_usd,
        ]
        if model:
            base += ", model=?"
            vals.append(model)
        base += " WHERE id=?"
        vals.append(attempt_id)
        self._conn.write(base, tuple(vals))

    def task_spend(self, task_id: int) -> tuple[int, float]:
        """Total (tokens, cost_usd) across all attempts — for budget caps.

        The token total includes prompt-cache tokens: on a cached run they are
        the bulk of the real context consumed, so a cap that ignored them would
        measure almost nothing (the defect this fix addresses)."""
        row = self._conn.execute(
            "SELECT COALESCE(SUM(tokens_in+tokens_out+cache_creation_tokens"
            "+cache_read_tokens),0) AS toks,"
            " COALESCE(SUM(cost_usd),0.0) AS cost"
            " FROM attempts WHERE task_id=?",
            (task_id,),
        ).fetchone()
        return int(row["toks"]), float(row["cost"])

    def attempt_tokens(self, task_id: int, kind: str) -> int:
        """Total tokens across one kind of attempt ('worker' | 'validator' |
        'summarizer') for a task. The context-budget handoff uses this to gauge
        a single role's accumulated context, separately from the whole-task
        budget cap (`task_spend`), which sums every kind. Includes prompt-cache
        tokens, so the measure matches the real context a cached run consumes."""
        row = self._conn.execute(
            "SELECT COALESCE(SUM(tokens_in+tokens_out+cache_creation_tokens"
            "+cache_read_tokens),0) AS toks"
            " FROM attempts WHERE task_id=? AND kind=?",
            (task_id, kind),
        ).fetchone()
        return int(row["toks"])

    def task_metrics(self, task_id: int) -> dict:
        toks, cost = self.task_spend(task_id)
        row = self._conn.execute(
            "SELECT COUNT(*) AS n,"
            " COALESCE(SUM(finished_at-started_at),0) AS wall"
            " FROM attempts WHERE task_id=? AND finished_at IS NOT NULL",
            (task_id,),
        ).fetchone()
        verdicts = self._conn.execute(
            "SELECT kind, confidence, tests_passed FROM verdicts"
            " WHERE task_id=? ORDER BY id",
            (task_id,),
        ).fetchall()
        return {
            "tokens": toks,
            "cost_usd": round(cost, 6),
            "attempts": row["n"],
            "wall_seconds": round(row["wall"], 3),
            "verdicts": [dict(v) for v in verdicts],
        }

    # -- verdicts ------------------------------------------------------------

    def add_verdict(self, task_id: int, attempt_id: int | None, v: Verdict) -> int:
        with self.transaction():
            cur = self._conn.execute(
                "INSERT INTO verdicts (task_id, attempt_id, kind, confidence,"
                " reasoning, tests_passed, created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    task_id,
                    attempt_id,
                    v.kind.value,
                    v.confidence,
                    v.reasoning,
                    None if v.tests_passed is None else int(v.tests_passed),
                    time.time(),
                ),
            )
            self.log_event(
                task_id,
                "verdict",
                {
                    "kind": v.kind.value,
                    "confidence": v.confidence,
                    "tests_passed": v.tests_passed,
                },
            )
        return cur.lastrowid

    # -- audit log -----------------------------------------------------------

    def log_event(self, task_id: int | None, kind: str, payload: dict) -> None:
        self._conn.write(
            "INSERT INTO events (task_id, ts, kind, payload) VALUES (?,?,?,?)",
            (task_id, time.time(), kind, json.dumps(payload)),
        )

    def events(self, task_id: int | None = None) -> list[dict]:
        if task_id is None:
            rows = self._conn.execute("SELECT * FROM events ORDER BY id").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE task_id=? ORDER BY id", (task_id,)
            ).fetchall()
        return [dict(r) | {"payload": json.loads(r["payload"])} for r in rows]

    # -- memory (spec §7) ----------------------------------------------------

    def memory_write(
        self,
        tier: str,
        key: str,
        value: str,
        approved: bool = False,
        pinned: bool = False,
    ) -> None:
        # Approval is approval *of a value*, so a rewrite that changes the value
        # drops back to unapproved: otherwise an agent could rewrite an approved
        # key and have arbitrary new content inherit the gate the whole memory
        # design rests on. A rewrite with the same value keeps its approval —
        # nothing was re-stated, so there is nothing to re-vet — and an explicit
        # approved=True is approving the incoming content, so it still wins.
        #
        # Pinning is sticky regardless: it is a statement about the *key* ("always
        # tell agents about this"), not about a particular value, and it never
        # grants a read on its own. Lowering either flag is via the setters.
        with self.transaction():
            self._conn.execute(
                "INSERT INTO memory (tier, key, value, approved, pinned,"
                " created_at) VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(tier, key) DO UPDATE SET value=excluded.value,"
                " approved=MAX(excluded.approved, CASE WHEN"
                " memory.value = excluded.value THEN memory.approved ELSE 0 END),"
                " pinned=MAX(memory.pinned, excluded.pinned)",
                (tier, key, value, int(approved), int(pinned), time.time()),
            )
            self.log_event(
                None,
                "memory_write",
                {"tier": tier, "key": key, "approved": approved, "pinned": pinned},
            )

    def memory_read(
        self,
        tier: str,
        key: str,
        approved_only: bool = True,
        task_id: int | None = None,
    ) -> str | None:
        """Read one fact, recording the read against the task that caused it.

        The lookup and the bumps it justifies are one transaction. As two
        separate lock acquisitions, a rewrite landing in the gap had its
        now-unapproved value credited with the hit, and `AND approved=1` on both
        UPDATEs says so in SQL as well — the lock is enough within one process,
        the predicate is what holds if a second one ever shares the database.

        `task_id` is what makes `hit_count` mean "distinct tasks this fact was
        relevant to", which is what promotion claims. A read with no task
        (`agentloop memory`, eval, a direct call) still records recency but no
        hit: promotion is a claim about tasks, and there isn't one.
        """
        gate = " AND approved=1" if approved_only else ""
        with self.transaction():
            row = self._conn.execute(
                f"SELECT id, value FROM memory WHERE tier=? AND key=?{gate}",
                (tier, key),
            ).fetchone()
            if row is None:
                return None
            mem_id = int(row["id"])
            now = time.time()
            touched = self._conn.execute(
                f"UPDATE memory SET last_used_at=? WHERE id=?{gate}", (now, mem_id)
            )
            # The gated UPDATE matching nothing means the row stopped being
            # readable between the SELECT and here. Recording the hit anyway
            # would put a row in `memory_hits` that `hit_count` never counted,
            # and OR IGNORE means the same task can never make it up later — the
            # evidence and the counter would disagree permanently.
            if task_id is not None and touched.rowcount == 1:
                # OR IGNORE plus the rowcount check is the whole "distinct
                # tasks" rule: the second prompt of the same task inserts
                # nothing, so it bumps nothing.
                inserted = self._conn.execute(
                    "INSERT OR IGNORE INTO memory_hits (memory_id, task_id, ts)"
                    " VALUES (?,?,?)",
                    (mem_id, task_id, now),
                )
                if inserted.rowcount == 1:
                    self._conn.execute(
                        f"UPDATE memory SET hit_count=hit_count+1 WHERE id=?{gate}",
                        (mem_id,),
                    )
            return row["value"]

    def memory_hits(self, mem_id: int) -> list[int]:
        """The tasks a fact has been relevant to — the evidence behind
        `hit_count`, so a promotion can be checked rather than trusted."""
        return [
            int(r["task_id"])
            for r in self._conn.execute(
                "SELECT task_id FROM memory_hits WHERE memory_id=? ORDER BY ts",
                (mem_id,),
            ).fetchall()
        ]

    def memory_promote(self, mem_id: int) -> None:
        """Move a project fact to the loop tier. One row, not two.

        Promotion used to *copy* the row and leave the project one in place,
        still approved and still counting: both were injected (a real fact
        evicted from a cap that was never full), `memory_promoted` re-fired on
        every later read, and revoking the project fact left the copy approved —
        the value-approval rule bypassed by memory's own promotion path.

        Moving the row keeps its `id`, so `approved`, `pinned`, `hit_count`,
        `last_used_at` and every `memory_hits` row follow it without being
        copied, as does the memory id already recorded in past `retrieval`
        events. Nothing is duplicated, so nothing can diverge.

        A `loop` row already holding the key is the one case the move cannot
        take; see `_merge_into_loop`.
        """
        with self.transaction():
            row = self._conn.execute(
                "SELECT * FROM memory WHERE id=?", (mem_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"No memory row {mem_id}")
            if row["tier"] != "project":
                raise ValueError(f"Only project facts promote (got {row['tier']!r})")
            key = row["key"]
            collision = self._conn.execute(
                "SELECT id FROM memory WHERE tier='loop' AND key=?", (key,)
            ).fetchone()
            if collision is None:
                self._conn.execute(
                    "UPDATE memory SET tier='loop' WHERE id=?", (mem_id,)
                )
                self.log_event(
                    None,
                    "memory_promoted",
                    {
                        "key": key,
                        "from": "project",
                        "to": "loop",
                        "hit_count": row["hit_count"],
                        "merged": False,
                    },
                )
            else:
                self._merge_into_loop(mem_id, int(collision["id"]))

    def _merge_into_loop(
        self, project_id: int, loop_id: int, origin: str = "promotion"
    ) -> None:
        """Collapse a `project`/`loop` pair of the same key onto the loop row.

        `UNIQUE(tier, key)` blocks the plain move whenever a `loop` row already
        holds the key — the state every database written by the old
        copy-promotion is in, which is why the migration reuses this too.

        **A winner is picked and the loser is written into the event.** Only the
        row *id* survives; its content is replaced. No other event payload
        records a memory value, so without this the displaced text would be gone
        for good.

        Which value wins depends on `origin`, because the two callers are not
        the same situation:

        - `'promotion'` — the live path. The promoted project value wins: it is
          the fact that just proved itself over `memory_promote_threshold`
          distinct tasks, and the loop row is the stale one.
        - `'migration'` — the one-shot repair of a database the old
          copy-promotion wrote (`_reconcile_memory_hits`). Here an **approved**
          loop value beats an unapproved project value. Those pairs are usually
          a vetted copy next to a project row that was rewritten afterwards (the
          approval-of-value rule un-approved it at that point), so "the promoted
          value wins" would overwrite vetted content with the unvetted rewrite
          on nothing more than opening the database, leaving the human-approved
          text recoverable only from a truncated event payload.

        Approval then follows the approval-of-value rule against *whichever
        value survived*: it stays approved only if a human approved that exact
        text. Two approved rows with different values are a genuine conflict, so
        that case drops to unapproved for a human to resolve — but a merge that
        does not displace any approved value must not quietly un-approve one,
        which is why the check is against the surviving value rather than
        against whichever row happened to be the loop row.

        Any drop from approved logs `memory_revoked`, no matter which row held
        the approval. Conditioning that on the loop row alone missed the case
        where the *project* row was the approved one: the fact stopped being
        injected and nothing in the feed said so.

        The hits move too. Deleting the project row cascades its `memory_hits`
        away, so they are copied over first and the survivor's `hit_count` is
        recounted from the merged set — the counter is the size of the evidence
        on both promotion paths, and the move path needs no recount only because
        it never changes either.
        """
        if origin not in ("promotion", "migration"):
            raise ValueError(f"Unknown merge origin {origin!r}")
        project = self._conn.execute(
            "SELECT * FROM memory WHERE id=?", (project_id,)
        ).fetchone()
        loop = self._conn.execute(
            "SELECT * FROM memory WHERE id=?", (loop_id,)
        ).fetchone()
        if project is None or loop is None:
            raise KeyError(f"No memory rows {project_id}/{loop_id} to merge")
        key = project["key"]
        p_approved, l_approved = bool(project["approved"]), bool(loop["approved"])
        same_value = loop["value"] == project["value"]
        # The migration protects vetted content; the live path prefers the fact
        # that just got hot. See the docstring.
        keep_loop_value = origin == "migration" and l_approved and not p_approved
        value = loop["value"] if keep_loop_value else project["value"]
        displaced = project["value"] if keep_loop_value else loop["value"]
        # Approval of the *surviving* value, not of a particular row.
        approved_for_value = (p_approved and value == project["value"]) or (
            l_approved and value == loop["value"]
        )
        conflict = p_approved and l_approved and not same_value
        survivor_approved = approved_for_value and not conflict
        with self.transaction():
            self.memory_write(
                "loop",
                key,
                value,
                approved=survivor_approved,
                pinned=bool(project["pinned"]),
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO memory_hits (memory_id, task_id, ts)"
                " SELECT ?, task_id, ts FROM memory_hits WHERE memory_id=?",
                (loop_id, project_id),
            )
            self._conn.execute("DELETE FROM memory WHERE id=?", (project_id,))
            self._conn.execute(
                "UPDATE memory SET hit_count="
                "(SELECT COUNT(*) FROM memory_hits WHERE memory_id=?)"
                " WHERE id=?",
                (loop_id, loop_id),
            )
            # `memory_promoted` is the record of a promotion, so the migration
            # does not borrow it: these rows were promoted by the old build,
            # long before this database was opened, and counting them as
            # promotions would make the feed report a promotion per upgrade.
            self.log_event(
                None,
                (
                    "memory_promoted"
                    if origin == "promotion"
                    else "memory_duplicates_merged"
                ),
                {
                    "key": key,
                    "from": "project",
                    "to": "loop",
                    "hit_count": project["hit_count"],
                    "merged": True,
                    "displaced_value": displaced[:_MAX_EVENT_VALUE_CHARS],
                    "displaced_approved": (
                        p_approved if keep_loop_value else l_approved
                    ),
                },
            )
            if (p_approved or l_approved) and not survivor_approved:
                self.log_event(
                    None,
                    "memory_revoked",
                    {"tier": "loop", "key": key, "reason": "value replaced by merge"},
                )

    def memory_list(
        self, tier: str | None = None, approved_only: bool = False
    ) -> list[dict]:
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
            "SELECT tier, key FROM memory WHERE id=?", (mem_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"No memory row {mem_id}")
        with self.transaction():
            self._conn.execute(
                "UPDATE memory SET approved=? WHERE id=?", (int(approved), mem_id)
            )
            self.log_event(
                None,
                "memory_approved" if approved else "memory_revoked",
                {"tier": row["tier"], "key": row["key"]},
            )

    def memory_set_pinned(self, mem_id: int, pinned: bool) -> None:
        row = self._conn.execute(
            "SELECT tier, key FROM memory WHERE id=?", (mem_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"No memory row {mem_id}")
        with self.transaction():
            self._conn.execute(
                "UPDATE memory SET pinned=? WHERE id=?", (int(pinned), mem_id)
            )
            self.log_event(
                None,
                "memory_pinned" if pinned else "memory_unpinned",
                {"tier": row["tier"], "key": row["key"]},
            )

    def memory_delete(self, mem_id: int) -> None:
        row = self._conn.execute(
            "SELECT tier, key FROM memory WHERE id=?", (mem_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"No memory row {mem_id}")
        with self.transaction():
            self._conn.execute("DELETE FROM memory WHERE id=?", (mem_id,))
            self.log_event(
                None, "memory_deleted", {"tier": row["tier"], "key": row["key"]}
            )

    # -- executed test results (spec §5) -------------------------------------

    def add_test_run(
        self, task_id: int, attempt_id: int | None, result: TestResult
    ) -> int:
        with self.transaction():
            cur = self._conn.execute(
                "INSERT INTO test_runs (task_id, attempt_id, status, exit_code,"
                " summary, stdout_tail, duration_s, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    task_id,
                    attempt_id,
                    result.status,
                    result.exit_code,
                    result.summary,
                    result.stdout_tail,
                    result.duration_s,
                    time.time(),
                ),
            )
            self.log_event(
                task_id,
                "test_run",
                {
                    "status": result.status,
                    "exit_code": result.exit_code,
                    "summary": result.summary,
                    "duration_s": result.duration_s,
                },
            )
        return cur.lastrowid

    def test_runs(self, task_id: int) -> list[dict]:
        return [
            dict(r)
            for r in self._conn.execute(
                "SELECT * FROM test_runs WHERE task_id=? ORDER BY id", (task_id,)
            ).fetchall()
        ]

    # -- validator eval harness ----------------------------------------------

    def add_eval_run(
        self,
        runner: str,
        n_fixtures: int,
        agreement: float,
        summary: dict,
        detail: list,
    ) -> int:
        with self.transaction():
            cur = self._conn.execute(
                "INSERT INTO eval_runs (runner, n_fixtures, agreement, summary,"
                " detail, created_at) VALUES (?,?,?,?,?,?)",
                (
                    runner,
                    n_fixtures,
                    agreement,
                    json.dumps(summary),
                    json.dumps(detail),
                    time.time(),
                ),
            )
            self.log_event(
                None,
                "eval_run",
                {
                    "runner": runner,
                    "n_fixtures": n_fixtures,
                    "agreement": round(agreement, 4),
                },
            )
        return cur.lastrowid

    def eval_runs(self) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM eval_runs ORDER BY id").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["summary"] = json.loads(d["summary"])
            d["detail"] = json.loads(d["detail"])
            out.append(d)
        return out

    # -- change feed for the Phase-2 dashboard --------------------------------

    def events_since(self, event_id: int, limit: int = 500) -> list[dict]:
        """Audit-log rows after `event_id`. The append-only log doubles as the
        dashboard's change feed: monotonic ids make SSE resumable by cursor."""
        rows = self._conn.execute(
            "SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?", (event_id, limit)
        ).fetchall()
        return [dict(r) | {"payload": json.loads(r["payload"])} for r in rows]

    def latest_event_id(self) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS m FROM events"
        ).fetchone()
        return int(row["m"])

    def run_metrics(self) -> dict:
        """Run-level rollup across all tasks (spec §6)."""
        totals = self._conn.execute(
            "SELECT COALESCE(SUM(tokens_in),0) AS tin,"
            " COALESCE(SUM(tokens_out),0) AS tout,"
            " COALESCE(SUM(cache_creation_tokens),0) AS cwrite,"
            " COALESCE(SUM(cache_read_tokens),0) AS cread,"
            " COALESCE(SUM(cost_usd),0.0) AS cost,"
            " COUNT(*) AS attempts,"
            " COALESCE(SUM(finished_at-started_at),0) AS wall"
            " FROM attempts WHERE finished_at IS NOT NULL"
        ).fetchone()
        by_status = {
            r["status"]: r["n"]
            for r in self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
            ).fetchall()
        }
        by_model = [
            dict(r)
            for r in self._conn.execute(
                "SELECT model, COUNT(*) AS attempts,"
                " COALESCE(SUM(tokens_in),0) AS tokens_in,"
                " COALESCE(SUM(tokens_out),0) AS tokens_out,"
                " COALESCE(SUM(cost_usd),0.0) AS cost_usd"
                " FROM attempts GROUP BY model ORDER BY cost_usd DESC"
            ).fetchall()
        ]
        revisions = self._conn.execute(
            "SELECT COALESCE(SUM(revision_count),0) AS r FROM tasks"
        ).fetchone()
        cwrite, cread = int(totals["cwrite"]), int(totals["cread"])
        return {
            "tokens_in": int(totals["tin"]),
            "tokens_out": int(totals["tout"]),
            "cache_creation_tokens": cwrite,
            "cache_read_tokens": cread,
            "tokens": (int(totals["tin"]) + int(totals["tout"]) + cwrite + cread),
            "cost_usd": round(float(totals["cost"]), 6),
            "attempts": int(totals["attempts"]),
            "wall_seconds": round(float(totals["wall"]), 3),
            "revisions": int(revisions["r"]),
            "tasks_by_status": by_status,
            "by_model": by_model,
        }
