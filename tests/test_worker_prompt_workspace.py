"""Slice 9 P3 — the worker's `## Workspace` block by mode.

Seam: `agents.run_worker`, driven directly with a `MockRunner` — the same seam
`_invoke`'s own docstring names as the enforcement surface for `cwd`, and the
narrowest one that can assert on the exact prompt string without going through
the whole loop.
"""

from __future__ import annotations

import pytest

from agentloop.agents import run_worker
from agentloop.config import LoopConfig
from agentloop.models import Task
from agentloop.registry import DEFAULT_AGENTS, Registry
from agentloop.runner import MockRunner
from agentloop.store import Store

APPROVE = "VERDICT: approve CONFIDENCE: 0.9 TESTS: pass\nfine"


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


@pytest.fixture()
def registry():
    return Registry(dict(DEFAULT_AGENTS))


@pytest.fixture()
def task(store):
    t = Task(
        id=None,
        title="Fix the bug",
        goal="Fix it",
        acceptance_criteria="Tests pass",
    )
    t.id = store.add_task(t)
    return store.get_task(t.id)


_SCRATCH_WORDING = (
    "\n## Workspace\nWrite your files and tests under `{ws}`. "
    "They are executed there automatically after you finish.\n"
)


def test_scratch_mode_workspace_block_is_byte_for_byte_unchanged(store, registry, task):
    """No config at all — the pre-P3 caller shape (every existing test that
    calls `run_worker` without a `config`)."""
    runner = MockRunner([APPROVE])
    run_worker(store, runner, registry, task, workspace="/ws/task-1")
    prompt = runner.calls[-1]["prompt"]
    assert _SCRATCH_WORDING.format(ws="/ws/task-1") in prompt


def test_scratch_mode_config_keeps_the_same_wording(store, registry, task, tmp_path):
    """An explicit `workspace_mode='scratch'` config (the default) must also
    leave the block byte-for-byte what it was — 'unset' and 'scratch' read
    identically here, same discipline as `_charter_block`."""
    runner = MockRunner([APPROVE])
    config = LoopConfig(workspace_root=str(tmp_path / "ws"), vcs_enabled=False)
    run_worker(store, runner, registry, task, workspace="/ws/task-1", config=config)
    prompt = runner.calls[-1]["prompt"]
    assert _SCRATCH_WORDING.format(ws="/ws/task-1") in prompt


def test_worktree_mode_workspace_block_reads_differently(
    store, registry, task, tmp_path
):
    """Worktree mode must not reuse the scratch wording — it is actively wrong
    for a directory that already holds the operator's tracked code — and must
    say so: read before writing, follow conventions, don't restructure."""
    runner = MockRunner([APPROVE])
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    config = LoopConfig(
        workspace_mode="worktree",
        repo_root=str(repo_root),
        worktree_root=str(tmp_path / "wt"),
        vcs_enabled=False,
    )
    run_worker(store, runner, registry, task, workspace="/ws/task-1", config=config)
    prompt = runner.calls[-1]["prompt"]

    assert _SCRATCH_WORDING.format(ws="/ws/task-1") not in prompt
    assert "## Workspace" in prompt
    assert "existing repository" in prompt
    assert "do not restructure" in prompt
    assert "/ws/task-1" in prompt


def test_neuter_the_scratch_wording_constant_is_actually_asserted(
    store, registry, task
):
    """[NEUTER] Confirms the byte-for-byte test above is not vacuous: a wrong
    wording constant must make it fail."""
    runner = MockRunner([APPROVE])
    run_worker(store, runner, registry, task, workspace="/ws/task-1")
    prompt = runner.calls[-1]["prompt"]
    wrong = _SCRATCH_WORDING.format(ws="/ws/DIFFERENT-PATH")
    assert wrong not in prompt
