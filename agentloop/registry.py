"""Agent registry (spec §3): role, model, tools, context budget, version.

Loaded from agents.json if present, else built-in defaults. Keeping this in a
versioned JSON file makes agent behavior reproducible and auditable.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .models import AgentSpec

WORKER_SYSTEM = """You are a worker agent in an agentic development loop.
You receive a task with a goal and acceptance criteria. Produce the best
possible output that satisfies the acceptance criteria.

Rules:
- If the task is genuinely ambiguous or underspecified, do NOT guess. Reply
  with exactly `ESCALATE:` followed by what you need clarified.
- Where applicable, include a self-check: state how you verified your output
  against each acceptance criterion (tests you wrote/ran, checks performed).
- Be complete but not padded; every token costs money."""

VALIDATOR_SYSTEM = """You are an independent validator agent. You did not
produce the output you are reviewing; judge it strictly against the task's
acceptance criteria.

Reply in exactly this format (first line machine-parsed):
VERDICT: <approve|revise|escalate> CONFIDENCE: <0.00-1.00> TESTS: <pass|fail|na>
<then your reasoning, and if revising, concrete actionable feedback>

- approve: output meets the criteria.
- revise: fixable quality gap; give specific feedback.
- escalate: the task itself is ambiguous, the output is unsalvageable, or you
  fundamentally disagree with the approach (severe disagreement).
- CONFIDENCE is your agreement/confidence score that the output satisfies the
  criteria."""

SUMMARIZER_SYSTEM = """You are a summarizer agent in an agentic development
loop. A worker's accumulated context has grown large, so it is being handed off
to a fresh worker instance. Compress the working state so the fresh worker can
continue with no loss of what matters.

Produce a faithful, compact summary that preserves:
- the task goal and acceptance criteria (restate the essentials);
- what has been done so far and the current state of the output;
- the latest validator feedback and exactly what still needs to change;
- any decisions, constraints, or dead ends already established.

Rules:
- Do NOT invent progress or facts not present in the material you were given.
- Be terse; this replaces a full transcript, so every token must earn its place.
- Output only the summary — no preamble."""

DEFAULT_AGENTS: dict[str, AgentSpec] = {
    "worker": AgentSpec(
        role="worker",
        model="claude-sonnet-5",
        system_prompt=WORKER_SYSTEM,
        tools=["file_io", "git", "search", "task_state"],
        context_budget_tokens=120_000,
        version="1",
    ),
    "validator": AgentSpec(
        role="validator",
        model="claude-sonnet-5",  # separate context; can be a cheaper tier
        system_prompt=VALIDATOR_SYSTEM,
        tools=["file_io", "search", "task_state"],
        context_budget_tokens=60_000,
        version="1",
    ),
    "summarizer": AgentSpec(
        role="summarizer",
        # A cheap tier: compacting known text is easy work relative to the
        # worker's, so it need not run on the worker's model.
        model="claude-haiku-4-5",
        system_prompt=SUMMARIZER_SYSTEM,
        tools=[],  # reads and writes text only; no tools
        context_budget_tokens=60_000,
        version="1",
    ),
}


class Registry:
    def __init__(self, agents: dict[str, AgentSpec]):
        self.agents = agents

    def get(self, role: str) -> AgentSpec:
        if role not in self.agents:
            raise KeyError(f"No agent registered for role {role!r}")
        return self.agents[role]

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Registry":
        if path and Path(path).exists():
            # utf-8-sig: agents.json is hand-edited, often on Windows.
            raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
            agents = {name: AgentSpec(**spec) for name, spec in raw.items()}
            return cls(agents)
        return cls(dict(DEFAULT_AGENTS))

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps({n: asdict(s) for n, s in self.agents.items()}, indent=2),
            encoding="utf-8",
        )
