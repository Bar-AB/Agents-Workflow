"""Core domain types. Kept dependency-free (stdlib dataclasses only)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    TESTING = "testing"
    VALIDATING = "validating"
    REVISING = "revising"
    NEEDS_HUMAN = "needs_human"
    PAUSED = "paused"
    DONE = "done"
    FAILED = "failed"
    ABORTED = "aborted"


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
    output: str = ""
    escalation_reason: str = ""
    control: str = "run"  # 'run' | 'pause' | 'abort'
    claimed_by: str | None = None
    kind: str = "task"
    plan_id: int | None = None
    project_id: int | None = None


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
    blocking: bool = False
    parked: bool = False
    attempt_id: int | None = None
    decided_by: str = ""
    decided_note: str = ""
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
    tool_calls: list[dict] = field(default_factory=list)
    usage_estimated: bool = False
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
    coverage_percent: float | None = None

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
    runner: str | None = None
