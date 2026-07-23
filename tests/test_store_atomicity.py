"""0b/0c: a state change and its audit event commit together, and task claims
are race-free. These are store-level guarantees the parallel-worker slice
depends on, tested directly against the Store."""

import threading

import pytest

from agentloop.models import Task, TaskStatus
from agentloop.store import Store


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
