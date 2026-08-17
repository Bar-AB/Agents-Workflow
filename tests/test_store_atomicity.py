"""0b/0c: a state change and its audit event commit together, and task claims
are race-free. These are store-level guarantees the parallel-worker slice
depends on, tested directly against the Store.

Slice 5 adds one more decision to the same register: a human's approve/reject on
a tool request is a compare-and-swap for exactly the reasons the claim is."""

import threading
import time

import pytest

from agentloop.models import (
    Task,
    TaskStatus,
    ToolRequest,
    ToolRequestSource,
    ToolRequestStatus,
)
from agentloop.store import Store, TransactionAborted


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "atomic.db")
    yield s
    s.close()


def a_task(title="T", status=TaskStatus.PENDING) -> Task:
    return Task(id=None, title=title, goal="g", acceptance_criteria="c", status=status)


# -- 0b: change + event are one commit ---------------------------------------


def test_add_task_row_and_event_are_all_or_nothing(store, monkeypatch):
    """Acceptance (0b): a failure injected between the row write and the event
    write persists neither."""
    monkeypatch.setattr(store, "log_event", _boom)

    with pytest.raises(RuntimeError):
        store.add_task(a_task())

    assert store.list_tasks() == []  # the row rolled back
    assert store.events() == []  # and so did (the absence of) its event


def test_set_status_change_and_event_are_atomic(store, monkeypatch):
    task = a_task()
    store.add_task(task)
    events_before = len(store.events())

    monkeypatch.setattr(store, "log_event", _boom)
    with pytest.raises(RuntimeError):
        store.set_status(task, TaskStatus.DONE)

    # Neither the status change nor a partial event survived.
    assert store.get_task(task.id).status == TaskStatus.PENDING
    assert len(store.events()) == events_before


def test_successful_transaction_commits_both(store):
    task = a_task()
    store.add_task(task)  # normal path: both land
    assert store.get_task(task.id) is not None
    kinds = [e["kind"] for e in store.events()]
    assert "task_defined" in kinds


def _boom(*_a, **_k):
    raise RuntimeError("crash between the row and its event")


# -- 0c: atomic task claim ---------------------------------------------------


def test_claim_hands_one_pending_task_to_exactly_one_worker(store):
    """Acceptance (0c): two workers claim concurrently against one pending
    task; exactly one wins, the other gets nothing."""
    store.add_task(a_task("only"))

    results: dict[str, Task | None] = {}
    barrier = threading.Barrier(2)

    def claim(worker_id):
        barrier.wait()  # maximize the race window
        results[worker_id] = store.claim_next_task(worker_id)

    threads = [threading.Thread(target=claim, args=(w,)) for w in ("w1", "w2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [w for w, task in results.items() if task is not None]
    assert len(winners) == 1, "exactly one worker may claim the task"
    losers = [w for w, task in results.items() if task is None]
    assert len(losers) == 1
    claimed = results[winners[0]]
    assert claimed.status == TaskStatus.IN_PROGRESS
    assert claimed.claimed_by == winners[0]


def test_claim_returns_next_pending_when_more_exist(store):
    store.add_task(a_task("first"))
    store.add_task(a_task("second"))

    t1 = store.claim_next_task("w1")
    t2 = store.claim_next_task("w2")
    assert {t1.title, t2.title} == {"first", "second"}
    assert store.claim_next_task("w3") is None  # nothing left to claim


def test_claim_resumes_only_its_own_in_flight_task(store):
    """A worker resumes an in-flight task it owns; another worker does not
    steal it (the invariant parallel workers rely on)."""
    store.add_task(a_task("resumable"))
    mine = store.claim_next_task("w1")  # -> in_progress, claimed_by w1
    assert mine.status == TaskStatus.IN_PROGRESS

    # A different worker must not pick up w1's in-flight task.
    assert store.claim_next_task("w2") is None
    # The owner resumes it.
    again = store.claim_next_task("w1")
    assert again is not None and again.id == mine.id


def test_claim_logs_an_event(store):
    store.add_task(a_task("audited"))
    claimed = store.claim_next_task("w1")
    kinds = [e["kind"] for e in store.events(claimed.id)]
    assert "task_claimed" in kinds


# -- slice 5: one tool request, two humans ------------------------------------


def test_two_concurrent_decisions_leave_exactly_one_decision_standing(
    store, monkeypatch
):
    """A tool decision is a compare-and-swap, not a read-then-write.

    The first version read the row and rejected an already-decided request
    *outside* its transaction, and updated `WHERE id=?` with no status predicate
    and no `rowcount` check. Two humans clearing the queue at once therefore both
    "succeeded": the row held whichever verdict committed last, both
    `tool_request_decided` events landed with nothing saying which took effect,
    and the loser's *returned* `ToolRequest` claimed a status the row did not
    have — which the CLI and the REST layer render as truth. Reverse the
    interleave and it is a gate bypass: a rejected `shell` becomes a grant
    `granted_tools` hands to the next invocation, with the denial in the log.

    Reachable by design, not in theory: `server.py` is a `ThreadingHTTPServer`
    and a CLI process holds its own connection and its own lock, and sqlite3
    opens no write transaction for a `SELECT` — the same reasoning
    `claim_next_task` documents.
    """
    task = a_task("contended")
    store.add_task(task)
    request_id = store.tool_request_add(
        task.id,
        role="worker",
        agent_kind="worker",
        tool="shell",
        status=ToolRequestStatus.PENDING.value,
        source=ToolRequestSource.MARKER.value,
        blocking=True,
    )

    # Hold the first decider open immediately *after* it has read the row — the
    # exact gap the old code left unguarded. With the read inside the
    # transaction the connection lock is held here, so the peer cannot even
    # start; without it, the peer runs the whole decision in this window.
    original_execute = store._conn.execute
    has_read = threading.Event()
    may_finish = threading.Event()
    paused = []

    def pause_after_the_first_row_read(sql, params=()):
        result = original_execute(sql, params)
        if "FROM tool_requests WHERE id=?" in sql and not paused:
            paused.append(True)
            has_read.set()
            may_finish.wait(10)
        return result

    monkeypatch.setattr(store._conn, "execute", pause_after_the_first_row_read)

    outcomes: dict[str, object] = {}

    def decide(who: str, approved: bool):
        try:
            outcomes[who] = store.tool_request_decide(
                request_id, approved=approved, by=who
            )
        except Exception as exc:  # the loser's refusal is the assertion
            outcomes[who] = exc

    first = threading.Thread(target=decide, args=("alice", False))
    first.start()
    assert has_read.wait(10), "the first decision never reached its row read"
    second = threading.Thread(target=decide, args=("bob", True))
    second.start()
    # Give the peer the whole window the old code left open. Under the CAS it
    # cannot use a window of any length; without one, 0.2s is orders of
    # magnitude more than the two statements need.
    time.sleep(0.2)
    may_finish.set()
    first.join(10)
    second.join(10)

    decided = [e for e in store.events(task.id) if e["kind"] == "tool_request_decided"]
    assert len(decided) == 1, "two decisions on one request must not both be recorded"
    winners = [v for v in outcomes.values() if isinstance(v, ToolRequest)]
    losers = [v for v in outcomes.values() if isinstance(v, ValueError)]
    assert len(winners) == 1, "exactly one decision may take effect"
    assert len(losers) == 1, "the loser must be refused, not silently overwritten"

    row = store.tool_request_get(request_id)
    # What the accessor returned is what it persisted, and the surviving event
    # describes that same decision.
    assert winners[0].status is row.status
    assert winners[0].decided_by == row.decided_by
    assert decided[0]["payload"]["by"] == row.decided_by
    assert decided[0]["payload"]["approved"] is (
        row.status is ToolRequestStatus.APPROVED
    )


def test_two_connections_deciding_one_request_leave_exactly_one_decision(store):
    """The same guarantee where the lock cannot be what provides it.

    The two-thread test above pins the *read placement* — it fails against the
    pre-fix accessor — but it cannot contend the `WHERE id=? AND status=?`
    predicate or the `rowcount` guard: `_LockedConnection.transaction` holds one
    RLock for its whole block, so under the fix the peer thread blocks before its
    first statement and never reaches the UPDATE. Two `Store`s on one database
    hold two connections and two locks, which is the shape the accessor's
    docstring names as its motivation (a `ThreadingHTTPServer` beside a CLI
    process) and the only shape where the predicate itself is doing the work.

    sqlite serializes the writers, so the loser's UPDATE matches zero rows, the
    `rowcount != 1` branch raises, and the read-back return means it can never be
    handed a silent no-op that reads as success.
    """
    task = a_task("contended across connections")
    store.add_task(task)
    request_id = store.tool_request_add(
        task.id,
        role="worker",
        agent_kind="worker",
        tool="shell",
        status=ToolRequestStatus.PENDING.value,
        source=ToolRequestSource.MARKER.value,
        blocking=True,
    )

    peer = Store(store.db_path)
    try:
        # The control on the mechanism: nothing shared between them can serialize
        # these two calls, so whatever refuses the second one is the SQL.
        assert peer._conn is not store._conn
        assert peer._conn._lock is not store._conn._lock

        outcomes = []
        for connection, who, approved in (
            (store, "alice", False),
            (peer, "bob", True),
        ):
            try:
                outcomes.append(
                    connection.tool_request_decide(
                        request_id, approved=approved, by=who
                    )
                )
            except ValueError as exc:  # the loser's refusal is the assertion
                outcomes.append(exc)

        winners = [o for o in outcomes if isinstance(o, ToolRequest)]
        losers = [o for o in outcomes if isinstance(o, ValueError)]
        assert len(winners) == 1, "exactly one decision may take effect"
        assert len(losers) == 1, "the loser is refused, not silently overwritten"

        # Both connections agree on the surviving decision, and it is the one the
        # winner returned — a rejected `shell` cannot become a grant.
        row = store.tool_request_get(request_id)
        assert peer.tool_request_get(request_id).status is row.status
        assert row.status is ToolRequestStatus.REJECTED
        assert row.decided_by == "alice"
        assert winners[0].status is row.status

        decided = [
            e for e in store.events(task.id) if e["kind"] == "tool_request_decided"
        ]
        assert len(decided) == 1, "a refused decision must log nothing"
        assert decided[0]["payload"]["by"] == "alice"
        assert decided[0]["payload"]["approved"] is False
    finally:
        peer.close()


# -- nested transactions: depth bookkeeping ----------------------------------


def test_a_swallowed_inner_failure_does_not_wedge_later_writes(store):
    """`transaction()` used to zero the depth on error instead of decrementing
    it. An enclosing block that caught an inner transaction's error then
    decremented to -1, and every later `commit()` saw a non-zero depth and
    silently stopped committing — a write that returned normally and never
    landed. Unreachable while nothing nests; slices 5-6 add nesting."""
    with pytest.raises(TransactionAborted):
        with store.transaction():
            store.log_event(None, "outer", {})
            try:
                with store.transaction():
                    raise RuntimeError("inner")
            except RuntimeError:
                pass  # the enclosing block swallows it

    assert store._conn._txn_depth == 0

    # The connection still commits, rather than accumulating writes forever.
    store.add_task(a_task("after the abort"))
    assert [t.title for t in store.list_tasks()] == ["after the abort"]


def test_a_swallowed_inner_failure_rolls_the_whole_group_back(store):
    """All-or-nothing survives the swallow: the outer block's writes cannot
    commit around a rolled-back inner one, because the half that landed would
    look like a complete group nobody questioned."""
    with pytest.raises(TransactionAborted):
        with store.transaction():
            store.add_task(a_task("outer work"))
            try:
                with store.transaction():
                    raise RuntimeError("inner")
            except RuntimeError:
                pass

    assert store.list_tasks() == []


def test_nested_transactions_still_commit_once_on_success(store):
    with store.transaction():
        store.add_task(a_task("nested"))
        with store.transaction():
            store.log_event(None, "inner", {})
    assert [t.title for t in store.list_tasks()] == ["nested"]
    assert store._conn._txn_depth == 0
