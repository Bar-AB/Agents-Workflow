"""Slice 9 remediation, HIGH-3: `run_planner`'s worktree-mode `cwd` (the
operator's `repo_root`) has no existence check before use, unlike every
worker/validator call site, each of which is preceded by
`Loop._require_workspace`.

Without the guard, a missing `repo_root` (a typo, an unexpanded `~` — see
HIGH-4, a deleted checkout) reaches the SDK as a missing cwd, which raises a
plain `CLIConnectionError`. `_with_retry`'s transient branch cannot tell that
apart from a real network blip, so it burns `infra_max_retries` paid attempts
and escalates as `infra_error`, blaming the network for a permanent
condition. `_require_workspace` refuses up front instead: no retry, no
`infra_error`, and the plan row escalates immediately naming the missing
directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentloop.config import LoopConfig
from agentloop.loop import Loop
from agentloop.models import TaskStatus
from agentloop.registry import Registry
from agentloop.runner import MockRunner
from agentloop.store import Store
from tests.test_worktree_vcs import make_repo


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def test_a_missing_repo_root_escalates_the_plan_as_a_config_error_not_infra(
    store, tmp_path
):
    missing_repo_root = tmp_path / "this_repo_does_not_exist"
    config = LoopConfig(
        db_path=store.db_path,
        workspace_root=str(tmp_path / "ws"),
        vcs_enabled=True,
        workspace_mode="worktree",
        repo_root=str(missing_repo_root),
        worktree_root=str(tmp_path / "wt"),
        infra_max_retries=2,
    )
    runner = MockRunner(
        ['[{"ref": "a", "title": "T", "goal": "G", "acceptance_criteria": "C"}]']
    )
    loop = Loop(store, runner, Registry.load(), config)

    plan_task = loop.plan("Ship a slugify utility with tests.", "Lowercase, tested.")

    assert plan_task.status == TaskStatus.NEEDS_HUMAN
    assert "does not exist" in plan_task.escalation_reason
    # No retry: the model was never actually called.
    assert runner.calls == []
    # No infra_error event: this is a config error, not a transient failure.
    assert [e for e in store.events(plan_task.id) if e["kind"] == "infra_error"] == []


def test_a_tilde_repo_root_resolves_end_to_end_through_the_loop(
    store, tmp_path, monkeypatch
):
    """HIGH-4, exercised through the same seam as HIGH-3: `repo_root: "~/op"`
    used to pass `LoopConfig`'s own containment check (which expands `~`) and
    then fail at runtime, because `loop._worktree_repo_root` read the raw
    field. With `~` expanded once at config load, the planner's `cwd` is a
    real, existing directory and the plan completes rather than escalating."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    repo_root = make_repo(tmp_path / "op_repo")
    config = LoopConfig(
        db_path=store.db_path,
        workspace_root=str(tmp_path / "ws"),
        vcs_enabled=True,
        workspace_mode="worktree",
        repo_root="~/op_repo",
        worktree_root=str(tmp_path / "wt"),
        plan_requires_approval=False,
    )
    runner = MockRunner(
        ['[{"ref": "a", "title": "T", "goal": "G", "acceptance_criteria": "C"}]']
    )
    loop = Loop(store, runner, Registry.load(), config)

    plan_task = loop.plan("Ship a slugify utility with tests.", "Lowercase, tested.")

    assert plan_task.status == TaskStatus.DONE
    assert len(runner.calls) == 1
    assert Path(runner.calls[0]["cwd"]).samefile(repo_root)


def test_an_existing_repo_root_is_unaffected(store, tmp_path):
    """Control: the guard must not refuse a perfectly good repository."""
    repo_root = make_repo(tmp_path / "operator_repo")
    config = LoopConfig(
        db_path=store.db_path,
        workspace_root=str(tmp_path / "ws"),
        vcs_enabled=True,
        workspace_mode="worktree",
        repo_root=str(repo_root),
        worktree_root=str(tmp_path / "wt"),
        plan_requires_approval=False,
    )
    runner = MockRunner(
        ['[{"ref": "a", "title": "T", "goal": "G", "acceptance_criteria": "C"}]']
    )
    loop = Loop(store, runner, Registry.load(), config)

    plan_task = loop.plan("Ship a slugify utility with tests.", "Lowercase, tested.")

    assert plan_task.status == TaskStatus.DONE
    assert len(runner.calls) == 1
    assert runner.calls[0]["cwd"] is not None
    assert Path(runner.calls[0]["cwd"]).samefile(repo_root)
