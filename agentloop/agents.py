"""Worker and validator wrappers: build prompts, invoke the ModelRunner,
record attempts/metrics in the store, parse validator verdicts."""

from __future__ import annotations

import re

from .config import estimate_cost_usd
from .memory import MemoryService
from .models import RunResult, Task, TestResult, Verdict, VerdictKind
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
) -> tuple[RunResult, int]:
    """Run one agent invocation with full attempt/metrics bookkeeping."""
    # The model call sits deliberately *between* two transactions, never inside
    # one: the store lock must not be held across a network call. Each paired
    # write (attempt row + its audit event) is atomic on its own.
    with store.transaction():
        attempt_id = store.start_attempt(task.id, kind, role, model)
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
                    "tool": call.get("tool", "unknown"),
                    "input": call.get("input"),
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


def _memory_block(
    memory: MemoryService | None, query: str = "", task_id: int | None = None
) -> str:
    """Approved facts only — unvetted memory never reaches a prompt.

    `query` is what the facts are ranked against: the task the agent is about to
    work on. Passing it is what turns memory injection from "the first 20 facts
    alphabetically" into "the 20 facts about this task"."""
    if memory is None:
        return ""
    facts = memory.facts_for_prompt(query=query, task_id=task_id)
    return f"\n## Known project facts\n{facts}\n" if facts else ""


def _retrieval_query(task: Task, extra: str = "") -> str:
    """The text a task's memory is retrieved against.

    Title/goal/criteria are the stable statement of what the task needs;
    `extra` (validator feedback, or the output under review) is what makes a
    revision retrieve differently from the first attempt. Bounded, so a huge
    worker output can't drown the task's own vocabulary."""
    base = f"{task.title}\n{task.goal}\n{task.acceptance_criteria}"
    return f"{base}\n{extra[:_MAX_QUERY_EXTRA_CHARS]}" if extra else base


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
    prompt += _memory_block(memory, _retrieval_query(task, feedback), task.id)
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
    prompt += _memory_block(memory, _retrieval_query(task, worker_output), task.id)
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
