"""Phase 1 of the multi-project dashboard slice: the `projects` table,
`tasks.project_id`, the default-project bootstrap + backfill, and the project
CRUD accessors.

Every task belongs to a project from the moment it exists — `resolve_project`
never raises on `None`, because the bootstrap guarantees a Default project
exists before any caller can ask.
"""

import threading

import pytest

from agentloop.models import Task, TaskStatus
from agentloop.store import Store


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "projects.db")
    yield s
    s.close()


def a_task(title="T", project_id=None) -> Task:
    return Task(
        id=None,
        title=title,
        goal="g",
        acceptance_criteria="c",
        status=TaskStatus.PENDING,
        project_id=project_id,
    )


# -- bootstrap -----------------------------------------------------------


def test_fresh_db_resolves_none_to_a_default_project(store):
    pid = store.resolve_project(None)
    proj = store.get_project(pid)
    assert proj is not None
    assert proj["name"] == "Default"
    assert proj["is_default"] == 1


def test_default_project_id_matches_resolve_project_none(store):
    assert store.default_project_id() == store.resolve_project(None)


def test_add_task_with_no_project_id_gets_the_default(store):
    tid = store.add_task(a_task())
    task = store.get_task(tid)
    assert task.project_id == store.resolve_project(None)


def test_task_defined_event_carries_project_id(store):
    tid = store.add_task(a_task())
    events = [e for e in store.events(tid) if e["kind"] == "task_defined"]
    assert events[0]["payload"]["project_id"] == store.resolve_project(None)


# -- CRUD ------------------------------------------------------------------


def test_create_project_and_get_by_name(store, tmp_path):
    pid = store.create_project("Widgets", str(tmp_path))
    proj = store.get_project(pid)
    assert proj["name"] == "Widgets"
    assert proj["repo_root"] == str(tmp_path)
    assert proj["workspace_mode"] == "scratch"
    assert proj["archived"] == 0

    by_name = store.get_project_by_name("Widgets")
    assert by_name["id"] == pid


def test_create_project_rejects_duplicate_name(store, tmp_path):
    store.create_project("Widgets", str(tmp_path))
    with pytest.raises(ValueError):
        store.create_project("Widgets", str(tmp_path))


def test_create_project_rejects_bad_workspace_mode(store, tmp_path):
    with pytest.raises(ValueError):
        store.create_project("Bad", str(tmp_path), workspace_mode="nonsense")


def test_create_project_rejects_relative_repo_root(store):
    with pytest.raises(ValueError):
        store.create_project("Rel", "relative/path")


def test_create_project_rejects_missing_dir(store, tmp_path):
    with pytest.raises(ValueError):
        store.create_project("Missing", str(tmp_path / "nope"))


def test_resolve_project_by_int_and_name_and_unknown(store, tmp_path):
    pid = store.create_project("Widgets", str(tmp_path))
    assert store.resolve_project(pid) == pid
    assert store.resolve_project("Widgets") == pid
    with pytest.raises(KeyError):
        store.resolve_project(999999)
    with pytest.raises(KeyError):
        store.resolve_project("Nonexistent")


def test_list_projects_excludes_archived_by_default(store, tmp_path):
    pid = store.create_project("Widgets", str(tmp_path))
    names = {p["name"] for p in store.list_projects()}
    assert "Widgets" in names
    assert "Default" in names

    # archive requires no non-terminal tasks; Widgets has none.
    store.archive_project(pid)
    names_default = {p["name"] for p in store.list_projects()}
    assert "Widgets" not in names_default
    names_all = {p["name"] for p in store.list_projects(include_archived=True)}
    assert "Widgets" in names_all


def test_rename_project(store, tmp_path):
    pid = store.create_project("Widgets", str(tmp_path))
    store.rename_project(pid, "Gadgets")
    assert store.get_project(pid)["name"] == "Gadgets"


def test_rename_project_rejects_collision(store, tmp_path):
    pid1 = store.create_project("Widgets", str(tmp_path))
    store.create_project("Gadgets", str(tmp_path))
    with pytest.raises(ValueError):
        store.rename_project(pid1, "Gadgets")


def test_repoint_project(store, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    pid = store.create_project("Widgets", str(tmp_path))
    store.repoint_project(pid, str(other), "worktree")
    proj = store.get_project(pid)
    assert proj["repo_root"] == str(other)
    assert proj["workspace_mode"] == "worktree"


def test_set_default_project_enforces_exactly_one(store, tmp_path):
    pid = store.create_project("Widgets", str(tmp_path))
    default_id = store.default_project_id()
    assert default_id != pid

    store.set_default_project(pid)
    assert store.default_project_id() == pid
    old = store.get_project(default_id)
    assert old["is_default"] == 0


def test_archive_project_refuses_default(store):
    default_id = store.default_project_id()
    with pytest.raises(ValueError):
        store.archive_project(default_id)


def test_archive_project_refuses_with_active_tasks(store, tmp_path):
    pid = store.create_project("Widgets", str(tmp_path))
    store.add_task(a_task(project_id=pid))
    with pytest.raises(ValueError):
        store.archive_project(pid)


def test_archive_project_allows_only_terminal_tasks(store, tmp_path):
    pid = store.create_project("Widgets", str(tmp_path))
    tid = store.add_task(a_task(project_id=pid))
    task = store.get_task(tid)
    store.set_status(task, TaskStatus.DONE)
    store.archive_project(pid)  # should not raise
    assert store.get_project(pid)["archived"] == 1


def test_archive_project_holds_the_lock_across_its_whole_check_and_write(
    store, tmp_path
):
    """[RED-FIRST] The non-terminal-task check and the archive write must
    run under ONE lock hold (one `with self.transaction():`), not a plain
    SELECT followed by a separate transaction -- the latter releases the
    lock between them, leaving a real gap under `ThreadingHTTPServer`
    concurrency where a task created in that gap is invisible to the
    check (a project could archive while holding a task the refusal
    exists to catch).

    Deterministic proof, not a timing-dependent race: pause execution
    (via a hook on the connection) at the exact instant AFTER the
    non-terminal-task SELECT runs but BEFORE the archive UPDATE, from
    inside `archive_project`'s own thread. A genuinely separate thread
    then attempts `add_task` on the same project. If the check and the
    write share one lock hold (the fix), that second thread's `add_task`
    call BLOCKS until `archive_project` finishes -- provably, by recording
    which of the two events happens first. Against the pre-fix code (two
    separate lock acquisitions), the second thread's `add_task` would
    complete WHILE `archive_project` is paused, proving the gap is real."""
    pid = store.create_project("Race", str(tmp_path))
    paused = threading.Event()
    resume = threading.Event()
    order: list[str] = []
    real_execute = store._conn.execute

    def hooked_execute(sql, params=()):
        if "SELECT COUNT(*) AS n FROM tasks" in sql:
            result = real_execute(sql, params)
            paused.set()
            resume.wait(timeout=10)
            return result
        return real_execute(sql, params)

    store._conn.execute = hooked_execute
    try:

        def archiver():
            try:
                store.archive_project(pid)
            finally:
                order.append("archive_project done")

        def adder():
            # A genuinely separate thread's add_task, issued while
            # archive_project is paused mid-check-and-write. Must run on
            # its own thread: calling this from the thread that later
            # signals `resume` would deadlock (it would block here forever,
            # since the lock is held by the paused archiver, and never
            # reach the line that lets the archiver continue).
            store.add_task(a_task(project_id=pid))
            order.append("add_task done")

        t_archive = threading.Thread(target=archiver)
        t_archive.start()
        assert paused.wait(timeout=10), "archive_project never reached the pause point"

        t_add = threading.Thread(target=adder)
        t_add.start()
        # `adder` is now blocked acquiring the same lock `archiver` holds
        # (it cannot even enter its own `with self.transaction():` yet).
        # Give it a moment to genuinely reach that blocked state before
        # releasing the archiver -- not a proof by itself, but a real
        # attempt was made and the join below is what actually verifies
        # the ordering.
        t_add.join(timeout=0.2)
        assert t_add.is_alive(), (
            "add_task returned before archive_project resumed -- the lock "
            "did not serialize them, which is exactly the bug this test "
            "exists to catch"
        )

        resume.set()
        t_archive.join(timeout=10)
        t_add.join(timeout=10)
    finally:
        store._conn.execute = real_execute

    # The fix's actual guarantee: add_task could only complete AFTER
    # archive_project's own transaction released the lock -- i.e. after
    # archive_project itself finished (it must have refused, since a task
    # existed the instant it committed... but since add_task ran while
    # archive_project was PAUSED mid-transaction, and the two share one
    # lock, add_task's own `with self.transaction():` cannot have started
    # until archive_project's finished. That serialization -- not which
    # one "won" -- is what the fix guarantees).
    assert order == ["archive_project done", "add_task done"], order


# -- scoped queries ---------------------------------------------------------


def test_list_tasks_filters_by_project(store, tmp_path):
    pid = store.create_project("Widgets", str(tmp_path))
    store.add_task(a_task("in default"))
    store.add_task(a_task("in widgets", project_id=pid))

    default_tasks = store.list_tasks(store.default_project_id())
    widgets_tasks = store.list_tasks(pid)
    assert {t.title for t in default_tasks} == {"in default"}
    assert {t.title for t in widgets_tasks} == {"in widgets"}

    unfiltered = store.list_tasks()
    assert {t.title for t in unfiltered} == {"in default", "in widgets"}


def test_claim_next_task_filters_by_project(store, tmp_path):
    pid = store.create_project("Widgets", str(tmp_path))
    store.add_task(a_task("in default"))
    store.add_task(a_task("in widgets", project_id=pid))

    claimed = store.claim_next_task("w1", project_id=pid)
    assert claimed.title == "in widgets"

    claimed2 = store.claim_next_task("w2", project_id=pid)
    assert claimed2 is None


# -- cross-project dependency refusal ---------------------------------------


def test_add_dependency_refuses_cross_project_edge(store, tmp_path):
    pid = store.create_project("Widgets", str(tmp_path))
    t1 = store.add_task(a_task("default task"))
    t2 = store.add_task(a_task("widgets task", project_id=pid))
    with pytest.raises(ValueError):
        store.add_dependency(t1, t2)


def test_add_dependency_allows_same_project_edge(store, tmp_path):
    pid = store.create_project("Widgets", str(tmp_path))
    t1 = store.add_task(a_task("a", project_id=pid))
    t2 = store.add_task(a_task("b", project_id=pid))
    store.add_dependency(t1, t2)  # should not raise
    assert store.dependencies(t1) == [t2]


# -- remediation: set_default_project / _ensure_default_project robustness --


def test_set_default_project_rejects_unknown_id_and_leaves_invariant_intact(store):
    default_id = store.default_project_id()
    with pytest.raises(KeyError):
        store.set_default_project(999999)
    # Invariant untouched: still exactly one is_default=1 row, the original.
    rows = [
        p for p in store.list_projects(include_archived=True) if p["is_default"] == 1
    ]
    assert len(rows) == 1
    assert rows[0]["id"] == default_id


def test_ensure_default_project_repairs_when_no_row_is_default(store):
    default_id = store.default_project_id()
    # Corrupt the invariant directly, bypassing the public API (simulating
    # what a bug in set_default_project could have left behind).
    store._conn.execute("UPDATE projects SET is_default=0")
    store._conn.commit()

    repaired_id = store._ensure_default_project()

    assert repaired_id == default_id
    rows = [
        p for p in store.list_projects(include_archived=True) if p["is_default"] == 1
    ]
    assert len(rows) == 1
    assert rows[0]["id"] == default_id
    # No duplicate 'Default'-named row was inserted.
    named_default = [
        p for p in store.list_projects(include_archived=True) if p["name"] == "Default"
    ]
    assert len(named_default) == 1
    events = [e for e in store.events(None) if e["kind"] == "project_default_repaired"]
    assert events
    assert events[-1]["payload"]["project_id"] == default_id


def test_ensure_default_project_repairs_when_extra_project_exists(store, tmp_path):
    widgets_id = store.create_project("Widgets", str(tmp_path))
    default_id = store.default_project_id()
    assert default_id != widgets_id

    store._conn.execute("UPDATE projects SET is_default=0")
    store._conn.commit()

    repaired_id = store._ensure_default_project()

    # Deterministic repair: promote the lowest-id project.
    lowest_id = min(default_id, widgets_id)
    assert repaired_id == lowest_id
    rows = [
        p for p in store.list_projects(include_archived=True) if p["is_default"] == 1
    ]
    assert len(rows) == 1
    assert rows[0]["id"] == lowest_id
    # No new 'Default' row was inserted despite one already being named that.
    named_default = [
        p for p in store.list_projects(include_archived=True) if p["name"] == "Default"
    ]
    assert len(named_default) == 1


def test_two_racing_first_opens_bootstrap_exactly_one_default(tmp_path):
    """Two `Store` instances (the two-process scenario `agentloop run` +
    `agentloop serve` against a brand-new db) racing the bootstrap insert in
    `_ensure_default_project` must not crash and must agree on one default
    project. Schema is pre-created and the `projects` table emptied first so
    the race under test is purely the bootstrap insert's own TOCTOU window —
    not unrelated sqlite/WAL contention from two connections racing
    `CREATE TABLE IF NOT EXISTS`/`PRAGMA journal_mode=WAL` on a literally
    nonexistent file, which is not part of this remediation."""
    db_path = tmp_path / "race.db"
    s1 = Store(db_path)
    s2 = Store(db_path)
    s1._conn.execute("DELETE FROM projects")
    s1._conn.commit()

    results: list[int] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def race(store: Store) -> None:
        try:
            barrier.wait(timeout=5)
            results.append(store._ensure_default_project())
        except BaseException as exc:  # noqa: BLE001 - capture for the assertion
            errors.append(exc)

    threads = [threading.Thread(target=race, args=(s,)) for s in (s1, s2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"racing bootstrap raised: {errors!r}"
    assert len(results) == 2
    assert len(set(results)) == 1

    named_default = [
        p for p in s1.list_projects(include_archived=True) if p["name"] == "Default"
    ]
    assert len(named_default) == 1
    s1.close()
    s2.close()
