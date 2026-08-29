"""End-to-end tests of the loop using MockRunner (no API keys)."""

import sys
from pathlib import Path

import pytest

from agentloop.agents import _MAX_TOOL_INPUT_CHARS
from agentloop import loop as loop_module
from agentloop.config import LoopConfig
from agentloop.executor import TestExecutor
from agentloop.loop import Loop
from agentloop.models import RunResult, Task, TaskStatus
from agentloop.registry import Registry
from agentloop.runner import MockRunner
from agentloop.store import Store


APPROVE = "VERDICT: approve CONFIDENCE: 0.92 TESTS: pass\nMeets all criteria."
REVISE = (
    "VERDICT: revise CONFIDENCE: 0.55 TESTS: fail\n"
    "Edge case for empty input is not handled; add a guard and a test."
)
SEVERE = (
    "VERDICT: escalate CONFIDENCE: 0.10 TESTS: fail\n"
    "Fundamentally wrong approach; solves a different problem."
)


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def make_loop(store, outputs, **cfg_overrides):
    # Workspaces live beside the test db so a run never touches the real repo,
    # and test execution is off unless a test explicitly opts in.
    cfg_overrides.setdefault("workspace_root", str(Path(store.db_path).parent / "ws"))
    cfg_overrides.setdefault("allow_test_exec", False)
    # Slice 6: git is off in the loop tests. A repo per workspace would
    # spawn real subprocesses in ~300 tests that are not about durability.
    cfg_overrides.setdefault("vcs_enabled", False)
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
    loop, runner = make_loop(store, ["v1 output", REVISE, "v2 output fixed", APPROVE])
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
    loop, _ = make_loop(store, ["ESCALATE: which locale rules should slugify follow?"])
    loop.run_task(task)
    assert task.status == TaskStatus.NEEDS_HUMAN
    assert "locale" in task.escalation_reason


@pytest.mark.parametrize("empty", ["", "   ", "\n\n\t "])
def test_empty_worker_output_escalates_and_is_never_validated(store, empty):
    """An empty worker output is the absence of work, not work.

    Validating it would review a blank output against criteria it cannot check,
    and an approve there marks the task DONE — which, under the task graph,
    releases dependents against upstream output that does not exist.
    """
    task = add_task(store)
    # APPROVE is scripted but must never be consumed: the loop has to stop at
    # the worker, so the validator is never invoked at all.
    loop, runner = make_loop(store, [empty, APPROVE])
    loop.run_task(task)

    assert task.status == TaskStatus.NEEDS_HUMAN
    assert "empty output" in task.escalation_reason
    assert task.revision_count == 0  # not a quality gap; not a revision
    # The validator never ran, so its scripted line is still queued.
    assert len(runner.calls) == 1
    assert runner.outputs == [APPROVE]
    assert store.task_metrics(task.id)["verdicts"] == []


def test_empty_output_escalation_does_not_release_dependents(store):
    """The reason this rule is load-bearing rather than cosmetic."""
    parent = add_task(store)
    child = add_task(store)
    store.add_dependency(child.id, parent.id)

    loop, _ = make_loop(store, ["", APPROVE])
    loop.run_task(parent)
    assert parent.status == TaskStatus.NEEDS_HUMAN

    # The dependent stays unclaimable: `done` is what satisfies a dependency,
    # and an empty parent never reached it.
    assert loop.store.claim_next_task("w") is None


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


def test_a_redone_task_is_claimable_again(store):
    """`agentloop redo` on a task the loop had claimed must hand it back to the
    queue for real. Nothing else in the store clears `claimed_by`, so a redo that
    only wrote the status left the row `pending` and still leased — and the claim's
    compare-and-swap can never match that."""
    task = add_task(store)
    loop, _ = make_loop(store, ["bad", SEVERE])
    claimed = store.claim_next_task("loop")
    loop.run_task(claimed)
    assert store.get_task(task.id).status == TaskStatus.NEEDS_HUMAN

    loop.human_redo(task.id)
    assert store.get_task(task.id).claimed_by is None
    reclaimed = store.claim_next_task("loop")
    assert reclaimed is not None and reclaimed.id == task.id


def test_a_stuck_lease_does_not_starve_other_pending_tasks(store):
    """The stuck row still matches the claim's SELECT, so it is re-picked and
    fails the swap on every one of the 100 attempts — starving every clean
    pending task behind it. Two tasks make that visible: without the release the
    second one is never handed out either."""
    held = add_task(store)
    clean = add_task(store)
    loop, _ = make_loop(store, ["bad", SEVERE])
    claimed = store.claim_next_task("loop")
    assert claimed.id == held.id
    loop.run_task(claimed)

    loop.human_redo(held.id)
    first = store.claim_next_task("loop")
    assert first is not None, "the released task starved the whole queue"
    # A second claim id, because one id resumes the in-flight work it already
    # owns rather than reaching past it.
    second = store.claim_next_task("loop-2")
    assert second is not None
    assert {first.id, second.id} == {held.id, clean.id}


def test_budget_cap_trips_to_human(store):
    task = add_task(store)
    loop, _ = make_loop(
        store, ["v1", REVISE, "v2", REVISE], max_tokens_per_task=10
    )  # tiny cap
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


# -- infra error handling (0d): transient failures don't kill the batch ------


def test_infra_error_escalates_and_the_batch_continues(store):
    """A runner that keeps raising exhausts retries -> the task lands in
    NEEDS_HUMAN with an infra_error reason, an infra_error event is logged, and
    the next pending task still runs."""
    add_task(store)  # task 1: its worker call always fails
    add_task(store)  # task 2: healthy, must still complete
    boom = RuntimeError("API 503")
    # retries=1 -> two failing worker calls exhaust it; then task 2 succeeds.
    loop, _ = make_loop(
        store, [boom, boom, "task2 output", APPROVE], infra_max_retries=1
    )
    processed = loop.run()

    assert processed == 2
    t1, t2 = store.list_tasks()
    assert t1.status == TaskStatus.NEEDS_HUMAN
    assert "infra_error" in t1.escalation_reason
    assert "API 503" in t1.escalation_reason
    assert t2.status == TaskStatus.DONE  # a flaky call didn't abort the run

    kinds = [e["kind"] for e in store.events(t1.id)]
    assert "infra_error" in kinds


def test_infra_error_is_not_a_revision(store):
    """A transient failure retried to success does not consume a revision."""
    task = add_task(store)
    boom = RuntimeError("network blip")
    loop, _ = make_loop(store, [boom, "worker output", APPROVE], infra_max_retries=2)
    loop.run_task(task)

    assert task.status == TaskStatus.DONE
    assert task.revision_count == 0  # retry != revise


# The stage tests above cover the *worker* call only. `_with_retry` wraps three
# more call sites, and "a retry is not a revision" is a claim about the helper,
# not about one caller. The two below pin the other two stages a task passes
# through in a normal round, and the third pins the backoff the helper applies
# between attempts — which no existing test could see, because the default
# `infra_retry_backoff_s` is 0.0 and the sleep is skipped entirely.


def _record_sleeps(monkeypatch) -> list[float]:
    """Attach a recorder to the sleep the retry loop actually calls.

    `loop.py` binds `import time` as a module, so the live call site is
    `agentloop.loop.time.sleep` and patching it there is patching the one the
    production branch reaches."""
    delays: list[float] = []
    monkeypatch.setattr(loop_module.time, "sleep", delays.append)
    return delays


class _FlakyExecutor(TestExecutor):
    """A TestExecutor whose first `run` raises, then delegates to the real one.

    `Loop(..., executor=…)` is the existing injection seam; nothing is patched."""

    __test__ = False

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.calls = 0

    def run(self, workspace):
        self.calls += 1
        if self.calls == 1:
            raise OSError("sandbox spawn failed")
        return super().run(workspace)


def test_infra_error_at_the_validator_stage_is_not_a_revision(store):
    """A transient failure of the *validator* call is retried to success and
    does not consume a revision — the worker is not re-run and the verdict that
    lands is the one the retry produced."""
    task = add_task(store)
    loop, _ = make_loop(
        store,
        ["worker output", RuntimeError("API 503"), APPROVE],
        infra_max_retries=2,
    )
    loop.run_task(task)

    assert task.status == TaskStatus.DONE
    assert task.revision_count == 0  # retry != revise
    stages = [
        e["payload"]["stage"]
        for e in store.events(task.id)
        if e["kind"] == "infra_error"
    ]
    assert stages == ["validator"]
    # One worker attempt, and the validator attempt that finally returned.
    assert store.task_metrics(task.id)["verdicts"][0]["kind"] == "approve"


def test_infra_error_at_the_executor_stage_is_not_a_revision(store, tmp_path):
    """A transient failure of the *executor* call is retried to success, does
    not consume a revision, and the retried run's result is still recorded as a
    test_run row — the retry replaces the failed call, it does not skip it."""
    task = add_task(store)
    config = LoopConfig(
        db_path=store.db_path,
        workspace_root=str(tmp_path / "ws"),
        allow_test_exec=False,
        infra_max_retries=2,
    )
    flaky = _FlakyExecutor(enabled=False)
    loop = Loop(
        store,
        MockRunner(["worker output", APPROVE]),
        Registry.load(),
        config,
        executor=flaky,
    )
    loop.run_task(task)

    assert task.status == TaskStatus.DONE
    assert task.revision_count == 0  # retry != revise
    stages = [
        e["payload"]["stage"]
        for e in store.events(task.id)
        if e["kind"] == "infra_error"
    ]
    assert stages == ["executor"]
    assert flaky.calls == 2  # raised once, then delegated
    assert len(store.test_runs(task.id)) == 1


def test_infra_retry_backoff_is_bounded_and_exponential(store, monkeypatch):
    """The backoff between retries doubles, is bounded by `infra_max_retries`,
    and is skipped entirely when the knob is 0.

    The zero-backoff half is the control: it varies the *config* so the other
    side of `if delay > 0` runs, which is what proves the recorder is attached
    to the live call site rather than passing vacuously."""
    delays = _record_sleeps(monkeypatch)
    task = add_task(store)
    boom = RuntimeError("API 503")
    loop, _ = make_loop(
        store,
        [boom] * 4,
        infra_max_retries=3,
        infra_retry_backoff_s=0.01,
    )
    loop.run_task(task)

    assert task.status == TaskStatus.NEEDS_HUMAN
    assert delays == [0.01, 0.02, 0.04]  # doubling, one per retry, then give up
    events = [e for e in store.events(task.id) if e["kind"] == "infra_error"]
    assert len(events) == 4  # infra_max_retries + 1 attempts, all logged
    assert [e["payload"]["attempt"] for e in events] == [1, 2, 3, 4]

    # Control: same scenario, backoff turned off -> the sleep never runs.
    delays.clear()
    other = add_task(store)
    loop2, _ = make_loop(
        store,
        [boom] * 4,
        infra_max_retries=3,
        infra_retry_backoff_s=0.0,
    )
    loop2.run_task(other)

    assert other.status == TaskStatus.NEEDS_HUMAN
    assert delays == []
    assert len([e for e in store.events(other.id) if e["kind"] == "infra_error"]) == 4


def test_relevant_memory_is_retrieved_into_the_worker_prompt(store):
    """Slice 2: the facts injected are the ones about *this* task, and the
    retrieval is attributable in the audit log."""
    store.memory_write("project", "slugify_rule", "slugify uses hyphens", approved=True)
    store.memory_write("project", "billing_rule", "invoices run monthly", approved=True)

    task = add_task(store)
    loop, runner = make_loop(
        store, ["output", APPROVE], memory_retrieval_backend="hash"
    )
    loop.run_task(task)

    prompt = runner.calls[0]["prompt"]
    assert prompt.index("slugify_rule") < prompt.index("billing_rule")

    retrievals = [e for e in store.events(task.id) if e["kind"] == "retrieval"]
    assert len(retrievals) == 2  # worker and validator each retrieve
    facts = {f["key"]: f["score"] for f in retrievals[0]["payload"]["facts"]}
    assert facts["slugify_rule"] > facts["billing_rule"]


def test_retrieval_can_be_disabled_without_losing_memory_injection(store):
    store.memory_write("project", "a_rule", "some fact", approved=True)
    task = add_task(store)
    loop, runner = make_loop(
        store, ["output", APPROVE], memory_retrieval_backend="none"
    )
    loop.run_task(task)

    assert "a_rule" in runner.calls[0]["prompt"]
    assert [e for e in store.events(task.id) if e["kind"] == "retrieval"] == []


def test_retrieval_is_attributed_to_the_attempt_that_used_the_facts(store):
    """One task retrieves once per agent per round. Without attempt_id and
    agent_kind those events are indistinguishable from each other, which is
    exactly what makes provenance useless — and `events` is append-only, so
    there is no backfill for the ones already written."""
    store.memory_write("project", "slugify_rule", "slugify uses hyphens", approved=True)
    task = add_task(store)
    loop, _ = make_loop(
        store,
        ["first output", REVISE, "second output", APPROVE],
        memory_retrieval_backend="hash",
    )
    loop.run_task(task)

    kinds = {
        r["id"]: r["kind"]
        for r in store._conn.execute(
            "SELECT id, kind FROM attempts WHERE task_id=? ORDER BY id", (task.id,)
        ).fetchall()
    }
    retrievals = [e for e in store.events(task.id) if e["kind"] == "retrieval"]
    assert [e["payload"]["agent_kind"] for e in retrievals] == [
        "worker",
        "validator",
        "worker",
        "validator",
    ]
    # The attempt_id is a real attempt row, and it is the attempt of that agent.
    for ev in retrievals:
        payload = ev["payload"]
        assert kinds[payload["attempt_id"]] == payload["agent_kind"]
        assert payload["role"]
    assert len({e["payload"]["attempt_id"] for e in retrievals}) == 4


def test_worker_and_validator_retrieve_against_different_queries(store):
    """The validator ranks facts against the output under review, the worker
    against the feedback it must address — a single per-task retrieval record
    would lose that difference entirely."""
    store.memory_write("project", "slugify_rule", "slugify uses hyphens", approved=True)
    task = add_task(store)
    loop, _ = make_loop(
        store,
        ["first output", REVISE, "second output", APPROVE],
        memory_retrieval_backend="hash",
    )
    loop.run_task(task)

    by_kind: dict[str, list[str]] = {"worker": [], "validator": []}
    for ev in store.events(task.id):
        if ev["kind"] == "retrieval":
            by_kind[ev["payload"]["agent_kind"]].append(ev["payload"]["query"])

    assert "first output" in by_kind["validator"][0]  # the output under review
    assert "first output" not in by_kind["worker"][0]
    assert "Edge case for empty input" in by_kind["worker"][1]  # the feedback


def test_tool_calls_are_recorded_as_provenance(store):
    """Registry tools already reach the SDK, so agents really do call them —
    slice 2 makes that auditable instead of invisible."""
    worker = RunResult(
        output="wrote the file",
        tool_calls=[
            {"tool": "Write", "input": {"file_path": "slugify.py"}},
            {"tool": "Bash", "input": {"command": "pytest -q"}},
        ],
    )
    task = add_task(store)
    loop, _ = make_loop(store, [worker, APPROVE])
    loop.run_task(task)

    calls = [e for e in store.events(task.id) if e["kind"] == "tool_call"]
    assert [c["payload"]["tool"] for c in calls] == ["Write", "Bash"]
    assert calls[0]["payload"]["agent_kind"] == "worker"
    # Recorded as a bounded string: the log says what a tool was called with,
    # and can never itself be the thing that fails the attempt.
    assert calls[0]["payload"]["input"] == '{"file_path": "slugify.py"}'
    # Attributable to the attempt that made them.
    assert calls[0]["payload"]["attempt_id"] == calls[1]["payload"]["attempt_id"]
    assert task.status == TaskStatus.DONE


def test_a_run_without_tool_use_logs_no_tool_call_events(store):
    task = add_task(store)
    loop, _ = make_loop(store, ["plain output", APPROVE])
    loop.run_task(task)
    assert [e for e in store.events(task.id) if e["kind"] == "tool_call"] == []


def test_memory_tiers_and_audit(store):
    store.memory_write("loop", "test_command", "pytest -q", approved=True)
    store.memory_write("project", "sketchy_fact", "maybe wrong")  # unapproved
    assert store.memory_read("loop", "test_command") == "pytest -q"
    assert store.memory_read("project", "sketchy_fact") is None  # gated
    kinds = [e["kind"] for e in store.events()]
    assert kinds.count("memory_write") == 2  # writes are auditable


# -- executed tests are authoritative (spec §5) ------------------------------


def make_testing_loop(store, tmp_path, outputs, files: dict[str, str], **overrides):
    """A loop whose workspace really contains tests, with execution enabled."""
    ws_root = tmp_path / "ws"
    task_ws = ws_root / "task-1"
    task_ws.mkdir(parents=True)
    for name, body in files.items():
        (task_ws / name).write_text(body, encoding="utf-8")
    return make_loop(
        store,
        outputs,
        workspace_root=str(ws_root),
        allow_test_exec=True,
        test_command=f"{sys.executable} -m pytest -q",
        **overrides,
    )


PASSING = "def test_ok():\n    assert True\n"
FAILING = "def test_bad():\n    assert False\n"


def test_executed_failure_blocks_a_confident_approval(store, tmp_path):
    """The core rule change: a validator can no longer approve past tests that
    actually fail."""
    task = add_task(store)
    loop, _ = make_testing_loop(
        store,
        tmp_path,
        ["out", APPROVE, "out2", APPROVE],
        {"test_bad.py": FAILING},
        max_revisions=1,
    )
    loop.run_task(task)

    assert task.status == TaskStatus.NEEDS_HUMAN
    assert task.revision_count == 1  # it revised rather than completing


def test_executed_pass_allows_approval(store, tmp_path):
    task = add_task(store)
    loop, _ = make_testing_loop(
        store, tmp_path, ["out", APPROVE], {"test_ok.py": PASSING}
    )
    loop.run_task(task)
    assert task.status == TaskStatus.DONE


def test_validator_test_claim_mismatch_is_recorded(store, tmp_path):
    task = add_task(store)
    loop, _ = make_testing_loop(
        store,
        tmp_path,
        ["out", APPROVE, "out2", APPROVE],
        {"test_bad.py": FAILING},
        max_revisions=1,
    )
    loop.run_task(task)

    mismatches = [e for e in store.events(task.id) if e["kind"] == "test_disagreement"]
    assert mismatches, "validator claiming pass over a real fail must be logged"
    assert mismatches[0]["payload"]["validator_claimed"] is True
    assert mismatches[0]["payload"]["actual"] is False


def test_real_test_results_are_stored_and_reach_the_validator(store, tmp_path):
    task = add_task(store)
    loop, runner = make_testing_loop(
        store, tmp_path, ["out", APPROVE], {"test_ok.py": PASSING}
    )
    loop.run_task(task)

    runs = store.test_runs(task.id)
    assert runs and runs[0]["status"] == "pass"
    assert runs[0]["exit_code"] == 0
    validator_prompt = runner.calls[1]["prompt"]
    assert "Executed test results (authoritative)" in validator_prompt


def test_no_workspace_falls_back_to_the_validator_claim(store, tmp_path):
    """With nothing to execute, the old behavior stands — 'na' is not a fail."""
    task = add_task(store)
    loop, _ = make_loop(
        store,
        ["out", APPROVE],
        workspace_root=str(tmp_path / "empty"),
        allow_test_exec=True,
    )
    loop.run_task(task)
    assert task.status == TaskStatus.DONE


def test_redo_wipes_the_workspace(store, tmp_path):
    task = add_task(store)
    loop, _ = make_testing_loop(
        store, tmp_path, ["out", SEVERE], {"test_ok.py": PASSING}
    )
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
    assert "do not trust me" not in worker_prompt  # gating holds end-to-end


def test_registry_tools_are_passed_to_the_runner(store):
    task = add_task(store)
    loop, runner = make_loop(store, ["out", APPROVE])
    loop.run_task(task)

    assert "file_io" in runner.calls[0]["tools"]  # worker
    assert "git" in runner.calls[0]["tools"]
    assert "git" not in runner.calls[1]["tools"]  # validator: read-only


def test_worker_is_told_where_its_workspace_is(store):
    task = add_task(store)
    loop, runner = make_loop(store, ["out", APPROVE])
    loop.run_task(task)
    assert "task-1" in runner.calls[0]["prompt"]


# -- context-budget handoff (Slice 1) ----------------------------------------


def _small_worker_budget_registry(worker_budget: int) -> Registry:
    """Registry whose worker has a tiny context budget so a single revision's
    accumulated spend trips the handoff threshold. Keeps the summarizer role."""
    from agentloop.models import AgentSpec
    from agentloop.registry import DEFAULT_AGENTS

    agents = dict(DEFAULT_AGENTS)
    w = agents["worker"]
    agents["worker"] = AgentSpec(
        role=w.role,
        model=w.model,
        system_prompt=w.system_prompt,
        tools=list(w.tools),
        context_budget_tokens=worker_budget,
        version=w.version,
    )
    return Registry(agents)


HANDOFF_SUMMARY = "SUMMARY: prior work compacted for a fresh worker instance."


def test_context_budget_handoff_compacts_and_completes(store, tmp_path):
    """Once the worker's accumulated context passes context_handoff_ratio of its
    budget, the loop summarizes the state and restarts the worker with the
    summary in place of the raw transcript, and the task still completes."""
    task = add_task(store)
    registry = _small_worker_budget_registry(worker_budget=40)
    config = LoopConfig(
        db_path=store.db_path,
        workspace_root=str(tmp_path / "ws"),
        allow_test_exec=False,
    )
    runner = MockRunner(
        [
            "worker1 raw output that must not reappear verbatim",
            REVISE,
            HANDOFF_SUMMARY,  # the summarizer's compacted state
            "worker2 output fixed",
            APPROVE,
        ]
    )
    loop = Loop(store, runner, registry, config)
    loop.run_task(task)

    assert task.status == TaskStatus.DONE
    # A handoff is not a revision: it must not consume the revision budget.
    assert task.revision_count == 1

    # 1. the handoff event fired, before/after token counts recorded, and the
    #    compaction actually shrank the context.
    handoffs = [e for e in store.events(task.id) if e["kind"] == "context_handoff"]
    assert handoffs, "a context_handoff event must fire once the budget is tripped"
    payload = handoffs[0]["payload"]
    assert payload["before_tokens"] > payload["after_tokens"]

    # 2. the summarizer was handed the real prior work + validator feedback.
    summarizer_prompt = runner.calls[2]["prompt"]
    assert "worker1 raw output" in summarizer_prompt
    assert "empty input" in summarizer_prompt  # the REVISE feedback

    # 3. the fresh worker prompt carried the SUMMARY, not the raw transcript.
    fresh_worker_prompt = runner.calls[3]["prompt"]
    assert "prior work compacted" in fresh_worker_prompt
    assert "worker1 raw output" not in fresh_worker_prompt


def test_no_handoff_when_context_stays_under_budget(store, tmp_path):
    """Below the threshold, no summarizer call is made and no handoff event is
    logged — the feature is inert on ordinary small tasks."""
    task = add_task(store)
    # Default worker budget (120k) is far above anything a mock run accumulates.
    loop, runner = make_loop(store, ["v1", REVISE, "v2 output", APPROVE])
    loop.run_task(task)

    assert task.status == TaskStatus.DONE
    assert not [e for e in store.events(task.id) if e["kind"] == "context_handoff"]
    # Exactly worker, validator, worker, validator — no summarizer slipped in.
    assert len(runner.calls) == 4
    # The ordinary revision path still carried the raw feedback, not a summary.
    assert "empty input" in runner.calls[2]["prompt"]


def test_a_non_serialisable_tool_input_still_finishes_the_attempt(store):
    """Telemetry must never fail an attempt. A tool input `json.dumps` cannot
    encode used to raise inside `_invoke`'s closing transaction, rolling back
    `finish_attempt` — the output, tokens and cost of a model call already paid
    for — after which `_with_retry` ran the same call again. `ClaudeSDKRunner`
    sanitises, but the ModelRunner seam does not require it and slice 4 adds a
    second runner."""
    worker = RunResult(
        output="wrote the file",
        tool_calls=[{"tool": "Write", "input": {"handle": object()}}],
        tokens_in=11,
        tokens_out=7,
    )
    task = add_task(store)
    loop, runner = make_loop(store, [worker, APPROVE])
    loop.run_task(task)

    assert task.status == TaskStatus.DONE
    assert len(runner.calls) == 2  # worker + validator: nothing was re-run
    # The attempt finished: its tokens were counted, so its row was committed
    # rather than rolled back by the telemetry that follows it.
    assert store.attempt_tokens(task.id, "worker") == 18
    assert task.output == "wrote the file"

    call = [e for e in store.events(task.id) if e["kind"] == "tool_call"][0]
    assert call["payload"]["tool"] == "Write"
    assert "object object at" in call["payload"]["input"]


class _Unrepresentable:
    """A tool argument that defeats both `json.dumps` and `repr`."""

    def __repr__(self):
        raise RuntimeError("no repr for you")


def test_a_tool_input_whose_repr_raises_still_finishes_the_attempt(store):
    """`repr` is the fallback, not a guarantee: a `__repr__` that raises would
    land back inside the very transaction the coercion exists to protect."""
    worker = RunResult(
        output="done", tool_calls=[{"tool": "X", "input": _Unrepresentable()}]
    )
    task = add_task(store)
    loop, runner = make_loop(store, [worker, APPROVE])
    loop.run_task(task)

    assert task.status == TaskStatus.DONE
    assert len(runner.calls) == 2  # nothing re-run
    call = [e for e in store.events(task.id) if e["kind"] == "tool_call"][0]
    assert call["payload"]["input"] == "<unrepresentable _Unrepresentable>"


def test_an_oversized_tool_input_is_truncated(store):
    """The audit log records what a tool was called with, not a copy of its
    payload — a megabyte of file content in an event row buys nothing."""
    worker = RunResult(
        output="done",
        tool_calls=[{"tool": "Write", "input": {"content": "x" * 10_000}}],
    )
    task = add_task(store)
    loop, _ = make_loop(store, [worker, APPROVE])
    loop.run_task(task)

    call = [e for e in store.events(task.id) if e["kind"] == "tool_call"][0]
    assert len(call["payload"]["input"]) <= _MAX_TOOL_INPUT_CHARS


def test_a_non_serialisable_tool_name_still_finishes_the_attempt(store):
    """The same hazard as the tool *input*, and it was missed the first time.
    Both fields go into the one `json.dumps` in `log_event`, so coercing only
    the input still let a non-string name roll back an already-paid
    `finish_attempt` and hand the call back to `_with_retry`. Duck-typed
    `ToolUseBlock`s make the name exactly as untrustworthy as the arguments."""
    worker = RunResult(
        output="wrote the file",
        tool_calls=[{"tool": object(), "input": {"path": "a.py"}}],
        tokens_in=11,
        tokens_out=7,
    )
    task = add_task(store)
    loop, runner = make_loop(store, [worker, APPROVE])
    loop.run_task(task)

    assert task.status == TaskStatus.DONE
    assert len(runner.calls) == 2  # worker + validator: nothing was re-run
    assert store.attempt_tokens(task.id, "worker") == 18
    assert task.output == "wrote the file"
    assert not [e for e in store.events(task.id) if e["kind"] == "infra_error"]

    call = [e for e in store.events(task.id) if e["kind"] == "tool_call"][0]
    assert "object object at" in call["payload"]["tool"]


def test_a_tool_name_whose_str_raises_still_finishes_the_attempt(store):
    """`str` is the fallback, not a guarantee — same reasoning as `repr` on the
    input side."""

    class _Unnameable:
        def __str__(self):
            raise RuntimeError("no name for you")

    worker = RunResult(
        output="done", tool_calls=[{"tool": _Unnameable(), "input": {"a": 1}}]
    )
    task = add_task(store)
    loop, runner = make_loop(store, [worker, APPROVE])
    loop.run_task(task)

    assert task.status == TaskStatus.DONE
    assert len(runner.calls) == 2  # nothing re-run
    call = [e for e in store.events(task.id) if e["kind"] == "tool_call"][0]
    assert call["payload"]["tool"] == "<unrepresentable _Unnameable>"


def test_a_plain_tool_name_is_recorded_unquoted(store):
    """The name is rendered as a name by the dashboard and read as one by
    slice 5's policy, so the coercion must not JSON-quote `Read` into `"Read"`
    the way the input side does."""
    worker = RunResult(output="done", tool_calls=[{"tool": "Read", "input": {}}])
    task = add_task(store)
    loop, _ = make_loop(store, [worker, APPROVE])
    loop.run_task(task)

    call = [e for e in store.events(task.id) if e["kind"] == "tool_call"][0]
    assert call["payload"]["tool"] == "Read"


def test_a_missing_tool_name_is_recorded_as_unknown(store):
    """A duck-typed block that carries no name at all is a record, not a
    crash."""
    worker = RunResult(output="done", tool_calls=[{"input": {"a": 1}}])
    task = add_task(store)
    loop, _ = make_loop(store, [worker, APPROVE])
    loop.run_task(task)

    call = [e for e in store.events(task.id) if e["kind"] == "tool_call"][0]
    assert call["payload"]["tool"] == "unknown"
