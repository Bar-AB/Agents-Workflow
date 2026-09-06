"""Slice 9 P2 remediation - the MEDIUM findings, all inside P2's surface.

Each test names the finding it pins. They share the worktree fixtures rather
than re-deriving them; a `.git` inside an operator repository is the subject in
every case, so a mocked git could not exhibit any of it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentloop import vcs
from agentloop.config import LoopConfig
from tests.test_worktree_vcs import git, git_write, make_repo


@pytest.fixture()
def repo_root(tmp_path) -> Path:
    return make_repo(tmp_path / "operator_repo")


@pytest.fixture()
def ws_root(tmp_path) -> Path:
    root = tmp_path / "wt_root"
    root.mkdir()
    return root


@pytest.fixture()
def config(ws_root) -> LoopConfig:
    return LoopConfig(workspace_root=str(ws_root), vcs_enabled=True)


# -- M-b: silence must never mean "nothing appeared" -------------------------


def test_working_tree_state_reports_that_it_truncated(repo_root, ws_root, config):
    """M-b. The H3 detection diffs two lists silently capped at 50. Over a real
    checkout more than 50 changed paths is ordinary, and the detection went
    quietly blind: the 51st path onward could never appear in `appeared`.

    The cap stays - telemetry must not flood an event payload - but it now
    says so, and the caller carries that into the payload."""
    ws = ws_root / "task-1"
    assert vcs.init_repo(ws, config, repo_root=str(repo_root), task_id=1).ok
    pin = vcs.config_pin(ws, repo_root)

    under = vcs.working_tree_state(
        ws, config, pin=pin, repo_root=str(repo_root), task_id=1
    )
    assert under.ok is True
    assert under.truncated == ()

    for i in range(60):
        (ws / f"f{i:03d}.txt").write_text("x\n", encoding="utf-8")
    over = vcs.working_tree_state(
        ws, config, pin=pin, repo_root=str(repo_root), task_id=1
    )
    assert over.ok is True
    assert len(over.changed_entries) == 50
    assert over.truncated == ("changed_entries",)


# -- M-c: an overwrite of an already-dirty path is a write too ---------------


def test_working_tree_state_carries_the_status_code_not_just_the_path(
    repo_root, ws_root, config
):
    """M-c. H3 compared path *names* only, so a validator overwriting a path
    the worker had already left dirty was invisible: `?? x` -> ` M x` and
    ` M x` -> `MM x` are both real writes with an unchanged path."""
    ws = ws_root / "task-1"
    assert vcs.init_repo(ws, config, repo_root=str(repo_root), task_id=1).ok
    pin = vcs.config_pin(ws, repo_root)

    (ws / "README.md").write_text("worker edit\n", encoding="utf-8")
    before = vcs.working_tree_state(
        ws, config, pin=pin, repo_root=str(repo_root), task_id=1
    )
    assert before.changed_entries == (" M README.md",)

    assert git_write(ws, "add", "README.md").returncode == 0
    (ws / "README.md").write_text("validator edit\n", encoding="utf-8")
    after = vcs.working_tree_state(
        ws, config, pin=pin, repo_root=str(repo_root), task_id=1
    )
    assert after.changed_entries == ("MM README.md",)
    # The entry changed while the *path* did not - which is exactly what a
    # path-only diff could not see.
    assert [e for e in after.changed_entries if e not in before.changed_entries] == [
        "MM README.md"
    ]


# -- M-e: the operator's own submodules are not "unrecoverable" --------------


def test_submodules_are_not_reported_as_unrecoverable_nested_repos(
    tmp_path, ws_root, config
):
    """M-e. A submodule checkout has a `.git` **file**, so `_nested_repos`
    reported every one of the operator's tracked gitlinks on every rollback.
    An audit log naming recoverable work as destroyed is the mirror of the
    overclaim `unrecoverable_nested_repos` exists to prevent."""
    inner = make_repo(tmp_path / "inner", {"lib.py": "x = 1\n"})
    outer = make_repo(tmp_path / "outer")
    added = git_write(
        outer,
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        str(inner),
        "vendor",
    )
    if added.returncode != 0:
        pytest.skip(f"this git refuses a local-path submodule: {added.stderr[:200]}")
    assert git_write(outer, "commit", "-q", "-m", "add submodule").returncode == 0
    assert (outer / "vendor" / ".git").exists()

    ws = ws_root / "task-1"
    result = vcs.init_repo(ws, config, repo_root=str(outer), task_id=1)
    assert result.ok is True
    assert (
        git_write(
            ws, "-c", "protocol.file.allow=always", "submodule", "update", "--init"
        ).returncode
        == 0
    )
    assert (ws / "vendor" / ".git").exists()

    assert vcs._nested_repos(ws, Path(outer)) == ()

    # A worker's *own* `git init` in the workspace is still reported: that one
    # really is outside the recovery surface.
    (ws / "scratch").mkdir()
    assert git_write(ws / "scratch", "init", "-q").returncode == 0
    assert vcs._nested_repos(ws, Path(outer)) == ("scratch",)


# -- M-f: an unbounded list must not reach an event payload ------------------


def test_nested_repos_is_capped_and_says_when_it_capped(tmp_path, ws_root, config):
    """M-f. `_porcelain_paths` is capped for exactly this reason and its
    sibling was not."""
    repo = make_repo(tmp_path / "many")
    ws = ws_root / "task-1"
    assert vcs.init_repo(ws, config, repo_root=str(repo), task_id=1).ok
    for i in range(vcs._MAX_REPORTED_PATHS + 5):
        d = ws / f"nested{i:03d}"
        d.mkdir()
        assert git_write(d, "init", "-q").returncode == 0

    found = vcs._nested_repos(ws, Path(repo))
    assert len(found) == vcs._MAX_REPORTED_PATHS

    rolled = vcs.rollback(
        ws,
        vcs.base_ref(1),
        config,
        pin=vcs.config_pin(ws, repo),
        repo_root=str(repo),
        task_id=1,
    )
    assert len(rolled.nested_repos) == vcs._MAX_REPORTED_PATHS
    assert "nested_repos" in rolled.truncated


# -- M-h: a shared default prefix is the collision the namespacing prevents --


def test_write_discarded_ref_requires_its_prefix():
    """M-h. The cross-task collision the per-task namespace exists to prevent
    was one forgotten argument away, in a function whose silent-overwrite
    failure mode its own comment calls unreportable."""
    import inspect

    params = inspect.signature(vcs._write_discarded_ref).parameters
    assert params["prefix"].default is inspect.Parameter.empty


# -- areas the review never reached ------------------------------------------


def test_remove_worktree_against_a_stale_admin_entry(repo_root, ws_root, config):
    """Never measured, though it is the exact state `remove_worktree`'s
    docstring says it exists to prevent: the directory is gone and the admin
    entry under `<repo_root>/.git/worktrees/<name>` is still registered.

    What it does is *refuse*, not repair, and that is the correct shape rather
    than a gap: with `<ws>` deleted the guard cannot establish anything about
    the workspace, and a guard that cannot answer says no. The reason it gives
    is `no-workspace`, which names the missing directory instead of blaming a
    git that is installed - so the caller's fallback (`clear_workspace`) is
    reached with an accurate diagnosis. The stale entry survives, and the
    operator-facing repair is `git worktree prune`, which is what P3's
    `agentloop workspace prune` will run."""
    import shutil

    ws = ws_root / "task-1"
    result = vcs.init_repo(ws, config, repo_root=str(repo_root), task_id=1)
    assert result.ok is True
    admin = repo_root / ".git" / "worktrees" / "task-1"
    assert admin.is_dir()

    # The failure mode the docstring names: an `rmtree` alone.
    shutil.rmtree(ws, ignore_errors=True)
    assert admin.is_dir()  # the entry outlived the directory
    assert "task-1" in git(repo_root, "worktree", "list").stdout

    stale = vcs.remove_worktree(
        ws, config, pin=result.pin, repo_root=str(repo_root), task_id=1
    )
    assert stale.ok is False
    assert stale.reason == "no-workspace"
    # Totality: still a JSON-encodable result, never a raise.
    assert isinstance(stale.stderr, str)

    # And the stale entry is still there, which is the honest state: this
    # module refused rather than pruning behind the guard's back.
    assert admin.is_dir()

    # A *live* worktree removes cleanly and leaves nothing registered, which is
    # the control that keeps the assertion above from passing for any reason.
    ws2 = ws_root / "task-2"
    assert vcs.init_repo(
        ws2, config, pin=result.pin, repo_root=str(repo_root), task_id=2
    ).ok
    gone = vcs.remove_worktree(
        ws2, config, pin=result.pin, repo_root=str(repo_root), task_id=2
    )
    assert gone.ok is True, gone
    assert (repo_root / ".git" / "worktrees" / "task-2").exists() is False
    assert "task-2" not in git(repo_root, "worktree", "list").stdout


def test_concurrent_init_repo_on_one_repository_degrades_to_a_closed_reason(
    repo_root, ws_root, config
):
    """Cross-workspace-same-repo contention is new in worktree mode: two tasks'
    `init_repo` race on one `<repo_root>/.git/index.lock`.

    The bar here is deliberately the module's own contract rather than "no
    contention": every entry point is **total**, so what must hold is that a
    loser gets a `VcsResult` with a reason from the closed vocabulary and never
    raises, and that a winner is genuinely correct rather than half-created.
    Whether the loser is retried is `loop._with_retry`'s decision (P3), not
    this module's.

    **Measured, and reported rather than dressed up: contention did not
    reproduce.** Three trials of eight concurrent `init_repo` calls on one
    repository all returned `ok=True` with an empty reason - `git worktree add`
    at distinct paths did not collide on `<repo_root>/.git/index.lock` on this
    platform and git version. So what this test currently pins is the
    *contract* (totality, closed vocabulary, no `ok=True` with a degraded
    reason, every winner correctly initialised) and **not** a demonstrated
    lock-loss path. Stated here because a test named for concurrency that never
    saw any would otherwise read as coverage it does not have."""
    import threading

    first = vcs.init_repo(
        ws_root / "task-0", config, repo_root=str(repo_root), task_id=0
    )
    assert first.ok is True
    baseline = first.pin

    results: dict[int, vcs.VcsResult] = {}
    start = threading.Barrier(6)

    def run(i: int) -> None:
        start.wait()
        results[i] = vcs.init_repo(
            ws_root / f"task-{i}",
            config,
            pin=baseline,
            repo_root=str(repo_root),
            task_id=i,
        )

    threads = [threading.Thread(target=run, args=(i,)) for i in range(1, 7)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert len(results) == 6

    allowed = {"", "git-failed", "timeout", "no-workspace", "not-a-workspace-repo"}
    for i, r in results.items():
        assert r.reason in allowed, (i, r)
        assert isinstance(r, vcs.VcsResult)
        # `ok=True` with a degraded reason is the one combination the
        # vocabulary forbids.
        assert not (r.ok and r.reason not in ("", "already")), (i, r)

    # Every winner is a real, guarded worktree on its own branch with its own
    # base ref - a half-created one would be worse than a refusal.
    winners = [i for i, r in results.items() if r.ok]
    assert winners, results
    for i in winners:
        ws = ws_root / f"task-{i}"
        assert vcs._guard(ws, config, repo_root=str(repo_root), task_id=i) is True
        assert git(ws, "rev-parse", "--verify", vcs.base_ref(i)).returncode == 0


def test_the_validator_write_helper_is_visible_to_the_ast_taint_walker():
    """M-a. The AST guard's taint is per-function and keyed on the `_vcs_`
    prefix (`test_vcs_loop._mentions_vcs_call`), so `_detect_validator_writes`
    handed its caller an **untainted** `before`, and a future status write
    branching on it would not have been caught.

    The rename is invisible to the existing guard *today* - nothing branches on
    it yet - so this test pins the mechanism directly rather than waiting for
    the defect it prevents. [NEUTER] by renaming the method back."""
    import ast

    from agentloop import loop as loop_module
    from tests.test_vcs_loop import _mentions_vcs_call

    tree = ast.parse(Path(loop_module.__file__).read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr.endswith("detect_validator_writes")
    ]
    assert calls, "nothing calls the validator-write detection"
    # The call expression **on its own**, not the method that contains it:
    # `run_task` holds four other `vcs.*` calls, so asking about the enclosing
    # function passes whatever this one is named - measured, and it is why the
    # first version of this test was hollow.
    for call in calls:
        assert _mentions_vcs_call(ast.Expr(value=call)) is True, ast.dump(call.func)
