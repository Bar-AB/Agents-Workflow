# The agentloop guide

Everything a developer or a new user needs, in one place: what this is, how
to run it, how it works end to end, why it's built the way it is, and how it
compares to other agentic coding setups. For the CLI/config reference, see
[README.md](../README.md). For contributor-level module detail, see
[CLAUDE.md](../CLAUDE.md). For the blow-by-blow of specific hardening passes,
see [docs/history/](history/).

## What this is

agentloop is a small, standalone system that runs AI coding tasks
**unattended**, safely, with a paper trail. You describe a task (or a goal to
decompose into many tasks), point it at a codebase, and it churns:

```
worker writes code -> tests run for real -> an independent validator reviews
it against your acceptance criteria -> approve / revise / escalate to a human
```

Every step — every prompt, every token spent, every git commit, every
decision — is a row in one SQLite database. A live dashboard reads that same
database. There is no separate state to keep in sync, and nothing about the
loop's behavior lives only in memory.

It is **not** a Claude Code plugin, and it doesn't run inside your terminal
session. It's a Python CLI + SQLite + a small web dashboard, designed to run
as a batch job (in CI, on a schedule, or just left running) against a real
model provider, with hard limits on cost and clear stop points for a human.

## Quick start

```bash
pip install -e ".[dev]"                          # + ".[claude]" for the real runner
pytest -q                                         # sanity check

agentloop add "Add slugify util" \
  --goal "Write slugify(text) in utils.py" \
  --criteria "Lowercase, hyphen-separated, handles unicode, has tests"

agentloop run --runner claude                     # or --runner openai, or --runner mock
agentloop status 1                                # tokens, cost, wall time, verdict
agentloop events 1                                # the full audit trail

cd web && npm install && npm run build && cd ..
agentloop serve                                   # dashboard at the printed URL
```

Full command reference (every subcommand, every flag) lives in
[README.md](../README.md#quick-start).

## Walkthrough: one task, start to finish

1. **`agentloop add`** writes a `pending` task row: title, goal, acceptance
   criteria, a risk level (0-2).
2. **`agentloop run`** claims it (an atomic compare-and-swap in SQLite, so
   two `agentloop run` processes can never grab the same task) and hands it
   to the **worker** agent, along with any approved memory facts relevant to
   this task and the project charter (house rules), if one is set.
3. The worker writes files into the task's own workspace — its own throwaway
   git repo, committed as a round each time it writes. By default that's a
   fresh scratch directory unrelated to any real repo; opt into
   `workspace_mode: "worktree"` and it becomes a real `git worktree` checkout
   of your actual repository instead, on its own branch, so a task can build
   on your existing code — that mode intentionally shares your repo's git
   history (see [worktree workspaces](../README.md#existing-repository-workspaces-slice-9)
   in the README for the isolation trade-offs that come with it).
4. Your `test_command` runs for real, in that workspace, sandboxed (argv-only,
   scrubbed environment, timeout, output cap) — never through a shell, never
   fed anything the model wrote as a command.
5. The **validator** — optionally a *different model / different provider*
   from the worker — reviews the diff, the real test output, and the
   criteria, and returns a verdict: approve, revise, or escalate, with a
   confidence score and a findings list.
6. The loop applies one fixed decision rule (see below) to that verdict. Most
   of the time: high confidence + passing tests -> **done**. Low confidence
   or failing tests -> **revise** (worker tries again, bounded by
   `max_revisions`). Ambiguous or over budget -> **needs_human**.
7. Every one of those steps writes an event to the append-only `events`
   table — the dashboard's live feed *is* that table, streamed over SSE.
8. If it needed a human: you review it in the dashboard or CLI
   (`agentloop approve|reject|redo`) and it resumes exactly where it left off,
   or restarts clean.

A **goal** instead of a single task goes through `agentloop plan`, which asks
a `planner` agent to decompose it into a DAG of tasks (parallel-safe,
dependency-ordered), which a human approves before any of it can run
(`agentloop approve-plan`).

## Architecture, at a glance

```
                 ┌─────────────┐
  agentloop add  │             │  agentloop plan
  ─────────────► │   SQLite    │ ◄──────────────── planner agent
                  │   store.py  │
                  │ (single     │
                  │  source of  │
                  │  truth)     │
                  └──────┬──────┘
                         │ claim_next_task (atomic)
                         ▼
                 ┌───────────────┐        ┌──────────────┐
                 │  worker agent │──edit──►│ task workspace│
                 │  (registry.py,│         │ own git repo  │
                 │   agents.py)  │         │  (vcs.py)      │
                 └───────┬───────┘        └──────┬────────┘
                         │                         │ real test run
                         ▼                         ▼ (executor.py, sandboxed)
                 ┌───────────────┐        ┌──────────────┐
                 │ validator agent│◄──────┤ test results  │
                 │ (independent   │        └──────────────┘
                 │  model/provider)│
                 └───────┬────────┘
                         ▼
                 ┌───────────────┐
                 │  loop.py       │  decision rules -> done / revise / needs_human
                 │  (state        │
                 │   machine)     │
                 └───────┬───────┘
                         ▼
                 ┌───────────────┐        ┌──────────────┐
                 │  events table  │──SSE──►│ web dashboard │
                 └───────────────┘        └──────────────┘
```

Module map (what to open for what):

| Module | Owns |
|---|---|
| `store.py` | The database — tasks, attempts, verdicts, events, memory, charter, task graph, projects. Everything else reads/writes through here. |
| `loop.py` | The state machine: applies the decision rules, runs the round loop, handles pause/resume/abort, parallel workers. |
| `agents.py` | Builds worker/validator/planner/summarizer prompts; parses verdicts and plans back out. |
| `registry.py` | Which model/provider/tools/prompt each role (worker, validator, planner, summarizer) uses. |
| `runner.py` | The model-provider seam: Claude SDK, any OpenAI-compatible HTTP endpoint, or a scripted mock for tests. |
| `executor.py` | Runs your test command for real, sandboxed. |
| `vcs.py` | Gives each task workspace its own git history so a rejected/redone round is recoverable, not deleted. |
| `memory.py` / `retrieval.py` | Two-tier fact memory (project-specific -> promoted to project-wide) with relevance ranking. |
| `toolpolicy.py` | Lets an agent ask for a tool mid-task; read-only asks auto-approve, everything else queues for a human. |
| `eval.py` | Offline calibration: does the validator agree with a human on gold examples? Does the whole loop land the right final status? |
| `server.py` / `web/` | REST + SSE dashboard, reading the same SQLite store live. |
| `cli.py` | The `agentloop` command. |

## Big decisions, and the trade-off behind each

- **SQLite, not Postgres, and no runtime dependencies.** One file is the
  entire system's state. Trade-off: no true concurrent writers across
  machines — acceptable, because this is a single-operator loop, not a
  multi-tenant service, and the schema is written so a Postgres migration
  later is a lift-and-shift, not a rewrite.
- **A second, independent model reviews the work.** The validator is a
  separate LLM call — and can be pinned to a *different provider* than the
  worker (`AgentSpec.runner`). A model is a weak check on its own output; a
  different model, ideally a different vendor, catches more.
- **The validator's word isn't final — the executed test result is.** If the
  validator claims "tests pass" but the sandboxed run actually failed, the
  loop overrides it and logs the disagreement. The validator's job is
  judgment on things a test can't check; a test's job is facts.
- **Every task workspace is its own git repo, never a shared one.** Rejecting
  or redoing a round doesn't delete history — it rolls back to a ref, and the
  discarded commit stays reachable. Parallel workers can't collide on one
  repo's index because there is no one repo.
- **A human is a stop condition, not a rubber stamp.** Escalation is the
  *default* outcome of ambiguity: an unparseable verdict, a severe
  disagreement, a budget overrun, or risk_level ≥ 2 all go to a human before
  anything is marked done. The system is built to fail toward asking, not
  toward guessing.
- **Memory is two-tier and gated, not a vector database from day one.** Facts
  start scoped to one project, unapproved by default, and only get promoted
  to shared/loop-wide memory after enough distinct tasks actually use them.
  Ranking (`retrieval.py`) decides *order*, never *whether something gets
  read at all* — the approval gate is the only thing that does that.
- **Findings and the project charter are evidence, not gates.** A validator
  can list what it checked, and a project can set house rules, but neither
  one directly flips a task's status — only the approve/revise/escalate
  verdict does. This keeps the one decision rule the state machine has to
  honor simple and auditable, rather than several competing sources of truth.
- **A tool an agent doesn't have is something it can ask for, not something
  it's blocked on forever.** Read-only asks (file read, search) auto-approve;
  anything side-effecting queues for a human, and a task can keep its partial
  work while it waits.
- **The sandbox's threat model is "arbitrary AI-generated code," not "a
  hijacked command."** The test command is allowlisted config, never model
  output, run with `shell=False`, in a scrubbed environment. Where a real
  container/network-isolation tier isn't wired in, it degrades to
  env-scrubbing with a loud warning rather than pretending to be fully
  isolated — see "What this doesn't do" below.

## How this differs from other agentic-coding setups

| | agentloop | cc10x (this repo's own dev tooling) | A single always-on agent (AutoGPT-style, or one long Claude Code session) |
|---|---|---|---|
| Runs how | Standalone Python CLI + SQLite, headless/unattended | Skills + subagents *inside* an interactive Claude Code session | One model, one long-running loop |
| Reviews its own work? | No — a separate validator call, optionally a separate provider | Uses a code-reviewer subagent, but it's advisory and a human reads it | Usually not, or self-review by the same model |
| State lives where | One SQLite file, queryable, dashboarded | Markdown/JSON files under `.cc10x/`, scoped to a session | Wherever the harness keeps it — often nowhere durable |
| Cost/budget enforcement | Hard per-task cap, tracked per attempt including prompt-cache tokens | Not applicable (interactive, human paces it) | Rarely bounded |
| Recoverability of a rejected attempt | Git ref per round, nothing is destroyed | N/A | Usually just retried or lost |
| Parallelism | Task-graph-aware, atomic claims, `max_parallel_workers` | Sequential within a session | Rarely parallel-safe |
| Best for | Batches of well-scoped tasks you want ground out overnight or in CI, with an audit trail | One developer, one sitting, human-paced iterative build/review/plan | Open-ended exploration, prototypes |

The short version: cc10x (and similar Claude Code workflows) make *you* a
better-assisted developer in the room. agentloop is built to work *without*
you in the room — the validator, the budget cap, the escalation rules and the
git rollback exist because nobody is watching every step.

## What's good to know (the honest gotchas)

- **`--runner mock` doesn't prove the loop "worked."** It's a scripted test
  backend with no real model behind it — every task ends
  `needs_human — unparseable validator output` unless a fixture scripts
  otherwise. It's for exercising plumbing, not for seeing a task actually get
  approved.
- **Sandbox isolation, by default, is environment-scrubbing, not a real
  container.** `sandbox_isolation='strict'` *asks* for a container/no-network
  tier; if none is wired into your setup, it degrades (with a warning) to the
  env-scrub tier, where filesystem and network access are still technically
  open to the test command.
- **Coverage numbers on the dashboard are a display value, not evidence.**
  They're parsed from test output text a worker could in principle fabricate;
  no decision rule reads them.
- **Nobody in this project can verify what the web dashboard actually looks
  like rendered.** UI changes are checked against a seeded demo database and
  a written checklist by an actual human, every time — not claimed done by
  an agent reading the source.
- **A worker-created nested git repository is a known, documented gap.** Git
  can only track it as a bare reference; its contents don't survive a
  rollback. Named per-rollback (`unrecoverable_nested_repos`) rather than
  silently claiming full recovery.
- **No decision rule reads which provider ran a role, or whether the git/eval
  machinery is on.** The git/eval axis is *proven* — a test diffs full
  observable state with the feature on vs. off. The provider axis is checked
  by a narrower object-identity test, not that same full-state differential.

## Where to go next

- **Using it day to day** (every CLI command and config knob): [README.md](../README.md)
- **Contributing / changing behavior** (module-by-module internals, the
  current decision rules, conventions to follow): [CLAUDE.md](../CLAUDE.md)
- **Why a specific hardening pass exists, what it found, how it was fixed**:
  [docs/history/](history/)
