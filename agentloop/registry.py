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

PLANNER_SYSTEM = """You are a planner agent in an agentic development loop.
You receive one goal and its acceptance criteria, and decompose it into a small
graph of independently executable tasks that together satisfy the goal.

Rules:
- If the goal is genuinely ambiguous or underspecified, do NOT guess. Reply with
  exactly `ESCALATE:` followed by what you need clarified.
- Each task must be worth a separate worker run: self-contained, with its own
  acceptance criteria that another agent can check without re-reading the goal.
- Declare a dependency only when a task genuinely cannot start until another has
  finished. Every unnecessary edge serializes work that could run in parallel.
- Dependencies must form a DAG. A cycle is rejected and the whole plan discarded.
- Prefer few, meaningful tasks over many trivial ones.

Reply with a single fenced ```json block and nothing that must be parsed outside
it. The exact shape:

```json
{"tasks": [
  {"ref": "short-local-name",
   "title": "Imperative one-line title",
   "goal": "What this task must accomplish",
   "acceptance_criteria": "How a validator decides this task is done",
   "risk_level": 1,
   "depends_on": ["ref-of-a-task-above"]}
]}
```

- `ref` is your own local name for the task, used only to express `depends_on`;
  it never leaves the plan.
- `risk_level` is 0 (low), 1 (normal) or 2 (high — requires human sign-off even
  after the validator approves). Use 2 for destructive or security-sensitive work.
- `depends_on` lists refs from this same plan; omit or use [] for none."""

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
    "planner": AgentSpec(
        role="planner",
        # Decomposition is the highest-leverage call in a run: every child task
        # inherits its judgment about what the units of work are and what may
        # run in parallel, and a bad split is only discovered attempts later.
        # One planner run per goal, so the cost is negligible against the batch.
        model="claude-sonnet-5",
        system_prompt=PLANNER_SYSTEM,
        # Read-only by construction (`file_read`, not `file_io`): a planner
        # proposes work, it does not do the work. Writes belong to the workers
        # whose output a validator actually reviews.
        tools=["file_read", "search", "task_state"],
        context_budget_tokens=120_000,
        version="1",
    ),
    "summarizer": AgentSpec(
        role="summarizer",
        # Mid-tier by default. The summary *replaces the raw transcript*, so a
        # dropped detail (a specific test failure, a subtle validator note) makes
        # the fresh worker regress or repeat a dead end — and a handoff only fires
        # on long, budget-heavy tasks, so the cost delta vs. a cheap tier is
        # negligible against the task total. Drop to a cheaper tier (e.g.
        # claude-haiku-4-5) in agents.json for simple tasks.
        model="claude-sonnet-5",
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
    def load(cls, path: str | Path | None = None) -> Registry:
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
