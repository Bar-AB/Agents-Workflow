# CLAUDE.md — project context for agentloop

## What this is
A general-purpose agentic development loop. Worker agents execute tasks; an
independent validator reviews output against acceptance criteria; humans stay
in the loop at task definition and review. One SQLite database is the single
source of truth for state, metrics, audit trail, and memory.

Current state: **Phase 1 + Phase 2** — sequential loop end-to-end with real
sandboxed test execution and wired memory, plus a live React dashboard
streaming from the same store. Still sequential by design. Later additions:
accurate token/cost accounting incl. prompt cache, a validator calibration
harness (`agentloop eval`), mid-run human control (pause/resume/abort), and
pinned memory facts. Plus a hardening pass (Slice 0): the executor scrubs the
child env to an allowlist and has a `sandbox_isolation` switch; store writes
pair each row change with its audit event in one `transaction()`; task claims
are atomic (`claimed_by` lease); and transient infra failures retry then
escalate instead of crashing the batch. Plus Slice 1: **context-budget handoff**
— once a worker's accumulated context on a task reaches `context_handoff_ratio`
of its `AgentSpec.context_budget_tokens`, a dedicated `summarizer` agent compacts
the working state and the worker is restarted from that summary in place of the
raw transcript (a `context_handoff` event); not counted as a revision. Plus
Slice 2: **relevance retrieval + provenance** — approved memory facts are ranked
against the task at hand through the new `retrieval.py` seam (stdlib hashing
backend, no dependencies) instead of being selected alphabetically, and the
audit log gained `retrieval` and `tool_call` events, each attributed to the
attempt and agent it belongs to. Plus Slice 3: **planner + task graph + parallel
workers** — a `planner` agent decomposes a goal into a DAG of tasks
(`agentloop plan`), persisted in the new `task_deps` table alongside `tasks.kind`
/ `plan_id` / `plan_approved`; being blocked (unfinished dependency, or a plan a
human hasn't signed off) is a *predicate inside the atomic claim*, not a status,
so dependency order holds identically for one worker and for
`max_parallel_workers` of them (default 1 = today's sequential loop unchanged).
Plus a corrective slice on the slice-2 memory code (no roadmap number — every
item was a documented rule quietly not holding): **promotion is a transition**
(`Store.memory_promote` moves the row instead of copying it, so there is no
duplicate to inject, to re-promote forever, or to leave approved after the
original is revoked), **a hit is a distinct task** (the new `memory_hits` table,
since worker + validator + one revision used to promote a fact inside a single
task), plus a one-transaction approval-gated `memory_read`, ranking that pads
rather than drops, tool-call telemetry that can never roll back a paid attempt,
and `transaction()` depth bookkeeping that can no longer go negative.
Plus Slice 3c: **project charter + validator findings**, two independent
additions that change **no decision rule**. The charter is human-authored,
project-wide prose in its own append-only `charter` table (one row per version,
`id` *is* the version), injected as a `## Project charter` block above the
memory block in the worker, validator and planner prompts — not the summarizer —
with `attempts.charter_version` recording which version each invocation ran
under. Agents have no write path to it; the CLI (`agentloop charter`) and the
dashboard are the only two. The validator now also enumerates what it checked
under a soft `FINDINGS:` marker, stored in `verdicts.findings` — **added to**
`Verdict.reasoning`, never subtracted from it, since `reasoning` is what the
loop feeds back as revision feedback.

## Commands
- Install: `pip install -e ".[dev]"` (add `.[claude]` for the real runner)
- Format: `ruff format .` — **run before every commit and push** (see Conventions)
- Tests: `pytest -q` (no API keys or network needed, nothing skipped)
- Run: `agentloop add "Title" --goal ... --criteria ... [--risk 0|1|2]`,
  or `agentloop plan "Goal" --criteria ...` (planner → task graph) then
  `agentloop approve-plan ID` to release it,
  then `agentloop run --runner claude|mock`, `agentloop status [ID]`,
  `agentloop events ID`, `agentloop approve|reject|redo ID`,
  `agentloop pause|resume|abort ID` (mid-run control),
  `agentloop memory list|approve|reject|add|pin|unpin` (`add --pinned`),
  `agentloop charter show [--version N]|set --file|--text [--note]|clear|history`
  (the human-only write surface for project-wide rules),
  `agentloop eval --runner mock|claude` (validator calibration)
- Dashboard: `cd web && npm install && npm run build`, then `agentloop serve`
  (frontend checks: `npm run typecheck`, `npm run build`)

## Architecture (agentloop/)
- `config.py` — LoopConfig: thresholds, budget caps; model pricing table.
  Memory retrieval knob (`memory_retrieval_backend`: `'hash'` (default) or
  `'none'`; anything else raises).
  Also sandbox knobs (`sandbox_env_allowlist`, `sandbox_isolation`), infra
  retry knobs (`infra_max_retries`, `infra_retry_backoff_s`), and the
  context-budget handoff threshold (`context_handoff_ratio`, default 0.70 of the
  worker's `context_budget_tokens`). Planner/parallel knobs:
  `plan_requires_approval` (default True), `max_plan_tasks` (20),
  `max_parallel_workers` (default 1 = sequential, unchanged).
- `models.py` — Task (incl. `kind` 'task'|'plan' and `plan_id`), TaskStatus,
  Verdict (incl. `findings`: what the validator checked, a *copy* of a slice of
  `reasoning` and never a piece removed from it), VerdictKind, AgentSpec,
  RunResult, PlannedTask (a planner-proposed
  graph node, with a local `ref` that expresses edges before db ids exist).
- `store.py` — SQLite source of truth. Tables: tasks (incl. a `control` column:
  run/pause/abort, written only by `set_control`; and a `claimed_by` lease
  column, written only by `claim_next_task`), attempts (per-invocation
  metrics: tokens/cost/wall time, incl. `cache_creation_tokens`/
  `cache_read_tokens`, plus `charter_version`), verdicts (incl. `findings`),
  events (append-only audit log — never
  UPDATE/DELETE), memory (two tiers project/loop; reads gated on `approved`,
  which a value change revokes; `pinned` flag; `last_used_at` set by
  `memory_read`), memory_hits, charter, test_runs, eval_runs. Schema is plain SQL so
  Postgres migration isn't a rewrite; `_migrate()` adds later columns to
  existing dbs (a whole new table needs no entry — `CREATE TABLE IF NOT EXISTS`
  covers it).
  `memory_promote(id)` **moves** a project row to `loop`
  (`UPDATE ... SET tier='loop'`) rather than copying it: the row keeps its id,
  so approval, pin, `hit_count` and its `memory_hits` follow it and there is no
  second row to inject alongside the first, to re-promote forever, or to stay
  approved after the original is revoked. On a `UNIQUE(tier, key)` collision
  `_merge_into_loop` runs instead: only the loop row's **id** survives, the
  promoted value replaces its contents, hits are merged and `hit_count`
  recounted. A winner *is* picked, so the displaced value and its approval go
  into the `memory_promoted` payload — no other event records a memory value.
  Approval is then decided **against the surviving value**, not against a
  particular row: it stays approved only if a human approved that exact text
  (`memory_write` alone keeps the *loop* row's, so an approved fact merged onto
  an unapproved row of the same value would lose its approval and then be
  deleted). Two approved rows with *different* values are a real conflict and
  drop to unapproved; one approved row whose value survives does not — testing
  the loop row's prior approval instead un-approved a vetted project value
  merely because an unrelated loop row held the key, and did it silently. Any
  drop from approved logs `memory_revoked`, whichever row held it.
  `_reconcile_memory_hits` applies that merge, once, to the duplicates an older
  database already holds, with two deliberate differences (`origin='migration'`)
  because it repairs a database rather than recording a win: an **approved**
  loop value outranks an unapproved project value — those pairs are typically a
  vetted copy beside a project row rewritten afterwards, so "the promoted value
  wins" would overwrite vetted content with the rewrite that un-approved it, on
  nothing more than opening the database — and it logs
  `memory_duplicates_merged`, not `memory_promoted`, since nothing was promoted
  here and reusing the event would make anyone counting promotions in the feed
  count database opens. It also **zeroes legacy `hit_count`** — the old counter
  counted prompts, so carrying it into a task-based threshold would promote most
  of the project tier on the first prompt after the upgrade, which is the exact
  failure this fixes.
  `memory_hits(memory_id, task_id)` is the evidence behind `hit_count`:
  `memory_read(..., task_id=…)` does `INSERT OR IGNORE` and bumps the counter
  only when the insert inserted, so `hit_count` counts *distinct tasks*, not
  prompts. Lookup and bumps are one transaction with `AND approved=1` on the
  UPDATEs, and the hit is recorded only if that gated UPDATE matched — otherwise
  `OR IGNORE` would leave an evidence row the counter could never make up.
  `transaction()` groups a state change and its audit event into one commit
  (all-or-nothing; rollback never touches committed `events`); route paired
  row+event writes through it. **Every unbatched write goes through
  `_LockedConnection.write()`**, which holds the lock across execute *and*
  commit: as two separate locked calls, a peer thread entering `transaction()`
  in the gap and failing would `rollback()` the shared connection and silently
  destroy the first thread's uncommitted row (a worker's output, an attempt's
  tokens) while its call returned normally. Never pair a bare
  `execute()` with a following `commit()`. `claim_next_task(worker_id)`
  atomically hands a task to exactly one worker (parallel-worker prerequisite);
  the sequential loop uses one stable id and is behaviorally unchanged. The
  claim is a **compare-and-swap** (`WHERE status='pending' AND claimed_by IS
  NULL`, checked via `rowcount`): the connection lock only serializes one
  process, and sqlite3 opens no write transaction for a `SELECT`, so two
  `agentloop run` processes could otherwise both read and both claim one row.
  `stranded_claims()` finds in-flight work held by a retired claim id (a shrunk
  worker pool); `Loop.run` logs a `claim_stranded` event per orphan rather than
  reclaiming it — a retired id is indistinguishable from a live second process
  in the same id-space, and stealing its task is worse. `attempt_tokens(id,
  kind)` sums one role's tokens (incl. prompt cache) — how the context-budget
  handoff measures the worker's accumulated context, separate from `task_spend`.
  `charter` is the project charter: **one row per version**, `id` *is* the
  version, and the highest id is what is in force — so "what is in effect" is a
  pure function of the table and no flag can disagree with it. Versions rather
  than rules-as-rows because a past attempt's recorded version has to stay
  *readable*, not merely identifiable (the same reasoning `_merge_into_loop`
  applies to a displaced memory value). `charter_set` refuses a body over
  `_MAX_CHARTER_CHARS` (4000) or a whitespace-only one — the charter is never
  trimmed at inject time, so oversize fails loudly at write time, and a typo'd
  `>` redirect must not be a silent policy removal; `charter_clear` is the
  explicit, audited way to turn it off, and it appends an empty version rather
  than deleting anything. `charter_active` returns None for both never-set and
  cleared, deliberately: the two must be indistinguishable so turning the
  charter off restores byte-for-byte the pre-charter prompt. Also
  `charter_history`/`charter_version(v)`. Events `charter_set`
  (`{version, note, n_chars}`) and `charter_cleared` carry no body — the row
  keeps it forever, and every event is pushed to every open SSE client.
  There is no per-attempt charter event: one scalar per invocation is a column
  (`attempts.charter_version`), not an event, which also makes "which tasks ran
  under v3" one join instead of a full-text scan of `worker_prompt` payloads.
  `task_metrics` exposes it as `charter_versions`, and selects `findings` —
  that `SELECT` is the *only* verdict data the dashboard gets, so a column
  missing from it is stored, exposed nowhere, and looks implemented.
  `task_deps` holds the planner's graph; `_UNBLOCKED` is the claimability
  predicate (`kind='task'` AND every dependency `done` AND the plan approved),
  applied *inside* `claim_next_task`'s transaction so it is evaluated against
  committed state and two workers finishing two dependencies at once can't both
  conclude a shared dependent is still blocked. `add_dependency` refuses any
  edge that would close a cycle (incl. self-edges) — a store-level invariant,
  not a hope about the planner. Also `dependencies`/`dependents`/`plan_tasks`/
  `set_plan_approved`/`is_plan_approved`.
- `registry.py` — agent registry: role, model, system prompt, tools, context
  budget, version. Defaults in code (`worker`, `validator`, `summarizer`
  for context handoffs, and `planner` for goal decomposition);
  `agentloop init-registry` → agents.json. A hand-edited
  agents.json predating `summarizer` degrades gracefully — `run_summarizer`
  falls back to the worker's spec rather than crashing the handoff. The
  `planner` has no such fallback (planning as a worker would decompose a goal
  with a prompt that never asked for a graph): `Loop.plan` checks for the role
  up front and escalates the plan row to NEEDS_HUMAN naming the registry —
  *before* the retry loop, since a missing role is a config error that retrying
  cannot fix and `infra_error` would point the human at the network instead.
  Its tools are `file_read` (Read only), not `file_io` — a planner proposes
  work, it does not do the work.
  `worker`, `validator` and `planner` are at **version "2"**: each is now told
  that a `## Project charter` block states rules holding across every task, and
  the validator additionally emits a `FINDINGS:` section. That prompt change is
  load-bearing, not cosmetic — the old `VALIDATOR_SYSTEM` said "judge it
  strictly against the task's acceptance criteria", which instructs a validator
  to *disregard* a charter, so injection alone would have been inert. A
  hand-edited `agents.json` predating the change degrades cleanly: the charter
  is still injected, the agent is simply not told to weigh it. Note this is the
  one part of the slice that is *not* inert on an unchartered project: the
  system prompts changed for everyone, so an unchartered project's validator now
  emits a `FINDINGS:` section it did not before. The **user** prompt is what an
  absent charter leaves byte-for-byte unchanged.
- `runner.py` — **ModelRunner protocol: the provider seam.** The loop never
  imports a vendor SDK directly. Backends: ClaudeSDKRunner (default),
  MockRunner (scripted, for tests). New providers (litellm, Codex
  cross-validator) = new implementations of `run()`. Usage is read from the
  terminal `ResultMessage` only (`extract_usage`) — all four fields, including
  prompt cache; summing per-message double-counts. Tool uses are the opposite:
  `extract_tool_calls` accumulates `ToolUseBlock`s *across* the stream into
  `RunResult.tool_calls` (duck-typed, so an SDK rename costs a record, not a
  run). `MockRunner` returns a scripted `RunResult` as-is, so a test can script
  tool calls without the SDK.
- `eval.py` — validator calibration harness. Fixtures (task/output/gold verdict
  + a scripted mock line) run through `run_validator`; reports agreement,
  confusion matrix, calibration table. Mock path is deterministic (CI); the
  claude path is a real measurement. Invocations run against a scratch
  in-memory store so they don't pollute the task board.
- `agents.py` — prompt building for worker/validator/summarizer/planner; verdict
  and plan parsing.
  `run_planner` decomposes a goal (its own `planner` attempt on the plan row);
  `parse_plan` validates a reply *whole* before anything is written — unique
  refs, every `depends_on` present in the same plan, DAG (Kahn), size cap — and
  raises `PlanError` on any failure, so a bad plan never becomes a partial graph.
  `_upstream_block` puts completed dependencies' output in a dependent worker's
  prompt (bounded): an edge that only delays a task is a schedule, not a plan.
  `run_summarizer` compacts a task's working state for a context handoff (its own
  `summarizer` attempt); `run_worker` takes a `handoff_summary` that replaces the
  raw previous-output+feedback block when a handoff has fired.
  Validator first line: `VERDICT: <kind> CONFIDENCE: <0-1> TESTS: <pass|fail|na>`.
  Injects approved memory facts and real test results into prompts.
  `_charter_block(store)` returns `(block, version)` and `("", None)` when there
  is no charter — the same "empty means absent" convention `_memory_block` uses,
  which is what makes the byte-for-byte guarantee hold structurally rather than
  by inspection. The body goes in **verbatim and whole**: never truncated,
  reordered or dropped at inject time. Injected by `run_worker`,
  `run_validator` and `run_planner`, above the memory block; **not**
  `run_summarizer`, whose output is consumed by a worker that rebuilds the block
  fresh anyway (so a charter survives a context handoff, which a rule mentioned
  once in a transcript does not). The version travels to `_invoke` and lands on
  the attempt row, so the recorded version is by construction the injected one.
  **The charter is never part of `_retrieval_query`** — folding it in would rank
  every task's memory against the same house-rule vocabulary, converging the
  ordering across all tasks and undoing slice 2.
  `parse_verdict` additionally extracts `_extract_findings(reasoning)`:
  a soft, line-anchored, case-insensitive `FINDINGS:` marker, running to the end
  of the reply or to the next markdown **section** heading (any level, and only
  when preceded by a blank line), bounded at `_MAX_FINDINGS_CHARS` (4000), and
  degrading to `""` on anything unexpected rather than raising. `reasoning`
  itself is untouched. The blank line in `_FINDINGS_END_RE` is load-bearing: a
  bare `^#{1,6}\s` also matches a `# TODO:` line quoted *inside* a finding — a
  code reviewer quoting a comment is the common case, not an exotic one — and
  every finding after it was dropped with no signal. Both ambiguities here
  resolve toward keeping too much: over-reading stores a little stray prose,
  under-reading destroys evidence, and only one of those is recoverable.
- `executor.py` — sandboxed test execution. Threat model is **arbitrary
  AI-generated code**, not just command hijack. Command is allowlisted in
  config (never model output), split to argv and run with `shell=False`, cwd
  pinned to `.agentloop/ws/task-{id}/`, timeout + output cap. Use
  `split_command`, not bare `shlex.split` — POSIX mode eats Windows path
  separators. The child env is scrubbed to `_BASE_ENV_ALLOWLIST` +
  `sandbox_env_allowlist` (never the parent env wholesale — that carries
  `ANTHROPIC_API_KEY`). `sandbox_isolation='strict'` requests a container/
  no-network tier and degrades to env-scrub with a warning when none is wired
  (documented residual risk: fs/network still open in the env-scrub tier).
- `memory.py` — two-tier policy: approved-only reads, unapproved agent writes,
  `hit_count`-based project→loop promotion (`_record_reads` bumps only facts a
  ranked injection scored above zero, and only once per `task_id`;
  `maybe_promote` delegates the move to `Store.memory_promote`). `_rank` pads
  candidates the backend didn't return back in, in candidate order, and
  truncates to `top_k` itself — ranking decides order, never membership, and a
  real index returns its own hits rather than the caller's set.
  `facts_for_prompt(query=…, task_id=…)` ranks
  the approved candidates through `retrieval.py` when a query is given and
  returns `(block, provenance)`; it never logs — a retrieval is only meaningful
  attached to the attempt it fed, so `agents._invoke` logs it. With no query (or
  `backend=None`) provenance is None: exactly the pre-slice-2 alphabetical
  selection, and nothing logged.
- `retrieval.py` — **RetrievalBackend protocol: the memory-relevance seam**
  (slice 2), same shape as `ModelRunner`. `embed()` is a stdlib hashed
  bag-of-words vector (blake2b, *not* builtin `hash` — that is salted per
  process); `HashingBackend` brute-forces cosine over the candidates and is the
  only backend. `get_backend` resolves `'hash'` and `'none'` and raises on
  anything else — no silent degrade to a working default. (A `ChromaBackend` +
  `[rag]` extra shipped briefly and was removed: same `embed()` on both sides
  meant identical ordering at any fact count this store holds, so it was an
  optional CI-untested path buying nothing.) `rank_exact`'s sort is stable on
  score alone, which is what makes the caller's `ORDER BY tier, key` the
  tie-break — a future index backend must therefore rebuild its subset in
  *candidate* order, never in the index's returned order.
  **The approval gate lives upstream:** callers pass in already-approved rows,
  so no backend can surface an unvetted fact — a future index may only re-rank
  rows the store already handed over, making it a derived cache and never a
  second source of truth.
- `loop.py` — orchestration state machine + human decision methods.
  `_maybe_handoff` runs at the iteration boundary (next to the budget/control
  checks): if the worker's context since the last handoff clears the ratio, it
  summarizes and returns the summary for that round's worker call; an in-loop
  watermark advances so only newly accumulated context can retrip.
  `plan()` runs the planner and persists the graph (tasks + edges + the
  `plan_created` event in one `transaction()`); `approve_plan()` releases it, and
  `human_approve` routes plan rows there so the dashboard's approve button
  releases the plan instead of marking a goal done with its tasks still blocked.
  `run()` is sequential at `max_parallel_workers=1` (`_run_serial`, unchanged);
  above 1, `_run_parallel` starts that many threads that each claim
  independently. It holds **no** schedule in memory — the store's claim decides
  what may run — and an idle worker *waits on a condition while any peer is
  busy* rather than exiting, because a plan usually has one root and exiting
  would collapse the rest of the graph back to serial.
- `server.py` — REST + SSE dashboard backend, stdlib `http.server` only. The
  append-only `events` table *is* the change feed: SSE is a `WHERE id > cursor`
  query, so reconnects resume losslessly via `Last-Event-ID` and the dashboard
  never mirrors state into a second store.
- `cli.py` — argparse CLI, structured plain output. `charter show|set|clear|
  history` is the human write surface for the project charter; a `ValueError`
  from `charter_set` renders through `main`'s handler as `error: ...`, which is
  the loud write-time refusal that pays for never truncating at inject time.
- `web/` — Vite + React + TypeScript dashboard. `types.ts` mirrors the server's
  JSON shapes; keep them in sync when changing an endpoint.

## Decision rules (do not change without updating tests + README)
- approve + confidence ≥ approve_threshold (0.70) + tests not failing → DONE,
  unless risk_level ≥ 2 → NEEDS_HUMAN sign-off first.
- **"tests not failing" = the executed result, not the validator's `TESTS:`
  claim.** Tests run in the task workspace between worker and validator; a
  validator claiming `pass` over an executed `fail` is logged as a
  `test_disagreement` event and cannot approve the task. `na` (no workspace or
  execution disabled) falls back to the validator's claim.
- revise, or approve below threshold → revision with validator feedback,
  bounded by max_revisions (3); exhausted → NEEDS_HUMAN.
- escalate verdict, or confidence < severe_threshold (0.40) → NEEDS_HUMAN
  immediately (no revision loop on severe disagreement).
- Worker output starting `ESCALATE:` → NEEDS_HUMAN (genuine ambiguity —
  agents ask instead of guessing).
- Budget cap (tokens or cost) exceeded → NEEDS_HUMAN. Cost and token totals
  include prompt-cache tokens (cache write ×1.25, read ×0.10 on the input rate);
  a cache-heavy run trips the cap that pre-fix it slipped past.
- Transient infra failure (a `runner.run()` / executor call raises) → retried
  with exponential backoff up to `infra_max_retries`; if it persists →
  NEEDS_HUMAN with an `infra_error` reason (each attempt logs an `infra_error`
  event). Distinct from a "revise" (infra failure is not a task-quality
  failure) and **not** counted against `max_revisions`; one flaky call does not
  abort the rest of the batch. This is the home for the retry/backoff the
  roadmap slice 6 lists — deduped to here.
- Context-budget handoff: at the iteration boundary (alongside the budget/control
  checks), if the worker's accumulated context on the task — `attempt_tokens(id,
  'worker')` since the last handoff, prompt cache included — reaches
  `context_handoff_ratio` (0.70) × its `AgentSpec.context_budget_tokens`, the
  `summarizer` agent compacts the working state and the worker is restarted from
  that summary in place of the raw transcript. Logged as a `context_handoff`
  event (before/after tokens). **Not a revision** (doesn't touch
  `max_revisions`); the summarizer call is retried on infra failure like any
  other and its cost still counts toward the task budget cap.
- Planner (`Loop.plan`): every failure mode escalates the **plan row** to
  NEEDS_HUMAN and creates **zero** child tasks — planner `ESCALATE:` (genuine
  ambiguity), unparseable JSON, a `depends_on` naming a task outside the plan, a
  cycle, or more than `max_plan_tasks`. Never repaired, never truncated, never
  partially applied: the missing half of a half-applied plan is invisible, while
  the half that landed looks like a complete plan someone approved. A missing
  `planner` registry role escalates up front (config error, not retried).
- A plan's tasks are not claimable until it is signed off when
  `plan_requires_approval` (default True) — a planner generating tasks *is* task
  definition. Released by `approve_plan` / `agentloop approve-plan` / the
  dashboard's approve button on the plan row. The gate lives on the plan row as
  a **claimability predicate**, not as a child status: a child parked in
  NEEDS_HUMAN would be marked DONE by a stray `approve` on work that never ran.
- A task is claimable only when **every dependency is DONE**. A dependent of an
  escalated/failed task is *skipped, not failed* — it stays `pending`, the batch
  finishes everything else, and resolving the dependency makes it claimable on
  the next run. "Blocked" is deliberately a predicate evaluated inside
  `claim_next_task`, never a status: nothing has to be un-set when the blocker
  clears, and waiting work can't be mistaken for finished work.
- Cycles are refused at `Store.add_dependency` (and again in `parse_plan`).
  A cycle is a permanent deadlock, not a slow graph, so "the graph is a DAG" is
  a store invariant rather than a hope about the planner.
- `max_parallel_workers` (default 1) bounds how many tasks run at once. At 1 the
  loop is behaviorally identical to before. Above 1 the atomic claim (slice 0c)
  is what guarantees one task = one worker; per-task attempts/verdicts/events
  stay isolated, but run wall time no longer equals the sum of task wall times
  and events from different tasks interleave (still ordered by id, each with its
  `task_id`).
- `human_approve` refuses a task still `pending`: approval signs off work that
  was produced and reviewed, and a pending task has no attempt, verdict or
  output. With a graph this is load-bearing rather than cosmetic — `done` is
  what satisfies a dependency, so approving unrun work would release its
  dependents to run against upstream output that does not exist.
- `approve_plan` refuses a plan with zero tasks (an unparseable/cyclic/escalated
  plan). Approving one would convert an escalation into a green DONE goal with
  nothing under it and blank the diagnosis off the row — the one place a human
  click could push against "fail safe toward NEEDS_HUMAN".
- Mid-run control signal, read each iteration boundary alongside the budget
  check: `pause` → PAUSED (survives restart, no auto-resume), `abort` → ABORTED
  (terminal but output/audit preserved). `control` is written only by
  `set_control` so the loop's stale in-memory task can't clobber it.
- Validator findings are **evidence, not a gate**. `approve` + confidence ≥ 0.70
  + tests not failing → DONE regardless of what the findings say, and an empty
  findings section is recorded as "no findings recorded" rather than treated as
  a reason to revise. BMAD's adversarial review treats zero findings as a
  trigger to re-analyse; their reviewer is advisory and a human filters its
  false positives, while ours drives an automatic state transition, so "no
  findings → reject" would convert a legitimately clean review into a revision
  loop against `max_revisions`. A missing or malformed section never fails an
  attempt: it degrades to empty, never raises.
- Likewise the **project charter is prompt content, not a gate**: a charter
  violation reaches the loop only as the validator's ordinary `revise`/
  `escalate` verdict. Nothing in `loop.py` reads the charter, and no status
  depends on it. An empty or absent charter produces a byte-for-byte identical
  prompt to the pre-charter loop — which is why "cleared" and "never set" are
  the same state, and why the block is `""` rather than a header with nothing
  under it. What it does **not** fix: it shares *rules*, not *decisions made in
  flight*, so two sibling workers still cannot see that one named a function
  `to_slug` while the other called it `slugify` (`_upstream_block` carries
  output along edges, and siblings have none). It collapses the class of
  conflicts to the ones a human anticipated and wrote down.
- Unparseable validator verdict → escalate at confidence 0 (never
  guess-approve). A `FINDINGS:` section never rescues one.
- `human_redo` = same task definition, fresh start, NO carried context
  (output/feedback/revision_count reset, workspace wiped); audit trail
  preserved.
- Approval is approval **of a value**: `memory_write` keeps `approved` across a
  rewrite only while the value is unchanged (`MAX(excluded.approved, CASE WHEN
  memory.value = excluded.value THEN memory.approved ELSE 0 END)`). Changing the
  content of an approved key returns it to unapproved — otherwise an agent
  rewriting a vetted key inherits the gate for arbitrary new content. `pinned`
  stays sticky across a value change (it is about the key, not the value).
- Memory reads are gated on `approved`; agent writes land unapproved. A
  project fact that is a *hit* on `memory_promote_threshold` (3) tasks promotes
  to `loop`. Injection is capped (20); `pinned` approved facts sort first and
  bypass that cap under a separate ceiling (10), but pinning never bypasses
  `approved`.
- A `hit_count` bump requires a **ranked injection scoring > 0**, not an
  injection. Under the cap every approved fact is injected into every prompt, so
  counting injections meant "existed while 3 tasks ran" and promoted the whole
  project tier; an unranked selection (no query / `'none'`) counts nothing. Still
  a proxy — the real signal is whether the fact changed the output, which the
  per-attempt `retrieval` provenance makes measurable later.
- A `hit_count` bump also requires a **distinct `task_id`**. Worker + validator
  + one revision are three ranked injections *inside one task*, so counting
  prompts promoted a fact on the strength of a single task while the docs and
  the test's own name claimed three. `memory_hits(memory_id, task_id)` is the
  record; the second prompt of a task inserts nothing and so counts nothing. A
  read with no task (`agentloop memory`, `eval`, a direct `MemoryService.read`)
  records recency but no hit — promotion is a claim about tasks, and there
  isn't one.
- Promotion is a **transition, not a copy**: the row moves tier and the project
  row does not survive. A copy left both rows approved, so both were injected
  (a real fact evicted from a cap that was never full), `memory_promoted`
  re-fired on every later read, and revoking the original left the copy
  approved — the value-approval rule bypassed by memory's own promotion path.
  See `store.memory_promote` / `_merge_into_loop` for the collision rule, and
  `_reconcile_memory_hits` for what opening an older database does. Note reads
  are approval-gated, so an *unapproved* project row collects no hits and the
  live path can never promote one — the "promoted row was unapproved" branches
  are reachable only through a direct `Store.memory_promote` call or the
  migration, which is how they are tested.
- Memory selection is **ranked by relevance to the task** (title/goal/criteria
  plus feedback or the output under review) when a query is supplied. Ranking
  decides *order only*: the approved gate, the 20/10 caps and the pinned ceiling
  are applied around it and are unchanged, so a fact that fits under the cap is
  never dropped for scoring low (a zero score costs it a promotion credit, not
  its slot). Ties fall back to
  loop-tier-then-alphabetical. No query, or `memory_retrieval_backend: 'none'`
  → the pre-slice-2 alphabetical selection and no `retrieval` event.
- Every memory injection with a query logs a `retrieval` event (query, backend,
  candidate count, and each injected fact's id/tier/key/pin/score) — the audit
  log recorded what *entered* memory but never what was read out of it. Logged
  in `agents._invoke`, in the same `transaction()` as the attempt row and
  carrying `attempt_id`/`agent_kind`/`role` like `tool_call`: one task retrieves
  once per agent per round, and worker and validator rank against different
  queries, so a task-only record could not tell them apart.
- Every tool an agent actually invokes logs a `tool_call` event (attempt_id,
  agent kind, tool, truncated input), sourced from `RunResult.tool_calls`.
  Registry tools already reach the SDK, so this was happening unrecorded;
  slice 5's auto-approval policy layers on this record. **Both** fields are
  coerced to a bounded **string** — the input by `agents._tool_input_repr`
  (JSON, else `repr`, else the type name), the name by `agents._tool_name_repr`
  (a `str` passes through unquoted, since the dashboard renders it as a name and
  slice 5 reads it as one). They share the one `json.dumps` in `log_event`, so
  coercing only the input left the identical hole open on the name:
  **telemetry must never fail an attempt.** A value `json.dumps`
  could not encode raised inside `_invoke`'s closing transaction and rolled back
  `finish_attempt` — the output, tokens and cost of a model call already paid
  for — after which `_with_retry` ran it again. `ClaudeSDKRunner` sanitises, but
  the seam does not require it and slice 4 adds a second runner, so the coercion
  lives at the seam.
- `Store.transaction()` decrements `_txn_depth` on the **error** path too, rolls
  back once at the outermost boundary, and raises `TransactionAborted` if an
  enclosing block swallowed an inner transaction's error. Zeroing the depth let
  the enclosing block decrement to -1, after which every `commit()` saw a
  non-zero depth and silently stopped committing. Unreachable until something
  nests a `try` around an inner transaction; slices 5-6 do.

## Conventions
- Python ≥ 3.10, stdlib-only core (no runtime deps); claude-agent-sdk and
  pytest are optional extras. Keep it that way — the dashboard uses
  `http.server` + SSE rather than FastAPI/websockets for exactly this reason.
- `Store` serializes all SQL through a lock and opens with
  `check_same_thread=False`: the server reads from request threads while the
  loop writes. Go through `self._conn`; don't reach for a raw connection.
- Dataclasses + str-Enums for domain types; type hints everywhere.
- Every state change and agent I/O gets a row in `events` — the audit log is
  load-bearing (debugging the loop, trusting memory contents).
- Tests are end-to-end through the Loop with MockRunner scripted outputs;
  add a test for any new decision rule.
- **Always run `ruff format .` before any commit and push** — the whole tree
  must be ruff-clean, so formatting never rides along in an unrelated diff.
  `ruff` is a `[dev]` extra.
- CLAUDE.md, *.db, and local config (loopconfig.json, agents.json) are
  gitignored.

## Roadmap (next slices, from the seed spec)
1. ~~Context-budget handoff: summarize + hand off to a fresh worker at ~70% of
   `context_budget_tokens`.~~ **Done (Slice 1)** — enforced in `loop.py`
   (`_maybe_handoff`) via the `summarizer` agent and `context_handoff` event.
2. ~~RAG store behind `MemoryService.facts_for_prompt`, with `retrieval` /
   `tool_call` provenance events.~~ **Done (Slice 2)** — `retrieval.py` seam,
   stdlib `HashingBackend`, no dependencies.
3. ~~Planner agent producing a task graph; then parallel workers.~~
   **Done (Slice 3)** — `planner` role + `task_deps`; claimability predicate in
   `claim_next_task`; `max_parallel_workers` (default 1).
4. Second-provider cross-validator via the ModelRunner seam.
5. Agent-requested tools with an auto-approval policy for read-only ones.
6. git-commit-per-task rollback; infra retry/backoff (distinct from "revise");
   batch whole-loop evaluation; coverage in test_runs.
7. Office-metaphor visualization layered on the existing dashboard data.
