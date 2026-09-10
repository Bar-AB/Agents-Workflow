"""Orchestration state machine (spec §4).

A `planner` agent decomposes a goal into a task graph (`Loop.plan`); `Loop.run`
drains it — sequentially by default, or `max_parallel_workers` at a time. No
schedule is held in memory: claimability is a predicate the store evaluates
inside the atomic claim (`store.claim_next_task`), so ordering holds the same
for one worker or many.

Per-round decision rules:
- worker output starts `ESCALATE:`, or verdict is `escalate` / confidence <
  severe_threshold -> NEEDS_HUMAN immediately (no revision).
- confidence >= approve_threshold and tests not failing -> DONE, unless
  risk_level >= human_review_risk_level, which requires a human sign-off first.
- otherwise -> revise, bounded by max_revisions; exhausted -> NEEDS_HUMAN.
- budget cap (tokens/cost) exceeded -> NEEDS_HUMAN.
- worker context crosses context_handoff_ratio of its budget -> summarized by
  the `summarizer` agent and restarted from the summary (`context_handoff`
  event); not a revision.
- a blocking `TOOL_REQUEST` -> NEEDS_HUMAN ("awaiting tool approval"); output
  and revision_count are kept. Scoped to worker/validator rounds only — a
  planner's blocking request queues but doesn't park the plan, since planning
  is read-only and a human reviews the plan anyway.
- a withheld/rejected tool request subtracts its *concrete* capability (not
  just the logical name) from the tools list, since `LOGICAL_TOOL_MAP` is not
  injective (`shell` and `git` both resolve to `Bash`). Audited as
  `tool_capability_withheld`.
- this worker's claim lease was stolen (a stranded claim reclaimed elsewhere)
  -> stand down, write no status; the already-paid attempt stays committed.
- transient infra failure -> retried with backoff, then NEEDS_HUMAN
  (`infra_error`); not counted as a revision.

Planning rules (`Loop.plan`) all fail the same way — NEEDS_HUMAN, zero child
tasks created, never partially applied:
- planner replies `ESCALATE:`, or the reply is unparseable / cyclic / has a
  dangling `depends_on` / exceeds `max_plan_tasks`.
- `plan_requires_approval` (default True) blocks a plan's tasks from being
  claimed until `approve_plan`.
- a task is claimable only once every dependency is DONE; a blocked task is
  skipped (not failed) and becomes claimable once its dependency resolves.

"Tests not failing" means the *executed* result, not the validator's own
`TESTS:` claim — a validator claiming pass over an executed fail is logged as
`test_disagreement` and cannot approve the task.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
import warnings
from pathlib import Path

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
from .executor import (
    TestExecutor,
    clear_workspace,
    credential_like_names,
    workspace_for,
)
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

_IDLE_POLL_SECONDS = 0.5

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
        self.worker_id = "loop"

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

        credential_like = credential_like_names(config.sandbox_env_allowlist)
        if credential_like:
            try:
                self.store.log_event(
                    None,
                    "config_warning",
                    {
                        "sandbox_env_allowlist_credential_like": credential_like,
                        "message": (
                            "These sandbox_env_allowlist entries look "
                            "credential-shaped (a name-pattern heuristic, not a "
                            "secret registry) and will be handed to arbitrary "
                            "generated test code. This is not refused — an "
                            "operator may need a provider key in a real test "
                            "suite — but it is not silent either."
                        ),
                    },
                )
            except Exception:
                pass

        projects = self.store.list_projects()
        if (
            len(projects) == 1
            and projects[0]["name"] == "Default"
            and projects[0]["repo_root"] == "."
            and projects[0]["workspace_mode"] == "scratch"
            and config.workspace_mode == "worktree"
        ):
            default_id = projects[0]["id"]
            resolved_repo_root = os.path.abspath(config.repo_root)
            try:
                self.store.repoint_project(
                    default_id, resolved_repo_root, config.workspace_mode
                )
            except Exception as exc:
                try:
                    self.store.log_event(
                        None,
                        "config_warning",
                        {
                            "resolved_repo_root": resolved_repo_root,
                            "workspace_mode": config.workspace_mode,
                            "error": str(exc),
                            "message": (
                                "Failed to reconcile the Default project's "
                                "repo_root/workspace_mode with loopconfig.json."
                            ),
                        },
                    )
                except Exception:
                    pass

    def run(self, max_tasks: int | None = None, project_id: int | None = None) -> int:
        """Process claimable tasks. Returns tasks processed.
        Safe to call after a crash/restart: state lives in the store.

        Sequential by default (`max_parallel_workers=1`). Above 1, that many
        threads each claim independently; the store's atomic claim is what makes
        that safe, and its blocked-task predicate is what keeps dependency order
        without the loop tracking a graph in memory.

        `project_id=None` (default, slice 10) spans every registered project
        in one pass — the same filter every thread shares in the parallel
        case, no per-project scheduling."""
        n = max(1, int(self.config.max_parallel_workers))
        self._warn_stranded_claims(self._worker_ids(n))
        if n == 1:
            return self._run_serial(self.worker_id, max_tasks, project_id)
        return self._run_parallel(n, max_tasks, project_id)

    def _run_serial(
        self, worker_id: str, max_tasks: int | None, project_id: int | None = None
    ) -> int:
        processed = 0
        while max_tasks is None or processed < max_tasks:
            task = self.store.claim_next_task(worker_id, project_id=project_id)
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

    def _run_parallel(
        self, n: int, max_tasks: int | None, project_id: int | None = None
    ) -> int:
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
                    task = self.store.claim_next_task(worker_id, project_id=project_id)
                    if task is None:
                        if state["busy"] == 0:
                            cond.notify_all()
                            return
                        cond.wait(timeout=_IDLE_POLL_SECONDS)
                        continue
                    state["claimed"] += 1
                    state["busy"] += 1
                try:
                    self.run_task(task)
                except BaseException as exc:  # noqa: BLE001 - re-raised below
                    errors.append(exc)
                    with cond:
                        state["busy"] -= 1
                        cond.notify_all()
                    return
                with cond:
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
            stopping.set()
            with cond:
                cond.notify_all()
            for t in threads:
                t.join()
            raise
        if errors:
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
        """Surface (never reclaim) in-flight tasks held by a claim id no live
        worker will use — lowering `max_parallel_workers` orphans them, and
        stealing one's task could be stealing a live process's work instead.
        """
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

    def plan(
        self,
        goal: str,
        acceptance_criteria: str,
        title: str = "",
        risk_level: int = 1,
        project_id: int | str | None = None,
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
            project_id=self.store.resolve_project(project_id),
        )
        self.store.add_task(plan_task)

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

        try:
            planner_runner = self._runner_for("planner")
        except _ConfigError as exc:
            self.store.set_status(plan_task, TaskStatus.NEEDS_HUMAN, reason=str(exc))
            return plan_task

        repo_root = self._worktree_repo_root(plan_task)
        try:
            if repo_root is not None:
                self._require_workspace("planner", repo_root)
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
                    cwd=(str(repo_root) if repo_root is not None else None),
                ),
            )
        except _ConfigError as exc:
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
                        project_id=plan_task.project_id,
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
        if not self.store.plan_tasks(plan_id):
            raise ValueError(
                f"Plan {plan_id} produced no tasks and cannot be approved "
                f"({task.escalation_reason or 'planning did not complete'}). "
                f"Re-plan the goal instead."
            )
        with self.store.transaction():
            self.store.log_event(plan_id, "human_approve_plan", {"note": note})
            self.store.set_plan_approved(plan_id, True)
            task.escalation_reason = ""
            self.store.set_status(task, TaskStatus.DONE)
        return self._require(plan_id)

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

    def _worktree_repo_root(self, task: Task | None = None) -> Path | None:
        """`None` in scratch mode; the absolute `repo_root` in worktree mode.

        Slice 9 P4. The single place `run_task` and its helpers ask "which
        mode is this run in" — every `vcs.*` call site below reads this once
        and threads the same answer through, rather than each re-deriving it,
        so scratch mode staying a *proven* no-op does not depend on getting
        the same derivation right in six different places. `os.path.abspath`,
        lexically, matching `vcs._git`'s own `-C` handling and
        `Store._repo_key` — not `Path.resolve()`, which would follow a
        junction and quietly answer a question about a different repository
        than the one `config.repo_root` names.

        Slice 10 Phase 3: optional `task`. When given and `task.project_id`
        resolves to a real project row whose `(repo_root, workspace_mode)`
        has moved off the bootstrap placeholder `(".", "scratch")`, reads
        THAT project's own `workspace_mode`/`repo_root` instead of the
        loop's global config — `None` if the row's `workspace_mode !=
        "worktree"`. When `task` is `None` (the plan-row call site, before
        any task workspace exists), the project lookup fails, or the
        project's row is STILL at the placeholder, falls back to
        `self.config.workspace_mode`/`self.config.repo_root` exactly as
        before this phase — the byte-for-byte fallback that keeps a bare
        `Loop(store, runner, registry, config)` with no extra `projects`
        row set up behaving identically to before this slice.

        Deliberately a placeholder check, not "is this the Default
        project": `Loop.__init__`'s reconciliation step tries to keep the
        Default project's row in sync with `self.config`, but it is a
        best-effort, never-fatal bootstrap — when it fails (e.g. a
        configured `repo_root` that does not exist), the row stays at the
        placeholder, and reading it there would silently reinterpret a real
        worktree-mode misconfiguration as scratch mode instead of letting it
        fail loudly the way it did before this slice (`_require_workspace`
        et al.). But once ANY project's row — including the Default
        project's, via a future `agentloop project repoint` (Phase 4) — has
        genuinely moved off the placeholder, that row is real operator
        intent and must take effect immediately; unconditionally preferring
        `self.config` for the Default project forever would make such a
        repoint silently inert until a config file was also hand-edited and
        the process restarted.

        The lookup is guarded: pre-Phase-3 this was a pure, in-memory
        function reading only `self.config`, and it is called from 5 sites
        in `run_task`/`plan`/`human_approve`/`human_reject`/`human_redo`,
        none of which wrap it in a retry/escalation path the way the
        adjacent `registry.get(role)` lookup in `run_task` does. A
        transient store error here (e.g. `database is locked` under two
        concurrent `agentloop` processes, the exact scenario this project's
        own claim-CAS design already treats as real) must degrade to the
        same config-based fallback the docstring already promises for "the
        project lookup fails" — silently returning `None`/config rather than
        crashing `run_task` uncaught and stranding the just-claimed task
        `in_progress` with its lease held and no escalation reason (the
        same 'wedged whole batch' shape CLAUDE.md documents for a missing
        registry role). Logged, not swallowed silently, so the degradation
        is visible."""
        if task is not None and task.project_id is not None:
            try:
                project = self.store.get_project(task.project_id)
            except Exception as exc:
                project = None
                try:
                    self.store.log_event(
                        task.id,
                        "config_warning",
                        {
                            "project_id": task.project_id,
                            "error": str(exc),
                            "message": (
                                "Failed to read this task's project row; "
                                "falling back to the loop's own config for "
                                "repo_root/workspace_mode resolution."
                            ),
                        },
                    )
                except Exception:
                    pass
            if project is not None and (
                project["repo_root"] != "." or project["workspace_mode"] != "scratch"
            ):
                if project["workspace_mode"] != "worktree":
                    return None
                return Path(os.path.abspath(project["repo_root"]))
        if self.config.workspace_mode != "worktree":
            return None
        return Path(os.path.abspath(self.config.repo_root))

    def _worktree_pin(self, task_id: int, repo_root: Path | None) -> str:
        """The pin to verify a `vcs.*` call against: the repository-level
        baseline in worktree mode (`Store.vcs_repo_pin`, P2's remediation —
        every task of one repository shares one `.git/config`, so a per-task
        mint would let an ordinary later task re-bless a config an earlier one
        poisoned), the per-task pin in scratch mode (`Store.vcs_pin`,
        unchanged since slice 8)."""
        if repo_root is not None:
            return self.store.vcs_repo_pin(str(repo_root))
        return self.store.vcs_pin(task_id)

    def _record_vcs_pin(self, task_id: int, repo_root: Path | None, pin: str) -> None:
        """Where a pin `init_repo` just minted is recorded — the repo-level
        baseline table in worktree mode, the per-task table in scratch mode.
        Mirrors `_worktree_pin`'s choice of table, and is only ever called
        with a non-empty `pin` (see the callers: `VcsResult.pin` is non-empty
        only on the branch that actually minted one)."""
        if repo_root is not None:
            self.store.set_vcs_repo_pin(str(repo_root), pin)
        else:
            self.store.set_vcs_pin(task_id, pin)

    def _vcs_mark_approved(self, task: Task, ws, repo_root: Path | None = None) -> None:
        """Point `refs/agentloop/approved` at the workspace tip (C3 and C4).

        Additive: it moves a ref and removes nothing. The result is logged and
        discarded. Every caller gates the call on `set_status` reporting that
        its DONE write *landed* — the row is lease-predicated, so a write that
        did not land would otherwise leave the approved ref asserting a
        transition the row never took.

        Slice 9 P4: `repo_root` is `None` in scratch mode (unchanged) and the
        operator's checkout root in worktree mode, where `mark_approved`
        itself picks the per-task ref (`vcs.approved_ref(task_id)`) over the
        shared constant — this call site only has to supply which mode it is
        in and the matching pin."""
        result = vcs.mark_approved(
            ws,
            self.config,
            pin=self._worktree_pin(task.id, repo_root),
            repo_root=repo_root,
            task_id=task.id,
            branch_prefix=self.config.vcs_branch_prefix,
        )
        if result.ok:
            self.store.log_event(
                task.id, "vcs_commit", {"sha": result.sha, "ref": "approved"}
            )
        elif result.reason != "disabled":
            self._vcs_degraded(task.id, "mark_approved", result)

    def _vcs_detect_validator_writes(
        self, task_id: int, ws, pin: str, before, repo_root: Path | None = None
    ) -> None:
        """Record what the validator changed in the workspace after the round
        was snapshotted (H3). **Detection, not prevention.**

        Slice 9 P4: `repo_root` threads the caller's mode through to
        `working_tree_state`, unchanged behaviour in scratch mode
        (`repo_root=None`).

        The shipped validator declares `file_io` (Read + Write + Edit) and
        slice 9 P1 pointed that write surface at the task workspace, while
        `run_task`'s order is worker -> round commit -> tests -> validator. So
        a validator write lands *after* the snapshot: the approved ref can be
        moved to a tip whose tree lacks it, `clean -ffdqx` deletes it with no
        discarded ref covering it, and a revision round starts from a tree
        `task.output` does not describe.

        Two things this deliberately does not do. It does not **prevent** the
        write - narrowing a shipped agent's declared capability has tool-gate
        blast radius and is a human's decision, not this call's. And it does
        not **commit** the write, which would be worse: it would make a
        validator's silent edits to the worker's output part of the approved
        tree. Naming the gap honestly is the same posture as
        `nested_repos` and `ignored_unrecoverable`.

        Call, log, discard, like every other `vcs.*` site: nothing here is read
        by a status transition, a threshold, a revision count or a budget
        check. Test-run artefacts are excluded by construction - the `before`
        snapshot is taken *after* the executor has run - so what is left is
        attributable to the validator.

        The name carries the `_vcs_` prefix because the AST guard's taint
        walker is per-function and keyed on it: without the prefix `before`
        arrives untainted and a future status write branching on it would not
        be caught. One word, and it is the difference between an enforced
        invariant and an argued one.

        Two things it compares, and both were wrong before this remediation.
        It diffs porcelain **entries** (`"<XY> <path>"`), not bare paths, so an
        *overwrite* of a path the worker had already left dirty is visible
        (` M x` -> `MM x` is a real write with an unchanged name). And it
        records when either snapshot was **truncated** by the reporting cap, or
        when the `after` snapshot could not be taken at all: over a real
        checkout more than the cap's worth of changed paths is ordinary, and a
        detection that silently stops looking is worse than none.

        That "worse than none" reasoning has to apply symmetrically. `before`
        arrives already folded to `None` for the case with nothing to watch
        (scratch mode, or vcs not ready) — silence there is correct, there was
        never a snapshot to fail. But `before is not None and not before.ok`
        means a snapshot was *attempted* and the read itself failed, which is
        exactly the after-side failure this function already logs a few lines
        down — dropping it here instead was the asymmetry HIGH-1 named."""
        if before is None:
            return
        if not before.ok:
            self.store.log_event(
                task_id,
                "validator_write_detection_failed",
                {
                    "reason": before.reason,
                    "note": (
                        "The workspace could not be read before the "
                        "validator ran, so whether it wrote anything is "
                        "unknown. This is not a claim that it did not."
                    ),
                },
            )
            return
        after = vcs.working_tree_state(
            ws,
            self.config,
            pin=pin,
            repo_root=repo_root,
            task_id=task_id,
            branch_prefix=self.config.vcs_branch_prefix,
        )
        if not after.ok:
            self.store.log_event(
                task_id,
                "validator_write_detection_failed",
                {
                    "reason": after.reason,
                    "note": (
                        "The workspace could not be re-read after the "
                        "validator ran, so whether it wrote anything is "
                        "unknown. This is not a claim that it did not."
                    ),
                },
            )
            return
        appeared = [
            entry
            for entry in after.changed_entries
            if entry not in before.changed_entries
        ]
        blind = tuple(
            f"{when}.{field}"
            for when, snapshot in (("before", before), ("after", after))
            for field in snapshot.truncated
        )
        if not appeared and not blind:
            return
        self.store.log_event(
            task_id,
            "validator_wrote_workspace",
            {
                "entries": appeared,
                "truncated": list(blind),
                "prevented": False,
                "note": (
                    "The validator changed the workspace after this round was "
                    "committed, so these entries are outside the round "
                    "snapshot and outside the discarded ref a later rollback "
                    "writes. This is a detection only: nothing stopped or "
                    "undid the write, and the validator's declared file_io "
                    "capability is unchanged. Where 'truncated' is non-empty "
                    "the named snapshot hit its reporting cap, so this list is "
                    "incomplete rather than exhaustive."
                ),
            },
        )

    def _vcs_rollback_to_base(
        self, task_id: int, repo_root: Path | None = None
    ) -> vcs.VcsResult:
        """Return a task's workspace to its base ref (C5 and C6).

        The one destructive call expression in this module - and it destroys
        nothing recoverable: `vcs.rollback` writes a discarded ref at the tip
        *before* anything moves (DD-12), so the discarded round stays
        reachable from `git log --all`, which is what makes "reject recovers
        the work" true rather than aspirational.

        Always to base, never to the approved ref (ADR-2/DD-2): the approved
        ref is a bookmark a human placed, and rolling onto it would let a
        reject of a later round silently resurrect an earlier approved one.

        `create=False` (the default) is load-bearing for the same reason it is
        at C4: rejecting a task whose workspace never existed must not conjure
        one - the guard then refuses with `not-a-workspace-repo`, and the
        workspace keeps whatever it held, which is today's behaviour exactly.

        Slice 9 P4: `repo_root` selects the per-task ref names (`base_ref`,
        `discarded_ref_prefix`) worktree mode requires (P1's probe: worktrees
        share one ref namespace, so a fixed name would let task 2's rollback
        target task 1's base) — `None` reproduces the scratch-mode constants
        byte for byte.

        The result is logged and returned; C6 reads it to choose a *filesystem
        shape* only. No status, threshold, revision count or budget rule reads
        it (DD-8).
        """
        ref = vcs.base_ref(task_id if repo_root is not None else None)
        prefix = vcs.discarded_ref_prefix(task_id if repo_root is not None else None)
        result = vcs.rollback(
            workspace_for(
                self.config.workspace_root,
                task_id,
                config=self.config,
                repo_root=repo_root,
            ),
            ref,
            self.config,
            pin=self._worktree_pin(task_id, repo_root),
            repo_root=repo_root,
            task_id=task_id,
            branch_prefix=self.config.vcs_branch_prefix,
        )
        if result.ok:
            payload = {"ref": "base"}
            if result.sha:
                payload["discarded_sha"] = result.sha
                payload["discarded_ref"] = f"{prefix}/{result.sha}"
            payload["files_removed"] = result.files_removed
            if result.nested_repos:
                payload["unrecoverable_nested_repos"] = list(result.nested_repos)
            if result.reason:
                payload["degraded"] = result.reason
            self.store.log_event(task_id, "vcs_rollback", payload)
            if result.reason:
                self._vcs_degraded(task_id, "rollback", result)
        elif result.reason != "disabled":
            preserved = bool(result.sha)
            extra: dict = {"history_preserved": preserved}
            if preserved:
                extra["discarded_sha"] = result.sha
                extra["discarded_ref"] = f"{prefix}/{result.sha}"
            if result.nested_repos:
                extra["unrecoverable_nested_repos"] = list(result.nested_repos)
            self._vcs_degraded(task_id, "rollback", result, extra)
        return result

    def _vcs_snapshot_repo_root(
        self, repo_root: Path | None, pin: str
    ) -> vcs.VcsResult | None:
        """The main repository's own `git status`, or `None` when there is
        nothing to snapshot (scratch mode, or vcs unavailable). Slice 9 P4,
        residual 2.

        `vcs.repo_status`, not `vcs.working_tree_state`: `repo_root` fits
        neither of `_guard`'s two shapes (it is not inside `workspace_root`,
        the way a scratch workspace must be, and its `.git` is a directory,
        not a worktree gitfile) — `repo_status` is the narrower entry point
        for reading the operator's own repository directly, pinned against
        the same repository-level baseline every worktree-mode call in this
        class already reads (`Store.vcs_repo_pin`).

        `None` means only "nothing to snapshot" (`repo_root is None`) — an
        attempted-and-failed read is returned as-is, `ok=False` and all, not
        folded away. HIGH-1's remediation: this used to fold `not result.ok`
        into `None` too, which made a genuine read failure indistinguishable
        from scratch mode at every caller, and `_vcs_detect_out_of_branch_
        write`'s before-side silently treated a failed snapshot as "nothing to
        watch" instead of "a gap in what was watched"."""
        if repo_root is None:
            return None
        return vcs.repo_status(repo_root, self.config, pin=pin)

    def _vcs_detect_out_of_branch_write(
        self,
        task_id: int,
        repo_root: Path | None,
        pin: str,
        before: vcs.VcsResult | None,
    ) -> None:
        """Residual 2 (slice 9 plan): the executor sandbox's documented
        `..`/absolute-path escape gets a far worse target under worktree mode
        — the operator's real checkout instead of a throwaway directory — and
        `worktree_root` living outside `repo_root` only *reduces* the
        likelihood (relative traversal now has to climb out of an
        agentloop-owned directory first), it does not close the escape: an
        absolute path still reaches the repository outright (test 28).

        **Detection, not prevention** — the same posture as
        `_vcs_detect_validator_writes` (H3), reusing its shape rather than
        inventing a second one: a `git status` snapshot of `repo_root` taken
        before and after the test command runs, diffed, and logged as a
        degradation when they differ. Nothing here stops or undoes a write;
        the event text says so explicitly, because a detection that reads
        like a barrier is worse than an absent one — an operator who believes
        the escape is closed stops looking for it.

        `before=None` covers only "nothing to snapshot" (scratch mode, or vcs
        not ready) — there is nothing to diff against, and this must not
        manufacture a false positive by comparing against an empty baseline.
        `before is not None and not before.ok` is the other case, HIGH-1's
        remediation target: a snapshot was attempted and the read itself
        failed. That used to be folded into the same `None` as "nothing to
        snapshot" (`_vcs_snapshot_repo_root` no longer does that), which made
        the before-side silently drop a detection gap the after-side already
        logged explicitly a few lines down — the exact asymmetry this
        function's own "detection that silently stops looking is worse than
        none" is about."""
        if before is None:
            return
        if not before.ok:
            self.store.log_event(
                task_id,
                "worktree_write_detection_failed",
                {
                    "reason": before.reason,
                    "note": (
                        "The main repository could not be read before the "
                        "test command ran, so whether it wrote outside the "
                        "task's branch is unknown. This is not a claim that "
                        "it did not."
                    ),
                },
            )
            return
        after = self._vcs_snapshot_repo_root(repo_root, pin)
        if after is None or not after.ok:
            self.store.log_event(
                task_id,
                "worktree_write_detection_failed",
                {
                    "reason": after.reason if after is not None else "",
                    "note": (
                        "The main repository could not be re-read after the "
                        "test command ran, so whether it wrote outside the "
                        "task's branch is unknown. This is not a claim that "
                        "it did not."
                    ),
                },
            )
            return
        appeared = [
            entry
            for entry in after.changed_entries
            if entry not in before.changed_entries
        ]
        blind = tuple(
            f"{when}.{field}"
            for when, snapshot in (("before", before), ("after", after))
            for field in snapshot.truncated
        )
        if not appeared and not blind:
            return
        self.store.log_event(
            task_id,
            "worktree_out_of_branch_write",
            {
                "entries": appeared,
                "truncated": list(blind),
                "prevented": False,
                "note": (
                    "The test command changed the main repository — outside "
                    "this task's worktree — while it ran. This is a "
                    "detection only: nothing stopped or undid the write. "
                    "worktree_root is configured outside repo_root, which "
                    "reduces the likelihood of an accidental relative-path "
                    "escape but does not close an absolute-path one; see "
                    "residual 2 in the slice 9 design."
                ),
            },
        )

    def run_task(self, task: Task) -> Task:
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
        handoff_watermark = 0
        vcs_ready: bool | None = None
        vcs_pin = ""
        round_n = 0
        repo_root = self._worktree_repo_root(task)
        while True:
            if self._claim_lost(task):
                return self._require(task.id)
            if self._control_stop(task):
                return self._require(task.id)
            if self._budget_tripped(task):
                return self._require(task.id)

            try:
                handoff_summary = self._maybe_handoff(
                    task, feedback, test_result, handoff_watermark
                )
                if handoff_summary is not None:
                    handoff_watermark = self.store.attempt_tokens(task.id, "worker")

                self.store.set_status(task, TaskStatus.IN_PROGRESS)
                ws = workspace_for(
                    self.config.workspace_root,
                    task.id,
                    create=(repo_root is None),
                    config=self.config,
                    repo_root=repo_root,
                )
                if vcs_ready is None:
                    vcs_pin = self._worktree_pin(task.id, repo_root)
                    init = vcs.init_repo(
                        ws,
                        self.config,
                        pin=vcs_pin,
                        repo_root=repo_root,
                        task_id=task.id,
                        start_ref=self.config.vcs_base_ref,
                        branch_prefix=self.config.vcs_branch_prefix,
                    )
                    if init.pin:
                        vcs_pin = init.pin
                        self._record_vcs_pin(task.id, repo_root, vcs_pin)
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
                    # Escalate, not revise: empty output isn't a quality gap a
                    # re-prompt can fix, and letting it through would let a
                    # validator approve a blank diff and release dependents
                    # against output that doesn't exist.
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
                    round_n += 1
                    committed = vcs.commit(
                        ws,
                        f"round {round_n}",
                        self.config,
                        pin=vcs_pin,
                        repo_root=repo_root,
                        task_id=task.id,
                        branch_prefix=self.config.vcs_branch_prefix,
                    )
                    if committed.ok:
                        self.store.log_event(
                            task.id,
                            "vcs_commit",
                            {"sha": committed.sha, "round": round_n},
                        )
                    else:
                        self._vcs_degraded(task.id, "commit", committed)

                pending_tools = self.store.pending_blocking_tool_requests(task.id)
                if pending_tools:
                    named = ", ".join(
                        f"{r.tool} (request {r.id}, asked by the {r.agent_kind})"
                        for r in pending_tools
                    )
                    with self.store.transaction():
                        if self.store.set_status(
                            task,
                            TaskStatus.NEEDS_HUMAN,
                            reason=(
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

                self.store.set_status(task, TaskStatus.TESTING)
                repo_before = (
                    self._vcs_snapshot_repo_root(repo_root, vcs_pin)
                    if vcs_ready
                    else None
                )
                test_result = self._with_retry(
                    task, "executor", lambda: self.executor.run(ws)
                )
                self.store.add_test_run(task.id, None, test_result)
                self._vcs_detect_out_of_branch_write(
                    task.id, repo_root, vcs_pin, repo_before
                )

                self.store.set_status(task, TaskStatus.VALIDATING)
                validator_runner = self._runner_for(task.validator_role)
                self._require_workspace("validator", ws)
                tree_before = (
                    vcs.working_tree_state(
                        ws,
                        self.config,
                        pin=vcs_pin,
                        repo_root=repo_root,
                        task_id=task.id,
                        branch_prefix=self.config.vcs_branch_prefix,
                    )
                    if vcs_ready
                    else None
                )
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
                        cwd=str(ws),
                    ),
                )
            except _ConfigError as exc:
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
            self._vcs_detect_validator_writes(
                task.id, ws, vcs_pin, tree_before, repo_root
            )

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
                        self._vcs_mark_approved(task, ws, repo_root)
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

    _TERMINAL = (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.ABORTED)
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
            with self.store.transaction():
                self.store.release_claim(task_id)
                task.claimed_by = None
                self.store.tool_requests_clear_parked(task_id)
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
            if self.store.set_status(
                task, TaskStatus.ABORTED, reason=note or "Aborted by human mid-run."
            ):
                self.store.tool_requests_clear_parked(task_id)
        return self._require(task_id)

    def _claim_lost(self, task: Task) -> bool:
        """Has this worker's lease been taken away since the last boundary?

        `PAUSED` is not a quiescence guarantee: `pause` stamps the status
        immediately without touching the lease, so a worker learns of it only
        at its next boundary, after the current model call returns. A human
        who pauses, sees nothing happen, and resumes returns the row to
        `pending` with no lease — exactly what the claim CAS matches — while
        the first worker is still inside the task. An idle peer or a second
        `agentloop run` process can then claim it too, and two workers end up
        running one task: two paid attempts against one budget, both writing
        `task.output`, either able to release graph dependents against output
        the other is overwriting.

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
            if holder is None:
                self.store.reset_unowned_to_pending(task.id, list(self._IN_FLIGHT))
        return True

    def _control_stop(self, task: Task) -> bool:
        """Honor a pause/abort signal set since the last boundary. Returns True
        if the loop should stop working this task."""
        control = self.store.get_control(task.id)
        if control == "abort":
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

    def human_approve(self, task_id: int, note: str = "") -> Task:
        task = self._require(task_id)
        if task.kind == "plan":
            return self.approve_plan(task_id, note)
        if task.status == TaskStatus.PENDING:
            raise ValueError(
                f"Task {task_id} has not run yet (status=pending); there is "
                f"nothing to approve. Use `run` to execute it, or `reject` to "
                f"drop it."
            )
        with self.store.transaction():
            self.store.log_event(task_id, "human_approve", {"note": note})
            landed = self.store.set_status(task, TaskStatus.DONE)
            if landed:
                self.store.tool_requests_clear_parked(task_id)
        fresh = self._require(task_id)
        if landed:
            repo_root = self._worktree_repo_root(fresh)
            self._vcs_mark_approved(
                fresh,
                workspace_for(
                    self.config.workspace_root,
                    task_id,
                    config=self.config,
                    repo_root=repo_root,
                ),
                repo_root,
            )
        return fresh

    def human_reject(self, task_id: int, note: str = "") -> Task:
        task = self._require(task_id)
        with self.store.transaction():
            self.store.log_event(task_id, "human_reject", {"note": note})
            landed = self.store.set_status(task, TaskStatus.FAILED, reason=note)
            if landed:
                self.store.tool_requests_clear_parked(task_id)
        if landed:
            self._vcs_rollback_to_base(task_id, self._worktree_repo_root(task))
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
                self.store.tool_requests_clear_parked(task.id)
                task.escalation_reason = ""
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
        self.store.set_control(task_id, "run")
        task.output = ""
        task.revision_count = 0
        task.escalation_reason = ""
        repo_root = self._worktree_repo_root(task)
        result = self._vcs_rollback_to_base(task_id, repo_root)
        if repo_root is not None:
            if result.ok and result.reason != "residue":
                ws = workspace_for(
                    self.config.workspace_root,
                    task_id,
                    config=self.config,
                    repo_root=repo_root,
                )
                pin = self._worktree_pin(task_id, repo_root)
                removed = vcs.remove_worktree(
                    ws,
                    self.config,
                    pin,
                    repo_root=repo_root,
                    task_id=task_id,
                    branch_prefix=self.config.vcs_branch_prefix,
                )
                if removed.ok or removed.reason == "no-workspace":
                    vcs.remove_task_branch(
                        repo_root,
                        self.config,
                        pin,
                        task_id=task_id,
                        branch_prefix=self.config.vcs_branch_prefix,
                    )
                    recreated = vcs.init_repo(
                        ws,
                        self.config,
                        pin,
                        repo_root=repo_root,
                        task_id=task_id,
                        start_ref=self.config.vcs_base_ref,
                        branch_prefix=self.config.vcs_branch_prefix,
                    )
                    if not recreated.ok and recreated.reason not in (
                        "already",
                        "disabled",
                    ):
                        self._vcs_degraded(task_id, "init", recreated)
                elif removed.reason != "disabled":
                    self._vcs_degraded(task_id, "remove_worktree", removed)
        elif not (result.ok and result.reason != "residue"):
            if not result.sha:
                if not clear_workspace(self.config.workspace_root, task_id):
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
        with self.store.transaction():
            if task.claimed_by and task.status in self._IN_FLIGHT:
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
            self.store.release_claim(task_id)
            task.claimed_by = None  # see `resume`: the writes below are predicated
            self.store.tool_requests_clear_parked(task_id)
            self.store.update_task(task)
            self.store.set_status(task, TaskStatus.PENDING, reason="")
        return task

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
