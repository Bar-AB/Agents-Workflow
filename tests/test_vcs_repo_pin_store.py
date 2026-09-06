"""Slice 9 P2 remediation C3 - the repo-level config-pin baseline.

`vcs_pins` is keyed by `task_id`, which is the right key for scratch mode
(workspace and `.git/config` are 1:1). In worktree mode every task shares one
`<repo_root>/.git/config`, so the baseline is a fact about the *repository* and
needs its own key. The pin has to live where no agent has a write path, which
is the whole reason it is not kept under `.git/` - so it goes in the store.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from agentloop import vcs
from agentloop.store import Store


def _link_dir(link, target) -> bool:
    """A directory link at `link` pointing at `target`, or False if this
    platform will not make one without privileges. Mirrors `test_vcs.py`'s
    `_link_dir` helper (a junction on Windows via `mklink /J`, a symlink
    elsewhere) — the alias has to be a real filesystem redirection the OS
    resolves, not merely a textually different spelling `os.path.abspath`
    already collapses."""
    try:
        if os.name == "nt":
            proc = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True,
                text=True,
                shell=False,
            )
            return proc.returncode == 0 and link.exists()
        os.symlink(target, link, target_is_directory=True)
        return True
    except Exception:
        return False


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


# -- CRITICAL remediation: a real alias must not reopen C3's pin-laundering
# escape --------------------------------------------------------------------


def test_a_real_filesystem_alias_of_one_repository_shares_its_baseline(tmp_path):
    """`_repo_key` must key on the resolved location, not the spelling.

    A junction/symlink is a *real* alias the OS resolves transparently — unlike
    a trailing slash or a `./` component, which `os.path.abspath` already
    collapses with no actual normalisation. `vcs.config_pin` reads through
    `Path(...).stat()`/`read_bytes()`, so it returns the identical fingerprint
    for both spellings of one physical `.git/config`. If `Store._repo_key`
    is purely lexical, the alias reads as a repository the store has never
    seen — an absent baseline, which `vcs._init_worktree` treats as "mint a
    fresh one" rather than "refuse" (exactly the escape P2's C3 closed,
    reopened through the store key's identity instead of the task id).

    Watched failing against the pre-fix (`os.path.abspath`-only) `_repo_key`:
    the alias's baseline read `""` instead of the canonical spelling's
    recorded pin."""
    canonical = tmp_path / "canonical-repo"
    canonical.mkdir()
    alias = tmp_path / "alias-repo"
    if not _link_dir(alias, canonical):
        pytest.skip("this platform/user cannot create a directory junction/symlink")

    subprocess.run(["git", "init", "-q"], cwd=str(canonical), check=True, shell=False)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "init",
        ],
        cwd=str(canonical),
        check=True,
        shell=False,
    )

    # Confirm the alias is a genuine filesystem redirection to the same
    # physical repository before asserting anything about the store: the
    # config-pin fingerprint, computed by reading actual bytes through the
    # OS, must already agree across both spellings.
    canonical_fingerprint = vcs.config_pin(canonical)
    alias_fingerprint = vcs.config_pin(alias)
    assert canonical_fingerprint != ""
    assert canonical_fingerprint == alias_fingerprint

    store = Store(":memory:")
    store.set_vcs_repo_pin(str(canonical), canonical_fingerprint)

    # The real bug: the alias must resolve to the SAME baseline, not read as
    # an unpinned, never-before-seen repository.
    assert store.vcs_repo_pin(str(alias)) == canonical_fingerprint
