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
- a *worker or validator* asked for a  -> needs_human ("awaiting tool approval"),
  tool it does not have and called        checked after the output is stored and
  the ask `blocking`                      before TESTING. Partial output kept,
                                          `revision_count` untouched, no
                                          validator attempt and no test run: a
                                          missing capability is not a quality gap
                                          a worker can be told to fix, so it is
                                          NOT a revision. The park stamps
                                          `parked=1` on exactly the rows it named
                                          and logs no event of its own (the
                                          `status:needs_human` one is the record).
                                          `approve_tool_request` lifts it only
                                          when all three hold: the row's `parked`
                                          is set, the task is at NEEDS_HUMAN *with
                                          the park's own reason still on it*, and
                                          no other pending+blocking+parked row
                                          remains — so a tool decision can never
                                          revert an escalation the tool queue did
                                          not cause, including the four that fire
                                          upstream of this check and leave the
                                          same status behind.
                                          `parked` is cleared by *every* exit from
                                          the parked state (the firing release,
                                          `human_redo`, `resume`'s PAUSED branch,
                                          and the three terminal exits), because a
                                          stale flag would make the four gates
                                          above this one revertible. `reject`
                                          records the denial and leaves the task
                                          parked.
                                          Scoped to worker/validator rounds on
                                          purpose, because that is where the check
                                          lives. `Loop.plan` passes the same config,
                                          so a **planner**'s blocking marker does
                                          create a pending+blocking row — but
                                          `run_task` is never called on a
                                          `kind='plan'` row, so nothing parks: the
                                          plan proceeds, creates its children, and
                                          the row sits in the queue for a human,
                                          `blocking` and unhonored. Safe direction
                                          (a plan is task definition a human signs
                                          off anyway, and the planner is read-only
                                          by construction), and stated here rather
                                          than left to be discovered from a rule
                                          that reads as though it applied to every
                                          agent.
- a tool is withheld (`pending`) or    -> its **concrete** capability is subtracted
  denied (`rejected`) for this role        from the `tools` list handed to the
  on this task                             runner, which also disables every other
                                           logical tool sharing it: rejecting
                                           `shell` removes `Bash`, so `git` stops
                                           working even though the role declares it
                                           and the baseline never gated it. Fail
                                           closed on purpose — `LOGICAL_TOOL_MAP`
                                           is not injective, so subtracting the
                                           *name* left the capability in place and
                                           the ledger recorded a withheld tool as
                                           though it had held. Applied once over
                                           the finished list (grants are appended
                                           before it), never per request. The
                                           collateral loss is audited as a
                                           `tool_capability_withheld` event carrying
                                           the sentence a human reads, and each
                                           request's own event names what deciding
                                           it also decides (`also_decides`), so the
                                           consequence is legible before the click.
                                           Not gated on `gate_declared_tools`: that
                                           knob decides whether a declared tool
                                           needs a grant, not whether a decision
                                           already made is honored. A machine
                                           `refused` row does not subtract — nobody
                                           was shown a closed gate, and an over-cap
                                           refusal would otherwise let an agent's
                                           own chattiness strip its role's baseline.
- this worker's `claimed_by` lease is  -> stand down: end the round, write no
  no longer its own at the boundary        status, take nothing back. The task
                                           belongs to whoever holds it now, so a
                                           `claim_lost` event and a
                                           RuntimeWarning are all this worker may
                                           leave. NOT a revision and not counted
                                           against max_revisions: nothing about
                                           the work failed. The already-paid
                                           attempt of the interrupted round stays
                                           committed — it was bought.
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

import os
import shutil
import threading
import time
import warnings
from pathlib import Path

# The **module**, never its names: P5/P6 patch this module attribute, and a
# direct name binding would make that patch inert (`agents.py` is the scar).
from . import vcs
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

# How a park announces itself on the row, and the *only* thing that distinguishes
# it from any other escalation holding the same status. Written by exactly one
# place (`run_task`'s park) and read by exactly one place
# (`approve_tool_request`'s condition 2), which is what makes it an internal
# contract rather than string-matching a message.
#
# It exists because `parked=1` alone answers "was the loop ever holding this task
# on this row", not "is that what is holding it now": a flag left standing by a
# route that did not clear it sat on a task escalated for an empty output, a
# worker `ESCALATE:`, a budget cap or an infra failure — all four fire *upstream*
# of the park check — and every one of those leaves the task at NEEDS_HUMAN, so
# the status term accepted them and approving the stale row blanked the very
# diagnosis the human was asked to act on.
#
# Rejected alternative: clearing `parked` at each of those four escalations
# instead. That is the paired-write shape this slice has already been bitten by
# twice — one half conditional, the other half a line someone must remember to
# add — and a fifth escalation added later would silently reopen the hole. A
# positive test for "the park is what is holding this task" cannot be forgotten
# by a new escalation, because a new escalation writes its own reason.
_PARK_REASON_PREFIX = "Awaiting tool approval: "


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


def _clear_worktree(ws) -> bool:
    """Remove everything in a workspace **except** its `.git`, and report
    whether the working tree is empty afterwards.

    Redo's fresh start and the recovery ref are not in conflict, and the fix
    for a rollback that left residue behind a written ref is not to choose
    between them: `clear_workspace` rmtrees `.git` and with it the discarded
    ref the audit event just named a human at, while leaving the tree alone
    breaks the "no carried-over context" promise. This keeps the repo — the
    next `init_repo` is idempotent — and clears what the next round would
    otherwise inherit.

    Total, like `clear_workspace`: never raises, and the answer is the observed
    state of the filesystem rather than a guess from which branch ran."""
    try:
        ws = Path(ws)
        if not ws.is_dir():
            return True
        for child in ws.iterdir():
            if child.name == ".git":
                continue
            try:
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
            except Exception:
                pass
        return not any(child.name != ".git" for child in ws.iterdir())
    except Exception:
        return False


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

        # A typo'd config key lands in the audit log, not only in a warning.
        # `LoopConfig.load` has no store, so it records what it ignored and this
        # is the first place with somewhere to put it. The stakes are a budget:
        # an operator who writes `"max_cost_per_task"` (dropping `usd`) keeps
        # the shipped default and bills every run against a cap they believe
        # they lowered, while `/api/config` renders the *effective* value with
        # nothing saying the file disagreed. A `warnings.warn` alone reaches
        # neither `agentloop events`, the REST API nor the SSE feed — the same
        # gap that made the original `print` insufficient, one channel up.
        #
        # Total: telemetry must never break construction.
        unknown = list(getattr(config, "unknown_keys", ()) or ())
        if unknown:
            try:
                self.store.log_event(
                    None,
                    "config_warning",
                    {
                        "path": str(getattr(config, "unknown_keys_path", "")),
                        "unknown_keys": unknown,
                        "message": (
                            "These keys were ignored; the shipped default is in "
                            "force for anything you meant to set."
                        ),
                    },
                )
            except Exception:
                pass

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
            # The guard below used to cover only `run_task`, leaving
            # `claim_next_task` — which opens a write transaction, and therefore
            # raises `sqlite3.OperationalError: database is locked` after the
            # busy timeout whenever two `agentloop run` processes share a
            # database — outside it. The thread then died past the `try`,
            # `errors` stayed empty, and `run()` returned a success count.
            #
            # Measured at `max_parallel_workers=3` with three pending tasks and
            # the claim raising from the second call on:
            #     RUN RETURNED: 1  -> reported success, no exception raised
            #     tasks: [(1,'needs_human'), (2,'pending'), (3,'pending')]
            # Two threads died, two tasks were silently dropped, and the only
            # signal was a `threading.excepthook` traceback on stderr — a
            # channel this project rules out everywhere else, because it reaches
            # neither `agentloop events` nor the SSE feed.
            #
            # The comment on the inner handler already stated the property
            # ("a thread that dies silently would leave the task claimed and the
            # run reporting success"); this is the one place it did not hold. The
            # sequential path propagates, so before this the two modes disagreed
            # about what a failed batch even looks like.
            try:
                _drain_body(worker_id)
            except BaseException as exc:  # noqa: BLE001 - re-raised by run()
                errors.append(exc)
                with cond:
                    cond.notify_all()

        def _drain_body(worker_id: str) -> None:
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
            # Every one of them is recorded before the first is raised. `errors`
            # is a list because n workers can fail independently — a real bug in
            # one thread and a locked database in two others is three
            # exceptions — and raising `errors[0]` reports one while discarding
            # the rest, on a path whose whole purpose is that a dying worker
            # must not be silent. The raise still carries only the first, so the
            # caller's behaviour is unchanged; what changes is that the others
            # stop vanishing.
            for exc in errors:
                try:
                    self.store.log_event(
                        None,
                        "worker_failed",
                        {
                            "error": f"{type(exc).__name__}: {exc}"[:1000],
                            "workers": n,
                            "raised": exc is errors[0],
                        },
                    )
                except Exception:
                    pass  # telemetry must not replace the exception below
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
                    self.store,
                    planner_runner,
                    self.registry,
                    plan_task,
                    self.memory,
                    config=self.config,
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

    # -- durability (slice 6): call, log, discard -----------------------------
    #
    # Every one of these is orchestration with **no logic in it**: no status,
    # threshold, revision count or budget rule reads a `VcsResult` (DD-8), and
    # none of them runs inside an open `Store.transaction()` (DD-10) — a 30s
    # `vcs_timeout_s` there would stall every dashboard reader on the shared
    # connection, and a raise would roll back the paired audit event.

    def _vcs_degraded(
        self, task_id: int, op: str, result, extra: dict | None = None
    ) -> None:
        """Record a durability degradation where something can see it.

        `"disabled"` and `"already"` never arrive here (F15c): a deliberate
        config choice is not a degradation, and an idempotent no-op is a
        success. Logging them would put a row and a `RuntimeWarning` on every
        run of the ~300 loop tests that switch vcs off.

        Both channels, per the `executor.py` precedent: a warning alone reaches
        neither `agentloop events` nor the SSE feed.
        """
        # A degradation is not always an *absence*: `rollback` can return
        # `ok=True, reason="residue"`, which means the call ran and files
        # survived it. Saying "ran without durability" there would be a
        # misdiagnosis of the same kind `no-workspace` was added to remove.
        if result.ok:
            message = (
                f"git {op} degraded ({result.reason}); task {task_id} kept a "
                f"partial result: {result.stderr}"
            )
        else:
            message = (
                f"git {op} unavailable ({result.reason}); "
                f"task {task_id} ran without durability: {result.stderr}"
            )
        warnings.warn(message, RuntimeWarning, stacklevel=2)
        self.store.log_event(
            task_id,
            "vcs_unavailable",
            {
                "op": op,
                "reason": result.reason,
                "stderr": result.stderr,
                **(extra or {}),
            },
        )

    def _vcs_mark_approved(self, task: Task, ws) -> None:
        """Point `refs/agentloop/approved` at the workspace tip (C3 and C4).

        Additive: it moves a ref and removes nothing. The result is logged and
        discarded. Every caller gates the call on `set_status` reporting that
        its DONE write *landed* — the row is lease-predicated, so a write that
        did not land would otherwise leave the approved ref asserting a
        transition the row never took."""
        result = vcs.mark_approved(ws, self.config, pin=self.store.vcs_pin(task.id))
        if result.ok:
            self.store.log_event(
                task.id, "vcs_commit", {"sha": result.sha, "ref": "approved"}
            )
        elif result.reason != "disabled":
            self._vcs_degraded(task.id, "mark_approved", result)

    def _vcs_rollback_to_base(self, task_id: int) -> vcs.VcsResult:
        """Return a task's workspace to `refs/agentloop/base` (C5 and C6).

        The one destructive call expression in this module - and it destroys
        nothing recoverable: `vcs.rollback` writes
        `refs/agentloop/discarded/<sha>` at the tip *before* anything moves
        (DD-12), so the discarded round stays reachable from `git log --all`,
        which is what makes "reject recovers the work" true rather than
        aspirational.

        Always to `base`, never to the approved ref (ADR-2/DD-2): the approved
        ref is a bookmark a human placed, and rolling onto it would let a
        reject of a later round silently resurrect an earlier approved one.

        `create=False` (the default) is load-bearing for the same reason it is
        at C4: rejecting a task whose workspace never existed must not conjure
        one - the guard then refuses with `not-a-workspace-repo`, and the
        workspace keeps whatever it held, which is today's behaviour exactly.

        The result is logged and returned; C6 reads it to choose a *filesystem
        shape* only. No status, threshold, revision count or budget rule reads
        it (DD-8).
        """
        result = vcs.rollback(
            workspace_for(self.config.workspace_root, task_id),
            vcs.BASE_REF,
            self.config,
            # The pin recorded when this workspace's repo was created. Read
            # from the store because that is the one place a worker cannot
            # write: a rollback runs `reset --hard` and `clean -ffdqx`, so a
            # config it did not vet is a command the worker chose.
            pin=self.store.vcs_pin(task_id),
        )
        if result.ok:
            payload = {"ref": "base"}
            if result.sha:
                # The pair is *absent*, not null, when HEAD was already at the
                # target and nothing was discarded: these two keys name the
                # recovery surface, and a null would assert one exists.
                payload["discarded_sha"] = result.sha
                payload["discarded_ref"] = f"{vcs.DISCARDED_REF_PREFIX}/{result.sha}"
            payload["files_removed"] = result.files_removed
            if result.nested_repos:
                # The ref above holds everything the rollback deleted *except*
                # a nested repository, which a commit can only record as a bare
                # gitlink. Named rather than quietly omitted: a log asserting a
                # recovery surface that does not hold the work is the defect,
                # not the bytes git cannot carry.
                payload["unrecoverable_nested_repos"] = list(result.nested_repos)
            if result.reason:
                # `reason` is `""` *exactly* when nothing degraded, so a
                # non-empty one on an ok result is a degradation the audit has
                # to carry: `"residue"` means the rollback ran and files
                # survived it, which the payload above reports as clean.
                payload["degraded"] = result.reason
            self.store.log_event(task_id, "vcs_rollback", payload)
            if result.reason:
                self._vcs_degraded(task_id, "rollback", result)
        elif result.reason != "disabled":
            # `sha` on a *failed* rollback means the discarded tip was recorded
            # before the failure, so the history a caller might now wipe is
            # exactly the history `vcs.rollback` refused to lose. The audit
            # says which of the two failures this was, because "ran without
            # durability" reads as "nothing happened" for both.
            preserved = bool(result.sha)
            extra: dict = {"history_preserved": preserved}
            if preserved:
                extra["discarded_sha"] = result.sha
                extra["discarded_ref"] = f"{vcs.DISCARDED_REF_PREFIX}/{result.sha}"
            if result.nested_repos:
                extra["unrecoverable_nested_repos"] = list(result.nested_repos)
            self._vcs_degraded(task_id, "rollback", result, extra)
        return result

    def run_task(self, task: Task) -> Task:
        # Both roles resolved up front, exactly as `plan()` resolves `planner`,
        # and for the two reasons that rule already gives.
        #
        # `Registry.load` replaces the built-in defaults wholesale with no merge
        # and no missing-role check, so a hand-edited agents.json that adds one
        # role (the documented slice-4 "pin the validator to openai" edit, done
        # by replacing the file) can leave `worker` undefined. `registry.get`
        # then raised a bare `KeyError` from `_maybe_handoff`, which matches
        # neither `except _ConfigError` nor `except _InfraError` and so escaped
        # `run_task` entirely: measured, the batch aborted, task 1 was left
        # `in_progress` still holding its lease with an empty
        # `escalation_reason` — a task the dashboard shows as running that
        # nothing is running — and every task behind it never ran. The next
        # `agentloop run` re-claimed it and died identically: permanent
        # starvation, with one stderr line as the only signal.
        #
        # A missing `validator` failed differently and no better: it was raised
        # *inside* `_with_retry`, so it became three paid retries and an
        # `infra_error` escalation, which CLAUDE.md names as pointing "the human
        # at the network instead of at agents.json".
        for role in (task.worker_role, task.validator_role):
            try:
                self.registry.get(role)
            except KeyError:
                self.store.set_status(
                    task,
                    TaskStatus.NEEDS_HUMAN,
                    reason=(
                        f"No {role!r} agent is registered; add one to agents.json "
                        f"(or delete it to fall back to the built-in defaults)."
                    ),
                )
                return self._require(task.id)

        feedback = ""
        test_result = TestResult()
        # Worker context consumed as of the last handoff. Measured, not reset in
        # the store, so it stays an in-loop local: a post-crash restart simply
        # re-measures from 0 and does one safe handoff at the first boundary if
        # the accumulated context already exceeds the threshold.
        handoff_watermark = 0
        # Durability locals (slice 6). `vcs_ready` is None until C1 has run
        # once for this `run_task` invocation, then a bool gating every later
        # vcs call; `round_n` names the per-round commits. Both are in-loop
        # locals for the same reason `handoff_watermark` is: a restart simply
        # re-initialises the repo, which is idempotent.
        vcs_ready: bool | None = None
        vcs_pin = ""
        round_n = 0
        while True:
            # Human control is read fresh from the store at each iteration
            # boundary, so a pause/abort set from another process (CLI or
            # dashboard) is honored between rounds rather than only on kill.
            # Ownership first, before anything that writes. Both checks below
            # stamp a status, and a status written by a worker that no longer
            # holds the lease lands on somebody else's round.
            # Every exit re-reads rather than returning the in-hand object.
            # `human_approve` already documents why: "`set_status` assigns the
            # new status onto it before the predicated write, so on a write that
            # did not land the object claims a transition the row never took."
            # `set_status` is lease-predicated and returns a bool, and these
            # exits did not check it — they returned the loop's *intention*
            # rather than the task's history. Latent only because all three
            # `run_task` call sites discard the value; the first caller to read
            # it (a `run --json`, a batch-eval assertion, a test) would get a
            # falsehood with no signal, so it is closed while it is still cheap.
            if self._claim_lost(task):
                return self._require(task.id)
            if self._control_stop(task):
                return self._require(task.id)
            if self._budget_tripped(task):
                return self._require(task.id)

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
                if vcs_ready is None:
                    # C1: one repo per task workspace (DD-1), created once per
                    # `run_task` invocation. `.git` is invisible to
                    # `_has_any_file`, so this cannot flip the tests gate.
                    vcs_pin = self.store.vcs_pin(task.id)
                    init = vcs.init_repo(ws, self.config, pin=vcs_pin)
                    if init.pin:
                        # Non-empty *only* when this call created the repo, so
                        # this records a config git wrote a moment ago and can
                        # never re-bless one a worker edited between rounds. It
                        # is recorded even on a failed init: the config exists
                        # either way, and a pin nobody recorded is a workspace
                        # nothing can ever act on again.
                        vcs_pin = init.pin
                        self.store.set_vcs_pin(task.id, vcs_pin)
                    vcs_ready = bool(init.ok or init.reason == "already")
                    if not init.ok and init.reason not in ("already", "disabled"):
                        self._vcs_degraded(task.id, "init", init)
                worker_runner = self._runner_for(task.worker_role)
                self._require_workspace("worker", ws)
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
                        config=self.config,
                    ),
                )
                if result.output.strip().upper().startswith("ESCALATE:"):
                    self.store.set_status(
                        task,
                        TaskStatus.NEEDS_HUMAN,
                        reason=f"Worker ambiguity: {result.output.strip()[9:].strip()}",
                    )
                    return self._require(task.id)
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
                    return self._require(task.id)
                task.output = result.output
                self.store.update_task(task)
                if vcs_ready:
                    # C2: the round's snapshot, taken after the output is in the
                    # store (which stays the sole source of truth for it) and
                    # *before* the pending-tool park, whose partial output is
                    # explicitly preserved and so must be in the commit.
                    round_n += 1
                    committed = vcs.commit(
                        ws, f"round {round_n}", self.config, pin=vcs_pin
                    )
                    if committed.ok:
                        self.store.log_event(
                            task.id,
                            "vcs_commit",
                            {"sha": committed.sha, "round": round_n},
                        )
                    else:
                        self._vcs_degraded(task.id, "commit", committed)

                # A capability the agent called load-bearing and does not have
                # (slice 5). Checked here — after the output is stored, before
                # TESTING — because that is the earliest point at which the work
                # already done is safe and nothing further has been spent: the
                # partial output is committed, no tests have run, and the
                # validator is not asked to review work the agent said it could
                # not finish. **Not a revision**: a missing capability is not a
                # quality gap a worker can be told to fix, so re-prompting would
                # burn the revision budget on a call that cannot succeed, and
                # `revision_count` is left alone.
                #
                # Only a `pending` + `blocking` row parks. A marker inside a
                # fenced code block *is* a live request — accepted, not worked
                # around: `parse_tool_requests` deliberately does not model
                # markdown fences (a second, lossy model of the reply that can
                # also drop a genuine ask), and the two failure modes are not
                # symmetric. A dropped request silently withholds a capability with
                # nothing in the ledger to show it was ever wanted; a false park
                # keeps the output, the workspace and the revision budget and asks
                # a human.
                #
                # What that costs the human, stated accurately, because the first
                # version of this comment claimed the recovery was free and it is
                # not. Four of the five exits are lossy: `approve_tool_request`
                # releases but *grants* the tool the quoted example named (a real
                # permission for a request nobody made); `reject_tool_request`
                # deliberately does not release, so it is a dead end;
                # `human_reject` and `abort` are terminal. `human_redo` releases
                # and wipes the workspace and resets `output` and
                # `revision_count` — exactly the three things this comment used to
                # promise were kept. **The neutral exit is `pause` then `resume`**:
                # it releases the lease, clears the `parked` flags, decides nothing,
                # and keeps all three. It is written down here because nothing else
                # would tell a human that the cheap way out is the one route not
                # named in the escalation reason.
                #
                # Not fixed by adding a release on rejection: a denial must never
                # restart a paid worker run against a gap the human just confirmed
                # will not be filled (`reject_tool_request`).
                pending_tools = self.store.pending_blocking_tool_requests(task.id)
                if pending_tools:
                    # The asking agent is named, not just the tool: a validator
                    # asking for `shell` to reproduce a bug and a worker unable to
                    # build without it produce the same park and want different
                    # answers from the human, and `agent_kind` is already on the
                    # row. Without it the two messages were indistinguishable.
                    named = ", ".join(
                        f"{r.tool} (request {r.id}, asked by the {r.agent_kind})"
                        for r in pending_tools
                    )
                    # One transaction: the `parked` stamp and the status event are
                    # one fact, and `tool_requests_mark_parked` logs nothing of its
                    # own precisely because `set_status` audits this transition
                    # with its reason. A second event for the park would let
                    # anyone counting escalations count them twice.
                    #
                    # The stamp is gated on the status write having *landed*, and
                    # the order is that way round for exactly that reason.
                    # `set_status` is lease-predicated, so it legitimately no-ops
                    # when a human took this task mid-round — and an unguarded stamp
                    # would then leave `parked=1` on a task that is not parked,
                    # which is the stale flag that makes a later unrelated
                    # escalation revertible by approving this request.
                    with self.store.transaction():
                        if self.store.set_status(
                            task,
                            TaskStatus.NEEDS_HUMAN,
                            reason=(
                                # "approve or reject" offered rejection as a way
                                # out, and it is not one: `reject_tool_request`
                                # deliberately does not release, so a human who
                                # followed this advice reached a dead end with the
                                # reason still recommending it. Nor is `pause` +
                                # `resume` or `redo` — each returns the task to the
                                # queue with this blocking row still standing, so
                                # the next round pays a worker call and parks
                                # again. Approving is the only decision that
                                # releases it, and the reason now says so instead
                                # of naming the routes that look symmetrical.
                                f"{_PARK_REASON_PREFIX}{named}. Partial output "
                                f"kept. `agentloop tools approve <id>` is the only "
                                f"decision that releases the task; rejecting "
                                f"records the denial and leaves it parked, and "
                                f"pause/resume or redo requeue it without "
                                f"deciding, so the next round parks on the same "
                                f"request."
                            ),
                        ):
                            self.store.tool_requests_mark_parked(
                                task.id, [r.id for r in pending_tools]
                            )
                    return self._require(task.id)

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
                self._require_workspace("validator", ws)
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
                        config=self.config,
                        # The same workspace the worker was given, for the same
                        # reason: the validator declares `file_io` and was
                        # reading the orchestrator's own directory while
                        # reviewing work that lives here. Spelled `cwd` and not
                        # `workspace` because it changes the validator's working
                        # directory *only* — the prompt is byte-for-byte what it
                        # was, and the worker's identically-valued `workspace=`
                        # eight lines up also feeds a prompt block.
                        cwd=str(ws),
                    ),
                )
            except _ConfigError as exc:
                # Escalates without a retry and without an infra_error event: a
                # typo'd runner name is not going to resolve on the third try.
                self.store.set_status(task, TaskStatus.NEEDS_HUMAN, reason=str(exc))
                return self._require(task.id)
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
                return self._require(task.id)
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
                return self._require(task.id)

            if approved:
                if task.risk_level >= cfg.human_review_risk_level:
                    self.store.set_status(
                        task,
                        TaskStatus.NEEDS_HUMAN,
                        reason="Validator approved; awaiting human sign-off "
                        "(high-risk task).",
                    )
                else:
                    landed = self.store.set_status(task, TaskStatus.DONE)
                    if landed and vcs_ready:
                        # C3: gated on the write *landing*, the one shape
                        # C4 and C5 also use. `set_status` is lease-predicated
                        # and returns whether the row was written, so a no-op
                        # DONE write must not move `refs/agentloop/approved`.
                        # The sibling high-risk branch above does not mark
                        # approved
                        # — that task is NEEDS_HUMAN, and its ref is written by
                        # `human_approve` instead (C4).
                        self._vcs_mark_approved(task, ws)
                return self._require(task.id)

            if task.revision_count >= cfg.max_revisions:
                self.store.set_status(
                    task,
                    TaskStatus.NEEDS_HUMAN,
                    reason=f"Exhausted {cfg.max_revisions} revisions without approval.",
                )
                return self._require(task.id)
            task.revision_count += 1
            self.store.set_status(task, TaskStatus.REVISING)
            feedback = verdict.reasoning

    # -- mid-run human control (pause / resume / abort) -----------------------

    _TERMINAL = (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.ABORTED)
    # The statuses `claim_next_task` re-offers to their owner — i.e. the ones a
    # live worker may still be inside. Distinct from "non-terminal": a parked
    # NEEDS_HUMAN task also holds its lease (the park does not release it), and a
    # redo there takes nothing from anybody.
    _IN_FLIGHT = (
        TaskStatus.IN_PROGRESS,
        TaskStatus.TESTING,
        TaskStatus.VALIDATING,
        TaskStatus.REVISING,
    )

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
            # Only this branch, and what that buys is narrower than it looks.
            # `resume` accepts any non-terminal task, including in-flight rows a
            # live worker still owns, and stripping *that* lease would leave a row
            # matching neither disjunct of the claim SELECT and invisible to
            # `stranded_claims` while `next_pending_task` still reported it —
            # "looks runnable, never runs", the failure the release exists to
            # remove, in a new shape. So the confinement is about not
            # manufacturing an unclaimable row.
            #
            # It is **not** what keeps two workers off one task, and must not be
            # read as such. `pause` stamps PAUSED immediately and never touches
            # the lease, so a worker inside a model call is still inside the task
            # when this branch runs; the row it leaves is pending and unleased,
            # which is exactly what the claim CAS matches. `Loop._claim_lost` is
            # the mechanism: the evicted worker re-reads its lease at the next
            # boundary and stands down. Nothing here can prove that worker gone.
            #
            # One transaction, not three: both parked accessors document "no
            # event of its own" *on the grounds that their caller writes a status
            # event in the same transaction*, and phase 5's release is specified
            # to copy this shape. As three commits that invariant was simply
            # false, and a crash between them left `parked=0` standing against
            # the old status. (A `try/except` here would be the hazard ADR-4
            # forbids; a transaction is not.)
            with self.store.transaction():
                self.store.release_claim(task_id)
                # The in-hand object has to learn what the store was just told, or
                # the very next line writes nothing: `update_task` is
                # lease-predicated, and the lease this object remembers no longer
                # exists. Same three lines in `human_redo` and in the tool-request
                # release, for the same reason.
                task.claimed_by = None
                # Resuming ends the parked state without deciding the request, so
                # the live flag goes with it; the row stays pending, so the need
                # is still recorded and the next round parks again.
                self.store.tool_requests_clear_parked(task_id)
                # Blanked explicitly, because `set_status(..., reason="")` does
                # not: it assigns only a *truthy* reason, so the row keeps
                # whatever it held. `pause` stamps "Paused by human; resume to
                # continue." onto every task it touches, so before this every
                # pause/resume cycle left that sentence on the row for the rest
                # of its life — measured still reading it on a `done` task,
                # asserting a suspension that had ended, directly above the
                # dashboard's decision buttons.
                #
                # This is the fifth release-to-PENDING path and the only one
                # that was missing the line: `reset_unowned_to_pending`,
                # `approve_tool_request`, `human_redo` and `approve_plan` all
                # blank it, and `store.py`'s own docstring explains why. Worse
                # here than elsewhere, because CLAUDE.md names pause+resume as
                # the *neutral* exit from a tool-request park — so the
                # documented recovery route was the one that overwrote the
                # park's diagnosis.
                task.escalation_reason = ""
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
        with self.store.transaction():
            self.store.log_event(task_id, "human_abort", {"note": note})
            # An aborted task is not being held at NEEDS_HUMAN on anything, so the
            # live flag is false and goes — see `tool_requests_clear_parked`.
            #
            # **Gated on the status write having landed**, the park's shape and for
            # the park's reason (`run_task`). `set_status` became lease-predicated
            # while this clear stayed unconditional, so a lease released between
            # this method's read and its write left `status=needs_human` with
            # `parked=0`: the transition never happened, and the release predicate's
            # condition 1 reads that flag, so the task was stuck at NEEDS_HUMAN with
            # no route back. Gated, it fails *closed* — the park stands and remains
            # liftable — which is why this is the fix rather than releasing the lease
            # here as `resume` and `human_redo` do: those two release it because
            # their own contract hands the task back, while a terminal human exit has
            # no business taking a lease off work it is not restarting.
            if self.store.set_status(
                task, TaskStatus.ABORTED, reason=note or "Aborted by human mid-run."
            ):
                self.store.tool_requests_clear_parked(task_id)
        return self._require(task_id)

    def _claim_lost(self, task: Task) -> bool:
        """Has this worker's lease been taken away since the last boundary?

        `PAUSED` is not a quiescence guarantee and never was: `pause` stamps the
        status immediately (its own docstring says so) and never touches the
        lease, so a worker learns of a pause only at its next boundary — after
        the current model call returns. A human who pauses, sees nothing happen
        and resumes therefore returns the row to `pending` with no lease, which
        is exactly what the claim's compare-and-swap matches, while the first
        worker is still inside the task. An idle peer takes it within
        `_IDLE_POLL_SECONDS`, or a second `agentloop run` process does at any
        parallelism, and then two workers are running one task: two paid worker
        attempts against one budget, two validator rounds, both writing
        `task.output`, either able to drive it terminal and release graph
        dependents against output the other is overwriting.

        The lease cannot be proven dead from *outside* — a retired claim id is
        indistinguishable from a live second process in the same id-space, which
        is why `stranded_claims` reports rather than reclaims. So the guarantee is
        built from the inside instead: the worker that lost the lease stands down.
        That is also what turns `human_redo`'s documented residual (a redo hands
        back a lease a live worker may still hold) from an assertion the human
        makes into one the evicted worker acts on.

        Deliberately keyed on the lease alone, not on the status: the lease *is*
        the ownership token, and every status a run passes through is written by
        the owner. `claimed_by` no longer matching covers both shapes — released
        to `NULL`, and already re-claimed by a peer.

        A task with no lease in hand (`run_task` called directly, as most tests
        and `agentloop status` do) has nothing to lose and is never stood down.

        Audited *and* warned, this project's standard for a degradation: stdout
        reaches neither `agentloop events` nor the SSE feed. No status write and
        no lease grab — the row is the new owner's. The interrupted round's
        attempt stays committed; it was paid for, and unwinding a paid attempt is
        the one thing this codebase never does.
        """
        if not task.claimed_by:
            return False
        current = self.store.get_task(task.id)
        holder = current.claimed_by if current else None
        if holder == task.claimed_by:
            return False
        warnings.warn(
            f"Worker {task.claimed_by!r} stood down from task {task.id}: its "
            f"lease is now held by {holder!r}. A pause+resume or a redo released "
            f"it mid-round; the task belongs to the new holder, so this round "
            f"ends here without writing to it.",
            RuntimeWarning,
            stacklevel=2,
        )
        with self.store.transaction():
            # Re-read inside the transaction. The read above decided *whether* to
            # stand down, which is this worker's own business and safe to answer
            # from a stale row; what gets written below is the shared row, and
            # between the two a human can approve, reject or redo the task.
            current = self.store.get_task(task.id)
            holder = current.claimed_by if current else None
            self.store.log_event(
                task.id,
                "claim_lost",
                {
                    "was": task.claimed_by,
                    "now": holder,
                    "status": (current.status.value if current else None),
                },
            )
            # The one case where standing down must *also* write: nobody holds the
            # lease and the row sits at a transient status. Only this worker could
            # have stamped that status — it happened between the release landing
            # and this boundary — and the row is now unreachable: it matches
            # neither disjunct of the claim SELECT (`status='pending'` is false and
            # `claimed_by=?` cannot match NULL) and is invisible to
            # `stranded_claims`, while `next_pending_task` still advertises it. So
            # `agentloop status` shows work the loop can never hand out, and only
            # `agentloop redo` recovers it.
            #
            # Deliberately *not* the general case: with `holder` set the row
            # belongs to whoever holds it now and this worker writes nothing, which
            # is the rule the rest of this method exists to enforce.
            #
            # Both halves of "unowned and mid-flight" are re-checked *by the
            # UPDATE*, not asserted here. The previous version read them, released
            # the lock, formatted a warning and then wrote — so "the row is unowned
            # by construction" was true of the read and false of the write, and a
            # `human_approve` landing in the gap had its signed-off `DONE` returned
            # to `PENDING` and re-claimed, with the graph dependents that `DONE`
            # released now racing a second worker. `set_status` cannot be the guard
            # (it writes the whole row from a `Task` object, unconditionally), so
            # the swap lives in the store beside the three other CASes.
            if holder is None:
                self.store.reset_unowned_to_pending(task.id, list(self._IN_FLIGHT))
        return True

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
        with self.store.transaction():
            self.store.log_event(task_id, "human_approve", {"note": note})
            # A DONE task is not parked on anything: `parked=1` asserts "the loop
            # stopped this task on this row and has not resumed past it", which is
            # false here, so clearing it is correct by definition rather than
            # defensive (`tool_requests_clear_parked` states the sentence, and why
            # `pause` — a suspension, not an exit — is the one caller absent). The
            # request row itself stays undecided — a human may still want to grant
            # it before a later redo.
            #
            # Gated on the status write, for the reason spelled out in `abort`.
            landed = self.store.set_status(task, TaskStatus.DONE)
            if landed:
                self.store.tool_requests_clear_parked(task_id)
        # Re-read rather than returning the in-hand object: `set_status` assigns the
        # new status onto it *before* the predicated write, so on a write that did
        # not land the object claims a transition the row never took.
        fresh = self._require(task_id)
        # C4: outside the transaction (DD-10), and only for a row that actually
        # reached DONE. A `risk_level >= human_review_risk_level` task reaches
        # DONE *only* through here, so without this the approved ref would be
        # absent for exactly the tasks a human vetted. Plan rows returned above.
        # `create=False` (the default) is load-bearing: approving a task whose
        # workspace never existed must not conjure one — the guard then refuses.
        if landed:
            self._vcs_mark_approved(
                fresh, workspace_for(self.config.workspace_root, task_id)
            )
        return fresh

    def human_reject(self, task_id: int, note: str = "") -> Task:
        task = self._require(task_id)
        with self.store.transaction():
            self.store.log_event(task_id, "human_reject", {"note": note})
            # Both the gate and the clear: see `human_approve` and `abort`.
            landed = self.store.set_status(task, TaskStatus.FAILED, reason=note)
            if landed:
                self.store.tool_requests_clear_parked(task_id)
        # C5: outside the transaction (DD-10), and gated on the write landing -
        # the same shape as C3 and C4. `set_status` is lease-predicated, so
        # "the row is already FAILED and committed" is exactly what its return
        # value exists to *not* assume: on a predicated miss the task was not
        # rejected, a live worker may be mid-round, and rolling its workspace
        # back would destroy the round that worker is still writing. On a
        # refusal, or a miss, the workspace simply keeps its files -
        # byte-for-byte what `human_reject` did before this slice, since it
        # touched the workspace not at all.
        #
        # There is deliberately no residue fallback here, and that is a
        # decision rather than an omission. Redo falls back to
        # `clear_workspace`, which rmtrees `.git` and with it the discarded
        # ref; a reject *keeps* the rejected work so a human can recover it, so
        # wiping the one thing that makes it recoverable would invert the point
        # of the call. Residue is audited instead - `_vcs_rollback_to_base`
        # logs `degraded` on the event and a `vcs_unavailable` row - so what
        # survived on disk is visible rather than hidden behind an ok result.
        if landed:
            self._vcs_rollback_to_base(task_id)
        return self._require(task_id)

    def approve_tool_request(self, request_id: int, note: str = "") -> Task:
        """Grant one requested tool, and lift the park **only if it caused it**.

        The grant is unconditional: the row becomes `approved`, which *is* the
        grant, and the next invocation of that role on that task gets the tool.

        The release is not. It fires only when all three hold:

        1. `request.parked` — the loop stopped this task *on this row*;
        2. the task is at NEEDS_HUMAN **and the park is what is holding it there**
           (`escalation_reason` carries `_PARK_REASON_PREFIX`) — so the park has
           not been resolved another way (a redo, an abort, a human decision on
           the task itself) *and* no other escalation has taken its place;
        3. no other `pending` + `blocking` + `parked` row remains — otherwise the
           release would pay a full worker round only to re-park at the next
           boundary.

        Anything narrower than all three inverts "fail safe toward NEEDS_HUMAN",
        which is the one property this project does not trade for simplicity.
        Releasing on `status == NEEDS_HUMAN` alone would let approving a stale
        `optional` request blank an exhausted-revisions, severe-disagreement or
        budget-cap diagnosis — the very message the human was being asked to act
        on. Deriving it from a pending blocking row alone is narrower and still
        wrong: a validator-authored blocking row sits on a task escalated for
        severe disagreement, so approving it would revert *that*.
        Condition 2's *status* half is redundant given that every terminal exit
        clears `parked` — redundant, not optional: it is the only thing standing
        between a stale flag and a reopened `DONE` task, and it costs one
        comparison. Its *reason* half is not redundant at all, and used not to
        exist: a status test defends terminal statuses only, while the four gates
        that fire upstream of the park check (empty output, worker `ESCALATE:`,
        budget cap, infra failure) all escalate to NEEDS_HUMAN — so a stale
        `parked` flag standing against one of those satisfied all three
        conditions, blanked the diagnosis and returned the task to `pending`.
        Latent rather than live (no live path has been shown to leave the flag
        standing there), and fixed anyway, because the claim above is the one this
        method is trusted for.

        A release is **not a redo**: `output`, `revision_count`, the audit trail
        and — the one that carries the work — the workspace all survive, so the
        worker continues from the files it already wrote with the tool it now has.
        What does *not* survive, and is not claimed to: the prompt. Nothing
        persists validator feedback, and on the park path there is none (the
        validator never ran), so the resumed worker re-reads the workspace rather
        than being handed its own partial text back.
        """
        req = self.store.tool_request_get(request_id)
        if req is None:
            raise KeyError(f"No tool request {request_id}")

        decision: dict[str, object] = {}

        def release_predicate() -> bool:
            """The three conditions, evaluated *inside* `tool_request_decide`'s
            transaction and *after* its compare-and-swap has flipped this row.

            Every term reads state a second `agentloop` process can be changing,
            so computed before the write this was the check-then-act the store
            spends three accessors refusing. Two humans clearing a two-row queue
            each saw the other's row still pending, each concluded "another park
            stands, do not release", and both granted — leaving the queue empty,
            `parked=1` standing and the task held at `NEEDS_HUMAN` with no route
            back (approve now raises, `resume` only acts on `PAUSED`, and a redo
            wipes the workspace the release exists to preserve). Called from
            inside, sqlite's write lock has already serialized the two decisions
            and the second one sees the first's committed row, so exactly one of
            them releases.

            Every read is fresh for the same reason, `req` included: a concurrent
            `human_redo` clears `parked` without deciding the row.
            """
            current_req = self.store.tool_request_get(request_id)
            # Freshly loaded: `set_status` writes the whole row from this object,
            # so a stale one would put back an `output` or a `revision_count` from
            # before.
            task = self._require(req.task_id)
            others = [
                r
                for r in self.store.pending_blocking_tool_requests(
                    req.task_id, parked_only=True
                )
                if r.id != request_id
            ]
            released = (
                bool(current_req and current_req.parked)
                # Not the status alone: four escalation gates fire upstream of the
                # park check and all four leave the task at NEEDS_HUMAN, so a
                # `parked` flag standing against one of them passed a
                # status-only test and blanked its diagnosis. The reason is what
                # says *which* escalation is holding the task, and only the park
                # writes this prefix (`_PARK_REASON_PREFIX`).
                and task.status == TaskStatus.NEEDS_HUMAN
                and task.escalation_reason.startswith(_PARK_REASON_PREFIX)
                and not others
            )
            decision["task"] = task
            decision["released"] = released
            return released

        with self.store.transaction():
            self.store.tool_request_decide(
                request_id,
                approved=True,
                by="human",
                note=note,
                released=release_predicate,
            )
            if decision["released"]:
                task = decision["task"]
                # The park is over, so the live flag goes with it and a later park
                # is a fresh fact rather than a residue of this one.
                self.store.tool_requests_clear_parked(task.id)
                # `set_status(..., reason="")` does not clear a stale reason —
                # only a truthy reason is assigned — so a released task would read
                # "awaiting tool approval" forever. Same explicit blanking
                # `human_redo` and `approve_plan` already do.
                task.escalation_reason = ""
                # Nothing else in the store clears `claimed_by`, and a `pending`
                # task that still holds a lease is unclaimable *and* starves the
                # claim loop behind it.
                self.store.release_claim(task.id)
                task.claimed_by = None  # see `resume`: the write below is predicated
                self.store.set_status(task, TaskStatus.PENDING, reason="")
        return self._require(req.task_id)

    def reject_tool_request(self, request_id: int, note: str = "") -> Task:
        """Deny one requested tool. The task **stays parked**.

        No new release path, deliberately: the three human choices that already
        exist (`approve` / `reject` / `redo` the task) cover what comes next, and a
        denial must never silently restart a paid worker run against a gap the
        human just confirmed will not be filled. The `parked` flag stays set for
        the same reason — the loop is still holding this task on this row.
        """
        req = self.store.tool_request_get(request_id)
        if req is None:
            raise KeyError(f"No tool request {request_id}")
        self.store.tool_request_decide(
            request_id, approved=False, by="human", note=note, released=False
        )
        return self._require(req.task_id)

    def human_redo(self, task_id: int, note: str = "") -> Task:
        """Full redo (spec §10 decision): same task definition, fresh start,
        NO carried-over context — output, feedback, and revision count reset.
        The audit trail of the failed run is preserved in events/attempts.

        **Residual, documented rather than fixed:** a redo of an *in-flight* task
        hands back a lease a live worker may still hold. It is inherent to the
        promise, not an oversight — `README.md` says a redo recovers a stranded
        claim, a stranded claim is in-flight by definition, and a retired claim id
        is indistinguishable from a live second process in the same id-space
        (`CLAUDE.md`, `stranded_claims`). Refusing an in-flight redo would remove
        exactly the recovery this exists to provide. A human redoing in-flight
        work **is** the human asserting the worker is gone, so the trail records
        that assertion as a `claim_taken_from_worker` event (and a
        `RuntimeWarning`, since stdout reaches neither `agentloop events` nor the
        SSE feed) instead of leaving it inferable only from timestamps.
        """
        task = self._require(task_id)
        self.store.log_event(task_id, "human_redo", {"note": note})
        # A fresh start clears any lingering pause/abort signal, otherwise the
        # redo would stop again at its first iteration boundary.
        self.store.set_control(task_id, "run")
        task.output = ""
        task.revision_count = 0
        task.escalation_reason = ""
        # Wipe the workspace too: a redo that reran over the previous attempt's
        # files would not be a fresh start. C6: with a repo, the rollback is
        # that wipe *and* keeps the round recoverable; without one - feature
        # off, git missing, a workspace predating this slice - or with residue
        # left behind, the fallback is today's `clear_workspace`, unchanged.
        # `vcs.is_repo` is deliberately not called first: `rollback` re-runs
        # the identical guard internally and says `not-a-workspace-repo`, so a
        # pre-check would be a second, racier copy of it.
        result = self._vcs_rollback_to_base(task_id)
        if not (result.ok and result.reason != "residue"):
            # ...except when the rollback failed *after* recording the
            # discarded tip (`sha` set on a failed result). `vcs.rollback`
            # aborts rather than lose that history, while `clear_workspace`
            # rmtrees `.git`, every round commit and that freshly written ref -
            # so an unconditional fallback destroyed precisely what the callee
            # had just refused to destroy, under a warning that reads as
            # "nothing happened". The tree is left as it is and the gap is
            # audited (`history_preserved` in the `vcs_unavailable` payload);
            # the next run re-initialises what is there, which is idempotent.
            #
            # The discriminator is "was a recovery ref written", not "did the
            # call succeed": `ok=True, reason="residue"` **with** a sha is the
            # production shape, so `result.ok or not result.sha` let an ok
            # result through to the wipe regardless of sha and destroyed the
            # ref `_vcs_rollback_to_base` had logged one statement earlier.
            if not result.sha:
                if not clear_workspace(self.config.workspace_root, task_id):
                    # The fallback for every failed rollback cannot itself fail
                    # silently: `rmtree` with an error handler installed
                    # swallows per-file failures, so a redo that kept the
                    # previous round would look exactly like one that did not.
                    warnings.warn(
                        f"Workspace for task {task_id} survived the redo wipe; "
                        f"the fresh start it promises is not what the next run "
                        f"will see.",
                        RuntimeWarning,
                        stacklevel=2,
                    )
                    self.store.log_event(
                        task_id,
                        "vcs_unavailable",
                        {"op": "clear_workspace", "reason": "residue", "stderr": ""},
                    )
            elif not _clear_worktree(
                workspace_for(self.config.workspace_root, task_id)
            ):
                # A ref exists, so the fresh start is kept by emptying the
                # working tree rather than by deleting the history that ref
                # names. Its own failure is audited for the same reason the
                # wipe's is: a redo that kept the previous round looks exactly
                # like one that did not.
                warnings.warn(
                    f"Workspace for task {task_id} kept files the redo could "
                    f"not clear; the fresh start it promises is not what the "
                    f"next run will see.",
                    RuntimeWarning,
                    stacklevel=2,
                )
                self.store.log_event(
                    task_id,
                    "vcs_unavailable",
                    {"op": "clear_worktree", "reason": "residue", "stderr": ""},
                )
        # One transaction for the release, the flag clear and the status write —
        # see `resume` for why the parked accessors' "no event of its own"
        # contract depends on it.
        with self.store.transaction():
            if task.claimed_by and task.status in self._IN_FLIGHT:
                # The residual in the docstring, made auditable: this redo is
                # taking a lease from work that still looks live.
                warnings.warn(
                    f"Redo of task {task_id} released a lease held by "
                    f"{task.claimed_by!r} while the task was {task.status.value}; "
                    f"a redo cannot tell a stranded claim from a live worker, so "
                    f"this is your assertion that the worker is gone.",
                    RuntimeWarning,
                    stacklevel=2,
                )
                self.store.log_event(
                    task_id,
                    "claim_taken_from_worker",
                    {"worker": task.claimed_by, "status": task.status.value},
                )
            # Hand the lease back with the task: nothing else in the store clears
            # `claimed_by`, so a redo that only wrote PENDING left a row the
            # claim's compare-and-swap could never match — which is what made
            # README's promise that a redo recovers a stranded claim aspirational
            # rather than true.
            self.store.release_claim(task_id)
            task.claimed_by = None  # see `resume`: the writes below are predicated
            # A redo also ends any park without deciding the request, so the live
            # "held on this row" flag must not survive into the fresh run.
            self.store.tool_requests_clear_parked(task_id)
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

    def _require_workspace(self, stage: str, ws) -> None:
        """Refuse to invoke an agent whose working directory is not there.

        Since slice 9's P1 the workspace is the agent's actual `cwd`, so its
        absence is now a *precondition* of the call rather than a detail of the
        prompt. The SDK's own answer is a `CLIConnectionError` ("Working
        directory does not exist"), an ordinary `Exception` — so `_with_retry`'s
        transient branch took it: three retries with backoff at full model cost,
        three `infra_error` rows, then NEEDS_HUMAN blaming the network for a
        permanent condition. Reachable in one round, because the worker holds
        `file_io` and Bash: a worker that removes or renames its own workspace
        leaves the validator pointing at a directory that no longer exists.

        Classified exactly as CLAUDE.md already classifies a missing API key or
        a 404 — config error, no retry, no `infra_error` event — and raised
        **outside** `_with_retry`, since `_ConfigError` is an `Exception` and
        that function's bare `except Exception` would otherwise retry it.

        The empty string is refused with the same breath: the SDK's `if
        self._cwd:` swallows it back to the orchestrator's own directory, which
        is the fail-open default this whole fix exists to close. Not reachable
        from today's callers; a guard that says no costs nothing.
        """
        path = str(ws or "")
        if path and os.path.isdir(path):
            return
        raise _ConfigError(
            f"{stage}: working directory does not exist: {path!r}. The task "
            f"workspace is where this agent's file tools resolve every relative "
            f"path, so the call was refused rather than made against the "
            f"orchestrator's own directory. Check `workspace_root` and whether "
            f"anything removed the workspace mid-task."
        )

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
