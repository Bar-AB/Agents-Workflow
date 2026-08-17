"""Core domain types. Kept dependency-free (stdlib dataclasses only)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class TaskStatus(str, Enum):
    PENDING = "pending"  # defined, waiting for the loop
    IN_PROGRESS = "in_progress"  # worker executing
    TESTING = "testing"  # executing the task's tests for real
    VALIDATING = "validating"  # validator reviewing
    REVISING = "revising"  # validator said revise; bounded retry
    NEEDS_HUMAN = "needs_human"  # escalated: ambiguity, severe disagreement,
    # budget trip, or high-risk sign-off
    PAUSED = "paused"  # human paused mid-run; survives restart,
    # resumes on explicit resume (not auto)
    DONE = "done"
    FAILED = "failed"  # human rejected / redo abandoned
    ABORTED = "aborted"  # human aborted mid-run; terminal but the
    # output and audit trail are left intact


class VerdictKind(str, Enum):
    APPROVE = "approve"
    REVISE = "revise"
    ESCALATE = "escalate"  # severe disagreement or genuine ambiguity


@dataclass
class Task:
    id: int | None
    title: str
    goal: str
    acceptance_criteria: str
    status: TaskStatus = TaskStatus.PENDING
    risk_level: int = 1  # 0=low, 1=normal, 2=high (spec §4.7)
    revision_count: int = 0
    worker_role: str = "worker"
    validator_role: str = "validator"
    # Latest worker output (also stored per-attempt in the attempts table).
    output: str = ""
    escalation_reason: str = ""
    # Mid-run human control signal, read at each loop iteration boundary.
    control: str = "run"  # 'run' | 'pause' | 'abort'
    # Which worker claimed this task (None = unclaimed). Set atomically by the
    # store's claim; a worker only resumes in-flight tasks it owns.
    claimed_by: str | None = None
    # 'task' = work a worker executes. 'plan' = a planner-owned container row
    # holding the goal that was decomposed; it carries the planner's attempts
    # and the plan's approval, and is never claimed by the loop — handing a
    # goal statement to a worker as though it were a task is exactly the bug.
    kind: str = "task"
    # Which plan produced this task (None for hand-defined tasks). Provenance,
    # and the handle the plan's approval gate is applied through.
    plan_id: int | None = None


class ToolRequestStatus(str, Enum):
    """What has been decided about one tool request.

    `auto` and `approved` are the two that *are* a grant — there is no separate
    grant object, so a permission can never exist with no request behind it.
    `refused` is terminal and machine-made (an unknown logical name, or the
    per-task cap), never a human's decision: a human's are `approved` and
    `rejected`.
    """

    AUTO = "auto"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    REFUSED = "refused"


class ToolRequestSource(str, Enum):
    """Who asked. `marker` is an agent-authored `TOOL_REQUEST:` line in its own
    reply; `declared` is the role's registry `tools` list, gated at invoke time
    behind `gate_declared_tools`. The distinction is load-bearing: the agent
    never asked for a declared tool, so it cannot have called it load-bearing,
    and a declared-source request is therefore never blocking."""

    MARKER = "marker"
    DECLARED = "declared"


@dataclass
class ToolRequest:
    """One ask for one logical tool, for one task, by one role.

    Task-scoped and role-scoped by construction: a grant is approval of *this*
    capability on *this* task, in the same register as approving a memory value
    or a plan, and never a standing permission. A standing grant is already
    expressible by editing agents.json — a deliberate act with a diff.
    """

    id: int | None
    task_id: int
    role: str
    agent_kind: str
    tool: str
    status: ToolRequestStatus = ToolRequestStatus.PENDING
    source: ToolRequestSource = ToolRequestSource.MARKER
    reason: str = ""
    # What the agent asked for: a blocking request parks its task rather than
    # letting it continue without the tool.
    blocking: bool = False
    # What the loop did about it: "this task is being held at NEEDS_HUMAN right
    # now, on this row". A *live* fact, not a historical one — the audit log
    # keeps the history — so every exit from the parked state clears it, and a
    # tool decision can never revert an escalation the tool queue did not cause.
    # It sits beside `blocking` because the pair is one fact in two tenses.
    parked: bool = False
    attempt_id: int | None = None
    decided_by: str = ""
    decided_note: str = ""
    # Floats, matching the REAL columns and `time.time()`, like every other
    # timestamp in the store.
    created_at: float = 0.0
    decided_at: float | None = None


@dataclass
class PlannedTask:
    """One node of a planner-proposed task graph, before it becomes a row.

    `ref` is the planner's own local name for the node: it has to express edges
    in a single reply, before any database id exists. Refs are resolved to task
    ids when the graph is persisted and are not stored.
    """

    ref: str
    title: str
    goal: str
    acceptance_criteria: str
    risk_level: int = 1
    depends_on: list[str] = field(default_factory=list)


@dataclass
class RunResult:
    """What a ModelRunner returns for one agent invocation.

    `tokens_in` is *new* input only. Prompt-cache tokens are tracked separately
    because they are priced differently (write 1.25x, read 0.10x the input
    rate) and a real run can hold thousands of cache tokens against a handful
    of new-input tokens — folding them into `tokens_in` both mis-prices the run
    and hides where the spend actually went.
    """

    output: str
    tokens_in: int = 0
    tokens_out: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    model: str = "unknown"
    # Tools the agent actually invoked during this run, as
    # {"tool": name, "input": {...}} — provenance for the audit log. Empty for
    # runners that cannot report them; a backend that stops reporting degrades
    # to "nothing recorded" rather than to a wrong record.
    tool_calls: list[dict] = field(default_factory=list)
    # True when some part of the token counts above is this runner's estimate
    # rather than the provider's measurement. Without it the estimate reaches
    # `attempts` and the dashboard indistinguishable from a measured number, and
    # a fabricated cost is displayed as a real one. `agents._invoke` turns the
    # pair below into one `runner_warning` event on the attempt.
    usage_estimated: bool = False
    # Why, in one bounded line. Free text rather than an enum because it is read
    # by a human debugging a run, and coerced (never trusted) at the logging
    # boundary: telemetry must never fail an attempt.
    notes: str = ""


@dataclass
class TestResult:
    """Outcome of really executing a task's tests (spec §5).

    `status` is authoritative over the validator's self-reported TESTS: field.
    `na` means no workspace or execution disabled — not a failure.
    """

    __test__ = False  # not a pytest test class, despite the name

    status: str = "na"  # pass | fail | na | error
    exit_code: int | None = None
    summary: str = ""
    stdout_tail: str = ""
    duration_s: float = 0.0

    @property
    def passed(self) -> bool | None:
        """Tri-state for the approval gate: True/False, or None when n/a."""
        if self.status == "pass":
            return True
        if self.status in ("fail", "error"):
            return False
        return None


@dataclass
class Verdict:
    kind: VerdictKind
    confidence: float  # 0.0–1.0 agreement/confidence score (spec §5)
    reasoning: str
    tests_passed: bool | None = None  # test state feeds the verdict (spec §5)
    # What the validator says it checked and what it found: evidence, not a
    # gate. A *copy* of a slice of `reasoning`, never a piece removed from it —
    # `reasoning` is what the loop feeds back to the worker as revision
    # feedback, so moving the findings out would strip the most actionable part
    # of every revision prompt. Empty when the validator wrote no findings
    # section, which is recorded and never treated as a reason to revise.
    findings: str = ""


@dataclass
class AgentSpec:
    """Agent registry entry (spec §3): reproducible, auditable agent config."""

    role: str
    model: str
    system_prompt: str
    tools: list[str] = field(default_factory=list)
    context_budget_tokens: int = 100_000
    version: str = "1"
    # Which ModelRunner backend serves this role (slice 4). None = the runner
    # the loop was constructed with, which is what makes an unpinned run
    # behaviorally identical to the pre-slice-4 loop.
    #
    # It lives here rather than in LoopConfig because model and provider are one
    # decision, and `model` is already here: a `claude-sonnet-5` string means
    # nothing to an OpenAI endpoint, so splitting the pair across two files
    # would let a config edit produce a combination that cannot run. The
    # registry is already the per-role surface for exactly this kind of choice
    # (prompt, tools, context budget, version), and it grows no knob per role.
    #
    # Defaulted, so a hand-edited agents.json predating the field still loads:
    # `Registry.load` splats `AgentSpec(**spec)`, and an absent key is the
    # default rather than a TypeError.
    runner: str | None = None
