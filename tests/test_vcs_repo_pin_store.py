"""Slice 9 P2 remediation C3 - the repo-level config-pin baseline.

`vcs_pins` is keyed by `task_id`, which is the right key for scratch mode
(workspace and `.git/config` are 1:1). In worktree mode every task shares one
`<repo_root>/.git/config`, so the baseline is a fact about the *repository* and
needs its own key. The pin has to live where no agent has a write path, which
is the whole reason it is not kept under `.git/` - so it goes in the store.
"""

from __future__ import annotations

from agentloop.store import Store


def test_a_repo_baseline_is_absent_until_recorded():
    store = Store(":memory:")
    assert store.vcs_repo_pin("C:/repo") == ""


def test_a_repo_baseline_round_trips_and_is_keyed_by_repo_not_task():
    store = Store(":memory:")
    store.set_vcs_repo_pin("/a/repo", "deadbeef")
    store.set_vcs_repo_pin("/b/repo", "cafef00d")
    assert store.vcs_repo_pin("/a/repo") == "deadbeef"
    assert store.vcs_repo_pin("/b/repo") == "cafef00d"


def test_the_repo_key_is_normalised_so_one_repo_has_one_baseline():
    """Two spellings of one path must not become two baselines - a second
    baseline is a gate with a second answer in it, which is what
    `set_vcs_pin`'s own docstring refuses for the per-task row."""
    store = Store(":memory:")
    store.set_vcs_repo_pin("/a/repo", "deadbeef")
    assert store.vcs_repo_pin("/a/repo/") == "deadbeef"
    assert store.vcs_repo_pin("/a/./repo") == "deadbeef"


def test_recording_replaces_rather_than_appends():
    store = Store(":memory:")
    store.set_vcs_repo_pin("/a/repo", "one")
    store.set_vcs_repo_pin("/a/repo", "two")
    assert store.vcs_repo_pin("/a/repo") == "two"
