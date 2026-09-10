"""Charter project scoping.

The charter table gained a `project_id` column so a multi-project database
never leaks one project's house rules into another project's agent prompts —
mirrors `test_memory_projects.py` for memory and adds the migration case
memory already covers for its own `project_id` column.
"""

import sqlite3
from pathlib import Path

import pytest

from agentloop.config import LoopConfig
from agentloop.loop import Loop
from agentloop.registry import Registry
from agentloop.runner import MockRunner
from agentloop.store import Store

APPROVE = "VERDICT: approve CONFIDENCE: 0.92 TESTS: pass\nMeets all criteria."


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "charter_projects.db")
    yield s
    s.close()


@pytest.fixture()
def two_projects(store, tmp_path):
    a_dir = tmp_path / "repo-a"
    b_dir = tmp_path / "repo-b"
    a_dir.mkdir()
    b_dir.mkdir()
    a = store.create_project("A", str(a_dir))
    b = store.create_project("B", str(b_dir))
    return a, b


def make_loop(store, outputs, **cfg_overrides):
    cfg_overrides.setdefault("workspace_root", str(Path(store.db_path).parent / "ws"))
    cfg_overrides.setdefault("allow_test_exec", False)
    cfg_overrides.setdefault("vcs_enabled", False)
    config = LoopConfig(db_path=store.db_path, **cfg_overrides)
    runner = MockRunner(outputs)
    return Loop(store, runner, Registry.load(), config), runner


def test_charter_is_independent_per_project(store, two_projects):
    a, b = two_projects
    store.charter_set("Rule for A", project_id=a)
    store.charter_set("Rule for B", project_id=b)

    _, body_a = store.charter_active(a)
    _, body_b = store.charter_active(b)
    assert body_a == "Rule for A"
    assert body_b == "Rule for B"


def test_charter_set_defaults_to_the_default_project(store, two_projects):
    default_id = store.resolve_project(None)
    store.charter_set("Unscoped rule")
    version, body = store.charter_active(default_id)
    assert body == "Unscoped rule"
    assert store.charter_version(version, default_id) is not None


def test_charter_version_is_not_visible_from_another_project(store, two_projects):
    a, b = two_projects
    version = store.charter_set("A's rule", project_id=a)

    # The version number is a global counter, not a per-project one -- it must
    # still refuse to resolve against the wrong project rather than silently
    # returning A's text to a caller operating in B.
    assert store.charter_version(version, a) is not None
    assert store.charter_version(version, b) is None


def test_charter_history_scoped_per_project(store, two_projects):
    a, b = two_projects
    store.charter_set("A v1", project_id=a)
    store.charter_set("A v2", project_id=a)
    store.charter_set("B v1", project_id=b)

    assert len(store.charter_history(a)) == 2
    assert len(store.charter_history(b)) == 1


def test_worker_in_one_project_never_sees_another_projects_charter(store, two_projects):
    """The real end-to-end proof: `_charter_block` is threaded from the
    task's own `project_id` through `run_worker`, so a task in project A
    must never carry project B's rules into its prompt, and vice versa."""
    a, b = two_projects
    store.charter_set("Only A agents must ever see this rule.", project_id=a)
    store.charter_set("Only B agents must ever see this rule.", project_id=b)

    from agentloop.models import Task

    ws = str(Path(store.db_path).parent / "ws")
    task_a = Task(
        id=None,
        title="Task in A",
        goal="Do the A thing.",
        acceptance_criteria="A criteria.",
        project_id=a,
    )
    task_b = Task(
        id=None,
        title="Task in B",
        goal="Do the B thing.",
        acceptance_criteria="B criteria.",
        project_id=b,
    )
    store.add_task(task_a)
    store.add_task(task_b)

    loop, runner = make_loop(
        store, ["out-a", APPROVE, "out-b", APPROVE], workspace_root=ws
    )
    loop.run_task(task_a)
    loop.run_task(task_b)

    worker_prompt_a = runner.calls[0]["prompt"]
    worker_prompt_b = runner.calls[2]["prompt"]
    assert "Only A agents must ever see this rule." in worker_prompt_a
    assert "Only B agents must ever see this rule." not in worker_prompt_a
    assert "Only B agents must ever see this rule." in worker_prompt_b
    assert "Only A agents must ever see this rule." not in worker_prompt_b


# -- migration: an existing charter table predating project_id --------------

PRE_PROJECT_SCOPED_CHARTER_SCHEMA = """
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
CREATE TABLE charter (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    body TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
INSERT INTO charter (body, note, created_at)
VALUES ('Legacy house rule.', 'set before projects existed', 0);
"""


def build_pre_project_scoped_charter_db(path):
    raw = sqlite3.connect(path)
    raw.executescript(PRE_PROJECT_SCOPED_CHARTER_SCHEMA)
    raw.commit()
    raw.close()
    return path


def test_a_charter_table_predating_project_id_migrates_and_backfills(tmp_path):
    """A real pre-this-change database: `charter` already exists, with a real
    row, and no `project_id` column at all. Must open without raising and
    backfill the existing row onto the Default project — the same promise
    `_ensure_default_project` already keeps for `tasks.project_id`."""
    db = build_pre_project_scoped_charter_db(tmp_path / "pre_charter_projects.db")

    raw = sqlite3.connect(db)
    cols = {r[1] for r in raw.execute("PRAGMA table_info(charter)")}
    raw.close()
    assert "project_id" not in cols

    migrated = Store(db)
    try:
        default_id = migrated.resolve_project(None)
        version, body = migrated.charter_active(default_id)
        assert body == "Legacy house rule."

        backfill_events = [
            e for e in migrated.events() if e["kind"] == "project_migration_backfill"
        ]
        assert len(backfill_events) == 1
        assert backfill_events[0]["payload"]["charter_rows_backfilled"] == 1
    finally:
        migrated.close()
