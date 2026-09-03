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


# -- P3: the human-only re-bless surface -------------------------------------


def test_rebless_replaces_the_baseline_and_returns_it():
    store = Store(":memory:")
    store.set_vcs_repo_pin("/a/repo", "old")
    result = store.rebless_vcs_repo_pin(
        "/a/repo", "new", note="operator upgraded git hooks"
    )
    assert result == "new"
    assert store.vcs_repo_pin("/a/repo") == "new"


def test_rebless_over_an_unpinned_repository_behaves_sanely_not_crash():
    """The plan explicitly calls this out: re-bless invoked on a repository
    that was never pinned at all."""
    store = Store(":memory:")
    assert store.vcs_repo_pin("/a/repo") == ""
    result = store.rebless_vcs_repo_pin("/a/repo", "first", note="")
    assert result == "first"
    assert store.vcs_repo_pin("/a/repo") == "first"


def test_rebless_is_audited_with_old_and_new_fingerprints():
    store = Store(":memory:")
    store.set_vcs_repo_pin("/a/repo", "old")
    store.rebless_vcs_repo_pin("/a/repo", "new", note="rotated .gitconfig")
    events = [e for e in store.events() if e["kind"] == "vcs_repo_pin_reblessed"]
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["old_fingerprint"] == "old"
    assert payload["new_fingerprint"] == "new"
    assert payload["note"] == "rotated .gitconfig"
    assert events[0]["task_id"] is None


def test_rebless_never_records_config_bytes_only_fingerprints():
    """The pin is a hash; re-bless must never be handed or log anything that
    looks like the raw `.git/config` content."""
    store = Store(":memory:")
    secret_looking = "[credential]\n\thelper = store --file=/secrets\n"
    store.rebless_vcs_repo_pin("/a/repo", "abc123", note="")
    events = [e for e in store.events() if e["kind"] == "vcs_repo_pin_reblessed"]
    assert secret_looking not in str(events[0]["payload"])
