"""Slice 9 P2 - per-task ref namespacing, rollback and worktree teardown.

P1 of the plan's probes: worktrees share **one** ref namespace, so the fixed
`refs/agentloop/base` that cannot collide when every task owns its own
repository collides on every task once they are worktrees of one repo. These
tests pin the namespacing, and pin the two properties a worktree rollback must
have that a scratch rollback does not: it returns to the *starting commit*
(never an empty tree, which would delete the operator's checkout on the task
branch) and it leaves the main repository byte-identical.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentloop import vcs

from agentloop.config import LoopConfig
from tests.test_worktree_vcs import git, git_write, make_config, make_repo


# Fixtures are defined here rather than imported: pytest resolves them by name
# and ruff reads an imported-then-shadowed fixture as a redefinition.


@pytest.fixture()
def repo_root(tmp_path) -> Path:
    return make_repo(tmp_path / "operator_repo")


@pytest.fixture()
def ws_root(tmp_path) -> Path:
    """Worktrees live OUTSIDE the repository (plan, Decisions)."""
    root = tmp_path / "wt_root"
    root.mkdir()
    return root


@pytest.fixture()
def config(ws_root) -> LoopConfig:
    return make_config(ws_root)


def _init(ws: Path, repo: Path, config, task_id: int):
    result = vcs.init_repo(ws, config, repo_root=str(repo), task_id=task_id)
    assert result.ok is True, result
    return result


def main_repo_state(repo: Path) -> dict:
    """The whole observable state of the operator's repository, for a
    differential: HEAD, current branch, and every tracked file's bytes."""
    files = {
        str(p.relative_to(repo)).replace("\\", "/"): p.read_bytes()
        for p in repo.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(repo).parts
    }
    return {
        "head": git(repo, "rev-parse", "HEAD").stdout.strip(),
        "branch": git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(),
        "status": git(repo, "status", "--porcelain", "-uall").stdout,
        "files": files,
    }


# -- 9. per-task refs --------------------------------------------------------


def test_two_tasks_base_refs_do_not_collide(repo_root, ws_root, config):
    """Test 9 [RED-FIRST]. Probe 1, and the defect a fixed ref name produces:
    task 2's `init_repo` would overwrite task 1's base, so task 1's rollback
    would target task 2's starting commit."""
    ws1 = ws_root / "task-1"
    ws2 = ws_root / "task-2"
    r1 = _init(ws1, repo_root, config, 1)

    # Move the repo on before task 2 starts, so the two starting commits are
    # genuinely different shas.
    (repo_root / "second.txt").write_text("later work\n", encoding="utf-8")
    assert git_write(repo_root, "add", "-A").returncode == 0
    assert git_write(repo_root, "commit", "-q", "-m", "second").returncode == 0
    r2 = _init(ws2, repo_root, config, 2)

    assert r1.sha != r2.sha
    assert vcs.base_ref(1) == "refs/agentloop/task-1/base"
    assert vcs.base_ref(2) == "refs/agentloop/task-2/base"
    assert vcs.base_ref() == vcs.BASE_REF  # scratch mode keeps the constant
    assert git(repo_root, "rev-parse", vcs.base_ref(1)).stdout.strip() == r1.sha
    assert git(repo_root, "rev-parse", vcs.base_ref(2)).stdout.strip() == r2.sha

    # ... and each task rolls back to its own base.
    (ws1 / "one.txt").write_text("task 1 work\n", encoding="utf-8")
    (ws2 / "two.txt").write_text("task 2 work\n", encoding="utf-8")
    assert vcs.commit(
        ws1, "round 1", config, pin=r1.pin, repo_root=str(repo_root), task_id=1
    ).ok
    assert vcs.commit(
        ws2, "round 1", config, pin=r2.pin, repo_root=str(repo_root), task_id=2
    ).ok

    back1 = vcs.rollback(
        ws1, vcs.base_ref(1), config, pin=r1.pin, repo_root=str(repo_root), task_id=1
    )
    assert back1.ok is True, back1
    assert git(ws1, "rev-parse", "HEAD").stdout.strip() == r1.sha
    assert not (ws1 / "one.txt").exists()
    # Task 2 is untouched by task 1's rollback.
    assert (ws2 / "two.txt").exists()
    assert git(ws2, "rev-parse", vcs.base_ref(2)).stdout.strip() == r2.sha


def test_approved_and_discarded_refs_are_per_task(repo_root, ws_root, config):
    """The same namespacing for the other two refs: `mark_approved` on task 1
    must not move task 2's bookmark."""
    ws1 = ws_root / "task-1"
    ws2 = ws_root / "task-2"
    r1 = _init(ws1, repo_root, config, 1)
    r2 = _init(ws2, repo_root, config, 2)
    (ws1 / "one.txt").write_text("x", encoding="utf-8")
    assert vcs.commit(
        ws1, "round 1", config, pin=r1.pin, repo_root=str(repo_root), task_id=1
    ).ok

    approved = vcs.mark_approved(
        ws1, config, pin=r1.pin, repo_root=str(repo_root), task_id=1
    )
    assert approved.ok is True
    assert vcs.approved_ref(1) == "refs/agentloop/task-1/approved"
    assert vcs.approved_ref() == vcs.APPROVED_REF
    assert git(repo_root, "rev-parse", vcs.approved_ref(1)).stdout.strip() == (
        approved.sha
    )
    assert git(repo_root, "rev-parse", "--verify", vcs.approved_ref(2)).returncode != 0
    assert r2.sha  # task 2 exists and was never touched


def test_a_worktree_call_without_a_task_id_refuses(repo_root, ws_root, config):
    """Fail closed rather than silently falling back to the shared scratch
    refs, which is exactly the collision probe 1 measured."""
    ws = ws_root / "task-1"
    result = vcs.init_repo(ws, config, repo_root=str(repo_root))
    assert result.ok is False
    assert result.reason == "no-task-id"
    assert not (ws / ".git").exists()


# -- 10-12. rollback ---------------------------------------------------------


def test_rollback_leaves_the_main_repository_byte_identical(repo_root, ws_root, config):
    """Test 10. A differential over the whole main-repo state: the worktree is
    a real branch of the operator's repository, so a rollback that touched
    their checkout would be destroying work no human asked to discard."""
    ws = ws_root / "task-1"
    result = _init(ws, repo_root, config, 1)
    before = main_repo_state(repo_root)

    (ws / "worker.py").write_text("print('work')\n", encoding="utf-8")
    assert vcs.commit(
        ws, "round 1", config, pin=result.pin, repo_root=str(repo_root), task_id=1
    ).ok
    rolled = vcs.rollback(
        ws,
        vcs.base_ref(1),
        config,
        pin=result.pin,
        repo_root=str(repo_root),
        task_id=1,
    )
    assert rolled.ok is True, rolled

    assert main_repo_state(repo_root) == before


def test_rollback_returns_to_the_starting_commit_not_an_empty_tree(
    repo_root, ws_root, config
):
    """Test 11 [NEUTER]. The single most dangerous difference from scratch
    mode. Scratch base is deliberately an *empty* commit; a worktree rolled
    back to an empty tree would delete the operator's entire checkout on the
    task branch."""
    ws = ws_root / "task-1"
    result = _init(ws, repo_root, config, 1)
    start = git(repo_root, "rev-parse", "HEAD").stdout.strip()
    assert result.sha == start
    tracked = (ws / "README.md").read_text(encoding="utf-8")

    (ws / "worker.py").write_text("print('work')\n", encoding="utf-8")
    (ws / "README.md").write_text("the worker rewrote this\n", encoding="utf-8")
    assert vcs.commit(
        ws, "round 1", config, pin=result.pin, repo_root=str(repo_root), task_id=1
    ).ok

    rolled = vcs.rollback(
        ws,
        vcs.base_ref(1),
        config,
        pin=result.pin,
        repo_root=str(repo_root),
        task_id=1,
    )
    assert rolled.ok is True, rolled
    # The checkout survives, at its starting content.
    assert (ws / "README.md").read_text(encoding="utf-8") == tracked
    assert not (ws / "worker.py").exists()
    assert git(ws, "rev-parse", "HEAD").stdout.strip() == start


def test_the_discarded_round_stays_reachable(repo_root, ws_root, config):
    """Test 12. Reachability (`git log --all`), never `git show`: an orphaned
    commit resolves perfectly well under `show`, which is how an acceptance
    test once passed against an implementation that had already destroyed the
    property."""
    ws = ws_root / "task-1"
    result = _init(ws, repo_root, config, 1)
    (ws / "worker.py").write_text("print('work')\n", encoding="utf-8")
    round1 = vcs.commit(
        ws, "round 1", config, pin=result.pin, repo_root=str(repo_root), task_id=1
    )
    assert round1.ok is True

    rolled = vcs.rollback(
        ws,
        vcs.base_ref(1),
        config,
        pin=result.pin,
        repo_root=str(repo_root),
        task_id=1,
    )
    assert rolled.ok is True
    assert rolled.sha == round1.sha

    ref = f"{vcs.discarded_ref_prefix(1)}/{round1.sha}"
    assert ref == f"refs/agentloop/task-1/discarded/{round1.sha}"
    reachable = git(repo_root, "log", "--all", "--format=%H").stdout.split()
    assert round1.sha in reachable
    assert git(repo_root, "rev-parse", ref).stdout.strip() == round1.sha
    assert git(repo_root, "show", f"{ref}:worker.py").stdout.strip() == "print('work')"


# -- 13. ignored files -------------------------------------------------------


def test_ignored_files_stay_out_and_the_result_says_they_are_unrecoverable(
    tmp_path, ws_root, config
):
    """Test 13. `-f` is dropped in worktree mode: force-adding ignored files
    is what makes a *scratch* rollback fully recoverable, but here it would
    commit `node_modules` into the task branch every round. The cost is a real
    contract change, so the result names it rather than letting the audit log
    assert a recovery surface that does not hold the work."""
    repo = make_repo(
        tmp_path / "operator_repo",
        {"README.md": "x\n", ".gitignore": "node_modules/\n"},
    )
    ws = ws_root / "task-1"
    result = _init(ws, repo, config, 1)

    (ws / "node_modules").mkdir()
    (ws / "node_modules" / "big.js").write_text("441 MB of this\n", encoding="utf-8")
    (ws / "worker.py").write_text("print('work')\n", encoding="utf-8")
    round1 = vcs.commit(
        ws, "round 1", config, pin=result.pin, repo_root=str(repo), task_id=1
    )
    assert round1.ok is True

    carried = git(ws, "ls-tree", "-r", "--name-only", round1.sha).stdout.split()
    assert "worker.py" in carried
    assert "node_modules/big.js" not in carried

    rolled = vcs.rollback(
        ws, vcs.base_ref(1), config, pin=result.pin, repo_root=str(repo), task_id=1
    )
    assert rolled.ok is True
    assert any(
        path.startswith("node_modules") for path in rolled.ignored_unrecoverable
    ), rolled.ignored_unrecoverable
    # ... and the claim is true: the clean really did delete them.
    assert not (ws / "node_modules").exists()
    # A healthy worktree rollback is not a degradation: the operator's tracked
    # files surviving the reset is the contract, not residue.
    assert rolled.reason == ""
    assert (ws / "README.md").exists()


# -- 14. teardown -----------------------------------------------------------


def test_remove_worktree_leaves_no_stale_entry(repo_root, ws_root, config):
    """Test 14 [RED-FIRST]. An `rmtree` alone leaves a stale admin entry and
    `git worktree list` keeps reporting a workspace that is gone."""
    ws = ws_root / "task-1"
    result = _init(ws, repo_root, config, 1)
    assert ws.as_posix() in git(repo_root, "worktree", "list").stdout

    removed = vcs.remove_worktree(
        ws, config, pin=result.pin, repo_root=str(repo_root), task_id=1
    )
    assert removed.ok is True, removed
    assert not ws.exists()
    listing = git(repo_root, "worktree", "list", "--porcelain").stdout
    assert "task-1" not in listing
    assert git(repo_root, "worktree", "list").returncode == 0
    # The branch outlives its worktree: a pruned task is still mergeable.
    assert git(repo_root, "rev-parse", "--verify", "agentloop/task-1").returncode == 0


# -- the relative-path lifecycle --------------------------------------------


def test_the_whole_lifecycle_works_from_a_relative_workspace_root(
    tmp_path, monkeypatch
):
    """Slice 8's `vcs` critical, and P1's own review finding, hid because every
    fixture used an absolute `tmp_path`. Not a third time: this drives init ->
    commit -> approve -> rollback -> remove with a **relative** repo_root and
    workspace, from a cwd inside `tmp_path`."""
    make_repo(tmp_path / "operator_repo")
    (tmp_path / "wt_root").mkdir()
    monkeypatch.chdir(tmp_path)
    repo = Path("operator_repo")
    ws = Path("wt_root") / "task-1"
    cfg = make_config(Path("wt_root"))

    result = vcs.init_repo(ws, cfg, repo_root=str(repo), task_id=1)
    assert result.ok is True, result
    assert (ws / "README.md").exists()

    (ws / "worker.py").write_text("print('work')\n", encoding="utf-8")
    round1 = vcs.commit(
        ws, "round 1", cfg, pin=result.pin, repo_root=str(repo), task_id=1
    )
    assert round1.ok is True, round1
    approved = vcs.mark_approved(
        ws, cfg, pin=result.pin, repo_root=str(repo), task_id=1
    )
    assert approved.ok is True, approved

    rolled = vcs.rollback(
        ws, vcs.base_ref(1), cfg, pin=result.pin, repo_root=str(repo), task_id=1
    )
    assert rolled.ok is True, rolled
    assert not (ws / "worker.py").exists()
    assert round1.sha in git(repo, "log", "--all", "--format=%H").stdout.split()

    removed = vcs.remove_worktree(
        ws, cfg, pin=result.pin, repo_root=str(repo), task_id=1
    )
    assert removed.ok is True, removed
    assert "task-1" not in git(repo, "worktree", "list", "--porcelain").stdout


@pytest.mark.parametrize("task_id", [1, 42])
def test_ref_helpers_are_pure_functions_of_the_task_id(task_id):
    assert vcs.base_ref(task_id) == f"refs/agentloop/task-{task_id}/base"
    assert vcs.approved_ref(task_id) == f"refs/agentloop/task-{task_id}/approved"
    assert (
        vcs.discarded_ref_prefix(task_id) == f"refs/agentloop/task-{task_id}/discarded"
    )
    assert vcs.discarded_ref_prefix() == vcs.DISCARDED_REF_PREFIX
