"""The orchestration loop (spec §4).

A `planner` agent decomposes a goal into a graph of tasks (`Loop.plan`), and the
loop drains that graph — sequentially by default, or `max_parallel_workers` at a
time. It holds no schedule in memory: "what may run now" is a predicate the
store evaluates inside the atomic claim, so dependency order and plan gating
hold identically for one worker and for many.

Decision rules per validation round:
- worker replies `ESCALATE:`            -> needs_human (genuine ambiguity)
- verdict escalate OR conf < severe     -> needs_human (severe disagreement)
- conf >= approve_threshold AND tests not failing
                                        -> done (or needs_human sign-off if
                                           risk_level >= human_review level)
- otherwise                             -> revise, bounded by max_revisions;
                                           exhausted retries -> needs_human
- budget cap exceeded at any point      -> needs_human (never burn unbounded)
- worker context >= context_handoff_     -> summarize the working state and
  ratio of its AgentSpec budget              restart the worker from that summary
                                             in place of the raw transcript
                                             (a `context_handoff` event). Checked
                                             at the boundary like the budget cap;
                                             NOT a revision and not counted
                                             against max_revisions.
- transient infra failure (runner /     -> retried with backoff; if it persists
  executor raises)                         -> needs_human with an infra_error
                                           reason. Distinct from a revise
                                           (infra failure is not a task-quality
                                           failure) and NOT counted against
                                           max_revisions. One flaky call does
                                           not abort the rest of the batch.

Planning rules (`Loop.plan`), all failing the same safe way — the plan row
escalates to NEEDS_HUMAN and no child tasks are created:
- planner replies `ESCALATE:`            -> needs_human (genuine ambiguity)
- unparseable / cyclic / dangling-ref /  -> needs_human ("Unusable plan"). The
  oversized plan                            whole plan is discarded, never
                                            partially applied or truncated.
- plan_requires_approval (default True)  -> the plan's tasks are not claimable
                                            until `approve_plan`; they wait as
                                            ordinary `pending` rows.
- a task is claimable only once every    -> a dependent of an escalated task is
  dependency is DONE                        skipped, not failed; resolving the
                                            dependency makes it claimable again.

"Tests not failing" means the *executed* result (spec §5). Tests run in the
task's workspace between the worker and the validator; the validator sees the
real output, and the gate consults the real status rather than the validator's
self-reported TESTS: field. A validator claiming pass against an executed fail
is recorded as a `test_disagreement` event — the loop measures its validators.
"""

from __future__ import annotations

import threading
import time
import warnings

from .agents import (
    PlanError,
    parse_plan,
    run_planner,
    run_summarizer,
    run_validator,
    run_worker,
)
from .config import OPENAI_MODELS, LoopConfig
from .executor import TestExecutor, clear_workspace, workspace_for
from .memory import MemoryService
from .models import Task, TaskStatus, TestResult, VerdictKind
from .registry import Registry
from .retrieval import get_backend
from .runner import (
    ModelRunner,
    OpenAICompatRunner,
    RunnerConfigError,
    get_runner,
)
from .store import Store


# How long an idle parallel worker parks before re-checking for claimable work.
# In-process changes notify it directly; this only bounds how late it notices a
# change made by another process.
_IDLE_POLL_SECONDS = 0.5


class _InfraError(Exception):
    """A runner/executor call failed after exhausting retries. Distinct from a
    task-quality failure: it escalates to NEEDS_HUMAN with an infra reason and
    does not count against max_revisions."""

    def __init__(self, stage: str, attempts: int, original: BaseException):
        self.stage = stage
        self.attempts = attempts
        self.original = original
        super().__init__(f"{stage}: {type(original).__name__}: {original}")


class _ConfigError(Exception):
    """The registry asks for something that does not exist — an unknown runner
    name on an `AgentSpec`, say. Deliberately not an `_InfraError`: retrying a
    configuration error with backoff only burns the clock to reach the same
    conclusion, and reporting it as `infra_error` points the human at the
    network instead of at their agents.json. Same reasoning `Loop.plan` applies
    to a missing `planner` role.

    A backend can raise the same class of problem from inside a call rather than
    at resolution time — a missing API key, a 401 on a revoked one, a 404 on a
    model that endpoint does not serve. Those arrive as `RunnerConfigError` from
    the seam and `_with_retry` converts them here, because `run()` is invoked
    inside the retry loop and would otherwise be retried like a network blip."""


# A model id belonging to the Anthropic family. Pinned to an OpenAI-compatible
# endpoint it is a guaranteed 404, so it is refused before the call rather than
# after three paid round trips.
_CLAUDE_MODEL_PREFIXES = ("claude-", "anthropic/")


def _check_model_for_backend(role: str, model: str, backend: ModelRunner) -> None:
    """Refuse a pinned model that cannot belong to its pinned provider.

    `AgentSpec` co-locates `runner` and `model` because "a `claude-sonnet-5`
    string means nothing to an OpenAI endpoint" — but nothing enforced the pair,
    so the single most likely first edit (set `runner: openai` on the validator,
    leave `model` alone) sent `claude-sonnet-5` to OpenAI, took a 404, retried
    it and escalated reading `infra_error`. The failure mode the comment claims
    to have designed away was the default first-use outcome.

    Deliberately asymmetric in strictness. A `claude-*` id on an OpenAI endpoint
    is *certainly* wrong, so it is a hard config error. An unrecognised id is
    not: pointing this backend at a gateway, a fine-tune or a self-hosted model
    is exactly what "second provider = second base_url" is for, and refusing it
    would break a legitimate deployment on nothing but an incomplete list. That
    one warns — it also prices at DEFAULT_PRICING, which is worth saying once.
    """
    if not isinstance(backend, OpenAICompatRunner):
        return
    if model.startswith(_CLAUDE_MODEL_PREFIXES):
        raise _ConfigError(
            f"Agent role {role!r} is pinned to the OpenAI-compatible backend "
            f"but its model is {model!r}, which is an Anthropic model id. That "
            f"request can only 404. Set a model the endpoint serves (e.g. one "
            f"of {', '.join(OPENAI_MODELS[:3])}, ...) in agents.json."
        )
    if model not in OPENAI_MODELS:
        warnings.warn(
            f"Agent role {role!r} is pinned to the OpenAI-compatible backend "
            f"with model {model!r}, which is not in config.OPENAI_MODELS. That "
            f"is fine for a gateway, fine-tune or self-hosted id, but it has no "
            f"pricing row, so its attempts cost DEFAULT_PRICING.",
            RuntimeWarning,
            stacklevel=2,
        )


class Loop:
    def __init__(
        self,
        store: Store,
        runner: ModelRunner,
        registry: Registry,
        config: LoopConfig,
        executor: TestExecutor | None = None,
        memory: MemoryService | None = None,
        runners: dict[str, ModelRunner] | None = None,
    ):
        self.store = store
        self.runner = runner
        # Backends a role may be pinned to by name (`AgentSpec.runner`), beyond
        # the default `runner` above. Doubles as the cache for names resolved
        # through `get_runner`: resolving per call would build a fresh backend
        # for every invocation, and a MockRunner's script is per instance, so a
        # test pinning a role would silently get an unscripted runner each round.
        #
        # What the lock guarantees is exactly one *construction* per name, so a
        # backend holding state (a script, a connection, a rate limiter) is the
        # same object for every thread. It does not make a backend thread-safe —
        # that is the backend's own contract, and `MockRunner` explicitly does
        # not offer it.
        self._runners: dict[str, ModelRunner] = dict(runners or {})
        self._runners_lock = threading.Lock()
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
            store,
            promote_threshold=config.memory_promote_threshold,
            backend=get_backend(config.memory_retrieval_backend, config),
        )
        # Stable id under which this loop claims tasks. Sequential today, so one
        # id; it must stay constant so a restart resumes its own in-flight tasks
        # (claim_next_task only resumes tasks a worker already owns).
        self.worker_id = "loop"

    # -- public API ----------------------------------------------------------

    def run(self, max_tasks: int | None = None) -> int:
        """Process claimable tasks. Returns tasks processed.
        Safe to call after a crash/restart: state lives in the store.

        Sequential by default (`max_parallel_workers=1`). Above 1, that many
        threads each claim independently; the store's atomic claim is what makes
        that safe, and its blocked-task predicate is what keeps dependency order
        without the loop tracking a graph in memory."""
        n = max(1, int(self.config.max_parallel_workers))
        # Checked for both paths: shrinking the pool *to* one worker is the most
        # likely way to strand a claim, so the sequential path needs this most.
        self._warn_stranded_claims(self._worker_ids(n))
        if n == 1:
            return self._run_serial(self.worker_id, max_tasks)
        return self._run_parallel(n, max_tasks)

    def _run_serial(self, worker_id: str, max_tasks: int | None) -> int:
        processed = 0
        while max_tasks is None or processed < max_tasks:
            # Atomic claim (not a bare SELECT): a task is handed to exactly one
            # worker, so two workers never grab the same row.
            task = self.store.claim_next_task(worker_id)
            if task is None:
                break
            self.run_task(task)
            processed += 1
        return processed

    def _worker_ids(self, n: int) -> list[str]:
        """Stable claim ids, so a restart resumes its own in-flight tasks.

        Worker 0 keeps the bare `worker_id` the sequential loop uses, so a run
        started sequentially and resumed with parallelism on (or the reverse for
        that worker) still finds its own in-flight task rather than stranding it.
        """
        return [self.worker_id] + [f"{self.worker_id}-{i}" for i in range(1, n)]

    def _run_parallel(self, n: int, max_tasks: int | None) -> int:
        """Run up to `n` tasks concurrently.

        No graph is held in memory and no scheduler decides what is ready: each
        thread just re-claims, and the store refuses to hand out a task whose
        dependencies are unfinished.

        A thread that finds nothing claimable **waits rather than exiting**,
        while any peer is still working. Exiting would be safe (the last thread
        standing eventually claims whatever it unblocked) but not parallel: a
        plan usually has a single root, so all but one worker would find nothing
        to do on the first pass, exit, and leave the entire rest of the graph —
        including tasks that are independent of each other — to run serially.
        The wait is over when no peer is busy, because then nothing can become
        claimable and whatever remains is blocked on work this run won't finish.
        """
        cond = threading.Condition()
        state = {"claimed": 0, "busy": 0}
        errors: list[BaseException] = []
        stopping = threading.Event()

        def drain(worker_id: str) -> None:
            while True:
                with cond:
                    if stopping.is_set():
                        cond.notify_all()
                        return
                    if max_tasks is not None and state["claimed"] >= max_tasks:
                        cond.notify_all()
                        return
                    # Counted at claim time, not completion: two threads that
                    # both finish under the cap must not both claim past it.
                    task = self.store.claim_next_task(worker_id)
                    if task is None:
                        if state["busy"] == 0:
                            cond.notify_all()
                            return
                        # A peer is mid-task and may unblock a dependent of it.
                        # Every in-process state change notifies under this same
                        # lock, so no wakeup is lost and the timeout is only a
                        # backstop — it exists so a change made by *another*
                        # process (an `approve-plan` mid-run) is noticed too.
                        # Kept coarse: at 0.05s seven idle workers would fire
                        # ~140 claim queries a second at the store lock the one
                        # busy worker is trying to use.
                        cond.wait(timeout=_IDLE_POLL_SECONDS)
                        continue
                    state["claimed"] += 1
                    state["busy"] += 1
                try:
                    self.run_task(task)
                except BaseException as exc:  # noqa: BLE001 - re-raised below
                    # A thread that dies silently would leave the task claimed
                    # and the run reporting success. Surface it like the
                    # sequential path does, once the other workers wind down.
                    errors.append(exc)
                    with cond:
                        state["busy"] -= 1
                        cond.notify_all()
                    return
                with cond:
                    # Finishing may have unblocked this task's dependents; wake
                    # the workers parked above so they can claim them.
                    state["busy"] -= 1
                    cond.notify_all()

        worker_ids = self._worker_ids(n)
        threads = [
            threading.Thread(target=drain, args=(wid,), name=f"agentloop-{wid}")
            for wid in worker_ids
        ]
        for t in threads:
            t.start()
        try:
            for t in threads:
                t.join()
        except KeyboardInterrupt:
            # Sequential mode exits promptly on Ctrl-C; without this the
            # parallel mode would not, because joining non-daemon threads waits
            # for every in-flight model call *and* keeps claiming new tasks
            # after the interrupt. Stop claiming, let the in-flight round
            # finish, then re-raise.
            stopping.set()
            with cond:
                cond.notify_all()
            for t in threads:
                t.join()
            raise
        if errors:
            raise errors[0]
        return state["claimed"]

    def _warn_stranded_claims(self, worker_ids: list[str]) -> None:
        """Surface in-flight tasks held by a claim id no live worker will use.

        Lowering `max_parallel_workers` between runs orphans whatever the
        retired ids were holding: `claim_next_task` only re-offers in-flight
        work to its exact owner, so those tasks become invisible to every
        claimer while `run()` returns a success count that silently excludes
        them. They are *not* reclaimed here — with the default worker id, a
        second `agentloop run` process would look identical to a retired
        worker, and stealing its live task is worse than leaving one stranded.
        Reporting it turns silent loss into something an operator can act on
        (`agentloop redo <id>`)."""
        stranded = self.store.stranded_claims(self.worker_id, worker_ids)
        for task in stranded:
            self.store.log_event(
                task.id,
                "claim_stranded",
                {
                    "claimed_by": task.claimed_by,
                    "status": task.status.value,
                    "active_workers": worker_ids,
                },
            )

    # -- planning (roadmap slice 3) -------------------------------------------

    def plan(
        self,
        goal: str,
        acceptance_criteria: str,
        title: str = "",
        risk_level: int = 1,
    ) -> Task:
        """Decompose a goal into a task graph. Returns the plan row.

        The plan itself is a task row of `kind='plan'`: it owns the planner's
        attempt and audit trail, and carries the approval that gates its
        children — but it is never claimed by the loop, because a goal statement
        is not work.

        Every failure mode ends the same way: the plan row escalates to
        NEEDS_HUMAN and **no child tasks exist**. A partially applied plan is
        worse than none — the missing half is invisible, while the half that
        landed looks like a complete plan somebody approved.
        """
        # Guard before the row exists: an empty goal would make the derived
        # title raise IndexError, and a failure *before* the plan row is
        # created is the one failure that cannot escalate a plan row.
        if not goal.strip():
            raise ValueError("A plan needs a goal to decompose (goal was empty).")
        if not acceptance_criteria.strip():
            raise ValueError(
                "A plan needs acceptance criteria; they are what each child "
                "task's criteria are derived from."
            )
        plan_task = Task(
            id=None,
            title=title or goal.strip().splitlines()[0][:120],
            goal=goal,
            acceptance_criteria=acceptance_criteria,
            risk_level=risk_level,
            kind="plan",
        )
        self.store.add_task(plan_task)

        # Checked before the retry loop: a hand-edited agents.json predating the
        # planner role is a configuration error, not a transient one, so
        # retrying it with backoff would only burn the clock to reach the same
        # conclusion — and reporting it as `infra_error` would point the human
        # at the network instead of at their registry. Unlike the summarizer,
        # there is no sane fallback: planning as a worker would decompose the
        # goal with a prompt that never asked for a graph.
        try:
            self.registry.get("planner")
        except KeyError:
            self.store.set_status(
                plan_task,
                TaskStatus.NEEDS_HUMAN,
                reason=(
                    "No 'planner' agent is registered; add one to agents.json "
                    "(or delete it to fall back to the built-in defaults)."
                ),
            )
            return plan_task

        # Resolved before the retry loop for the same reason the role itself is:
        # a runner name that does not exist is a config error, not a transient
        # one, and it escalates the plan row with no child tasks created.
        try:
            planner_runner = self._runner_for("planner")
        except _ConfigError as exc:
            self.store.set_status(plan_task, TaskStatus.NEEDS_HUMAN, reason=str(exc))
            return plan_task

        try:
            result = self._with_retry(
                plan_task,
                "planner",
                lambda: run_planner(
                    self.store, planner_runner, self.registry, plan_task, self.memory
                ),
            )
        except _ConfigError as exc:
            # A config error the backend only discovers when called (a missing
            # key, a model it does not serve). Same outcome as an unknown runner
            # name resolved above: the plan row escalates with zero child tasks,
            # no retry and no `infra_error`.
            self.store.set_status(plan_task, TaskStatus.NEEDS_HUMAN, reason=str(exc))
            return plan_task
        except _InfraError as exc:
            self.store.set_status(
                plan_task,
                TaskStatus.NEEDS_HUMAN,
                reason=(
                    f"infra_error after {exc.attempts} attempt(s) at "
                    f"'{exc.stage}': {type(exc.original).__name__}: {exc.original}"
                ),
            )
            return plan_task

        # Same rule the worker has: an agent that hits genuine ambiguity asks
        # instead of guessing, and guessing here would fabricate a whole graph.
        if result.output.strip().upper().startswith("ESCALATE:"):
            self.store.set_status(
                plan_task,
                TaskStatus.NEEDS_HUMAN,
                reason=f"Planner ambiguity: {result.output.strip()[9:].strip()}",
            )
            return plan_task

        try:
            planned = parse_plan(result.output, max_tasks=self.config.max_plan_tasks)
        except PlanError as exc:
            self.store.set_status(
                plan_task, TaskStatus.NEEDS_HUMAN, reason=f"Unusable plan: {exc}"
            )
            return plan_task

        # One transaction: the tasks, their edges and the audit event land
        # together, so a crash mid-write can't leave a graph missing the edges
        # that are the only thing keeping its tasks in order.
        #
        # The except is not decoration. `parse_plan` already rejects cycles, so
        # `add_dependency`'s own refusal should be unreachable — but "should be
        # unreachable" is not the same as "cannot happen", and without this an
        # unexpected failure here would roll the graph back correctly and then
        # propagate, leaving a plan row parked at `pending` with no reason on
        # it. That is the one shape this method promises never to produce.
        try:
            with self.store.transaction():
                ids: dict[str, int] = {}
                for node in planned:
                    child = Task(
                        id=None,
                        title=node.title,
                        goal=node.goal,
                        acceptance_criteria=node.acceptance_criteria,
                        risk_level=node.risk_level,
                        kind="task",
                        plan_id=plan_task.id,
                    )
                    ids[node.ref] = self.store.add_task(child)
                edges = 0
                for node in planned:
                    for dep in node.depends_on:
                        self.store.add_dependency(ids[node.ref], ids[dep])
                        edges += 1
                self.store.log_event(
                    plan_task.id,
                    "plan_created",
                    {
                        "n_tasks": len(planned),
                        "n_edges": edges,
                        "tasks": [
                            {
                                "id": ids[n.ref],
                                "ref": n.ref,
                                "title": n.title,
                                "depends_on": [ids[d] for d in n.depends_on],
                            }
                            for n in planned
                        ],
                    },
                )
        except Exception as exc:
            self.store.set_status(
                plan_task,
                TaskStatus.NEEDS_HUMAN,
                reason=f"Plan could not be persisted: {type(exc).__name__}: {exc}",
            )
            return plan_task

        if self.config.plan_requires_approval:
            self.store.set_status(
                plan_task,
                TaskStatus.NEEDS_HUMAN,
                reason=(
                    f"Plan ready: {len(planned)} task(s) awaiting human sign-off "
                    f"(agentloop approve-plan {plan_task.id})."
                ),
            )
        else:
            self.store.set_plan_approved(plan_task.id, True)
            self.store.set_status(plan_task, TaskStatus.DONE)
        return plan_task

    def approve_plan(self, plan_id: int, note: str = "") -> Task:
        """Sign a plan off: its tasks become claimable.

        The plan row goes DONE because *planning* is what finished — the work it
        described is tracked by the child tasks, each with its own validator
        round. Idempotent, so approving twice is harmless.
        """
        task = self._require(plan_id)
        if task.kind != "plan":
            raise ValueError(
                f"Task {plan_id} is not a plan (kind={task.kind!r}); "
                f"use approve/reject for ordinary tasks."
            )
        # A plan that produced no tasks is a *failed* plan (unparseable, cyclic,
        # planner escalation). Approving it would turn an escalation into a
        # green DONE goal with nothing under it, and would blank the diagnosis
        # off the row. The decision rules fail safe toward NEEDS_HUMAN; this is
        # the one place a human click could push the other way.
        if not self.store.plan_tasks(plan_id):
            raise ValueError(
                f"Plan {plan_id} produced no tasks and cannot be approved "
                f"({task.escalation_reason or 'planning did not complete'}). "
                f"Re-plan the goal instead."
            )
        # One transaction: the audit event, the approval flag and the status
        # are one decision. Committed separately, a crash between them leaves
        # the log claiming an approval that never released the children, or
        # children released under a plan row still reading needs_human.
        with self.store.transaction():
            self.store.log_event(plan_id, "human_approve_plan", {"note": note})
            self.store.set_plan_approved(plan_id, True)
            # Clear the "awaiting sign-off" reason: it is answered, and leaving
            # it on a released plan reads as though it were still blocked.
            task.escalation_reason = ""
            self.store.set_status(task, TaskStatus.DONE)
        return self._require(plan_id)

    def run_task(self, task: Task) -> Task:
        feedback = ""
        test_result = TestResult()
        # Worker context consumed as of the last handoff. Measured, not reset in
        # the store, so it stays an in-loop local: a post-crash restart simply
        # re-measures from 0 and does one safe handoff at the first boundary if
        # the accumulated context already exceeds the threshold.
        handoff_watermark = 0
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
                # Context-budget handoff (slice 1): when the worker's accumulated
                # context on this task passes context_handoff_ratio of its
                # AgentSpec budget, compact the working state and restart the
                # worker from that summary instead of the raw transcript. Checked
                # at the boundary alongside the budget cap; not a revision.
                handoff_summary = self._maybe_handoff(
                    task, feedback, test_result, handoff_watermark
                )
                if handoff_summary is not None:
                    handoff_watermark = self.store.attempt_tokens(task.id, "worker")

                # Worker self-checks in its own output (spec §4.2–4.3).
                self.store.set_status(task, TaskStatus.IN_PROGRESS)
                ws = workspace_for(self.config.workspace_root, task.id, create=True)
                worker_runner = self._runner_for(task.worker_role)
                result = self._with_retry(
                    task,
                    "worker",
                    lambda: run_worker(
                        self.store,
                        worker_runner,
                        self.registry,
                        task,
                        feedback,
                        memory=self.memory,
                        workspace=str(ws),
                        test_result=test_result,
                        handoff_summary=handoff_summary,
                    ),
                )
                if result.output.strip().upper().startswith("ESCALATE:"):
                    self.store.set_status(
                        task,
                        TaskStatus.NEEDS_HUMAN,
                        reason=f"Worker ambiguity: {result.output.strip()[9:].strip()}",
                    )
                    return task
                if not result.output.strip():
                    # An empty worker output is not work; it is the absence of
                    # work, and every downstream step treats it as the former.
                    # The validator would review a blank diff against criteria it
                    # cannot check, and an approve there marks the task DONE —
                    # which, under the slice-3 graph, is exactly what releases
                    # dependents to run against upstream output that does not
                    # exist. `human_approve` refuses a `pending` task for this
                    # same reason; this is the same refusal one step earlier.
                    #
                    # Escalating (rather than revising) because emptiness is not
                    # a quality gap a worker can be told to fix: it means the
                    # provider returned nothing, and re-prompting the same way
                    # burns the revision budget on a call that already failed
                    # silently. A runner that knows *why* it is empty raises
                    # instead (see OpenAICompatRunner's error-envelope and
                    # finish_reason checks); this catches the backends that
                    # cannot tell — ClaudeSDKRunner joins its chunks, so a stream
                    # carrying no text is "" with nothing to report.
                    self.store.set_status(
                        task,
                        TaskStatus.NEEDS_HUMAN,
                        reason=(
                            "Worker returned an empty output — nothing to "
                            "validate. The model call succeeded but produced no "
                            "text; check the runner's audit events for this "
                            "attempt."
                        ),
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
                # The cross-validator (slice 4): when the validator's spec pins a
                # different backend, the output is reviewed by a model from
                # another family than produced it. Nothing below this line
                # changes — the verdict path is identical either way.
                validator_runner = self._runner_for(task.validator_role)
                verdict, attempt_id = self._with_retry(
                    task,
                    "validator",
                    lambda: run_validator(
                        self.store,
                        validator_runner,
                        self.registry,
                        task,
                        task.output,
                        memory=self.memory,
                        test_result=test_result,
                    ),
                )
            except _ConfigError as exc:
                # Escalates without a retry and without an infra_error event: a
                # typo'd runner name is not going to resolve on the third try.
                self.store.set_status(task, TaskStatus.NEEDS_HUMAN, reason=str(exc))
                return task
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
        # Approving a plan means "run what it proposed", not "this goal is
        # finished". Without this, the dashboard's approve button and
        # `agentloop approve <plan-id>` would mark the plan DONE while leaving
        # its children blocked on an approval that never happened.
        if task.kind == "plan":
            return self.approve_plan(task_id, note)
        # Approval is a human signing off on work that was *done and reviewed*.
        # A PENDING task has produced nothing: no worker attempt, no validator
        # verdict, no output. Marking it DONE would not just mis-record it —
        # with a task graph, DONE is what satisfies a dependency, so approving
        # an unrun task releases its dependents to run against upstream output
        # that does not exist. Fail safe: refuse rather than complete.
        if task.status == TaskStatus.PENDING:
            raise ValueError(
                f"Task {task_id} has not run yet (status=pending); there is "
                f"nothing to approve. Use `run` to execute it, or `reject` to "
                f"drop it."
            )
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

    def _runner_for(self, role: str) -> ModelRunner:
        """Which backend serves this role (slice 4).

        Provider selection is policy, so it lives here rather than in
        `agents.py`: the `run_*` functions already take a runner and stay a pure
        "invoke this runner" layer, which is what lets them be reused by `eval`
        and by anything else that wants to drive one agent with a backend of its
        own choosing. Pushing the lookup into them would give every caller the
        loop's registry policy whether it wanted it or not, and would put a
        second thing in the module whose job is building prompts.

        An unpinned role returns `self.runner` — the same object, not a copy or
        an equivalent — so with nothing pinned anywhere the loop makes exactly
        the calls it made before this slice existed.

        A role missing from the registry is *not* an error here: `run_summarizer`
        deliberately falls back to the worker's spec for a hand-edited
        agents.json predating that role, and this lookup must not turn that
        graceful degrade into a crash.
        """
        try:
            spec = self.registry.get(role)
        except KeyError:
            return self.runner
        name = spec.runner
        if not name:
            return self.runner
        # Check-then-act under a lock, not around it: `max_parallel_workers > 1`
        # runs this from several threads at once, and the dict is sold as a
        # cache *guaranteeing* one instance per name. What it actually
        # guarantees without the lock is one instance per name eventually — fine
        # for the two stateless backends that ship, wrong for anything holding a
        # connection, and wrong today for a MockRunner injected through
        # `runners={...}`, whose script is per instance.
        with self._runners_lock:
            if name not in self._runners:
                try:
                    self._runners[name] = get_runner(name)
                except (ValueError, RunnerConfigError) as exc:
                    raise _ConfigError(
                        f"Agent role {role!r} is pinned to runner {name!r}, "
                        f"which cannot be used ({exc}). Fix `runner` for that "
                        f"role in agents.json."
                    ) from exc
            backend = self._runners[name]
        _check_model_for_backend(role, spec.model, backend)
        return backend

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
            except RunnerConfigError as exc:
                # A permanent, operator-fixable provider problem raised from
                # inside the call rather than at resolution time: a missing key,
                # a revoked one, a model the endpoint does not serve. Retrying
                # it burns the clock to reach the same conclusion and an
                # `infra_error` event points the human at the network. Escalates
                # like any other config error, with no event of its own — the
                # reason lands on the task row.
                raise _ConfigError(f"{stage}: {exc}") from exc
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

    def _maybe_handoff(
        self, task: Task, feedback: str, test_result: TestResult, watermark: int
    ) -> str | None:
        """Context-budget handoff (slice 1). If the worker's accumulated context
        since the last handoff has reached `context_handoff_ratio` of its
        AgentSpec budget, summarize the working state and return the summary for
        the next worker call to use in place of the raw transcript; else None.

        The summarization is a bounded-retried ModelRunner call (so a transient
        failure escalates like any other infra error, and it works under
        MockRunner). It is recorded as its own attempt, not counted against
        max_revisions — a handoff is not a task-quality revision."""
        spec = self.registry.get(task.worker_role)
        used = self.store.attempt_tokens(task.id, "worker") - watermark
        threshold = self.config.context_handoff_ratio * spec.context_budget_tokens
        if used < threshold:
            return None
        summarizer_runner = self._runner_for("summarizer")
        summary = self._with_retry(
            task,
            "summarizer",
            lambda: run_summarizer(
                self.store,
                summarizer_runner,
                self.registry,
                task,
                feedback,
                test_result,
            ),
        )
        self.store.log_event(
            task.id,
            "context_handoff",
            {
                "role": task.worker_role,
                "before_tokens": used,
                "after_tokens": summary.tokens_out,
                "budget_tokens": spec.context_budget_tokens,
                "ratio": self.config.context_handoff_ratio,
            },
        )
        return summary.output

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
