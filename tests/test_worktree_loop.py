"""Slice 9 P4 - the loop wired to worktree mode. Tests 15-22 of the plan.

Real `git` subprocesses (via `vcs.py`, through `Loop.run_task`), a scripted
`MockRunner` for the model calls (the project's standard loop seam) and, for
tests 15-17, a real `pytest` subprocess against a real tracked test file - the
tests gate is the one thing this slice exists to turn back on.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agentloop import vcs
from agentloop.config import LoopConfig
from agentloop.executor import workspace_for
from agentloop.loop import Loop
from agentloop.models import Task, TaskStatus
from agentloop.registry import Registry
from agentloop.runner import MockRunner
from agentloop.store import Store
from tests.test_loop import APPROVE
from tests.test_worktree_vcs import git, git_write, make_repo


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def add_task(store, risk=1) -> Task:
    task = Task(
        id=None,
        title="Add slugify util",
        goal="Write a slugify(text) function.",
        acceptance_criteria="Lowercase, hyphen-separated, tested.",
        risk_level=risk,
    )
    store.add_task(task)
    return task


def make_worktree_loop(store, repo_root, wt_root, outputs, **overrides) -> tuple:
    overrides.setdefault("allow_test_exec", False)
    config = LoopConfig(
        db_path=store.db_path,
        workspace_mode="worktree",
        repo_root=str(repo_root),
        worktree_root=str(wt_root),
        vcs_enabled=True,
        **overrides,
    )
    runner = MockRunner(outputs)
    return Loop(store, runner, Registry.load(), config), runner


def ws_of(loop, task_id) -> Path:
    return workspace_for(
        loop.config.workspace_root,
        task_id,
        config=loop.config,
        repo_root=Path(loop.config.repo_root).resolve(),
    )


class WritingRunner(MockRunner):
    """A `MockRunner` that also writes a worker file into the workspace on the
    first (worker) call - `MockRunner` executes no tools, and an unmodified
    checkout proves nothing about what the worker did to it."""

    def __init__(self, outputs, ws: Path, files: dict[str, str]):
        super().__init__(outputs)
        self._ws = ws
        self._files = files
        self._written = False

    def run(self, *args, **kw):
        if not self._written:
            self._written = True
            for name, body in self._files.items():
                target = self._ws / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(body, encoding="utf-8")
        return super().run(*args, **kw)


PASSING = "def test_ok():\n    assert True\n"
FAILING = "def test_bad():\n    assert False\n"


# -- test 15: the workspace starts with the repo's tracked files -------------


def test_worker_workspace_starts_with_the_repos_tracked_files(store, tmp_path):
    """[RED-FIRST] The whole slice in one assertion. Against the pre-P4 tree
    (loop.py never passing repo_root) this workspace is the pre-slice-9 blank
    scratch directory and `README.md` is not in it."""
    repo_root = make_repo(
        tmp_path / "operator_repo", {"README.md": "the operator's code\n"}
    )
    task = add_task(store)
    loop, _ = make_worktree_loop(
        store, repo_root, tmp_path / "wt_root", ["out", APPROVE]
    )

    loop.run_task(task)

    ws = ws_of(loop, task.id)
    assert (ws / "README.md").read_text(encoding="utf-8") == "the operator's code\n"
    assert task.status == TaskStatus.DONE


# -- test 16: the real suite gates approval -----------------------------------


def test_a_failing_real_suite_in_the_worktree_blocks_approval(store, tmp_path):
    repo_root = make_repo(tmp_path / "operator_repo", {"test_bad.py": FAILING})
    task = add_task(store)
    loop, _ = make_worktree_loop(
        store,
        repo_root,
        tmp_path / "wt_root",
        ["out", APPROVE, "out2", APPROVE],
        allow_test_exec=True,
        test_command=f"{sys.executable} -m pytest -q",
        max_revisions=1,
    )

    loop.run_task(task)

    assert task.status == TaskStatus.NEEDS_HUMAN
    assert task.revision_count == 1
    runs = store.test_runs(task.id)
    assert runs and runs[0]["status"] == "fail"


def test_a_passing_real_suite_in_the_worktree_allows_approval(store, tmp_path):
    repo_root = make_repo(tmp_path / "operator_repo", {"test_ok.py": PASSING})
    task = add_task(store)
    loop, _ = make_worktree_loop(
        store,
        repo_root,
        tmp_path / "wt_root",
        ["out", APPROVE],
        allow_test_exec=True,
        test_command=f"{sys.executable} -m pytest -q",
    )

    loop.run_task(task)

    assert task.status == TaskStatus.DONE
    runs = store.test_runs(task.id)
    assert runs and runs[0]["status"] == "pass"


# -- test 17: approval leaves a mergeable branch with only the worker's diff --


def test_an_approved_task_leaves_a_mergeable_branch_with_only_the_workers_diff(
    store, tmp_path
):
    repo_root = make_repo(tmp_path / "operator_repo", {"README.md": "unchanged\n"})
    task = add_task(store)
    loop, _ = make_worktree_loop(
        store, repo_root, tmp_path / "wt_root", ["out", APPROVE]
    )
    ws = ws_of(loop, task.id)
    loop.runner = WritingRunner(
        ["out", APPROVE], ws, {"feature.py": "def f():\n    return 1\n"}
    )

    loop.run_task(task)
    assert task.status == TaskStatus.DONE

    branch = f"{loop.config.vcs_branch_prefix}{task.id}"
    merge_base = git(repo_root, "merge-base", "main", branch).stdout.strip()
    assert merge_base
    diff = git(repo_root, "diff", f"{merge_base}...{branch}", "--name-only").stdout
    assert diff.split() == ["feature.py"]
    # Mergeable: an ordinary merge into main succeeds with no conflict.
    merged = git_write(repo_root, "merge", "--no-edit", branch)
    assert merged.returncode == 0, merged.stderr


# -- test 18: human_reject rolls the worktree back; main repo untouched ------


def test_human_reject_rolls_the_worktree_back_leaving_main_repo_untouched(
    store, tmp_path
):
    repo_root = make_repo(tmp_path / "operator_repo")
    main_head_before = git(repo_root, "rev-parse", "HEAD").stdout.strip()
    task = add_task(store)
    loop, _ = make_worktree_loop(
        store, repo_root, tmp_path / "wt_root", ["out", APPROVE]
    )
    ws = ws_of(loop, task.id)
    loop.runner = WritingRunner(["out", APPROVE], ws, {"scratch.py": "x = 1\n"})
    loop.run_task(task)
    assert (ws / "scratch.py").exists()

    loop.human_reject(task.id, note="not this")

    assert store.get_task(task.id).status == TaskStatus.FAILED
    # Rolled back to the branch's starting commit: the worker's file is gone.
    assert not (ws / "scratch.py").exists()
    # The main repository - branch, HEAD, working tree - is untouched.
    assert git(repo_root, "rev-parse", "HEAD").stdout.strip() == main_head_before
    assert git(repo_root, "status", "--porcelain").stdout.strip() == ""


# -- test 19: human_redo removes and recreates; discarded ref kept -----------


def test_human_redo_removes_and_recreates_the_worktree_and_keeps_the_discarded_ref(
    store, tmp_path
):
    repo_root = make_repo(tmp_path / "operator_repo")
    task = add_task(store)
    loop, _ = make_worktree_loop(
        store, repo_root, tmp_path / "wt_root", ["out", APPROVE]
    )
    ws = ws_of(loop, task.id)
    loop.runner = WritingRunner(["out", APPROVE], ws, {"scratch.py": "x = 1\n"})
    loop.run_task(task)
    task.revision_count = 2  # pretend a revision happened, to prove the reset
    tip_before_redo = git(ws, "rev-parse", "HEAD").stdout.strip()

    loop.human_redo(task.id)

    fresh = store.get_task(task.id)
    assert fresh.output == ""
    assert fresh.revision_count == 0
    admin = repo_root / ".git" / "worktrees" / f"task-{task.id}"
    assert admin.is_dir(), "recreated, not left stale"
    ws_after = ws_of(loop, task.id)
    assert ws_after.is_dir()
    assert not (ws_after / "scratch.py").exists(), "a genuinely fresh checkout"
    # The discarded round stays reachable from the main repo's reflog of refs -
    # `git log --all` sees every ref this module writes, including per-task
    # discarded ones, because they all live under refs/agentloop/.
    reachable = git(repo_root, "log", "--all", "--format=%H").stdout.split()
    assert tip_before_redo in reachable


def test_human_redo_twice_does_not_hit_the_branch_collision_p2_deferred(
    store, tmp_path
):
    """The gap P2 flagged and named for P4: `init_repo`'s worktree branch
    always creates with `-b`, so a *second* redo of the same task used to fail
    loudly on the branch `init_repo` made the first time. [RED-FIRST] against
    the pre-fix tree (before `vcs.remove_task_branch` was wired into
    `human_redo`) this raises nothing observable but leaves the second redo's
    worktree missing/stale, because `init_repo`'s `git worktree add -b` fails
    and the loop's own `_vcs_degraded` records it rather than crashing."""
    repo_root = make_repo(tmp_path / "operator_repo")
    task = add_task(store)
    loop, _ = make_worktree_loop(
        store, repo_root, tmp_path / "wt_root", ["out", APPROVE]
    )
    loop.run_task(task)

    loop.human_redo(task.id)
    loop.human_redo(task.id)  # the second redo is the one that used to fail

    ws = ws_of(loop, task.id)
    assert vcs.is_repo(ws, loop.config, repo_root=repo_root, task_id=task.id) is True
    assert (
        len(
            [
                e
                for e in store.events(task.id)
                if e["kind"] == "vcs_unavailable" and e["payload"].get("op") == "init"
            ]
        )
        == 0
    ), "the second redo's init_repo must not have degraded"


# -- P4 remediation 4: a real ref collision must not destroy history ---------


def test_human_redo_does_not_destroy_history_when_the_discarded_ref_write_collides(
    store, tmp_path
):
    """[RED-FIRST] Reproduces the round-4 critical with a real git ref
    collision - no monkeypatching. `_write_discarded_ref` needs to create
    `refs/agentloop/task-<id>/discarded/<sha>`; pre-creating
    `refs/agentloop/task-<id>/discarded` itself as an ordinary ref makes
    git's own hierarchical ref-namespace rules fail that write for real, so
    `vcs.rollback` correctly returns `ok=False` with nothing destructive run.
    `human_redo`'s worktree branch used to ignore that result and proceed to
    `remove_worktree` -> `remove_task_branch` -> `init_repo` anyway, deleting
    the only ref (the task branch) that kept the round's commits reachable -
    permanently, once an ordinary `git gc` ran, since `git show <sha>`
    resolving an orphan proves nothing about survivability.

    `risk=2` deliberately - a task that reaches DONE also gets
    `refs/agentloop/task-<id>/approved`, which would keep the round
    reachable regardless of this bug and be a false negative for exactly this
    defect (per the prompt: "an earlier attempt with an approved task was a
    false negative"). At risk 2 the approve verdict parks the task at
    NEEDS_HUMAN instead, so the task branch is the *only* ref holding the
    round reachable."""
    repo_root = make_repo(tmp_path / "operator_repo")
    main_head = git(repo_root, "rev-parse", "HEAD").stdout.strip()
    task = add_task(store, risk=2)
    loop, _ = make_worktree_loop(
        store, repo_root, tmp_path / "wt_root", ["out", APPROVE]
    )
    ws = ws_of(loop, task.id)
    loop.runner = WritingRunner(["out", APPROVE], ws, {"scratch.py": "x = 1\n"})
    loop.run_task(task)
    assert store.get_task(task.id).status == TaskStatus.NEEDS_HUMAN
    tip_before_redo = git(ws, "rev-parse", "HEAD").stdout.strip()
    assert tip_before_redo != main_head, "the round must have its own commit"

    # Pre-create the discarded ref's own path as an ordinary ref (not a
    # directory) - a real git subprocess, not a monkeypatch - so the later
    # `update-ref refs/agentloop/task-<id>/discarded/<sha>` genuinely fails
    # with "... exists; cannot create ... /discarded/<sha>". Pointed at the
    # unrelated main-branch commit, deliberately not at `tip_before_redo` -
    # aiming it at the very commit under test would accidentally keep that
    # commit reachable through the collision ref itself, defeating the whole
    # reproduction.
    collide = vcs.discarded_ref_prefix(task.id)
    ref_write = git(repo_root, "update-ref", collide, main_head)
    assert ref_write.returncode == 0, ref_write.stderr

    loop.human_redo(task.id)

    # The round's commit must stay reachable from *some* ref, not merely
    # resolvable by sha - `git show <sha>` resolves an orphan perfectly well.
    reachable = git(repo_root, "log", "--all", "--format=%H").stdout.split()
    assert tip_before_redo in reachable, (
        "the round's commit became unreachable from every ref"
    )

    # Ordinary maintenance any operator or CI system might run. If the
    # commit above is not ref-reachable, this collects it for real.
    git(repo_root, "reflog", "expire", "--expire=now", "--all")
    git(repo_root, "gc", "--prune=now")
    exists = git(repo_root, "cat-file", "-e", tip_before_redo)
    assert exists.returncode == 0, (
        "the commit object was permanently destroyed by a redo that hit a "
        "real ref-write collision"
    )


# -- test 20: two parallel tasks do not collide -------------------------------


class ParallelWritingRunner:
    """Thread-safe, content-routing (see the slice-6 sibling in
    test_vcs_loop.py) - a `Barrier` proves the two tasks were genuinely in
    flight together, not merely fast."""

    def __init__(self, n_parallel: int):
        import threading

        self.barrier = threading.Barrier(n_parallel, timeout=30)
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def run(self, system_prompt, prompt, model, tools=None, cwd=None):
        if prompt.startswith("# Task under review"):
            from agentloop.runner import RunResult

            return RunResult(output=APPROVE, tokens_in=10, tokens_out=5, model="mock")
        from agentloop.runner import RunResult

        ws = Path(cwd) if cwd else None
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            self.barrier.wait()
            if ws is not None:
                (ws / f"{ws.name}.txt").write_text(
                    f"work of {ws.name}", encoding="utf-8"
                )
            return RunResult(
                output=f"output of {ws.name if ws else '?'}",
                tokens_in=10,
                tokens_out=5,
                model="mock",
            )
        finally:
            with self._lock:
                self.active -= 1


def test_two_parallel_worktree_tasks_do_not_collide(store, tmp_path):
    repo_root = make_repo(tmp_path / "operator_repo")
    a, b = add_task(store), add_task(store)
    runner = ParallelWritingRunner(2)
    config = LoopConfig(
        db_path=store.db_path,
        workspace_mode="worktree",
        repo_root=str(repo_root),
        worktree_root=str(tmp_path / "wt_root"),
        vcs_enabled=True,
        allow_test_exec=False,
        max_parallel_workers=2,
    )
    loop = Loop(store, runner, Registry.load(), config)

    assert loop.run() == 2
    assert runner.max_active == 2, "the two tasks never overlapped"
    for task_id in (a.id, b.id):
        assert store.get_task(task_id).status == TaskStatus.DONE

    ws_a = ws_of(loop, a.id)
    ws_b = ws_of(loop, b.id)
    assert ws_a != ws_b
    assert (ws_a / f"{ws_a.name}.txt").exists()
    assert (ws_b / f"{ws_b.name}.txt").exists()
    assert not (ws_a / f"{ws_b.name}.txt").exists()
    assert not (ws_b / f"{ws_a.name}.txt").exists()
    branch_a = f"{config.vcs_branch_prefix}{a.id}"
    branch_b = f"{config.vcs_branch_prefix}{b.id}"
    head_a = git(repo_root, "rev-parse", branch_a).stdout.strip()
    head_b = git(repo_root, "rev-parse", branch_b).stdout.strip()
    assert head_a and head_b and head_a != head_b


# -- test 21: scratch mode vs pre-slice-9 is a proven whole-state no-op ------


def _sha256_tree(root: Path) -> dict:
    """A pure-Python fingerprint of a directory's tracked-relevant content,
    excluding `.git` - independent of `vcs.py` so it cannot share a bug with
    the code it is checking."""
    import hashlib

    out = {}
    for p in sorted(root.rglob("*")):
        if ".git" in p.relative_to(root).parts:
            continue
        if p.is_file():
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def test_scratch_mode_is_identical_whether_or_not_worktree_knobs_are_set(
    store, tmp_path
):
    """[NEUTER]. Mirrors `test_vcs_loop.py`'s
    `test_vcs_disabled_and_enabled_produce_identical_observable_state`: the
    slice's central claim is that `workspace_mode='scratch'` (the default) is
    a *proven* no-op, so filling in every worktree-mode knob
    (`repo_root`/`worktree_root`/`vcs_base_ref`/`vcs_branch_prefix`) with real,
    reachable values and leaving `workspace_mode` at its default must produce
    the identical run a config with none of those knobs set does."""
    from tests.test_vcs_loop import (
        SCRIPT,
        differing_keys,
        snapshot,
    )

    repo_root = make_repo(tmp_path / "unrelated_operator_repo")

    def run_arm(**cfg):
        task = add_task(store)
        ws_root = tmp_path / f"ws_{task.id}"
        config = LoopConfig(
            db_path=store.db_path,
            workspace_root=str(ws_root),
            allow_test_exec=False,
            vcs_enabled=True,
            **cfg,
        )
        runner = MockRunner(list(SCRIPT))
        loop = Loop(store, runner, Registry.load(), config)
        loop.run_task(task)
        return snapshot(store, task.id, str(ws_root)), task

    bare, bare_task = run_arm()
    filled, filled_task = run_arm(
        workspace_mode="scratch",
        repo_root=str(repo_root),
        worktree_root=str(tmp_path / "some_other_wt_root"),
        vcs_base_ref="HEAD",
        vcs_branch_prefix="agentloop/task-",
    )

    assert differing_keys(filled, bare) == []
    assert filled == bare
    assert filled["status"] == TaskStatus.DONE.value

    # Control (a): the knobs are not simply unread everywhere - flipping
    # `workspace_mode` to 'worktree' with the *same* extra knobs must produce
    # a run that touches the operator repo (branch created), proving the
    # comparison above is a real differential and not two runs that both
    # ignored everything.
    task = add_task(store)
    wt_config = LoopConfig(
        db_path=store.db_path,
        workspace_root=str(tmp_path / f"ws_{task.id}"),
        allow_test_exec=False,
        vcs_enabled=True,
        workspace_mode="worktree",
        repo_root=str(repo_root),
        worktree_root=str(tmp_path / "wt_root_control"),
    )
    wt_loop = Loop(store, MockRunner(list(SCRIPT)), Registry.load(), wt_config)
    wt_loop.run_task(task)
    assert (
        git(repo_root, "branch", "--list", f"agentloop/task-{task.id}").stdout.strip()
        != ""
    ), "the control run must actually have touched the operator repo"

    # Control (b): the filtered vcs_* events were non-empty in the bare arm,
    # matching the sibling differential's own non-vacuity check.
    unfiltered = [e["kind"] for e in store.events(bare_task.id)]
    removed = [k for k in unfiltered if k.startswith("vcs_")]
    assert removed, "nothing vcs-shaped was filtered; the differential is vacuous"


# -- test 22: the AST guard extended to the new call shapes ------------------


def test_ast_guard_still_bites_a_worktree_result_wired_to_status():
    """Slice 9 P4. The guard itself (and its inventory-count control) lives in
    `test_vcs_loop.py`; this pins that a worktree-flavoured violation - a
    `repo_root=`/`task_id=`-carrying call assigned to a name that then reaches
    a status write - is caught by the *same* walker, not a shape the taint
    tracker happens to miss because it looks different from the scratch-mode
    plants already tested there."""
    import ast

    from tests.test_vcs_loop import status_writes_downstream_of_vcs

    planted = ast.parse(
        "def f(self, task, repo_root):\n"
        "    r = vcs.init_repo(ws, self.config, pin, repo_root=repo_root, "
        "task_id=task.id)\n"
        "    if r.ok:\n"
        "        self.store.set_status(task, 'done')\n"
    )
    assert len(status_writes_downstream_of_vcs(planted)) == 1

    planted_helper = ast.parse(
        "def g(self, task, repo_root):\n"
        "    r = self._vcs_rollback_to_base(task.id, repo_root)\n"
        "    self.store.set_status(task, 'failed', reason=r.reason)\n"
    )
    assert len(status_writes_downstream_of_vcs(planted_helper)) == 1
