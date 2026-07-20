"""End-to-end tests of the loop using MockRunner (no API keys)."""

import sys
from pathlib import Path

import pytest

from agentloop.config import LoopConfig
from agentloop.loop import Loop
from agentloop.models import Task, TaskStatus
from agentloop.registry import Registry
from agentloop.runner import MockRunner
from agentloop.store import Store


APPROVE = "VERDICT: approve CONFIDENCE: 0.92 TESTS: pass\nMeets all criteria."
REVISE = ("VERDICT: revise CONFIDENCE: 0.55 TESTS: fail\n"
          "Edge case for empty input is not handled; add a guard and a test.")
SEVERE = ("VERDICT: escalate CONFIDENCE: 0.10 TESTS: fail\n"
          "Fundamentally wrong approach; solves a different problem.")


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def make_loop(store, outputs, **cfg_overrides):
    # Workspaces live beside the test db so a run never touches the real repo,
    # and test execution is off unless a test explicitly opts in.
    cfg_overrides.setdefault(
        "workspace_root", str(Path(store.db_path).parent / "ws"))
    cfg_overrides.setdefault("allow_test_exec", False)
    config = LoopConfig(db_path=store.db_path, **cfg_overrides)
    runner = MockRunner(outputs)
    return Loop(store, runner, Registry.load(), config), runner


def add_task(store, risk=1) -> Task:
    task = Task(id=None, title="Add slugify util",
                goal="Write a slugify(text) function.",
                acceptance_criteria="Lowercase, hyphen-separated, tested.",
                risk_level=risk)
    store.add_task(task)
    return task


def test_happy_path_approved_first_try(store):
    task = add_task(store)
    loop, runner = make_loop(store, ["def slugify(...): ...", APPROVE])
    loop.run_task(task)

    assert task.status == TaskStatus.DONE
    m = store.task_metrics(task.id)
    assert m["attempts"] == 2  # worker + validator
    assert m["verdicts"][0]["kind"] == "approve"
    assert m["tokens"] > 0
    # Validator saw the worker's output, not its own context
    assert "def slugify" in runner.calls[1]["prompt"]


def test_revise_then_approve(store):
    task = add_task(store)
    loop, runner = make_loop(
        store, ["v1 output", REVISE, "v2 output fixed", APPROVE])
    loop.run_task(task)

    assert task.status == TaskStatus.DONE
    assert task.revision_count == 1
    # Revision prompt carried the validator's feedback to the worker
    assert "empty input" in runner.calls[2]["prompt"]
    kinds = [v["kind"] for v in store.task_metrics(task.id)["verdicts"]]
    assert kinds == ["revise", "approve"]


def test_bounded_retries_then_escalate(store):
    task = add_task(store)
    outputs = ["v1", REVISE, "v2", REVISE, "v3", REVISE]
    loop, _ = make_loop(store, outputs, max_revisions=2)
    loop.run_task(task)

    assert task.status == TaskStatus.NEEDS_HUMAN
    assert "Exhausted 2 revisions" in task.escalation_reason


def test_severe_disagreement_escalates_immediately(store):
    task = add_task(store)
    loop, _ = make_loop(store, ["bad output", SEVERE])
    loop.run_task(task)

    assert task.status == TaskStatus.NEEDS_HUMAN
    assert task.revision_count == 0  # no revision loop on severe disagreement
    assert "Severe disagreement" in task.escalation_reason


def test_low_confidence_approve_is_not_done(store):
    # approve verdict below the 0.70 threshold must NOT complete the task
    task = add_task(store)
    weak = "VERDICT: approve CONFIDENCE: 0.60 TESTS: pass\nProbably fine."
    loop, _ = make_loop(store, ["out", weak, "out2", APPROVE])
    loop.run_task(task)
    assert task.status == TaskStatus.DONE
    assert task.revision_count == 1  # forced one revision first


def test_worker_ambiguity_escalates(store):
    task = add_task(store)
    loop, _ = make_loop(
        store, ["ESCALATE: which locale rules should slugify follow?"])
    loop.run_task(task)
    assert task.status == TaskStatus.NEEDS_HUMAN
    assert "locale" in task.escalation_reason


def test_high_risk_needs_human_signoff_then_approve(store):
    task = add_task(store, risk=2)
    loop, _ = make_loop(store, ["out", APPROVE])
    loop.run_task(task)
    assert task.status == TaskStatus.NEEDS_HUMAN
    assert "sign-off" in task.escalation_reason

    loop.human_approve(task.id, note="LGTM")
    assert store.get_task(task.id).status == TaskStatus.DONE


def test_human_redo_resets_context(store):
    task = add_task(store)
    loop, _ = make_loop(store, ["bad", SEVERE])
    loop.run_task(task)
    assert task.status == TaskStatus.NEEDS_HUMAN

    loop.human_redo(task.id, note="start over")
    fresh = store.get_task(task.id)
    assert fresh.status == TaskStatus.PENDING
    assert fresh.output == "" and fresh.revision_count == 0
    # Audit trail of the failed run is preserved (spec §11)
    kinds = [e["kind"] for e in store.events(task.id)]
    assert "human_redo" in kinds and "verdict" in kinds


def test_budget_cap_trips_to_human(store):
    task = add_task(store)
    loop, _ = make_loop(store, ["v1", REVISE, "v2", REVISE],
                        max_tokens_per_task=10)  # tiny cap
    loop.run_task(task)
    assert task.status == TaskStatus.NEEDS_HUMAN
    assert "Budget cap" in task.escalation_reason


def test_resumability_from_store(store):
    """Loop picks up pending tasks from the store after a 'restart'."""
    add_task(store)
    add_task(store)
    loop, _ = make_loop(store, ["o1", APPROVE, "o2", APPROVE])
    n = loop.run()
    assert n == 2
    assert all(t.status == TaskStatus.DONE for t in store.list_tasks())


def test_unparseable_verdict_escalates(store):
    task = add_task(store)
    loop, _ = make_loop(store, ["out", "looks good to me!"])
    loop.run_task(task)
    assert task.status == TaskStatus.NEEDS_HUMAN


def test_memory_tiers_and_audit(store):
    store.memory_write("loop", "test_command", "pytest -q", approved=True)
    store.memory_write("project", "sketchy_fact", "maybe wrong")  # unapproved
    assert store.memory_read("loop", "test_command") == "pytest -q"
    assert store.memory_read("project", "sketchy_fact") is None  # gated
    kinds = [e["kind"] for e in store.events()]
    assert kinds.count("memory_write") == 2  # writes are auditable


# -- executed tests are authoritative (spec §5) ------------------------------

def make_testing_loop(store, tmp_path, outputs, files: dict[str, str],
                      **overrides):
    """A loop whose workspace really contains tests, with execution enabled."""
    ws_root = tmp_path / "ws"
    task_ws = ws_root / "task-1"
    task_ws.mkdir(parents=True)
    for name, body in files.items():
        (task_ws / name).write_text(body, encoding="utf-8")
    return make_loop(store, outputs, workspace_root=str(ws_root),
                     allow_test_exec=True,
                     test_command=f"{sys.executable} -m pytest -q",
                     **overrides)


PASSING = "def test_ok():\n    assert True\n"
FAILING = "def test_bad():\n    assert False\n"


def test_executed_failure_blocks_a_confident_approval(store, tmp_path):
    """The core rule change: a validator can no longer approve past tests that
    actually fail."""
    task = add_task(store)
    loop, _ = make_testing_loop(
        store, tmp_path, ["out", APPROVE, "out2", APPROVE],
        {"test_bad.py": FAILING}, max_revisions=1)
    loop.run_task(task)

    assert task.status == TaskStatus.NEEDS_HUMAN
    assert task.revision_count == 1        # it revised rather than completing


def test_executed_pass_allows_approval(store, tmp_path):
    task = add_task(store)
    loop, _ = make_testing_loop(store, tmp_path, ["out", APPROVE],
                                {"test_ok.py": PASSING})
    loop.run_task(task)
    assert task.status == TaskStatus.DONE


def test_validator_test_claim_mismatch_is_recorded(store, tmp_path):
    task = add_task(store)
    loop, _ = make_testing_loop(
        store, tmp_path, ["out", APPROVE, "out2", APPROVE],
        {"test_bad.py": FAILING}, max_revisions=1)
    loop.run_task(task)

    mismatches = [e for e in store.events(task.id)
                  if e["kind"] == "test_disagreement"]
    assert mismatches, "validator claiming pass over a real fail must be logged"
    assert mismatches[0]["payload"]["validator_claimed"] is True
    assert mismatches[0]["payload"]["actual"] is False


def test_real_test_results_are_stored_and_reach_the_validator(store, tmp_path):
    task = add_task(store)
    loop, runner = make_testing_loop(store, tmp_path, ["out", APPROVE],
                                     {"test_ok.py": PASSING})
    loop.run_task(task)

    runs = store.test_runs(task.id)
    assert runs and runs[0]["status"] == "pass"
    assert runs[0]["exit_code"] == 0
    validator_prompt = runner.calls[1]["prompt"]
    assert "Executed test results (authoritative)" in validator_prompt


def test_no_workspace_falls_back_to_the_validator_claim(store, tmp_path):
    """With nothing to execute, the old behavior stands — 'na' is not a fail."""
    task = add_task(store)
    loop, _ = make_loop(store, ["out", APPROVE],
                        workspace_root=str(tmp_path / "empty"),
                        allow_test_exec=True)
    loop.run_task(task)
    assert task.status == TaskStatus.DONE


def test_redo_wipes_the_workspace(store, tmp_path):
    task = add_task(store)
    loop, _ = make_testing_loop(store, tmp_path, ["out", SEVERE],
                                {"test_ok.py": PASSING})
    loop.run_task(task)
    stale = tmp_path / "ws" / "task-1" / "test_ok.py"
    assert stale.exists()

    loop.human_redo(task.id)
    assert not stale.exists(), "a redo must not inherit the old attempt's files"


# -- memory and tools reach the agents ---------------------------------------

def test_approved_memory_is_injected_into_prompts(store):
    store.memory_write("loop", "test_command", "pytest -q", approved=True)
    store.memory_write("project", "unvetted", "do not trust me")

    task = add_task(store)
    loop, runner = make_loop(store, ["out", APPROVE])
    loop.run_task(task)

    worker_prompt = runner.calls[0]["prompt"]
    assert "Known project facts" in worker_prompt
    assert "pytest -q" in worker_prompt
    assert "do not trust me" not in worker_prompt   # gating holds end-to-end


def test_registry_tools_are_passed_to_the_runner(store):
    task = add_task(store)
    loop, runner = make_loop(store, ["out", APPROVE])
    loop.run_task(task)

    assert "file_io" in runner.calls[0]["tools"]     # worker
    assert "git" in runner.calls[0]["tools"]
    assert "git" not in runner.calls[1]["tools"]     # validator: read-only


def test_worker_is_told_where_its_workspace_is(store):
    task = add_task(store)
    loop, runner = make_loop(store, ["out", APPROVE])
    loop.run_task(task)
    assert "task-1" in runner.calls[0]["prompt"]
