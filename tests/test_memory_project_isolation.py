"""Phase 5 of the multi-project dashboard slice: `project_id` threaded
through `MemoryService` and into the worker/validator/planner prompts,
closing the exact gap CLAUDE.md's own roadmap note names as a real
correctness risk (Project A's facts reaching Project B's prompts).
"""

import pytest

from agentloop.memory import MemoryService
from agentloop.models import Task
from agentloop.retrieval import HashingBackend
from agentloop.store import Store


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "isolation.db")
    yield s
    s.close()


@pytest.fixture()
def memory(store):
    return MemoryService(store, promote_threshold=3, backend=HashingBackend())


@pytest.fixture()
def two_projects(store, tmp_path):
    a_dir = tmp_path / "repo-a"
    b_dir = tmp_path / "repo-b"
    a_dir.mkdir()
    b_dir.mkdir()
    a = store.create_project("A", str(a_dir))
    b = store.create_project("B", str(b_dir))
    return a, b


def a_task(store, project_id, title="T") -> int:
    task = Task(id=None, title=title, goal="g", acceptance_criteria="c")
    task.project_id = project_id
    store.add_task(task)
    return task.id


# -- the design doc's own stated correctness risk, made executable ---------


def test_facts_for_prompt_never_leaks_across_projects(store, memory, two_projects):
    """Two projects, each with an approved fact under the SAME key, ranked
    against the SAME query text. Project A's prompt must never contain
    Project B's value, and vice versa."""
    a, b = two_projects
    store.memory_write("project", "style", "use tabs", approved=True, project_id=a)
    store.memory_write("project", "style", "use spaces", approved=True, project_id=b)

    block_a, _ = memory.facts_for_prompt(query="use tabs use spaces", project_id=a)
    block_b, _ = memory.facts_for_prompt(query="use tabs use spaces", project_id=b)

    assert "use tabs" in block_a
    assert "use spaces" not in block_a
    assert "use spaces" in block_b
    assert "use tabs" not in block_b


def test_facts_for_prompt_with_no_project_id_uses_the_default_not_every_project(
    store, memory, two_projects
):
    """`facts_for_prompt`'s own `project_id=None` must resolve to ONE
    project (the active default), never `Store.memory_list`'s "every
    project" meaning -- a caller passing None must still get one project's
    facts, not a blend of every registered project's."""
    a, b = two_projects
    default_id = store.resolve_project(None)
    store.memory_write(
        "project", "unscoped", "default value", approved=True, project_id=default_id
    )
    store.memory_write("project", "unscoped", "a value", approved=True, project_id=a)
    store.memory_write("project", "unscoped", "b value", approved=True, project_id=b)

    block, _ = memory.facts_for_prompt()
    assert "default value" in block
    assert "a value" not in block
    assert "b value" not in block


# -- read()'s promotion must target the READ project, not the active one ---


def test_read_promotes_against_the_read_project_not_the_active_one(
    store, memory, two_projects
):
    a, b = two_projects
    store.set_default_project(b)
    store.memory_write("project", "hot", "deploy rollout", approved=True, project_id=a)

    for i in range(3):
        value = memory.read(
            "project", "hot", task_id=a_task(store, a, f"T{i}"), project_id=a
        )
        assert value == "deploy rollout"

    a_loop = [
        r for r in store.memory_list(tier="loop", project_id=a) if r["key"] == "hot"
    ]
    b_loop = [
        r for r in store.memory_list(tier="loop", project_id=b) if r["key"] == "hot"
    ]
    assert len(a_loop) == 1
    assert len(b_loop) == 0


def test_maybe_promote_with_no_project_id_resolves_the_default_not_every_project(
    store, memory, two_projects
):
    """[RED-FIRST] `maybe_promote`/`_find` must resolve an omitted
    `project_id` the same way `facts_for_prompt` does -- `Store.memory_list`
    (what `_find` calls) treats `project_id=None` as "every project"
    (Phase 2's deliberate meaning), which is the WRONG meaning at this seam.

    Project A's fact is pushed PAST the promote threshold (real hits, real
    distinct tasks) while the DEFAULT project holds no such fact at all.
    `maybe_promote("project", "hot")` with NO project_id must act on the
    default project's (empty) view and promote nothing -- an unresolved
    `_find` would instead scan every project, find A's now-hot row (the
    only "hot" row with enough hits), and silently promote a fact that has
    nothing to do with the default project."""
    a, b = two_projects
    default_id = store.resolve_project(None)
    assert default_id not in (a, b)

    store.memory_write("project", "hot", "a value", approved=True, project_id=a)
    # Push A's fact past promote_threshold=3 via real distinct-task reads,
    # scoped correctly to A -- this part of the pipeline (facts_for_prompt/
    # read with an EXPLICIT project_id) is already proven correct above.
    for i in range(3):
        memory.read("project", "hot", task_id=a_task(store, a, f"pre{i}"), project_id=a)
    # The read-path's own maybe_promote(project_id=a) call already promotes
    # it correctly -- confirm that, then demote it back to project tier so
    # the actual assertion below tests maybe_promote's OWN resolution, not
    # a promotion that already happened as a side effect of read().
    assert store.memory_list(tier="loop", project_id=a)[0]["key"] == "hot"
    loop_row_id = store.memory_list(tier="loop", project_id=a)[0]["id"]
    store._conn.execute(
        "UPDATE memory SET tier='project', hit_count=3 WHERE id=?", (loop_row_id,)
    )
    store._conn.commit()

    # maybe_promote with NO project_id must resolve to the (empty-of-"hot")
    # default project -- never silently act on A's now-eligible row.
    promoted = memory.maybe_promote("project", "hot")
    assert promoted is False
    a_rows = store.memory_list(tier="project", project_id=a)
    assert len(a_rows) == 1 and a_rows[0]["key"] == "hot"
    assert store.memory_list(tier="loop", project_id=a) == []


# -- facts_for_prompt's promotion (the DOMINANT path) must do the same -----


def test_facts_for_prompt_promotes_against_the_task_project_not_the_active_one(
    store, memory, two_projects
):
    """`agents._memory_block` -> `facts_for_prompt` -> `_record_reads` is the
    path every actual worker/validator/planner prompt goes through -- not
    the narrower `read()` path the test above covers. This is BLOCKING #2 of
    the plan's own fresh review, made executable."""
    a, b = two_projects
    store.set_default_project(b)
    store.memory_write("project", "hot", "deploy rollout", approved=True, project_id=a)

    for i in range(3):
        memory.facts_for_prompt(
            query="deploy rollout",
            task_id=a_task(store, a, f"T{i}"),
            project_id=a,
        )

    a_loop = [
        r for r in store.memory_list(tier="loop", project_id=a) if r["key"] == "hot"
    ]
    b_loop = [
        r for r in store.memory_list(tier="loop", project_id=b) if r["key"] == "hot"
    ]
    assert len(a_loop) == 1
    assert len(b_loop) == 0


# -- agents.run_worker seam: the actual prompt a worker receives -----------


def test_run_worker_prompt_never_contains_another_projects_facts(
    store, two_projects, tmp_path
):
    from agentloop.agents import run_worker
    from agentloop.config import LoopConfig
    from agentloop.models import TaskStatus
    from agentloop.registry import Registry
    from agentloop.runner import MockRunner

    a, b = two_projects
    store.memory_write("project", "style", "use tabs", approved=True, project_id=a)
    store.memory_write("project", "style", "use spaces", approved=True, project_id=b)
    mem = MemoryService(store, promote_threshold=3, backend=HashingBackend())

    task = Task(
        id=None,
        title="Use the house style",
        goal="Format the file per the house style.",
        acceptance_criteria="Matches project style.",
        status=TaskStatus.PENDING,
        project_id=a,
    )
    store.add_task(task)
    runner = MockRunner(["worker output"])
    config = LoopConfig(db_path=store.db_path)

    run_worker(store, runner, Registry.load(), task, memory=mem, config=config)

    assert len(runner.calls) == 1
    prompt = runner.calls[0]["prompt"]
    assert "use tabs" in prompt
    assert "use spaces" not in prompt
