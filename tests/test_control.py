"""Mid-run human control: pause / resume / abort, read at each iteration
boundary and driven through the store so it works cross-process."""

from pathlib import Path

import pytest

from agentloop.loop import Loop
from agentloop.models import TaskStatus
from agentloop.registry import Registry
from agentloop.store import Store
from tests.test_loop import APPROVE, REVISE, add_task, make_loop


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


class ControllingRunner:
    """A MockRunner that sets a control signal partway through a run, simulating
    a human hitting pause/abort in another process mid-loop."""

    def __init__(self, outputs, store, task_id, control, at_call):
        self.outputs = list(outputs)
        self.store = store
        self.task_id = task_id
        self.control = control
        self.at_call = at_call
        self.n = 0
        self.calls = []

    def run(self, system_prompt, prompt, model, tools=None, cwd=None):
        self.calls.append({"prompt": prompt})
        self.n += 1
        if self.n == self.at_call:
            self.store.set_control(self.task_id, self.control)
        out = self.outputs.pop(0) if self.outputs else "(out)"
        from agentloop.models import RunResult

        return RunResult(output=out, tokens_in=1, tokens_out=1, model="mock")


def _cfg(store):
    from agentloop.config import LoopConfig

    return LoopConfig(
        db_path=store.db_path,
        workspace_root=str(Path(store.db_path).parent / "ws"),
        allow_test_exec=False,
        vcs_enabled=False,  # slice 6: no git subprocess here
    )


def test_pause_before_run_is_not_picked_up(store):
    task = add_task(store)
    loop, _ = make_loop(store, ["out", APPROVE])
    loop.pause(task.id)
    assert store.get_task(task.id).status == TaskStatus.PAUSED
    # PAUSED is not in the resumable set, so run() finds nothing to do.
    assert loop.run() == 0
    assert store.get_task(task.id).status == TaskStatus.PAUSED


def test_pause_mid_run_stops_at_boundary(store):
    task = add_task(store)
    # Revise on the first validation, and a human pauses during that first
    # round; the loop must stop at the next boundary instead of revising on.
    runner = ControllingRunner(
        ["v1", REVISE, "v2", APPROVE], store, task.id, "pause", at_call=2
    )
    loop = Loop(store, runner, Registry.load(), _cfg(store))
    loop.run_task(task)

    fresh = store.get_task(task.id)
    assert fresh.status == TaskStatus.PAUSED
    assert fresh.revision_count == 1  # it revised once, then paused
    assert runner.n == 2  # stopped before the 2nd worker call


def test_paused_task_survives_restart_and_resumes(store):
    task = add_task(store)
    loop, _ = make_loop(store, ["out", APPROVE])
    loop.pause(task.id)

    # "Restart": a brand-new Store/Loop over the same db file.
    store2 = Store(store.db_path)
    try:
        loop2, _ = make_loop(store2, ["out", APPROVE])
        assert store2.get_task(task.id).status == TaskStatus.PAUSED
        loop2.resume(task.id)
        assert store2.get_task(task.id).status == TaskStatus.PENDING
        loop2.run()
        assert store2.get_task(task.id).status == TaskStatus.DONE
    finally:
        store2.close()


def test_abort_mid_run_is_terminal_but_defensible(store):
    task = add_task(store)
    runner = ControllingRunner(
        ["v1", REVISE, "v2", APPROVE], store, task.id, "abort", at_call=2
    )
    loop = Loop(store, runner, Registry.load(), _cfg(store))
    loop.run_task(task)

    fresh = store.get_task(task.id)
    assert fresh.status == TaskStatus.ABORTED
    # Defensible: the worker's output and the audit trail are intact.
    assert fresh.output == "v1"
    kinds = [e["kind"] for e in store.events(task.id)]
    assert "control:abort" in kinds
    assert "verdict" in kinds  # earlier work still recorded


def test_aborted_task_is_not_resumable_by_the_loop(store):
    task = add_task(store)
    loop, _ = make_loop(store, ["out", APPROVE])
    loop.abort(task.id, note="wrong direction")
    assert store.get_task(task.id).status == TaskStatus.ABORTED
    assert loop.run() == 0  # loop leaves it alone


def test_every_control_transition_is_audited(store):
    task = add_task(store)
    loop, _ = make_loop(store, ["out", APPROVE])
    loop.pause(task.id)
    loop.resume(task.id)
    kinds = [e["kind"] for e in store.events(task.id)]
    assert "control:pause" in kinds and "control:run" in kinds
    assert "status:paused" in kinds


def test_abort_and_resume_are_noops_on_a_terminal_task(store):
    """Aborting or resuming a DONE task via CLI/REST (the dashboard hides the
    buttons, but the API does not) must not discard its terminal status."""
    task = add_task(store)
    loop, _ = make_loop(store, ["out", APPROVE])
    loop.run_task(task)
    assert store.get_task(task.id).status == TaskStatus.DONE

    loop.abort(task.id, note="oops wrong id")
    assert store.get_task(task.id).status == TaskStatus.DONE  # unchanged
    assert store.get_control(task.id) == "run"  # not flipped

    loop.resume(task.id)
    assert store.get_task(task.id).status == TaskStatus.DONE  # still done


def test_control_stop_keeps_an_existing_abort_reason(store):
    task = add_task(store)
    loop, _ = make_loop(store, ["out", APPROVE])
    # abort() stores the note in escalation_reason...
    loop.abort(task.id, note="diverging badly")
    assert "diverging badly" in store.get_task(task.id).escalation_reason
    # ...and a subsequent boundary check keeps it rather than overwriting.
    loop._control_stop(store.get_task(task.id))
    assert "diverging badly" in store.get_task(task.id).escalation_reason


def test_pause_survives_the_loops_own_task_writes(store):
    """The loop holds a task loaded with control='run'; its status writes must
    not clobber a concurrent pause (update_task never writes control)."""
    task = add_task(store)
    runner = ControllingRunner(
        ["v1", REVISE, "v2", APPROVE], store, task.id, "pause", at_call=1
    )
    loop = Loop(store, runner, Registry.load(), _cfg(store))
    loop.run_task(task)
    # Pause was set during call 1; despite the loop's set_status writes through
    # the round, the signal held and the task ended paused.
    assert store.get_control(task.id) == "pause"
    assert store.get_task(task.id).status == TaskStatus.PAUSED


def test_a_resumed_task_is_claimable_again(store):
    """Resuming a PAUSED task returns it to the queue, and the queue can only
    hand out a row whose lease is clear."""
    task = add_task(store)
    loop, _ = make_loop(store, ["out", APPROVE])
    claimed = store.claim_next_task("loop")
    assert claimed.claimed_by == "loop"
    loop.pause(task.id)
    assert store.get_task(task.id).status == TaskStatus.PAUSED

    loop.resume(task.id)
    assert store.get_task(task.id).claimed_by is None
    reclaimed = store.claim_next_task("loop")
    assert reclaimed is not None and reclaimed.id == task.id


def test_resuming_a_live_in_flight_task_keeps_its_lease(store):
    """`resume` accepts any non-terminal task, in-flight ones a live worker owns
    included (this API is wider than the dashboard's buttons). Releasing the lease
    there would be worse than the bug it fixes: an in-flight row with no lease
    matches neither disjunct of the claim SELECT and is invisible to
    `stranded_claims`, while `next_pending_task` still reports it — actionable
    work the loop can never hand out."""
    task = add_task(store)
    loop, _ = make_loop(store, ["out", APPROVE])
    claimed = store.claim_next_task("loop")
    assert claimed.status == TaskStatus.IN_PROGRESS

    loop.resume(task.id)
    fresh = store.get_task(task.id)
    assert fresh.claimed_by == "loop"  # untouched: its owner is still working
    assert fresh.status == TaskStatus.IN_PROGRESS
    resumed = store.claim_next_task("loop")
    assert resumed is not None and resumed.id == task.id


# -- an evicted worker stands down --------------------------------------------


class EvictingRunner:
    """A runner that, partway through a round, does to the task exactly what a
    human hitting pause-then-resume from another process does — and then lets the
    idle peer claim it, which is the window `resume`'s lease release opens.

    `at_call=0` never evicts: that is the control case, and it is what makes the
    stand-down assertions mean "it stopped because the lease went" rather than
    "it stops"."""

    def __init__(self, outputs, loop, store, task_id, at_call, peer="loop-1"):
        self.outputs = list(outputs)
        self.loop = loop
        self.store = store
        self.task_id = task_id
        self.at_call = at_call
        self.peer = peer
        self.calls = []
        self.peer_claim = None

    def run(self, system_prompt, prompt, model, tools=None, cwd=None):
        self.calls.append({"prompt": prompt})
        if len(self.calls) == self.at_call:
            self.loop.pause(self.task_id)
            self.loop.resume(self.task_id)
            # Pending and unleased is precisely what the claim CAS matches, so a
            # peer polling every _IDLE_POLL_SECONDS takes it. Asserted here, not
            # merely assumed: if this ever stops succeeding, the eviction the
            # rest of the test reasons about did not happen.
            self.peer_claim = self.store.claim_next_task(self.peer)
            assert self.peer_claim is not None, "the eviction window did not open"
        out = self.outputs.pop(0) if self.outputs else "(out)"
        from agentloop.models import RunResult

        return RunResult(output=out, tokens_in=1, tokens_out=1, model="mock")


def _evicting_loop(store, task, at_call):
    loop = Loop(store, None, Registry.load(), _cfg(store))
    runner = EvictingRunner(
        ["v1", REVISE, "v2", APPROVE], loop, store, task.id, at_call=at_call
    )
    loop.runner = runner
    return loop, runner


def test_a_worker_whose_lease_was_taken_by_a_peer_stands_down(store, recwarn):
    """The concrete double-claim `PAUSED` never prevented.

    `pause` stamps the status immediately and never touches the lease, so a
    worker only learns of the pause at its next boundary — after the current
    model call returns. A human who pauses, sees nothing happen and resumes
    therefore hands the row to the idle peer (pending + unleased is what the
    claim CAS matches) while the first worker is still inside the task. Two paid
    worker attempts against one budget, two validator rounds, both writing
    `task.output`, either able to drive the task terminal.

    Proving the lease dead from outside is not possible — a retired claim id is
    indistinguishable from a live process. So the evicted worker stands down: at
    the boundary it re-reads the row and ends the round when the lease is no
    longer its own."""
    task = add_task(store)
    loop, runner = _evicting_loop(store, task, at_call=2)
    claimed = store.claim_next_task("loop")
    assert claimed.claimed_by == "loop"

    loop.run_task(claimed)

    # Round 1's worker + validator ran and are paid for; round 2's worker never
    # started, so the peer is alone in the task.
    assert len(runner.calls) == 2
    assert store.task_metrics(task.id)["attempts"] == 2
    # The stand-down writes no status and does not take the lease back: the task
    # belongs to the peer now, and clobbering its row is the failure, not the fix.
    assert store.get_task(task.id).claimed_by == "loop-1"
    lost = [e for e in store.events(task.id) if e["kind"] == "claim_lost"]
    assert len(lost) == 1
    assert lost[0]["payload"]["was"] == "loop"
    assert lost[0]["payload"]["now"] == "loop-1"
    assert any("stood down" in str(w.message) for w in recwarn.list), (
        "a degradation gets a RuntimeWarning as well as an event"
    )
    # Not a quality failure: the round that was interrupted is not a revision
    # anybody has to pay for. **Zero, not one** — and that is a strengthening, not
    # a weakening. The eviction lands during round 1's validator call, so its
    # `revision_count += 1` and its `set_status(REVISING)` both happen while the
    # peer already owns the row; lease-predicated, they now match no row and
    # no-op, where before they wrote a revision count and a status onto somebody
    # else's round. The sentence above is what the row finally says.
    assert store.get_task(task.id).revision_count == 0
    assert "status:revising" not in [e["kind"] for e in store.events(task.id)]


def test_a_worker_that_still_holds_its_lease_runs_the_next_round(store):
    """The control case for the test above: the identical script and fixture with
    no eviction runs both rounds to completion. Without it, "the worker stopped"
    would also pass against a check that stops every worker at every boundary."""
    task = add_task(store)
    loop, runner = _evicting_loop(store, task, at_call=0)
    claimed = store.claim_next_task("loop")

    loop.run_task(claimed)

    assert len(runner.calls) == 4
    assert store.get_task(task.id).status is TaskStatus.DONE
    assert [e for e in store.events(task.id) if e["kind"] == "claim_lost"] == []


def test_the_stand_down_check_runs_before_any_work_or_status_write(store):
    """Placement: the check is the *first* thing at the boundary, ahead of
    `_control_stop`. Both writes below it belong to whoever owns the task now — a
    PAUSED/ABORTED stamp from a worker that no longer holds the lease is a write
    over a peer's round."""
    task = add_task(store)
    loop, runner = _evicting_loop(store, task, at_call=0)
    claimed = store.claim_next_task("loop")
    store.release_claim(task.id)  # evicted before the first boundary
    store.set_control(task.id, "pause")  # and _control_stop would fire too

    loop.run_task(claimed)

    assert runner.calls == []  # no model call was paid for
    kinds = [e["kind"] for e in store.events(task.id)]
    assert kinds.count("claim_lost") == 1
    # No PAUSED/ABORTED stamp: those belong to whoever owns the task now, and this
    # worker does not. The pause signal itself is untouched, so the next holder
    # stops at its first boundary.
    assert "status:paused" not in kinds
    assert store.get_control(task.id) == "pause"
    # The row is unowned *and* was left at a transient status by this worker, so
    # standing down hands it back as claimable rather than stranding it: an
    # in-flight row with no lease matches neither disjunct of the claim SELECT and
    # is invisible to `stranded_claims` while `next_pending_task` advertises it.
    assert store.get_task(task.id).status is TaskStatus.PENDING
    assert store.claim_next_task("peer").id == task.id


def test_a_decision_landing_in_the_stand_down_window_is_not_undone(store, monkeypatch):
    """The stand-down's `PENDING` write is a compare-and-swap, not a read then a
    write.

    The read (`get_task`) was a separate locked call; the lock was then released,
    `warnings.warn` formatted and wrote to stderr, and only afterwards did the
    transaction open and write. The comment claimed "the row is unowned by
    construction" — true of the *read*, not of the write — and cross-process the
    RLock protects nothing at all.

    A `human_approve` landing in that window left a signed-off `DONE` task
    returned to `PENDING` and re-claimed. Under the slice-3 graph that is worse
    than a lost decision: `DONE` had already released the task's dependents, so
    they run against output a second worker is now overwriting. `human_reject`
    behaves the same and, unlike `abort`, leaves `control='run'` so nothing
    self-heals.
    """
    task = add_task(store)
    loop, _ = make_loop(store, ["out", APPROVE])
    claimed = store.claim_next_task("loop")
    store.release_claim(task.id)  # a peer released it: unowned, still in_progress

    # The window itself: the warning is formatted and written between the read and
    # the write, with no lock held.
    import agentloop.loop as loop_module

    def decide_in_the_window(*args, **kwargs):
        loop.human_approve(task.id, note="signed off while the worker stood down")

    monkeypatch.setattr(loop_module.warnings, "warn", decide_in_the_window)

    assert loop._claim_lost(claimed) is True

    stored = store.get_task(task.id)
    assert stored.status is TaskStatus.DONE, "a human decision must not be undone"
    assert store.claim_next_task("peer") is None, "and it must not be re-claimable"


def test_standing_down_clears_a_stale_escalation_reason(store):
    """`set_status(current, PENDING, reason="")` does not clear
    `escalation_reason` — only a truthy reason is assigned — so a task that had
    once been escalated returned to the queue carrying the old diagnosis, and
    `agentloop status` reported a stale reason against a runnable task. Blanked
    explicitly, exactly as `approve_tool_request` and `human_redo` do."""
    task = add_task(store)
    store.set_status(task, TaskStatus.PENDING, reason="a diagnosis from last time")
    claimed = store.claim_next_task("loop")
    assert claimed.escalation_reason == "a diagnosis from last time"
    store.release_claim(task.id)

    loop, _ = make_loop(store, ["out", APPROVE])
    assert loop._claim_lost(claimed) is True

    stored = store.get_task(task.id)
    assert stored.status is TaskStatus.PENDING
    assert stored.escalation_reason == ""


class RedoingRunner:
    """A runner that does to the task, partway through a round, exactly what a
    human running `agentloop redo` from another process does."""

    def __init__(self, outputs, loop, task_id, at_call):
        self.outputs = list(outputs)
        self.loop = loop
        self.task_id = task_id
        self.at_call = at_call
        self.calls = []

    def run(self, system_prompt, prompt, model, tools=None, cwd=None):
        self.calls.append({"prompt": prompt})
        if len(self.calls) == self.at_call:
            self.loop.human_redo(self.task_id)
        out = self.outputs.pop(0) if self.outputs else "(out)"
        from agentloop.models import RunResult

        return RunResult(output=out, tokens_in=1, tokens_out=1, model="mock")


def _redoing_loop(store, task, at_call):
    loop = Loop(store, None, Registry.load(), _cfg(store))
    runner = RedoingRunner(
        ["v1 worker output", REVISE, "v2", APPROVE], loop, task.id, at_call=at_call
    )
    loop.runner = runner
    return loop, runner


def test_a_redo_landing_mid_round_is_not_half_undone_by_the_evicted_worker(store):
    """`human_redo`'s contract is "same task definition, fresh start, **NO**
    carried context" — and the evicted worker's own in-round writes were putting
    the carried context back.

    The stand-down cannot protect this by reading a fresh row: the writes at
    `task.output = result.output; update_task(task)` and `set_status(REVISING)`
    happen *before* the next boundary, so by the time the freshness matters the
    stale values are already the row's current values. What came out was a hybrid
    — workspace wiped, but `output` and `revision_count` carried — and, worse than
    stranded, *runnable*: the stand-down hands it back to the queue and the loop
    picks it up.

    So the writes are lease-predicated (`UPDATE … WHERE id=? AND claimed_by=?`),
    the discipline the store already applies to the claim and to both tool-request
    accessors: a released worker's writes match no row and no-op, rather than
    resurrecting state a human explicitly reset.
    """
    task = add_task(store)
    loop, runner = _redoing_loop(store, task, at_call=1)
    claimed = store.claim_next_task("loop")

    loop.run_task(claimed)

    stored = store.get_task(task.id)
    # Exactly what `human_redo` left, and nothing the evicted round wrote over it.
    assert stored.output == ""
    assert stored.revision_count == 0
    assert stored.escalation_reason == ""
    assert stored.status is TaskStatus.PENDING
    assert stored.claimed_by is None
    # And the round really was interrupted mid-flight — otherwise the assertions
    # above would be measuring a run that never wrote anything.
    assert [e["kind"] for e in store.events(task.id)].count("human_redo") == 1
    # It went on *past the worker's own writes* — `task.output = ...` and
    # `set_status(REVISING)`, the two that used to put the redone context back —
    # and reached the stage after them, which is what this test measures. The
    # test-run row is the proof: it is written between the worker and the
    # validator and is not lease-predicated.
    assert len(store.test_runs(task.id)) == 1
    # It stops short of the validator, and that is a *deliberate* consequence of
    # the slice-9 cwd fix rather than a weakened assertion: `human_redo` wipes
    # the workspace, which is now the directory the validator would run in, so
    # the loop refuses that call as a config error instead of invoking an agent
    # in a directory that is gone. Re-creating it would hand the validator an
    # empty tree and hide the wipe.
    assert len(runner.calls) == 1


def test_a_worker_holding_its_lease_still_writes_its_output(store):
    """The control on the lease predicate: with nothing evicting it, the identical
    fixture writes output, counts its revision and finishes. Without this,
    "the row was not written" would also pass against a predicate that never
    matches anything."""
    task = add_task(store)
    loop, runner = _redoing_loop(store, task, at_call=0)
    claimed = store.claim_next_task("loop")

    loop.run_task(claimed)

    stored = store.get_task(task.id)
    assert stored.output == "v2"
    assert stored.revision_count == 1
    assert stored.status is TaskStatus.DONE
    assert len(runner.calls) == 4


def test_an_evicted_round_does_not_stamp_a_park_it_cannot_write(store):
    """A hole opened by making the in-round writes lease-predicated, closed here.

    The park is two writes in one transaction: `tool_requests_mark_parked` stamps
    `parked=1` and `set_status(NEEDS_HUMAN)` records the escalation. Once the
    status write can legitimately no-op — the worker's lease was taken mid-round —
    an unguarded stamp leaves `parked=1` standing against a task that is *not*
    parked. That is precisely the stale flag the release predicate's third term
    exists to prevent: a later, unrelated escalation to NEEDS_HUMAN would become
    revertible by approving the tool request.

    So the stamp follows the status write's `rowcount`, not the loop's intention.
    """
    task = add_task(store)
    loop = Loop(store, None, Registry.load(), _cfg(store))
    reply = "worker out\nTOOL_REQUEST: shell (blocking) - cannot build without it"
    runner = RedoingRunner([reply, APPROVE], loop, task.id, at_call=1)
    loop.runner = runner
    claimed = store.claim_next_task("loop")

    loop.run_task(claimed)

    # The ask is recorded — the row is written outside the lease's protection, and
    # withholding a capability silently is what this slice refuses.
    (row,) = store.tool_requests(task_id=task.id)
    assert row.blocking is True
    # But nothing claims the loop is holding this task on it: the redo took the
    # task, and its status is the redo's.
    assert row.parked is False
    stored = store.get_task(task.id)
    assert stored.status is TaskStatus.PENDING
    assert "Awaiting tool approval" not in stored.escalation_reason
