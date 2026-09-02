"""Slice 3c: the project charter, and the validator showing its work.

Two independent items, both offline through MockRunner (no API keys, no
network). Neither changes a decision rule, and several of these tests exist
precisely to hold that line: the charter is prompt content, findings are
evidence, and the state machine reads neither.

The parallel test uses a content-routed runner rather than MockRunner's
positional script, for the reason given in tests/test_planner.py: with two
workers popping concurrently a positional script would make the test assert an
ordering it created itself.
"""

import threading
import time
from pathlib import Path

import pytest

from agentloop.agents import _MAX_FINDINGS_CHARS
from agentloop.config import LoopConfig
from agentloop.loop import Loop
from agentloop.memory import _MAX_PINNED_FACTS
from agentloop.models import AgentSpec, RunResult, Task, TaskStatus
from agentloop.registry import DEFAULT_AGENTS, Registry
from agentloop.runner import MockRunner
from agentloop.store import _MAX_CHARTER_CHARS, Store

APPROVE = "VERDICT: approve CONFIDENCE: 0.92 TESTS: pass\nMeets all criteria."
REVISE = (
    "VERDICT: revise CONFIDENCE: 0.55 TESTS: fail\n"
    "Edge case for empty input is not handled; add a guard and a test."
)

CHARTER = (
    "1. Errors are raised, never returned as None.\n"
    "2. Every public function has a docstring.\n"
    "3. Prefer stdlib over a new dependency."
)

PLAN_JSON = """```json
{"tasks": [
  {"ref": "core", "title": "Write slugify()",
   "goal": "Implement slugify(text)",
   "acceptance_criteria": "Lowercase, hyphen-separated",
   "risk_level": 1, "depends_on": []}
]}
```"""


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def make_loop(store, outputs, registry=None, **cfg_overrides):
    cfg_overrides.setdefault("workspace_root", str(Path(store.db_path).parent / "ws"))
    cfg_overrides.setdefault("allow_test_exec", False)
    # Slice 6: git is off in the loop tests. A repo per workspace would
    # spawn real subprocesses in ~300 tests that are not about durability.
    cfg_overrides.setdefault("vcs_enabled", False)
    config = LoopConfig(db_path=store.db_path, **cfg_overrides)
    runner = MockRunner(outputs)
    return Loop(store, runner, registry or Registry.load(), config), runner


def add_task(store, title="Add slugify util") -> Task:
    task = Task(
        id=None,
        title=title,
        goal="Write a slugify(text) function.",
        acceptance_criteria="Lowercase, hyphen-separated, tested.",
    )
    store.add_task(task)
    return task


def _small_worker_budget_registry(worker_budget: int) -> Registry:
    """A worker whose context budget a single revision trips, so a handoff
    really fires. Same idiom as the slice-1 tests; keeps the summarizer role."""
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


# -- ITEM 1: the project charter ----------------------------------------------


def test_empty_charter_prompt_is_byte_for_byte_unchanged(tmp_path):
    """Absent and cleared must be indistinguishable, and an unchartered project
    must get byte for byte the prompt it got before the charter existed. The
    other 229 tests passing unmodified is the wider evidence for the second
    half; this is the direct assertion for the first.

    Both stores share a workspace_root and both tasks are id 1 in their own
    fresh db, so every other input to the prompt is identical by construction.

    Scope worth being exact about: this is the *user* prompt. The registry's
    system prompts did change for every project, chartered or not — the
    validator's old "judge it strictly against the task's acceptance criteria"
    instructed it to disregard a charter, so injection alone would have been
    inert. That is why all three roles moved to version "2". What the charter
    itself contributes to a prompt is nothing at all when there is none.
    """
    ws = str(tmp_path / "ws")
    untouched = Store(tmp_path / "a.db")
    cleared = Store(tmp_path / "b.db")
    try:
        cleared.charter_set(CHARTER)
        cleared.charter_clear(note="rolled back")

        calls = []
        for st in (untouched, cleared):
            task = add_task(st)
            loop, runner = make_loop(st, ["out", APPROVE], workspace_root=ws)
            loop.run_task(task)
            calls.append(runner.calls)

        for i in (0, 1):  # worker, then validator
            assert calls[0][i]["prompt"] == calls[1][i]["prompt"]
            # The system prompt is charter-independent: it says how to treat a
            # charter block, never what one says.
            assert calls[0][i]["system"] == calls[1][i]["system"]
            assert "## Project charter" not in calls[0][i]["prompt"]
        assert cleared.charter_active() is None
    finally:
        untouched.close()
        cleared.close()


def test_charter_reaches_worker_validator_and_planner(store, tmp_path):
    """The role enumeration. A new agent role has to be added to this test —
    that is the cheap substitute for having `_invoke` inject the charter itself,
    which would also charter the summarizer.

    The summarizer is excluded because its output is consumed by a worker that
    rebuilds the charter fresh, so a real handoff is driven here rather than
    asserted about in the abstract.
    """
    store.charter_set(CHARTER)

    # Worker + validator + a real context handoff (so the summarizer is
    # genuinely invoked, not merely absent).
    task = add_task(store)
    loop, runner = make_loop(
        store,
        [
            "worker1 raw output",
            REVISE,
            "SUMMARY: compacted prior work.",
            "worker2 output fixed",
            APPROVE,
        ],
        registry=_small_worker_budget_registry(worker_budget=40),
    )
    loop.run_task(task)
    assert task.status == TaskStatus.DONE

    worker_prompt, validator_prompt = runner.calls[0], runner.calls[1]
    summarizer_prompt = runner.calls[2]
    assert "## Project charter (v1)" in worker_prompt["prompt"]
    assert CHARTER in worker_prompt["prompt"]
    assert "## Project charter" in validator_prompt["prompt"]
    assert CHARTER in validator_prompt["prompt"]
    # Not the summarizer: the fresh worker gets its own copy anyway.
    assert "## Project charter" not in summarizer_prompt["prompt"]

    # And the planner, which authors the criteria the validator judges against.
    plan_loop, plan_runner = make_loop(store, [PLAN_JSON])
    plan_loop.plan("Build a slugify library", "Published and tested")
    assert "## Project charter" in plan_runner.calls[0]["prompt"]
    assert CHARTER in plan_runner.calls[0]["prompt"]


def test_charter_sits_above_the_memory_block(store):
    store.charter_set(CHARTER)
    store.memory_write("project", "style", "Use tabs.", approved=True)
    task = add_task(store)
    loop, runner = make_loop(store, ["out", APPROVE])
    loop.run_task(task)

    prompt = runner.calls[0]["prompt"]
    assert prompt.index("## Project charter") < prompt.index("## Known project facts")


def test_charter_is_exempt_from_the_memory_caps(store):
    """The charter is not riding the memory path, so the pinned ceiling that
    (correctly) drops the 11th pinned fact must not drop the 11th charter rule.
    A rule that silently falls off the end of a cap is worse than no rule."""
    rules = "\n".join(f"{i}. Rule number {i} must always hold." for i in range(1, 16))
    store.charter_set(rules)
    for i in range(_MAX_PINNED_FACTS + 5):
        store.memory_write(
            "project", f"fact-{i:02d}", f"pinned fact {i}", approved=True, pinned=True
        )

    task = add_task(store)
    loop, runner = make_loop(store, ["out", APPROVE])
    loop.run_task(task)
    prompt = runner.calls[0]["prompt"]

    for i in range(1, 16):
        assert f"{i}. Rule number {i} must always hold." in prompt
    injected = sum(1 for i in range(_MAX_PINNED_FACTS + 5) if f"fact-{i:02d}" in prompt)
    assert injected == _MAX_PINNED_FACTS


def test_oversized_charter_is_refused_at_write(store):
    """Loud at write time, because it is never trimmed at inject time."""
    store.charter_set(CHARTER, note="v1")
    with pytest.raises(ValueError, match="over the"):
        store.charter_set("x" * (_MAX_CHARTER_CHARS + 1))

    assert store.charter_active() == (1, CHARTER)
    assert len([e for e in store.events() if e["kind"] == "charter_set"]) == 1
    assert len(store.charter_history()) == 1


def test_whitespace_only_charter_is_refused_but_clear_is_explicit(store):
    """A typo'd redirect that empties a rules file must not silently remove
    every project rule; `charter_clear` is the audited way to do that."""
    store.charter_set(CHARTER)
    with pytest.raises(ValueError, match="charter clear"):
        store.charter_set("   \n  ")
    assert store.charter_active() == (1, CHARTER)

    version = store.charter_clear(note="paused the house rules")
    assert version == 2
    assert store.charter_active() is None
    cleared = [e for e in store.events() if e["kind"] == "charter_cleared"]
    assert len(cleared) == 1
    assert cleared[0]["payload"]["note"] == "paused the house rules"


def test_charter_version_is_recorded_on_every_attempt(store):
    """ "Was this approved under the old rules?" — answerable by a column, not by
    diffing prompt blobs, and the old text is still readable afterwards."""
    store.charter_set(CHARTER, note="v1")
    task_a = add_task(store, title="Task A")
    loop_a, _ = make_loop(store, ["out a", APPROVE])
    loop_a.run_task(task_a)

    store.charter_set(CHARTER + "\n4. Never log secrets.", note="v2")
    task_b = add_task(store, title="Task B")
    loop_b, _ = make_loop(store, ["out b", APPROVE])
    loop_b.run_task(task_b)

    assert store.task_metrics(task_a.id)["charter_versions"] == [1]
    assert store.task_metrics(task_b.id)["charter_versions"] == [2]
    # The historical text survives the newer version; the id is not just a tag.
    assert store.charter_version(1)["body"] == CHARTER
    assert "Never log secrets" in store.charter_version(2)["body"]


def test_charter_history_is_append_only(store):
    bodies = ["A. first rules", "B. second rules", "C. third rules"]
    versions = [store.charter_set(b, note=f"edit {i}") for i, b in enumerate(bodies)]

    assert versions == [1, 2, 3]
    history = store.charter_history()
    assert [h["id"] for h in history] == [1, 2, 3]
    assert [h["body"] for h in history] == bodies
    assert len([e for e in store.events() if e["kind"] == "charter_set"]) == 3
    # The whole public charter surface, enumerated: appending a version and
    # reading one. Any *other* public `charter_*` accessor is a way to change
    # what a past attempt says it ran under, so a new one has to be justified
    # here rather than added quietly.
    surface = {
        n for n in dir(store) if n.startswith("charter") and not n.startswith("_")
    }
    assert surface == {
        "charter_active",
        "charter_set",
        "charter_clear",
        "charter_history",
        "charter_version",
    }
    # And every earlier body is still readable after two newer versions exist.
    assert [store.charter_version(v)["body"] for v in versions] == bodies


def test_a_memory_write_cannot_touch_the_charter(store):
    """The assertion option (a) — the charter as pinned memory facts — could not
    make. `memory_write` drops `approved` on a value change and reads are gated
    on it, so an agent write to a charter key would have *muted* that rule."""
    store.charter_set(CHARTER)
    rule = "1. Errors are raised, never returned as None."
    store.memory_write("loop", rule, "actually, returning None is fine")

    # The write really landed — this is a live write against the exact key, not
    # a write that happened to miss.
    written = [r for r in store.memory_list() if r["key"] == rule]
    assert len(written) == 1
    assert written[0]["value"] == "actually, returning None is fine"
    # ...and the charter is untouched by it. Under the rejected "charter as
    # pinned memory facts" option this same write would have replaced the rule's
    # text and dropped `approved` to 0, silently removing it from every prompt.
    assert store.charter_active() == (1, CHARTER)
    assert rule in store.charter_active()[1]


def test_charter_survives_a_context_handoff(store):
    """The block is rebuilt on every worker call, so it outlives the transcript
    a handoff replaces — which a rule mentioned once in that transcript does
    not."""
    store.charter_set(CHARTER)
    task = add_task(store)
    loop, runner = make_loop(
        store,
        [
            "worker1 raw output",
            REVISE,
            "SUMMARY: compacted prior work.",
            "worker2 output fixed",
            APPROVE,
        ],
        registry=_small_worker_budget_registry(worker_budget=40),
    )
    loop.run_task(task)

    handoffs = [e for e in store.events(task.id) if e["kind"] == "context_handoff"]
    assert handoffs, "this test is only meaningful if a handoff really fired"
    post_handoff_worker = runner.calls[3]["prompt"]
    assert "compacted prior work" in post_handoff_worker  # it is the fresh worker
    assert "worker1 raw output" not in post_handoff_worker
    assert CHARTER in post_handoff_worker


def test_charter_is_not_part_of_the_retrieval_query(store):
    """The ranking-convergence trap. Folding the charter into the retrieval
    query would rank every task's memory against the same house-rule
    vocabulary, converging the ordering across all tasks and undoing slice 2."""
    store.charter_set("Never use the word xyzzyquux in any identifier.")
    store.memory_write("project", "a", "fact about slugs", approved=True)
    store.memory_write("project", "b", "fact about tests", approved=True)

    task = add_task(store)
    loop, runner = make_loop(store, ["out", APPROVE])
    loop.run_task(task)

    retrievals = [e for e in store.events(task.id) if e["kind"] == "retrieval"]
    assert retrievals, "ranking must have happened for this test to mean anything"
    for ev in retrievals:
        assert "xyzzyquux" not in ev["payload"]["query"]
    # ...while the charter really was in the prompt those retrievals fed.
    assert "xyzzyquux" in runner.calls[0]["prompt"]


def test_an_older_database_migrates_to_the_charter_schema(tmp_path):
    """Slice 3c adds a column to `attempts` and one to `verdicts`, which
    `CREATE TABLE IF NOT EXISTS` never applies to a db that already exists. A
    pre-charter store must open, gain both columns, and read back sanely: no
    charter was in effect for its old attempts, and its old verdicts recorded
    no findings — which is exactly what NULL and '' mean here."""
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
            created_at REAL NOT NULL, updated_at REAL NOT NULL);
        CREATE TABLE attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL, kind TEXT NOT NULL,
            agent_role TEXT NOT NULL, model TEXT NOT NULL,
            started_at REAL NOT NULL, finished_at REAL,
            output TEXT NOT NULL DEFAULT '',
            tokens_in INTEGER NOT NULL DEFAULT 0,
            tokens_out INTEGER NOT NULL DEFAULT 0,
            cost_usd REAL NOT NULL DEFAULT 0.0);
        CREATE TABLE verdicts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL, attempt_id INTEGER,
            kind TEXT NOT NULL, confidence REAL NOT NULL,
            reasoning TEXT NOT NULL DEFAULT '',
            tests_passed INTEGER, created_at REAL NOT NULL);
        INSERT INTO tasks (title, goal, acceptance_criteria, created_at,
                           updated_at)
        VALUES ('Legacy task', 'do it', 'works', 0, 0);
        INSERT INTO attempts (task_id, kind, agent_role, model, started_at,
                              finished_at)
        VALUES (1, 'worker', 'worker', 'mock', 0, 1);
        INSERT INTO verdicts (task_id, kind, confidence, created_at)
        VALUES (1, 'approve', 0.9, 0);
        """
    )
    raw.commit()
    raw.close()

    migrated = Store(db)
    try:
        metrics = migrated.task_metrics(1)
        # No charter existed, so no attempt claims to have run under one.
        assert metrics["charter_versions"] == []
        assert metrics["verdicts"][0]["findings"] == ""
        # And the charter table itself is usable in the same db.
        assert migrated.charter_active() is None
        assert migrated.charter_set(CHARTER) == 1
        assert migrated.charter_active() == (1, CHARTER)
    finally:
        migrated.close()


# -- the sibling-task parallel case -------------------------------------------


SIBLING_PLAN = """```json
{"tasks": [
  {"ref": "left", "title": "Write the parser",
   "goal": "Implement parse()",
   "acceptance_criteria": "Handles empty input",
   "risk_level": 1, "depends_on": []},
  {"ref": "right", "title": "Write the printer",
   "goal": "Implement print()",
   "acceptance_criteria": "Round-trips the parser output",
   "risk_level": 1, "depends_on": []}
]}
```"""


class GraphRunner:
    """Content-routed, thread-safe runner (see tests/test_planner.py).

    `barrier_titles` is how concurrency is proved rather than timed: the named
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

    def run(self, system_prompt, prompt, model, tools=None, cwd=None):
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
                time.sleep(0.01)
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


def test_sibling_tasks_share_the_charter_and_nothing_else(store, tmp_path):
    """The slice's headline claim, stated as an assertion: two tasks with no
    edge between them see the same house rules and no other shared context.

    `_upstream_block` carries output along *edges*, and siblings have none — so
    the charter is the only thing these two share, which is exactly what it
    fixes and exactly the limit of what it fixes."""
    store.charter_set(CHARTER)
    runner = GraphRunner(
        plan=SIBLING_PLAN,
        barrier_titles=("Write the parser", "Write the printer"),
    )
    loop = make_graph_loop(
        store, tmp_path, runner, max_parallel_workers=2, infra_max_retries=0
    )
    loop.plan("Build a round-trip library", "Parses and prints")
    assert loop.run() == 2

    tasks = [t for t in store.list_tasks() if t.kind == "task"]
    assert len(tasks) == 2
    assert all(t.status == TaskStatus.DONE for t in tasks)
    # The barrier only releases when both siblings are in flight together.
    assert runner.max_active >= 2

    worker_prompts = [p for p in runner.prompts if p.startswith("# Task:")]
    validator_prompts = [p for p in runner.prompts if p.startswith("# Task under")]
    assert len(worker_prompts) == 2 and len(validator_prompts) == 2
    for prompt in worker_prompts + validator_prompts:
        # A validator that does not know the house rules cannot catch a
        # violation of them, so both roles carry it.
        assert CHARTER in prompt
    for prompt in worker_prompts:
        assert "## Upstream results" not in prompt

    for t in tasks:
        assert store.task_metrics(t.id)["charter_versions"] == [1]


def test_sequential_default_is_unchanged_with_a_charter(store, tmp_path):
    """max_parallel_workers still defaults to 1 with a charter set: the added
    block is the only difference from today's loop."""
    store.charter_set(CHARTER)
    runner = GraphRunner(plan=SIBLING_PLAN)
    loop = make_graph_loop(store, tmp_path, runner)
    assert LoopConfig().max_parallel_workers == 1
    loop.plan("Build a round-trip library", "Parses and prints")
    assert loop.run() == 2

    assert runner.max_active == 1, "the default loop must not run anything in parallel"
    workers = {
        e["payload"]["worker"] for e in store.events() if e["kind"] == "task_claimed"
    }
    assert workers == {"loop"}


# -- ITEM 2: the validator shows its work -------------------------------------


APPROVE_WITH_FINDINGS = (
    "VERDICT: approve CONFIDENCE: 0.92 TESTS: pass\n"
    "\n"
    "FINDINGS:\n"
    "- Checked unicode handling -> clean, covered by test_unicode.\n"
    "- Checked the empty-string case -> clean.\n"
    "\n"
    "Meets all criteria."
)


def test_findings_are_recorded_on_an_approve(store):
    """Findings on an approve are the normal case, not an exception: a clean
    review still says what it checked."""
    task = add_task(store)
    loop, _ = make_loop(store, ["out", APPROVE_WITH_FINDINGS])
    loop.run_task(task)

    assert task.status == TaskStatus.DONE  # rule unchanged
    verdict = store.task_metrics(task.id)["verdicts"][0]
    assert "Checked unicode handling" in verdict["findings"]
    assert "Checked the empty-string case" in verdict["findings"]


@pytest.mark.parametrize("level", ["#", "##", "###", "######"])
def test_a_section_heading_after_the_findings_ends_the_section(store, level):
    """The marker is soft: findings run to the end of the reply, so a validator
    that keeps writing is recorded rather than silently cut. A markdown section
    heading is the one terminator, at *any* level — matching only `#` and `##`
    would quietly fold a `### Reasoning` section into the evidence while the
    docs promised otherwise."""
    reply = (
        "VERDICT: approve CONFIDENCE: 0.92 TESTS: pass\n"
        "FINDINGS:\n"
        "- Checked unicode handling -> clean.\n"
        "\n"
        f"{level} Reasoning\n"
        "Nothing here is a finding.\n"
    )
    task = add_task(store)
    loop, _ = make_loop(store, ["out", reply])
    loop.run_task(task)

    findings = store.task_metrics(task.id)["verdicts"][0]["findings"]
    assert "Checked unicode handling" in findings
    assert "Nothing here is a finding" not in findings
    assert task.status == TaskStatus.DONE


def test_a_hash_quoted_inside_a_finding_does_not_truncate_the_list(store):
    """A code validator quoting a comment is the common case here, not an exotic
    one. Treating any `#`-prefixed line as a heading dropped every finding after
    it with no signal — the record this whole item exists to create, silently
    incomplete. A real section heading follows a blank line; a quoted one does
    not, and the ambiguity is resolved toward keeping too much rather than too
    little, because over-inclusion costs tidiness and under-inclusion destroys
    evidence."""
    reply = (
        "VERDICT: revise CONFIDENCE: 0.55 TESTS: fail\n"
        "FINDINGS:\n"
        "- Checked the module header -> it still says\n"
        "# TODO: remove this shim\n"
        "- Checked the unicode path -> raises on combining marks.\n"
    )
    task = add_task(store)
    loop, _ = make_loop(store, ["out", reply, "fixed", APPROVE])
    loop.run_task(task)

    findings = store.task_metrics(task.id)["verdicts"][0]["findings"]
    assert "Checked the module header" in findings
    assert "TODO: remove this shim" in findings
    assert "raises on combining marks" in findings, (
        "a quoted `#` line must not silently truncate the findings after it"
    )


def test_missing_findings_never_fails_the_attempt(store):
    """The direct expression of the constraint. Today's bare APPROVE has no
    findings section at all, and that must cost nothing: no exception, no
    re-run of a paid model call, no change of outcome."""
    task = add_task(store)
    loop, _ = make_loop(store, ["out", APPROVE])
    loop.run_task(task)

    assert task.status == TaskStatus.DONE
    metrics = store.task_metrics(task.id)
    assert metrics["verdicts"][0]["findings"] == ""
    assert metrics["attempts"] == 2  # exactly worker + validator
    assert metrics["tokens"] > 0
    # No _with_retry re-run of an attempt that had already been paid for.
    assert not [e for e in store.events(task.id) if e["kind"] == "infra_error"]
    # The event records the absence as a flag, and never the text.
    verdict_events = [e for e in store.events(task.id) if e["kind"] == "verdict"]
    assert verdict_events[0]["payload"]["has_findings"] is False
    assert "findings" not in verdict_events[0]["payload"]


def test_findings_change_no_decision(store, tmp_path):
    """Same kind and confidence, with and without findings — identical outcome.
    Findings are evidence; the loop reads exactly one number and it isn't this
    one. Including an approve whose findings read like complaints."""
    grumpy = (
        "VERDICT: approve CONFIDENCE: 0.92 TESTS: pass\n"
        "FINDINGS:\n"
        "- Checked naming -> I would have called it to_slug, not slugify.\n"
        "- Checked the docstring -> terse and I dislike it.\n"
        "Approving anyway; the criteria are met."
    )
    results = []
    for i, verdict in enumerate((APPROVE, APPROVE_WITH_FINDINGS, grumpy)):
        st = Store(tmp_path / f"c{i}.db")
        try:
            task = add_task(st)
            loop, _ = make_loop(
                st, ["out", verdict], workspace_root=str(tmp_path / "w")
            )
            loop.run_task(task)
            results.append((task.status, task.revision_count))
        finally:
            st.close()

    assert results[0] == results[1] == results[2]
    assert results[0] == (TaskStatus.DONE, 0)


def test_reasoning_and_revision_feedback_are_unchanged(store):
    """Findings are copied out of `reasoning`, never moved out of it. The loop
    feeds `reasoning` back to the worker as revision feedback, so subtracting
    the findings would silently strip the most actionable part of every
    revision prompt."""
    revise = (
        "VERDICT: revise CONFIDENCE: 0.55 TESTS: fail\n"
        "FINDINGS:\n"
        "- Checked the empty input path -> raises IndexError, needs a guard.\n"
        "Address the finding above."
    )
    task = add_task(store)
    loop, runner = make_loop(store, ["v1", revise, "v2 fixed", APPROVE])
    loop.run_task(task)

    assert task.status == TaskStatus.DONE
    assert task.revision_count == 1
    # The actionable detail lived *inside* the findings section, and still
    # reached the worker.
    assert "raises IndexError, needs a guard" in runner.calls[2]["prompt"]
    verdict = store.task_metrics(task.id)["verdicts"][0]
    assert "raises IndexError" in verdict["findings"]


def test_unparseable_verdict_still_escalates_at_zero(store):
    """Findings never rescue an unparseable verdict — the first line is still
    the only thing the machine trusts."""
    task = add_task(store)
    reply = (
        "FINDINGS:\n"
        "- Checked everything -> all good, ship it.\n"
        "I forgot the verdict line entirely."
    )
    loop, _ = make_loop(store, ["out", reply])
    loop.run_task(task)

    assert task.status == TaskStatus.NEEDS_HUMAN
    verdict = store.task_metrics(task.id)["verdicts"][0]
    assert verdict["kind"] == "escalate"
    assert verdict["confidence"] == 0.0


def test_oversized_findings_are_truncated_not_raised(store):
    """Truncation is right for findings and wrong for the charter, and the
    asymmetry is principled: the charter is an input that must arrive whole,
    findings are an account of something that already happened."""
    huge = "VERDICT: approve CONFIDENCE: 0.92 TESTS: pass\nFINDINGS:\n" + (
        "- checked a thing -> fine.\n" * 40_000
    )
    task = add_task(store)
    loop, _ = make_loop(store, ["out", huge])
    loop.run_task(task)

    assert task.status == TaskStatus.DONE
    findings = store.task_metrics(task.id)["verdicts"][0]["findings"]
    assert len(findings) == _MAX_FINDINGS_CHARS
