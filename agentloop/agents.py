"""Worker and validator wrappers: build prompts, invoke the ModelRunner,
record attempts/metrics in the store, parse validator verdicts."""

from __future__ import annotations

import re

from .config import estimate_cost_usd
from .models import RunResult, Task, Verdict, VerdictKind
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
            role: str, model: str, system: str,
            prompt: str) -> tuple[RunResult, int]:
    """Run one agent invocation with full attempt/metrics bookkeeping."""
    attempt_id = store.start_attempt(task.id, kind, role, model)
    store.log_event(task.id, f"{kind}_prompt", {"role": role, "prompt": prompt})
    result = runner.run(system, prompt, model)
    cost = estimate_cost_usd(result.model, result.tokens_in, result.tokens_out)
    store.finish_attempt(attempt_id, result.output, result.tokens_in,
                         result.tokens_out, cost)
    store.log_event(task.id, f"{kind}_output", {
        "role": role, "output": result.output,
        "tokens_in": result.tokens_in, "tokens_out": result.tokens_out,
        "cost_usd": cost})
    return result, attempt_id


def run_worker(store: Store, runner: ModelRunner, registry: Registry,
               task: Task, feedback: str = "") -> RunResult:
    spec = registry.get(task.worker_role)
    prompt = (
        f"# Task: {task.title}\n\n"
        f"## Goal\n{task.goal}\n\n"
        f"## Acceptance criteria\n{task.acceptance_criteria}\n"
    )
    if feedback:
        prompt += (
            f"\n## Your previous output\n{task.output}\n"
            f"\n## Validator feedback (revision {task.revision_count})\n"
            f"{feedback}\n\nRevise your output to address the feedback."
        )
    result, _ = _invoke(store, runner, task, "worker", spec.role, spec.model,
                        spec.system_prompt, prompt)
    return result


def run_validator(store: Store, runner: ModelRunner, registry: Registry,
                  task: Task, worker_output: str) -> tuple[Verdict, int]:
    spec = registry.get(task.validator_role)
    prompt = (
        f"# Task under review: {task.title}\n\n"
        f"## Goal\n{task.goal}\n\n"
        f"## Acceptance criteria\n{task.acceptance_criteria}\n\n"
        f"## Worker output\n{worker_output}\n"
    )
    result, attempt_id = _invoke(store, runner, task, "validator", spec.role,
                                 spec.model, spec.system_prompt, prompt)
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
