"""Worker and validator wrappers: build prompts, invoke the ModelRunner,
record attempts/metrics in the store, parse validator verdicts."""

from __future__ import annotations

import json
import re

from .config import estimate_cost_usd
from .memory import MemoryService
from .models import (
    PlannedTask,
    RunResult,
    Task,
    TaskStatus,
    TestResult,
    Verdict,
    VerdictKind,
)
from .registry import Registry
from .runner import ModelRunner
from .store import Store

_VERDICT_RE = re.compile(
    r"VERDICT:\s*(approve|revise|escalate)\s*"
    r"CONFIDENCE:\s*([01](?:\.\d+)?)\s*"
    r"TESTS:\s*(pass|fail|na)",
    re.IGNORECASE,
)

# How much of the free-text context (feedback, or the output under review) feeds
# the memory retrieval query alongside the task definition.
_MAX_QUERY_EXTRA_CHARS = 2000

# How much of each completed dependency's output is handed to a dependent
# worker. Bounded because a task can wait on several others and their combined
# transcripts would otherwise be the largest thing in the prompt.
_MAX_UPSTREAM_CHARS = 2000

# How much of a tool's arguments the audit log keeps. The record answers "what
# was this tool called with", not "what did it write".
_MAX_TOOL_INPUT_CHARS = 2000
_MAX_TOOL_NAME_CHARS = 200

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)```", re.DOTALL | re.IGNORECASE)

# Largest planner reply worth attempting to parse. A real plan is a few KB; far
# past that the reply is runaway or hostile, and parsing it is the expensive
# part, so the check belongs before `json.loads`, not after.
_MAX_PLAN_REPLY_CHARS = 200_000


def _invoke(
    store: Store,
    runner: ModelRunner,
    task: Task,
    kind: str,
    role: str,
    model: str,
    system: str,
    prompt: str,
    tools: list[str] | None = None,
    retrieval: dict | None = None,
) -> tuple[RunResult, int]:
    """Run one agent invocation with full attempt/metrics bookkeeping."""
    # The model call sits deliberately *between* two transactions, never inside
    # one: the store lock must not be held across a network call. Each paired
    # write (attempt row + its audit event) is atomic on its own.
    with store.transaction():
        attempt_id = store.start_attempt(task.id, kind, role, model)
        # Which facts memory put in front of *this* agent on *this* round. It
        # goes in the opening transaction rather than the closing one because
        # the retrieval already happened — it built the prompt below — so a
        # failed model call must not lose the record of what was injected.
        # `tool_call` is its mirror image: those only exist once the run is over.
        if retrieval is not None:
            store.log_event(
                task.id,
                "retrieval",
                {
                    "attempt_id": attempt_id,
                    "agent_kind": kind,
                    "role": role,
                    **retrieval,
                },
            )
        store.log_event(
            task.id,
            f"{kind}_prompt",
            {"role": role, "prompt": prompt, "tools": list(tools or [])},
        )
    result = runner.run(system, prompt, model, tools)
    cost = estimate_cost_usd(
        result.model,
        result.tokens_in,
        result.tokens_out,
        result.cache_creation_tokens,
        result.cache_read_tokens,
    )
    with store.transaction():
        store.finish_attempt(
            attempt_id,
            result.output,
            result.tokens_in,
            result.tokens_out,
            cost,
            model=result.model,
            cache_creation_tokens=result.cache_creation_tokens,
            cache_read_tokens=result.cache_read_tokens,
        )
        # Provenance for what the agent actually did, not just what it said:
        # one event per tool use, in the same transaction as the attempt it
        # belongs to. Slice 5's approval policy layers on top of this record.
        for call in result.tool_calls:
            store.log_event(
                task.id,
                "tool_call",
                {
                    "attempt_id": attempt_id,
                    "agent_kind": kind,
                    "role": role,
                    "tool": _tool_name_repr(call.get("tool")),
                    "input": _tool_input_repr(call.get("input")),
                },
            )
        store.log_event(
            task.id,
            f"{kind}_output",
            {
                "role": role,
                "output": result.output,
                "tokens_in": result.tokens_in,
                "tokens_out": result.tokens_out,
                "cache_creation_tokens": result.cache_creation_tokens,
                "cache_read_tokens": result.cache_read_tokens,
                "cost_usd": cost,
            },
        )
    return result, attempt_id


def _tool_name_repr(value) -> str:
    """A tool's name as a bounded string, for the audit log only.

    Same hazard as `_tool_input_repr`, same transaction, and it was missed the
    first time: `tool` and `input` are encoded by the *one* `json.dumps` in
    `log_event`, so coercing only the input still let a non-string name roll
    back an already-paid `finish_attempt` and hand the call back to
    `_with_retry`. Duck-typed `ToolUseBlock`s make the name exactly as
    untrustworthy as the arguments.

    Not routed through `_tool_input_repr`: this field is rendered as a name by
    the dashboard and read as one by slice 5's policy, and `json.dumps` would
    turn `Read` into `"Read"`.
    """
    if value is None:
        return "unknown"
    if isinstance(value, str):
        return value[:_MAX_TOOL_NAME_CHARS]
    try:
        text = str(value)
    except Exception:
        # A `__str__` that raises would put us straight back in the transaction
        # this function exists to protect.
        text = f"<unrepresentable {type(value).__name__}>"
    return text[:_MAX_TOOL_NAME_CHARS]


def _tool_input_repr(value) -> str:
    """A tool's arguments as a bounded string, for the audit log only.

    Telemetry must never fail an attempt. `json.dumps` on a value it cannot
    encode raised inside `_invoke`'s closing transaction, rolling back
    `finish_attempt` — the output, tokens and cost of a model call already paid
    for — after which `_with_retry` ran the same call again. `ClaudeSDKRunner`
    sanitises its blocks, but the `ModelRunner` seam does not require it and a
    second provider is a new chance to get it wrong, so the coercion lives here,
    at the seam, rather than in one backend.

    Truncated because the log records what a tool was called with, not a copy of
    its payload: a megabyte of file content in an event row buys nothing.
    """
    try:
        text = json.dumps(value, sort_keys=True)
    except Exception:
        # `repr` is a fallback, not a guarantee — a __repr__ that raises would
        # put us straight back in the transaction this function exists to
        # protect, so the last resort names the type and nothing else.
        try:
            text = repr(value)
        except Exception:
            text = f"<unrepresentable {type(value).__name__}>"
    return text[:_MAX_TOOL_INPUT_CHARS]


def _memory_block(
    memory: MemoryService | None, query: str = "", task_id: int | None = None
) -> tuple[str, dict | None]:
    """Approved facts only — unvetted memory never reaches a prompt.

    `query` is what the facts are ranked against: the task the agent is about to
    work on. Passing it is what turns memory injection from "the first 20 facts
    alphabetically" into "the 20 facts about this task".

    Returns the block and its provenance, which the caller hands to `_invoke` so
    the retrieval is recorded against the attempt it fed. An unranked selection
    (no query, or ranking disabled) has no provenance and logs nothing.

    `task_id` is what any hit this injection earns is counted against — see
    `MemoryService._record_reads`."""
    if memory is None:
        return "", None
    facts, provenance = memory.facts_for_prompt(query=query, task_id=task_id)
    block = f"\n## Known project facts\n{facts}\n" if facts else ""
    return block, provenance


def _retrieval_query(task: Task, extra: str = "") -> str:
    """The text a task's memory is retrieved against.

    Title/goal/criteria are the stable statement of what the task needs;
    `extra` (validator feedback, or the output under review) is what makes a
    revision retrieve differently from the first attempt. Bounded, so a huge
    worker output can't drown the task's own vocabulary."""
    base = f"{task.title}\n{task.goal}\n{task.acceptance_criteria}"
    return f"{base}\n{extra[:_MAX_QUERY_EXTRA_CHARS]}" if extra else base


def _upstream_block(store: Store, task: Task) -> str:
    """What the tasks this one waited for actually produced.

    A dependency edge that only delays a task is a schedule, not a plan: the
    reason "write the tests" waits for "write slugify()" is that it needs to see
    slugify(). Bounded per upstream task so a fan-in node's prompt stays mostly
    about its own work.

    Only DONE dependencies contribute, and that is checked here rather than
    assumed from the claim gate. The gate holds at *claim* time, but this block
    is rebuilt on every revision round — so an upstream task rejected or redone
    mid-round would otherwise be fed downstream as "what this task waited for"
    when it is no longer a finished result at all.
    """
    if task.id is None:
        return ""
    outputs = []
    for dep_id in store.dependencies(task.id):
        dep = store.get_task(dep_id)
        if dep is None or dep.status != TaskStatus.DONE or not dep.output.strip():
            continue
        outputs.append(f"### {dep.title}\n{dep.output[:_MAX_UPSTREAM_CHARS]}")
    if not outputs:
        return ""
    return (
        "\n## Upstream results (output of the tasks this one depends on)\n"
        + "\n\n".join(outputs)
        + "\n"
    )


def _test_block(result: TestResult | None) -> str:
    """Real executed results, so the validator judges reality rather than the
    worker's account of it."""
    if result is None or result.status == "na":
        return ""
    return (
        f"\n## Executed test results (authoritative)\n"
        f"status: {result.status} (exit code {result.exit_code})\n"
        f"{result.summary}\n\n"
        f"```\n{result.stdout_tail[-1500:]}\n```\n"
    )


def run_worker(
    store: Store,
    runner: ModelRunner,
    registry: Registry,
    task: Task,
    feedback: str = "",
    memory: MemoryService | None = None,
    workspace: str | None = None,
    test_result: TestResult | None = None,
    handoff_summary: str | None = None,
) -> RunResult:
    spec = registry.get(task.worker_role)
    prompt = (
        f"# Task: {task.title}\n\n"
        f"## Goal\n{task.goal}\n\n"
        f"## Acceptance criteria\n{task.acceptance_criteria}\n"
    )
    memory_block, retrieval = _memory_block(
        memory, _retrieval_query(task, feedback), task.id
    )
    prompt += memory_block
    prompt += _upstream_block(store, task)
    if workspace:
        prompt += (
            f"\n## Workspace\nWrite your files and tests under `{workspace}`. "
            f"They are executed there automatically after you finish.\n"
        )
    if handoff_summary is not None:
        # Context-budget handoff (slice 1): the prior worker's context grew past
        # its budget, so a fresh instance continues from a compacted summary in
        # place of the raw transcript (previous output + feedback), which is
        # exactly what would have overflowed.
        prompt += (
            f"\n## Handoff summary of prior work (context compacted)\n"
            f"{handoff_summary}\n"
            f"\nContinue the task from this summary, addressing the feedback it "
            f"describes. Prior work is summarized above rather than repeated in "
            f"full."
        )
    elif feedback:
        prompt += (
            f"\n## Your previous output\n{task.output}\n"
            f"\n## Validator feedback (revision {task.revision_count})\n"
            f"{feedback}\n"
        )
        prompt += _test_block(test_result)
        prompt += "\nRevise your output to address the feedback."
    result, _ = _invoke(
        store,
        runner,
        task,
        "worker",
        spec.role,
        spec.model,
        spec.system_prompt,
        prompt,
        spec.tools,
        retrieval,
    )
    return result


def run_summarizer(
    store: Store,
    runner: ModelRunner,
    registry: Registry,
    task: Task,
    feedback: str = "",
    test_result: TestResult | None = None,
) -> RunResult:
    """Compact a task's working state for a context-budget handoff (slice 1).

    A ModelRunner call (so it works under MockRunner in tests). Recorded as its
    own attempt (kind='summarizer'), so its cost feeds the task budget cap but
    is kept separate from the worker's accumulated-context measure."""
    try:
        spec = registry.get("summarizer")
    except KeyError:
        # A hand-edited agents.json may predate the summarizer role; fall back
        # to the worker's spec so a handoff degrades rather than crashing.
        spec = registry.get(task.worker_role)
    prompt = (
        f"# Task being handed off: {task.title}\n\n"
        f"## Goal\n{task.goal}\n\n"
        f"## Acceptance criteria\n{task.acceptance_criteria}\n\n"
        f"## Work so far (latest worker output)\n{task.output}\n"
    )
    if feedback:
        prompt += f"\n## Latest validator feedback\n{feedback}\n"
    prompt += _test_block(test_result)
    prompt += (
        "\nSummarize this working state so a fresh worker instance can continue "
        "with no loss of what matters."
    )
    result, _ = _invoke(
        store,
        runner,
        task,
        "summarizer",
        spec.role,
        spec.model,
        spec.system_prompt,
        prompt,
        spec.tools,
    )
    return result


class PlanError(ValueError):
    """A planner reply that cannot become a task graph.

    Raised for anything unusable — unparseable JSON, a missing field, a
    dangling `depends_on` ref, a cycle, an oversized plan. The caller discards
    the *whole* plan and escalates: a half-applied decomposition is worse than
    none, because the missing half is invisible while the present half looks
    like a complete plan someone approved.
    """


def run_planner(
    store: Store,
    runner: ModelRunner,
    registry: Registry,
    plan_task: Task,
    memory: MemoryService | None = None,
) -> RunResult:
    """Decompose a goal into a task graph (roadmap slice 3).

    Recorded as its own attempt (kind='planner') against the plan row, so the
    decomposition is auditable and its cost is attributed like any other agent
    invocation. Returns the raw reply; `parse_plan` turns it into nodes.
    """
    spec = registry.get("planner")
    prompt = (
        f"# Goal to decompose\n{plan_task.goal}\n\n"
        f"## Acceptance criteria for the goal as a whole\n"
        f"{plan_task.acceptance_criteria}\n"
    )
    memory_block, retrieval = _memory_block(
        memory, _retrieval_query(plan_task), plan_task.id
    )
    prompt += memory_block
    prompt += (
        "\nDecompose this into independently executable tasks and their "
        "dependencies, in the JSON format your instructions specify."
    )
    result, _ = _invoke(
        store,
        runner,
        plan_task,
        "planner",
        spec.role,
        spec.model,
        spec.system_prompt,
        prompt,
        spec.tools,
        retrieval,
    )
    return result


def parse_plan(text: str, max_tasks: int) -> list[PlannedTask]:
    """Parse a planner reply into graph nodes, or raise `PlanError`.

    Validation is deliberately all-or-nothing and happens *before* anything is
    written: refs must be unique, every `depends_on` must name a task in the
    same plan, and the edges must form a DAG. A plan that fails any of these is
    not repaired or partially applied — it escalates to a human, the same way an
    unparseable verdict escalates rather than being guessed at.
    """
    # Bound the input before parsing it. `max_tasks` is a cap on the *parsed*
    # plan, which is too late: a runaway or hostile reply costs the CPU and
    # memory of parsing it first, and deep nesting raises RecursionError, which
    # is not a JSONDecodeError and would escape as a traceback rather than an
    # escalation. Model replies that are legitimately plans are far under this.
    if len(text) > _MAX_PLAN_REPLY_CHARS:
        raise PlanError(
            f"planner reply is {len(text)} chars, over the "
            f"{_MAX_PLAN_REPLY_CHARS} limit; refusing to parse it"
        )
    fenced = _JSON_FENCE_RE.search(text)
    raw = fenced.group(1) if fenced else text
    try:
        data = json.loads(raw.strip())
    except RecursionError as exc:
        raise PlanError("planner reply is nested too deeply to parse") from exc
    except ValueError as exc:  # JSONDecodeError is a ValueError
        raise PlanError(f"planner reply is not valid JSON: {exc}") from exc

    # Accept the documented {"tasks": [...]} shape, and a bare array, which is
    # the one deviation a model reliably makes and which is unambiguous anyway.
    if isinstance(data, dict):
        items = data.get("tasks")
    elif isinstance(data, list):
        items = data
    else:
        items = None
    if not isinstance(items, list):
        raise PlanError("planner reply has no 'tasks' array")
    if not items:
        raise PlanError("planner returned an empty plan (no tasks)")
    if len(items) > max_tasks:
        raise PlanError(
            f"plan has {len(items)} tasks, more than the {max_tasks} allowed "
            f"(max_plan_tasks)"
        )

    planned: list[PlannedTask] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise PlanError(f"task #{i + 1} is not an object")
        ref = str(item.get("ref") or "").strip() or f"task-{i + 1}"
        missing = [f for f in ("title", "goal") if not str(item.get(f) or "").strip()]
        if missing:
            raise PlanError(f"task {ref!r} is missing: {', '.join(missing)}")
        criteria = str(item.get("acceptance_criteria") or "").strip()
        if not criteria:
            # The validator judges strictly against acceptance criteria, so a
            # task without any is a task no validator can ever approve.
            raise PlanError(f"task {ref!r} is missing: acceptance_criteria")
        try:
            risk = int(item.get("risk_level", 1))
        except (TypeError, ValueError):
            raise PlanError(f"task {ref!r} has a non-numeric risk_level") from None
        if risk not in (0, 1, 2):
            raise PlanError(f"task {ref!r} has risk_level {risk} (expected 0, 1 or 2)")
        deps = item.get("depends_on") or []
        if not isinstance(deps, list):
            raise PlanError(f"task {ref!r} has a non-list depends_on")
        # De-duplicate: the edge table is keyed on (task, depends_on), so a
        # repeated ref inserts one row. Counting it twice would make the
        # `plan_created` event and the `task_dependency` events disagree with
        # the graph they claim to describe.
        deps = list(dict.fromkeys(str(d).strip() for d in deps))
        planned.append(
            PlannedTask(
                ref=ref,
                title=str(item["title"]).strip(),
                goal=str(item["goal"]).strip(),
                acceptance_criteria=criteria,
                risk_level=risk,
                depends_on=deps,
            )
        )

    refs = [p.ref for p in planned]
    if len(set(refs)) != len(refs):
        raise PlanError("plan reuses a task ref; refs must be unique")
    known = set(refs)
    for p in planned:
        for dep in p.depends_on:
            if dep not in known:
                raise PlanError(
                    f"task {p.ref!r} depends on {dep!r}, which is not in this plan"
                )
            if dep == p.ref:
                raise PlanError(f"task {p.ref!r} depends on itself (cycle)")
    _reject_cycles(planned)
    return planned


def _reject_cycles(planned: list[PlannedTask]) -> None:
    """Raise if the plan's edges are not a DAG.

    Kahn's algorithm: repeatedly remove nodes with no unmet dependency. Whatever
    remains is exactly the set of nodes trapped in (or downstream of) a cycle.
    """
    remaining = {p.ref: set(p.depends_on) for p in planned}
    while True:
        ready = [ref for ref, deps in remaining.items() if not deps]
        if not ready:
            break
        for ref in ready:
            del remaining[ref]
        for deps in remaining.values():
            deps.difference_update(ready)
    if remaining:
        raise PlanError(
            "plan dependencies contain a cycle involving: "
            + ", ".join(sorted(remaining))
        )


def run_validator(
    store: Store,
    runner: ModelRunner,
    registry: Registry,
    task: Task,
    worker_output: str,
    memory: MemoryService | None = None,
    test_result: TestResult | None = None,
) -> tuple[Verdict, int]:
    spec = registry.get(task.validator_role)
    prompt = (
        f"# Task under review: {task.title}\n\n"
        f"## Goal\n{task.goal}\n\n"
        f"## Acceptance criteria\n{task.acceptance_criteria}\n\n"
        f"## Worker output\n{worker_output}\n"
    )
    memory_block, retrieval = _memory_block(
        memory, _retrieval_query(task, worker_output), task.id
    )
    prompt += memory_block
    prompt += _test_block(test_result)
    result, attempt_id = _invoke(
        store,
        runner,
        task,
        "validator",
        spec.role,
        spec.model,
        spec.system_prompt,
        prompt,
        spec.tools,
        retrieval,
    )
    return parse_verdict(result.output), attempt_id


def parse_verdict(text: str) -> Verdict:
    """Parse the validator's structured first line. An unparseable verdict is
    itself a failure signal -> escalate at confidence 0 (never guess-approve)."""
    m = _VERDICT_RE.search(text)
    if not m:
        return Verdict(
            kind=VerdictKind.ESCALATE,
            confidence=0.0,
            reasoning=f"Unparseable validator output:\n{text}",
        )
    kind = VerdictKind(m.group(1).lower())
    confidence = max(0.0, min(1.0, float(m.group(2))))
    tests = {"pass": True, "fail": False, "na": None}[m.group(3).lower()]
    reasoning = text[m.end() :].strip()
    return Verdict(
        kind=kind, confidence=confidence, reasoning=reasoning, tests_passed=tests
    )
