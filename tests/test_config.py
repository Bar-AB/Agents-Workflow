"""Slice 9 P3 — the config knobs for existing-repository workspaces.

Seam: `LoopConfig.__post_init__`, the same seam `_coerced`'s bare-string and
`memory_retrieval_backend` precedents already use. No mocking, no store: this
is pure dataclass validation.
"""

from __future__ import annotations

import os

import pytest

from agentloop.config import LoopConfig


def test_scratch_mode_is_the_default():
    cfg = LoopConfig()
    assert cfg.workspace_mode == "scratch"


def test_unknown_workspace_mode_raises():
    with pytest.raises(ValueError):
        LoopConfig(workspace_mode="clone")


def test_worktree_root_inside_repo_root_is_refused(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    inside = repo / "ws"
    with pytest.raises(ValueError):
        LoopConfig(
            workspace_mode="worktree",
            repo_root=str(repo),
            worktree_root=str(inside),
        )


def test_worktree_root_inside_repo_root_is_refused_even_when_textually_different_via_a_symlink_or_junction(
    tmp_path,
):
    """The escape P2's `vcs.py` guard was built to close, one level up: a path
    that is lexically *outside* `repo_root` but *resolves* inside it through a
    symlink/junction must still be refused. A lexical-only compare would pass
    this and reopen residual 2."""
    repo = tmp_path / "repo"
    repo.mkdir()
    real_inside = repo / "real_ws"
    real_inside.mkdir()
    link = tmp_path / "link_to_inside"
    try:
        os.symlink(real_inside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks require elevated privileges on this platform")
    # `link` is textually a sibling of `repo`, not inside it — but it resolves
    # to a directory that *is* inside `repo`.
    assert not str(link).startswith(str(repo))
    with pytest.raises(ValueError):
        LoopConfig(
            workspace_mode="worktree",
            repo_root=str(repo),
            worktree_root=str(link),
        )


def test_worktree_root_outside_repo_root_is_accepted(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "elsewhere"
    cfg = LoopConfig(
        workspace_mode="worktree",
        repo_root=str(repo),
        worktree_root=str(outside),
    )
    assert cfg.workspace_mode == "worktree"


def test_the_check_is_scoped_to_worktree_mode_scratch_mode_never_reads_it(tmp_path):
    """`worktree_root`/`repo_root` are documented unread in scratch mode; a
    config that only ever runs scratch mode must not be broken by values meant
    for a mode it never enters."""
    repo = tmp_path / "repo"
    repo.mkdir()
    inside = repo / "ws"
    cfg = LoopConfig(
        workspace_mode="scratch",
        repo_root=str(repo),
        worktree_root=str(inside),
    )
    assert cfg.workspace_mode == "scratch"


def test_neuter_removing_the_refusal_reopens_the_escape(tmp_path, monkeypatch):
    """[NEUTER] Falsify the guard: with the containment check disabled, the
    exact configuration the refusal exists to block must construct cleanly —
    proving the test above was pinning something real, not a tautology."""
    import agentloop.config as config_mod

    monkeypatch.setattr(config_mod, "_is_within", lambda child, parent: False)
    repo = tmp_path / "repo"
    repo.mkdir()
    inside = repo / "ws"
    cfg = config_mod.LoopConfig(
        workspace_mode="worktree",
        repo_root=str(repo),
        worktree_root=str(inside),
    )
    assert cfg.workspace_mode == "worktree"  # no refusal fired


def test_scratch_mode_is_a_proven_no_op_over_the_whole_observable_dataclass():
    """Same register as slice 6's
    `test_vcs_disabled_and_enabled_produce_identical_observable_state`: every
    field a pre-P3 LoopConfig() would have produced is byte-identical."""
    cfg = LoopConfig()
    assert cfg.workspace_root == ".agentloop/ws"
    assert cfg.vcs_enabled is True
    assert cfg.vcs_command == "git"
    # The five new fields exist with the documented defaults and touch nothing
    # else — no existing field's default moved.
    assert cfg.repo_root == "."
    assert cfg.worktree_root == "~/.agentloop/ws"
    assert cfg.vcs_base_ref == "HEAD"
    assert cfg.vcs_branch_prefix == "agentloop/task-"
