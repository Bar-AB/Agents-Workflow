"""Mid-run human control: pause / resume / abort, read at each iteration
boundary and driven through the store so it works cross-process."""

from pathlib import Path

import pytest

from agentloop.loop import Loop
from agentloop.models import TaskStatus
from agentloop.registry import Registry
from agentloop.runner import MockRunner
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

    def run(self, system_prompt, prompt, model, tools=None):
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
