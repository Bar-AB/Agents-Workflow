"""Slice 9 remediation, HIGH-1: both out-of-branch write detectors fold a
failed **before** snapshot into `None` and return with no event logged, while
the **after**-side failure already logs an explicit `*_write_detection_failed`
event. `_vcs_detect_validator_writes`'s own docstring states "a detection that
silently stops looking is worse than none" -- then does exactly that a few
lines earlier, for the before side.

`before is None` legitimately means "nothing to watch" (scratch mode, or vcs
not ready) and must stay silent -- these tests isolate the *other* case: the
snapshot was attempted and the read itself failed.
"""

from __future__ import annotations


import pytest

from agentloop import vcs
from agentloop.store import Store
from tests.test_loop import APPROVE
from tests.test_vcs_loop import add_task, events_of_kind, make_loop


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def test_a_failed_before_snapshot_for_validator_writes_is_logged_not_silent(
    store, monkeypatch
):
    """Fail exactly the first `vcs.working_tree_state` call (the validator's
    `tree_before` snapshot at the `run_task` call site) and let every later
    call through untouched, so the *after* snapshot inside
    `_vcs_detect_validator_writes` still succeeds -- isolating the before-side
    gap from the already-correct after-side behaviour."""
    task = add_task(store)
    loop, _ = make_loop(store, ["worker output", APPROVE])

    real = vcs.working_tree_state
    calls = {"n": 0}

    def flaky(*args, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return vcs.VcsResult(ok=False, reason="git-failed")
        return real(*args, **kw)

    monkeypatch.setattr("agentloop.loop.vcs.working_tree_state", flaky)

    loop.run_task(task)

    found = events_of_kind(store, task.id, "validator_write_detection_failed")
    assert len(found) == 1, [e["kind"] for e in store.events(task.id)]
    note = found[0]["payload"]["note"].lower()
    assert "not" in note and "claim" in note  # must not claim nothing appeared


def test_a_failed_before_snapshot_for_out_of_branch_writes_is_logged_not_silent(
    store, monkeypatch, tmp_path
):
    """Same shape, for residual 2's `repo_status` snapshot of `repo_root`
    around the test command -- fail only the first `vcs.repo_status` call
    (the before-snapshot at the `run_task` call site) and let the after-side
    call through."""
    from agentloop import vcs as vcs_module
    from tests.test_worktree_vcs import make_repo

    repo_root = make_repo(tmp_path / "operator_repo")
    wt_root = tmp_path / "wt_root"
    task = add_task(store)
    loop, _ = make_loop(
        store,
        ["worker output", APPROVE],
        workspace_mode="worktree",
        repo_root=str(repo_root),
        worktree_root=str(wt_root),
    )

    real = vcs_module.repo_status
    calls = {"n": 0}

    def flaky(*args, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return vcs_module.VcsResult(ok=False, reason="git-failed")
        return real(*args, **kw)

    monkeypatch.setattr("agentloop.loop.vcs.repo_status", flaky)

    loop.run_task(task)

    found = events_of_kind(store, task.id, "worktree_write_detection_failed")
    assert len(found) == 1, [e["kind"] for e in store.events(task.id)]
    note = found[0]["payload"]["note"].lower()
    assert "not" in note and "claim" in note
