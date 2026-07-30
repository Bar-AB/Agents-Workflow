"""Promotion is a *transition*, and a hit is a distinct task.

Promotion used to write a second row in the `loop` tier and leave the `project`
row in place, still approved and still counting. Both rows were then injected
(one real fact evicted from a cap that was never full), `memory_promoted`
re-fired on every later read, and revoking the project fact left the loop copy
approved — slice 2's value-approval rule bypassed by memory's own promotion
path. And with no task identity, worker + validator + one revision promoted a
fact inside a single task.
"""

import pytest

from agentloop.memory import MemoryService
from agentloop.models import Task
from agentloop.retrieval import HashingBackend
from agentloop.store import Store


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "promote.db")
    yield s
    s.close()


@pytest.fixture()
def memory(store):
    return MemoryService(store, promote_threshold=3, backend=HashingBackend())


def a_task(store, title="T") -> int:
    task = Task(id=None, title=title, goal="g", acceptance_criteria="c")
    store.add_task(task)
    return task.id


def injected_keys(block: str) -> list[str]:
    """The keys of an injected memory block, in order. Lines look like
    `- (tier) [*] key: value`."""
    return [line.split(": ", 1)[0].split()[-1] for line in block.splitlines()]


def promote(store, memory, key, tier="project", n=3):
    """Read `key` from `n` distinct tasks — enough to cross the threshold."""
    for i in range(n):
        memory.read(tier, key, task_id=a_task(store, f"T{i}"))


# -- 1. one row, one event ----------------------------------------------------


def test_promotion_moves_the_row_instead_of_copying_it(store, memory):
    """Acceptance: the fact ends up in `loop` and nowhere else. A surviving
    project row is a duplicate that gets injected alongside its own promotion."""
    store.memory_write("project", "test_command", "pytest -q", approved=True)
    promote(store, memory, "test_command")

    rows = store.memory_list()
    assert [(r["tier"], r["key"]) for r in rows] == [("loop", "test_command")]


def test_promoting_twice_logs_one_event(store, memory):
    """Acceptance: the project row used to keep its tier and its hit_count, so
    every later read re-entered promotion above the threshold and logged another
    event — forever, growing with every injection."""
    store.memory_write("project", "k", "deploy rollout", approved=True)
    promote(store, memory, "k")
    # Keep injecting it, from fresh tasks, well past the threshold. This has to
    # go through the normal retrieval path rather than reading the `loop` tier
    # directly: `maybe_promote` has always returned False for a non-project
    # tier, so a loop read never re-enters promotion under *either*
    # implementation and would prove nothing. Under the old copy-promotion the
    # project row is still here, still ranked, and still above the threshold.
    for i in range(5):
        memory.facts_for_prompt(
            query="deploy rollout", task_id=a_task(store, f"later{i}")
        )

    promotions = [e for e in store.events() if e["kind"] == "memory_promoted"]
    assert len(promotions) == 1
    assert promotions[0]["payload"]["key"] == "k"


def test_n_approved_facts_yield_n_distinct_injected_keys(store, memory):
    """Acceptance: N approved facts yield min(N, cap) *distinct* keys. With a
    copy left behind, twenty facts injected twenty lines covering nineteen keys
    — one real fact evicted by another fact's duplicate."""
    for i in range(20):
        store.memory_write("project", f"fact_{i:02d}", f"value {i}", approved=True)
    promote(store, memory, "fact_00")

    block, _ = memory.facts_for_prompt(query="value")
    keys = injected_keys(block)
    assert len(keys) == 20
    assert len(set(keys)) == 20


def test_a_promoted_fact_keeps_its_id_approval_pin_and_hits(store, memory):
    """The row moves, so everything hanging off its identity moves with it —
    including the memory id already recorded in past `retrieval` events."""
    store.memory_write("project", "k", "deploy rollout", approved=True, pinned=True)
    before = store.memory_list()[0]
    promote(store, memory, "k")

    after = store.memory_list()[0]
    assert after["id"] == before["id"]
    assert after["tier"] == "loop"
    assert after["approved"] == 1
    assert after["pinned"] == 1
    assert after["hit_count"] == 3


# -- 2. revocation reaches the promoted fact ----------------------------------


def test_revoking_a_promoted_fact_stops_it_being_injected(store, memory):
    """Promotion used to carry approval into a second, independently approved
    row: revoking the project fact left the loop copy approved and injected."""
    store.memory_write("project", "k", "deploy rollout", approved=True)
    promote(store, memory, "k")

    store.memory_set_approved(store.memory_list()[0]["id"], False)
    assert memory.facts_for_prompt(query="deploy rollout")[0] == ""


def test_rewriting_a_promoted_fact_revokes_it(store, memory):
    """The value-approval rule applies to the promoted fact too — slice 5 lets
    agents write, and a rewrite must not inherit the gate."""
    store.memory_write("project", "k", "deploy rollout", approved=True)
    promote(store, memory, "k")

    store.memory_write("loop", "k", "something else entirely")
    assert store.memory_list()[0]["approved"] == 0
    assert memory.facts_for_prompt(query="deploy rollout")[0] == ""


# -- 1 (collision). an existing loop key is the survivor ----------------------


def test_promoting_onto_an_existing_loop_key_keeps_one_row(store, memory):
    """UNIQUE(tier, key) blocks the move when a loop row already holds the key —
    which is the state every database written by the old copy-promotion is in."""
    store.memory_write("loop", "k", "deploy rollout", approved=True)
    store.memory_write("project", "k", "deploy rollout", approved=True)
    promote(store, memory, "k")

    rows = store.memory_list()
    assert [(r["tier"], r["key"]) for r in rows] == [("loop", "k")]
    assert rows[0]["approved"] == 1  # same value: nothing to re-vet
    # The evidence follows the key, not the row: deleting the project row would
    # otherwise cascade its memory_hits away and leave the survivor's hit_count
    # describing only half the history of the fact it now represents.
    assert len(store.memory_hits(rows[0]["id"])) == 3
    assert rows[0]["hit_count"] == 3


def test_a_merge_keeps_approval_when_the_loop_row_was_unvetted(store, memory):
    """The surviving row holds the project fact's *value*, so it must hold the
    project fact's approval too. Deferring entirely to `memory_write` kept the
    loop row's approval instead, so an approved fact promoted onto an unapproved
    row of the same value was deleted and the vetted content vanished."""
    store.memory_write("loop", "k", "deploy rollout")  # unapproved
    store.memory_write("project", "k", "deploy rollout", approved=True)
    promote(store, memory, "k")

    rows = store.memory_list()
    assert len(rows) == 1
    assert rows[0]["approved"] == 1
    assert "deploy rollout" in memory.facts_for_prompt(query="deploy rollout")[0]


def test_a_conflicting_promotion_falls_back_to_unapproved(store, memory):
    """Two different approved values for one key is a conflict a human resolves.
    The promoted value wins the row, but not the approval: it drops to unvetted
    so the merge cannot quietly install content nobody compared."""
    store.memory_write("loop", "k", "the old answer", approved=True)
    store.memory_write("project", "k", "deploy rollout", approved=True)
    promote(store, memory, "k")

    rows = store.memory_list()
    assert len(rows) == 1
    assert rows[0]["value"] == "deploy rollout"
    assert rows[0]["approved"] == 0
    assert rows[0]["hit_count"] == len(store.memory_hits(rows[0]["id"])) == 3


def test_a_merge_keeps_approval_when_only_the_project_row_was_vetted(store, memory):
    """Approval is approval *of a value*, and the surviving row holds the project
    fact's value — the one a human approved. Testing the loop row's prior
    approval instead dropped it here purely because an unrelated loop row
    happened to hold the key, and dropped it silently: the fact stopped being
    injected and nothing in the feed said so. Live from slice 5, where an agent
    writing `loop/<key>` with any junk value would un-approve a vetted
    `project/<key>` at its next promotion."""
    store.memory_write("loop", "k", "some unvetted answer")  # unapproved, differs
    store.memory_write("project", "k", "deploy rollout", approved=True)
    promote(store, memory, "k")

    rows = store.memory_list()
    assert len(rows) == 1
    assert rows[0]["value"] == "deploy rollout"
    assert rows[0]["approved"] == 1  # nothing a human vetted was displaced
    assert [e for e in store.events() if e["kind"] == "memory_revoked"] == []
    assert "deploy rollout" in memory.facts_for_prompt(query="deploy rollout")[0]


def test_a_merge_keeps_approval_when_only_the_loop_row_was_vetted(store):
    """Same rule from the other side: identical values, and the approval a human
    gave that text belongs to the surviving row whichever tier recorded it.

    Driven through `Store.memory_promote` rather than the `promote` helper
    because reads are approval-gated: an unapproved project row is never a
    retrieval candidate, so it collects no hits and the live path can never
    promote it. This is the store-level rule under a direct call — the shape the
    migration below also relies on."""
    store.memory_write("loop", "k", "deploy rollout", approved=True)
    store.memory_write("project", "k", "deploy rollout")  # unapproved
    project_id = [r["id"] for r in store.memory_list() if r["tier"] == "project"][0]
    store.memory_promote(project_id)

    rows = store.memory_list()
    assert len(rows) == 1
    assert rows[0]["approved"] == 1
    assert [e for e in store.events() if e["kind"] == "memory_revoked"] == []


def test_a_merge_that_displaces_a_vetted_value_revokes_and_says_so(store):
    """The one case where a merge really does take an approval away: an
    unapproved project value overwrites a vetted loop value. It drops to
    unapproved *and* logs, because a fact that stops being injected should show
    a revocation rather than a bare `memory_write` to infer it from.

    Direct call for the same reason as above. Note the migration takes the
    *opposite* branch on this input — see
    `test_upgrading_does_not_overwrite_a_vetted_value_with_an_unvetted_one`."""
    store.memory_write("loop", "k", "THE VETTED ANSWER", approved=True)
    store.memory_write("project", "k", "deploy rollout")  # unapproved, differs
    project_id = [r["id"] for r in store.memory_list() if r["tier"] == "project"][0]
    store.memory_promote(project_id)

    rows = store.memory_list()
    assert len(rows) == 1
    assert rows[0]["value"] == "deploy rollout"
    assert rows[0]["approved"] == 0
    revoked = [e for e in store.events() if e["kind"] == "memory_revoked"]
    assert revoked and revoked[0]["payload"]["key"] == "k"


def test_promotion_carries_the_pin_across_a_collision(store, memory):
    """Pinning is a statement about the key, so it survives the merge — the copy
    path dropped it, silently demoting a fact a human said must always appear."""
    store.memory_write("loop", "k", "old", approved=True)
    store.memory_write("project", "k", "deploy rollout", approved=True, pinned=True)
    promote(store, memory, "k")

    assert store.memory_list()[0]["pinned"] == 1


# -- 3. a hit is a distinct task ----------------------------------------------


def test_three_prompts_within_one_task_do_not_promote(store, memory):
    """Worker + validator + one revision is three ranked injections of the same
    fact inside a *single* task. Promotion claims 'relevant to three tasks'."""
    store.memory_write("project", "k", "deploy rollout", approved=True)
    task_id = a_task(store)
    for _ in range(3):
        memory.facts_for_prompt(query="deploy rollout", task_id=task_id)

    row = store.memory_list()[0]
    assert row["tier"] == "project"
    assert row["hit_count"] == 1


def test_three_distinct_tasks_promote(store, memory):
    store.memory_write("project", "k", "deploy rollout", approved=True)
    for i in range(3):
        memory.facts_for_prompt(query="deploy rollout", task_id=a_task(store, f"T{i}"))

    assert store.memory_list()[0]["tier"] == "loop"


def test_the_ranked_path_drives_a_full_transition(store, memory):
    """The transition and collision tests above reach promotion through the
    `promote` helper, i.e. `MemoryService.read(task_id=…)` — a route no
    production caller takes, since the loop only ever promotes as a side effect
    of a *ranked* injection. They would all still pass if the ranked path
    stopped recording hits entirely, so at least one case drives the whole
    transition the way the loop actually does."""
    store.memory_write("project", "k", "deploy rollout", approved=True, pinned=True)
    mem_id = store.memory_list()[0]["id"]
    for i in range(3):
        memory.facts_for_prompt(query="deploy rollout", task_id=a_task(store, f"T{i}"))

    rows = store.memory_list()
    assert [(r["tier"], r["key"]) for r in rows] == [("loop", "k")]
    assert rows[0]["id"] == mem_id  # moved, not copied
    assert rows[0]["approved"] == 1 and rows[0]["pinned"] == 1
    assert rows[0]["hit_count"] == len(store.memory_hits(mem_id)) == 3
    assert len([e for e in store.events() if e["kind"] == "memory_promoted"]) == 1


def test_the_ranked_path_drives_a_collision_merge(store, memory):
    """The collision branch is reached from the same ranked injection, so it
    needs the same treatment."""
    store.memory_write("loop", "k", "deploy rollout")  # unapproved: not a candidate
    store.memory_write("project", "k", "deploy rollout", approved=True)
    for i in range(3):
        memory.facts_for_prompt(query="deploy rollout", task_id=a_task(store, f"T{i}"))

    rows = store.memory_list()
    assert [(r["tier"], r["key"]) for r in rows] == [("loop", "k")]
    assert rows[0]["approved"] == 1
    assert rows[0]["hit_count"] == len(store.memory_hits(rows[0]["id"])) == 3


def test_a_read_with_no_task_records_no_hit(store, memory):
    """`agentloop memory`, eval, and direct reads are not tasks. Promotion is a
    claim about tasks, so an anonymous read must not count toward it."""
    store.memory_write("project", "k", "deploy rollout", approved=True)
    for _ in range(5):
        memory.facts_for_prompt(query="deploy rollout")

    row = store.memory_list()[0]
    assert row["hit_count"] == 0
    assert row["tier"] == "project"


def test_hits_are_recorded_per_task(store, memory):
    """The table is the evidence behind the counter: which tasks a fact served
    is what promotion claims, and it is now answerable rather than asserted."""
    store.memory_write("project", "k", "deploy rollout", approved=True)
    first, second = a_task(store, "A"), a_task(store, "B")
    for task_id in (first, first, second):
        memory.facts_for_prompt(query="deploy rollout", task_id=task_id)

    mem_id = store.memory_list()[0]["id"]
    assert sorted(store.memory_hits(mem_id)) == sorted([first, second])


def test_hits_survive_promotion(store, memory):
    store.memory_write("project", "k", "deploy rollout", approved=True)
    promote(store, memory, "k")
    mem_id = store.memory_list()[0]["id"]
    assert len(store.memory_hits(mem_id)) == 3


# -- 5a. the read is one transaction, gated on approval -----------------------


def test_a_read_credits_no_hit_to_a_value_that_lost_approval(store):
    """The SELECT and the bump used to take the lock twice, so a rewrite landing
    in the gap had its now-unapproved value credited with the hit."""
    store.memory_write("project", "k", "vetted", approved=True)
    task_id = a_task(store)

    original = store._conn.execute
    fired = []

    def rewrite_in_the_gap(sql, params=()):
        # Stand in for the rewrite a peer lands between the read's SELECT and
        # the bump it justifies. Only the first such SELECT is intercepted.
        looked_up = "SELECT" in sql.upper() and "FROM MEMORY " in f"{sql.upper()} "
        row = original(sql, params)
        if looked_up and not fired:
            fired.append(True)
            store.memory_write("project", "k", "something else entirely")
        return row

    store._conn.execute = rewrite_in_the_gap
    try:
        store.memory_read("project", "k", task_id=task_id)
    finally:
        store._conn.execute = original

    assert fired, "the interleaving under test never happened"
    row = store.memory_list()[0]
    assert row["approved"] == 0
    assert row["hit_count"] == 0
    # And no evidence row either. `INSERT OR IGNORE` means a hit recorded here
    # could never be made up: if the value were re-approved and the same task
    # read it again the insert would be ignored, so the counter and the table
    # would disagree permanently.
    assert store.memory_hits(row["id"]) == []


# -- upgrading a database written before hits were counted per task -----------


def legacy_db(tmp_path, rows):
    """A database as the pre-`memory_hits` build left it: memory rows carrying
    prompt-counted `hit_count` values, and no `memory_hits` table."""
    path = tmp_path / "legacy.db"
    store = Store(path)
    for tier, key, value, hit_count, approved in rows:
        store.memory_write(tier, key, value, approved=approved)
        store._conn.write(
            "UPDATE memory SET hit_count=? WHERE tier=? AND key=?",
            (hit_count, tier, key),
        )
    store._conn.write("DROP TABLE memory_hits")
    store.close()
    return path


def test_upgrading_resets_prompt_counted_hits(tmp_path):
    """The old counter counted prompts, so every fact already at the threshold
    would promote on its first relevant read after the upgrade — the exact
    failure this build removes, preserved for every existing database. There is
    nothing to backfill from, so the counters restart against the table that now
    defines them."""
    path = legacy_db(
        tmp_path,
        [("project", f"fact_{i}", "deploy rollout", 7, True) for i in range(4)],
    )

    store = Store(path)
    try:
        memory = MemoryService(store, promote_threshold=3, backend=HashingBackend())
        memory.facts_for_prompt(query="deploy rollout", task_id=a_task(store))

        assert [r["tier"] for r in store.memory_list()] == ["project"] * 4
        assert [e for e in store.events() if e["kind"] == "memory_promoted"] == []
        reset = [e for e in store.events() if e["kind"] == "memory_hit_counts_reset"]
        assert reset and reset[0]["payload"]["rows"] == 4
    finally:
        store.close()


def test_upgrading_leaves_hit_count_equal_to_its_evidence(tmp_path):
    """`hit_count` is the size of a fact's `memory_hits` set. A legacy row read
    8 against an empty set, which is the invariant the rest of the code trusts."""
    path = legacy_db(tmp_path, [("project", "k", "deploy rollout", 7, True)])

    store = Store(path)
    try:
        row = store.memory_list()[0]
        assert row["hit_count"] == len(store.memory_hits(row["id"])) == 0
    finally:
        store.close()


def test_upgrading_merges_duplicate_keys_left_by_copy_promotion(tmp_path):
    """The old promotion copied, so a database can hold both rows for one key:
    both injected, one real fact evicted from a cap that is not full, and
    revoking one leaving the other approved. Merged on upgrade rather than left
    to clear themselves the next time each fact happens to get hot."""
    path = legacy_db(
        tmp_path,
        [
            ("project", "k", "deploy rollout", 5, True),
            ("loop", "k", "deploy rollout", 0, True),
            ("project", "other", "unrelated", 0, True),
        ],
    )

    store = Store(path)
    try:
        rows = [(r["tier"], r["key"]) for r in store.memory_list()]
        assert sorted(rows) == [("loop", "k"), ("project", "other")]
        assert store.memory_list(tier="loop")[0]["approved"] == 1
    finally:
        store.close()


def test_upgrading_does_not_overwrite_a_vetted_value_with_an_unvetted_one(tmp_path):
    """The state the old copy-promotion routinely left: a vetted `loop` copy
    beside a `project` row that was rewritten afterwards — the approval-of-value
    rule un-approved it at that point. "The promoted value wins" is right on the
    live path, where the project fact just proved itself over three tasks, but on
    a one-shot repair it would replace human-approved content with the unvetted
    rewrite on nothing more than opening the database, leaving the vetted text
    recoverable only from a truncated event payload."""
    path = legacy_db(
        tmp_path,
        [
            ("loop", "test_command", "pytest -q", 0, True),
            ("project", "test_command", "rm -rf / # totally fine", 5, False),
        ],
    )

    store = Store(path)
    try:
        rows = store.memory_list()
        assert len(rows) == 1
        assert rows[0]["value"] == "pytest -q"
        assert rows[0]["approved"] == 1
        assert store.memory_read("loop", "test_command") == "pytest -q"
        # The unvetted rewrite is the loser here, so it is what stays recoverable.
        merged = [e for e in store.events() if e["kind"] == "memory_duplicates_merged"]
        assert merged[0]["payload"]["displaced_value"] == "rm -rf / # totally fine"
        assert merged[0]["payload"]["displaced_approved"] is False
        # Nothing was un-approved, so nothing claims to have been.
        assert [e for e in store.events() if e["kind"] == "memory_revoked"] == []
    finally:
        store.close()


def test_upgrading_does_not_log_promotions_for_rows_it_did_not_promote(tmp_path):
    """`memory_promoted` is the record of a promotion. These rows were promoted
    by the old build long before this database was opened, so borrowing the event
    would make anyone counting promotions in the feed count database opens."""
    path = legacy_db(
        tmp_path,
        [
            ("project", "k", "deploy rollout", 5, True),
            ("loop", "k", "deploy rollout", 0, True),
        ],
    )

    store = Store(path)
    try:
        assert [e for e in store.events() if e["kind"] == "memory_promoted"] == []
        merged = [e for e in store.events() if e["kind"] == "memory_duplicates_merged"]
        assert len(merged) == 1 and merged[0]["payload"]["key"] == "k"
    finally:
        store.close()


def test_a_merge_records_the_value_it_displaced(tmp_path, store, memory):
    """The promoted value wins, so the loser has to stay recoverable: no other
    event payload records a memory value, so without this the displaced text
    would be gone for good."""
    store.memory_write("loop", "k", "THE VETTED LOOP ANSWER", approved=True)
    store.memory_write("project", "k", "deploy rollout", approved=True)
    promote(store, memory, "k")

    promoted = [e for e in store.events() if e["kind"] == "memory_promoted"][0]
    assert promoted["payload"]["displaced_value"] == "THE VETTED LOOP ANSWER"
    assert promoted["payload"]["displaced_approved"] is True


def test_a_merge_that_drops_approval_says_so_in_the_feed(store, memory):
    """A vetted fact that stops being injected should show a revocation, not
    leave a bare `memory_write` for a dashboard reader to infer it from."""
    store.memory_write("loop", "k", "the old answer", approved=True)
    store.memory_write("project", "k", "deploy rollout", approved=True)
    promote(store, memory, "k")

    revoked = [e for e in store.events() if e["kind"] == "memory_revoked"]
    assert revoked and revoked[0]["payload"]["key"] == "k"


def test_a_merge_that_keeps_approval_logs_no_revocation(store, memory):
    store.memory_write("loop", "k", "deploy rollout", approved=True)
    store.memory_write("project", "k", "deploy rollout", approved=True)
    promote(store, memory, "k")

    assert [e for e in store.events() if e["kind"] == "memory_revoked"] == []
