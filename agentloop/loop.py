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
- transient infra failure (runner /     -> retried with backoff; if it persists
  executor raises)                         -> needs_human with an infra_error
                                           reason. Distinct from a revise
                                           (infra failure is not a task-quality
                                           failure) and NOT counted against
                                           max_revisions. One flaky call does
                                           not abort the rest of the batch.

"Tests not failing" means the *executed* result (spec §5). Tests run in the
task's workspace between the worker and the validator; the validator sees the
real output, and the gate consults the real status rather than the validator's
self-reported TESTS: field. A validator claiming pass against an executed fail
is recorded as a `test_disagreement` event — the loop measures its validators.
"""

from __future__ import annotations

import time

from .agents import run_validator, run_worker
from .config import LoopConfig
from .executor import TestExecutor, clear_workspace, workspace_for
from .memory import MemoryService
from .models import Task, TaskStatus, TestResult, VerdictKind
from .registry import Registry
from .runner import ModelRunner
from .store import Store


class _InfraError(Exception):
    """A runner/executor call failed after exhausting retries. Distinct from a
    task-quality failure: it escalates to NEEDS_HUMAN with an infra reason and
    does not count against max_revisions."""

    def __init__(self, stage: str, attempts: int, original: BaseException):
        self.stage = stage
        self.attempts = attempts
        self.original = original
        super().__init__(f"{stage}: {type(original).__name__}: {original}")


class Loop:
    def __init__(
        self,
        store: Store,
        runner: ModelRunner,
        registry: Registry,
        config: LoopConfig,
        executor: TestExecutor | None = None,
        memory: MemoryService | None = None,
    ):
        self.store = store
        self.runner = runner
        self.registry = registry
        self.config = config
        self.executor = executor or TestExecutor(
            command=config.test_command,
            timeout_s=config.test_timeout_s,
            enabled=config.allow_test_exec,
            env_allowlist=config.sandbox_env_allowlist,
            isolation=config.sandbox_isolation,
        )
        self.memory = memory or MemoryService(
            store, promote_threshold=config.memory_promote_threshold
        )
        # Stable id under which this loop claims tasks. Sequential today, so one
        # id; it must stay constant so a restart resumes its own in-flight tasks
        # (claim_next_task only resumes tasks a worker already owns).
        self.worker_id = "loop"

    # -- public API ----------------------------------------------------------

    def run(self, max_tasks: int | None = None) -> int:
        """Process pending tasks sequentially. Returns tasks processed.
        Safe to call after a crash/restart: state lives in the store."""
        processed = 0
        while max_tasks is None or processed < max_tasks:
            # Atomic claim (not a bare SELECT): a task is handed to exactly one
            # worker, so two workers never grab the same row. Sequential today.
            task = self.store.claim_next_task(self.worker_id)
            if task is None:
                break
            self.run_task(task)
            processed += 1
        return processed

    def run_task(self, task: Task) -> Task:
        feedback = ""
        test_result = TestResult()
        while True:
            # Human control is read fresh from the store at each iteration
            # boundary, so a pause/abort set from another process (CLI or
            # dashboard) is honored between rounds rather than only on kill.
            if self._control_stop(task):
                return task
            if self._budget_tripped(task):
                return task

            # Agent/executor calls are wrapped so a transient infra failure
            # (API 5xx, network blip) is retried and, if it persists, escalates
            # to NEEDS_HUMAN rather than crashing the whole batch. This is not a
            # "revise": infra failure is not a task-quality failure.
            try:
                # Worker self-checks in its own output (spec §4.2–4.3).
                self.store.set_status(task, TaskStatus.IN_PROGRESS)
                ws = workspace_for(self.config.workspace_root, task.id, create=True)
                result = self._with_retry(
                    task,
                    "worker",
                    lambda: run_worker(
                        self.store,
                        self.runner,
                        self.registry,
                        task,
                        feedback,
                        memory=self.memory,
                        workspace=str(ws),
                        test_result=test_result,
                    ),
                )
                if result.output.strip().upper().startswith("ESCALATE:"):
                    self.store.set_status(
                        task,
                        TaskStatus.NEEDS_HUMAN,
                        reason=f"Worker ambiguity: {result.output.strip()[9:].strip()}",
                    )
                    return task
                task.output = result.output
                self.store.update_task(task)

                # Tests are part of validation, executed for real (spec §5).
                self.store.set_status(task, TaskStatus.TESTING)
                test_result = self._with_retry(
                    task, "executor", lambda: self.executor.run(ws)
                )
                self.store.add_test_run(task.id, None, test_result)

                # Validation runs in a separate context (spec §5).
                self.store.set_status(task, TaskStatus.VALIDATING)
                verdict, attempt_id = self._with_retry(
                    task,
                    "validator",
                    lambda: run_validator(
                        self.store,
                        self.runner,
                        self.registry,
                        task,
                        task.output,
                        memory=self.memory,
                        test_result=test_result,
                    ),
                )
            except _InfraError as exc:
                self.store.set_status(
                    task,
                    TaskStatus.NEEDS_HUMAN,
                    reason=(
                        f"infra_error after {exc.attempts} attempt(s) at "
                        f"'{exc.stage}': {type(exc.original).__name__}: "
                        f"{exc.original}"
                    ),
                )
                return task
            self.store.add_verdict(task.id, attempt_id, verdict)

            # Executed truth beats the validator's account of it. Record the
            # mismatch: a validator that rubber-stamps failing tests is a
            # measurable reliability problem, not a silent one.
            tests_ok = test_result.passed
            if tests_ok is None:
                tests_ok = verdict.tests_passed
            elif (
                verdict.tests_passed is not None
                and verdict.tests_passed != test_result.passed
            ):
                self.store.log_event(
                    task.id,
                    "test_disagreement",
                    {
                        "validator_claimed": verdict.tests_passed,
                        "actual": test_result.passed,
                        "summary": test_result.summary,
                    },
                )

            cfg = self.config
            severe = (
                verdict.kind == VerdictKind.ESCALATE
                or verdict.confidence < cfg.severe_threshold
            )
            approved = (
                verdict.kind == VerdictKind.APPROVE
                and verdict.confidence >= cfg.approve_threshold
                and tests_ok is not False
            )

            if severe:
                self.store.set_status(
                    task,
                    TaskStatus.NEEDS_HUMAN,
                    reason=(
                        "Severe disagreement "
                        f"(confidence={verdict.confidence:.2f}): "
                        f"{verdict.reasoning[:500]}"
                    ),
                )
                return task

            if approved:
                if task.risk_level >= cfg.human_review_risk_level:
                    self.store.set_status(
                        task,
                        TaskStatus.NEEDS_HUMAN,
                        reason="Validator approved; awaiting human sign-off "
                        "(high-risk task).",
                    )
                else:
                    self.store.set_status(task, TaskStatus.DONE)
                return task

            if task.revision_count >= cfg.max_revisions:
                self.store.set_status(
                    task,
                    TaskStatus.NEEDS_HUMAN,
                    reason=f"Exhausted {cfg.max_revisions} revisions without approval.",
                )
                return task
            task.revision_count += 1
            self.store.set_status(task, TaskStatus.REVISING)
            feedback = verdict.reasoning

    # -- mid-run human control (pause / resume / abort) -----------------------

    _TERMINAL = (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.ABORTED)

    def pause(self, task_id: int) -> Task:
        """Signal a running loop to pause at its next iteration boundary. Also
        marks the task PAUSED now so it's visible and the loop won't pick it up
        even when nothing is mid-flight. A paused task survives a restart."""
        task = self._require(task_id)
        if task.status in self._TERMINAL:
            return task
        self.store.set_control(task_id, "pause")
        self.store.set_status(
            task, TaskStatus.PAUSED, reason="Paused by human; resume to continue."
        )
        return self._require(task_id)

    def resume(self, task_id: int) -> Task:
        """Clear the pause and return the task to the pending queue so the loop
        continues it. Preserves revision_count/output — resume is not a redo.
        A no-op on a terminal task (nothing to resume)."""
        task = self._require(task_id)
        if task.status in self._TERMINAL:
            return task
        self.store.set_control(task_id, "run")
        if task.status == TaskStatus.PAUSED:
            self.store.set_status(task, TaskStatus.PENDING, reason="")
        return self._require(task_id)

    def abort(self, task_id: int, note: str = "") -> Task:
        """Terminally stop a task mid-run. Defensible: output and the full audit
        trail are left intact; nothing is wiped. A no-op on an already-terminal
        task — aborting a DONE/FAILED task must not discard its final status."""
        task = self._require(task_id)
        if task.status in self._TERMINAL:
            return task
        self.store.set_control(task_id, "abort")
        self.store.log_event(task_id, "human_abort", {"note": note})
        self.store.set_status(
            task, TaskStatus.ABORTED, reason=note or "Aborted by human mid-run."
        )
        return self._require(task_id)

    def _control_stop(self, task: Task) -> bool:
        """Honor a pause/abort signal set since the last boundary. Returns True
        if the loop should stop working this task."""
        control = self.store.get_control(task.id)
        if control == "abort":
            # Preserve a reason the human's abort() call already stored (e.g. a
            # --note); only fall back to the generic message when there is none.
            current = self.store.get_task(task.id)
            reason = (
                current.escalation_reason
                if current and current.escalation_reason
                else "Aborted by human mid-run."
            )
            self.store.set_status(task, TaskStatus.ABORTED, reason=reason)
            return True
        if control == "pause":
            self.store.set_status(
                task, TaskStatus.PAUSED, reason="Paused by human; resume to continue."
            )
            return True
        return False

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
        # A fresh start clears any lingering pause/abort signal, otherwise the
        # redo would stop again at its first iteration boundary.
        self.store.set_control(task_id, "run")
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

    def _with_retry(self, task: Task, stage: str, fn):
        """Call `fn`, retrying transient failures with exponential backoff.
        Each failure is logged as an `infra_error` event; once retries are
        exhausted the failure is raised as `_InfraError` for the caller to
        escalate. Bounded retry lives here (deduped from the roadmap's slice 6),
        kept separate from the revision loop."""
        attempts = 0
        while True:
            try:
                return fn()
            except Exception as exc:  # transient infra failure
                attempts += 1
                self.store.log_event(
                    task.id,
                    "infra_error",
                    {
                        "stage": stage,
                        "attempt": attempts,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
                if attempts > self.config.infra_max_retries:
                    raise _InfraError(stage, attempts, exc) from exc
                delay = self.config.infra_retry_backoff_s * (2 ** (attempts - 1))
                if delay > 0:
                    time.sleep(delay)

    def _budget_tripped(self, task: Task) -> bool:
        tokens, cost = self.store.task_spend(task.id)
        cfg = self.config
        if tokens > cfg.max_tokens_per_task or cost > cfg.max_cost_usd_per_task:
            self.store.set_status(
                task,
                TaskStatus.NEEDS_HUMAN,
                reason=f"Budget cap exceeded (tokens={tokens}, cost=${cost:.2f}).",
            )
            return True
        return False

    def _require(self, task_id: int) -> Task:
        task = self.store.get_task(task_id)
        if task is None:
            raise KeyError(f"No task {task_id}")
        return task
