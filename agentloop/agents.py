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


def _invoke(store: Store, runner: ModelRunner, task: Task, kind: str,
            role: str, model: str, system: str, prompt: str,
            tools: list[str] | None = None) -> tuple[RunResult, int]:
    """Run one agent invocation with full attempt/metrics bookkeeping."""
    attempt_id = store.start_attempt(task.id, kind, role, model)
    store.log_event(task.id, f"{kind}_prompt",
                    {"role": role, "prompt": prompt, "tools": list(tools or [])})
    result = runner.run(system, prompt, model, tools)
    cost = estimate_cost_usd(result.model, result.tokens_in, result.tokens_out,
                             result.cache_creation_tokens,
                             result.cache_read_tokens)
    store.finish_attempt(attempt_id, result.output, result.tokens_in,
                         result.tokens_out, cost, model=result.model,
                         cache_creation_tokens=result.cache_creation_tokens,
                         cache_read_tokens=result.cache_read_tokens)
    store.log_event(task.id, f"{kind}_output", {
        "role": role, "output": result.output,
        "tokens_in": result.tokens_in, "tokens_out": result.tokens_out,
        "cache_creation_tokens": result.cache_creation_tokens,
        "cache_read_tokens": result.cache_read_tokens,
        "cost_usd": cost})
    return result, attempt_id


def _memory_block(memory: MemoryService | None) -> str:
    """Approved facts only — unvetted memory never reaches a prompt."""
    if memory is None:
        return ""
    facts = memory.facts_for_prompt()
    return f"\n## Known project facts\n{facts}\n" if facts else ""


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


def run_worker(store: Store, runner: ModelRunner, registry: Registry,
               task: Task, feedback: str = "",
               memory: MemoryService | None = None,
               workspace: str | None = None,
               test_result: TestResult | None = None) -> RunResult:
    spec = registry.get(task.worker_role)
    prompt = (
        f"# Task: {task.title}\n\n"
        f"## Goal\n{task.goal}\n\n"
        f"## Acceptance criteria\n{task.acceptance_criteria}\n"
    )
    prompt += _memory_block(memory)
    if workspace:
        prompt += (
            f"\n## Workspace\nWrite your files and tests under `{workspace}`. "
            f"They are executed there automatically after you finish.\n"
        )
    if feedback:
        prompt += (
            f"\n## Your previous output\n{task.output}\n"
            f"\n## Validator feedback (revision {task.revision_count})\n"
            f"{feedback}\n"
        )
        prompt += _test_block(test_result)
        prompt += "\nRevise your output to address the feedback."
    result, _ = _invoke(store, runner, task, "worker", spec.role, spec.model,
                        spec.system_prompt, prompt, spec.tools)
    return result


def run_validator(store: Store, runner: ModelRunner, registry: Registry,
                  task: Task, worker_output: str,
                  memory: MemoryService | None = None,
                  test_result: TestResult | None = None
                  ) -> tuple[Verdict, int]:
    spec = registry.get(task.validator_role)
    prompt = (
        f"# Task under review: {task.title}\n\n"
        f"## Goal\n{task.goal}\n\n"
        f"## Acceptance criteria\n{task.acceptance_criteria}\n\n"
        f"## Worker output\n{worker_output}\n"
    )
    prompt += _memory_block(memory)
    prompt += _test_block(test_result)
    result, attempt_id = _invoke(store, runner, task, "validator", spec.role,
                                 spec.model, spec.system_prompt, prompt,
                                 spec.tools)
    return parse_verdict(result.output), attempt_id


def parse_verdict(text: str) -> Verdict:
    """Parse the validator's structured first line. An unparseable verdict is
    itself a failure signal -> escalate at confidence 0 (never guess-approve)."""
    m = _VERDICT_RE.search(text)
    if not m:
        return Verdict(kind=VerdictKind.ESCALATE, confidence=0.0,
                       reasoning=f"Unparseable validator output:\n{text}")
    kind = VerdictKind(m.group(1).lower())
    confidence = max(0.0, min(1.0, float(m.group(2))))
    tests = {"pass": True, "fail": False, "na": None}[m.group(3).lower()]
    reasoning = text[m.end():].strip()
    return Verdict(kind=kind, confidence=confidence, reasoning=reasoning,
                   tests_passed=tests)
