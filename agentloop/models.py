"""Core domain types. Kept dependency-free (stdlib dataclasses only)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class TaskStatus(str, Enum):
    PENDING = "pending"            # defined, waiting for the loop
    IN_PROGRESS = "in_progress"    # worker executing
    VALIDATING = "validating"      # validator reviewing
    REVISING = "revising"          # validator said revise; bounded retry
    NEEDS_HUMAN = "needs_human"    # escalated: ambiguity, severe disagreement,
                                   # budget trip, or high-risk sign-off
    DONE = "done"
    FAILED = "failed"              # human rejected / redo abandoned


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
    risk_level: int = 1              # 0=low, 1=normal, 2=high (spec §4.7)
    revision_count: int = 0
    worker_role: str = "worker"
    validator_role: str = "validator"
    # Latest worker output (also stored per-attempt in the attempts table).
    output: str = ""
    escalation_reason: str = ""


@dataclass
class RunResult:
    """What a ModelRunner returns for one agent invocation."""
    output: str
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = "unknown"


@dataclass
class Verdict:
    kind: VerdictKind
    confidence: float          # 0.0–1.0 agreement/confidence score (spec §5)
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
