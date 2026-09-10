"""Opening a database written by an older agentloop (Property 7).

`CREATE TABLE IF NOT EXISTS` never applies a new *column* to a table that
already exists, so every slice that adds one adds it to `_migrate`'s
`additions` too (DD-11). Slice 6 adds columns from two independent parts, but
they are proved by one property — "a slice-5 database opens and works" — so
the old-schema fixture lives once, here, rather than split across the two
feature modules.
"""

import sqlite3

from agentloop.models import TestResult
from agentloop.store import Store

# The slice-5 shape of every table this slice touches: `test_runs` without
# `coverage_percent`, `eval_runs` without `kind`. Written by hand rather than
# taken from `_SCHEMA`, because a fixture that read today's schema would be
# migrated by construction and could never fail.
SLICE5_SCHEMA = """
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL, goal TEXT NOT NULL,
    acceptance_criteria TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    risk_level INTEGER NOT NULL DEFAULT 1,
    revision_count INTEGER NOT NULL DEFAULT 0,
    worker_role TEXT NOT NULL DEFAULT 'worker',
    validator_role TEXT NOT NULL DEFAULT 'validator',
    output TEXT NOT NULL DEFAULT '',
    escalation_reason TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE test_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    attempt_id INTEGER,
    status TEXT NOT NULL,
    exit_code INTEGER,
    summary TEXT NOT NULL DEFAULT '',
    stdout_tail TEXT NOT NULL DEFAULT '',
    duration_s REAL NOT NULL DEFAULT 0.0,
    created_at REAL NOT NULL);
CREATE TABLE eval_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    runner TEXT NOT NULL,
    n_fixtures INTEGER NOT NULL,
    agreement REAL NOT NULL,
    summary TEXT NOT NULL DEFAULT '{}',
    detail TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL);
INSERT INTO tasks (title, goal, acceptance_criteria, created_at, updated_at)
VALUES ('Legacy task', 'do it', 'works', 0, 0);
INSERT INTO test_runs (task_id, status, exit_code, summary, duration_s,
                       created_at)
VALUES (1, 'pass', 0, '3 passed', 0.4, 0);
INSERT INTO eval_runs (runner, n_fixtures, agreement, created_at)
VALUES ('mock', 4, 1.0, 0);
"""


def build_slice5_db(path):
    raw = sqlite3.connect(path)
    raw.executescript(SLICE5_SCHEMA)
    raw.commit()
    raw.close()
    return path


def test_a_slice5_database_gains_the_slice6_columns(tmp_path):
    """A pre-slice-6 store must open, gain the column, and read back sanely:
    no coverage was ever parsed for its old rows, and NULL — not 0.0 — is what
    "nothing was reported" means. The control is the fresh write afterwards:
    without it, a store that added the column but never wrote it would pass."""
    db = build_slice5_db(tmp_path / "slice5.db")

    # The fixture really is missing the column, or the test proves nothing.
    raw = sqlite3.connect(db)
    cols = {r[1] for r in raw.execute("PRAGMA table_info(test_runs)")}
    raw.close()
    assert "coverage_percent" not in cols

    migrated = Store(db)
    try:
        rows = migrated.test_runs(1)
        assert len(rows) == 1
        assert "coverage_percent" in rows[0]
        assert rows[0]["coverage_percent"] is None
        assert rows[0]["status"] == "pass"

        # ...and the migrated column is writable in the same database.
        migrated.add_test_run(
            1, None, TestResult(status="pass", exit_code=0, coverage_percent=87.0)
        )
        assert migrated.test_runs(1)[1]["coverage_percent"] == 87.0
    finally:
        migrated.close()


def test_a_pre_projects_database_bootstraps_a_default_and_backfills_tasks(tmp_path):
    """An old db (no `projects` table, `tasks` with no `project_id` column at
    all) must open, gain a Default project, and have every existing task's
    `project_id` backfilled to it — no data loss, and `resolve_project(None)`
    on the migrated store answers with that same Default project.

    Reuses the slice-5 fixture (SLICE5_SCHEMA), which already predates both
    the `projects` table and `tasks.project_id`, rather than adding a third
    schema shape — the property under test ("a pre-projects database migrates
    cleanly") does not care which older slice's schema it starts from.
    """
    db = build_slice5_db(tmp_path / "pre_projects.db")

    # The fixture really is missing both, or the test proves nothing.
    raw = sqlite3.connect(db)
    tables = {
        r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    task_cols = {r[1] for r in raw.execute("PRAGMA table_info(tasks)")}
    raw.close()
    assert "projects" not in tables
    assert "project_id" not in task_cols

    migrated = Store(db)
    try:
        projects = migrated.list_projects()
        assert len(projects) == 1
        assert projects[0]["name"] == "Default"
        assert projects[0]["is_default"] == 1

        default_id = migrated.resolve_project(None)
        assert default_id == projects[0]["id"]

        # The pre-existing 'Legacy task' row (id=1 from SLICE5_SCHEMA) survived
        # with no data loss and now carries the backfilled project_id.
        task = migrated.get_task(1)
        assert task is not None
        assert task.title == "Legacy task"
        assert task.project_id == default_id

        backfill_events = [
            e for e in migrated.events() if e["kind"] == "project_migration_backfill"
        ]
        assert len(backfill_events) == 1
        assert backfill_events[0]["payload"]["project_id"] == default_id
        assert backfill_events[0]["payload"]["tasks_backfilled"] == 1
    finally:
        migrated.close()


def test_resolve_project_none_on_a_brand_new_db_returns_the_default(tmp_path):
    """No pre-existing data at all: `resolve_project(None)` must still answer
    with a valid id whose row is named 'Default' — the bootstrap runs on
    every `Store.__init__`, not only when migrating an old database."""
    store = Store(tmp_path / "brand_new.db")
    try:
        pid = store.resolve_project(None)
        proj = store.get_project(pid)
        assert proj is not None
        assert proj["name"] == "Default"
    finally:
        store.close()


# A `memory` table shaped like it was just before `memory_hits` existed
# (tier/key/value/hit_count/approved/pinned/created_at/last_used_at, no
# `project_id`, `UNIQUE(tier, key)`) and NO `memory_hits` table at all — the
# exact trigger for `_reconcile_memory_hits()`. A real `project`/`loop` key
# collision on 'style' so `_reconcile_memory_hits` -> `_merge_into_loop` ->
# `memory_write` actually fires on the legacy path, not just an inert call.
PRE_MEMORY_HITS_MEMORY_SCHEMA = """
CREATE TABLE memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tier TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    hit_count INTEGER NOT NULL DEFAULT 0,
    approved INTEGER NOT NULL DEFAULT 0,
    pinned INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    last_used_at REAL,
    UNIQUE(tier, key)
);
INSERT INTO memory (tier, key, value, approved, created_at) VALUES
  ('project', 'style', 'use tabs', 1, 0),
  ('loop', 'style', 'use spaces', 0, 0);
"""


def build_pre_memory_hits_db(path):
    raw = sqlite3.connect(path)
    raw.executescript(SLICE5_SCHEMA)
    raw.executescript(PRE_MEMORY_HITS_MEMORY_SCHEMA)
    raw.commit()
    raw.close()
    return path


def test_legacy_pre_memory_hits_db_migrates_without_project_id_error(tmp_path):
    """Plan-vs-code gap #6: `_reconcile_memory_hits()` -> `_merge_into_loop()`
    -> `memory_write()`, and `memory_write` is project_id-aware. On a
    pre-memory_hits db, `memory.project_id` must exist BEFORE that legacy
    branch runs, or this raises `sqlite3.OperationalError: no such column:
    project_id`. Must open cleanly, and the merged row must carry the
    correct project_id."""
    db = build_pre_memory_hits_db(tmp_path / "pre_memory_hits.db")

    raw = sqlite3.connect(db)
    tables = {
        r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    raw.close()
    assert "memory" in tables
    assert "memory_hits" not in tables

    migrated = Store(db)  # must not raise
    try:
        default_id = migrated.resolve_project(None)
        rows = migrated.memory_list(project_id=default_id)
        style_rows = [r for r in rows if r["key"] == "style"]
        # Merged onto one loop row (migration origin: approved project value
        # wins per _merge_into_loop's docstring).
        assert len(style_rows) == 1
        assert style_rows[0]["tier"] == "loop"
        assert style_rows[0]["value"] == "use tabs"
        assert style_rows[0]["project_id"] == default_id
    finally:
        migrated.close()


def test_memory_rebuild_preserves_autoincrement(tmp_path):
    """A deleted row's id must never be reissued to a later fact. Falsified
    by temporarily reverting the rebuild DDL to `id INTEGER PRIMARY KEY` (no
    AUTOINCREMENT) and confirming this goes red — see the ADR in the plan."""
    db = build_pre_memory_hits_db(tmp_path / "autoincrement.db")
    store = Store(db)
    try:
        default_id = store.resolve_project(None)
        rows = store.memory_list(project_id=default_id)
        max_id_before = max(r["id"] for r in rows)
        # Delete whichever row currently holds the table's max id.
        victim = next(r for r in rows if r["id"] == max_id_before)
        store.memory_delete(victim["id"])
        # A brand-new fact, no explicit id.
        store.memory_write("project", "brand-new-fact", "hello", project_id=default_id)
        new_row = store.memory_list(project_id=default_id, tier="project")
        new_id = next(r["id"] for r in new_row if r["key"] == "brand-new-fact")
        assert new_id > max_id_before, (
            f"deleted id {max_id_before} was reissued to a new fact (got {new_id})"
        )
    finally:
        store.close()


def test_memory_rebuild_preserves_true_historical_high_water_mark(tmp_path):
    """A row deleted BEFORE the rebuild ever runs must still keep its id from
    being reissued — the sneakier trigger than `test_memory_rebuild_preserves_
    autoincrement` above, which only proves ordinary post-migration
    AUTOINCREMENT works (its delete happens after `Store()` already migrated).

    `DROP TABLE` on an AUTOINCREMENT table deletes its `sqlite_sequence` row —
    the one place the table's *true* historical high-water mark lived — so a
    naive id-preserving copy (`INSERT INTO memory_new (id, ...) SELECT id, ...
    FROM memory`) only carries forward the max id among rows that *survive* to
    migration time. If the row that held the table's true max id was deleted
    beforehand (an entirely ordinary event — `_merge_into_loop`'s
    collision-loser DELETE, or `memory_delete`), the rebuilt table's sequence
    starts from `max(surviving ids)`, not the true historical max, and the
    very first post-migration `memory_write` silently reissues the deleted
    row's old id to an unrelated new fact.

    Built by hand via raw sqlite3, entirely BEFORE `Store()` is ever
    constructed — the exact shape of the failure-hunter's live reproduction.
    Falsified by temporarily dropping the `old_seq`-carrying fix and
    confirming this goes red.
    """
    db = tmp_path / "pre_migration_delete.db"
    raw = sqlite3.connect(db)
    raw.executescript(SLICE5_SCHEMA)
    raw.executescript(
        """
        CREATE TABLE memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tier TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            hit_count INTEGER NOT NULL DEFAULT 0,
            approved INTEGER NOT NULL DEFAULT 0,
            pinned INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            last_used_at REAL,
            UNIQUE(tier, key)
        );
        INSERT INTO memory (tier, key, value, approved, created_at) VALUES
          ('project', 'fact-1', 'v1', 1, 0),
          ('project', 'fact-2', 'v2', 1, 0),
          ('project', 'fact-3', 'v3', 1, 0);
        """
    )
    # fact-3 holds the table's true max id (3). Delete it BEFORE Store() ever
    # opens this database, so the true high-water mark only ever lived in
    # sqlite_sequence, never in a surviving row.
    deleted_id = raw.execute("SELECT id FROM memory WHERE key='fact-3'").fetchone()[0]
    assert deleted_id == 3
    raw.execute("DELETE FROM memory WHERE id=?", (deleted_id,))
    raw.commit()
    raw.close()

    store = Store(db)
    try:
        default_id = store.resolve_project(None)
        store.memory_write("project", "brand-new-fact", "hello", project_id=default_id)
        new_row = store.memory_list(project_id=default_id, tier="project")
        new_id = next(r["id"] for r in new_row if r["key"] == "brand-new-fact")
        assert new_id > deleted_id, (
            f"pre-migration-deleted id {deleted_id} was reissued to a new fact"
            f" (got {new_id})"
        )
    finally:
        store.close()


def test_memory_hits_row_survives_the_rebuild_and_still_resolves(tmp_path):
    """Exit Criteria (plan Phase 2, line 593): 'A `memory_hits` row written
    before the rebuild still resolves to the correct fact after it.'

    Realistic shape of nearly every real pre-slice-10 database: `memory_hits`
    already exists (it predates this slice), so `_reconcile_memory_hits`'s
    legacy branch does NOT run (`"memory_hits" not in existing` is False) --
    but `_rebuild_memory_unique_constraint` still runs, because the old
    `memory` table's `UNIQUE(tier, key)` has not yet been widened. That
    rebuild does `DROP TABLE memory`, and `memory_hits.memory_id REFERENCES
    memory(id) ON DELETE CASCADE` -- so if the `PRAGMA foreign_keys=OFF`
    guarding that DROP were ever missing or misplaced, the DROP would cascade
    and silently wipe every `memory_hits` row.

    Falsified by temporarily moving the `PRAGMA foreign_keys=OFF` call to
    AFTER `_rebuild_memory_unique_constraint` runs (the code's own comment
    already warns the ordering is load-bearing) and confirming this test goes
    red.
    """
    db = tmp_path / "pre_rebuild_with_hits.db"
    raw = sqlite3.connect(db)
    raw.executescript(SLICE5_SCHEMA)
    raw.executescript(
        """
        CREATE TABLE memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tier TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            hit_count INTEGER NOT NULL DEFAULT 0,
            approved INTEGER NOT NULL DEFAULT 0,
            pinned INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            last_used_at REAL,
            UNIQUE(tier, key)
        );
        CREATE TABLE memory_hits (
            memory_id INTEGER NOT NULL REFERENCES memory(id) ON DELETE CASCADE,
            task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            ts REAL NOT NULL,
            UNIQUE(memory_id, task_id)
        );
        INSERT INTO memory (tier, key, value, approved, hit_count, created_at)
        VALUES ('project', 'reliable-fact', 'the value', 1, 1, 0);
        INSERT INTO memory_hits (memory_id, task_id, ts) VALUES (1, 1, 0);
        """
    )
    raw.commit()
    raw.close()

    # The fixture really predates the rebuild and already has memory_hits, or
    # this test proves nothing.
    check = sqlite3.connect(db)
    tables = {
        r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    sql_row = check.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='memory'"
    ).fetchone()
    check.close()
    assert "memory_hits" in tables
    assert "UNIQUE(project_id, tier, key)" not in sql_row[0]

    store = Store(db)
    try:
        default_id = store.resolve_project(None)
        rows = store.memory_list(project_id=default_id, tier="project")
        fact = next(r for r in rows if r["key"] == "reliable-fact")
        assert fact["value"] == "the value"
        assert fact["id"] == 1
        assert store.memory_hits(fact["id"]) == [1]
    finally:
        store.close()
