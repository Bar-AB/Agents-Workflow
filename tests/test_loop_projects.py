"""Phase 3 of the multi-project dashboard slice: `Loop._worktree_repo_root`
threaded through `Task.project_id`, and project scope on `Loop.run`/`Loop.plan`.

Real `git` subprocesses (via `vcs.py`), a scripted `MockRunner` for the model
calls, matching `test_worktree_loop.py`'s existing seam exactly — no new
seam introduced.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentloop.config import LoopConfig
from agentloop.executor import workspace_for
from agentloop.loop import Loop
from agentloop.models import Task, TaskStatus
from agentloop.registry import Registry
from agentloop.runner import MockRunner
from agentloop.store import Store
from tests.test_loop import APPROVE
from tests.test_worktree_vcs import make_repo


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def add_task(store, project_id=None) -> Task:
    task = Task(
        id=None,
        title="Add slugify util",
        goal="Write a slugify(text) function.",
        acceptance_criteria="Lowercase, hyphen-separated, tested.",
        project_id=project_id,
    )
    store.add_task(task)
    return task


def make_loop(store, outputs, **overrides) -> tuple:
    overrides.setdefault("allow_test_exec", False)
    config = LoopConfig(db_path=store.db_path, vcs_enabled=True, **overrides)
    runner = MockRunner(outputs)
    return Loop(store, runner, Registry.load(), config), runner


def worktree_workspace(loop, task) -> Path:
    repo_root = loop._worktree_repo_root(task)
    return workspace_for(
        loop.config.workspace_root, task.id, config=loop.config, repo_root=repo_root
    )


# -- (a): two worktree-mode projects, one Loop.run() call -----------------


def test_two_worktree_mode_projects_each_resolve_their_own_repo_root(store, tmp_path):
    """[RED-FIRST] Against pre-Phase-3 code, `_worktree_repo_root` ignores
    `task` entirely and both tasks would resolve the SAME (the loop's own
    config) repo_root — this proves each task's workspace holds its OWN
    project's tracked file, not a shared or wrong one."""
    repo_a = make_repo(tmp_path / "repo-a", {"MARKER.txt": "project A\n"})
    repo_b = make_repo(tmp_path / "repo-b", {"MARKER.txt": "project B\n"})

    a = store.create_project("A", str(repo_a), workspace_mode="worktree")
    b = store.create_project("B", str(repo_b), workspace_mode="worktree")

    task_a = add_task(store, project_id=a)
    task_b = add_task(store, project_id=b)

    loop, _ = make_loop(
        store,
        # worker output, validator verdict, per task, in claim order (a then b)
        ["out-a", APPROVE, "out-b", APPROVE],
        worktree_root=str(tmp_path / "wt_root"),
    )

    processed = loop.run()

    assert processed == 2
    assert store.get_task(task_a.id).status == TaskStatus.DONE
    assert store.get_task(task_b.id).status == TaskStatus.DONE

    ws_a = worktree_workspace(loop, store.get_task(task_a.id))
    ws_b = worktree_workspace(loop, store.get_task(task_b.id))
    assert (ws_a / "MARKER.txt").read_text(encoding="utf-8") == "project A\n"
    assert (ws_b / "MARKER.txt").read_text(encoding="utf-8") == "project B\n"
    # [RED-FIRST, CRITICAL regression] content alone can pass while the
    # checkout lands in the WRONG place: executor.workspace_for used to gate
    # its worktree-vs-scratch dispatch on the Loop's own global
    # config.workspace_mode (here: the default "scratch", since this Loop
    # was never told either project's mode) instead of on repo_root is not
    # None -- silently checking out a real worktree under the scratch-mode
    # workspace_root (inside this very repo checkout) rather than under
    # worktree_root, escaping the isolation boundary entirely.
    wt_root = (tmp_path / "wt_root").resolve()
    assert ws_a.resolve().is_relative_to(wt_root), (
        f"project A's workspace {ws_a} escaped worktree_root {wt_root}"
    )
    assert ws_b.resolve().is_relative_to(wt_root), (
        f"project B's workspace {ws_b} escaped worktree_root {wt_root}"
    )


# -- (b): Loop.run(project_id=X) claims only that project's tasks ---------


def test_run_with_project_id_claims_only_that_projects_tasks(store, tmp_path):
    repo_a = make_repo(tmp_path / "repo-a2", {"MARKER.txt": "a\n"})
    repo_b = make_repo(tmp_path / "repo-b2", {"MARKER.txt": "b\n"})
    a = store.create_project("A2", str(repo_a), workspace_mode="worktree")
    b = store.create_project("B2", str(repo_b), workspace_mode="worktree")

    task_a = add_task(store, project_id=a)
    task_b = add_task(store, project_id=b)

    loop, _ = make_loop(
        store, ["out-a", APPROVE], worktree_root=str(tmp_path / "wt_root2")
    )

    processed = loop.run(project_id=a)

    assert processed == 1
    assert store.get_task(task_a.id).status == TaskStatus.DONE
    # Project B's task was never claimed -- still pending.
    assert store.get_task(task_b.id).status == TaskStatus.PENDING


# -- (d) BLOCKING #2 regression: relative repo_root reconciliation --------


def test_default_project_reconciliation_preserves_relative_worktree_repo_root(
    store, tmp_path, monkeypatch
):
    """A relative `repo_root` is a real, supported worktree-mode
    configuration (slice 9) -- the bootstrap reconciliation must resolve it
    to absolute BEFORE storing it, never silently drop `workspace_mode` to
    'scratch' because `repoint_project`'s validation requires an absolute
    path."""
    real_repo = make_repo(tmp_path / "real_repo")
    monkeypatch.chdir(tmp_path)
    relative_repo_root = os.path.relpath(real_repo, tmp_path)

    default_row = store.get_project(store.resolve_project(None))
    assert (default_row["repo_root"], default_row["workspace_mode"]) == (
        ".",
        "scratch",
    )

    config = LoopConfig(
        db_path=store.db_path,
        workspace_mode="worktree",
        repo_root=relative_repo_root,
        worktree_root=str(tmp_path / "wt_root3"),
    )
    Loop(store, MockRunner([]), Registry.load(), config)

    updated = store.get_project(store.resolve_project(None))
    assert updated["workspace_mode"] == "worktree"
    assert updated["repo_root"] == os.path.abspath(relative_repo_root)


def test_default_project_reconciliation_failure_is_visible_not_silent(store, tmp_path):
    """A genuine reconciliation failure (the configured directory doesn't
    exist) must not break loop construction, but must not be silent either
    -- a `config_warning` event names the failure."""
    missing_repo_root = str(tmp_path / "does_not_exist")

    config = LoopConfig(
        db_path=store.db_path,
        workspace_mode="worktree",
        repo_root=missing_repo_root,
        worktree_root=str(tmp_path / "wt_root4"),
    )
    Loop(store, MockRunner([]), Registry.load(), config)  # must not raise

    warnings = [e for e in store.events() if e["kind"] == "config_warning"]
    assert any(
        "resolved_repo_root" in w["payload"]
        and w["payload"]["resolved_repo_root"] == os.path.abspath(missing_repo_root)
        for w in warnings
    )
    # The Default project's row was never corrupted by the failed attempt.
    default_row = store.get_project(store.resolve_project(None))
    assert (default_row["repo_root"], default_row["workspace_mode"]) == (
        ".",
        "scratch",
    )


# -- Default-project placeholder-vs-repointed precedence (review MEDIUM) --


def test_worktree_repo_root_uses_config_while_default_project_is_at_the_placeholder(
    store, tmp_path
):
    """While the Default project's row is still exactly at the bootstrap
    placeholder, `_worktree_repo_root` must defer to `self.config` for a
    task in that project -- not the (uninformative) placeholder row."""
    repo_root = make_repo(tmp_path / "cfg_repo")
    config = LoopConfig(
        db_path=store.db_path,
        workspace_mode="worktree",
        repo_root=str(repo_root),
        worktree_root=str(tmp_path / "wt_root5"),
    )
    loop = Loop(store, MockRunner([]), Registry.load(), config)
    # Reconciliation above already moved the Default project off the
    # placeholder as a side effect -- construct a SEPARATE store/loop where
    # it genuinely stays at the placeholder (scratch-mode config) to isolate
    # this precondition.
    store2 = Store(tmp_path / "test2.db")
    config2 = LoopConfig(db_path=store2.db_path)  # scratch, no reconciliation
    loop2 = Loop(store2, MockRunner([]), Registry.load(), config2)
    default_row = store2.get_project(store2.resolve_project(None))
    assert (default_row["repo_root"], default_row["workspace_mode"]) == (
        ".",
        "scratch",
    )
    task = add_task(store2, project_id=default_row["id"])
    # loop2's own config is scratch -> None, matching the placeholder-defer
    # rule, not whatever the (untouched) row happens to hold.
    assert loop2._worktree_repo_root(task) is None
    store2.close()
    del loop  # constructed only to exercise reconciliation's own side effect


def test_worktree_repo_root_uses_the_row_once_default_project_is_genuinely_repointed(
    store, tmp_path
):
    """Once the Default project's row has genuinely moved off the
    placeholder (a real `repoint_project` call, e.g. a future `agentloop
    project repoint Default`), that row must take effect immediately --
    never silently ignored just because it happens to be the Default
    project."""
    old_repo = make_repo(tmp_path / "old_repo")
    new_repo = make_repo(tmp_path / "new_repo", {"MARKER.txt": "new\n"})
    config = LoopConfig(
        db_path=store.db_path,
        workspace_mode="worktree",
        repo_root=str(old_repo),
        worktree_root=str(tmp_path / "wt_root6"),
    )
    loop = Loop(store, MockRunner([]), Registry.load(), config)
    default_id = store.resolve_project(None)

    # A genuine repoint, off the placeholder, to a DIFFERENT repo than
    # loop.config still names.
    store.repoint_project(default_id, str(new_repo), "worktree")

    task = add_task(store, project_id=default_id)
    resolved = loop._worktree_repo_root(task)
    assert resolved == Path(os.path.abspath(str(new_repo)))
    assert resolved != Path(os.path.abspath(str(old_repo)))


# -- plan()'s project_id parameter and child inheritance -------------------


def test_plan_children_inherit_the_plans_own_resolved_project_id(store, tmp_path):
    repo = make_repo(tmp_path / "plan_repo")
    project_id = store.create_project("PlanProj", str(repo), workspace_mode="worktree")
    config = LoopConfig(db_path=store.db_path, allow_test_exec=False)
    plan_output = (
        '[{"ref": "a", "title": "T", "goal": "G", "acceptance_criteria": "C"}]'
    )
    runner = MockRunner([plan_output])
    loop = Loop(store, runner, Registry.load(), config)

    plan_task = loop.plan("Ship a thing.", "It works.", project_id=project_id)

    assert plan_task.project_id == project_id
    children = [t for t in store.list_tasks() if t.plan_id == plan_task.id]
    assert len(children) == 1
    assert children[0].project_id == project_id


# -- human_approve/reject/redo against a non-default project ---------------


def test_human_reject_resolves_repo_root_from_the_tasks_own_project(store, tmp_path):
    repo_a = make_repo(tmp_path / "repo-a3", {"MARKER.txt": "a\n"})
    a = store.create_project("A3", str(repo_a), workspace_mode="worktree")
    task = add_task(store, project_id=a)
    loop, _ = make_loop(
        store,
        ["out-a", "VERDICT: revise CONFIDENCE: 0.5 TESTS: na\nnope."],
        worktree_root=str(tmp_path / "wt_root7"),
        max_revisions=0,
    )

    loop.run_task(task)
    assert store.get_task(task.id).status == TaskStatus.NEEDS_HUMAN

    rejected = loop.human_reject(task.id, note="not good enough")
    assert rejected.status == TaskStatus.FAILED


# -- run(project_id=X) under max_parallel_workers > 1 -----------------------


def test_run_with_project_id_filters_correctly_under_parallel_workers(store, tmp_path):
    repo_a = make_repo(tmp_path / "repo-a4", {"MARKER.txt": "a\n"})
    repo_b = make_repo(tmp_path / "repo-b4", {"MARKER.txt": "b\n"})
    a = store.create_project("A4", str(repo_a), workspace_mode="worktree")
    b = store.create_project("B4", str(repo_b), workspace_mode="worktree")

    task_a1 = add_task(store, project_id=a)
    task_a2 = add_task(store, project_id=a)
    task_b1 = add_task(store, project_id=b)

    loop, _ = make_loop(
        store,
        # A single repeated value, not an ["out", APPROVE] pair: MockRunner's
        # script is positional and racy under real concurrency (its own
        # docstring), so an identical value at every position sidesteps the
        # ordering problem entirely -- matches the existing parallel-worker
        # tests in tests/test_loop.py ([APPROVE] * 20).
        [APPROVE] * 6,
        worktree_root=str(tmp_path / "wt_root8"),
        max_parallel_workers=2,
    )

    processed = loop.run(project_id=a)

    assert processed == 2
    assert store.get_task(task_a1.id).status == TaskStatus.DONE
    assert store.get_task(task_a2.id).status == TaskStatus.DONE
    # Project B's task was never claimed under the parallel path either.
    assert store.get_task(task_b1.id).status == TaskStatus.PENDING
