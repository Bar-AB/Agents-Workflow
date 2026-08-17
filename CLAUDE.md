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
Plus Slice 4: **second-provider cross-validator** — the `ModelRunner` seam
finally carries a second provider. `OpenAICompatRunner` POSTs to any
OpenAI-compatible `/v1/chat/completions` endpoint over stdlib `urllib.request`
(no SDK, no runtime dependency, no optional extra — `retrieval.py` already
established what an optional CI-untested provider path is worth), so "second
provider" means "second base_url". Which backend serves a role is a *registry*
decision — a new `AgentSpec.runner` field, `None` meaning the loop's default —
resolved per role in `loop.py` (`_runner_for`), so pinning the validator to
another model family makes a worker's output reviewed by something that is not
its own family. It is a provider axis, **not a new gate**: no decision rule
reads it, and with nothing pinned anywhere every role goes to the same runner
object the loop was constructed with, so an unpinned run is the pre-slice-4 loop
exactly.
Hardened after an independent review found the money paths were the weak ones:
usage parsing is now **total** (a wrong *type* used to raise *after* the
completion was billed, and `_with_retry` then paid for it again — the
`_tool_name_repr` defect one layer earlier); the never-zero guard checks each
field independently and falls back to `total_tokens`; pricing normalizes the
**dated snapshot id** providers actually echo (`gpt-4o-mini-2024-07-18`) to its
family, without which every new pricing row and the whole `CACHE_MULTIPLIERS`
table were unreachable on real traffic; a permanent provider failure (missing
key, 400/401/403/404) is a `RunnerConfigError` that escalates as a **config
error** carrying the provider's own body, instead of three paid retries reported
as `infra_error`; a `claude-*` model pinned to the OpenAI backend is refused
before the call; the bearer token cannot follow a redirect and a non-loopback
`base_url` must be https; and a degraded run (estimated usage) is recorded as a
`runner_warning` event rather than printed to stdout where nothing can see it.

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
  Tool-request knobs (slice 5): `tool_readonly_allowlist` (`["file_read",
  "search", "task_state"]` by default — read-only requests auto-approve; `web`
  deliberately excluded because it egresses the prompt), `gate_declared_tools`
  (default False), and `max_tool_requests_per_task` (10, counting **pending**
  rows only, with a blocking ask exempt). Knob validation: `LoopConfig.__post_init__`
  normalizes field types by declared annotation, so a `null` list becomes `[]`,
  but a bare string raises, because `x in "string"` is a substring test that
  fails **open** on a permission allowlist — this is the layer that catches a
  misconfigured `loopconfig.json` before it reaches a paid attempt.
  `MODEL_PRICING` also carries the OpenAI-compatible models
  `OpenAICompatRunner` emits (point-in-time list rates); `OPENAI_MODELS` names
  them so a test can assert every one has a row — a missing model does not fail,
  it silently prices at `DEFAULT_PRICING`. `CACHE_WRITE_MULTIPLIER` /
  `CACHE_READ_MULTIPLIER` (1.25 / 0.10) turned out to encode **Anthropic's**
  cache pricing, so slice 4 made them per-model overridable through
  `CACHE_MULTIPLIERS` + `cache_multipliers(model)`: OpenAI charges nothing to
  write a cache entry and discounts a cached read by a factor that varies per
  family (0.10x on gpt-5, 0.25x on gpt-4.1/o3, 0.50x on gpt-4o), so left global
  a cross-provider run would under-bill a gpt-4o cached read five-fold and the
  budget cap is only as honest as the worst-priced attempt under it. Overrides
  rather than a third element on the pricing tuple: `(input, output)` is what
  the docs, the tests and the dashboard all read, and widening it would rewrite
  all of them to express something only two providers care about. An absent
  override is the Anthropic pair, so every pre-slice-4 model prices as it did.
  **Both tables are keyed through `pricing_key(model)`**, not by exact match:
  OpenAI echoes the resolved *snapshot* (`gpt-4o-mini-2024-07-18`,
  `gpt-5-2025-08-07`), never the requested alias, so an exact lookup missed on
  every real call — gpt-4o-mini priced at `DEFAULT_PRICING`'s 20x input rate and
  took Anthropic's cache multipliers, which is precisely the five-fold cached
  read under-bill `CACHE_MULTIPLIERS` was added to prevent. Both new tables were
  therefore unreachable in production while the dashboard reported the
  fabricated numbers as measured ones. `pricing_key` tries an exact hit, then
  the id with a trailing `-YYYY-MM-DD` stripped, then the longest table key the
  id extends at a `-` boundary (so `gpt-5-mini-…` prices as `gpt-5-mini`, not
  `gpt-5`). It normalizes at the *pricing* boundary only — `RunResult.model`
  keeps the serving snapshot, because which snapshot ran is provenance.
- `models.py` — Task (incl. `kind` 'task'|'plan' and `plan_id`), TaskStatus,
  Verdict (incl. `findings`: what the validator checked, a *copy* of a slice of
  `reasoning` and never a piece removed from it), VerdictKind, AgentSpec (incl.
  `runner`: which backend serves this role, `None` = the loop's default —
  it sits beside `model` because provider and model are one decision, and a
  `claude-sonnet-5` string means nothing to an OpenAI endpoint, so splitting the
  pair across two files would let a config edit produce a combination that
  cannot run; defaulted, so an agents.json predating it still loads through
  `AgentSpec(**spec)`), RunResult, PlannedTask (a planner-proposed
  graph node, with a local `ref` that expresses edges before db ids exist).
- `store.py` — SQLite source of truth. Tables: tasks (incl. a `control` column:
  run/pause/abort, written only by `set_control`; and a `claimed_by` lease
  column, written only by `claim_next_task`), attempts (per-invocation
  metrics: tokens/cost/wall time, incl. `cache_creation_tokens`/
  `cache_read_tokens`, plus `charter_version`), verdicts (incl. `findings`),
  events (append-only audit log — never UPDATE/DELETE), memory (two tiers
  project/loop; reads gated on `approved`, which a value change revokes; `pinned`
  flag; `last_used_at` set by `memory_read`), memory_hits, charter, test_runs,
  eval_runs, tool_requests (the capability ledger: one row per ask for one logical
  tool, for one task, by one role; `UNIQUE(task_id, role, tool)` and deliberately
  no foreign keys — an FK would raise inside a paid transaction; an `auto`/`approved`
  row *is* the grant — no separate grant table, so a permission never exists without
  the request that justifies it). Schema is plain SQL so Postgres migration isn't a
  rewrite; `_migrate()` adds later columns to existing dbs (a whole new table needs
  no entry — `CREATE TABLE IF NOT EXISTS` covers it). `release_claim` — written
  only by `set_status` when returning to `pending` — clears the `claimed_by` lease,
  fixing the pre-slice-5 bug where `human_redo` and `resume` left the lease set,
  making the task unclaimable forever and **starving every pending task behind it**.
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
  budget, version, and (slice 4) the `runner` a role is pinned to. Defaults in
  code (`worker`, `validator`, `summarizer`
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
  `worker`, `validator` and `planner` were put at **version "2"** by slice 3c:
  each is told that a `## Project charter` block states rules holding across
  every task, and the validator additionally emits a `FINDINGS:` section. That
  prompt change is load-bearing, not cosmetic — the old `VALIDATOR_SYSTEM` said
  "judge it strictly against the task's acceptance criteria", which instructs a
  validator to *disregard* a charter, so injection alone would have been inert.
  A hand-edited `agents.json` predating the change degrades cleanly: the charter
  is still injected, the agent is simply not told to weigh it. That was the one
  part of slice 3c that is *not* inert on an unchartered project: the system
  prompts changed for everyone, so an unchartered project's validator now emits
  a `FINDINGS:` section it did not before. The **user** prompt is what an absent
  charter leaves byte-for-byte unchanged.
  Slice 5 takes the same three roles to **version "3"**, teaching them the
  `TOOL_REQUEST: <tool> (blocking|optional) - reason` marker grammar so an agent
  that discovers mid-task it needs a capability can ask for one.
  `summarizer` stays at version "1" and is never taught — its output is consumed
  by a worker that rebuilds its own prompt fresh.
  **The inertness claim here is narrower than slice 3c's, and in a way worth
  stating precisely, because the obvious phrasing is false.** It is conditional
  on **no marker being emitted**, *not* on a config knob: an unconfigured run is
  byte-for-byte the pre-slice-5 loop only while no agent writes a marker, because
  `tools_for` then returns the declared list untouched and no row exists to
  subtract. A marker **does** write a pending row and log `tool_requested` with
  `gate_declared_tools` off — that knob decides whether a *declared* tool needs a
  grant, never whether a marker is honoured (see `tools_for`, and the
  self-revocation defect that turned on exactly this distinction). And since the
  system prompts now *teach* the grammar, an agent may emit one where it
  previously would not have. So "off by default" is true of the **gate** and
  false of the **request path**, and the two must not be collapsed.
- `runner.py` — **ModelRunner protocol: the provider seam.** The loop never
  imports a vendor SDK directly. Backends: ClaudeSDKRunner (default),
  **OpenAICompatRunner** (slice 4) and MockRunner (scripted, for tests). Usage
  is read from the
  terminal `ResultMessage` only (`extract_usage`) — all four fields, including
  prompt cache; summing per-message double-counts. Tool uses are the opposite:
  `extract_tool_calls` accumulates `ToolUseBlock`s *across* the stream into
  `RunResult.tool_calls` (duck-typed, so an SDK rename costs a record, not a
  run). `MockRunner` returns a scripted `RunResult` as-is, so a test can script
  tool calls without the SDK.
  `OpenAICompatRunner` POSTs to `{base_url}/chat/completions` with
  `urllib.request` — stdlib, so it is fully exercisable against a canned
  response, which matters most for the usage parsing: that is what the budget
  cap ultimately measures and the first thing a provider schema change breaks.
  `extract_openai_usage` is the pure seam for it, and it exists because
  OpenAI's `prompt_tokens` is **inclusive** of `prompt_tokens_details.
  cached_tokens` while Anthropic reports the two disjointly — and
  `RunResult.tokens_in` is *new* input only, so the cached share is subtracted
  out rather than added on (adding would bill it twice, at the full rate on top
  of the discounted one), clamped at zero so a provider over-reporting cache can
  never *lower* a task's measured spend back under a cap it had passed. Cache
  writes report 0: OpenAI's caching is automatic and carries no write charge, so
  reporting the prompt as a cache write would invent spend. Every field goes
  through `_int_or_zero`, which makes the function **total**: it cannot raise on
  any input. That is a money rule, not tidiness — `agents._invoke` calls
  `runner.run()` outside any transaction and reaches `finish_attempt` only on a
  clean return, so a raise here discards the tokens and cost of a completion the
  provider already billed and `loop.py`'s bare `except Exception` then re-pays
  for it, up to `infra_max_retries + 1` times, for a deterministic parse
  failure. `run()` wraps the whole usage block for the same reason: content is
  extracted first and *may* raise, usage second and may not.
  It **never returns zero tokens silently**, checked **per field** — missing
  input and missing output are estimated independently at a rounded-up
  ~4-chars-per-token, and `total_tokens - completion_tokens` is preferred over
  an estimate when it is there. `tokens_in == 0` with `cache_read > 0` is a
  legitimately fully-cached prompt and is *not* treated as missing; requiring
  all three fields to be zero (the original guard) let a provider omitting
  `prompt_tokens` record 0 input against a multi-KB validator prompt, where
  input is the dominant cost. A run whose usage was estimated sets
  `RunResult.usage_estimated` / `notes`, which `agents._invoke` records as a
  **`runner_warning` event** in the same closing transaction as the attempt —
  a `warnings.warn` alone never reaches `agentloop events`, the REST API or the
  SSE feed, so the estimate would land in `attempts` indistinguishable from a
  measured number and the dashboard would show a fabricated cost as a real one.
  The note is coerced through `agents._runner_note_repr`, because telemetry must
  never fail an attempt.
  `extract_openai_tool_calls` reports a call only when the response carries one
  (same "nothing recorded rather than a wrong record" contract as the SDK path)
  and tags every one **`executed: False`** — this backend sends no `tools` and
  has no execution loop, so what it sees is a call the model *asked for* and
  nobody ran, which CLAUDE.md's "every tool an agent actually invokes" rule
  distinguishes and slice 5's auto-approval policy must not conflate. The
  `tool_call` event carries the flag (SDK calls default to `True`).
  The API key is read from the environment at call time, checked *before* the
  request so a missing one is a `RunnerConfigError` rather than a retried 401,
  and used for exactly one thing — the Authorization header, on a request that
  **cannot be redirected**: the runner builds its own opener with a `_NoRedirect`
  handler, because urllib's stock handler strips only content-length/content-type
  and forwards `Authorization` across hosts, and `_check_base_url` refuses a
  non-`https` `base_url` unless its host is loopback. Neither is a live exploit
  (`base_url` is operator config), but this project scrubs `ANTHROPIC_API_KEY`
  out of the sandbox by construction rather than by trusting what runs there, and
  the wire deserves the same. `_classify_http_error` reads the **body** of every
  `HTTPError` into the message — that is the only place the provider says
  `model_not_found` vs `invalid_api_key` vs a transient overload — and splits
  400/401/403/404 (permanent → `RunnerConfigError`) from 408/429/5xx (transient →
  `ProviderResponseError`, retried). `_extract_message` raises
  `ProviderResponseError` rather than returning `output=""` for an `error`
  envelope under HTTP 200 (what OpenRouter/vLLM/Azure gateways return for quota
  and policy failures), a null `content`, or a `finish_reason` outside
  stop/tool_calls (`length` = truncated). **Documented
  residual limitation**, in the same register as `sandbox_isolation='strict'`:
  a chat-completions call has no tool-execution loop, so a pinned role's
  registry `tools` are **dropped with a warning** — the run degrades rather than
  crashing (`run_summarizer`'s precedent) but says so at the moment the gap
  opens. Forwarding them would be worse than dropping: the model would emit
  calls nobody executes and then reason as though they had run. This is why the
  role to pin is the **validator** — its prompt already carries the worker
  output, the executed test results, the charter and memory, so it reviews what
  it was given — and not the worker, whose whole job is writing files.
  `get_runner` knows `claude` / `openai` / `mock`; the four `--runner`
  `choices` lists in `cli.py` must be kept in step with it.
- `eval.py` — validator calibration harness. Fixtures (task/output/gold verdict
  + a scripted mock line) run through `run_validator`; reports agreement,
  confusion matrix, calibration table. Mock path is deterministic (CI); the
  `claude` and `openai` paths are real measurements, each gated on its own
  credential and *skipped by name* without it — `--runner openai` used to fall
  through to the mock branch, printing scripted-fixture agreement numbers as a
  calibration report at exit 0, and a calibration number that measured nothing
  is worse than none. Invocations run against a scratch
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
- `toolpolicy.py` — **tool capability gate (slice 5).** agentloop never executes
  an agent's tools: the Claude SDK runs them *inside* `runner.run()` and
  `OpenAICompatRunner` runs none — so the only place a gate can bite is the
  `tools` list handed to the runner. `tools_for` is the single enforcement point,
  called from `agents.run_worker/run_validator/run_planner` before the model call,
  with no post-hoc check on observed `tool_calls` and no use of the SDK's live
  `can_use_tool` callback (which would block a paid call waiting on a human).
  `classify(tool, config)` answers which tier a *marker-named* tool falls in: auto
  (allowlisted read-only), gated (needs a human), or unknown (not a tool). `parse_tool_requests`
  extracts every `TOOL_REQUEST: <tool> (blocking|optional) - reason` marker an agent
  wrote in its output — total and never raises, even inside a paid transaction.
  `effective_tools` computes what one role would actually get given a set of rows,
  pure over its arguments (no store, no writes). `decision_effect` evaluates that
  computation four times (now, if approved, if rejected, if absent) to render what
  approving or rejecting would *really* do — read-only, so a GET built on it does
  not mutate. A grant adds; the gate removes: a request for a tool puts it in the
  list even when the role never declared it, and only withholding removes a declared
  tool — which means withholding one concrete capability disables *every* logical
  tool name that overlaps it (e.g., rejecting `shell` also disables `git`), because
  a gate a human believes is closed but is not is worse than an inconvenient one.
  Fail closed, and the surprising side effect is audited (`tool_capability_withheld`)
  rather than left to be discovered.
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
  `_runner_for(role)` is where provider selection lives (slice 4): it reads the
  role's `AgentSpec.runner` and returns the pinned backend, or `self.runner`
  itself — the same object, not an equivalent — when nothing is pinned, which is
  what makes an unpinned run structurally identical to the pre-slice-4 loop
  rather than identical by inspection. It is here and not in `agents.py` on
  purpose: the `run_*` functions already take a runner and stay a pure "invoke
  this runner" layer, which is what lets `eval` and anything else drive one
  agent with a backend of its own; moving the lookup into them would hand every
  caller the loop's registry policy and put a second job in the module that
  builds prompts. Resolved names are cached in `Loop._runners` (also the
  injection point: `Loop(..., runners={...})`), because `get_runner` builds a
  *new* backend per call and a MockRunner's script is per instance. That
  check-then-act is under `_runners_lock`, so the guarantee is exactly one
  *construction* per name even at `max_parallel_workers > 1` — it does not make
  a backend thread-safe, which is the backend's own contract (`ModelRunner.run`
  states it; `MockRunner` explicitly does not meet it). `_runner_for` also
  validates the pair: a `claude-*` model pinned to the OpenAI-compatible backend
  is a `_ConfigError` naming role and model, since that request can only 404 —
  the "set `runner: openai`, forget `model`" edit was the *default* first-use
  outcome of the very failure `AgentSpec.runner`'s comment claims to design
  away. An unrecognised (non-Anthropic) id only warns: a gateway, fine-tune or
  self-hosted id is exactly what "second provider = second base_url" is for. A
  role missing from the registry resolves to the default rather than raising, so
  `run_summarizer`'s deliberate fallback for an older agents.json still degrades
  instead of crashing.
- `server.py` — REST + SSE dashboard backend, stdlib `http.server` only. The
  append-only `events` table *is* the change feed: SSE is a `WHERE id > cursor`
  query, so reconnects resume losslessly via `Last-Event-ID` and the dashboard
  never mirrors state into a second store.
- `cli.py` — argparse CLI, structured plain output. `charter show|set|clear|
  history` is the human write surface for the project charter; a `ValueError`
  from `charter_set` renders through `main`'s handler as `error: ...`, which is
  the loud write-time refusal that pays for never truncating at inject time.
  `--runner claude|openai|mock` appears on **four** subcommands (`plan`, `run`,
  `serve`, `eval`) and sets only the loop's *default* backend; per-role pins are
  an agents.json decision and are applied on top of it.
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
- **Empty worker output → NEEDS_HUMAN**, checked immediately after the
  `ESCALATE:` test and before anything is stored or validated. An empty output
  is the *absence* of work, and every downstream step treats it as the presence
  of work: the validator would review a blank output against criteria it cannot
  check, and an approve there marks the task DONE — which, under the slice-3
  graph, is precisely what releases dependents to run against upstream output
  that does not exist. `human_approve` refuses a `pending` task for the same
  reason; this is that refusal one step earlier, where the loop rather than a
  human is about to do it. **Escalate, not revise**: emptiness is not a quality
  gap a worker can be told to fix, so re-prompting identically would spend the
  revision budget on a call that already failed silently, and `max_revisions`
  is not touched. A runner that knows *why* the reply is empty raises instead
  (`OpenAICompatRunner` checks for an `error` envelope, null `content`, and a
  `finish_reason` outside `{stop, tool_calls}`); this rule catches the backends
  that cannot tell — `ClaudeSDKRunner` joins its chunks, so a stream carrying no
  text is `""` with nothing to report. Pre-dates slice 4; slice 4's HTTP backend
  made it reachable often enough to be worth a rule.
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
- **Which provider serves a role is not a gate either.** `AgentSpec.runner`
  changes who is asked, never what the answer means: approve/revise/escalate,
  the 0.70/0.40 thresholds, revision counting and the budget cap all read the
  same fields regardless of which backend produced them. A cross-provider run
  therefore takes the identical status transitions a single-provider run does —
  that is the whole point of putting the second provider behind the existing
  seam instead of beside it.
- Both providers' spend lands in **one** task budget. `task_spend` sums every
  attempt on the task whatever ran it, and cost is computed per attempt from the
  *serving* model (`estimate_cost_usd(result.model, …)`, with that model's own
  cache multipliers), so a worker at $3.00 on Anthropic and a validator at $1.25
  on OpenAI trip a $4.00 cap together even though neither trips it alone. A cap
  that only measured the loop's default runner would be a cap with a hole in it.
- A pin naming a runner that does not exist is a **config error, not an infra
  failure**: it escalates to NEEDS_HUMAN naming the bad backend, with no retry
  and no `infra_error` event. Same rule as a missing `planner` role — retrying
  with backoff only burns the clock to reach the same conclusion, and an
  `infra_error` would point the human at the network instead of at agents.json.
  The same classification applies to problems a backend can only discover when
  called: a missing API key, a revoked one (401), a model the endpoint does not
  serve (404), a malformed request (400). Those raise `RunnerConfigError` at the
  seam and `_with_retry` converts them to `_ConfigError` **without** logging an
  `infra_error` — `run()` is invoked *inside* the retry loop, so as ordinary
  exceptions they were three paid round trips reported as a network failure.
  408/429/5xx stay transient and are retried; the provider's own error body
  travels with the message either way, since that is the only place the two are
  distinguishable.
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
  agent kind, tool, truncated input, and `executed`), sourced from
  `RunResult.tool_calls`. `executed` distinguishes a tool that *ran* from one
  the model merely asked for: the SDK path executes what it reports and defaults
  to True, while `OpenAICompatRunner` has no execution loop and reports False,
  and slice 5's auto-approval policy must not read a request as an execution.
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
- Agent-requested tool gate (slice 5): an agent may request a tool via a
  `TOOL_REQUEST: <tool> (blocking|optional) - reason` marker in its output.
  **Read-only requests are auto-approved**, matching the memory approval
  philosophy — a tool the project judges safe to hand over without a human is
  auto-approved and audited (`tool_auto_approved` event), reaching the next
  invocation's tools list. **Side-effecting requests queue for human sign-off**
  (`tool_request_decided` with human approval or rejection). An **optional**
  request queues the row and the task continues without the tool; a **blocking**
  request parks the task at NEEDS_HUMAN, with partial output kept and
  `max_revisions` untouched — not revise/escalate — so the gate is not an
  escalation engine. The escalation reason is prose beginning
  `Awaiting tool approval: ` and names the tools, the request ids and the asking
  agent; there is no reason *code*, and no event kind called `tool_request`
  (the five are `tool_requested`, `tool_auto_approved`, `tool_request_refused`,
  `tool_request_decided`, `tool_capability_withheld`).
  Worker and validator rounds are park-qualified;
  the planner never parks on a tool request (planning is not production work).
  **Approving the pending request returns the task to `pending`** with the row,
  the audit trail and the workspace intact — not a redo, which would throw away
  the work already done and reset the revision count. **A human's rejection
  subtracts the capability**, not just records the denial — so rejecting `shell`
  also disables `git` (they both resolve to the Bash capability) — **which is the
  fail-closed direction**: a human who believes they closed a gate must find it
  closed. The side effect is audited (`tool_capability_withheld`).
- An **undecided** (pending) request for a capability the role already holds —
  via the allowlist, the registry baseline, or `gate_declared_tools` off — is a
  no-op for the *subtraction*: the row is still written and still audited
  (`tool_requested` fires regardless of the knob), it simply takes nothing away,
  because nobody was ever asked. A **rejected**
  row subtracts unconditionally, baseline or not. That distinction keeps an agent
  from revoking its own baseline simply by asking for it (`TOOL_REQUEST: file_read`),
  which would be silent and worse than blocking it. A **machine-refused** row
  (unknown tool name, or over the per-task cap) subtracts nothing: nobody was
  shown a closed gate, and letting refusals subtract would let an agent's own
  chattiness strip its role's baseline with no human in the loop.
- **The gate is not a new decision axis.** No threshold, revision count or budget
  rule reads a tool request. Same register as Slice 4: the capability either
  reaches the next invocation's tools list or it does not, and every downstream
  rule — approve/revise/escalate, the 0.70/0.40 thresholds, revision counting,
  the budget cap — reads the same fields regardless. Same register as Slice 4's
  provider rule: the gate changes *what an agent can do*, never *what its answer
  means*, so a gated run takes the identical status transitions an ungated one
  does. (The requests' own spend is not exempt from the budget cap — a parked
  task's paid worker round counts like any other.)
- **Approving the request is the only decision that releases a parked task**, and
  the three routes that look symmetrical are not. `reject_tool_request`
  deliberately does **not** release: the `parked` flag stays set and the task stays
  NEEDS_HUMAN, because a denial must never silently restart a paid worker run
  against a gap the human just confirmed will not be filled. `pause`+`resume` and
  `human_redo` both requeue the task with the request still **undecided**, so the
  next round pays for a worker call and parks on the same request — `pause`+
  `resume` is neutral about *state* (nothing decided, output and revision count
  intact), which is not the same as being a way forward. The park's escalation
  reason says exactly this, rather than offering "approve or reject" as two
  symmetrical exits; it used to, and rejection was a dead end the reason kept
  recommending.
- **`human_approve` on a parked task is *not* refused — it marks the task DONE**,
  and `human_reject` fails it; both leave the request **undecided** and clear the
  `parked` flag. Kept, not changed: the human is signing off the *partial output*
  the park preserved, which is real reviewed work (unlike the `pending` case
  `human_approve` does refuse), so it is allowed by the same rule that allows
  approving any NEEDS_HUMAN task, and under the slice-3 graph that DONE releases
  dependents as a statement that the partial output is enough for them.
  **What that costs on the dashboard is a UI obligation, not a rule change.** The
  task screen showed the park's own reason — "approving the request is the only
  decision that releases the task" — directly above a task-level **Approve**
  button meaning something else entirely, while the control the sentence asks for
  lived on another tab. Same word, different decision, and the safe one out of
  reach. `TaskDetail` therefore renders the task's own requests inline, through
  the *same* `ToolRequestRow` the queue uses (never a second copy — that drift is
  what four phases of this slice were spent removing), with a banner naming the
  difference whenever a `pending` row carries `parked`. Found by a human looking
  at the screen after every automated pass had cleared it: each reviewer audited
  the *new* panel, and this was the interaction between the new reason text and
  the *old* buttons.
  The same review found the last place on that screen where a **colour** asserted
  something the gate does not do: the status chip went green for `approved`/`auto`
  on the status alone, so an `approved` row whose capability another withheld
  request still subtracts rendered a green chip directly above its own body text
  reading "NOT in force". Green now requires `effect.in_effect` as well — a grant
  that is *live*, not merely one that was *made*. `in_effect` and not
  `capability_live`, because a granted in-process tool (`task_state`) confers no
  concrete capability and is still genuinely in force, so the concrete list would
  strip the green off a real grant.

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
- **Telemetry and human-facing text must never fail an attempt, and must never
  assert more than their inputs prove.** Telemetry is logged inside a paid
  transaction (one `json.dumps` at `log_event` time), so a value the encoder
  rejects rolls back the `finish_attempt` and the retry buys the completion
  again. Every string rendered on the screen (`RunMetrics` fields, event
  payloads, decision effects in the dashboard) must be computed from the gate's
  actual outputs, never from a map or heuristic that knows only part of the
  state. Slice 5 found multiple claims the code could not back: a screen that
  renders "this would grant X" where the gate would not deliver X is a gate a
  human believes is closed but is not, and that is the worse direction.
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
4. ~~Second-provider cross-validator via the ModelRunner seam.~~
   **Done (Slice 4)** — stdlib `OpenAICompatRunner`, per-role pinning via
   `AgentSpec.runner`, resolved in `loop._runner_for`; per-provider cache
   pricing in `config.CACHE_MULTIPLIERS`.
5. ~~Agent-requested tools with an auto-approval policy for read-only ones.~~
   **Done (Slice 5)** — `TOOL_REQUEST: <tool> (blocking|optional)` marker parsing
   in `agents.py`, policy in `toolpolicy.py`, the `tool_requests` ledger table,
   CLI `agentloop tools` surface, dashboard tools panel, and the fixed
   `Store.release_claim` that makes `agentloop redo <id>` recover stranded
   claims in `main`.
6. git-commit-per-task rollback; infra retry/backoff (distinct from "revise");
   batch whole-loop evaluation; coverage in test_runs.
7. Office-metaphor visualization layered on the existing dashboard data.
