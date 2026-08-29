"""Slice 6, Phase 4: the loop's non-destructive vcs call sites (C1-C4).

End-to-end through the `Loop` with scripted `MockRunner` outputs — the project's
standard seam for loop behaviour — against real `git` subprocesses in a
throwaway workspace under `tmp_path`. The operator's real repository is never a
subject: every workspace root is created inside the test's own temp directory.

Phase 5 adds `rollback`'s two callers (C5 `human_reject`, C6 `human_redo`) -
the only call sites that remove files, and the reason the nine-phase ordering
kept them behind the proven guard.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import threading
import warnings
from pathlib import Path

import pytest

from agentloop import vcs
from agentloop.loop import Loop
from agentloop.models import Task, TaskStatus
from agentloop.registry import Registry
from agentloop.runner import MockRunner, RunResult
from agentloop.store import Store
from tests.test_loop import APPROVE, REVISE

PLAN_JSON = """```json
{"tasks": [
  {"ref": "core", "title": "Write slugify()",
   "goal": "Implement slugify(text)",
   "acceptance_criteria": "Lowercase, hyphen-separated",
   "risk_level": 1, "depends_on": []}
]}
```
"""


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def make_loop(store, outputs, **cfg_overrides):
    """Like `tests.test_loop.make_loop`, but vcs is **on** by default — this
    module is the one that exercises it."""
    from agentloop.config import LoopConfig

    cfg_overrides.setdefault("workspace_root", str(Path(store.db_path).parent / "ws"))
    cfg_overrides.setdefault("allow_test_exec", False)
    cfg_overrides.setdefault("vcs_enabled", True)
    config = LoopConfig(db_path=store.db_path, **cfg_overrides)
    runner = MockRunner(outputs)
    return Loop(store, runner, Registry.load(), config), runner


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


def ws_of(loop, task_id) -> Path:
    return Path(loop.config.workspace_root) / f"task-{task_id}"


def git(ws: Path, *args: str) -> subprocess.CompletedProcess:
    """Read-only git run by the *test*, never by `vcs.py`."""
    return subprocess.run(
        ["git", *args], cwd=str(ws), capture_output=True, text=True, shell=False
    )


def payload_ref(sha: str) -> str:
    """The discarded ref a rollback of `sha` must have written."""
    return f"{vcs.DISCARDED_REF_PREFIX}/{sha}"


def non_git_files(ws: Path) -> list[str]:
    """Everything a rollback is supposed to remove. `.git` is not working-tree
    content, so it is excluded - the repo surviving is the point."""
    if not ws.is_dir():
        return []
    return sorted(
        str(p.relative_to(ws))
        for p in ws.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(ws).parts
    )


class WritingRunner(MockRunner):
    """A `MockRunner` that also puts the worker's "output" on disk.

    `MockRunner` executes no tools, so a scripted run leaves an empty
    workspace - and a rollback over an empty workspace proves nothing. The
    files are written on the first call, which `run_task` makes *after* C1's
    `init_repo` and *before* C2's commit: exactly where a real worker's writes
    land."""

    def __init__(self, outputs, ws: Path, files: dict[str, str]):
        super().__init__(outputs)
        self._ws = ws
        self._files = files
        self._written = False

    def run(self, *args, **kw):
        if not self._written:
            self._written = True
            self._ws.mkdir(parents=True, exist_ok=True)
            for name, body in self._files.items():
                (self._ws / name).write_text(body, encoding="utf-8")
        return super().run(*args, **kw)


def events_of_kind(store, task_id, kind) -> list[dict]:
    return [e for e in store.events(task_id) if e["kind"] == kind]


def failing_git(tmp_path: Path) -> str:
    """A stub `git` that always exits 1 (see `tests/test_vcs.stub_git`)."""
    if os.name == "nt":
        path = tmp_path / "stub_git.bat"
        path.write_text("@echo off\r\necho stub failure 1>&2\r\nexit /b 1\r\n")
    else:
        path = tmp_path / "stub_git.sh"
        path.write_text("#!/bin/sh\necho 'stub failure' 1>&2\nexit 1\n")
        os.chmod(path, 0o755)
    return str(path)


# -- C1: init ----------------------------------------------------------------


def test_a_workspace_gets_a_repo_and_a_base_ref_on_the_first_round(store):
    task = add_task(store)
    loop, _ = make_loop(store, ["worker output", APPROVE])
    loop.run_task(task)

    ws = ws_of(loop, task.id)
    assert vcs.is_repo(ws, loop.config) is True
    assert git(ws, "rev-parse", vcs.BASE_REF).returncode == 0


# -- C2: one commit per worker round ----------------------------------------


def test_each_worker_round_is_committed(store):
    task = add_task(store)
    loop, _ = make_loop(store, ["v1 output", REVISE, "v2 output", APPROVE])
    loop.run_task(task)

    rounds = [
        e
        for e in events_of_kind(store, task.id, "vcs_commit")
        if "round" in e["payload"]
    ]
    assert [e["payload"]["round"] for e in rounds] == [1, 2]
    shas = [e["payload"]["sha"] for e in rounds]
    assert all(len(s) == 40 for s in shas)
    assert shas[0] != shas[1]
    # Recoverability is reachability, not `git show` on an orphan.
    reachable = git(ws_of(loop, task.id), "log", "--all", "--format=%H").stdout
    assert all(sha in reachable for sha in shas)


# -- C3 / C4: the approved ref ----------------------------------------------


def test_the_done_transition_writes_the_approved_ref(store):
    task = add_task(store)
    loop, _ = make_loop(store, ["worker output", APPROVE])
    loop.run_task(task)

    assert task.status == TaskStatus.DONE
    ws = ws_of(loop, task.id)
    assert git(ws, "rev-parse", vcs.APPROVED_REF).returncode == 0
    approved = [
        e
        for e in events_of_kind(store, task.id, "vcs_commit")
        if e["payload"].get("ref") == "approved"
    ]
    assert len(approved) == 1


def test_a_high_risk_task_parked_for_sign_off_has_no_approved_ref_yet(store):
    """The control that makes the next test's assertion mean something: the
    sibling NEEDS_HUMAN branch must not write the ref."""
    task = add_task(store, risk=2)
    loop, _ = make_loop(store, ["worker output", APPROVE])
    loop.run_task(task)

    assert task.status == TaskStatus.NEEDS_HUMAN
    assert git(ws_of(loop, task.id), "rev-parse", vcs.APPROVED_REF).returncode != 0


def test_human_approve_writes_the_approved_ref(store):
    task = add_task(store, risk=2)
    loop, _ = make_loop(store, ["worker output", APPROVE])
    loop.run_task(task)
    assert task.status == TaskStatus.NEEDS_HUMAN

    loop.human_approve(task.id, note="looks right")

    ws = ws_of(loop, task.id)
    assert store.get_task(task.id).status == TaskStatus.DONE
    assert git(ws, "rev-parse", vcs.APPROVED_REF).returncode == 0


def test_human_reject_writes_no_approved_ref(store):
    """Control for the test above: same setup, the other decision."""
    task = add_task(store, risk=2)
    loop, _ = make_loop(store, ["worker output", APPROVE])
    loop.run_task(task)

    loop.human_reject(task.id, note="no")

    ws = ws_of(loop, task.id)
    assert store.get_task(task.id).status == TaskStatus.FAILED
    assert git(ws, "rev-parse", vcs.APPROVED_REF).returncode != 0


def test_a_plan_row_is_never_committed(store, monkeypatch):
    loop, _ = make_loop(store, [PLAN_JSON])
    plan = loop.plan("Build a slugify library", "Published and tested")

    calls: list[object] = []
    real = vcs.mark_approved
    monkeypatch.setattr(
        vcs,
        "mark_approved",
        lambda ws, config: (calls.append(ws), real(ws, config))[1],
    )
    loop.human_approve(plan.id)

    assert store.is_plan_approved(plan.id) is True
    assert calls == []
    assert events_of_kind(store, plan.id, "vcs_commit") == []
    assert events_of_kind(store, plan.id, "vcs_unavailable") == []


# -- G-4: no git subprocess inside an open store transaction -----------------


def test_no_vcs_call_runs_inside_a_store_transaction(store, monkeypatch):
    seen: list[int] = []

    def wrap(name):
        real = getattr(vcs, name)

        def spy(*args, **kw):
            seen.append(store._conn._txn_depth)
            assert store._conn._txn_depth == 0, (
                f"vcs.{name} ran inside an open transaction "
                f"(depth={store._conn._txn_depth}) — see DD-10"
            )
            return real(*args, **kw)

        monkeypatch.setattr(vcs, name, spy)

    for name in ("init_repo", "commit", "mark_approved"):
        wrap(name)

    task = add_task(store, risk=2)
    loop, _ = make_loop(store, ["worker output", APPROVE])
    loop.run_task(task)
    loop.human_approve(task.id)

    # Liveness: the assertion above proves nothing if nothing was wrapped.
    assert len(seen) >= 3
    assert seen == [0] * len(seen)


# -- degradation vs. a deliberate config choice (F15c) -----------------------


def test_vcs_unavailable_is_logged_once_per_task_run(store, tmp_path):
    task = add_task(store)
    loop, _ = make_loop(
        store,
        ["v1 output", REVISE, "v2 output", APPROVE],
        vcs_command=failing_git(tmp_path),
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        loop.run_task(task)

    assert task.status == TaskStatus.DONE  # durability never fails a task
    unavailable = events_of_kind(store, task.id, "vcs_unavailable")
    assert len(unavailable) == 1
    assert unavailable[0]["payload"]["op"] == "init"
    assert unavailable[0]["payload"]["reason"] == "git-failed"
    assert events_of_kind(store, task.id, "vcs_commit") == []


def test_vcs_disabled_logs_nothing_and_warns_nothing(store):
    task = add_task(store)
    loop, _ = make_loop(store, ["worker output", APPROVE], vcs_enabled=False)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        loop.run_task(task)
        loop.human_approve(task.id)

    assert [e for e in store.events(task.id) if e["kind"].startswith("vcs_")] == []
    assert [w for w in caught if issubclass(w.category, RuntimeWarning)] == []


def test_a_genuinely_degraded_run_logs_once_and_warns_once(store, tmp_path):
    """Control for the test above: silence is specific to `"disabled"`, not a
    warning path that never fires."""
    task = add_task(store)
    loop, _ = make_loop(
        store,
        ["worker output", APPROVE],
        vcs_command=str(tmp_path / "no-such-git-binary"),
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        loop.run_task(task)

    assert len(events_of_kind(store, task.id, "vcs_unavailable")) == 1
    assert len([w for w in caught if issubclass(w.category, RuntimeWarning)]) == 1


# -- C5: human_reject rolls back --------------------------------------------


def test_reject_rolls_the_workspace_back_and_keeps_the_work_in_history(store):
    """Design test #1, the slice's headline acceptance test. Assertion (b) is
    not redundant with (c): `git show` resolves an orphaned commit, so (a)+(c)
    alone pass against an implementation that has already destroyed the
    history. (b) is the assertion that fails against it (DD-12)."""
    task = add_task(store)
    loop, _ = make_loop(store, ["worker output", APPROVE])
    ws = ws_of(loop, task.id)
    loop.runner = WritingRunner(
        ["worker output", APPROVE], ws, {"out.txt": "the round's work"}
    )
    loop.run_task(task)
    assert task.status == TaskStatus.DONE
    round_sha = events_of_kind(store, task.id, "vcs_commit")[0]["payload"]["sha"]
    assert non_git_files(ws) == ["out.txt"]

    loop.human_reject(task.id, note="not what I asked for")

    # (a) the workspace is back at base
    assert non_git_files(ws) == []
    assert vcs.is_repo(ws, loop.config) is True
    # (b) the discarded round is still *reachable*, not merely resolvable
    assert "round 1" in git(ws, "log", "--all", "--oneline").stdout
    # (b2) ...and reachable *through the discarded ref specifically*.
    #
    # (b) alone does not prove DD-12. This task reached DONE first, so
    # `mark_approved` wrote `refs/agentloop/approved` AT THE ROUND COMMIT, and
    # `--all` walks every ref: measured, both `approved` and `discarded/<sha>`
    # point at it, so (b) passes with the discarded-ref mechanism entirely dead.
    # Found by mutation, after (b) had already been written *specifically* to be
    # the assertion `git show` could not fake — the same class of gap, one layer
    # up. The payload assertions below are no substitute either: they prove the
    # event *names* a ref, not that git can resolve it.
    assert (
        git(ws, "rev-parse", "--verify", "--quiet", payload_ref(round_sha)).returncode
        == 0
    ), (
        "the discarded ref does not resolve; (b) was satisfied by refs/agentloop/approved"
    )
    # (c) and its content survives
    shown = git(ws, "show", f"{round_sha}:out.txt")
    assert shown.returncode == 0
    assert shown.stdout.strip() == "the round's work"
    # (d) the status path is untouched
    fresh = store.get_task(task.id)
    assert fresh.status == TaskStatus.FAILED
    assert fresh.escalation_reason == "not what I asked for"

    rolled = events_of_kind(store, task.id, "vcs_rollback")
    assert len(rolled) == 1
    payload = rolled[0]["payload"]
    assert payload["ref"] == "base"
    assert payload["discarded_sha"] == round_sha
    assert payload["discarded_ref"] == f"{vcs.DISCARDED_REF_PREFIX}/{round_sha}"
    assert payload["files_removed"] == 1


def test_reject_without_a_repo_leaves_the_workspace_exactly_as_it_was(store):
    """The pre-slice-6 behaviour, and the control for the test above: with the
    feature off, `human_reject` touches the workspace not at all."""
    task = add_task(store)
    loop, _ = make_loop(store, ["worker output", APPROVE], vcs_enabled=False)
    ws = ws_of(loop, task.id)
    loop.runner = WritingRunner(
        ["worker output", APPROVE], ws, {"out.txt": "the round's work"}
    )
    loop.run_task(task)
    assert non_git_files(ws) == ["out.txt"]

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        loop.human_reject(task.id, note="no")

    assert non_git_files(ws) == ["out.txt"]
    assert (ws / "out.txt").read_text(encoding="utf-8") == "the round's work"
    assert (ws / ".git").exists() is False
    assert store.get_task(task.id).status == TaskStatus.FAILED
    # A deliberate config choice is not a degradation (F15c).
    assert [e for e in store.events(task.id) if e["kind"].startswith("vcs_")] == []
    assert [w for w in caught if issubclass(w.category, RuntimeWarning)] == []


def test_reject_on_a_repo_less_workspace_does_not_touch_anything_outside_it(
    store, tmp_path
):
    """The loop-level companion to design test #4 (containment). The workspace
    root sits inside a throwaway repo, so an unguarded `reset --hard` would
    resolve to *that* repo and destroy the operator's tree."""
    outer = tmp_path / "outer"
    outer.mkdir()
    assert git(outer, "init", "-q").returncode == 0
    (outer / "tracked.txt").write_text("operator content", encoding="utf-8")
    assert git(outer, "add", "-A").returncode == 0
    committed = git(
        outer,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@localhost",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        "outer",
    )
    assert committed.returncode == 0, committed.stderr

    task = add_task(store)
    loop, _ = make_loop(store, ["worker output"], workspace_root=str(outer / "ws"))
    ws = ws_of(loop, task.id)
    ws.mkdir(parents=True)
    (ws / "leftover.txt").write_text("slice-5 workspace", encoding="utf-8")

    with pytest.warns(RuntimeWarning):
        loop.human_reject(task.id, note="drop it")

    assert (outer / "tracked.txt").read_text(encoding="utf-8") == "operator content"
    assert git(outer, "diff", "--name-only").stdout.strip() == ""
    assert (ws / "leftover.txt").read_text(encoding="utf-8") == "slice-5 workspace"
    unavailable = events_of_kind(store, task.id, "vcs_unavailable")
    assert len(unavailable) == 1
    assert unavailable[0]["payload"]["op"] == "rollback"
    assert unavailable[0]["payload"]["reason"] == "not-a-workspace-repo"
    assert events_of_kind(store, task.id, "vcs_rollback") == []


# -- C6: human_redo rolls back, or falls back to a wipe ----------------------


def test_redo_empties_the_workspace_but_keeps_the_previous_round_in_history(store):
    """Design test #9, with its `vcs_enabled=False` control. Both branches
    converge on "no non-`.git` file"; only the fallback removes the
    directory."""
    task = add_task(store)
    loop, _ = make_loop(store, ["worker output", APPROVE])
    ws = ws_of(loop, task.id)
    loop.runner = WritingRunner(
        ["worker output", APPROVE], ws, {"out.txt": "the round's work"}
    )
    loop.run_task(task)
    round_sha = events_of_kind(store, task.id, "vcs_commit")[0]["payload"]["sha"]

    loop.human_redo(task.id, note="start over")

    fresh = store.get_task(task.id)
    assert fresh.status == TaskStatus.PENDING
    assert fresh.output == ""
    assert fresh.revision_count == 0
    assert non_git_files(ws) == []
    assert vcs.is_repo(ws, loop.config) is True
    assert "round 1" in git(ws, "log", "--all", "--oneline").stdout
    assert git(ws, "show", f"{round_sha}:out.txt").stdout.strip() == "the round's work"
    payload = events_of_kind(store, task.id, "vcs_rollback")[0]["payload"]
    assert payload["discarded_sha"] == round_sha

    # A second redo discards nothing: the two keys are *absent*, not null.
    loop.human_redo(task.id)
    second = events_of_kind(store, task.id, "vcs_rollback")[1]["payload"]
    assert second["ref"] == "base"
    assert second["files_removed"] == 0
    assert "discarded_sha" not in second
    assert "discarded_ref" not in second

    # Control (ADR-3): with no repo, redo is today's `clear_workspace` and the
    # directory itself is gone.
    other = add_task(store)
    off, _ = make_loop(store, ["worker output", APPROVE], vcs_enabled=False)
    off_ws = ws_of(off, other.id)
    off.runner = WritingRunner(
        ["worker output", APPROVE], off_ws, {"out.txt": "the round's work"}
    )
    off.run_task(other)
    assert off_ws.is_dir()

    off.human_redo(other.id)

    assert off_ws.exists() is False
    assert events_of_kind(store, other.id, "vcs_rollback") == []


def test_redo_falls_back_to_a_wipe_when_rollback_leaves_residue(store, monkeypatch):
    """G-2: `ok=True, reason="residue"` **with no sha** means the rollback ran,
    files survived it and nothing was recorded, so there is no history a wipe
    could destroy and redo's "fresh start" promise is kept by the wipe. It is
    the control for `test_redo_keeps_the_recovery_ref_the_event_just_named`:
    the discriminator is the sha, not `ok`. The workspace really holds an
    initialised repo, so `clear_workspace` faces the read-only git objects it
    faces in production (DD-14)."""
    import agentloop.loop as loop_mod

    task = add_task(store)
    loop, _ = make_loop(store, ["worker output", APPROVE])
    ws = ws_of(loop, task.id)
    loop.runner = WritingRunner(
        ["worker output", APPROVE], ws, {"out.txt": "the round's work"}
    )
    loop.run_task(task)
    assert (ws / ".git").is_dir()

    monkeypatch.setattr(
        loop_mod.vcs,
        "rollback",
        lambda *a, **k: vcs.VcsResult(ok=True, reason="residue"),
    )
    loop.human_redo(task.id)

    assert ws.exists() is False
    assert store.get_task(task.id).status == TaskStatus.PENDING


# ===========================================================================
# Phase 6 - whole-feature proofs: degradation, inertness, parallel safety.
#
# Each of the three behavioural proofs needs the *finished* feature on both
# sides to diff, which is why they sit behind P4/P5 rather than beside them.
# ===========================================================================


# -- the inertness snapshot (Property 3, DD-15) ------------------------------

# The pre-declared exclusion table from `## Provable properties` item 3. It is
# reproduced here as data, not as prose, so the applied list is inspectable:
# applying exactly these is the contract, and narrowing the snapshot any
# further is the drift this phase is forbidden to commit.
SNAPSHOT_EXCLUSIONS = (
    "id",
    "task_id",
    "attempt_id",
    "created_at",
    "duration_s",
    "ts",
)
WORKSPACE_PLACEHOLDER = "<WS>"


def _drop_excluded(row: dict) -> dict:
    return {k: v for k, v in row.items() if k not in SNAPSHOT_EXCLUSIONS}


def snapshot(store, task_id: int, workspace_root: str) -> dict:
    """The full observable state of one task run, after DD-15's normalisation.

    `workspace_root` is the third argument the plan's `snapshot(store, task_id)`
    sketch left implicit: the normalisation replaces *this run's own* workspace
    path, and that path is a function of the root and the task id, neither of
    which is recoverable from the store row alone.
    """
    task = store.get_task(task_id)
    ws = Path(workspace_root) / f"task-{task_id}"

    def normalise(text: str) -> str:
        for form in (str(ws), ws.as_posix()):
            text = text.replace(form, WORKSPACE_PLACEHOLDER)
        return text

    verdicts = [
        _drop_excluded(dict(r))
        for r in store._conn.execute(
            "SELECT * FROM verdicts WHERE task_id=? ORDER BY id", (task_id,)
        ).fetchall()
    ]
    metrics = store.task_metrics(task_id)
    events = store.events(task_id)
    prompts = [
        {
            "kind": e["kind"],
            "role": e["payload"]["role"],
            "prompt": normalise(e["payload"]["prompt"]),
            "tools": e["payload"]["tools"],
        }
        for e in events
        if e["kind"].endswith("_prompt")
    ]
    return {
        "status": task.status.value,
        "revision_count": task.revision_count,
        "escalation_reason": task.escalation_reason,
        "output": task.output,
        "verdicts": verdicts,
        "attempts": metrics["attempts"],
        "tokens": metrics["tokens"],
        "cost_usd": metrics["cost_usd"],
        "test_runs": [_drop_excluded(r) for r in store.test_runs(task_id)],
        "event_kinds": [e["kind"] for e in events if not e["kind"].startswith("vcs_")],
        "prompts": prompts,
    }


def differing_keys(a: dict, b: dict) -> list[str]:
    return sorted(k for k in a if a[k] != b[k])


def run_arm(store, script, **cfg):
    """One scripted `run_task`, returning `(snapshot, task, loop)`."""
    task = add_task(store)
    loop, _ = make_loop(store, list(script), **cfg)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        loop.run_task(task)
    return snapshot(store, task.id, loop.config.workspace_root), task, loop


SCRIPT = ["v1 output", REVISE, "v2 output", APPROVE]


# -- design test #7: a missing git binary changes nothing --------------------


def test_a_missing_git_binary_leaves_every_loop_behavior_unchanged(store, tmp_path):
    """git is an external executable in the category of `test_command`: its
    absence degrades durability and never touches a decision. Proved as a
    differential against the feature *off*, not asserted."""
    missing = str(tmp_path / "no-such-git-binary")
    broken, broken_task, _ = run_arm(store, SCRIPT, vcs_command=missing)
    off, off_task, _ = run_arm(store, SCRIPT, vcs_enabled=False)

    assert differing_keys(broken, off) == []
    assert broken == off
    assert broken["status"] == TaskStatus.DONE.value
    assert broken["revision_count"] == 1

    # Degradation is reported once per `run_task`, not once per skipped call:
    # the run made two worker rounds and reached DONE, so an implementation
    # without the `vcs_ready` gate would have logged init + 2 commits +
    # mark_approved.
    unavailable = events_of_kind(store, broken_task.id, "vcs_unavailable")
    assert len(unavailable) == 1
    assert unavailable[0]["payload"]["op"] == "init"
    assert unavailable[0]["payload"]["reason"] == "git-missing"
    assert events_of_kind(store, broken_task.id, "vcs_commit") == []
    # ... and a deliberate `vcs_enabled=False` is not a degradation at all.
    assert events_of_kind(store, off_task.id, "vcs_unavailable") == []

    # Control: with a working git the same script *does* produce vcs events, so
    # the assertions above are about a missing binary and not about a code path
    # that never runs either way.
    live, live_task, _ = run_arm(store, SCRIPT)
    assert differing_keys(live, off) == []
    assert events_of_kind(store, live_task.id, "vcs_unavailable") == []
    assert len(events_of_kind(store, live_task.id, "vcs_commit")) == 3


# -- design test #8: the inertness differential (Property 3) -----------------


def test_vcs_disabled_and_enabled_produce_identical_observable_state(store):
    """The slice's central claim - that it adds no decision rule - discharged by
    diffing the full observable state of two identical scripts, one with git on
    and one with it off, after the exclusion table pre-declared in the plan
    (DD-15) and nothing further."""
    off, _, _ = run_arm(store, SCRIPT, vcs_enabled=False)
    on, on_task, on_loop = run_arm(store, SCRIPT)

    assert differing_keys(on, off) == []
    assert on == off

    # The snapshot is not vacuous: it carries the state the decision rules read.
    assert on["status"] == TaskStatus.DONE.value
    assert on["revision_count"] == 1
    assert [v["kind"] for v in on["verdicts"]] == ["revise", "approve"]
    assert on["attempts"] == 4
    assert on["tokens"] > 0
    assert "worker_prompt" in on["event_kinds"]
    assert [p["kind"] for p in on["prompts"]] == [
        "worker_prompt",
        "validator_prompt",
        "worker_prompt",
        "validator_prompt",
    ]
    assert all(p["tools"] for p in on["prompts"])
    assert all(
        WORKSPACE_PLACEHOLDER in p["prompt"]
        for p in on["prompts"]
        if p["kind"] == "worker_prompt"
    )

    # Control (a) - sensitivity. A deliberately varied script must make the two
    # snapshots differ; without it the equality above could hold because the
    # comparison compares nothing.
    varied, _, _ = run_arm(store, ["only output", APPROVE])
    varied_diffs = differing_keys(varied, off)
    assert varied_diffs, "the snapshot cannot tell two different runs apart"
    for key in ("revision_count", "output", "verdicts", "attempts", "event_kinds"):
        assert key in varied_diffs

    # Control (b) - filter non-vacuity. The `vcs_*` filter must have had
    # something to remove in the enabled arm, otherwise this is a diff of two
    # git-free runs dressed up as a proof.
    unfiltered = [e["kind"] for e in store.events(on_task.id)]
    removed = [k for k in unfiltered if k.startswith("vcs_")]
    assert removed, "nothing vcs-shaped was filtered; the differential is vacuous"
    assert len(unfiltered) - len(on["event_kinds"]) == len(removed)
    assert vcs.is_repo(ws_of(on_loop, on_task.id), on_loop.config) is True


# -- design test #11: two parallel tasks, two independent repos --------------


class ParallelWritingRunner:
    """Thread-safe, content-routing, and it writes into the workspace the prompt
    names.

    Positional `MockRunner` is documented single-threaded-only, so under
    `max_parallel_workers=2` it would hand whichever thread called first
    whatever output is next - the test would then be asserting on an ordering it
    created itself. Routing on content removes that; the barrier is what proves
    the two tasks were genuinely in flight together rather than merely fast.
    """

    def __init__(self, n_parallel: int):
        self.barrier = threading.Barrier(n_parallel, timeout=30)
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def run(self, system_prompt, prompt, model, tools=None):
        if prompt.startswith("# Task under review"):
            return RunResult(output=APPROVE, tokens_in=10, tokens_out=5, model="mock")
        ws = Path(prompt.split("Write your files and tests under `")[1].split("`")[0])
        name = ws.name  # "task-<id>", unique per task by construction
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            self.barrier.wait()
            (ws / f"{name}.txt").write_text(f"work of {name}", encoding="utf-8")
            return RunResult(
                output=f"output of {name}", tokens_in=10, tokens_out=5, model="mock"
            )
        finally:
            with self._lock:
                self.active -= 1


def test_two_parallel_tasks_get_independent_repos(store, tmp_path):
    """ADR-1/DD-1 is one repo per task workspace and no shared git index. Proved
    rather than asserted: two tasks run at the same moment and neither repo can
    see the other's commits."""
    from agentloop.config import LoopConfig

    a, b = add_task(store), add_task(store)
    runner = ParallelWritingRunner(2)
    config = LoopConfig(
        db_path=store.db_path,
        workspace_root=str(tmp_path / "ws"),
        allow_test_exec=False,
        vcs_enabled=True,
        max_parallel_workers=2,
    )
    loop = Loop(store, runner, Registry.load(), config)

    assert loop.run() == 2
    assert runner.max_active == 2, "the two tasks never overlapped"
    for task_id in (a.id, b.id):
        assert store.get_task(task_id).status == TaskStatus.DONE

    ws_a, ws_b = ws_of(loop, a.id), ws_of(loop, b.id)
    assert ws_a != ws_b
    for ws in (ws_a, ws_b):
        assert vcs.is_repo(ws, config) is True
        # Its own root, not a subdirectory of one shared repo.
        toplevel = git(ws, "rev-parse", "--show-toplevel").stdout.strip()
        assert Path(toplevel).resolve() == ws.resolve()

    def shas(ws):
        return set(git(ws, "log", "--all", "--format=%H").stdout.split())

    shas_a, shas_b = shas(ws_a), shas(ws_b)
    # Control: base + the round commit in each, and two heads that are not the
    # same commit - without it "disjoint" would be satisfied by two empty sets.
    assert len(shas_a) >= 2 and len(shas_b) >= 2
    head_a = git(ws_a, "rev-parse", "HEAD").stdout.strip()
    head_b = git(ws_b, "rev-parse", "HEAD").stdout.strip()
    assert head_a and head_b and head_a != head_b
    # Neither repo has heard of the other's *work*. Their empty base commits can
    # legitimately share a sha - git is content addressed, and both are the same
    # empty tree with the same pinned identity and message - so identity is
    # asserted on the commits that carry a worker's files.
    assert head_a not in shas_b
    assert head_b not in shas_a
    assert git(ws_a, "cat-file", "-e", head_b).returncode != 0
    assert git(ws_b, "cat-file", "-e", head_a).returncode != 0
    # And each holds only its own worker's file.
    assert non_git_files(ws_a) == [f"task-{a.id}.txt"]
    assert non_git_files(ws_b) == [f"task-{b.id}.txt"]


# -- the structural check: no status write downstream of a vcs result --------

_STATUS_WRITERS = frozenset({"set_status", "update_task"})
_STATUS_ATTRS = frozenset({"status", "escalation_reason", "revision_count"})


def _loop_tree():
    source = (
        Path(__file__).resolve().parent.parent / "agentloop" / "loop.py"
    ).read_text(encoding="utf-8")
    return ast.parse(source)


def vcs_call_names(tree, inverted: bool = False) -> list[str]:
    """Every `vcs.<name>(...)` call expression in `tree`.

    `inverted=True` flips the predicate to `self.store.<name>(...)` - the
    walker-liveness control, which proves the walk can see calls at all.
    """
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        base = node.func.value
        if inverted:
            if isinstance(base, ast.Attribute) and base.attr == "store":
                found.append(node.func.attr)
        elif isinstance(base, ast.Name) and base.id == "vcs":
            found.append(node.func.attr)
    return found


def _reads(node) -> set:
    return {
        n.id
        for n in ast.walk(node)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    }


def _mentions_vcs_call(node) -> bool:
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            base = n.func.value
            if isinstance(base, ast.Name) and base.id == "vcs":
                return True
            # The two helpers that wrap one and return its `VcsResult`.
            if n.func.attr.startswith("_vcs_"):
                return True
    return False


def _contains_status_write(node) -> bool:
    for n in ast.walk(node):
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr in _STATUS_WRITERS
        ):
            return True
        if isinstance(n, ast.Assign):
            for target in n.targets:
                if isinstance(target, ast.Attribute) and target.attr in _STATUS_ATTRS:
                    return True
    return False


def status_writes_downstream_of_vcs(tree) -> list[str]:
    """Every place a vcs result reaches a status write, as `function: shape`.

    Taint is tracked **per function** and a name dies when it is rebound: the
    local name `result` is bound five times in `loop.py` and only one binding is
    a vcs call - the others read `result.output` off the worker's `RunResult`,
    an attribute `VcsResult` does not have. Without the rebinding rule a
    name-based walk reports those four as violations.
    """
    violations: list[str] = []

    def scan(body, tainted, where):
        for stmt in body:
            if (
                isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
            ):
                name = stmt.targets[0].id
                if _mentions_vcs_call(stmt.value) or (_reads(stmt.value) & tainted):
                    tainted.add(name)
                else:
                    tainted.discard(name)  # rebinding kills the taint
                continue
            if isinstance(stmt, (ast.If, ast.While)):
                if (_reads(stmt.test) & tainted) and _contains_status_write(stmt):
                    violations.append(f"{where}: branch on a vcs result writes status")
                scan(stmt.body, tainted, where)
                scan(stmt.orelse, tainted, where)
                continue
            # A status write handed a tainted value directly.
            for n in ast.walk(stmt):
                if (
                    isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr in _STATUS_WRITERS
                ):
                    args = list(n.args) + [k.value for k in n.keywords]
                    if any(_reads(arg) & tainted for arg in args):
                        violations.append(
                            f"{where}: vcs result passed to a status write"
                        )
            for field in ("body", "orelse", "finalbody"):
                nested = getattr(stmt, field, None)
                if isinstance(nested, list) and nested:
                    scan(nested, tainted, where)
            for handler in getattr(stmt, "handlers", []):
                scan(handler.body, tainted, where)

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scan(node.body, set(), node.name)
    return violations


def test_no_status_write_is_downstream_of_a_vcs_result():
    """DD-8, proved structurally: no runtime test can prove the *absence* of a
    data dependency, so this one reads the source."""
    tree = _loop_tree()

    # Control 1 - the subject pin. "No vcs result reaches a status decision" is
    # trivially true of a `loop.py` containing no vcs calls at all, which is
    # exactly what a broken import or a rename produces. A later phase that
    # legitimately changes the count changes this number in the same commit.
    names = vcs_call_names(tree)
    assert len(names) == 4, f"expected 4 vcs.* call expressions, found {names}"
    assert set(names) == {"init_repo", "commit", "mark_approved", "rollback"}

    # Control 2 - walker liveness, the inverted predicate.
    assert len(vcs_call_names(tree, inverted=True)) > 0

    assert status_writes_downstream_of_vcs(tree) == []

    # Control 3 - the check can fail. Both violation shapes against a planted
    # subject, plus the false positive the rebinding rule exists to kill.
    planted_branch = ast.parse(
        "def f(self, task):\n"
        "    r = vcs.commit(ws, 'round 1', self.config)\n"
        "    if r.ok:\n"
        "        self.store.set_status(task, 'done')\n"
    )
    assert len(status_writes_downstream_of_vcs(planted_branch)) == 1
    planted_arg = ast.parse(
        "def f(self, task):\n"
        "    r = self._vcs_rollback_to_base(task.id)\n"
        "    self.store.set_status(task, 'failed', reason=r.reason)\n"
    )
    assert len(status_writes_downstream_of_vcs(planted_arg)) == 1
    rebound = ast.parse(
        "def f(self, task):\n"
        "    result = vcs.commit(ws, 'round 1', self.config)\n"
        "    result = self._with_retry(task, 'worker')\n"
        "    if result.output:\n"
        "        self.store.set_status(task, 'done')\n"
    )
    assert status_writes_downstream_of_vcs(rebound) == []


# ===========================================================================
# Remediation cycle 1 - the loop's half of the two blind reviews.
# ===========================================================================


def set_status_that_never_lands(store, blocked: TaskStatus):
    """A `set_status` whose write for `blocked` does not land, faithfully:
    the real one assigns the new status onto the in-hand object *before* the
    lease-predicated write, and returns whether the row was written. That gap
    is the whole finding - a caller reading the object instead of the return
    value sees a transition the row never took."""
    real = store.set_status

    def fake(task, status, *args, **kw):
        if status is blocked:
            task.status = status
            return False
        return real(task, status, *args, **kw)

    return fake


def test_a_done_write_that_did_not_land_writes_no_approved_ref(store, monkeypatch):
    """HIGH 2 (C3). `set_status` is lease-predicated and returns whether the
    row was written; C3 discarded it, so a no-op DONE write still moved
    `refs/agentloop/approved` - a durability claim about a transition that did
    not happen. C4 already gated correctly; the three sites now share a shape."""
    task = add_task(store)
    loop, _ = make_loop(store, ["worker output", APPROVE])
    monkeypatch.setattr(
        store, "set_status", set_status_that_never_lands(store, TaskStatus.DONE)
    )

    loop.run_task(task)

    ws = ws_of(loop, task.id)
    assert git(ws, "rev-parse", "--verify", "--quiet", vcs.APPROVED_REF).returncode != 0
    assert [
        e
        for e in events_of_kind(store, task.id, "vcs_commit")
        if e["payload"].get("ref") == "approved"
    ] == []


def test_a_landing_done_write_still_writes_the_approved_ref(store):
    """Control for the row above: the gate is on the write landing, not a
    branch that never fires."""
    task = add_task(store)
    loop, _ = make_loop(store, ["worker output", APPROVE])

    loop.run_task(task)

    ws = ws_of(loop, task.id)
    assert git(ws, "rev-parse", "--verify", "--quiet", vcs.APPROVED_REF).returncode == 0


def test_a_reject_that_did_not_land_rolls_nothing_back(store, monkeypatch):
    """HIGH 2 (C5). The comment said "the row is already FAILED and committed"
    - exactly what `set_status`'s return value exists to *not* assume. On a
    lease-predicated miss the workspace was rolled back for a task that was
    not rejected, while a live worker may be mid-round."""
    task = add_task(store)
    loop, _ = make_loop(store, ["worker output", APPROVE])
    loop.runner = WritingRunner(
        ["worker output", APPROVE], ws_of(loop, task.id), {"out.txt": "the work"}
    )
    loop.run_task(task)
    ws = ws_of(loop, task.id)
    assert (ws / "out.txt").exists()

    monkeypatch.setattr(
        store, "set_status", set_status_that_never_lands(store, TaskStatus.FAILED)
    )
    loop.human_reject(task.id, note="no")

    assert (ws / "out.txt").read_text(encoding="utf-8") == "the work"
    assert events_of_kind(store, task.id, "vcs_rollback") == []


def test_a_rollback_that_left_residue_is_audited_on_reject(store, monkeypatch):
    """HIGH 1. `reason` is `""` *exactly* when nothing degraded, and rollback
    can return `ok=True, reason="residue"`. Branching on `ok` alone dropped
    that: no warning, no `vcs_unavailable` row, and a `vcs_rollback` payload
    claiming a clean rollback over work still sitting on disk."""
    import agentloop.loop as loop_mod

    task = add_task(store)
    loop, _ = make_loop(store, ["worker output", APPROVE])
    loop.run_task(task)

    monkeypatch.setattr(
        loop_mod.vcs,
        "rollback",
        lambda *a, **k: vcs.VcsResult(ok=True, reason="residue", files_removed=2),
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        loop.human_reject(task.id, note="no")

    degraded = events_of_kind(store, task.id, "vcs_unavailable")
    assert [e["payload"]["reason"] for e in degraded] == ["residue"]
    assert events_of_kind(store, task.id, "vcs_rollback")[-1]["payload"][
        "degraded"
    ] == ("residue")
    assert [w for w in caught if issubclass(w.category, RuntimeWarning)] != []


def test_a_clean_rollback_is_not_audited_as_degraded(store):
    """Control: `reason=""` stays silent, so the row above is not passing
    against an audit path that fires unconditionally."""
    task = add_task(store)
    loop, _ = make_loop(store, ["worker output", APPROVE])
    loop.run_task(task)

    loop.human_reject(task.id, note="no")

    assert events_of_kind(store, task.id, "vcs_unavailable") == []
    assert (
        "degraded" not in events_of_kind(store, task.id, "vcs_rollback")[-1]["payload"]
    )


def test_redo_keeps_a_workspace_whose_history_was_preserved(store, monkeypatch):
    """HIGH 3. `vcs.rollback` aborts rather than lose the history - but any
    `ok=False`, including a reset or clean that failed *after* the discarded
    ref was written, routed to `clear_workspace`, which rmtrees `.git`, every
    round commit and that freshly written ref. The caller destroyed exactly
    what the callee refused to destroy."""
    import agentloop.loop as loop_mod

    task = add_task(store)
    loop, _ = make_loop(store, ["worker output", APPROVE])
    ws = ws_of(loop, task.id)
    loop.runner = WritingRunner(
        ["worker output", APPROVE], ws, {"out.txt": "the round's work"}
    )
    loop.run_task(task)
    round_sha = git(ws, "rev-parse", "HEAD").stdout.strip()

    monkeypatch.setattr(
        loop_mod.vcs,
        "rollback",
        lambda *a, **k: vcs.VcsResult(ok=False, reason="git-failed", sha=round_sha),
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        loop.human_redo(task.id)

    assert (ws / ".git").is_dir(), "the preserved history was wiped by the caller"
    degraded = events_of_kind(store, task.id, "vcs_unavailable")[-1]["payload"]
    assert degraded["history_preserved"] is True
    assert degraded["discarded_ref"] == f"{vcs.DISCARDED_REF_PREFIX}/{round_sha}"


def test_redo_still_wipes_when_nothing_was_preserved(store, monkeypatch):
    """Control for the row above: a rollback that never got far enough to
    record anything still falls back to the wipe, and says so."""
    import agentloop.loop as loop_mod

    task = add_task(store)
    loop, _ = make_loop(store, ["worker output", APPROVE])
    ws = ws_of(loop, task.id)
    loop.runner = WritingRunner(
        ["worker output", APPROVE], ws, {"out.txt": "the round's work"}
    )
    loop.run_task(task)

    monkeypatch.setattr(
        loop_mod.vcs,
        "rollback",
        lambda *a, **k: vcs.VcsResult(ok=False, reason="git-failed"),
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        loop.human_redo(task.id)

    assert ws.exists() is False
    degraded = events_of_kind(store, task.id, "vcs_unavailable")[-1]["payload"]
    assert degraded["history_preserved"] is False


def test_the_rollback_event_names_a_nested_repo_it_cannot_recover(store):
    """CRITICAL 2, residual half, at the audit seam: the payload must not
    assert a recovery surface that does not hold the work."""
    task = add_task(store)
    loop, _ = make_loop(store, ["worker output", APPROVE])
    ws = ws_of(loop, task.id)
    loop.runner = WritingRunner(
        ["worker output", APPROVE], ws, {"out.txt": "the round's work"}
    )
    loop.run_task(task)
    sub = ws / "sub"
    sub.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(sub), check=True, shell=False)

    loop.human_reject(task.id, note="no")

    payload = events_of_kind(store, task.id, "vcs_rollback")[-1]["payload"]
    assert payload["unrecoverable_nested_repos"] == ["sub"]


def test_the_rollback_event_claims_no_gap_when_there_is_none(store):
    """Control: the ordinary rollback names no gap."""
    task = add_task(store)
    loop, _ = make_loop(store, ["worker output", APPROVE])
    loop.runner = WritingRunner(
        ["worker output", APPROVE], ws_of(loop, task.id), {"out.txt": "work"}
    )
    loop.run_task(task)

    loop.human_reject(task.id, note="no")

    payload = events_of_kind(store, task.id, "vcs_rollback")[-1]["payload"]
    assert "unrecoverable_nested_repos" not in payload


def test_no_vcs_call_including_rollback_runs_inside_a_store_transaction(
    store, monkeypatch
):
    """MEDIUM b. The DD-10 test wrapped `init_repo`/`commit`/`mark_approved`
    only and never drove `human_reject`/`human_redo`, so the one destructive
    entry point - sitting one line above an open transaction in `human_redo` -
    was unguarded by a test named for a property of *every* vcs call."""
    seen: list[int] = []

    def wrap(name):
        real = getattr(vcs, name)

        def spy(*args, **kw):
            seen.append(store._conn._txn_depth)
            assert store._conn._txn_depth == 0, (
                f"vcs.{name} ran inside an open transaction "
                f"(depth={store._conn._txn_depth}) - see DD-10"
            )
            return real(*args, **kw)

        monkeypatch.setattr(vcs, name, spy)

    for name in ("init_repo", "commit", "mark_approved", "rollback"):
        wrap(name)

    first = add_task(store)
    loop, _ = make_loop(store, ["worker output", APPROVE])
    loop.run_task(first)
    loop.human_redo(first.id)

    second = add_task(store)
    loop.runner = MockRunner(["worker output", APPROVE])
    loop.run_task(second)
    loop.human_reject(second.id, note="no")

    # Liveness, raised to cover the destructive site: init + commit +
    # mark_approved on each of two runs, plus the two rollbacks.
    assert len(seen) >= 8
    assert seen == [0] * len(seen)


def test_a_redo_wipe_that_left_the_workspace_is_audited(store, monkeypatch):
    """MEDIUM f, at the caller. `clear_workspace` is the fallback for every
    failed rollback, and it could not report failure - so a redo that left the
    previous round on disk was invisible to `agentloop events` and the feed."""
    import agentloop.loop as loop_mod

    task = add_task(store)
    loop, _ = make_loop(store, ["worker output", APPROVE], vcs_enabled=False)
    loop.run_task(task)
    monkeypatch.setattr(loop_mod, "clear_workspace", lambda *a, **k: False)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        loop.human_redo(task.id)

    degraded = events_of_kind(store, task.id, "vcs_unavailable")
    assert [e["payload"]["op"] for e in degraded] == ["clear_workspace"]
    assert [w for w in caught if issubclass(w.category, RuntimeWarning)] != []


def test_a_redo_wipe_that_worked_is_not_audited(store):
    """Control: the ordinary wipe stays silent."""
    task = add_task(store)
    loop, _ = make_loop(store, ["worker output", APPROVE], vcs_enabled=False)
    loop.run_task(task)

    loop.human_redo(task.id)

    assert events_of_kind(store, task.id, "vcs_unavailable") == []


# ===========================================================================
# Remediation cycle 2 - reproduced by execution against the cycle-1 loop.
# ===========================================================================


def test_reject_keeps_work_a_worker_never_got_committed(store):
    """CRITICAL RR-2, at the seam a human actually reaches. `ESCALATE:`
    returns *before* C2's commit, so a worker that wrote files and then asked
    a clarifying question has files on disk and zero round commits - the
    ordinary shape, not an exotic one. Measured pre-fix: `files_removed=1`,
    `sha=""`, `reason=""` - the whole working tree deleted with nothing
    written to recover it from, audited as a clean rollback."""
    task = add_task(store)
    loop, _ = make_loop(store, ["ESCALATE: which date format?"])
    ws = ws_of(loop, task.id)
    loop.runner = WritingRunner(
        ["ESCALATE: which date format?"], ws, {"out.txt": "the worker's real work"}
    )
    loop.run_task(task)

    assert store.get_task(task.id).status == TaskStatus.NEEDS_HUMAN
    assert events_of_kind(store, task.id, "vcs_commit") == []
    assert non_git_files(ws) == ["out.txt"]

    loop.human_reject(task.id, note="wrong approach")

    assert non_git_files(ws) == []
    payload = events_of_kind(store, task.id, "vcs_rollback")[0]["payload"]
    assert payload["files_removed"] == 1
    sha = payload["discarded_sha"]
    ref = payload["discarded_ref"]
    assert git(ws, "rev-parse", "--verify", "--quiet", ref).returncode == 0
    assert git(ws, "show", f"{ref}:out.txt").stdout.strip() == "the worker's real work"
    assert sha in git(ws, "log", "--all", "--format=%H").stdout


def test_redo_keeps_the_recovery_ref_the_event_just_named(store, monkeypatch):
    """HIGH RR-3. `result.ok or not result.sha` let an **ok** result through to
    the wipe regardless of sha, and `ok=True, reason="residue"` *with* a sha is
    the production shape: `_vcs_rollback_to_base` logged `discarded_sha` and
    the next statement rmtree'd the `.git` holding that ref. The discriminator
    is "was a recovery ref written", not "did the call succeed"."""
    import agentloop.loop as loop_mod

    task = add_task(store)
    loop, _ = make_loop(store, ["worker output", APPROVE])
    ws = ws_of(loop, task.id)
    loop.runner = WritingRunner(
        ["worker output", APPROVE], ws, {"out.txt": "the round's work"}
    )
    loop.run_task(task)
    round_sha = events_of_kind(store, task.id, "vcs_commit")[0]["payload"]["sha"]

    monkeypatch.setattr(
        loop_mod.vcs,
        "rollback",
        lambda *a, **k: vcs.VcsResult(
            ok=True, reason="residue", sha=round_sha, files_removed=1
        ),
    )
    loop.human_redo(task.id)

    # The fresh start is kept by clearing the working tree, not by deleting
    # the history the event just pointed a human at.
    assert ws.is_dir()
    assert (ws / ".git").is_dir()
    assert non_git_files(ws) == []
    ref = f"{vcs.DISCARDED_REF_PREFIX}/{round_sha}"
    assert git(ws, "update-ref", ref, round_sha).returncode == 0
    assert git(ws, "show", f"{round_sha}:out.txt").stdout.strip() == "the round's work"
    assert store.get_task(task.id).status == TaskStatus.PENDING


# -- the config pin, end to end (remediation cycle 3) ------------------------


class ConfigRewritingRunner(MockRunner):
    """A worker that writes `.git/config`, and nothing outside its workspace.

    `.git` lives *inside* the workspace, so this is an ordinary file write for
    a worker - and `filter.<name>.clean` names a program git runs on the next
    `add`. The edit is made through `git config --local`, which guarantees a
    well-formed config: a hand-appended one can be malformed, and git refusing
    to parse it would produce a refusal that proves nothing."""

    def __init__(self, outputs, ws: Path, canary: Path):
        super().__init__(outputs)
        self._ws = ws
        self._canary = canary
        self._done = False

    def run(self, *args, **kw):
        if not self._done:
            self._done = True
            script = self._ws / "pwn.py"
            script.write_text(
                "import pathlib, sys\n"
                f"pathlib.Path({self._canary.as_posix()!r}).write_text('pwned')\n"
                "sys.stdout.write(sys.stdin.read())\n",
                encoding="utf-8",
            )
            cmd = f'"{Path(sys.executable).as_posix()}" "{script.as_posix()}"'
            assert (
                git(self._ws, "config", "--local", "filter.pwn.clean", cmd).returncode
                == 0
            )
            (self._ws / ".gitattributes").write_text("* filter=pwn\n", encoding="utf-8")
            (self._ws / "work.txt").write_text("payload", encoding="utf-8")
        return super().run(*args, **kw)


def test_a_worker_that_rewrites_its_git_config_gets_no_git_command_at_all(
    store, tmp_path
):
    """The wiring proof for the config pin. `vcs_enabled` defaults to **True**
    and `commit` runs every round, so without the pin an ordinary run executed
    a worker-named program - on `allow_test_exec=False`, the operator's
    explicit "execute nothing the agent wrote" switch.

    Every part of the loop's behaviour is unchanged (DD-8): the task still
    reaches DONE on the same script. What changes is that durability degrades,
    loudly and in the audit log, instead of running the worker's program."""
    canary = tmp_path / "canary.txt"
    task = add_task(store)
    loop, _ = make_loop(store, [])
    loop.runner = ConfigRewritingRunner(
        ["v1 output", APPROVE], ws_of(loop, task.id), canary
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        loop.run_task(task)

    assert canary.exists() is False, "a worker's git config executed a command"
    assert store.get_task(task.id).status == TaskStatus.DONE
    # The pin was recorded where the worker has no write path, at init.
    assert store.vcs_pin(task.id) != ""

    # C1 created the repo (the pin is minted there), and nothing after it ran.
    assert (ws_of(loop, task.id) / ".git").is_dir()
    assert events_of_kind(store, task.id, "vcs_commit") == []
    reasons = [
        (e["payload"]["op"], e["payload"]["reason"])
        for e in events_of_kind(store, task.id, "vcs_unavailable")
    ]
    assert reasons == [
        ("commit", "config-changed"),
        ("mark_approved", "config-changed"),
    ]
    assert any("config-changed" in str(w.message) for w in caught)


def test_a_redo_over_a_rewritten_config_wipes_instead_of_running_git(store, tmp_path):
    """The destructive caller, end to end. `human_redo` reaches `rollback`,
    which runs `reset --hard` and `clean -ffdqx` - the two commands that would
    hand a worker's `filter.*.clean` the whole workspace. Refused, so redo
    takes the documented fallback (`clear_workspace`) and the contract it owes
    its caller - a fresh start - still holds."""
    canary = tmp_path / "canary.txt"
    task = add_task(store)
    loop, _ = make_loop(store, [])
    ws = ws_of(loop, task.id)
    loop.runner = ConfigRewritingRunner(["v1 output", APPROVE], ws, canary)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        loop.run_task(task)
    assert (ws / "work.txt").exists()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        loop.human_redo(task.id, note="fresh start")

    assert canary.exists() is False
    assert non_git_files(ws) == []
    assert ws.exists() is False, "redo owes its caller an empty workspace"
    assert store.get_task(task.id).status == TaskStatus.PENDING
    assert any(
        e["payload"]["reason"] == "config-changed"
        for e in events_of_kind(store, task.id, "vcs_unavailable")
    )
    assert any("config-changed" in str(w.message) for w in caught)
