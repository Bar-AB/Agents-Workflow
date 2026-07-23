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


@dataclass
class AgentSpec:
    """Agent registry entry (spec §3): reproducible, auditable agent config."""

    role: str
    model: str
    system_prompt: str
    tools: list[str] = field(default_factory=list)
    context_budget_tokens: int = 100_000
    version: str = "1"
