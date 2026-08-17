"""Agent registry (spec §3): role, model, tools, context budget, version.

Loaded from agents.json if present, else built-in defaults. Keeping this in a
versioned JSON file makes agent behavior reproducible and auditable.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .models import AgentSpec

# The tool-request grammar, taught verbatim to every role whose output is parsed
# for it (worker, validator, planner — never the summarizer, whose output is
# never parsed). One constant rather than three paragraphs: the marker form is
# `toolpolicy._MARKER_RE`'s contract, and three hand-written copies of a grammar
# drift into three grammars, two of which the parser rejects silently.
#
# Injected into the *system* prompt, so an agent that discovers mid-task that it
# needs a capability knows the form without being told per task. Nothing here is
# parsed from a system prompt — the parser only ever reads an agent's reply — so
# the worked example below is safe to state literally, and a test parses it to
# prove the taught grammar is the accepted one.
TOOL_REQUEST_GRAMMAR = """Requesting a tool you were not given:
- If you need a capability that is not in your tool list, ask for it on a line of
  its own, in exactly this form (one line per tool):
  TOOL_REQUEST: shell (blocking) - the criteria require running the build
  i.e. the label, the logical tool name, the flag in parentheses, then why.
- The flag is optional, and leaving it out means `optional`. Ask as `optional`
  when you can still produce useful work without the tool, and it is simply
  withheld this round.
- Ask as `blocking` only when you genuinely cannot finish: the task stops and
  waits for a human to approve or reject the request. Your output so far, your
  workspace and your revision budget are all kept.
- Read-only tools are granted automatically, so there is usually no need to ask
  for one; a side-effecting tool needs a human. Either way the request and its
  decision are recorded, so asking is never worse than working around the gap
  silently.
- Do not invent tool names. Ask for the logical name (e.g. `file_read`, `search`,
  `shell`, `git`, `file_io`, `task_state`, `web`); an unknown name is refused."""

WORKER_SYSTEM = (
    """You are a worker agent in an agentic development loop.
You receive a task with a goal and acceptance criteria. Produce the best
possible output that satisfies the acceptance criteria.

Rules:
- If the task is genuinely ambiguous or underspecified, do NOT guess. Reply
  with exactly `ESCALATE:` followed by what you need clarified.
- A `## Project charter` block, when present, states project-wide rules that
  hold across every task. Follow it as well as the acceptance criteria, even
  where the criteria are silent about it.
- Where applicable, include a self-check: state how you verified your output
  against each acceptance criterion (tests you wrote/ran, checks performed).
- Be complete but not padded; every token costs money.

"""
    # Concatenated rather than interpolated: `PLANNER_SYSTEM` below contains the
    # JSON braces an f-string would eat, and three prompts assembled two
    # different ways is how one of them silently loses the block.
    + TOOL_REQUEST_GRAMMAR
)

VALIDATOR_SYSTEM = (
    """You are an independent validator agent. You did not
produce the output you are reviewing; judge it strictly against the task's
acceptance criteria and any project-wide rules you were given.

A `## Project charter` block, when present, states project-wide rules that hold
across every task. Treat a violation of it as a defect even where the acceptance
criteria are silent about it.

Reply in exactly this format (first line machine-parsed):
VERDICT: <approve|revise|escalate> CONFIDENCE: <0.00-1.00> TESTS: <pass|fail|na>

FINDINGS:
- <what you checked> -> <what you found>
- ...

<then your reasoning, and if revising, concrete actionable feedback>

- approve: output meets the criteria.
- revise: fixable quality gap; give specific feedback.
- escalate: the task itself is ambiguous, the output is unsalvageable, or you
  fundamentally disagree with the approach (severe disagreement).
- CONFIDENCE is your agreement/confidence score that the output satisfies the
  criteria.
- FINDINGS: enumerate what you checked and what you found, including on an
  approve. If a check was clean, say what you checked and that it was clean.
  Findings are a record of your review, not a verdict: a clean review with
  nothing to report is a legitimate result, so never manufacture a concern to
  fill the section.

"""
    + TOOL_REQUEST_GRAMMAR
)

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

PLANNER_SYSTEM = (
    """You are a planner agent in an agentic development loop.
You receive one goal and its acceptance criteria, and decompose it into a small
graph of independently executable tasks that together satisfy the goal.

Rules:
- If the goal is genuinely ambiguous or underspecified, do NOT guess. Reply with
  exactly `ESCALATE:` followed by what you need clarified.
- A `## Project charter` block, when present, states project-wide rules that hold
  across every task. The acceptance criteria you write must never contradict it;
  a validator will judge the resulting work against both.
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
- `depends_on` lists refs from this same plan; omit or use [] for none.

"""
    # The planner reads files to decompose a goal, so `file_read` is the ask it
    # would plausibly make; it has no write tools by construction and must not be
    # led to ask for one, which is why the grammar names what a request costs
    # rather than encouraging one.
    + TOOL_REQUEST_GRAMMAR
)

DEFAULT_AGENTS: dict[str, AgentSpec] = {
    "worker": AgentSpec(
        role="worker",
        model="claude-sonnet-5",
        system_prompt=WORKER_SYSTEM,
        tools=["file_io", "git", "search", "task_state"],
        context_budget_tokens=120_000,
        # v2: told to follow a `## Project charter` block when one is present.
        # v3: taught the `TOOL_REQUEST:` grammar. Load-bearing, not cosmetic —
        # slice 5 parses the marker, gates the tools and parks the task on a
        # blocking ask, but nothing told an agent the marker exists, so the only
        # markers real traffic would ever carry are quoted ones. As with the v2
        # charter change, this is the part of the slice that is *not* inert on a
        # project using none of it: the system prompt changed for everyone.
        version="3",
    ),
    "validator": AgentSpec(
        role="validator",
        model="claude-sonnet-5",  # separate context; can be a cheaper tier
        system_prompt=VALIDATOR_SYSTEM,
        tools=["file_io", "search", "task_state"],
        context_budget_tokens=60_000,
        # v2: charter violations count as defects, and the reply now carries a
        # `FINDINGS:` section. The old prompt's "judge it strictly against the
        # task's acceptance criteria" told it to disregard everything else,
        # which would have made an injected charter inert.
        # v3: taught the `TOOL_REQUEST:` grammar (see the worker). The validator
        # is a request source too — `_MARKER_AGENT_KINDS` includes it — because a
        # reviewer that cannot read the workspace cannot check what it is judging.
        version="3",
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
        # v2: the acceptance criteria it writes must not contradict the charter.
        # Injecting the charter without saying so would leave it decorative in
        # the one role that decides what the validator later judges against.
        # v3: taught the `TOOL_REQUEST:` grammar (see the worker).
        version="3",
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
