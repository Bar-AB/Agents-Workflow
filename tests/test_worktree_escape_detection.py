"""Slice 9 P4, residual 2 (tests 27-29): the executor sandbox's documented
`..`/absolute-path escape gets a far worse target under worktree mode - the
operator's real checkout instead of a throwaway directory - mitigated by
putting `worktree_root` outside `repo_root` (reduces, does not close) plus a
`git status` differential that **detects**, never prevents, an out-of-branch
write.

No test in this module may claim the escape is closed; two of the three
(28, and the absolute half of 27) are written to PASS *because the write
succeeds*, with a comment saying so."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agentloop import vcs
from agentloop.config import LoopConfig
from agentloop.executor import TestExecutor, worktree_root_for
from agentloop.loop import Loop
from agentloop.models import Task
from agentloop.registry import Registry
from agentloop.runner import MockRunner
from agentloop.store import Store
from tests.test_loop import APPROVE
from tests.test_worktree_vcs import make_repo


def _write_script(tmp_path: Path) -> Path:
    """A tiny, tracked-nowhere helper: `python escape.py <path> <content>`
    writes `content` to `path` (relative paths are resolved against the
    process's cwd, i.e. the workspace `TestExecutor` sets as `cwd=`)."""
    script = tmp_path / "escape.py"
    script.write_text(
        "import sys\n"
        "with open(sys.argv[1], 'w', encoding='utf-8') as f:\n"
        "    f.write(sys.argv[2])\n",
        encoding="utf-8",
    )
    return script


def _init_worktree_task(config: LoopConfig, ws: Path, repo_root: Path, task_id: int):
    pin = vcs.config_pin(repo_root, repo_root)
    result = vcs.init_repo(
        ws,
        config,
        pin,
        repo_root=repo_root,
        task_id=task_id,
        start_ref="HEAD",
        branch_prefix=config.vcs_branch_prefix,
    )
    assert result.ok, result
    return pin


# -- test 27: measured both ways ----------------------------------------------


def test_relative_traversal_reaches_the_repo_when_worktree_root_is_inside_it(
    tmp_path,
):
    """The **inside** half. Only reachable by bypassing `LoopConfig`'s own
    `__post_init__` refusal (see test 30) — constructed directly against
    `vcs.init_repo`/`TestExecutor`, which know nothing about that config-level
    guard, exactly as test 30's own docstring says: "becomes reachable through
    ordinary config" once the check is removed means the check is the *only*
    thing stopping it, not anything in `vcs.py` or `executor.py`."""
    repo_root = make_repo(tmp_path / "operator_repo")
    script = _write_script(tmp_path)
    config = LoopConfig(
        workspace_root=str(tmp_path / "unused"),
        vcs_enabled=True,
        allow_test_exec=True,
        test_command=f"{sys.executable} {script} ../../PWNED.txt pwned-inside",
    )
    # A workspace two levels *under* repo_root — the layout `__post_init__`
    # refuses when it is `config.worktree_root`, built here without going
    # through that field at all.
    ws = repo_root / "inside_ws" / "task-1"
    _init_worktree_task(config, ws, repo_root, 1)

    result = TestExecutor(command=config.test_command, enabled=True).run(ws)

    assert result.status == "pass", result.summary
    assert (repo_root / "PWNED.txt").read_text(encoding="utf-8") == "pwned-inside"


def test_the_same_relative_traversal_lands_in_worktree_root_when_it_is_outside(
    tmp_path,
):
    """The **outside** half — the shipped layout. Same relative depth
    (`../../`) as the inside case above, so the only variable is where
    `worktree_root` sits; the write must land inside the agentloop-owned
    `worktree_root`, not the operator's repository."""
    repo_root = make_repo(tmp_path / "operator_repo")
    wt_root = tmp_path / "wt_root"
    script = _write_script(tmp_path)
    config = LoopConfig(
        workspace_root=str(tmp_path / "unused"),
        vcs_enabled=True,
        allow_test_exec=True,
        workspace_mode="worktree",
        repo_root=str(repo_root),
        worktree_root=str(wt_root),
        test_command=f"{sys.executable} {script} ../../PWNED.txt pwned-outside",
    )
    ws = worktree_root_for(config, repo_root) / "task-1"
    _init_worktree_task(config, ws, repo_root, 1)

    result = TestExecutor(command=config.test_command, enabled=True).run(ws)

    assert result.status == "pass", result.summary
    # Landed one level above `<repo-hash-dir>/task-1` — inside `wt_root`,
    # never inside the repository.
    assert (wt_root / "PWNED.txt").exists()
    assert not (repo_root / "PWNED.txt").exists()


# -- test 28: the absolute case is deliberately left open ---------------------


def test_an_absolute_path_write_still_reaches_the_repo_even_with_worktree_root_outside(
    tmp_path,
):
    """[Deliberately uncomfortable] Residual 2 is *reduced*, not closed:
    written to PASS **because the write succeeds** — an absolute path needs no
    traversal and does not care where `worktree_root` is. A later isolation
    slice that closes this should flip this assertion, which is the point of
    writing it this way rather than skipping the case."""
    repo_root = make_repo(tmp_path / "operator_repo")
    wt_root = tmp_path / "wt_root2"
    script = _write_script(tmp_path)
    target = repo_root / "PWNED_ABS.txt"
    config = LoopConfig(
        workspace_root=str(tmp_path / "unused"),
        vcs_enabled=True,
        allow_test_exec=True,
        workspace_mode="worktree",
        repo_root=str(repo_root),
        worktree_root=str(wt_root),
        test_command=f'{sys.executable} {script} "{target}" pwned-absolute',
    )
    ws = worktree_root_for(config, repo_root) / "task-1"
    _init_worktree_task(config, ws, repo_root, 1)

    result = TestExecutor(command=config.test_command, enabled=True).run(ws)

    # The write succeeds — this is the honest, uncomfortable result, not a bug
    # in the test. `worktree_root` outside `repo_root` never claimed to close
    # an absolute-path escape.
    assert result.status == "pass", result.summary
    assert target.read_text(encoding="utf-8") == "pwned-absolute"


# -- test 29: detected, never claimed to be prevented -------------------------


def test_an_out_of_branch_write_is_detected_and_the_event_does_not_claim_prevention(
    store, tmp_path
):
    repo_root = make_repo(tmp_path / "operator_repo")
    wt_root = tmp_path / "wt_root3"
    script = _write_script(tmp_path)
    target = repo_root / "PWNED_DETECT.txt"
    task = Task(
        id=None,
        title="t",
        goal="g",
        acceptance_criteria="c",
    )
    store.add_task(task)
    config = LoopConfig(
        db_path=store.db_path,
        workspace_root=str(tmp_path / "unused"),
        vcs_enabled=True,
        allow_test_exec=True,
        workspace_mode="worktree",
        repo_root=str(repo_root),
        worktree_root=str(wt_root),
        test_command=f'{sys.executable} {script} "{target}" pwned',
        max_revisions=1,
    )
    loop = Loop(store, MockRunner(["out", APPROVE]), Registry.load(), config)

    loop.run_task(task)

    events = [
        e for e in store.events(task.id) if e["kind"] == "worktree_out_of_branch_write"
    ]
    assert len(events) == 1, store.events(task.id)
    payload = events[0]["payload"]
    assert payload["prevented"] is False
    note = payload["note"].lower()
    assert "detect" in note
    assert "nothing stopped or undid the write" in note
    assert target.read_text(encoding="utf-8") == "pwned"  # the write really landed


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()
