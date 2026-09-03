"""Slice 9 P3 — the two operator-facing surfaces P2's review named as no
longer optional: `agentloop workspace prune` and the re-bless command.

Real `git` subprocesses (via `vcs.py`), exactly as the other worktree test
files: the risk here is git's own resolution of a path and its own admin
state, which a mocked git cannot exhibit. `loop.py` is not wired to worktree
mode yet (that is P4), so these tests build the fixture state directly through
`vcs.py`/`Store`, the same shape the loop will produce once it is wired.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from agentloop import vcs
from agentloop.cli import main
from agentloop.config import LoopConfig
from agentloop.models import Task, TaskStatus
from agentloop.store import Store
from tests.test_worktree_vcs import git, make_repo


def _config_file(tmp_path: Path, **overrides) -> str:
    cfg = tmp_path / "loopconfig.json"
    data = {"db_path": str(tmp_path / "agentloop.db")}
    data.update(overrides)
    cfg.write_text(json.dumps(data), encoding="utf-8")
    return str(cfg)


@pytest.fixture(autouse=True)
def _in_tmp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


# -- `agentloop workspace prune` ----------------------------------------------


def test_prune_removes_a_terminal_tasks_worktree_and_leaves_no_stale_entry(tmp_path):
    repo_root = make_repo(tmp_path / "operator_repo")
    wt_root = tmp_path / "wt_root"

    store = Store(tmp_path / "agentloop.db")
    task = Task(id=None, title="t", goal="g", acceptance_criteria="c")
    task_id = store.add_task(task)
    store.set_status(store.get_task(task_id), TaskStatus.DONE)

    config = LoopConfig(
        db_path=str(tmp_path / "agentloop.db"),
        workspace_mode="worktree",
        repo_root=str(repo_root),
        worktree_root=str(wt_root),
        vcs_enabled=True,
    )
    # Build the workspace through the real seam (`executor.workspace_for`),
    # not by hand — that is what pins the directory layout the CLI command
    # will look for.
    from agentloop.executor import workspace_for

    ws = workspace_for(
        config.workspace_root, task_id, create=True, config=config, repo_root=repo_root
    )
    assert ws.is_dir()
    admin = repo_root / ".git" / "worktrees" / f"task-{task_id}"
    assert admin.is_dir()
    store.set_vcs_repo_pin(str(repo_root), vcs.config_pin(repo_root, repo_root))
    store.close()

    cfg_path = _config_file(
        tmp_path,
        workspace_mode="worktree",
        repo_root=str(repo_root),
        worktree_root=str(wt_root),
        vcs_enabled=True,
    )
    rc = main(["--config", cfg_path, "workspace", "prune"])
    assert rc == 0
    assert not admin.exists()
    assert f"task-{task_id}" not in git(repo_root, "worktree", "list").stdout


def test_prune_in_scratch_mode_is_a_no_op(tmp_path, capsys):
    cfg_path = _config_file(tmp_path)  # workspace_mode defaults to 'scratch'
    rc = main(["--config", cfg_path, "workspace", "prune"])
    assert rc == 0
    assert "nothing to prune" in capsys.readouterr().out.lower()


def test_prune_also_clears_a_stale_admin_entry_left_by_a_manual_delete(tmp_path):
    """The case `vcs.remove_worktree` correctly refuses rather than repairs:
    the workspace directory is already gone by some other means, and only the
    bare `git worktree prune` catch-all clears the registration."""
    import shutil

    repo_root = make_repo(tmp_path / "operator_repo2")
    wt_root = tmp_path / "wt_root2"

    store = Store(tmp_path / "agentloop.db")
    task = Task(id=None, title="t", goal="g", acceptance_criteria="c")
    task_id = store.add_task(task)
    store.set_status(store.get_task(task_id), TaskStatus.ABORTED)

    config = LoopConfig(
        db_path=str(tmp_path / "agentloop.db"),
        workspace_mode="worktree",
        repo_root=str(repo_root),
        worktree_root=str(wt_root),
        vcs_enabled=True,
    )
    from agentloop.executor import workspace_for

    ws = workspace_for(
        config.workspace_root, task_id, create=True, config=config, repo_root=repo_root
    )
    admin = repo_root / ".git" / "worktrees" / f"task-{task_id}"
    assert admin.is_dir()
    store.set_vcs_repo_pin(str(repo_root), vcs.config_pin(repo_root, repo_root))
    store.close()

    # Simulate an operator deleting the directory directly, outside the loop.
    shutil.rmtree(ws, ignore_errors=True)
    assert admin.is_dir()  # the entry outlives the directory

    cfg_path = _config_file(
        tmp_path,
        workspace_mode="worktree",
        repo_root=str(repo_root),
        worktree_root=str(wt_root),
        vcs_enabled=True,
    )
    rc = main(["--config", cfg_path, "workspace", "prune"])
    assert rc == 0
    assert not admin.exists()


def test_prune_leaves_the_operators_own_unrelated_worktree_untouched(tmp_path):
    """Slice 9 P3 remediation cycle 3, HIGH 2. `git worktree prune` is
    repo-wide by git's own design — it has no branch/owner scoping, so a
    bare call clears the admin entry for *any* worktree whose directory is
    unreachable, whether agentloop created it or not.

    [RED-FIRST] Measured against the pre-fix tree: with an agentloop
    worktree and a separate, manually-created operator worktree
    (`operator/manual-feature`, not `agentloop/task-N`) both missing their
    directories, `agentloop workspace prune`'s bare `git worktree prune`
    catch-all cleared *both* admin entries — the operator's branch survives,
    but its worktree registration is gone, so a directory that reappears
    later (a remounted drive, a restore) is orphaned: its `.git` gitlink
    points at a deleted admin directory."""
    import shutil

    from agentloop.executor import workspace_for
    from tests.test_worktree_vcs import git_write

    repo_root = make_repo(tmp_path / "operator_repo5")
    wt_root = tmp_path / "wt_root5b"

    store = Store(tmp_path / "agentloop.db")
    task = Task(id=None, title="t", goal="g", acceptance_criteria="c")
    task_id = store.add_task(task)
    store.set_status(store.get_task(task_id), TaskStatus.ABORTED)

    config = LoopConfig(
        db_path=str(tmp_path / "agentloop.db"),
        workspace_mode="worktree",
        repo_root=str(repo_root),
        worktree_root=str(wt_root),
        vcs_enabled=True,
    )
    ws = workspace_for(
        config.workspace_root, task_id, create=True, config=config, repo_root=repo_root
    )
    agentloop_admin = repo_root / ".git" / "worktrees" / f"task-{task_id}"
    assert agentloop_admin.is_dir()
    store.set_vcs_repo_pin(str(repo_root), vcs.config_pin(repo_root, repo_root))
    store.close()

    # A worktree the operator made themselves, entirely outside agentloop —
    # a different branch, not under agentloop's branch prefix.
    operator_ws = tmp_path / "operator_manual_worktree"
    added = git_write(
        repo_root,
        "worktree",
        "add",
        str(operator_ws),
        "-b",
        "operator/manual-feature",
    )
    assert added.returncode == 0, added.stderr
    operator_admin = repo_root / ".git" / "worktrees" / "operator_manual_worktree"
    assert operator_admin.is_dir()

    # Both directories become unreachable (deleted, or an unmounted drive).
    shutil.rmtree(ws, ignore_errors=True)
    shutil.rmtree(operator_ws, ignore_errors=True)
    assert agentloop_admin.is_dir()
    assert operator_admin.is_dir()

    cfg_path = _config_file(
        tmp_path,
        workspace_mode="worktree",
        repo_root=str(repo_root),
        worktree_root=str(wt_root),
        vcs_enabled=True,
    )
    rc = main(["--config", cfg_path, "workspace", "prune"])
    assert rc == 0

    assert not agentloop_admin.exists(), "agentloop's own stale entry must be cleared"
    assert operator_admin.is_dir(), (
        "the operator's own unrelated worktree registration must survive "
        "agentloop's prune — git worktree prune is repo-wide and must not "
        "be run unscoped"
    )
    listing = git(repo_root, "worktree", "list").stdout
    assert "operator/manual-feature" in listing or "operator_manual_worktree" in listing


# -- `agentloop workspace rebless` --------------------------------------------


def test_rebless_clears_config_changed_and_the_next_task_can_proceed(tmp_path):
    repo_root = make_repo(tmp_path / "operator_repo3")
    config = LoopConfig(
        workspace_mode="worktree",
        repo_root=str(repo_root),
        worktree_root=str(tmp_path / "wt_root3"),
        vcs_enabled=True,
    )

    store = Store(tmp_path / "agentloop.db")
    # Establish a baseline, the way the first task on this repo would.
    old_pin = vcs.config_pin(repo_root, repo_root)
    store.set_vcs_repo_pin(str(repo_root), old_pin)

    # A legitimate operator edit to .git/config.
    subprocess.run(
        ["git", "config", "--local", "core.autocrlf", "false"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
    )
    new_pin_before_rebless = vcs.config_pin(repo_root, repo_root)
    assert new_pin_before_rebless != old_pin

    # Without re-blessing, the next task refuses.
    from agentloop.executor import workspace_for

    stale_pin = store.vcs_repo_pin(str(repo_root))
    workspace_for(
        config.workspace_root,
        1,
        create=True,
        config=config,
        repo_root=repo_root,
        pin=stale_pin,
    )
    admin_before = repo_root / ".git" / "worktrees" / "task-1"
    assert not admin_before.is_dir(), "must have refused under the stale pin"
    store.close()

    cfg_path = _config_file(
        tmp_path,
        workspace_mode="worktree",
        repo_root=str(repo_root),
        worktree_root=str(tmp_path / "wt_root3"),
        vcs_enabled=True,
    )
    rc = main(["--config", cfg_path, "workspace", "rebless", "--note", "operator edit"])
    assert rc == 0

    store2 = Store(tmp_path / "agentloop.db")
    assert store2.vcs_repo_pin(str(repo_root)) == new_pin_before_rebless
    reblessed_pin = store2.vcs_repo_pin(str(repo_root))
    store2.close()

    # Now the next task proceeds under the reblessed baseline.
    workspace_for(
        config.workspace_root,
        2,
        create=True,
        config=config,
        repo_root=repo_root,
        pin=reblessed_pin,
    )
    admin_after = repo_root / ".git" / "worktrees" / "task-2"
    assert admin_after.is_dir(), "must succeed under the reblessed baseline"


def test_rebless_when_the_repository_was_never_pinned_behaves_sanely_not_crash(
    tmp_path,
):
    repo_root = make_repo(tmp_path / "operator_repo4")
    cfg_path = _config_file(
        tmp_path,
        workspace_mode="worktree",
        repo_root=str(repo_root),
        worktree_root=str(tmp_path / "wt_root4"),
        vcs_enabled=True,
    )
    rc = main(["--config", cfg_path, "workspace", "rebless"])
    assert rc == 0

    store = Store(tmp_path / "agentloop.db")
    recorded = store.vcs_repo_pin(str(repo_root))
    store.close()
    assert recorded == vcs.config_pin(repo_root, repo_root)
    assert recorded != ""


def test_rebless_against_a_repo_root_with_no_git_at_all_is_a_clean_error(
    tmp_path, capsys
):
    not_a_repo = tmp_path / "not_a_repo"
    not_a_repo.mkdir()
    cfg_path = _config_file(
        tmp_path,
        workspace_mode="worktree",
        repo_root=str(not_a_repo),
        worktree_root=str(tmp_path / "wt_root5"),
        vcs_enabled=True,
    )
    rc = main(["--config", cfg_path, "workspace", "rebless"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
