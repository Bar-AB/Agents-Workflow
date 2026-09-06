"""H3 (slice 9, carried in from P1's review): the validator can write into the
task workspace, after the round snapshot, and nothing recorded it.

The shipped `validator` declares `file_io` = Read + **Write** + Edit, and P1
pointed that write surface at the task workspace. `run_task`'s order is
worker -> `vcs.commit(round)` -> executor -> validator, so anything the
validator writes lands *after* the snapshot: `refs/agentloop/approved` can be
moved to a tip whose tree lacks it, `rollback`'s `clean -ffdqx` deletes it with
no `refs/agentloop/discarded/<sha>` covering it, and a revision round starts
from a tree the validator silently altered while `task.output` says otherwise.

**This is detection, not prevention**, and the tests below pin that distinction
in both directions: the event fires, and the file is still on disk afterwards.
Narrowing the shipped validator to `file_read` is a change to a shipped agent's
declared capability with tool-gate blast radius - a decision for the human, not
for this phase - and *committing* the writes would be worse still, since it
would make a validator's silent edits to the worker's output part of the
approved tree.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentloop.runner import MockRunner
from agentloop.store import Store
from tests.test_loop import APPROVE
from tests.test_vcs_loop import add_task, events_of_kind, make_loop, ws_of


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


class ValidatorWritingRunner(MockRunner):
    """Writes into the workspace on the **validator** call - the second run of
    a one-round task - which is where a real `file_io` validator's writes land:
    after the round commit, before any status decision."""

    def __init__(self, outputs, ws: Path, files: dict[str, str], on_call: int = 2):
        super().__init__(outputs)
        self._ws = ws
        self._files = files
        self._on_call = on_call
        self._calls = 0

    def run(self, *args, **kw):
        self._calls += 1
        if self._calls == self._on_call:
            self._ws.mkdir(parents=True, exist_ok=True)
            for name, body in self._files.items():
                (self._ws / name).write_text(body, encoding="utf-8")
        return super().run(*args, **kw)


def test_a_validator_write_after_the_round_snapshot_is_detected(store, tmp_path):
    task = add_task(store)
    loop, _ = make_loop(store, ["worker output", APPROVE])
    ws = ws_of(loop, task.id)
    loop.runner = ValidatorWritingRunner(
        ["worker output", APPROVE], ws, {"validator_edit.py": "the validator wrote me"}
    )

    loop.run_task(task)

    found = events_of_kind(store, task.id, "validator_wrote_workspace")
    assert len(found) == 1, [e["kind"] for e in store.events(task.id)]
    payload = found[0]["payload"]
    assert any("validator_edit.py" in p for p in payload["entries"]), payload

    # Detection, not prevention - asserted on the filesystem, not on wording
    # alone: the write really did land and really was not undone.
    assert (ws / "validator_edit.py").read_text(encoding="utf-8") == (
        "the validator wrote me"
    )


def test_the_detection_event_does_not_claim_the_write_was_prevented(store, tmp_path):
    task = add_task(store)
    loop, _ = make_loop(store, ["worker output", APPROVE])
    ws = ws_of(loop, task.id)
    loop.runner = ValidatorWritingRunner(
        ["worker output", APPROVE], ws, {"validator_edit.py": "x"}
    )
    loop.run_task(task)

    payload = events_of_kind(store, task.id, "validator_wrote_workspace")[0]["payload"]
    assert payload["prevented"] is False
    note = payload["note"].lower()
    assert "detect" in note
    for overclaim in ("prevented", "blocked", "refused", "reverted", "rolled back"):
        assert overclaim not in note, note
    # It also must not claim the writes are recoverable: they are outside the
    # round commit by construction, which is the whole finding.
    assert "recoverable" not in note


def test_a_validator_that_writes_nothing_produces_no_event(store):
    """The control. An event on every round would be noise, and noise is how a
    real detection gets ignored."""
    task = add_task(store)
    loop, _ = make_loop(store, ["worker output", APPROVE])

    loop.run_task(task)

    assert events_of_kind(store, task.id, "validator_wrote_workspace") == []


def test_detection_is_silent_when_vcs_is_off(store):
    """`vcs_enabled=False` stays a behavioural no-op: the detection is a git
    `status` and there is nothing to ask when the feature is off."""
    task = add_task(store)
    loop, _ = make_loop(store, ["worker output", APPROVE], vcs_enabled=False)
    ws = ws_of(loop, task.id)
    loop.runner = ValidatorWritingRunner(
        ["worker output", APPROVE], ws, {"validator_edit.py": "x"}
    )

    loop.run_task(task)

    assert events_of_kind(store, task.id, "validator_wrote_workspace") == []
