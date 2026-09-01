# agentloop

A general-purpose agentic development loop: worker agents execute tasks, an
independent validator reviews them against acceptance criteria, humans stay in
the loop at definition and review, and everything — state, metrics, audit
trail, memory — lives in one SQLite source of truth.

**Phase 1** (the vertical slice) and **Phase 2** (the live dashboard) are both
in. The full task lifecycle runs end-to-end with real, executed tests, and a
React dashboard streams the loop's state live from the same SQLite store the
loop writes to.

## Layout

```
agentloop/
  config.py    thresholds, budget caps, model pricing, sandbox + server knobs
  models.py    Task, PlannedTask, Verdict, TestResult, AgentSpec, statuses
  store.py     SQLite source of truth: tasks, task_deps (the task graph),
               attempts (metrics), verdicts, charter (append-only, versioned
               project rules), test_runs (incl. reported coverage),
               events (immutable audit log), two-tier memory
  registry.py  agent registry: role, model, prompt, tools, budget, version
  runner.py    ModelRunner seam: ClaudeSDKRunner | OpenAICompatRunner | MockRunner
  agents.py    worker/validator/planner prompt building, verdict + plan parsing
  executor.py  sandboxed test execution in a per-task workspace, and the
               coverage number parsed back out of its output
  vcs.py       per-task workspace git repos: init / commit / mark-approved /
               rollback — total, never-raising, and containment-guarded
  memory.py    two-tier memory policy: gating + auto-promotion
  retrieval.py RetrievalBackend seam: HashingBackend (stdlib bag-of-words)
  loop.py      orchestration state machine + planning + human decisions +
               mid-run control + parallel workers
  eval.py      evaluation harness: per-verdict validator calibration, and
               batch whole-loop runs measuring final status against gold
  server.py    REST + SSE dashboard backend (stdlib only)
  cli.py       add / plan / approve-plan / run / status / approve / reject /
               redo / pause / resume / abort / events / serve / memory /
               charter / eval
web/           Vite + React + TypeScript dashboard
tests/         876 tests on MockRunner + real subprocesses (no API keys needed)
```

## Quick start

```bash
pip install -e ".[dev]"          # + ".[claude]" for the real runner
pytest -q                        # verify the loop
```

Every `agentloop …` line below assumes the virtualenv is **activated** — the
console script is installed into it, not onto your PATH. Either activate it
(`.venv\Scripts\activate` on Windows, `source .venv/bin/activate` elsewhere) or
call it by path: `.venv\Scripts\agentloop.exe serve`.

```bash
agentloop add "Add slugify util" \
  --goal "Write slugify(text) in utils.py" \
  --criteria "Lowercase, hyphen-separated, handles unicode, has tests" \
  --risk 1

# ...or let a planner decompose a whole goal into a task graph
agentloop plan "Build a slugify library" \
  --criteria "Published, tested, documented"
agentloop approve-plan 1         # sign the plan off; its tasks may now run

agentloop run --runner claude    # or --runner openai
# `--runner mock` runs the loop with no provider and no cost, but it is a *test*
# backend with no script: every task ends `needs_human — unparseable validator
# output`. Useful for exercising plumbing, not for seeing the loop succeed.
agentloop status 1               # metrics: tokens, cost, wall time, verdicts
agentloop events 1               # immutable audit trail
agentloop approve 1              # human sign-off for escalated/high-risk tasks
agentloop redo 1                 # full redo: fresh start, no carried context
agentloop pause 1                # steer a running loop: pause / resume / abort
agentloop memory add k v --pinned --approved   # a fact that always injects
agentloop charter set --file RULES.md          # rules every agent prompt carries
agentloop tools list --pending   # tool requests awaiting a human (--task N for one task)
agentloop tools approve <id>     # grant a requested tool (applies to the next invocation)
agentloop tools reject <id>      # deny it — and every name sharing its capability
agentloop eval --runner mock     # validator calibration (mock, claude, or openai)
agentloop eval --mode batch      # whole-loop fixtures: does the loop still decide right?
```

### Dashboard (Phase 2)

```bash
cd web && npm install && npm run build   # once
agentloop serve                          # http://127.0.0.1:8765
```

**The dashboard is an unauthenticated mutation API, and slice 8 closed the hole
that made that dangerous from outside the machine.** Every request must now be
same-origin: a `POST` carrying a foreign `Origin` is refused 403, and so is any
request whose `Host` is a name that is not this server. Before that, any page
the operator happened to have open could drive it with a browser *simple
request* — measured, `POST /api/charter` from `Origin: http://evil.example`
returned 200 and replaced the charter, which is injected verbatim into every
worker, validator and planner prompt. A missing `Host` check made DNS rebinding
enough to *read* `/api/tasks` and `/api/config`.

`Origin: null` is refused like any other foreign origin, not treated as an
absent one: a browser sends the literal `null` for an *opaque* origin — from a
sandboxed iframe, and after any redirect chain that crossed origins — so it is a
cross-origin request that declines to name itself. An **absent** `Origin` is
still accepted, because only a browser sets one and a browser cannot omit it
cross-origin, so requiring it would break curl and the CLI to stop nothing.
Every refusal is recorded as a `dashboard_refused` event, so an attempt shows up
in `agentloop events` rather than nowhere.

What that does **not** cover, deliberately, and in the same register as the
env-scrub tier's residual risk: anything already running on this machine. There
is no token and no login. `--host 0.0.0.0` still works (an IP-literal `Host`
cannot be DNS-rebound), so serving to a LAN is still your decision to make and
still unauthenticated when you make it.

Task board, agent state, cost/token tiles, verdict history with the validator's
findings, executed test runs, a live audit feed, memory gating, the project
charter panel, tool-request approval panel (showing which capabilities the agents
are asking for, whether they auto-approve, and what approving or rejecting would
deliver), and approve/reject/redo — all reading the same store the loop writes to.
`npm run dev` proxies the API for hot reload.

## Decision rules (spec §4–§5)

Validator returns `VERDICT: <kind> CONFIDENCE: <0-1> TESTS: <pass|fail|na>`:

| Condition | Outcome |
|---|---|
| approve, confidence ≥ 0.70, tests not failing | done (or human sign-off if risk ≥ 2) |
| revise, or approve below threshold | revision with feedback, max 3 |
| escalate, or confidence < 0.40 | needs_human (severe disagreement) |
| worker replies `ESCALATE:` | needs_human (genuine ambiguity) |
| worker returns an empty output | needs_human — nothing to validate, and a `done` here would release dependents against output that does not exist; not a revise |
| budget cap exceeded | needs_human (never burn unbounded) |
| unparseable verdict | needs_human (never guess-approve) |
| transient infra failure (runner/executor raises) | retried with backoff, then needs_human (`infra_error`) — not a revise |
| worker context ≥ handoff ratio of its budget | summarize state + restart worker from the summary (`context_handoff`) — not a revise |
| planner replies `ESCALATE:` | needs_human (genuine ambiguity), no tasks created |
| plan unparseable / cyclic / dangling ref / oversized | needs_human ("Unusable plan"), no tasks created — never partially applied |
| task has an unfinished dependency, or an unapproved plan | not claimable; it waits as `pending` rather than failing |
| human approves a task that never ran (`pending`) | refused — approval signs off reviewed work, and `done` is what releases dependents |
| human approves a plan that produced no tasks | refused — a failed plan stays escalated with its diagnosis intact |
| agent requests a read-only tool | auto-approved and audited (`tool_auto_approved`); reaches the next invocation |
| agent requests a side-effecting tool (optional) | queued for a human (`tool_requested`); the task continues without the tool |
| agent requests a side-effecting tool (blocking) | needs_human (`tool_requested`), parking the task with partial output kept and revisions untouched |
| human approves a blocked tool request | task returned to `pending`, decision audited (`tool_request_decided`); the grant applies on next invocation |
| human rejects a tool request | decision audited (`tool_request_decided`); the capability and any sibling names resolving to it are withheld on next invocation |

**"Tests not failing" means the executed result.** Tests really run in the
task's workspace between worker and validator; the validator sees the real
output, and the gate consults the real status rather than the validator's
`TESTS:` claim. A validator claiming `pass` over an executed `fail` is logged
as a `test_disagreement` event — so a validator cannot approve past failing
tests, and its reliability is measured rather than assumed.

**Slice 6 adds no decision rule.** Nothing in the table above reads a commit
sha, a coverage percentage or a batch-evaluation result to decide a status: the
durability layer records what happened and the measurement layer reports a
number, and no threshold, revision count or budget check consults either. A
`VcsResult` is logged and discarded at every one of its call sites. This is
enforced rather than asserted — a committed test walks `loop.py`'s AST and fails
if a `vcs.*` result ever flows into a status decision — and it is why
`vcs_enabled=False` is a proven behavioral no-op: the loop takes the identical
transitions with the whole feature switched off.

The coverage number is the clearest example. When the test command's output
reports one, it is parsed back out of the output already captured (no second
subprocess) and stored on the test run, where the dashboard shows it. When it
reports none, the column is `NULL` and the dashboard shows **nothing** — `NULL`
means "no coverage was reported", never "0% coverage", and the parser resolves
every ambiguity toward `NULL` for the same reason: the text it reads includes
model-written output, so a wrong measurement is worse than an absent one. It is
a display value, not evidence, and nothing may promote it to evidence without a
different source.

What slice 6 *does* change is what those decisions leave on disk, which is a
behaviour change and not a rule change. **`reject` now rolls the workspace back**
to `refs/agentloop/base` instead of leaving the rejected round in place, and
**`redo` empties the workspace without destroying its history** — both rounds
stay reachable through a discarded ref. The status transitions are exactly the
ones the table already describes; see
[Workspace history and rollback](#workspace-history-and-rollback-slice-6).

### The validator shows its work

Under the verdict line the validator enumerates what it checked and what it
found, in a free-form `FINDINGS:` section — including on an approve, where "I
checked the unicode path and it was clean" is the useful record. It is stored on
the verdict row and shown under the verdict chips in the dashboard.

**Findings add evidence and change no outcome.** The decision table above gains
no row, because nothing decided changed: an approve at 0.92 with tests passing
is DONE whatever the findings say, and an empty findings section is recorded as
"no findings recorded" rather than treated as a reason to revise. A reviewer
that must always find something turns a legitimately clean review into a
revision loop; ours drives an automatic state transition rather than advising a
human who can filter it, so it does not get that trigger.

The section is soft on purpose. A missing or malformed one degrades to empty and
never fails the attempt, and the findings are *copied* out of the reasoning
rather than moved out of it — the reasoning is what the loop hands back to the
worker as revision feedback, and the findings are usually its most actionable
part. Where the section ends is ambiguous in prose, and the ambiguity is
resolved toward keeping too much rather than too little: a `# TODO` line quoted
inside a finding does not end it, only a real section heading after a blank line
does. Over-reading stores a little stray prose; under-reading destroys evidence.

Asking for findings means the `worker`, `validator` and `planner` system prompts
moved to version `"2"` for **every** project, chartered or not — the validator's
old instruction to judge strictly against the acceptance criteria told it to
disregard anything else. It is the only part of this that an unchartered project
notices.

All thresholds live in `LoopConfig` (`loopconfig.json`), agents in
`agents.json` (`agentloop init-registry`).

## Project charter

Project-wide rules, written by a human, injected into every worker, validator
and planner prompt:

```bash
agentloop charter set --file RULES.md --note "house style, agreed 2026-07"
agentloop charter show               # what is in effect
agentloop charter show --version 2   # what a past task actually ran under
agentloop charter history
agentloop charter clear --note "..." # explicit, audited removal
```

**Why it exists.** Acceptance criteria are per-task, so a rule that applies to
*every* task has nowhere to live. Two sibling tasks running in parallel have no
edge between them and therefore share no context at all — `## Upstream results`
only carries output along dependency edges. The charter is the one thing they
both see.

**What it does not fix.** It shares *rules*, not *decisions made in flight*. Two
siblings still cannot see that one named a function `to_slug` while the other
called it `slugify`. The charter collapses the class of conflicts to the ones a
human anticipated and wrote down; it does not eliminate the class.

**It is prompt content, not a gate.** Nothing in the loop reads it and no status
depends on it. A charter violation reaches the loop only as the validator's
ordinary `revise`/`escalate` verdict — which is also why the validator gets the
charter too: one that does not know the house rules cannot catch a breach of
them.

**Shape.** One row per *version* in an append-only `charter` table, so a past
attempt's version is still readable rather than merely identifiable, and every
attempt records the version it ran under (`agentloop status ID`, and a "Charter"
row in the dashboard's task detail, so "was this approved under the old rules?"
is answerable where the approve button is). Agents have no write path: the CLI
and the dashboard panel are the only two.

An empty or absent charter produces byte-for-byte the prompt the loop built
before the feature existed, and "cleared" is indistinguishable from "never set".
Oversize (over 4000 characters) is refused **loudly at write time** rather than
trimmed at inject time — a rule that silently falls off the end of a cap is
worse than no rule. A whitespace-only body is refused for the same reason;
clearing is its own audited operation.

**Cost.** The charter is rebuilt into every worker, validator and planner prompt
on every round: at the 4000-character cap that is roughly 1000 tokens per
invocation, counting toward `task_spend` and therefore toward the budget cap
exactly as memory does. On a chartered project a tripped cap is not a mystery.

## Memory (spec §7)

Two tiers, `project` and `loop`. Reads are gated on `approved`: a fact nobody
vetted never reaches a prompt, because a bad fact entering memory quietly
poisons every later task. Approval is approval **of a value** — rewriting a key
with different content returns it to unapproved, so an agent cannot smuggle new
content past the gate by overwriting a key a human already accepted. A rewrite
that changes nothing keeps its approval, and writing with `--approved` is
approving the incoming content. Agent writes land unapproved and surface in the
dashboard (or `agentloop memory`) for a human to accept or discard. A project
fact that turns up relevant to `memory_promote_threshold` tasks is promoted to
the loop tier — re-answering the same question is exactly the wasted spend
tiering removes.

Both words in "relevant to N tasks" are load-bearing.

**Relevant**, not merely injected: a hit is an injection the retrieval ranked
above zero. While the store holds fewer facts than the cap every approved fact
is injected into every prompt, so counting injections would mean "existed while
three tasks ran" and promote the whole project tier on schedule.

**Tasks**, not prompts: a worker round, a validator round and one revision are
three injections of the same fact inside a *single* task, so counting prompts
promoted a fact on the strength of one ordinary task. Hits are recorded per task
in `memory_hits`, and `hit_count` is the size of that set — which also makes
"which tasks did this fact serve?" answerable, so a promotion can be checked
rather than trusted. A read with no task behind it (`agentloop memory`, `eval`)
records recency but no hit.

It is still a proxy — what promotion wants to know is whether a fact changed the
output, which the `retrieval` provenance now makes measurable.

**Promotion is a transition, not a copy.** The row *moves* to the `loop` tier,
keeping its id, approval, pin and hit history. Nothing is duplicated, so there
is no second row to inject alongside the first, none to re-promote forever, and
none left approved after the original is revoked.

Where a `loop` row already holds the key, only that row's *id* survives: the
promoted value replaces its contents and the two facts' hits are merged. That is
a winner being picked, so the displaced value is written into the
`memory_promoted` event — nothing else in the audit log records a memory value,
and it must stay recoverable. Approval then follows the approval-of-value rule,
applied to *whichever value survived*: it stays approved only if a human
approved that exact text. Two approved rows with different values are a genuine
conflict, so that case drops to unapproved for a human to resolve. Any drop from
approved logs `memory_revoked`, whichever row held the approval — a fact that
stops being injected says so in the feed rather than leaving a bare
`memory_write` to infer it from.

Opening an older database merges the duplicate rows the old copy-promotion left
behind, and resets `hit_count` — the old counter counted prompts, and carrying
those numbers into a threshold that now means tasks would promote most of the
project tier on the first prompt after the upgrade. That merge differs from the
live one in two ways, because it repairs an existing database rather than
recording a fact that won on merit: an **approved** loop value outranks an
unapproved project value (otherwise opening the database would overwrite vetted
content with the rewrite that un-approved it), and it logs
`memory_duplicates_merged` rather than `memory_promoted` — nothing was promoted
here, and reusing the event would make anyone counting promotions in the feed
count database opens instead.

**Pinned facts.** Prompt injection is capped (20 facts) so memory can't crowd
out the task; past the cap, ordinary facts drop by alphabetical accident. Mark
a must-have fact `--pinned` (or pin it in the dashboard) and it sorts first and
bypasses the cap under its own smaller ceiling (10). Pinning does not bypass
approval — a pinned but unvetted fact still never reaches a prompt.

### Retrieval: the facts about *this* task

Which facts survive that cap used to be decided by alphabetical order, which
has nothing to do with the work in hand. Facts are now **ranked by relevance to
the task** — title, goal, criteria, plus the validator feedback (or the output
under review) that makes a revision retrieve differently from a first attempt.

Ranking sits behind the `RetrievalBackend` seam in `retrieval.py`, the same
shape as the `ModelRunner` seam.

One backend ships: **`HashingBackend`** (the default, stdlib only) — hashed
bag-of-words vectors and cosine similarity, brute-forced over the candidates.
It matches on shared vocabulary rather than paraphrase; that's the price of
zero dependencies, and it still beats the alphabet.

The seam is the point, not the backend count. A real embedding model is a
drop-in `search()`, and that is the change to make the day ranking needs to
understand paraphrase. A Chroma-index backend shipped here briefly and was
removed: it embedded with the same `embed()` as the stdlib backend, so it
returned the same order at any fact count this store holds — an optional,
CI-untested dependency buying nothing. An unknown backend name now raises
rather than degrading to a working default: which ranking ran is part of how a
run behaved.

Ranking decides *order*; it never widens what may be injected. The candidate
set is what `approved` already allowed through, so no backend — local, remote,
or not yet written — can surface an unvetted fact; any future index is a
derived cache that may only re-rank rows the store just handed it, never
resurrect a revoked one. The caps and the pinned ceiling are unchanged, and a
fact that fits under the cap is never dropped for scoring low — a zero score
costs it a promotion credit, not its slot. That holds even for a backend that
returns fewer candidates than it was given, which is the normal shape of a real
index: whatever it ignores is appended behind what it ranked, in the store's own
order, so a group of N always yields min(N, cap). With no query, or
`memory_retrieval_backend: "none"`, selection falls back to exactly the old
alphabetical behaviour — there is nothing to rank against, so inventing an
order would be worse than the plain one.

### Provenance: what memory said, and what agents did

The audit log recorded what *entered* memory but never what was read out of it,
so a bad answer couldn't be traced to the fact that caused it. Two event kinds
close that:

- **`retrieval`** — query, backend, candidate count, and every injected fact
  with its id, tier, pin state and score, attributed to the attempt that used
  them (`attempt_id`, `agent_kind`, `role`). A task retrieves once per agent per
  round and the worker and validator rank against different queries, so without
  that attribution the rows are indistinguishable — and `events` is append-only,
  so there is no backfilling the ones already written.
- **`tool_call`** — one row per tool an agent actually invoked (registry tools
  already reach the SDK, so this was happening unrecorded), attributed to the
  attempt that made it. Slice 5's auto-approval policy layers on this record
  rather than inventing authorization and provenance at once. The tool's
  arguments are recorded as a coerced, truncated string: telemetry must never be
  the thing that fails an attempt, and a value the JSON encoder chokes on used
  to roll back the finished attempt — output, tokens and cost — of a model call
  that had already been paid for.

## Token & cost accounting

Usage is read from the SDK's terminal `ResultMessage` only (it already carries
the whole-run total, so summing per-message double-counts) and captures all
four fields: new input, output, and **prompt-cache** writes and reads. Cache is
priced off the input rate — writes ×1.25, reads ×0.10 — and counts toward both
the cost and token budget caps. This matters: a cached run can report 2 new
input tokens against ~21,000 in cache, so the pre-fix cap, blind to cache,
measured almost nothing. `estimate_cost_usd`'s cache arguments default to 0, so
the zero-priced `MockRunner` stays free.

## Mid-run human control

A running task can be steered between iterations without killing the process:
`agentloop pause|resume|abort <id>`, or the buttons in the dashboard. The signal
is written through the store, so it works cross-process and is read fresh at
each loop boundary (where the budget cap is checked). A paused task survives a
restart and does not auto-resume; an aborted task is terminal but defensible —
its output and full audit trail are left intact. Every transition is an event.

## Context-budget handoff

An agent's `context_budget_tokens` (in the registry) used to be unenforced, so a
long revision chain could silently pile transcript on transcript until the
context overflowed. Now the loop watches how much context the **worker** has
accumulated on a task (summed across its attempts, prompt-cache tokens included),
and once that reaches `context_handoff_ratio` of the worker's budget (default
`0.70`), it **hands off**: a dedicated `summarizer` agent compacts the working
state — goal, criteria, work so far, and the latest validator feedback — and the
worker is restarted from that summary *in place of* the raw transcript, which is
exactly what would have overflowed.

The check sits at the iteration boundary next to the budget cap, and a handoff is
**not a revision** — it doesn't consume `max_revisions`. Each handoff logs a
`context_handoff` event with before/after token counts, and the summarizer runs
as its own recorded attempt (its cost still counts toward the task budget cap).
After a handoff the watermark advances, so only newly accumulated context can
trip the next one.

## Planner, task graph, and parallel workers

A goal bigger than one task used to have to be split by hand. `agentloop plan`
hands it to a **`planner` agent**, which returns a small graph: independently
executable tasks plus the dependencies between them. The plan itself is a task
row of `kind='plan'` — it owns the planner's attempt and audit trail, and it is
never claimed by the loop, because a goal statement is not work.

**A bad plan never becomes tasks.** An unparseable reply, a cycle, a
`depends_on` naming a task that isn't in the plan, or a plan over
`max_plan_tasks` all end the same way: the plan row escalates to `needs_human`
and **zero** child tasks exist. Nothing is repaired, truncated, or partially
applied — a half-applied decomposition is worse than none, because the missing
half is invisible while the half that landed looks like a complete plan somebody
approved. A planner that hits genuine ambiguity replies `ESCALATE:` and asks,
exactly as a worker does.

**Plans are gated by default.** A planner generating tasks *is* task definition,
which humans stay in the loop for, so `plan_requires_approval` (default `true`)
holds a plan's tasks until `agentloop approve-plan <id>` — or the dashboard's
approve button on the plan row, which routes to the same place rather than
marking the goal "done" while its tasks stay blocked forever. Set it `false` for
autonomous batch runs; each child's validator round is still the gate on output.

### Blocked is a predicate, not a status

A task waiting on an unfinished dependency, or on an unapproved plan, stays an
ordinary `pending` row. Claimability is decided inside the store's atomic claim:
a task is handed out only when every edge points at a `done` task and its plan
is signed off. That has three consequences worth stating:

- **Nothing has to be un-set when a blocker clears.** Approve the plan, or
  resolve the dependency, and the next run claims what it unblocked.
- **A dependent of an escalated task is skipped, not failed.** The batch
  finishes everything else and returns; the dependent is still there, unspent.
- **One worker and many behave identically**, because neither holds a schedule
  in memory — they ask the same question of the same committed state.

Cycles are refused at the edge (`Store.add_dependency`), not just by the plan
parser. A cycle isn't a slow graph, it's a permanent deadlock: every task in it
waits on another, so none is ever claimable and the loop drains to a silent
stall. Keeping the check in the store makes "the graph is a DAG" an invariant
rather than a hope about the planner.

Dependency edges also carry data, not just order: a dependent's worker prompt
includes an **`## Upstream results`** block with the output of the tasks it
waited for (bounded per upstream task). The reason "write the tests" waits for
"write slugify()" is that it needs to see slugify(); an edge that only delays a
task is a schedule, not a plan.

### Parallel workers

`max_parallel_workers` (default **1**) is how many tasks may run at once. At 1
the loop is the sequential loop unchanged — one claim id, one thread, identical
ordering. Above 1, that many threads each claim independently, so only tasks
with no unfinished dependency ever run together, and the atomic claim means two
workers can never hold the same row.

A worker that finds nothing claimable **waits while any peer is still busy**
rather than exiting. Exiting would still be correct — the last thread standing
eventually claims whatever it unblocked — but it would not be parallel: a plan
usually has a single root, so every other worker would find nothing on the first
pass, exit, and leave the whole rest of the graph to run serially. The wait ends
when no peer is busy, since then nothing can become claimable.

The cap is deliberate rather than "run everything ready": it bounds simultaneous
model spend and concurrent sandboxed test subprocesses. Per-task audit isolation
is unaffected — each task still gets its own claim, attempts, verdicts and
events. Note that with parallelism on, per-task wall times no longer sum to the
run's wall time, and audit events from different tasks interleave in the log
(they remain totally ordered by id, and each carries its `task_id`).

The claim is a **compare-and-swap** (`WHERE status='pending' AND claimed_by IS
NULL`), not just a lock-protected update. Within one process the connection lock
would suffice, but two `agentloop run` processes hold separate connections and
separate locks, and Python's sqlite3 does not open a write transaction for a
`SELECT` — so both could read the same pending row. The guard makes the loser's
update match zero rows and it moves on to the next candidate.

**Shrinking the pool strands claims.** `claim_next_task` only re-offers in-flight
work to its exact owner, so retiring a claim id (going from 4 workers back to 1)
hides whatever those ids held from every claimer. That is reported rather than
repaired: each orphan logs a `claim_stranded` event, and `agentloop redo <id>`
recovers it by clearing the lease (a **live bug in shipped `main`** was that nothing
ever cleared `tasks.claimed_by`, so `redo` and `resume` left the lease set, the
CAS (`WHERE claimed_by IS NULL`) failed on that row forever, and it **starved every
pending task behind it**; `Store.release_claim` is the sole writer of lease
releases and makes that promise true). It is deliberately not auto-reclaimed — with
the default worker id, a second `agentloop run` process is indistinguishable from
a retired worker, and stealing its live task is worse than leaving one stranded. A
real lease with an expiry is the proper fix and belongs with the durability slice.

## Validator calibration harness

The decision rules lean on the validator's `CONFIDENCE` number, so `agentloop
eval` measures whether it's calibrated. It runs ~20 fixtures with known-correct
verdicts through `run_validator` and reports agreement rate, an
approve/revise/escalate confusion matrix, and a confidence-vs-correctness
calibration table (buckets straddling the 0.40/0.70 thresholds). `--runner mock`
is deterministic and runs in CI to exercise the harness mechanics; `--runner
claude` and `--runner openai` (both opt-in, skipped when their respective
`ANTHROPIC_API_KEY` or `OPENAI_API_KEY` is unset) produce genuine calibration
measurements. Results persist to the `eval_runs` table.

**Two modes, one table.** `--mode verdict` (the default) is the calibration
above: fixtures go through `run_validator` and *agreement* means "the validator
returned the gold verdict kind". `--mode batch` (slice 6) is a whole-loop
evaluation — each of its 9 fixtures is driven through a real `Loop` and
*agreement* means "the task reached the gold `TaskStatus`", so it measures the
decision rules end to end rather than one agent's judgement. The rows live in
the same `eval_runs` table with a `kind` column (`'verdict'` | `'batch'`)
discriminating them; pre-slice-6 rows default to `'verdict'`, which is not a
placeholder but the correct value, since per-verdict calibration was the only
harness that existed when they were written. **Batch mode is mock-only and
refuses a non-mock runner loudly**: every fixture is a scripted `MockRunner`
with test execution and git off, and a batch run that reached a live provider
would be measuring the model, not the rules.

## Agent-requested tools (Slice 5)

An agent may request a tool at run time by writing a `TOOL_REQUEST: <tool>
(blocking|optional) - reason` marker in its output. **Read-only requests are
auto-approved** (e.g., `file_read`, `search`, `task_state`), logged as events and
reaching the next invocation. **Side-effecting requests queue for human approval**.

- **Optional requests** — the agent can proceed without the tool; the task
  continues running and the request sits in the queue until a human approves,
  rejects or closes the task.
- **Blocking requests** — the agent cannot finish without the tool; the task
  parks at `needs_human`, keeping its partial output and revision count. A human
  can then:
  - **Approve** — the task returns to `pending` with the row, workspace and audit
    trail intact, not a redo (which would throw away the work done so far).
    The grant applies on the next invocation.
  - **Reject** — the capability is withheld. On the next invocation, the tool
    (and any other logical name resolving to the same capability) is removed
    from the allowed list. This is **fail-closed**: a gate a human believes is
    shut must be shut. If rejecting also disables other tools the role holds
    (e.g., rejecting `shell` disables `git` because both resolve to the Bash
    capability), that side effect is audited (`tool_capability_withheld`) so a
    human knows what else stopped working.
  - **Redo** — the task starts fresh from its original goal (no carried context,
    revision count reset, workspace wiped) and the request stays **undecided**.

  Only **approve** releases a parked task. The other routes look symmetrical and
  are not: rejecting records the denial and leaves the task parked, and `redo` or
  `pause`+`resume` return it to the queue with the blocking request still
  standing — so the next round pays for a worker call and parks on the same
  request. `pause`+`resume` is neutral about *state* (nothing is decided, the
  output and revision count survive), which is not the same as being a way
  forward.

The gate is enforced in `agents.run_worker`, `agents.run_validator` and
`agents.run_planner` before the model call, and is therefore invisible when
both `gate_declared_tools` is off (the default) and the agent writes no marker.

**Configuration:** `tool_readonly_allowlist` names the read-only tools
(`["file_read", "search", "task_state"]` by default; `web` is deliberately
absent). `gate_declared_tools` (default False) applies the gate to tools
declared in `agents.json` when the loop runs. `max_tool_requests_per_task` (10)
bounds the queue by counting **pending** rows only; an ask over the cap is
`refused` and audited rather than queued. A **blocking** ask is exempt from the
cap — it is the one an agent says it cannot finish without, so letting the cap
silently drop it would turn "I need this" into "continue without it and tell no
human". A machine `refused` row is **not** a human denial and withholds nothing:
nobody was shown a closed gate, and letting refusals subtract would let an
agent's own chattiness strip its role's shipped baseline with no human involved.

**Limitations and residual risks:**

- **The two capability-granting POST routes are unauthenticated.** They inherit
  the existing memory-approve pattern and the server binds `127.0.0.1` by
  default, so as shipped this is not exposed — but serving on `0.0.0.0` now hands
  a LAN peer a permission-**grant** button rather than a memory-approval one.
- A marker inside a **fenced code block is a live request**: the parser does not
  understand markdown fences, and teaching it to would mean a second, lossy model
  of the reply. A quoted `(blocking)` example will therefore stall a task for a
  human. Stated here rather than discovered.
- The guarantee is over the `tools` list **passed** to the runner, not over what a
  provider honours. A backend that ignores the argument cannot be checked from
  this seam; `OpenAICompatRunner` drops tools with a `RuntimeWarning` because a
  chat-completions call has no tool-execution loop.
- Every request is a request for a *logical* name, and the gate subtracts the
  *concrete* capability, so denial is coarser than the name: see **Reject** above.

## Sandboxing

This runs **arbitrary, AI-generated code** — whatever the worker wrote, invoked
by the test command. So command hijack is the *lesser* worry; the code the
command runs is the real exposure. Defenses are layered:

- **Command**: allowlisted in config, never taken from model output; split into
  argv and run without a shell (`pytest -q; rm -rf /` is literal argv, not
  operators); cwd pinned to `.agentloop/ws/task-{id}/`; timeout + output cap.
  A redo *empties* the workspace, so "fresh start" means it — and where the
  workspace is a git repo (slice 6, below) emptying it is not destroying it:
  the discarded round stays reachable from a ref.
- **Environment**: the child gets a **scrubbed, allowlisted env** — the parent
  environment (which holds `ANTHROPIC_API_KEY` and every other secret) is never
  passed wholesale, so generated code can't read credentials from it. Extra
  vars a project genuinely needs go in `sandbox_env_allowlist`.
- **Resource bounds are real bounds** (slice 8). Output is read into a bounded
  ring buffer as it arrives rather than captured whole and truncated
  afterwards — a child was measured writing 331 MB in 4 s into the
  orchestrator's heap, extrapolating to ~9.8 GB at the default timeout, to
  store a 4000-character tail. And the timeout now kills the whole **process
  tree** (`taskkill /F /T` on Windows, a POSIX process group elsewhere) instead
  of only the direct
  child: a surviving grandchild holding the inherited stdout pipe was measured
  defeating a 3 s timeout for 20.3 s, and one that never exits blocked the loop
  indefinitely while holding the task claim. Both were promises the module
  docstring already made. **Residual:** `taskkill /T` walks the live
  parent-PID chain, so a grandchild whose intermediate parent has already
  exited is reparented and is not reached — a Windows Job object would close
  that and this does not. Named here rather than designed away, in the register
  of the env-scrub tier's residual risk.
- **The sandbox can find the interpreter's own tools** (slice 8). The child
  `PATH` gets the running interpreter's script directory prepended, because the
  default `test_command` is `pytest -q` and invoking `agentloop` by path (which
  the quick start offers) left the venv's `Scripts` off `PATH`. That failure was
  quiet in the worst way: `status="error"` is not `"fail"`, so the tests gate
  fell back to the validator's own `TESTS:` claim — the exact thing executed
  tests exist to replace.
- **Isolation tier** (`sandbox_isolation`): `env` (default) is env-scrub only.
  `strict` asks for a container / no-network / read-only-fs tier when a backend
  is available and **degrades to env-scrub with a warning** when it is not.
  **Residual risk of the env-scrub tier:** generated code still has this
  process's filesystem write access (absolute / `..` paths escape the
  workspace) and network access — only the environment is contained. Run under
  `strict` with a real backend, or an external sandbox, for untrusted code.
- **Turning test execution off is not the end of the question.**
  `allow_test_exec=False` means agentloop runs nothing the agent wrote — but the
  agent's *files* are still handled by other programs afterwards, and the one
  agentloop itself runs every round is `git` (slice 6, below, on by default).
  Git takes program names from `.git/config`, which lives inside the workspace
  the agent writes to, so the durability feature would have handed back exactly
  the execution this switch refuses. That is what the **config pin** in
  "Containment" below is protecting, and why it refuses rather than repairs.

## Workspace history and rollback (Slice 6)

Before slice 6, a `reject` left the rejected round sitting in the workspace and
a `redo` deleted it outright. Both are now recoverable: **every task workspace
is its own throwaway git repository**, `.agentloop/ws/task-{id}/`, created by
the loop and never seen by a human unless they go looking.

**One repo per task, never a shared timeline.** Two parallel workers can never
collide on a git index because they never share one, and a task's history is
disposable exactly as its workspace is. The cost is the honest half of that
trade: there is no cross-task branch to diff, no single log of "what the loop
did today", and nothing here is ever pushed anywhere. If you want a project-wide
timeline, this is not it.

**Three refs, and what each one means:**

| Ref | Written when | Means |
|---|---|---|
| `refs/agentloop/base` | the repo is initialised, at the first round | the empty starting point a rollback returns to |
| `refs/agentloop/approved` | the task reaches `done`, by the loop or by a human | the tip a human (or the decision rules) signed off |
| `refs/agentloop/discarded/<sha>` | **before** any rollback moves anything | the tip that was about to be discarded |

Each worker round commits (`round 1`, `round 2`, …) on top. A rollback resets to
`base` and cleans the tree — but it writes the discarded ref *first*, which is
the whole mechanism. **A plain `git reset --hard` orphans the commits it moves
off**: `git show <sha>` still resolves them, so a check written against `git
show` passes while the work has in fact fallen out of `git log --all` and out of
every ref. Recoverability is therefore asserted on **reachability**, never on
`git show`. To find rolled-back work:

```bash
cd .agentloop/ws/task-7
git log --all --oneline                 # every round, including discarded ones
git for-each-ref refs/agentloop/         # base, approved, and each discarded tip
git show refs/agentloop/discarded/<sha>  # the discarded round itself
git checkout -b recovered <sha>          # get it back into a working tree
```

The round commit uses `git add -A -f`, force-adding **ignored** files. That is
deliberate: the rollback's `git clean -ffdqx` deletes ignored files too, so
anything `add` skipped would be destroyed with nothing to recover it from. The
price is that a worker-generated, `.gitignore`d build directory is committed
every round.

**Containment: the guard, and why it is not paranoia.** In a directory that is
not a repository, `git rev-parse --show-toplevel` walks *up* — inside
`.agentloop/ws/task-7/` with no `.git`, it answers with **this project's own
repository**. An unguarded `git add -A && git commit` there would stage and
commit your entire working tree, and an unguarded `reset --hard`/`clean` would
destroy it. So no side-effecting git command runs unless **all five** of these
hold:

1. `<workspace>/.git` is a directory;
2. git's own toplevel for that cwd *is* the workspace;
3. the resolved workspace lies inside `workspace_root`;
4. the resolved `<workspace>/.git` lies inside `workspace_root`;
5. and git's own answer to `rev-parse --show-toplevel --absolute-git-dir
   --git-common-dir` puts **both** resolved git locations at that same one
   place.

The fourth is not a restatement of the third. Git derives the worktree from the
directory *containing* `.git`, not from where `.git` resolves to, so an NTFS
junction (or symlink) at `<workspace>/.git` pointing at your real repository
satisfies the first three — measured, with the victim repository's branch
pointer rewritten off its own work.

**Condition 5 is identity, not containment**, and that distinction is the whole
of it. `workspace_root` is the parent of *every* task workspace, so
"contained in `workspace_root`" was satisfied by two things it should never
have been: a directory the worker created *inside its own workspace* (git reads
`config` from the **common dir**, so pointing `.git/commondir` there hands the
worker the config git actually parses while `<workspace>/.git/config` — the
file the pin below fingerprints — stays byte-identical: measured, pin
unchanged, guard True, `commit` ok=True, canary written outside the workspace);
and **another task's workspace**, which aimed one task's `reset --hard` /
`clean -ffdqx` at a sibling task's repository. A fingerprint pins a *value*, and
it pins the *decision* only if the file it hashes is provably the file the
consumer reads.

**The fifth is why the guard asks git instead of testing paths, and it is the
one to leave alone.** Conditions 1–4 all inspect *filesystem paths*, which is a
guess at what git will make of them — and `.git` sits inside the workspace the
agent writes to. Git resolves its ref and object store from `.git/commondir`, so
a single ordinary file write there (no junction, no gitfile, no environment
variable) redirected every ref and object write into the operator's repository
while all four path conditions held: measured, guard True, `commit` ok=True,
victim's branch pointer rewritten. A fifth *path* check would have been the same
mistake one level deeper, so the guard reads the location git actually
resolved — which closes commondir, gitfiles, `GIT_DIR`, worktree links and the
next redirect at once. Any error while computing any condition refuses: a guard
that errors is a guard that says no.

**The config pin: `.git/config` is inside the workspace too.** The five
conditions above all answer *where git points*. They say nothing about *what git
runs* — and git config names programs git executes: `filter.<name>.clean` on
every `add`, `core.fsmonitor` on every index refresh, among others. A worker
writing only inside its own workspace could therefore set one and have the next
round's commit run it. Measured, and measured on the configuration where it
matters most: `allow_test_exec=False` (your explicit *"execute nothing the agent
wrote"* switch) with `vcs_enabled=True` (the default), from a call the loop makes
every round and from `reject`/`redo` on the dashboard.

A denylist cannot close this — a filter name is arbitrary, so there is no key to
enumerate. So the config is **pinned**, which is an allowlist of exactly one
value:

- `init_repo` fingerprints the `.git/config` that `git init` itself wrote, at
  the moment it wrote it;
- the fingerprint is stored in **the database** (`vcs_pins`), the one place in
  the system an agent has no write path to — a pin kept under `.git/` or
  anywhere in the workspace would be as writable as the attack it catches;
- every later side-effecting call replays it and refuses on any mismatch,
  unreadable config or missing pin (`config-changed` / `config-unpinned`),
  *before spawning anything*: it is one bounded read of a **regular** file and
  a hash. Regular, because `stat()` reports `st_size == 0` for a FIFO, so a
  size bound alone would pass one and then block on the read forever — with no
  timeout, unlike every git spawn here.

The pin only pins a *decision* because condition 5 above holds: it fingerprints
`<workspace>/.git/config`, and git reads `config` from the **common dir**, so
without identity a redirected common dir left the pinned file untouched and the
parsed one attacker-owned. A fingerprint pins a value; anchoring the location is
what makes it pin the choice.

An existing repository is never re-pinned — only the branch that *creates* one
mints a pin — because re-pinning would bless a worker's edit on the next round
and turn the gate into the attacker's signature. Git was measured never to
rewrite `.git/config` during any command agentloop issues (`init`, `add`,
`commit`, `update-ref`, `status`, `rev-parse`, `reset --hard`, `clean`), so a
changed config is always somebody else's. When the pin refuses, durability
degrades exactly as a missing git binary does: a `vcs_unavailable` event and a
`RuntimeWarning`, no change to any status transition, and the task runs on.

**Your *own* git config is never read and never written.** agentloop passes its commit
identity per invocation (`-c user.name=… -c user.email=…`) and never runs `git
config --global`. It also neutralises your config in both directions it can act:
`GIT_CONFIG_GLOBAL=os.devnull` in the child environment, plus `-c
commit.gpgsign=false -c core.hooksPath=` on every invocation and `-c
init.templateDir=` on `init`. Both, not one — the environment variable needs git
≥ 2.32 and the `-c` pins carry the guarantee below that floor. This is not
theoretical tidiness: `GIT_CONFIG_NOSYSTEM` alone leaves `~/.gitconfig` live,
and an ordinary `commit.gpgsign=true` was measured to fail *every* commit, while
a `core.hooksPath` pre-commit hook ran inside the workspace and wrote a file
outside it.

**Events.** Three kinds reach `agentloop events` and the live feed:
`vcs_commit` (a round snapshot or the approved-ref move), `vcs_rollback` (which
ref, how many files were removed, and the discarded sha when there was one), and
`vcs_unavailable` (git missing, timed out, refused by the containment guard or
the config pin, or degraded).
Durability never fails an attempt: every entry point returns a result instead of
raising, and a failure is logged and stepped over.

**Knobs** (`loopconfig.json`):

| Knob | Default | Meaning |
|---|---|---|
| `vcs_enabled` | `true` | Off is a proven behavioral no-op — the loop takes identical transitions, and no repo is created. |
| `vcs_command` | `"git"` | An executable **path**, not a command line. Deliberately unlike `test_command`: `"git --no-pager"` is treated as one executable name and fails as missing. |
| `vcs_timeout_s` | `30` | Per git invocation. A timeout degrades the feature; it never hangs and never raises. |

**A pre-slice-6 workspace has no `.git`.** Nothing migrates it: the
guard refuses, `reject` leaves it exactly as it was (today's behaviour), and
`redo` falls back to wiping it. The next `run_task` initialises a repo, so the
workspace becomes durable from its next round onward rather than retroactively.

**Residuals — stated, not designed away**, in the same register as the env-scrub
tier's residual risk above:

- **Your test command now runs inside a git repository**, where before it did
  not. Ignore-aware linters, coverage source discovery, `git ls-files`-based
  collectors and repo-local hooks can all behave differently as a result. No
  automated proof of non-interference is possible here — the inertness
  differential runs with test execution off and structurally cannot see this —
  so it is documented rather than claimed. `vcs_enabled=false` turns it off.
- **A nested repository's objects are unrecoverable after a rollback.** If a
  worker runs `git init` inside its own workspace, the round commit can only
  record that directory as a bare gitlink, so its contents are not carried into
  the discarded ref and `clean -ffdqx` deletes them for good. Git cannot nest
  repositories this way and no flag changes it. What agentloop does instead is
  refuse to overclaim: the `vcs_rollback` event carries
  `unrecoverable_nested_repos` naming every such directory, so the audit log
  never asserts a recovery surface that does not hold the work.
- **`redo` and `reject` are reachable mid-round from the dashboard** and take no
  claim, so a rollback can race a running round's commit on one workspace. The
  window is TOCTOU between the guard's `rev-parse` and the destructive command.
  Both racers target the *same* workspace, so the outcome is a lost commit or an
  `index.lock` surfacing as a `vcs_unavailable` event — not an escape of
  containment. Accepted and documented; not defended.
- **Discarded refs accumulate.** Nothing prunes `refs/agentloop/discarded/*`,
  deliberately: they *are* the recovery surface, and the workspace is disposable.
  An operator counting refs after a dozen rejections should know why there are a
  dozen.

## Provider seam

The loop only knows the `ModelRunner` protocol. `ClaudeSDKRunner` is the
default backend; `OpenAICompatRunner` (slice 4) brings a second provider into
the same architecture, so a validator can review a worker's output on a
different model family — a cross-validator for independent review. Which
backend serves which role is a registry decision (`AgentSpec.runner`), so you
can pin the validator to OpenAI while the worker stays on Claude, ensuring a
model does not rubber-stamp its own output. This keeps the project
open-sourceable and multi-provider. Your code, your license; SDK users bring
their own credentials.

**Configuration:** `OpenAICompatRunner` works with any OpenAI-compatible
endpoint — OpenAI, Azure, OpenRouter, vLLM, or a local endpoint. The API key
is read from `OPENAI_API_KEY` (required to run) and the base URL from
`OPENAI_BASE_URL` (defaults to OpenAI's public endpoint). Usage is parsed and
pricing is applied per model, including provider-specific cache pricing (OpenAI
caches automatically, Anthropic charges write tokens). An unpinned run is
behaviorally identical to the pre-slice-4 loop — every role uses the loop's
default runner when nothing is pinned.

**Pinning a role**, in `agents.json` (`agentloop init-registry` writes the
defaults; the `runner` key is optional and absent means "the loop's default"):

```jsonc
{
  "worker":    { "role": "worker",    "model": "claude-sonnet-5",
                 /* system_prompt, tools, … as written by init-registry */ },
  "validator": { "role": "validator", "model": "gpt-5-mini",
                 "runner": "openai",  /* ← the only added key */ }
}
```

The worker keeps writing files on Claude; the validator reviews its output on
another family. `model` and `runner` travel together deliberately — a
`claude-sonnet-5` string means nothing to an OpenAI endpoint, so the pair is
validated and a mismatch is refused rather than sent.

**Limitations:** The OpenAI-compatible backend cannot execute tools — a
chat-completions call has no execution loop — so a role pinned to it reviews
what is in its prompt rather than reading the workspace. This is why the role
to pin is the **validator** (whose prompt carries worker output, test results,
charter and memory) and not the worker (whose job is writing files).

Pricing rows are point-in-time list rates, kept in `MODEL_PRICING`. A model
with no row prices at `DEFAULT_PRICING` rather than failing, so a stale or
missing rate is a wrong cost, not a crash — check them when the numbers start
to matter. Dated snapshot ids (`gpt-4o-mini-2024-07-18`, which is what the API
echoes back) are normalized to their family before the lookup, so the rate you
listed is the rate you are charged at.

**Config errors fail fast:** A missing or revoked API key, a non-https base URL,
a model the endpoint does not serve, or a `claude-*` model pinned to the OpenAI
backend (which would 404) escalate to `NEEDS_HUMAN` without retry, naming the
problem. That's different from transient HTTP errors (408, 429, 5xx), which retry.

## Roadmap (from the seed spec)

- [x] Task lifecycle: define → execute → validate → approve/revise/escalate
- [x] Bounded retries, severe-disagreement tier, human escalation
- [x] Metrics per attempt (tokens, cost, wall time), budget caps
- [x] Immutable audit log; resumable loop; two-tier gated memory
- [x] Real test execution in a sandboxed per-task workspace, feeding the
      validator; registry tools passed through to the SDK
- [x] Memory wired into prompts, with gating and auto-promotion
- [x] Phase 2: live dashboard (REST + SSE) over the same store
- [x] Accurate token/cost accounting incl. prompt cache, feeding the budget cap
- [x] Validator calibration harness (`agentloop eval`)
- [x] Mid-run human control: pause / resume / abort, cross-process
- [x] Pinned memory facts that bypass the injection cap
- [x] Context-budget handoff (summarize + fresh worker at ≥70% of the budget)
- [x] Relevance retrieval behind the memory tables (stdlib `RetrievalBackend`)
- [x] Retrieval / tool-call provenance events, attributed to the attempt and
      agent that used them
- [x] Planner agent + task graph; parallel workers
- [x] Second-provider cross-validator (stdlib `OpenAICompatRunner`, per-role
      pinning via `AgentSpec.runner`)
- [x] Agent-requested tools with an auto-approval policy
- [x] git-commit-per-task rollback: one git repo per task workspace, so
      `reject` and `redo` recover the discarded round instead of destroying it
- [x] Infra retry/backoff, verified at the validator and executor stages
      (the behaviour pre-dated this slice; slice 6 added its missing coverage)
- [x] Batch whole-loop evaluation (`agentloop eval --mode batch`)
- [x] Coverage captured in `test_runs`, parsed from output already collected
- [x] Office-metaphor visualization layered on the existing dashboard data
- [x] **Hardening and finalization pass (slice 8)** — a full adversarial review
      before first real use, and every finding it produced. Three criticals, all
      reproduced before they were fixed and each with a regression test that was
      watched failing first:
      - the dashboard accepted cross-origin mutations (a foreign `Origin` could
        rewrite the project charter, which is injected into every agent prompt),
        and never checked `Host`;
      - the **default** `ClaudeSDKRunner` had neither of the two money guards its
        OpenAI sibling shipped with — `extract_usage` raised on a wrong *type*
        after a completion was billed, and four silent zeros could reach
        `attempts` as a measured $0.00;
      - `vcs._git` passed a relative `-C` on top of an already-changed cwd, so
        with the shipped default `workspace_root` **the entire durability
        feature was inert on every install** — and every one of ~2900 lines of
        vcs tests used an absolute `tmp_path`, so none could see it.
      Plus: the verdict parser rejected ordinary LLM markdown (five of six real
      formats escalated a decision the validator had actually made); an
      undecided tool request revoked a capability the role already held (on the
      example the shipped prompt teaches); the sandbox captured output unbounded
      and its timeout did not bound wall time; a missing registry role wedged the
      whole batch; a claim failure in a parallel worker was swallowed and
      reported as success; `resume` left the pause message on the row forever;
      and the 500k token cap escalated a *successful* first real task.
