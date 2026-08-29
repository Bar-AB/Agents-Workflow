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
