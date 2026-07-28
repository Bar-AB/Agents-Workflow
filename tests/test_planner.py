"""Slice 3: planner agent + task graph, then parallel workers.

End-to-end through the Loop, no API keys and no network. The planner tests use
MockRunner's positional script; the parallel tests use a content-routed,
thread-safe runner instead — a positional script is nondeterministic once two
workers pop from it concurrently, which would make the test lie about ordering.
"""

import threading
import time

import pytest

from agentloop.agents import PlanError, parse_plan
from agentloop.config import LoopConfig
from agentloop.loop import Loop
from agentloop.models import RunResult, Task, TaskStatus
from agentloop.registry import Registry
from agentloop.runner import MockRunner
from agentloop.store import Store

APPROVE = "VERDICT: approve CONFIDENCE: 0.92 TESTS: pass\nMeets all criteria."
SEVERE = (
    "VERDICT: escalate CONFIDENCE: 0.10 TESTS: fail\n"
    "Fundamentally wrong approach; solves a different problem."
)

# A 3-task graph with one dependency: `tests` and `docs` both depend on `core`
# and are independent of each other, so they are the pair that may overlap.
PLAN_JSON = """Here is the decomposition.

```json
{"tasks": [
  {"ref": "core", "title": "Write slugify()",
   "goal": "Implement slugify(text) in slugify.py",
   "acceptance_criteria": "Lowercase, hyphen-separated, handles unicode",
   "risk_level": 1, "depends_on": []},
  {"ref": "tests", "title": "Write the test suite",
   "goal": "Cover slugify() with pytest cases",
   "acceptance_criteria": "Unicode, empty string and punctuation are covered",
   "risk_level": 1, "depends_on": ["core"]},
  {"ref": "docs", "title": "Write the README",
   "goal": "Document slugify() usage",
   "acceptance_criteria": "Install, usage and an example are present",
   "risk_level": 0, "depends_on": ["core"]}
]}
```
"""


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def make_loop(store, outputs, **cfg_overrides):
    from pathlib import Path

    cfg_overrides.setdefault("workspace_root", str(Path(store.db_path).parent / "ws"))
    cfg_overrides.setdefault("allow_test_exec", False)
    config = LoopConfig(db_path=store.db_path, **cfg_overrides)
    runner = MockRunner(outputs)
    return Loop(store, runner, Registry.load(), config), runner


def titles(store) -> list[str]:
    return [t.title for t in store.list_tasks()]


def by_title(store, title: str) -> Task:
    for t in store.list_tasks():
        if t.title == title:
            return t
    raise AssertionError(f"no task titled {title!r} in {titles(store)}")


# -- the planner produces a graph --------------------------------------------


def test_planner_emits_a_task_graph_with_dependencies(store):
    """The acceptance criterion's shape: a 3-task graph with one dependency,
    persisted as task rows plus edges, attributable to a planner attempt."""
    loop, runner = make_loop(store, [PLAN_JSON])
    plan = loop.plan("Build a slugify library", "Published, tested, documented")

    assert plan.kind == "plan"
    children = [t for t in store.list_tasks() if t.kind == "task"]
    assert [c.title for c in children] == [
        "Write slugify()",
        "Write the test suite",
        "Write the README",
    ]
    assert all(c.plan_id == plan.id for c in children)
    assert all(c.status == TaskStatus.PENDING for c in children)
    # risk_level survives the round trip — the planner sets it per child.
    assert by_title(store, "Write the README").risk_level == 0

    core = by_title(store, "Write slugify()")
    tests = by_title(store, "Write the test suite")
    docs = by_title(store, "Write the README")
    assert store.dependencies(core.id) == []
    assert store.dependencies(tests.id) == [core.id]
    assert store.dependencies(docs.id) == [core.id]
    assert sorted(store.dependents(core.id)) == sorted([tests.id, docs.id])

    # The planner ran as its own recorded attempt on the plan row.
    assert store.task_metrics(plan.id)["attempts"] == 1
    kinds = [e["kind"] for e in store.events(plan.id)]
    assert "planner_prompt" in kinds and "plan_created" in kinds
    created = [e for e in store.events(plan.id) if e["kind"] == "plan_created"][0]
    assert created["payload"]["n_tasks"] == 3
    assert created["payload"]["n_edges"] == 2
    # The planner saw the goal it was asked to decompose.
    assert "slugify library" in runner.calls[0]["prompt"]


def test_a_plan_row_is_never_claimed_by_the_loop(store):
    """The plan is a container, not work. If it were claimable the loop would
    hand a goal statement to a worker as though it were a task."""
    loop, _ = make_loop(store, [PLAN_JSON], plan_requires_approval=False)
    plan = loop.plan("Build a slugify library", "Published, tested, documented")

    claimed = []
    # Retire each claim: an in-flight task is deliberately re-offered to the
    # worker that owns it (that is how a crashed run resumes), so draining the
    # queue means finishing what we claim.
    while (t := store.claim_next_task("probe")) is not None:
        claimed.append(t.id)
        store.set_status(t, TaskStatus.DONE)
    assert plan.id not in claimed
    assert len(claimed) == 3  # the children are claimable, the plan is not


# -- plan gating (humans stay in the loop at task definition) -----------------


def test_plan_children_are_gated_until_the_plan_is_approved(store):
    """A planner generating tasks *is* task definition, so by default nothing it
    produced runs until a human signs the plan off."""
    loop, _ = make_loop(store, [PLAN_JSON])
    plan = loop.plan("Build a slugify library", "Published, tested, documented")

    assert plan.status == TaskStatus.NEEDS_HUMAN
    assert "sign-off" in plan.escalation_reason
    assert loop.run() == 0, "an unapproved plan's children must not run"
    assert all(
        t.status == TaskStatus.PENDING for t in store.list_tasks() if t.kind == "task"
    )

    loop.approve_plan(plan.id, note="looks right")
    assert store.get_task(plan.id).status == TaskStatus.DONE
    assert "plan_approved" in [e["kind"] for e in store.events(plan.id)]

    loop.runner = MockRunner(
        ["core out", APPROVE, "tests out", APPROVE, "docs", APPROVE]
    )
    assert loop.run() == 3
    assert all(
        t.status == TaskStatus.DONE for t in store.list_tasks() if t.kind == "task"
    )


def test_plan_approval_can_be_configured_off(store):
    """`plan_requires_approval=False` is the autonomous-batch escape hatch."""
    loop, _ = make_loop(
        store,
        [PLAN_JSON, "core out", APPROVE, "tests out", APPROVE, "docs out", APPROVE],
        plan_requires_approval=False,
    )
    plan = loop.plan("Build a slugify library", "Published, tested, documented")

    assert plan.status == TaskStatus.DONE
    assert loop.run() == 3


def test_approving_a_plan_row_through_the_ordinary_approve_path_releases_it(store):
    """The dashboard's approve button and `agentloop approve <plan-id>` hit
    human_approve. On a plan row that must release the plan, not silently mark a
    goal 'done' while its children stay blocked forever."""
    loop, _ = make_loop(store, [PLAN_JSON])
    plan = loop.plan("Build a slugify library", "Published, tested, documented")
    loop.human_approve(plan.id, note="ship it")

    assert store.is_plan_approved(plan.id)
    loop.runner = MockRunner(["a", APPROVE, "b", APPROVE, "c", APPROVE])
    assert loop.run() == 3


# -- a bad plan never becomes tasks ------------------------------------------


def test_unparseable_plan_escalates_and_creates_no_tasks(store):
    loop, _ = make_loop(store, ["Sure! I'd start by writing the slugify function."])
    plan = loop.plan("Build a slugify library", "Published, tested, documented")

    assert plan.status == TaskStatus.NEEDS_HUMAN
    assert "Unusable plan" in plan.escalation_reason
    assert [t for t in store.list_tasks() if t.kind == "task"] == []


def test_a_cyclic_plan_is_rejected_whole(store):
    """A cycle is a deadlock: every task in it waits on another. Reject the plan
    rather than persisting a graph the loop can never drain."""
    cyclic = """```json
{"tasks": [
  {"ref": "a", "title": "A", "goal": "g", "acceptance_criteria": "c",
   "depends_on": ["b"]},
  {"ref": "b", "title": "B", "goal": "g", "acceptance_criteria": "c",
   "depends_on": ["a"]}
]}
```"""
    loop, _ = make_loop(store, [cyclic])
    plan = loop.plan("Goal", "Criteria")

    assert plan.status == TaskStatus.NEEDS_HUMAN
    assert "cycle" in plan.escalation_reason.lower()
    assert [t for t in store.list_tasks() if t.kind == "task"] == []


def test_a_plan_referencing_an_unknown_task_is_rejected_whole(store):
    dangling = """```json
{"tasks": [
  {"ref": "a", "title": "A", "goal": "g", "acceptance_criteria": "c",
   "depends_on": ["nope"]}
]}
```"""
    loop, _ = make_loop(store, [dangling])
    plan = loop.plan("Goal", "Criteria")

    assert plan.status == TaskStatus.NEEDS_HUMAN
    assert "nope" in plan.escalation_reason
    assert [t for t in store.list_tasks() if t.kind == "task"] == []


def test_an_oversized_plan_is_rejected(store):
    big = {
        "tasks": [
            {"ref": f"t{i}", "title": f"T{i}", "goal": "g", "acceptance_criteria": "c"}
            for i in range(9)
        ]
    }
    import json as _json

    loop, _ = make_loop(store, [_json.dumps(big)], max_plan_tasks=8)
    plan = loop.plan("Goal", "Criteria")

    assert plan.status == TaskStatus.NEEDS_HUMAN
    assert "8" in plan.escalation_reason
    assert [t for t in store.list_tasks() if t.kind == "task"] == []


def test_planner_ambiguity_escalates(store):
    """Same rule the worker has: agents ask instead of guessing."""
    loop, _ = make_loop(store, ["ESCALATE: which package layout should I assume?"])
    plan = loop.plan("Build a slugify library", "Published, tested, documented")

    assert plan.status == TaskStatus.NEEDS_HUMAN
    assert "package layout" in plan.escalation_reason
    assert [t for t in store.list_tasks() if t.kind == "task"] == []


def test_planner_infra_failure_escalates_without_a_partial_graph(store):
    boom = RuntimeError("API 503")
    loop, _ = make_loop(store, [boom, boom, boom], infra_max_retries=1)
    plan = loop.plan("Goal", "Criteria")

    assert plan.status == TaskStatus.NEEDS_HUMAN
    assert "infra_error" in plan.escalation_reason
    assert [t for t in store.list_tasks() if t.kind == "task"] == []


def test_approving_a_task_that_never_ran_is_refused(store):
    """Approval signs off work that was done and reviewed. A pending task has no
    attempt, no verdict and no output — and with a graph, DONE is what satisfies
    a dependency, so approving unrun work would release its dependents to run
    against upstream output that does not exist."""
    loop, _ = make_loop(store, [PLAN_JSON], plan_requires_approval=False)
    loop.plan("Build a slugify library", "Published, tested, documented")
    core = by_title(store, "Write slugify()")
    dependent = by_title(store, "Write the test suite")

    with pytest.raises(ValueError, match="has not run yet"):
        loop.human_approve(core.id)
    assert store.get_task(core.id).status == TaskStatus.PENDING
    # And the dependent was therefore never released.
    assert store.claim_next_task("probe").id != dependent.id


def test_a_failed_plan_cannot_be_approved(store):
    """A plan that produced no tasks is a failed plan. Approving it would turn
    an escalation into a green DONE goal with nothing under it and wipe the
    diagnosis off the row — the decision rules point the other way."""
    loop, _ = make_loop(store, ["not json at all"])
    plan = loop.plan("Goal", "Criteria")
    assert plan.status == TaskStatus.NEEDS_HUMAN

    with pytest.raises(ValueError, match="produced no tasks"):
        loop.approve_plan(plan.id)
    with pytest.raises(ValueError, match="produced no tasks"):
        loop.human_approve(plan.id)  # the dashboard's button routes here too

    after = store.get_task(plan.id)
    assert after.status == TaskStatus.NEEDS_HUMAN
    assert "Unusable plan" in after.escalation_reason  # diagnosis preserved
    assert not store.is_plan_approved(plan.id)


def test_planning_an_empty_goal_is_refused_cleanly(store):
    """The one failure that cannot escalate a plan row is one that happens
    before the row exists, so it must be refused up front rather than crash."""
    loop, _ = make_loop(store, [PLAN_JSON])
    with pytest.raises(ValueError, match="goal"):
        loop.plan("   \n ", "Criteria")
    with pytest.raises(ValueError, match="acceptance criteria"):
        loop.plan("A real goal", "")
    assert store.list_tasks() == []


def test_a_repeated_dependency_ref_does_not_double_count_edges(store):
    """The edge table is keyed on (task, depends_on), so a repeated ref is one
    row. The audit log must not claim two."""
    dup = """```json
{"tasks": [
  {"ref": "a", "title": "A", "goal": "g", "acceptance_criteria": "c"},
  {"ref": "b", "title": "B", "goal": "g", "acceptance_criteria": "c",
   "depends_on": ["a", "a"]}
]}
```"""
    loop, _ = make_loop(store, [dup])
    plan = loop.plan("Goal", "Criteria")

    created = [e for e in store.events(plan.id) if e["kind"] == "plan_created"][0]
    assert created["payload"]["n_edges"] == 1
    assert len(store.dependencies(by_title(store, "B").id)) == 1
    assert len([e for e in store.events() if e["kind"] == "task_dependency"]) == 1


def test_an_oversized_planner_reply_is_refused_before_parsing(store):
    with pytest.raises(PlanError, match="refusing to parse"):
        parse_plan("x" * 300_000, max_tasks=8)


def test_a_deeply_nested_planner_reply_escalates_rather_than_crashing(store):
    """RecursionError is not a JSONDecodeError; unhandled it would escape as a
    traceback instead of escalating the plan row."""
    nested = "[" * 20_000 + "]" * 20_000
    loop, _ = make_loop(store, [nested])
    plan = loop.plan("Goal", "Criteria")

    assert plan.status == TaskStatus.NEEDS_HUMAN
    assert "Unusable plan" in plan.escalation_reason
    assert [t for t in store.list_tasks() if t.kind == "task"] == []


def test_upstream_results_exclude_a_dependency_that_is_no_longer_done(store):
    """The claim gate holds at claim time, but this block is rebuilt every
    revision round — a redone upstream task must stop being presented as a
    finished result."""
    from agentloop.agents import _upstream_block

    up = Task(id=None, title="Up", goal="g", acceptance_criteria="c")
    down = Task(id=None, title="Down", goal="g", acceptance_criteria="c")
    store.add_task(up)
    store.add_task(down)
    store.add_dependency(down.id, up.id)
    up.output = "FINISHED UPSTREAM WORK"
    store.set_status(up, TaskStatus.DONE)
    assert "FINISHED UPSTREAM WORK" in _upstream_block(store, down)

    store.set_status(up, TaskStatus.PENDING)  # e.g. a human_redo mid-round
    assert _upstream_block(store, down) == ""


def test_a_registry_without_a_planner_escalates_before_spending_anything(store):
    """A hand-edited agents.json predating the planner role is a config error,
    not a transient one: it must name the registry rather than be retried with
    backoff and reported as an infra failure."""
    from agentloop.registry import DEFAULT_AGENTS

    agents = {k: v for k, v in DEFAULT_AGENTS.items() if k != "planner"}
    config = LoopConfig(db_path=store.db_path, allow_test_exec=False)
    runner = MockRunner([PLAN_JSON])
    loop = Loop(store, runner, Registry(agents), config)
    plan = loop.plan("Goal", "Criteria")

    assert plan.status == TaskStatus.NEEDS_HUMAN
    assert "agents.json" in plan.escalation_reason
    assert "infra_error" not in plan.escalation_reason
    assert runner.calls == [], "no model call should have been made"
    assert [t for t in store.list_tasks() if t.kind == "task"] == []


def test_parse_plan_rejects_an_empty_plan():
    with pytest.raises(PlanError):
        parse_plan('{"tasks": []}', max_tasks=8)


# -- dependency ordering ------------------------------------------------------


def test_the_loop_runs_a_graph_in_dependency_order(store):
    loop, runner = make_loop(
        store,
        [PLAN_JSON, "core out", APPROVE, "tests out", APPROVE, "docs out", APPROVE],
        plan_requires_approval=False,
    )
    loop.plan("Build a slugify library", "Published, tested, documented")
    assert loop.run() == 3

    worker_prompts = [
        c["prompt"] for c in runner.calls if c["prompt"].startswith("# Task:")
    ]
    order = [p.splitlines()[0] for p in worker_prompts]
    assert order[0] == "# Task: Write slugify()", (
        "the dependency must run before its dependents"
    )
    assert set(order[1:]) == {
        "# Task: Write the test suite",
        "# Task: Write the README",
    }


def test_a_dependent_sees_its_upstream_results(store):
    """Dependency order without data flow is just a delay: the dependent's
    worker gets the output of everything it was made to wait for."""
    loop, runner = make_loop(
        store,
        [
            PLAN_JSON,
            "SLUGIFY IMPLEMENTATION v1",
            APPROVE,
            "tests",
            APPROVE,
            "d",
            APPROVE,
        ],
        plan_requires_approval=False,
    )
    loop.plan("Build a slugify library", "Published, tested, documented")
    loop.run()

    dependent = [
        c["prompt"]
        for c in runner.calls
        if c["prompt"].startswith("# Task: Write the test suite")
    ][0]
    assert "Upstream results" in dependent
    assert "SLUGIFY IMPLEMENTATION v1" in dependent
    # The independent root has nothing upstream, so it gets no such block.
    root = [
        c["prompt"]
        for c in runner.calls
        if c["prompt"].startswith("# Task: Write slugify()")
    ][0]
    assert "Upstream results" not in root


def test_a_dependent_of_an_escalated_task_stays_blocked_not_failed(store):
    """Blocked is a claimability predicate, not a status: the batch finishes
    everything else and the dependent simply waits. Resolving the dependency
    makes it claimable on the next run — nothing was lost."""
    loop, _ = make_loop(
        store,
        [PLAN_JSON, "core out", SEVERE],
        plan_requires_approval=False,
    )
    loop.plan("Build a slugify library", "Published, tested, documented")
    assert loop.run() == 1  # only `core` was ever claimable

    core = by_title(store, "Write slugify()")
    assert core.status == TaskStatus.NEEDS_HUMAN
    assert by_title(store, "Write the test suite").status == TaskStatus.PENDING
    assert by_title(store, "Write the README").status == TaskStatus.PENDING

    # Human resolves the dependency; the dependents become claimable.
    loop.human_approve(core.id, note="acceptable after all")
    loop.runner = MockRunner(["tests out", APPROVE, "docs out", APPROVE])
    assert loop.run() == 2
    assert by_title(store, "Write the test suite").status == TaskStatus.DONE
    assert by_title(store, "Write the README").status == TaskStatus.DONE


def test_an_older_database_migrates_to_the_graph_schema(tmp_path):
    """Slice 3 adds three columns to an existing table, which
    `CREATE TABLE IF NOT EXISTS` will never apply to a db that already exists.
    A pre-slice-3 store must open, gain the columns, and behave normally."""
    import sqlite3

    db = tmp_path / "old.db"
    raw = sqlite3.connect(db)
    raw.executescript(
        """
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL, goal TEXT NOT NULL,
            acceptance_criteria TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            risk_level INTEGER NOT NULL DEFAULT 1,
            revision_count INTEGER NOT NULL DEFAULT 0,
            worker_role TEXT NOT NULL DEFAULT 'worker',
            validator_role TEXT NOT NULL DEFAULT 'validator',
            output TEXT NOT NULL DEFAULT '',
            escalation_reason TEXT NOT NULL DEFAULT '',
            control TEXT NOT NULL DEFAULT 'run',
            claimed_by TEXT,
            created_at REAL NOT NULL, updated_at REAL NOT NULL);
        INSERT INTO tasks (title, goal, acceptance_criteria, created_at,
                           updated_at)
        VALUES ('Legacy task', 'do it', 'works', 0, 0);
        """
    )
    raw.commit()
    raw.close()

    migrated = Store(db)
    try:
        legacy = migrated.get_task(1)
        # An existing row defaults to an ordinary, plan-less, claimable task —
        # not something stranded behind a plan gate that was never there.
        assert legacy.kind == "task"
        assert legacy.plan_id is None
        assert migrated.claim_next_task("loop").id == 1
        # And the graph table exists, so a plan can be built in the same db.
        assert migrated.dependencies(1) == []
    finally:
        migrated.close()


def test_store_rejects_a_dependency_that_would_close_a_cycle(store):
    """Store-level invariant, independent of the planner: the graph the loop
    drains can never contain a cycle."""
    a = Task(id=None, title="A", goal="g", acceptance_criteria="c")
    b = Task(id=None, title="B", goal="g", acceptance_criteria="c")
    store.add_task(a)
    store.add_task(b)
    store.add_dependency(b.id, a.id)
    with pytest.raises(ValueError, match="cycle"):
        store.add_dependency(a.id, b.id)
    with pytest.raises(ValueError, match="cycle"):
        store.add_dependency(a.id, a.id)


# -- parallel workers ---------------------------------------------------------


class GraphRunner:
    """Thread-safe runner that routes on prompt content rather than call order.

    Under concurrency a positional script (MockRunner) hands whichever thread
    calls first whatever output is next, so the test would assert on an ordering
    it created itself. Routing on content removes that.

    `barrier_titles` is how concurrency is *proved* rather than timed: the named
    tasks' worker calls all wait on one barrier, so the test can only pass if
    they are genuinely in flight at the same moment.
    """

    def __init__(self, plan: str | None = None, barrier_titles: tuple = ()):
        self.plan = plan
        self.barrier_titles = barrier_titles
        self.barrier = (
            threading.Barrier(len(barrier_titles), timeout=10)
            if barrier_titles
            else None
        )
        self._lock = threading.Lock()
        self.prompts: list[str] = []
        self.active = 0
        self.max_active = 0

    def run(self, system_prompt, prompt, model, tools=None):
        with self._lock:
            self.prompts.append(prompt)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if prompt.startswith("# Goal to decompose"):
                return self._result(self.plan)
            if prompt.startswith("# Task under review"):
                return self._result(APPROVE)
            title = prompt.splitlines()[0].removeprefix("# Task: ")
            if self.barrier is not None and title in self.barrier_titles:
                self.barrier.wait()
            else:
                time.sleep(0.01)  # let a peer thread get scheduled
            return self._result(f"output of {title}")
        finally:
            with self._lock:
                self.active -= 1

    @staticmethod
    def _result(text: str) -> RunResult:
        return RunResult(output=text, tokens_in=10, tokens_out=5, model="mock")


def make_graph_loop(store, tmp_path, runner, **overrides):
    overrides.setdefault("plan_requires_approval", False)
    config = LoopConfig(
        db_path=store.db_path,
        workspace_root=str(tmp_path / "ws"),
        allow_test_exec=False,
        **overrides,
    )
    return Loop(store, runner, Registry.load(), config)


def test_independent_tasks_run_concurrently_while_dependencies_hold(store, tmp_path):
    """The slice's acceptance criterion: a 3-task graph with one dependency runs
    in order, and the two independent tasks really overlap."""
    runner = GraphRunner(
        plan=PLAN_JSON,
        barrier_titles=("Write the test suite", "Write the README"),
    )
    loop = make_graph_loop(
        store, tmp_path, runner, max_parallel_workers=3, infra_max_retries=0
    )
    loop.plan("Build a slugify library", "Published, tested, documented")

    assert loop.run() == 3
    assert all(
        t.status == TaskStatus.DONE for t in store.list_tasks() if t.kind == "task"
    )
    # The barrier only releases when both dependents are in flight together.
    assert runner.max_active >= 2

    # And `core` still finished before either of them started.
    worker_prompts = [p for p in runner.prompts if p.startswith("# Task:")]
    assert worker_prompts[0].startswith("# Task: Write slugify()")


def test_two_workers_never_run_the_same_task(store, tmp_path):
    """Per-task audit isolation: one claim, one worker attempt, one verdict —
    the atomic claim (slice 0c) is what this consumes."""
    runner = GraphRunner(plan=PLAN_JSON)
    loop = make_graph_loop(store, tmp_path, runner, max_parallel_workers=4)
    loop.plan("Build a slugify library", "Published, tested, documented")
    loop.run()

    for task in store.list_tasks():
        if task.kind != "task":
            continue
        claims = [e for e in store.events(task.id) if e["kind"] == "task_claimed"]
        assert len(claims) == 1, f"task {task.id} was claimed {len(claims)} times"
        metrics = store.task_metrics(task.id)
        assert metrics["attempts"] == 2  # exactly one worker + one validator
        assert len(metrics["verdicts"]) == 1


def test_sequential_remains_the_default_and_is_unchanged(store, tmp_path):
    """max_parallel_workers defaults to 1: one claim id, one thread, today's
    behaviour byte for byte."""
    assert LoopConfig().max_parallel_workers == 1
    runner = GraphRunner(plan=PLAN_JSON)
    loop = make_graph_loop(store, tmp_path, runner)
    loop.plan("Build a slugify library", "Published, tested, documented")
    assert loop.run() == 3

    assert runner.max_active == 1, "the default loop must not run anything in parallel"
    workers = {
        e["payload"]["worker"] for e in store.events() if e["kind"] == "task_claimed"
    }
    assert workers == {"loop"}


def test_a_concurrent_rollback_cannot_discard_another_threads_write(store):
    """`execute()` then `commit()` as two calls takes the connection lock twice.
    In the gap, a peer's failing `transaction()` rolls back the *shared*
    connection and destroys the uncommitted row — while the writer's call
    returned normally. Silent loss of a worker's output or an attempt's tokens,
    and it becomes reachable the moment two workers run."""
    task = Task(id=None, title="T", goal="g", acceptance_criteria="c")
    store.add_task(task)

    started = threading.Event()
    release = threading.Event()

    def rollback_peer():
        started.wait(2)
        try:
            with store.transaction():
                store.log_event(task.id, "doomed", {})
                raise RuntimeError("peer transaction fails")
        except RuntimeError:
            pass
        release.set()

    peer = threading.Thread(target=rollback_peer)
    peer.start()
    task.output = "IMPORTANT WORKER OUTPUT"
    started.set()
    store.update_task(task)
    release.wait(2)
    peer.join(2)

    assert store.get_task(task.id).output == "IMPORTANT WORKER OUTPUT"
    # The peer's own write really was rolled back — the fix must not have
    # turned the rollback into a no-op.
    assert [e for e in store.events(task.id) if e["kind"] == "doomed"] == []


def test_a_claim_lost_to_another_connection_is_not_taken_twice(store, tmp_path):
    """Two `agentloop run` processes hold separate connections and separate
    locks, so the in-process lock cannot serialize them. The claim's UPDATE is a
    compare-and-swap; the loser must not also run the task."""
    task = Task(id=None, title="T", goal="g", acceptance_criteria="c")
    store.add_task(task)

    other = Store(store.db_path)  # a second connection, as a second process has
    try:
        first = store.claim_next_task("proc-a")
        second = other.claim_next_task("proc-b")
        assert first is not None and first.id == task.id
        assert second is None, "the second connection must not claim a taken task"

        claims = [e for e in store.events(task.id) if e["kind"] == "task_claimed"]
        assert len(claims) == 1
        assert claims[0]["payload"]["worker"] == "proc-a"
    finally:
        other.close()


def test_shrinking_the_worker_pool_reports_stranded_claims(store, tmp_path):
    """`claim_next_task` only re-offers in-flight work to its exact owner, so
    retiring a claim id hides whatever it held from every claimer. That must not
    be silent: `run()` would otherwise report success while excluding it."""
    for _ in range(3):
        store.add_task(Task(id=None, title="T", goal="g", acceptance_criteria="c"))
    # Simulate a crashed 3-worker run: each id holds one in-flight task.
    for wid in ("loop", "loop-1", "loop-2"):
        claimed = store.claim_next_task(wid)
        assert claimed is not None

    runner = GraphRunner()
    loop = make_graph_loop(store, tmp_path, runner, max_parallel_workers=1)
    loop.run()  # a 1-worker restart: loop-1 and loop-2 are now orphaned

    stranded = [e for e in store.events() if e["kind"] == "claim_stranded"]
    assert {e["payload"]["claimed_by"] for e in stranded} == {"loop-1", "loop-2"}


def test_max_tasks_still_bounds_a_parallel_run(store, tmp_path):
    runner = GraphRunner(plan=PLAN_JSON)
    loop = make_graph_loop(store, tmp_path, runner, max_parallel_workers=3)
    loop.plan("Build a slugify library", "Published, tested, documented")

    assert loop.run(max_tasks=1) == 1
    done = [
        t
        for t in store.list_tasks()
        if t.kind == "task" and t.status == TaskStatus.DONE
    ]
    assert [t.title for t in done] == ["Write slugify()"]
