"""The working directory an agent actually runs in (slice 9, P1).

An independent bug, fixed and shipped ahead of the rest of slice 9: the worker
prompt has always *asked* the agent to write under the task workspace
(`agents.run_worker`'s `## Workspace` block) while `ClaudeSDKRunner` passed no
`cwd` at all, so the agent's real working directory was the orchestrator's —
the operator's own repository. The instruction was the only thing pointing at
the workspace, and an instruction is not a boundary.

The fix widens the `ModelRunner` seam with `cwd`, so it is tested at that seam
in two places: what `ClaudeSDKRunner` hands the SDK (test 23), and what the
loop hands the seam for the worker *and* the validator (test 24, a pair —
the validator holds `file_io` too and had the same bug, quieter).
"""

import dataclasses
import os
import re
import shutil
from pathlib import Path

import pytest

from agentloop.config import LoopConfig
from agentloop.loop import Loop
from agentloop.models import Task, TaskStatus
from agentloop.registry import Registry
from agentloop.runner import ClaudeAgentOptions, ClaudeSDKRunner, MockRunner
from agentloop.store import Store

# `[claude]` is an optional extra (`pip install -e ".[dev]"` is the documented
# install), and `ClaudeAgentOptions` is None without it. Three tests here are
# claims about the *vendor's* option object and cannot run without the vendor;
# the rest — including both tests that pin the working directory the loop hands
# the seam — need no SDK, and used to vanish with them, because an ImportError
# at module scope is a collection error that deletes the whole module.
needs_sdk = pytest.mark.skipif(
    ClaudeAgentOptions is None,
    reason="requires the optional [claude] extra (this is a claim about the SDK)",
)


APPROVE = "VERDICT: approve CONFIDENCE: 0.92 TESTS: pass\nMeets all criteria."


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def _config(store, tmp_path, **overrides):
    overrides.setdefault("workspace_root", str(tmp_path / "ws"))
    overrides.setdefault("allow_test_exec", False)
    overrides.setdefault("vcs_enabled", False)
    return LoopConfig(db_path=store.db_path, **overrides)


def _add_task(store) -> Task:
    task = Task(
        id=None,
        title="Add slugify util",
        goal="Write a slugify(text) function.",
        acceptance_criteria="Lowercase, hyphen-separated, tested.",
        risk_level=0,
    )
    store.add_task(task)
    return task


# --- 23: build_options passes cwd equal to the workspace ---------------------


@needs_sdk
def test_the_sdk_option_object_really_has_a_cwd_field():
    """The premise of test 23, checked against the *installed* SDK rather than
    taken from a spec: `cwd` is a real field on `ClaudeAgentOptions`, so
    forwarding it is wiring and not an invention. Not a RED-first test — it
    passes before the fix, because it is a claim about the vendor, not about us.
    """
    names = {f.name for f in dataclasses.fields(ClaudeAgentOptions)}
    assert "cwd" in names


@needs_sdk
def test_build_options_passes_cwd_equal_to_the_workspace(tmp_path):
    """Test 23. The SDK executes the agent's tools inside `run()`, so the only
    place a working directory can be set is the options object."""
    ws = tmp_path / "ws" / "task-1"
    ws.mkdir(parents=True)
    options = ClaudeSDKRunner().build_options(
        "sys", "claude-opus-5", ["file_io"], str(ws)
    )
    assert options.cwd == str(ws)


@needs_sdk
def test_build_options_leaves_cwd_unset_when_none_is_given(tmp_path):
    """The control for the above: a caller with no workspace to point at (the
    summarizer, and the planner until P3 gives it `repo_root`) must not get an
    invented directory. Without this, a `build_options` that hard-coded any
    path would satisfy test 23."""
    options = ClaudeSDKRunner().build_options("sys", "claude-opus-5", None, None)
    assert options.cwd is None


# --- 24: worker- AND validator-written files land in the workspace -----------


class WritingRunner:
    """A backend whose agent holds `file_io` and writes a file, into whatever
    working directory it was handed.

    This stands in for the SDK's own behaviour under `ClaudeAgentOptions.cwd`:
    the SDK's `Write` tool resolves a relative path against the process's
    working directory, so `cwd or Path.cwd()` is the same rule the real agent
    follows. Before the fix the loop handed the seam nothing, so `cwd` is None
    and the write lands in the orchestrator's directory — which is what this
    test measures.

    Single-threaded, positional script, exactly like `MockRunner`.
    """

    def __init__(self, outputs: list[str]):
        self.outputs = list(outputs)
        self.cwds: list[str | None] = []

    def run(self, system_prompt, prompt, model, tools=None, cwd=None):
        from agentloop.models import RunResult

        self.cwds.append(cwd)
        name = f"agent-{len(self.cwds)}.txt"
        (Path(cwd) if cwd else Path.cwd()).joinpath(name).write_text(
            "written by the agent", encoding="utf-8"
        )
        output = self.outputs.pop(0) if self.outputs else "(mock output)"
        return RunResult(output=output, tokens_in=10, tokens_out=5, model="mock")


def test_worker_and_validator_write_into_the_workspace_not_the_orchestrator_cwd(
    store, tmp_path, monkeypatch
):
    """Test 24, the pair. Both agents hold `file_io`; before the fix both wrote
    into the orchestrator's working directory — the operator's repository on a
    real run.

    The orchestrator's cwd is moved to a scratch directory for the duration, so
    the pre-fix behaviour is *observable* here instead of writing into the
    checkout this test runs from."""
    orchestrator = tmp_path / "orchestrator"
    orchestrator.mkdir()
    monkeypatch.chdir(orchestrator)

    task = _add_task(store)
    runner = WritingRunner(["the worker output", APPROVE])
    loop = Loop(store, runner, Registry.load(), _config(store, tmp_path))
    loop.run_task(task)

    assert task.status == TaskStatus.DONE
    ws = Path(tmp_path / "ws" / f"task-{task.id}")
    worker_file, validator_file = ws / "agent-1.txt", ws / "agent-2.txt"
    assert worker_file.exists(), (
        f"worker wrote outside its workspace; cwds seen: {runner.cwds}"
    )
    assert validator_file.exists(), (
        f"validator wrote outside its workspace; cwds seen: {runner.cwds}"
    )
    # The other half of the same claim: nothing landed where it used to.
    assert sorted(p.name for p in orchestrator.iterdir()) == []


def test_the_loop_hands_the_workspace_to_the_seam_for_both_roles(store, tmp_path):
    """The same fact one layer down, at the seam itself, so a failure says
    *what* was passed rather than only where a file ended up. `MockRunner`
    records `cwd` for exactly this."""
    task = _add_task(store)
    runner = MockRunner(["the worker output", APPROVE])
    loop = Loop(store, runner, Registry.load(), _config(store, tmp_path))
    loop.run_task(task)

    ws = str(Path(tmp_path / "ws" / f"task-{task.id}"))
    assert [c["cwd"] for c in runner.calls] == [ws, ws]


def test_the_planner_is_given_no_cwd_yet(store, tmp_path):
    """A plan row has no task workspace, so there is nothing honest to point
    the planner at until P3 supplies `repo_root`. Pinned rather than left
    implicit: `None` here is a decision, and P3 flipping it should be a visible
    change to this assertion."""
    runner = MockRunner(
        ['[{"ref": "a", "title": "T", "goal": "G", "acceptance_criteria": "C"}]']
    )
    loop = Loop(store, runner, Registry.load(), _config(store, tmp_path))
    loop.plan("Ship a slugify utility with tests.", "Lowercase, hyphenated, tested.")

    assert [c["cwd"] for c in runner.calls] == [None]


# --- X1: the prompt and the cwd must name the same absolute directory --------


def test_a_relative_workspace_root_still_names_one_directory(
    store, tmp_path, monkeypatch
):
    """The relative-vs-absolute differential slice 8 established as the standing
    pattern, applied to the thing P1 introduced.

    `LoopConfig.workspace_root` ships **relative** (`.agentloop/ws`). That one
    string is both put in the worker prompt (`## Workspace`) and, since P1, made
    the agent's working directory — so a relative value is read twice from two
    different directories: the orchestrator's when the loop resolves it, and the
    *workspace* when the agent reads the instruction naming it. The agent then
    creates `<ws>/.agentloop/ws/task-N` and works there, pytest's default
    `norecursedirs` skips the dotted directory, nothing is collected, and the
    tests gate blocks approval every round until the revisions run out.

    Every other test in this file uses an absolute `tmp_path` root, which is
    exactly the blind spot slice 8's `vcs` critical had.
    """
    orchestrator = tmp_path / "orchestrator"
    orchestrator.mkdir()
    monkeypatch.chdir(orchestrator)

    task = _add_task(store)
    runner = MockRunner(["the worker output", APPROVE])
    config = LoopConfig(
        db_path=store.db_path,
        workspace_root="ws",  # relative, exactly as the shipped default is
        allow_test_exec=False,
        vcs_enabled=False,
    )
    loop = Loop(store, runner, Registry.load(), config)
    loop.run_task(task)

    cwd = runner.calls[0]["cwd"]
    assert Path(cwd).is_absolute(), f"the seam was handed a relative cwd: {cwd!r}"

    named = re.search(
        r"Write your files and tests under `([^`]+)`", runner.calls[0]["prompt"]
    )
    assert named, "the worker prompt lost its ## Workspace block"
    # The load-bearing assertion: the directory the agent stands in and the
    # directory the instruction names are the same one, resolved from anywhere.
    assert Path(named.group(1)).is_absolute()
    assert os.path.normcase(named.group(1)) == os.path.normcase(cwd)
    assert Path(cwd) == (orchestrator / "ws" / f"task-{task.id}")


# --- M1: cwd is a second enforcement surface and must be auditable -----------


def test_the_prompt_event_records_the_working_directory(store, tmp_path):
    """`tools` is in the `{kind}_prompt` payload because it is the enforcement
    surface. `cwd` is now a second one — it decides where `Write` lands — so it
    belongs in the same payload or it is invisible to `agentloop events`, the
    REST API and the SSE feed."""
    task = _add_task(store)
    runner = MockRunner(["the worker output", APPROVE])
    loop = Loop(store, runner, Registry.load(), _config(store, tmp_path))
    loop.run_task(task)

    ws = str(Path(tmp_path / "ws" / f"task-{task.id}"))
    kinds = {}
    for event in store.events(task.id):
        if event["kind"] in ("worker_prompt", "validator_prompt"):
            kinds[event["kind"]] = event["payload"]
    assert kinds["worker_prompt"]["cwd"] == ws
    assert kinds["validator_prompt"]["cwd"] == ws


# --- M2: a working directory that is gone is a config error, not a blip ------


class VanishingRunner:
    """A worker that removes its own workspace — it holds `file_io` and Bash,
    so this is one `rm -rf` away on a live run."""

    def __init__(self, ws: Path):
        self.ws = ws
        self.calls = 0

    def run(self, system_prompt, prompt, model, tools=None, cwd=None):
        from agentloop.models import RunResult

        self.calls += 1
        shutil.rmtree(self.ws, ignore_errors=True)
        return RunResult(
            output="the worker output", tokens_in=10, tokens_out=5, model="mock"
        )


def test_a_missing_working_directory_escalates_as_a_config_error(store, tmp_path):
    """The SDK raises a plain `CLIConnectionError` for a missing cwd, which
    `_with_retry`'s bare `except Exception` treats as transient: three paid
    retries, three `infra_error` rows, then NEEDS_HUMAN blaming the network for
    a permanent, operator-visible condition. Same classification gap
    `RunnerConfigError` already closes for a missing key or a 404."""
    task = _add_task(store)
    ws = Path(tmp_path / "ws" / "task-1")
    runner = VanishingRunner(ws)
    loop = Loop(store, runner, Registry.load(), _config(store, tmp_path))
    loop.run_task(task)

    assert task.status == TaskStatus.NEEDS_HUMAN
    assert "working directory" in (task.escalation_reason or "").lower()
    assert [e for e in store.events(task.id) if e["kind"] == "infra_error"] == []
    # One paid call, not four: the worker's. The validator never ran.
    assert runner.calls == 1
