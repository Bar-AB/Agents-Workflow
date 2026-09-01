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
- `charter` is the human-authored, project-wide rule document, one row per
  *version* (`id` is the version). Append-only: the accessors only INSERT, the
  highest id is what is in force, and every attempt records the version it ran
  under, so "what did this task's agents actually read" stays answerable after
  the rules change. Agents have no write path to it at all.
- `tool_requests` is the capability ledger: one row per ask for one logical tool,
  for one task, by one role. An `auto`/`approved` row *is* the grant — there is no
  separate grant table, so a permission can never exist without the request that
  justifies it. It declares no foreign keys on purpose (see the DDL), and
  `release_claim` next to it is the sole writer of a `claimed_by` release, because
  a task returned to `pending` while still leased is unclaimable *and* starves the
  claim loop behind it.
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
from collections.abc import Callable
from pathlib import Path

from .models import (
    Task,
    TaskStatus,
    TestResult,
    ToolRequest,
    ToolRequestSource,
    ToolRequestStatus,
    Verdict,
)

# The logical -> concrete translation, carried on every tool-request event.
# **Both `git` and `shell` map to `["Bash"]`**, so a human shown "git — commit the
# fix" who approves it is actually granting unrestricted Bash, while the row, the
# decision event and the dashboard all say `git`. The read-only allowlist excludes
# both, so this is not an auto-approval bypass — it is the ledger's *label*
# understating the grant, on the one screen where a human's decision is the whole
# control. Fixed by making the ask self-describing rather than by narrowing the
# map: two logical names sharing a concrete tool is a legitimate thing to express,
# and rewriting it would change what every existing registry means.
from .runner import resolve_tools, tools_sharing_capability

# How many candidates a claim will try before giving up. Each retry means
# another process won that row, so this only bounds a pathological live-lock;
# in practice the loser's next look finds a different task or nothing.
_CLAIM_ATTEMPTS = 100

# Widest a coerced text field may be. Generous: this is a last-resort bound on a
# value that was already supposed to be a bounded string, not the field's real
# limit (`toolpolicy.MAX_TOOL_REASON_CHARS` is that, for the one field a caller
# fills from model output).
_MAX_COERCED_TEXT_CHARS = 4000


def _bounded_text(value, limit: int = _MAX_COERCED_TEXT_CHARS) -> str:
    """Any value as a bounded plain `str`, never raising.

    Used on the text columns of `tool_requests`, which is written from inside
    `_invoke`'s closing transaction over an already-paid `finish_attempt`: a
    `NOT NULL` violation on a `None` tool, a failed parameter bind on a non-`str`
    reason, or a `json.dumps` refusal on an unencodable `why` each rolled that
    attempt back, after which `_with_retry` bought the completion a second time.
    All three were measured, and the reachable one needs no exotic caller — an
    `agents.json` with `tools: [null]` loads unvalidated through
    `AgentSpec(**spec)`.

    A sibling of `agents._plain_str` rather than a shared helper: `agents`
    imports `store`, so moving one to the other's module would cycle, and a new
    module to hold six lines buys less than it costs. Kept here because this is
    the layer whose docstring makes the never-raises promise, so the guarantee
    and the code that provides it stay in one file.
    """
    if isinstance(value, str):
        return value[:limit]
    if value is None:
        # Not "unknown": these are columns a human reads back as an identifier,
        # and `None` is the literal thing the caller sent.
        return "None"[:limit]
    try:
        return str(value)[:limit]
    except Exception:
        # A `__str__` that raises would put us straight back in the transaction
        # this function exists to protect.
        return f"<unrepresentable {type(value).__name__}>"[:limit]


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

-- Human-authored, project-wide rules injected into every worker, validator and
-- planner prompt. One row per *version*: the charter is a document, and a past
-- attempt's recorded version must still be readable, not merely identifiable.
-- Append-only in practice — `charter_set` and `charter_clear` only ever INSERT,
-- and the row with the highest id is the charter in effect, so "what is in
-- force" is a pure function of the table and nothing can disagree with it.
-- Declared before `attempts` so the FK target exists when the script runs.
CREATE TABLE IF NOT EXISTS charter (
    id INTEGER PRIMARY KEY AUTOINCREMENT,   -- the version number
    body TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',          -- why this edit was made
    created_at REAL NOT NULL
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
    cost_usd REAL NOT NULL DEFAULT 0.0,        -- includes cache cost
    -- Which charter version was in the prompt of this invocation. NULL = none
    -- in effect. A per-invocation measurement, so it is a column on `attempts`
    -- rather than an event: "which tasks ran under v3" is then one join instead
    -- of a full-text scan of every `*_prompt` payload.
    charter_version INTEGER REFERENCES charter(id)
);

CREATE TABLE IF NOT EXISTS verdicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    attempt_id INTEGER REFERENCES attempts(id),
    kind TEXT NOT NULL,               -- approve | revise | escalate
    confidence REAL NOT NULL,
    reasoning TEXT NOT NULL DEFAULT '',
    tests_passed INTEGER,             -- NULL = n/a
    -- What the validator says it checked and what it found. A *copy* of a slice
    -- of `reasoning`, never a piece removed from it: `reasoning` is fed back to
    -- the worker as revision feedback, so subtracting the findings out would
    -- strip the most actionable part of every revision prompt. Evidence, not a
    -- gate — nothing in the loop reads this column.
    findings TEXT NOT NULL DEFAULT '',
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
    -- Coverage as the test command reported it, NULL when it reported none
    -- (slice 6). NULL is not 0%. A display value: nothing in the loop reads it.
    coverage_percent REAL,
    created_at REAL NOT NULL
);

-- The git config pin (slice 6). A task's workspace repo keeps its `.git/config`
-- *inside* the agent-writable workspace, and git config names programs git runs
-- (`filter.<name>.clean`, `core.fsmonitor`), so an unpinned round commit is
-- arbitrary command execution from a worker that wrote only inside its own
-- directory -- measured, on `allow_test_exec=False`. The fingerprint of the
-- config `git init` wrote therefore lives *here*: the store is the one place in
-- this system an agent has no write path to, and a pin kept anywhere under the
-- workspace would be as writable as the attack it is meant to catch.
--
-- One row per task, because there is one repo per task workspace (DD-1), and
-- `task_id` is the primary key rather than an autoincrement id: this is a live
-- fact with exactly one current value, not history (the audit log is the
-- history), so a re-created workspace *replaces* its pin instead of adding a
-- second one a lookup would have to choose between.
--
-- No foreign key, for `tool_requests`' reason: these rows are written on the
-- paid per-round path and an FK violation there must never be able to roll back
-- an attempt.
CREATE TABLE IF NOT EXISTS vcs_pins (
    task_id     INTEGER PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    created_at  REAL NOT NULL
);

-- Agent-requested tools (roadmap slice 5): one row per ask for one logical tool,
-- for one task, by one role. The ledger *is* the grant table — an `auto` or
-- `approved` row is the permission — because a second `tool_grants` table would
-- let a grant exist with no request behind it and the ledger would stop being
-- the audit trail.
--
-- Deliberately declares **no foreign keys**, unlike every other table here.
-- `PRAGMA foreign_keys=ON` is live, and these rows are written inside
-- `agents._invoke`'s closing transaction, which already holds a paid
-- `finish_attempt`: an FK violation there would roll back the tokens and cost of
-- a completion the provider has already billed, and the retry would pay again.
-- Its absence is load-bearing, not an oversight.
--
-- `REAL` timestamps, like every other timestamp column, written from
-- `time.time()`. TEXT affinity would store '1780000000.123456', hand a `str`
-- back through a field annotated `float`, make ORDER BY lexicographic — and
-- `_migrate()` only *adds* columns, so it could never be corrected afterwards.
--
-- UNIQUE(task_id, role, tool) is the dedupe, and it absorbs three real cases at
-- once: an agent repeating its marker across revision rounds, a validator
-- quoting the worker's marker, and a re-request of an already-denied tool. Only
-- a `pending` row may be upgraded optional -> blocking; a decided row carries a
-- decision an agent must not reopen.
CREATE TABLE IF NOT EXISTS tool_requests (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      INTEGER NOT NULL,
    attempt_id   INTEGER,
    role         TEXT NOT NULL,
    -- Which agent asked, as the loop's own literal ('worker' | 'validator' |
    -- 'planner'). NOT the same string as `role`: `role` is the registry spec's
    -- role, and a custom `task.worker_role` makes the two differ, so collapsing
    -- them would destroy the one column that answers "who asked for this".
    agent_kind   TEXT NOT NULL,
    tool         TEXT NOT NULL,
    reason       TEXT NOT NULL DEFAULT '',
    -- What the agent asked for: blocking means "I cannot finish without this".
    blocking     INTEGER NOT NULL DEFAULT 0,
    -- What the loop did about it: this task is being held at NEEDS_HUMAN right
    -- now, on this row. A live fact, not history (the audit log is the history),
    -- so every exit from the parked state clears it — otherwise a stale flag
    -- would let a tool approval revert an escalation the tool queue never caused.
    parked       INTEGER NOT NULL DEFAULT 0,
    source       TEXT NOT NULL,   -- 'marker' | 'declared'
    status       TEXT NOT NULL,   -- 'auto'|'pending'|'approved'|'rejected'|'refused'
    decided_by   TEXT NOT NULL DEFAULT '',
    decided_note TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL,
    decided_at   REAL,
    UNIQUE(task_id, role, tool)
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
    -- Which harness wrote this row, and therefore what `agreement` counts:
    -- 'verdict' = one validator verdict kind against gold, 'batch' = one whole
    -- task's final TaskStatus against gold. One number, one meaning, one
    -- discriminator -- a second table would have duplicated the row shape,
    -- which is already exactly right, and split "the eval history" in two.
    kind TEXT NOT NULL DEFAULT 'verdict',
    created_at REAL NOT NULL
);
"""


# How much of a displaced memory value a merge event keeps. Enough to recognise
# and recover what was overwritten; not a second copy of the store.
_MAX_EVENT_VALUE_CHARS = 400

# Largest charter a human may set. Refused *loudly at write time* rather than
# trimmed at inject time: the charter is an input the agent must obey in full,
# so a rule that silently falls off the end of a cap is worse than no rule.
# ~4000 chars is roughly 1000 tokens, against a memory block that can already
# reach ~8000. A module constant, like the other prompt-shape bounds, not a
# LoopConfig field — LoopConfig holds decision thresholds and budget caps.
_MAX_CHARTER_CHARS = 4000


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
            # Same FK trap and same handling as `plan_id` above: fresh dbs get
            # `REFERENCES charter(id)` from _SCHEMA, older dbs get the bare
            # column. NULL means "no charter was in effect", which is exactly
            # what every pre-charter attempt row correctly becomes.
            ("attempts", "charter_version", "INTEGER"),
            ("verdicts", "findings", "TEXT NOT NULL DEFAULT ''"),
            ("test_runs", "coverage_percent", "REAL"),
            # Pre-existing rows are per-verdict calibration runs, because that
            # is the only harness that existed when they were written -- so the
            # default is not a placeholder, it is the correct value.
            ("eval_runs", "kind", "TEXT NOT NULL DEFAULT 'verdict'"),
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

    def release_claim(self, task_id: int) -> None:
        """Drop a task's `claimed_by` lease — the sole writer of the release.

        Every path that returns a task to `PENDING` must call this. Nothing else
        in the store ever clears the column, so a release that only wrote the
        status left the row `pending` *and* leased: `claim_next_task`'s
        compare-and-swap (`WHERE status='pending' AND claimed_by IS NULL`) can
        then never match it, while the SELECT still keeps finding it — so the task
        is not merely stuck, it burns every claim attempt and starves the pending
        work behind it.

        A store accessor rather than a field on `update_task`, for the same reason
        `update_task` already omits `control`: the loop holds a `Task` loaded at
        the start of a run, and writing a stale ownership field from it would
        clobber a concurrent claim. `set_control` is the precedent.

        The UPDATE is guarded on `claimed_by IS NOT NULL` and the event is logged
        only when it matched — `memory_read`'s discipline. A lease taken is
        audited (`task_claimed`), so a lease dropped is too; but a release that
        released nothing must not put a `claim_released` in the log, or `resume`
        on an unclaimed task would manufacture a release that never happened.
        """
        with self.transaction():
            row = self._conn.execute(
                "SELECT claimed_by FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            worker = row["claimed_by"] if row else None
            cur = self._conn.execute(
                "UPDATE tasks SET claimed_by=NULL, updated_at=? WHERE id=?"
                " AND claimed_by IS NOT NULL",
                (time.time(), task_id),
            )
            if cur.rowcount == 1:
                self.log_event(task_id, "claim_released", {"worker": worker})

    def reset_unowned_to_pending(
        self, task_id: int, from_statuses: list[TaskStatus]
    ) -> bool:
        """Hand an unowned, mid-flight row back to the queue — as a **CAS**.

        The one write an evicted worker still has to make (`Loop._claim_lost`):
        the row holds no lease and sits at a transient status only this worker
        could have stamped, so it matches neither disjunct of the claim SELECT and
        is invisible to `stranded_claims` while `next_pending_task` advertises it.

        A compare-and-swap because the caller cannot hold that fact still. It read
        the row, released the lock, formatted a `RuntimeWarning` to stderr and only
        then opened its transaction — and a `human_approve` landing in that window
        left a signed-off `DONE` task returned to `PENDING` and re-claimed, with
        its graph dependents already released by the `DONE`. `set_status` cannot be
        the CAS: it writes the whole row unconditionally, from a `Task` object, so
        it would put back whatever that object was holding. This is the fourth
        instance of the shape in this store and the remedy is the existing one —
        `claim_next_task`, `tool_request_decide` and `tool_requests_mark_parked`
        all guard on a predicated UPDATE's `rowcount`.

        `from_statuses` is the caller's list (`Loop._IN_FLIGHT`) rather than a
        second copy of it here: which statuses a live worker may be inside is a
        loop fact, and two copies of it would drift the first time a status is
        added.

        `escalation_reason` is blanked explicitly. `set_status(..., reason="")`
        assigns only a truthy reason, so a task that had once been escalated came
        back to the queue carrying the old diagnosis — `approve_tool_request` and
        `human_redo` both blank it for the same reason.

        Returns whether the swap took, and logs only then: a `status:pending`
        event for a row that was already claimed by somebody else would be a
        transition that never happened (`release_claim`'s discipline).
        """
        values = [s.value for s in from_statuses]
        if not values:
            return False
        with self.transaction():
            placeholders = ",".join("?" for _ in values)
            cur = self._conn.execute(
                "UPDATE tasks SET status=?, escalation_reason='', updated_at=?"
                f" WHERE id=? AND claimed_by IS NULL AND status IN ({placeholders})",
                (TaskStatus.PENDING.value, time.time(), task_id, *values),
            )
            if cur.rowcount != 1:
                return False
            self.log_event(task_id, f"status:{TaskStatus.PENDING.value}", {})
        return True

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

    def update_task(self, task: Task) -> bool:
        """Write a `Task`'s mutable columns. **Predicated on the lease it holds.**

        Deliberately does NOT write the `control` column: the loop holds a task
        object loaded at the start of the run, and writing its stale `control`
        here would clobber a pause/abort a human set concurrently from another
        process. `set_control` is the sole writer of control.

        The same argument applies to the columns it *does* write, and did not use
        to. A `Task` carrying a lease is a worker's in-round view of the row, and
        a human can release that lease mid-round: `human_redo` resets `output` and
        `revision_count` and hands the lease back, and the evicted worker then
        wrote its own stale values straight over the reset — `Loop._claim_lost`
        cannot help, because those writes land *before* the next boundary, so the
        "freshly loaded row" it reasons about already holds them. What came out was
        a half-applied redo: workspace wiped, output and revision budget carried,
        and the row runnable rather than visibly stranded.

        So the UPDATE carries `AND claimed_by=?` whenever the object holds a
        lease — the CAS discipline of `claim_next_task`, `tool_request_decide`,
        `tool_requests_mark_parked` and `reset_unowned_to_pending`. A released
        worker's write matches no row and no-ops. With no lease in hand (a direct
        `run_task`, as most tests and every human decision path do) there is no
        ownership to check and the write is unconditional, exactly as before.

        Returns whether the row was written, so a caller pairing it with an event
        does not log a transition that did not happen.
        """
        sql = (
            "UPDATE tasks SET status=?, revision_count=?, output=?,"
            " escalation_reason=?, updated_at=? WHERE id=?"
        )
        params: tuple = (
            task.status.value,
            task.revision_count,
            task.output,
            task.escalation_reason,
            time.time(),
            task.id,
        )
        if task.claimed_by:
            sql += " AND claimed_by=?"
            params += (task.claimed_by,)
        return self._conn.write(sql, params).rowcount == 1

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

    def set_status(self, task: Task, status: TaskStatus, reason: str = "") -> bool:
        """Write a status and its event as one commit. Returns whether it landed —
        `update_task` is lease-predicated, so a caller pairing further writes with
        this transition (the tool-request park's `parked` stamp) can follow the row
        rather than its own intention."""
        task.status = status
        if reason:
            task.escalation_reason = reason
        with self.transaction():  # row change + its event: one commit
            # The event follows the row, never the intent: `update_task` is
            # lease-predicated, so a worker whose lease a human took mid-round
            # writes nothing here — and a `status:revising` in the log for a
            # transition that never landed would be the audit trail describing the
            # loop's belief rather than the task's history.
            if not self.update_task(task):
                return False
            self.log_event(
                task.id, f"status:{status.value}", {"reason": reason} if reason else {}
            )
        return True

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

    # -- project charter -----------------------------------------------------

    def charter_active(self) -> tuple[int, str] | None:
        """The charter in effect as `(version, body)`, or None if there is none.

        "None" covers both never-set and explicitly cleared, deliberately: a
        cleared charter must be indistinguishable from one that never existed,
        so turning the charter off restores byte-for-byte the prompts the loop
        built before it was introduced.
        """
        row = self._conn.execute(
            "SELECT id, body FROM charter ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None or not row["body"].strip():
            return None
        return int(row["id"]), row["body"]

    def charter_set(self, body: str, note: str = "") -> int:
        """Publish a new charter version. Returns the new version number.

        Validation happens *before* anything is written, so a refusal leaves the
        previous version in force and puts nothing in the audit log. Both
        failures are loud on purpose:

        - Over `_MAX_CHARTER_CHARS`: the alternative is trimming at inject time,
          which drops a rule the human believes is being enforced.
        - Whitespace-only: a typo'd `>` redirect that empties a rules file must
          not be a silent policy removal. `charter_clear` is the explicit,
          audited way to turn the charter off.
        """
        if len(body) > _MAX_CHARTER_CHARS:
            raise ValueError(
                f"charter is {len(body)} chars, over the {_MAX_CHARTER_CHARS} "
                f"limit; shorten it rather than having it silently truncated"
            )
        if not body.strip():
            raise ValueError(
                "refusing to set an empty charter; use `charter clear` to "
                "remove the charter explicitly"
            )
        return self._charter_insert(body, note, "charter_set")

    def charter_clear(self, note: str = "") -> int:
        """Turn the charter off by appending an empty version.

        A new row rather than a DELETE: the table is the whole history, and a
        past attempt's `charter_version` has to stay readable. The clear itself
        is a version, so "when did the rules stop applying" is in the record.
        """
        return self._charter_insert("", note, "charter_cleared")

    def _charter_insert(self, body: str, note: str, event: str) -> int:
        with self.transaction():
            cur = self._conn.execute(
                "INSERT INTO charter (body, note, created_at) VALUES (?,?,?)",
                (body, note, time.time()),
            )
            version = cur.lastrowid
            # The body is not duplicated into the payload: unlike a memory
            # merge's displaced value, the charter row keeps it forever, and
            # every event row is pushed to every open dashboard stream.
            payload = {"version": version, "note": note}
            if event == "charter_set":
                payload["n_chars"] = len(body)
            self.log_event(None, event, payload)
        return version

    def charter_history(self) -> list[dict]:
        """Every version, oldest first. There is no update or delete path."""
        return [
            dict(r)
            for r in self._conn.execute("SELECT * FROM charter ORDER BY id").fetchall()
        ]

    def charter_version(self, version: int) -> dict | None:
        """The text a past attempt actually ran under, by version number."""
        row = self._conn.execute(
            "SELECT * FROM charter WHERE id=?", (version,)
        ).fetchone()
        return dict(row) if row else None

    # -- attempts / metrics --------------------------------------------------

    def start_attempt(
        self,
        task_id: int,
        kind: str,
        role: str,
        model: str,
        charter_version: int | None = None,
    ) -> int:
        cur = self._conn.write(
            "INSERT INTO attempts (task_id, kind, agent_role, model, started_at,"
            " charter_version) VALUES (?,?,?,?,?,?)",
            (task_id, kind, role, model, time.time(), charter_version),
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
            "SELECT kind, confidence, tests_passed, findings FROM verdicts"
            " WHERE task_id=? ORDER BY id",
            (task_id,),
        ).fetchall()
        # Which charter version(s) this task's agents actually ran under, so
        # "was this approved under the old rules" is answerable where the
        # approve button is. NULLs (no charter in effect) are left out rather
        # than rendered as a version nobody can look up.
        charter_versions = [
            int(r["charter_version"])
            for r in self._conn.execute(
                "SELECT DISTINCT charter_version FROM attempts"
                " WHERE task_id=? AND charter_version IS NOT NULL"
                " ORDER BY charter_version",
                (task_id,),
            ).fetchall()
        ]
        # What this task's agents asked for and what was decided. Selected here
        # because this is the only task data the dashboard gets: a column stored
        # and exposed nowhere is a queue nobody can clear, and looks implemented.
        tool_requests = self._conn.execute(
            "SELECT * FROM tool_requests WHERE task_id=? ORDER BY id", (task_id,)
        ).fetchall()
        return {
            "tokens": toks,
            "cost_usd": round(cost, 6),
            "attempts": row["n"],
            "wall_seconds": round(row["wall"], 3),
            "verdicts": [dict(v) for v in verdicts],
            "charter_versions": charter_versions,
            "tool_requests": [dict(r) for r in tool_requests],
        }

    # -- verdicts ------------------------------------------------------------

    def add_verdict(self, task_id: int, attempt_id: int | None, v: Verdict) -> int:
        with self.transaction():
            cur = self._conn.execute(
                "INSERT INTO verdicts (task_id, attempt_id, kind, confidence,"
                " reasoning, tests_passed, findings, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (
                    task_id,
                    attempt_id,
                    v.kind.value,
                    v.confidence,
                    v.reasoning,
                    None if v.tests_passed is None else int(v.tests_passed),
                    v.findings,
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
                    # A flag, not the text: the verdict row is the record, and
                    # every event is pushed to every connected dashboard, so a
                    # 4000-char blob per round would bloat the SSE feed.
                    "has_findings": bool(v.findings),
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
                " summary, stdout_tail, duration_s, coverage_percent,"
                " created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    task_id,
                    attempt_id,
                    result.status,
                    result.exit_code,
                    result.summary,
                    result.stdout_tail,
                    result.duration_s,
                    result.coverage_percent,
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

    # -- the workspace git config pin (slice 6) -------------------------------

    def set_vcs_pin(self, task_id: int, fingerprint: str) -> None:
        """Record the fingerprint of the `.git/config` `vcs.init_repo` just
        watched `git init` write for this task's workspace.

        Replaces rather than appends: a workspace that is wiped and re-created
        (`human_redo`) gets a new config and therefore a new pin, and a lookup
        that had to choose between two would be a gate with a second answer in
        it. No paired event, deliberately — this is derived bookkeeping about a
        directory, not a state change of the task, and what the audit log has
        to carry is the *refusal* it produces (`vcs_unavailable`, via the
        loop's `_vcs_degraded`), which is the only part a human can act on."""
        self._conn.write(
            "INSERT INTO vcs_pins (task_id, fingerprint, created_at) "
            "VALUES (?, ?, ?) ON CONFLICT(task_id) DO UPDATE SET "
            "fingerprint=excluded.fingerprint, created_at=excluded.created_at",
            (task_id, fingerprint, time.time()),
        )

    def vcs_pin(self, task_id: int) -> str:
        """The recorded pin, or `""` when there is none — which every
        side-effecting `vcs` entry point refuses on (`"config-unpinned"`), so
        an absent row fails closed rather than opening the gate."""
        rows = self._conn.execute(
            "SELECT fingerprint FROM vcs_pins WHERE task_id=?", (task_id,)
        ).fetchall()
        return rows[0]["fingerprint"] if rows else ""

    # -- agent tool requests (roadmap slice 5) --------------------------------

    # Which event kind records a fresh row, by the status it was created with. A
    # mapping rather than branches because the three are one decision — "what
    # happened to this ask" — and an unlisted status would otherwise log nothing
    # at all, which is a row without its event.
    _TOOL_REQUEST_EVENTS = {
        ToolRequestStatus.AUTO.value: "tool_auto_approved",
        ToolRequestStatus.REFUSED.value: "tool_request_refused",
    }

    # What the two enum-valued columns may hold. Checked in Python rather than as
    # a DDL `CHECK`: this table is written inside `_invoke`'s closing transaction,
    # which holds an already-paid `finish_attempt`, so a constraint violation
    # there would discard tokens the provider billed and `_with_retry` would buy
    # them again — the hazard ADR-4 and the deliberate absence of foreign keys
    # both exist to avoid. An out-of-enum value is therefore *coerced*, never
    # raised on: without either guard an `INSERT` succeeded and then
    # `_row_to_tool_request` raised `ValueError` on every later read, so one bad
    # write made the whole ledger — CLI, REST and dashboard — permanently
    # unreadable while `granted_tools` silently skipped the row.
    _TOOL_REQUEST_STATUSES = frozenset(s.value for s in ToolRequestStatus)
    _TOOL_REQUEST_SOURCES = frozenset(s.value for s in ToolRequestSource)

    def tool_request_add(
        self,
        task_id: int,
        role: str,
        agent_kind: str,
        tool: str,
        status: str,
        source: str,
        reason: str = "",
        blocking: bool = False,
        attempt_id: int | None = None,
        why: str = "",
        max_per_task: int | None = None,
    ) -> int | None:
        """Record an ask for one tool, or upgrade the pending one that exists.

        Returns the row id when a request is now standing on the agent's behalf,
        and **None** when nothing was: the tool was already decided (so the
        decision holds), or the task is at its request cap (so a `refused` row was
        written instead). It **never raises on either path, for any argument** —
        this runs inside `_invoke`'s closing transaction, which holds an
        already-paid `finish_attempt`, and a raise there would discard the tokens
        and cost of a completion the provider has billed, after which the retry
        pays again. That is why the UNIQUE collision is resolved by an explicit
        read-then-update rather than by letting `INSERT` raise `IntegrityError`,
        and why every text argument goes through `_bounded_text` and the cap
        through `int()` before either can reach a parameter bind, a `NOT NULL`
        column, a frozenset membership test or `log_event`'s one `json.dumps`.
        The claim was originally made ahead of the code: three inputs broke it,
        and the reachable one needed no exotic caller (`tools: [null]` in a
        hand-edited agents.json).

        What makes that read-then-write race-free is **the claim, not the lock**:
        the connection lock only serializes one process, and `tool_request_decide`
        documents why that is not enough for a row two humans can reach. This path
        is different because a task is held by exactly one worker — the atomic
        claim's whole guarantee — so exactly one agent invocation is writing
        requests for a given `task_id` at a time, whatever the parallelism. Take
        that away and this would need `tool_request_decide`'s compare-and-swap.

        `parked` is not a parameter. Only the loop's park writes it, so a fresh
        row can never be born holding a task at `NEEDS_HUMAN`.

        The upgrade rule is pending-only: round 1 may ask `(optional)` and round 2
        `(blocking)`, and a plain `INSERT OR IGNORE` would silently drop that, so a
        task that should park never would. An `approved`/`rejected`/`refused` row
        carries a decision — human or terminal — that an agent must not reopen by
        re-asking, which is what keeps a denial from ping-ponging.

        `status` and `source` are coerced to their enums (see
        `_TOOL_REQUEST_STATUSES`), with the original value recorded in `why`:
        `refused` is the fail-safe landing place because it grants nothing and
        parks nothing.
        """
        # Every text column, coerced before it can reach a bind or a
        # `json.dumps`. The three raise-sources this closes were measured, not
        # imagined, and `_bounded_text` records which one each was; the callers
        # coerce too (`agents._tool_name_repr` / `_plain_str`,
        # `toolpolicy.tools_for`), but the promise this docstring makes is about
        # *this* function, so it is kept here rather than delegated upward.
        role = _bounded_text(role)
        agent_kind = _bounded_text(agent_kind)
        tool = _bounded_text(tool)
        reason = _bounded_text(reason)
        why = _bounded_text(why)
        # `status`/`source` are compared against a frozenset below, which raises
        # `TypeError` on an unhashable value rather than reporting a miss; and the
        # cap is compared with `>=`, which raises on a `str` — reachable, because
        # `loopconfig.json` is loaded without type checking. Coerced up here, so
        # the whole class is closed rather than the three inputs that were
        # measured. This `except` is not ADR-4's prohibition: that one is about
        # wrapping a *nested transaction*, and this touches no store at all.
        status = _bounded_text(status)
        source = _bounded_text(source)
        try:
            max_per_task = None if max_per_task is None else int(max_per_task)
        except (TypeError, ValueError):
            max_per_task = None

        out_of_enum = []
        if status not in self._TOOL_REQUEST_STATUSES:
            out_of_enum.append(f"status {status!r}")
            status = ToolRequestStatus.REFUSED.value
        if source not in self._TOOL_REQUEST_SOURCES:
            out_of_enum.append(f"source {source!r}")
            source = ToolRequestSource.MARKER.value
        if out_of_enum:
            coerced = f"out-of-enum {' and '.join(out_of_enum)} coerced at write time"
            why = f"{coerced}; {why}" if why else coerced

        with self.transaction():
            existing = self._conn.execute(
                "SELECT * FROM tool_requests WHERE task_id=? AND role=? AND tool=?",
                (task_id, role, tool),
            ).fetchone()
            if existing is not None:
                upgradable = (
                    existing["status"] == ToolRequestStatus.PENDING.value
                    and blocking
                    and not existing["blocking"]
                )
                if not upgradable:
                    return None
                request_id = int(existing["id"])
                self._conn.execute(
                    "UPDATE tool_requests SET blocking=1 WHERE id=?", (request_id,)
                )
                self.log_event(
                    task_id,
                    "tool_requested",
                    {
                        "request_id": request_id,
                        "attempt_id": attempt_id,
                        "agent_kind": agent_kind,
                        "role": role,
                        "tool": tool,
                        "resolved": resolve_tools([tool]),
                        "also_decides": tools_sharing_capability(tool),
                        "blocking": True,
                        "source": existing["source"],
                        "reason": existing["reason"],
                        "status": existing["status"],
                        "upgraded": True,
                    },
                )
                return request_id

            over_cap = False
            if max_per_task is not None:
                # `pending` only: literally the queue a human still has to clear.
                # Every other status is *answered* — `auto` and `refused` by the
                # machine, `approved` and `rejected` by a human — and counting an
                # answered row as queue made the bound self-fulfilling. Once a task
                # held `max_per_task` of them, *nothing* further could be recorded
                # on it: an auto-approvable read-only request came back `refused`
                # with no grant possible on that task again, and a `blocking` one
                # was stored `status='refused'` where
                # `pending_blocking_tool_requests` cannot see it, so "I cannot
                # finish without this" became "continue without it and tell no
                # human". Excluding only `refused` moved that dead end one status
                # along rather than removing it: `UNIQUE(task_id, role, tool)` is
                # per role, so three roles over seven logical names — plus a
                # declared row per role per tool under `gate_declared_tools` —
                # reach ten decided rows on one task.
                #
                # Nothing is opened by narrowing it. `auto` is bounded by the
                # read-only allowlist, and `approved`/`rejected` each cost a human
                # a decision, which is the only budget this cap was ever
                # protecting.
                held = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM tool_requests WHERE task_id=?"
                    " AND status = ?",
                    (task_id, ToolRequestStatus.PENDING.value),
                ).fetchone()
                over_cap = int(held["n"]) >= max_per_task
            if over_cap:
                # Refused *and audited*, never silently dropped: an agent emitting
                # 500 markers must not hand a human 500 rows to clear, but a
                # request that vanished without a trace is worse than one denied.
                # The row is what makes it once — every repeat of the same tool
                # now finds it and returns above, so the event fires a single time.
                # Repeats *of one tool*, that is: distinct names each get their own
                # row, which is what `config.max_tool_requests_per_task`'s comment
                # states.
                status = ToolRequestStatus.REFUSED.value
                # The cap's reason wins and the caller's rides along, rather than
                # the reverse: `tools_for` passes "not a known logical tool name"
                # on the same call, and a row refused *for the cap* audited as a
                # bad tool name is a wrong answer to "why was this withheld".
                cap_why = f"over the per-task cap of {max_per_task} tool requests"
                why = f"{cap_why}; {why}" if why else cap_why

            cur = self._conn.execute(
                "INSERT INTO tool_requests (task_id, attempt_id, role, agent_kind,"
                " tool, reason, blocking, source, status, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    task_id,
                    attempt_id,
                    role,
                    agent_kind,
                    tool,
                    reason,
                    int(bool(blocking)),
                    source,
                    status,
                    time.time(),
                ),
            )
            request_id = int(cur.lastrowid)
            payload = {
                "request_id": request_id,
                "attempt_id": attempt_id,
                "agent_kind": agent_kind,
                "role": role,
                "tool": tool,
                # What the name actually confers, beside the name itself.
                "resolved": resolve_tools([tool]),
                # And which *other* logical names a decision on this one settles,
                # because the map is not injective and enforcement is over the
                # concrete capability: denying `shell` also stops `git` working.
                # Recorded on the request rather than only on the withholding
                # event, since this is the text an approve prompt is built from and
                # the consequence has to be legible *before* the click, not after.
                # Pure (`tools_sharing_capability` reads one dict), which is what
                # lets it sit in a payload inside a paid transaction.
                "also_decides": tools_sharing_capability(tool),
                "blocking": bool(blocking),
                "source": source,
                "reason": reason,
                "status": status,
                "upgraded": False,
            }
            if why:
                payload["why"] = why
            self.log_event(
                task_id,
                self._TOOL_REQUEST_EVENTS.get(status, "tool_requested"),
                payload,
            )
            return None if over_cap else request_id

    def tool_requests(
        self, task_id: int | None = None, status: str | None = None
    ) -> list[ToolRequest]:
        """The request ledger, oldest first — what the CLI and the dashboard list."""
        q = "SELECT * FROM tool_requests"
        where, params = [], []
        if task_id is not None:
            where.append("task_id=?")
            params.append(task_id)
        if status:
            where.append("status=?")
            params.append(status)
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY id"
        return [
            self._row_to_tool_request(r)
            for r in self._conn.execute(q, tuple(params)).fetchall()
        ]

    def tool_request_get(self, request_id: int) -> ToolRequest | None:
        row = self._conn.execute(
            "SELECT * FROM tool_requests WHERE id=?", (request_id,)
        ).fetchone()
        return self._row_to_tool_request(row) if row else None

    def tool_request_decide(
        self,
        request_id: int,
        approved: bool,
        by: str = "human",
        note: str = "",
        released: bool | Callable[[], bool] = False,
    ) -> ToolRequest:
        """Record a human's approve/reject on a pending request.

        Only a `pending` row may be decided. `auto` and `refused` were decided by
        the machine and `approved`/`rejected` by a human, and re-deciding either
        would let a second click reverse a permission the audit trail already
        recorded as final — so this raises `ValueError`, which the CLI renders as
        `error: …` and the REST layer as a 400.

        `released` is the *caller's* fact, not this accessor's: whether the loop's
        park was lifted by this decision is a decision-rule judgment that belongs
        to `Loop`, and it rides the event so the audit trail says what the human's
        click actually did.

        It may be passed as a **callable**, and that is how `Loop` passes it. The
        predicate reads other rows on the same task ("does another parked blocking
        row still stand?"), so evaluating it *before* this UPDATE is the same
        check-then-act this method's own CAS exists to refuse: two processes
        clearing a two-row queue both read "one other row still pending", both
        concluded "do not release", both granted — and the task was stranded at
        `NEEDS_HUMAN` with an empty queue and no route back. Called from here it
        runs after this row's flip and inside this transaction, so sqlite's write
        lock has already serialized the two decisions and the second one sees the
        first's committed row. A `bool` is still accepted for the callers whose
        answer does not depend on the flip (`reject_tool_request`, and every test
        that decides a row directly).

        The decision is a **compare-and-swap**, and the read that finds the row
        lives inside the same transaction — `claim_next_task`'s discipline, for
        `claim_next_task`'s reason. `server.py` is a `ThreadingHTTPServer` and a
        CLI process holds its own connection and its own lock, and sqlite3 opens
        no write transaction for a `SELECT`: with the check outside, two humans
        (or one double-click) both passed it, both updated `WHERE id=?`, and the
        row kept whichever verdict committed last while *both* decisions sat in
        the append-only log with nothing saying which took effect. Worse, each
        call returned the status it thought it had written, so the loser's
        `ToolRequest` said `approved` over a rejected row — and an approve landing
        last turns a human's denial of `shell` into a grant `granted_tools` hands
        to the next invocation. So the guard is the `rowcount` of a
        status-predicated UPDATE, never a prior read.
        """
        status = (
            ToolRequestStatus.APPROVED.value
            if approved
            else ToolRequestStatus.REJECTED.value
        )
        with self.transaction():
            row = self._conn.execute(
                "SELECT * FROM tool_requests WHERE id=?", (request_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"No tool request {request_id}")
            cur = self._conn.execute(
                "UPDATE tool_requests SET status=?, decided_by=?, decided_note=?,"
                " decided_at=? WHERE id=? AND status=?",
                (
                    status,
                    by,
                    note,
                    time.time(),
                    request_id,
                    ToolRequestStatus.PENDING.value,
                ),
            )
            if cur.rowcount != 1:
                # One extra read, inside the transaction, so "no such row" and
                # "already decided" stay distinguishable: the CLI renders the
                # first as a 404-ish `error:` and the REST layer maps `KeyError`
                # to 404 and `ValueError` to 400.
                current = self._conn.execute(
                    "SELECT status FROM tool_requests WHERE id=?", (request_id,)
                ).fetchone()
                if current is None:
                    raise KeyError(f"No tool request {request_id}")
                raise ValueError(
                    f"Tool request {request_id} is already {current['status']}; "
                    f"a decided request is final."
                )
            # Evaluated here and nowhere earlier: after the CAS above, before the
            # event that reports it, inside this transaction.
            released_now = bool(released() if callable(released) else released)
            self.log_event(
                row["task_id"],
                "tool_request_decided",
                {
                    "request_id": request_id,
                    "tool": row["tool"],
                    # The decision event carries it too: this is what an audit
                    # reads to answer "what was this human actually granting".
                    "resolved": resolve_tools([row["tool"]]),
                    # A rejection takes the concrete capability away, so it also
                    # ends these — the decision event says so on its own, without a
                    # reader having to find the request that preceded it.
                    "also_decides": tools_sharing_capability(row["tool"]),
                    "role": row["role"],
                    "approved": bool(approved),
                    "by": by,
                    "note": note,
                    "released": released_now,
                },
            )
        return self.tool_request_get(request_id)

    def granted_tools(self, task_id: int, role: str) -> list[str]:
        """Logical tools this role may use on this task, in request order.

        An `auto` or `approved` row *is* the grant. Request order (ascending id)
        rather than alphabetical because the caller appends these to the declared
        list and the resulting `tools` order is recorded in the `{kind}_prompt`
        event — so the order is part of the audit trail, and must be derived from
        something that cannot change under a rename.
        """
        return [
            r["tool"]
            for r in self._conn.execute(
                "SELECT tool FROM tool_requests WHERE task_id=? AND role=?"
                " AND status IN (?,?) ORDER BY id",
                (
                    task_id,
                    role,
                    ToolRequestStatus.AUTO.value,
                    ToolRequestStatus.APPROVED.value,
                ),
            ).fetchall()
        ]

    def withheld_tools(
        self, task_id: int, role: str, statuses: tuple[str, ...] | None = None
    ) -> list[str]:
        """Logical tools this role asked for and does not have, in request order.

        The mirror of `granted_tools`, and the exact complement a *human's*
        decision governs: `rejected` is a denial they made and `pending` is one
        they have not made yet. `toolpolicy.subtract_withheld` takes the concrete
        capability of these away, so which statuses appear here is a decision rule
        rather than a query detail.

        `refused` is deliberately absent. It is the machine declining to queue an
        ask — an unknown logical name (which confers nothing concrete anyway) or
        one over the per-task cap — so nobody was ever shown a closed gate; and
        including it would let an agent's own chattiness strip its role's shipped
        baseline through the cap, with no human in the loop.

        `statuses` narrows the read to a subset of that same complement, and exists
        for exactly one caller question: **which of these did a human actually
        decide?** `toolpolicy.tools_for` needs the `pending` subset because a row
        nobody has decided must not revoke a capability the role holds
        unconditionally, while a `rejected` one must. The store stays a
        parameterised read either way — *which* names the role already holds is a
        policy fact (the read-only allowlist and the registry baseline), and this
        module knows neither.
        """
        statuses = statuses or (
            ToolRequestStatus.PENDING.value,
            ToolRequestStatus.REJECTED.value,
        )
        placeholders = ",".join("?" for _ in statuses)
        return [
            r["tool"]
            for r in self._conn.execute(
                "SELECT tool FROM tool_requests WHERE task_id=? AND role=?"
                f" AND status IN ({placeholders}) ORDER BY id",
                (task_id, role, *statuses),
            ).fetchall()
        ]

    def pending_blocking_tool_requests(
        self, task_id: int, parked_only: bool = False
    ) -> list[ToolRequest]:
        """Undecided requests the agent called load-bearing — what the loop's park
        check reads, and (with `parked_only`) what the release check reads.

        `parked_only` is the difference between "this task needs a decision" and
        "the loop is holding this task on these rows right now". The release needs
        the second: an escalation the park did not raise leaves every row at
        `parked=0`, so no tool decision can move a task the tool queue did not stop.
        """
        gate = " AND parked=1" if parked_only else ""
        rows = self._conn.execute(
            "SELECT * FROM tool_requests WHERE task_id=? AND status=?"
            f" AND blocking=1{gate} ORDER BY id",
            (task_id, ToolRequestStatus.PENDING.value),
        ).fetchall()
        return [self._row_to_tool_request(r) for r in rows]

    def tool_requests_mark_parked(self, task_id: int, request_ids: list[int]) -> None:
        """Stamp the rows a park is holding a task on.

        No event of its own, deliberately: this is called inside the park's
        transaction, whose event is the `status:needs_human` `set_status` already
        writes. A second event for the same fact would let anyone counting
        escalations count them twice. The row change and an event still share one
        commit, so the pairing rule holds.

        Scoped by `task_id` as well as by id so a mis-passed id from another task
        cannot silently park a stranger's row — and the `rowcount` is checked,
        `release_claim`'s discipline: an id set that stamped nothing used to let
        the park proceed anyway, after which the release found no parked row and
        approving the very request the task was held on left it at NEEDS_HUMAN.
        A mismatch is a caller bug, so it raises *inside* this transaction and the
        park it is nested in rolls back whole rather than half-applying.
        """
        ids = sorted(set(request_ids))
        if not ids:
            return
        with self.transaction():
            placeholders = ",".join("?" for _ in ids)
            cur = self._conn.execute(
                f"UPDATE tool_requests SET parked=1 WHERE task_id=? AND id IN"
                f" ({placeholders})",
                (task_id, *ids),
            )
            if cur.rowcount != len(ids):
                raise ValueError(
                    f"Cannot park task {task_id} on requests {ids}: only "
                    f"{cur.rowcount} of {len(ids)} belong to it."
                )

    def tool_requests_clear_parked(self, task_id: int) -> None:
        """Forget that the loop was holding this task on any request.

        Called by *every* exit from the parked state, and the set is named in
        full: the firing release, `human_redo`, `resume`'s `PAUSED → PENDING`
        branch, and the three terminal exits `human_approve` (DONE),
        `human_reject` (FAILED) and `abort` (ABORTED). The rows themselves stay
        `pending` and `blocking` — the need is still unmet, so a task that becomes
        runnable again parks at the next round, which is the correct answer.

        **`Loop.pause` is the one caller deliberately absent, because a pause is
        not an exit from the park — it is a suspension of it.** `pause` accepts any
        non-terminal task, so a parked NEEDS_HUMAN task can become PAUSED with the
        flag standing, and that is the honest record: the loop stopped this task on
        this row and has not resumed past it (which is what the flag means — see
        below). Its only non-terminal continuation is `resume`, which *is* in the
        list, and its terminal continuations (`human_reject`, `abort`) are too, so
        no route out of PAUSED leaves the flag behind. Clearing it here instead
        would be strictly worse than redundant: `PAUSED` is reachable only through
        `pause`, so `resume`'s clear would become unreachable in practice and
        `test_a_resume_from_paused_clears_the_parked_flags` would pass by
        measuring `pause` — a test proving something other than its name, which is
        the shape this codebase has already been bitten by twice.

        The flag's meaning is therefore stated as "the loop stopped this task on
        this row and has not resumed past it", not "the task is at NEEDS_HUMAN
        right now". Both sentences agree on every status but PAUSED, and only the
        first is true there. The release predicate's own
        `status == NEEDS_HUMAN` term is what keeps a paused task un-releasable, so
        the two facts are checked separately rather than one standing in for the
        other.

        The runnable exits must clear it or a later, unrelated escalation becomes
        revertible by approving the stale request. The terminal three clear it
        because the flag *means* "the loop is holding this task at NEEDS_HUMAN
        right now, on this row", and on a done/failed/aborted task that sentence
        is false — correct by definition, not defensively. Leaving it to the
        release predicate's `status == NEEDS_HUMAN` test would make a live-state
        column disagree with the live state, on the strength of a check in
        another module.

        Like the mark, it logs nothing: its callers write a status event in the
        same transaction.
        """
        self._conn.write(
            "UPDATE tool_requests SET parked=0 WHERE task_id=? AND parked=1",
            (task_id,),
        )

    @staticmethod
    def _row_to_tool_request(row: sqlite3.Row) -> ToolRequest:
        return ToolRequest(
            id=row["id"],
            task_id=row["task_id"],
            role=row["role"],
            agent_kind=row["agent_kind"],
            tool=row["tool"],
            status=ToolRequestStatus(row["status"]),
            source=ToolRequestSource(row["source"]),
            reason=row["reason"],
            blocking=bool(row["blocking"]),
            parked=bool(row["parked"]),
            attempt_id=row["attempt_id"],
            decided_by=row["decided_by"],
            decided_note=row["decided_note"],
            created_at=float(row["created_at"]),
            decided_at=None if row["decided_at"] is None else float(row["decided_at"]),
        )

    # -- validator eval harness ----------------------------------------------

    def add_eval_run(
        self,
        runner: str,
        n_fixtures: int,
        agreement: float,
        summary: dict,
        detail: list,
        kind: str = "verdict",
    ) -> int:
        """Persist one harness run. `kind` says what `agreement` counts: a
        validator verdict kind ('verdict') or a whole task's final status
        ('batch'). Defaulted and trailing, so the per-verdict caller is
        unchanged."""
        with self.transaction():
            cur = self._conn.execute(
                "INSERT INTO eval_runs (runner, n_fixtures, agreement, summary,"
                " detail, kind, created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    runner,
                    n_fixtures,
                    agreement,
                    json.dumps(summary),
                    json.dumps(detail),
                    kind,
                    time.time(),
                ),
            )
            self.log_event(
                None,
                "eval_run",
                {
                    "runner": runner,
                    "kind": kind,
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
                # Same population as the headline totals above, which filter on
                # `finished_at IS NOT NULL`. An attempt row exists from
                # `start_attempt` and is completed only by `finish_attempt`, so
                # every in-flight round — and every attempt whose
                # `finish_attempt` was rolled back — was counted here and not
                # there. `sum(by_model.attempts) > attempts` on any dashboard
                # opened during a live run, with no explanation available to the
                # reader, and costs and tokens agreed (the unfinished rows are
                # 0) so only the count diverged — which reads as a rounding
                # artefact rather than as two aggregates measuring two different
                # things.
                " FROM attempts WHERE finished_at IS NOT NULL"
                " GROUP BY model ORDER BY cost_usd DESC"
            ).fetchall()
        ]
        revisions = self._conn.execute(
            "SELECT COALESCE(SUM(revision_count),0) AS r FROM tasks"
        ).fetchone()
        # How many tool requests are waiting on a human, run-wide: the queue is
        # only useful if it is visible without opening a task.
        waiting = self._conn.execute(
            "SELECT COUNT(*) AS n FROM tool_requests WHERE status=?",
            (ToolRequestStatus.PENDING.value,),
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
            "pending_tool_requests": int(waiting["n"]),
            "tasks_by_status": by_status,
            "by_model": by_model,
        }
