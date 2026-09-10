"""Phase 2 of the multi-project dashboard slice: `memory.project_id` and the
`UNIQUE(project_id, tier, key)` rebuild.

The same (tier, key) text is a different fact in a different project. These
tests prove memory is genuinely isolated per project through the public
Store API — never a shared fact leaking across a project boundary.
"""

import pytest

from agentloop.store import Store


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "memory_projects.db")
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


def test_same_key_two_projects_are_independent_facts(store, two_projects):
    a, b = two_projects
    store.memory_write("project", "style", "tabs", approved=True, project_id=a)
    store.memory_write("project", "style", "spaces", approved=True, project_id=b)

    assert store.memory_read("project", "style", project_id=a) == "tabs"
    assert store.memory_read("project", "style", project_id=b) == "spaces"


def test_memory_write_defaults_to_the_default_project(store, two_projects):
    default_id = store.resolve_project(None)
    store.memory_write("project", "unscoped-fact", "value", approved=True)
    assert store.memory_read("project", "unscoped-fact", project_id=default_id) == (
        "value"
    )


def test_memory_list_with_no_project_id_includes_every_project(store, two_projects):
    a, b = two_projects
    store.memory_write("project", "k1", "va", approved=True, project_id=a)
    store.memory_write("project", "k1", "vb", approved=True, project_id=b)

    all_rows = store.memory_list(tier="project")
    keys_and_values = {
        (r["project_id"], r["value"]) for r in all_rows if r["key"] == "k1"
    }
    assert keys_and_values == {(a, "va"), (b, "vb")}

    scoped = store.memory_list(tier="project", project_id=a)
    assert [r["value"] for r in scoped if r["key"] == "k1"] == ["va"]


def test_memory_promote_never_crosses_projects(store, two_projects):
    a, b = two_projects
    store.memory_write("loop", "existing", "stale-b-value", approved=True, project_id=b)
    id_a = store._conn.execute(
        "INSERT INTO memory (project_id, tier, key, value, approved, created_at)"
        " VALUES (?, 'project', 'existing', 'fresh-a-value', 1, 0)",
        (a,),
    ).lastrowid

    # Promoting project A's fact must never collide with, or merge into,
    # project B's loop-tier row of the same key.
    store.memory_promote(id_a)

    loop_rows = store.memory_list(tier="loop")
    a_loop = [r for r in loop_rows if r["project_id"] == a and r["key"] == "existing"]
    b_loop = [r for r in loop_rows if r["project_id"] == b and r["key"] == "existing"]
    assert len(a_loop) == 1
    assert a_loop[0]["value"] == "fresh-a-value"
    assert len(b_loop) == 1
    assert b_loop[0]["value"] == "stale-b-value"


def test_memory_promote_merges_within_the_same_project_only(store, two_projects):
    a, b = two_projects
    store.memory_write("loop", "dup", "old-a-loop", approved=True, project_id=a)
    id_a_project = store._conn.execute(
        "INSERT INTO memory (project_id, tier, key, value, approved, created_at)"
        " VALUES (?, 'project', 'dup', 'new-a-project', 1, 0)",
        (a,),
    ).lastrowid

    store.memory_promote(id_a_project)

    a_loop = [
        r for r in store.memory_list(tier="loop", project_id=a) if r["key"] == "dup"
    ]
    assert len(a_loop) == 1
    # Live promotion origin: the promoted (project) value wins.
    assert a_loop[0]["value"] == "new-a-project"
    # Project B was never touched.
    assert store.memory_read("loop", "dup", project_id=b) is None


def test_reconcile_memory_hits_never_pairs_across_projects(store, two_projects):
    """`_reconcile_memory_hits`'s dupe-detection JOIN matches on `key`/`tier`
    only, with no `project_id` predicate. `UNIQUE(project_id, tier, key)`
    makes it entirely legal for two different projects to each hold a
    `('loop', <same key>)` row -- this proves the JOIN never merges project
    A's `project`-tier row into project B's `loop`-tier row of the same key
    text, or vice versa.

    Calls `_reconcile_memory_hits()` directly (rather than going through a
    legacy-db `Store()` open) to engineer the cross-project collision this
    method's only real caller (a pre-`memory_hits` migration) could never
    itself produce -- that call site predates multi-project support, so every
    row it ever sees belongs to the just-backfilled default project. The
    unenforced call-site invariant is exactly what this test must not rely on.

    Falsified by temporarily removing the `AND l.project_id = p.project_id`
    predicate and confirming this goes red.
    """
    a, b = two_projects
    # Project A: a project/loop pair of the same key -- a legitimate dupe
    # `_reconcile_memory_hits` should merge.
    store.memory_write(
        "project", "shared-key", "a-project-value", approved=True, project_id=a
    )
    store.memory_write(
        "loop", "shared-key", "a-loop-value", approved=True, project_id=a
    )
    # Project B: only a project-tier row of the SAME key text, no loop row of
    # its own. Before the fix, the JOIN could pair this project-B row with
    # project A's loop row purely on key/tier, merging across projects.
    store.memory_write(
        "project", "shared-key", "b-project-value", approved=True, project_id=b
    )

    store._reconcile_memory_hits()

    # Project A's pair merged onto its own loop row, project A's project value
    # winning (this key's rows are both unapproved-vs-approved -- both
    # approved here, so it stays approved on whichever the merge picks;
    # what matters is which *project* holds the surviving rows).
    a_rows = [r for r in store.memory_list(project_id=a) if r["key"] == "shared-key"]
    assert len(a_rows) == 1
    assert a_rows[0]["tier"] == "loop"

    # Project B's project-tier row must survive untouched -- never merged
    # into, or deleted by, project A's loop row.
    b_rows = [r for r in store.memory_list(project_id=b) if r["key"] == "shared-key"]
    assert len(b_rows) == 1
    assert b_rows[0]["tier"] == "project"
    assert b_rows[0]["value"] == "b-project-value"
