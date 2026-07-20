"""The orchestration loop (spec §4). Sequential for now by design; the store
schema already supports parallel workers later.

Decision rules per validation round:
- worker replies `ESCALATE:`            -> needs_human (genuine ambiguity)
- verdict escalate OR conf < severe     -> needs_human (severe disagreement)
- conf >= approve_threshold AND tests not failing
                                        -> done (or needs_human sign-off if
                                           risk_level >= human_review level)
- otherwise                             -> revise, bounded by max_revisions;
                                           exhausted retries -> needs_human
- budget cap exceeded at any point      -> needs_human (never burn unbounded)

"Tests not failing" means the *executed* result (spec §5). Tests run in the
task's workspace between the worker and the validator; the validator sees the
real output, and the gate consults the real status rather than the validator's
self-reported TESTS: field. A validator claiming pass against an executed fail
is recorded as a `test_disagreement` event — the loop measures its validators.
"""

from __future__ import annotations

from .agents import run_validator, run_worker
from .config import LoopConfig
from .executor import TestExecutor, clear_workspace, workspace_for
from .memory import MemoryService
from .models import Task, TaskStatus, TestResult, VerdictKind
from .registry import Registry
from .runner import ModelRunner
from .store import Store


class Loop:
    def __init__(self, store: Store, runner: ModelRunner,
                 registry: Registry, config: LoopConfig,
                 executor: TestExecutor | None = None,
                 memory: MemoryService | None = None):
        self.store = store
        self.runner = runner
        self.registry = registry
        self.config = config
        self.executor = executor or TestExecutor(
            command=config.test_command,
            timeout_s=config.test_timeout_s,
            enabled=config.allow_test_exec,
        )
        self.memory = memory or MemoryService(
            store, promote_threshold=config.memory_promote_threshold)

    # -- public API ----------------------------------------------------------

    def run(self, max_tasks: int | None = None) -> int:
        """Process pending tasks sequentially. Returns tasks processed.
        Safe to call after a crash/restart: state lives in the store."""
        processed = 0
        while max_tasks is None or processed < max_tasks:
            task = self.store.next_pending_task()
            if task is None:
                break
            self.run_task(task)
            processed += 1
        return processed

    def run_task(self, task: Task) -> Task:
        feedback = ""
        test_result = TestResult()
        while True:
            if self._budget_tripped(task):
                return task

            # Worker self-checks in its own output (spec §4.2–4.3).
            self.store.set_status(task, TaskStatus.IN_PROGRESS)
            ws = workspace_for(self.config.workspace_root, task.id, create=True)
            result = run_worker(self.store, self.runner, self.registry,
                                task, feedback, memory=self.memory,
                                workspace=str(ws), test_result=test_result)
            if result.output.strip().upper().startswith("ESCALATE:"):
                self.store.set_status(
                    task, TaskStatus.NEEDS_HUMAN,
                    reason=f"Worker ambiguity: {result.output.strip()[9:].strip()}")
                return task
            task.output = result.output
            self.store.update_task(task)

            # Tests are part of validation, executed for real (spec §5).
            self.store.set_status(task, TaskStatus.TESTING)
            test_result = self.executor.run(ws)
            self.store.add_test_run(task.id, None, test_result)

            # Validation runs in a separate context from the worker (spec §5).
            self.store.set_status(task, TaskStatus.VALIDATING)
            verdict, attempt_id = run_validator(
                self.store, self.runner, self.registry, task, task.output,
                memory=self.memory, test_result=test_result)
            self.store.add_verdict(task.id, attempt_id, verdict)

            # Executed truth beats the validator's account of it. Record the
            # mismatch: a validator that rubber-stamps failing tests is a
            # measurable reliability problem, not a silent one.
            tests_ok = test_result.passed
            if tests_ok is None:
                tests_ok = verdict.tests_passed
            elif (verdict.tests_passed is not None
                    and verdict.tests_passed != test_result.passed):
                self.store.log_event(task.id, "test_disagreement", {
                    "validator_claimed": verdict.tests_passed,
                    "actual": test_result.passed,
                    "summary": test_result.summary})

            cfg = self.config
            severe = (verdict.kind == VerdictKind.ESCALATE
                      or verdict.confidence < cfg.severe_threshold)
            approved = (verdict.kind == VerdictKind.APPROVE
                        and verdict.confidence >= cfg.approve_threshold
                        and tests_ok is not False)

            if severe:
                self.store.set_status(
                    task, TaskStatus.NEEDS_HUMAN,
                    reason=("Severe disagreement "
                            f"(confidence={verdict.confidence:.2f}): "
                            f"{verdict.reasoning[:500]}"))
                return task

            if approved:
                if task.risk_level >= cfg.human_review_risk_level:
                    self.store.set_status(
                        task, TaskStatus.NEEDS_HUMAN,
                        reason="Validator approved; awaiting human sign-off "
                               "(high-risk task).")
                else:
                    self.store.set_status(task, TaskStatus.DONE)
                return task

            if task.revision_count >= cfg.max_revisions:
                self.store.set_status(
                    task, TaskStatus.NEEDS_HUMAN,
                    reason=f"Exhausted {cfg.max_revisions} revisions without "
                           "approval.")
                return task
            task.revision_count += 1
            self.store.set_status(task, TaskStatus.REVISING)
            feedback = verdict.reasoning

    # -- human decisions (spec §4.6–4.7) --------------------------------------

    def human_approve(self, task_id: int, note: str = "") -> Task:
        task = self._require(task_id)
        self.store.log_event(task_id, "human_approve", {"note": note})
        self.store.set_status(task, TaskStatus.DONE)
        return task

    def human_reject(self, task_id: int, note: str = "") -> Task:
        task = self._require(task_id)
        self.store.log_event(task_id, "human_reject", {"note": note})
        self.store.set_status(task, TaskStatus.FAILED, reason=note)
        return task

    def human_redo(self, task_id: int, note: str = "") -> Task:
        """Full redo (spec §10 decision): same task definition, fresh start,
        NO carried-over context — output, feedback, and revision count reset.
        The audit trail of the failed run is preserved in events/attempts."""
        task = self._require(task_id)
        self.store.log_event(task_id, "human_redo", {"note": note})
        task.output = ""
        task.revision_count = 0
        task.escalation_reason = ""
        # Wipe the workspace too: a redo that reran over the previous attempt's
        # files would not be a fresh start.
        clear_workspace(self.config.workspace_root, task_id)
        self.store.update_task(task)
        self.store.set_status(task, TaskStatus.PENDING, reason="")
        return task

    # -- internals -----------------------------------------------------------

    def _budget_tripped(self, task: Task) -> bool:
        tokens, cost = self.store.task_spend(task.id)
        cfg = self.config
        if tokens > cfg.max_tokens_per_task or cost > cfg.max_cost_usd_per_task:
            self.store.set_status(
                task, TaskStatus.NEEDS_HUMAN,
                reason=f"Budget cap exceeded (tokens={tokens}, "
                       f"cost=${cost:.2f}).")
            return True
        return False

    def _require(self, task_id: int) -> Task:
        task = self.store.get_task(task_id)
        if task is None:
            raise KeyError(f"No task {task_id}")
        return task
