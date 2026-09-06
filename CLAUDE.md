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
Plus Slice 6: **durability and whole-loop evaluation** — four additions whose
defining property is what they *don't* touch. (1) **Every task workspace is its
own git repository** (`vcs.py`): one repo per `.agentloop/ws/task-{id}/`, never
a shared timeline, so parallel workers cannot collide on an index; each worker
round commits, DONE writes `refs/agentloop/approved`, and `human_reject` /
`human_redo` roll back to `refs/agentloop/base` instead of leaving or deleting
the round. (2) **Infra retry/backoff verified** at the validator and executor
stages — the behaviour pre-existed in `loop._with_retry`; this slice added the
coverage, not the code, which is why it appears nowhere in the diff of
`loop.py`'s retry path. (3) **Batch whole-loop evaluation**
(`agentloop eval --mode batch`, `eval_runs.kind` discriminating `'verdict'` from
`'batch'`): 9 fixtures driven through a real `Loop`, measuring the final
`TaskStatus` against gold rather than one agent's verdict, mock-only and
refusing a live runner loudly. (4) **Coverage in `test_runs.coverage_percent`**,
parsed out of the test output already captured — no second subprocess — and
`NULL` when nothing was reported.
**No decision rule reads any of it**, and that is the load-bearing claim rather
than a modest one: no status transition, threshold, revision count or budget
check consults a `VcsResult`, a commit sha, a coverage number or a batch result.
A committed AST test walks `loop.py` and fails if one ever does, and
`vcs_enabled=False` is a *proven* behavioral no-op (an enabled-vs-disabled
differential over the full observable state, not an inspection). The hard-won
parts are all containment and honesty about limits, and containment took three
rounds of measurement because each defeated the previous *shape* of guard. The
obvious two conditions ("this workspace is its own repo root") are defeated by a
**junction at `<ws>/.git`** — git derives the worktree from the directory
*containing* `.git`, not from where `.git` resolves to, so the junction reports
the workspace as its own toplevel and passes, measured with the victim
repository's branch rewritten off its own work; hence containment of the
*resolved* `.git` as well. All four path conditions are then defeated by an
ordinary file write to **`<ws>/.git/commondir`**, because git resolves its ref
and object store from that file and `.git` is inside the agent-writable
workspace — so the guard stopped testing paths and **asked git**: `rev-parse
--show-toplevel --absolute-git-dir --git-common-dir`, with both resolved git
locations required to **be** `<ws>/.git` by identity. Containment in
`workspace_root` was the first shape of that check and was itself an escape:
`workspace_root` is the parent of *every* task workspace, so it admitted both a
directory the worker made inside its own workspace and a *sibling task's*
repository. And all five are orthogonal to
**what git runs**: `.git/config` is an ordinary file in the workspace, and
`filter.<name>.clean` / `core.fsmonitor` name programs git executes on an
ordinary round commit — measured as arbitrary command execution outside the
workspace under `allow_test_exec=False`. That axis is closed by the **config
pin**: `init_repo` fingerprints the config git itself wrote, the fingerprint
lives in the store (`vcs_pins`) where no agent can write, and every
side-effecting call replays it and refuses on `"config-changed"` /
`"config-unpinned"`. A denylist was never available — filter names are
arbitrary, so there is no key to pin. Rollback keeps history through a **pre-written**
`refs/agentloop/discarded/<sha>`, because `reset --hard` orphans the commits it
moves off and `git show <sha>` resolves an orphan perfectly well, so
recoverability must be asserted on *reachability* (`git log --all`) and never on
`git show`; the round commit **force-adds ignored files** (`add -A -f`) so that
everything the rollback's `clean -ffdqx` deletes is genuinely recoverable, at the
cost of committing a worker-generated build directory each round; and a
worker-created *nested* repository is a documented residual — git can only record
it as a bare gitlink, so its objects do not survive a rollback, which the
`vcs_rollback` payload names per-rollback via `unrecoverable_nested_repos`
rather than letting the audit log assert a recovery surface that does not hold
the work. Second residual, in the same register: with `vcs_enabled=True` the
operator's `test_command` now runs *inside a git repo* where it previously did
not, which ignore-aware linters, coverage source discovery and `git ls-files`
collectors can all notice. That one is *unprovable here* rather than merely
unproven — the inertness differential runs with test execution off and
structurally cannot see it — so it is documented in README's residual list
instead of claimed away.

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
  `agentloop eval --runner mock|claude --mode verdict|batch`
  (`verdict` = validator calibration; `batch` = whole-loop decision rules,
  mock-only)
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
  rows only, with a blocking ask exempt). Durability knobs (slice 6):
  `vcs_enabled` (default True — off is a *proven* behavioral no-op, not merely a
  documented one), `vcs_command` (default `"git"`) and `vcs_timeout_s` (30).
  `vcs_command` is an executable **path, not a command line** — deliberately
  unlike `test_command`, which is a command line and goes through
  `split_command`; `"git --no-pager"` is therefore one executable name that
  fails as `"git-missing"`, which is why the README's knob table says so rather
  than leaving it to be discovered. Knob validation: `LoopConfig.__post_init__`
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
  graph node, with a local `ref` that expresses edges before db ids exist), and
  `TestResult.coverage_percent` (slice 6): `float | None`, defaulted, where
  `None` is the honest value for "the command reported none" and the type is
  optional rather than `0.0` precisely so nothing downstream can read an absent
  measurement as a low one.
- `store.py` — SQLite source of truth. Tables: tasks (incl. a `control` column:
  run/pause/abort, written only by `set_control`; and a `claimed_by` lease
  column, written only by `claim_next_task`), attempts (per-invocation
  metrics: tokens/cost/wall time, incl. `cache_creation_tokens`/
  `cache_read_tokens`, plus `charter_version`), verdicts (incl. `findings`),
  events (append-only audit log — never UPDATE/DELETE), memory (two tiers
  project/loop; reads gated on `approved`, which a value change revokes; `pinned`
  flag; `last_used_at` set by `memory_read`), memory_hits, charter, test_runs
  (incl. `coverage_percent REAL`, nullable *because* NULL is the honest value —
  a `NOT NULL DEFAULT 0` here would have made "no coverage reported" read as
  "0% covered", and a schema column can only be added, never corrected, since
  `_migrate` only adds), eval_runs (incl. `kind TEXT NOT NULL DEFAULT 'verdict'`
  discriminating per-verdict calibration from a slice-6 whole-loop batch run —
  the default is not a placeholder but the correct value, since per-verdict was
  the only harness that existed when the pre-existing rows were written),
  vcs_pins (one row per task — `task_id` *is* the primary key — holding the
  fingerprint of the `.git/config` `vcs.init_repo` watched `git init` write:
  the pin has to live where an agent has no write path, and `.git/config` is
  inside the workspace it is defending. A live fact with one current value, not
  history, so a re-created workspace *replaces* its pin; an absent row is
  `""`, which every side-effecting `vcs` call refuses on),
  tool_requests (the capability ledger: one row per ask for one logical
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
  Slice 6 adds the second mode: `run_batch_eval` drives each of `BATCH_FIXTURES`
  (9 of them) through a real `Loop` and measures the final `TaskStatus` against
  gold, so "agreement" means *the decision rules landed the task where they
  should have* rather than *the validator returned the right verdict kind* —
  the same word measuring two different things, which is why `eval_runs.kind`
  has to exist rather than the rows merely coexisting. Each fixture gets its own
  in-memory store, scripted `MockRunner`, `allow_test_exec=False` and
  `vcs_enabled=False`, so a batch run is deterministic and touches nothing but a
  temp directory; only the summary row lands in the real store. **Mock-only, and
  it refuses a non-mock runner loudly** — a batch run against a live provider
  would be measuring the model rather than the rules, and `eval`'s own history
  (the `--runner openai` fall-through that printed scripted numbers as a
  measurement) is the reason that refusal is loud instead of a silent fallback.
  The fixture list tracks the decision rules: a slice that adds a rule adds a
  fixture.
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
  Slice 6 adds two things here. `_has_any_file` now **skips `.git`** (matched on
  the path *component*, so `src/.gitignore` still counts): every workspace is a
  repo now, and counting git's own objects would stop a genuinely empty
  workspace reporting `status='na'` and instead run the test command against
  nothing — the one way per-task repos could silently move the *tests gate*, the
  single thing this slice promised not to touch. And `parse_coverage(output)` is
  a pure, total function returning `None` or a float in `[0, 100]`, where `None`
  means "no coverage was reported" and never "0%". **Its ambiguity resolves the
  opposite way from `agents._extract_findings`**, deliberately: over-reading
  findings stores stray prose a human reading a verdict can discount, whereas a
  fabricated coverage number renders on the dashboard as a *measurement*, so two
  disagreeing TOTAL rows (a multi-suite run) are `None` rather than a guess. The
  text it parses includes model-written output — a worker can print
  `TOTAL … 100%` — which is tolerable only because no decision rule reads it;
  promoting it to evidence would need a different source. `clear_workspace` also
  gained a read-only-retry handler: `rmtree(ignore_errors=True)` cannot delete
  the read-only objects git writes, so before slice 6 fixed it the redo fallback
  left the workspace standing with the previous round still in it.
- `vcs.py` — **per-task workspace version control (slice 6).** One throwaway git
  repo per `.agentloop/ws/task-{id}/`, no remote and no shared timeline, so
  `redo`/`reject` recover the discarded round instead of destroying it.
  Deliberately *not* in `executor.py`, but **not** because the threat model is
  narrower — it is a different shape. `executor.py` runs arbitrary AI-generated
  code; this module runs its own fixed commands, but it runs them *inside a
  directory that code writes to*, so **both** axes are live and the module
  defends both. **Aim** — in a workspace with no `.git`, `git rev-parse
  --show-toplevel` walks up and answers with the operator's real repository, so
  an unguarded `add -A && commit` stages and commits their whole working tree.
  **Execution** — a fixed argv is not a fixed *program list*: git config names
  programs git runs, and `.git/config` lives inside the workspace.
  Hence `_guard`'s four *path* conditions before any side-effecting command:
  `<ws>/.git` is a directory, git's toplevel for that cwd *is* the workspace, the
  resolved workspace is inside `workspace_root`, and the **resolved location of
  `<ws>/.git`** is too. The fourth is not a restatement of the third — git
  derives the worktree from the directory *containing* `.git`, so a junction at
  `<ws>/.git` pointing at the real repo passes the first three (measured, branch
  pointer rewritten). Any error computing any condition refuses: a guard that
  errors is a guard that says no.
  **And then it asks git where it actually is, which is the layer to leave
  alone.** All four conditions above test *filesystem paths*, and a path test is
  a guess at what git will make of the path. `.git` is inside the
  agent-writable workspace and git reads its ref and object store from
  `.git/commondir`, so **one ordinary file write** at `<ws>/.git/commondir` — no
  junction, no gitfile, no environment variable — redirected every ref and
  object write into the operator's repository with all four path conditions
  holding: measured, guard True, `commit` ok=True, the victim's branch pointer
  rewritten. A fifth path check would be the same mistake one level deeper, so
  the guard's single `rev-parse --show-toplevel` is
  `rev-parse --show-toplevel --absolute-git-dir --git-common-dir` and **both**
  resolved git locations must **be** `<ws>/.git` by identity — *not* merely lie
  inside `workspace_root`, which is what this check required at first and which
  was itself an escape: `workspace_root` holds every task workspace, so
  containment admitted a directory the worker made inside its own workspace
  (redirecting the parsed config while the pinned file stayed byte-identical)
  and a sibling task's repository (aiming one task's `reset --hard` /
  `clean -ffdqx` at another's history). Reading the location
  git *resolved* rather than the one the filesystem suggests closes commondir,
  gitfiles, `GIT_DIR`, worktree links and the next redirect at once — which is
  why the extra two outputs are not redundant, and why collapsing them back to
  `--show-toplevel` reopens a critical.
  **The config pin closes the execution axis, and it is an allowlist because a
  denylist cannot exist here.** `filter.<name>.clean` and `core.fsmonitor` name
  programs git executes on an ordinary `add`/`commit`, and a filter name is
  arbitrary — there is no key to enumerate, so "block the dangerous settings" is
  not a reachable design. Measured before the pin, with the worker writing only
  inside its own workspace: `git config --local filter.pwn.clean <python>` plus
  a `.gitattributes`, guard True before *and* after, `commit` ok=True, canary
  written outside the workspace; `core.fsmonitor` did it with no `.gitattributes`
  at all. Note the configuration that bit: `allow_test_exec=False` (the
  operator's explicit *"execute nothing the agent wrote"* switch) with
  `vcs_enabled=True` (the default), from a call the loop makes **every round**
  and from `human_reject`/`human_redo` on the dashboard — so the durability
  feature was handing back the execution the sandbox switch had just refused.
  So `init_repo` **mints** a pin — `config_pin(ws)`, a bounded read and a
  blake2b of the `.git/config` git itself just wrote — and returns it on
  `VcsResult.pin`; the caller records it in the **store** (`Store.set_vcs_pin`),
  which is the one place in this system an agent has no write path to, and a pin
  kept anywhere under `.git/` or the workspace would be exactly as writable as
  the attack it is meant to catch. Every side-effecting entry point takes
  `pin=` and refuses on a mismatch, an unreadable config or a missing pin
  (`"config-changed"` / `"config-unpinned"`), and the check runs **before any
  subprocess** — it is a file read and a hash, cheap enough for the per-round hot
  path, and nothing may spawn under a config this module did not write — a
  guarantee that holds only in union with `_guard`'s **git-reported** check (the
  fifth, after the four path conditions above), which
  require the resolved git dir *and common dir* to **be** `<ws>/.git` by
  identity rather than merely to sit inside `workspace_root`: git parses
  `config` from the common dir, so containment alone let a worker redirect
  `.git/commondir` at a directory inside its own workspace and own the parsed
  config while the fingerprinted file stayed byte-identical (and let `<ws>/.git`
  resolve into a *sibling task's* repository). The read is also of a **regular**
  file only — `stat()` reports `st_size == 0` for a FIFO, so the size bound
  alone would pass one and block the read forever, with no timeout. Three
  properties make it hold rather than merely look like it holds: git was
  *measured* never to rewrite `.git/config` during any command this module
  issues (`init`, `add`, `commit`, `update-ref`, `status`, `rev-parse`,
  `reset --hard`, `clean`), so a change is always somebody else's; the mint
  happens on exactly the branch that *creates* the repo, never on one it found,
  because re-pinning an existing repository would bless the edit on the next
  round and turn the gate into the attacker's signature; and `""` is checked
  first, so an unpinned caller cannot match an unreadable config by comparing
  `""` to `""`. `is_repo` deliberately takes no pin — it is a containment
  observation with no side effect, so a True there does not promise a later
  `commit` is accepted. `rollback` writes
  `refs/agentloop/discarded/<sha>` **before** anything moves — named for the sha
  and not a counter, because a counter is a read-then-write that two rollbacks
  can collide on and `update-ref` overwrites silently, losing exactly the history
  the ref exists to keep while reporting `ok=True`. The identity is passed per
  invocation (`-c user.name/-c user.email`) and the operator's config is
  neutralised twice over (`GIT_CONFIG_GLOBAL=os.devnull` plus
  `-c commit.gpgsign=false -c core.hooksPath=`, `-c init.templateDir=` on init):
  `GIT_CONFIG_NOSYSTEM` leaves `~/.gitconfig` live, where a plain
  `commit.gpgsign=true` was measured to fail every commit and a `core.hooksPath`
  hook to run inside the workspace and write outside it. Its own
  `_child_env()` over `_GIT_ENV_ALLOWLIST`, not `executor._child_env` (a
  *method*, reading `self.env_allowlist`) and taking no config, because the one
  knob it could read is `sandbox_env_allowlist` — the test sandbox's knob, which
  would let an operator re-admit precisely what this env removes. **Every entry
  point is total**: `init_repo`/`commit`/`mark_approved`/`rollback` always return
  a JSON-encodable `VcsResult` and `is_repo` always returns a `bool`, so a caller
  may ignore the result entirely — durability must never fail an attempt.
  `VcsResult.reason` is a closed vocabulary (`"disabled"`, `"git-missing"`,
  `"no-workspace"`, `"git-failed"`, `"timeout"`, `"not-a-workspace-repo"`,
  `"config-unpinned"`, `"config-changed"`, `"already"`, `"residue"`,
  `"unrecorded"`) and is `""` *exactly* when nothing degraded;
  `"no-workspace"` exists because `subprocess.run` raises `FileNotFoundError` for
  a missing `cwd` exactly as for a missing executable, and reporting that as
  `"git-missing"` told an operator to install git they already had.
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
  The slice-6 durability wiring is **six call sites and no logic**: `run_task`
  initialises the repo once per invocation and commits each round, carrying the
  workspace's config pin — read from `Store.vcs_pin`, recorded through
  `set_vcs_pin` on the one branch that *creates* a repo (`VcsResult.pin` is
  non-empty only there, including on a failed init, since a pin nobody recorded
  is a workspace nothing can act on again) and replayed on every later call; `_vcs_mark_approved`
  moves `refs/agentloop/approved` when a task reaches DONE — by the rules *or*
  by `human_approve`, which for `risk_level >= human_review_risk_level` is the
  only route to DONE, so omitting it would leave the approved ref absent for
  exactly the tasks a human vetted — and `_vcs_rollback_to_base` returns the
  workspace to `refs/agentloop/base` on `human_reject` and `human_redo`. Always
  to `base`, never to the approved ref: that ref is a bookmark a human placed,
  and rolling onto it would let a reject of a later round silently resurrect an
  earlier approved one. Every call is *call, log, discard*; the one branch
  (`human_redo` choosing between the rollback and `clear_workspace`) reads
  `VcsResult.ok`/`reason` to pick a **filesystem shape**, never a status. Two
  invariants make that structural rather than stylistic: **no git subprocess runs
  inside `Store.transaction()`** (the connection lock is held across execute and
  commit, so a 30-second `vcs_timeout_s` in there would stall every dashboard
  reader, and a raise would roll back the paired audit event) — so each `vcs_*`
  event is a standalone `log_event`, correct because it is a pure audit fact with
  no row change to pair with; and `_vcs_degraded` excludes `"disabled"` and
  `"already"` from the degradation path, because a deliberate configuration is
  not a degradation and an idempotent no-op is a success — logging them would
  have put a row and a `RuntimeWarning` on each of the ~300 loop tests that run
  with vcs off. `human_redo`'s fallback is likewise not unconditional: when a
  rollback fails *after* recording the discarded tip, the wipe is skipped and the
  gap audited (`history_preserved`), since `clear_workspace` would rmtree exactly
  the ref `vcs.rollback` had just refused to lose, under a warning reading
  "nothing happened".
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
  an agents.json decision and are applied on top of it. `eval` additionally
  takes `--mode verdict|batch` (slice 6); `--mode batch` with a non-mock
  `--runner` is refused with a non-zero exit rather than quietly downgraded.
- `web/` — Vite + React + TypeScript dashboard. `types.ts` mirrors the server's
  JSON shapes; keep them in sync when changing an endpoint. `test_runs` reaches
  the API wholesale (`server.py` returns the rows), so `coverage_percent` needed
  no server change — and `TaskDetail` renders it **only when it is not null**,
  with no placeholder at all, because a dash or a `0%` in that slot would show an
  absent measurement as a measured one. The three `vcs_*` event digests in
  `EventFeed` follow the same rule at a finer grain: `discarded_sha`,
  `discarded_ref`, `degraded` and `unrecoverable_nested_repos` are **absent, not
  null**, when they do not apply, so each is tested before it is rendered and the
  nested-repo line names the gap rather than implying a recovery surface.

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
- **Slice 6 adds no decision rule**, and the negative is stated here rather than
  left implicit because a durability layer is exactly the kind of addition that
  grows one by accident. No status transition, threshold, revision count or
  budget check reads a `VcsResult`, a commit sha, `test_runs.coverage_percent`
  or a batch-evaluation result; every `vcs.*` call in `loop.py` is *call, log,
  discard*. Unlike slice 4's provider rule and slice 5's gate rule, which are
  argued, this one is **enforced**:
  `test_vcs_loop.py::test_no_status_write_is_downstream_of_a_vcs_result` walks
  `loop.py`'s AST and fails if a vcs result ever flows into a status write, and
  `::test_vcs_disabled_and_enabled_produce_identical_observable_state` runs the
  same task with the feature on and off and diffs the whole observable state, so
  `vcs_enabled=False` is a proven no-op rather than a documented intention.
  Coverage is a *display value*, not evidence — nothing may promote it to
  evidence without a different source, since the text it is parsed from includes
  model-written output a worker can fabricate.
- What slice 6 *does* change is the **workspace contract of `reject` and
  `redo`**, which is a behaviour change with no status attached and therefore
  belongs here as well as in the README. `human_reject` now rolls the workspace
  back to `refs/agentloop/base` instead of leaving the rejected round in place,
  and `human_redo`'s "fresh start, no carried context" now means *emptied*
  rather than *destroyed*: `vcs.rollback` writes
  `refs/agentloop/discarded/<sha>` at the tip before anything moves, so the
  discarded round stays reachable from `git log --all`. The rollback target is
  always `base` and never the approved ref, because the approved ref is a
  bookmark a human placed and rolling onto it would let a reject of a later round
  silently resurrect an earlier approved one. The statuses, the revision count
  and the audit trail are exactly what they were; only what is left on disk
  differs — and on a pre-slice-6 workspace with no `.git`, not even that: the
  guard refuses, reject leaves the tree untouched and redo falls back to the
  wipe, which is the pre-slice-6 behaviour precisely.

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
6. ~~git-commit-per-task rollback; infra retry/backoff (distinct from "revise");
   batch whole-loop evaluation; coverage in test_runs.~~ **Done (Slice 6)** —
   `vcs.py` gives every task workspace its own git repo (round commits,
   `refs/agentloop/base`/`approved`, and a pre-written
   `refs/agentloop/discarded/<sha>` that is what makes a rolled-back round
   recoverable), wired at six call sites in `loop.py` that read nothing;
   `agentloop eval --mode batch` + `eval_runs.kind` measure the decision rules
   end to end; `test_runs.coverage_percent` records what the test output already
   reported, NULL when it reported none. The infra-retry half was **already
   deduped into the decision-rules section in slice 0** — `loop._with_retry`
   shipped then and this slice changed none of it; what it added is the missing
   coverage at the validator and executor stages, which is why "verified" and
   not "implemented" is the honest word for that quarter of the item.
7. Office-metaphor visualization layered on the existing dashboard data.
   **Built and verified on branch `slice-7-office-view`**, unmerged pending a
   human eyeball pass (`temp/slice7-ui-checklist.md` + the seeded
   `temp/demo-slice7.db`). No agent in this project can verify rendered `web/`
   output; that is a structural gap, not a temporary one.
8. ~~Hardening and finalization pass before first real use.~~ **Done (Slice 8)**
   — see below.
9. ~~Run agentloop against an existing repository, on its own branch, with its
   real test suite.~~ **Done (Slice 9)** — see below. `workspace_mode:
   "worktree"` gives each task a real `git worktree` checkout of `repo_root`
   instead of an empty scratch directory; scratch mode (the default) is a
   **proven** no-op, in the same register as `vcs_enabled=False`.

## Slice 8: hardening and finalization

A full adversarial review of every module (four independent reviewers) before
the project was used against a live provider for the first time, plus every
finding it produced. The framing that matters: **the repo's own database held 0
tasks and 0 attempts**, so nothing here had ever been driven end to end by a
real model — and three of the criticals sat on exactly that path, invisible to
740 passing mock-based tests. 878 tests now; every fix landed with a regression
test that was watched failing first, and the two guards worth doubting were
falsified by neutering the mechanism and confirming the test went red.

**Three criticals.**
- **`server.py` accepted cross-origin mutations.** An unauthenticated mutation
  API on a documented default port with no `Origin` and no `Host` check. A
  browser *simple request* (`Content-Type: text/plain`, no preflight, permitted
  cross-origin) reached every POST route: measured, `POST /api/charter` from
  `Origin: http://evil.example` returned 200 and replaced the charter body —
  which `agents._charter_block` injects verbatim into every worker, validator
  and planner prompt, making it remote prompt injection into an agent holding
  `file_io`, `git` and Bash. `POST /api/tasks` returned 201; `Host:
  attacker.example.com` returned 200 with full task bodies, so DNS rebinding was
  enough to read goals, worker output and `test_command`. Both conditions are
  now checked before routing, on GET as well as POST, and they are two
  conditions because they stop two different attacks: `Origin` catches the
  browser that knows it is elsewhere, `Host` catches rebinding, where the
  browser believes the attacker's name *is* this server and so sends no foreign
  `Origin` at all. An **absent** `Origin` is accepted — only a browser sets one,
  and a browser cannot omit it cross-origin, so requiring it would break curl
  and the CLI in order to stop nothing. An IP-literal `Host` is accepted because
  rebinding needs a *name* to re-resolve, which keeps `--host 0.0.0.0` working.
  Deliberately not a token: an attacker already executing code on this machine
  is out of scope here exactly as they are for `executor.py`'s env scrub.
- **The default runner had neither money guard its sibling shipped with.**
  Every totality and never-zero guard was written for slice 4's
  `OpenAICompatRunner` and never back-fitted to `ClaudeSDKRunner`. So
  `extract_usage` used a bare `int()` and raised on a wrong *type* (`'n/a'`, a
  nested dict, NaN) — **after** the stream completed and the completion was
  billed. `agents._invoke` reaches `finish_attempt` only on a clean return, so
  the tokens and cost were discarded and `loop._with_retry`'s bare
  `except Exception` bought the same completion again, up to
  `infra_max_retries + 1` times, reported as an `infra_error` pointing the
  operator at their network. `extract_tool_calls` had the same shape on
  `message.content`. And with no never-zero guard, four silent zeros reached
  `attempts` as a measured $0.00 with `usage_estimated=False` and no
  `runner_warning` — so `_budget_tripped` could never fire, `_maybe_handoff`
  could never fire, and the dashboard rendered the fabricated number as spend.
  Both extractors are now total, and the never-zero block is **one shared
  helper** (`runner.never_zero_usage`) rather than a second copy, because a
  second copy is what drifted the first time. Found independently by two
  reviewers, which is this project's strongest signal.
- **`vcs` was inert on every default install.** `_run` sets `cwd=<ws>` and
  `_git` also appended `-C <ws>`, so git changed into the workspace and then
  resolved the same *relative* path again from inside itself. The shipped
  `workspace_root` is relative (`.agentloop/ws`), so every side-effecting call
  returned `git-failed` and the whole of slice 6 — round commits, the approved
  ref, recoverable reject and redo — never ran, announced only as one
  `RuntimeWarning` per task saying the task "ran without durability". **Every
  one of ~2900 lines of vcs tests used an absolute `tmp_path`**, which is why a
  green suite proved nothing here. Fixed with `os.path.abspath` on the `-C`
  value — lexical, deliberately not `Path.resolve()`, which would follow a
  junction at `<ws>` and quietly take over the one decision `_guard` exists to
  make. The regression test drives the full lifecycle from a relative root and
  is paired with a relative-vs-absolute differential.

**The rest, in the order they will bite an operator.**
- **`agents._VERDICT_RE` rejected ordinary LLM markdown.** Five of six realistic
  formats — per-field emphasis, comma or pipe separators, a bare `.95`, `n/a`
  for `na`, a percentage — parsed as `ESCALATE` at confidence 0, which is below
  `severe_threshold` and so goes straight to NEEDS_HUMAN with no revision round,
  recording "Unparseable validator output" over a verdict that had actually
  approved. `toolpolicy._MARKER_RE` had already won this argument (LLM output is
  markdown); the parser driving an automatic state transition had none of that
  hardening. **Decoration and separators widened, semantics untouched**: the
  three verdict kinds, the three tests values and the requirement that all three
  labelled fields be present are unchanged, prose that merely sounds like an
  approval still escalates at 0, and nine "not a verdict" controls pin that.
- **An undecided tool request revoked a capability the role already held.**
  `effective_tools` exempted a pending row by *logical name* while
  `subtract_withheld` works over the *concrete* footprint, and
  `LOGICAL_TOOL_MAP` is not injective. Measured with the shipped worker spec: a
  pending, **optional** `shell` — nobody's decision, never shown to a human —
  cost the role its declared `git`, because both resolve to `Bash`. The trigger
  is the shipped system prompt's own worked example (`TOOL_REQUEST: shell
  (blocking)`). The exemption is now decided in the same currency as the
  subtraction. Only the *pending* side widened: a **rejected** row still
  subtracts unconditionally, which is the fail-closed half and what stops an
  agent revoking its own baseline by asking for it. Five existing tests encoded
  the name-level approximation and were rewritten rather than the fix being bent
  to them — CLAUDE.md's rule says "a capability the role already holds", and the
  later E3/G1 remediation block says the same; those five predate it and were
  never reconciled. Each rewrite is annotated with that reasoning, and each now
  asserts a *more* honest answer than it did (the CLI went from promising
  "approving grants Bash" to stating "approving changes nothing — this role
  already has Bash").
- **The sandbox's two stated bounds were not bounds.** `capture_output=True`
  materialised the whole stream and truncated afterwards — measured, 331 MB in
  4 s (662 MB peak), extrapolating to ~9.8 GB at the default timeout, to keep
  4000 characters; three lines of generated test code could OOM-kill the loop
  and take a paid completion with it. And `subprocess.run`'s timeout kills only
  the direct child, then blocks in `communicate()` until every inherited pipe
  handle closes: a grandchild holding stdout defeated a 3 s timeout for 20.3 s,
  and one that never exits blocked forever, inside `_with_retry`, holding the
  claim. `_run_bounded` now reads into a bounded ring buffer in a daemon thread
  and kills the whole **process tree** (`taskkill /F /T` on Windows, a POSIX process group elsewhere).
  The timeout summary reports the *measured* wait as well as the requested one.
- **A missing registry role wedged the whole batch.** `Registry.load` replaces
  the defaults wholesale with no merge and no missing-role check, so a
  hand-edited `agents.json` can leave `worker` undefined; `registry.get` then
  raised a bare `KeyError` from `_maybe_handoff`, matching neither handler and
  escaping `run_task`. Measured: the batch aborted, task 1 sat `in_progress`
  holding its lease with an empty `escalation_reason`, everything behind it
  never ran, and the next `agentloop run` died identically — permanent
  starvation with one stderr line as the only signal. A missing `validator`
  failed differently and no better: three paid retries and an `infra_error`, the
  misdiagnosis CLAUDE.md explicitly names. Both roles are now resolved up front
  exactly as `plan()` resolves `planner`.
- **A claim failure in a parallel worker was swallowed and reported as
  success.** `_run_parallel.drain`'s guard covered `run_task` but not
  `claim_next_task`, which opens a write transaction and so raises
  `OperationalError: database is locked` whenever two `agentloop run` processes
  share a database. Measured at `max_parallel_workers=3`: `run()` returned 1 and
  raised nothing while two threads died and two tasks were silently dropped. The
  sequential path propagates, so the two modes disagreed about what a failed
  batch even looks like. The whole drain body is now guarded.
- **`resume` left the pause message on the row forever.** `set_status(...,
  reason="")` assigns only a *truthy* reason, and `pause` stamps "Paused by
  human; resume to continue." onto every task it touches — measured still
  reading it on a `done` task. Four sibling release-to-PENDING paths already
  blanked it explicitly; `resume` was the fifth and the only one missing the
  line, and it is the one CLAUDE.md names as the *neutral* exit from a
  tool-request park.
- **`max_tokens_per_task` escalated a successful first task.** Measured on a
  real `--runner claude` run of the README's own quick-start task: the validator
  returned `approve` at 0.85 and the task escalated with "Budget cap exceeded
  (tokens=556515, cost=$0.49)" — the *token* cap tripping at under a tenth of
  the cost cap. Raised to 3M. The **rule is deliberately unchanged**: cache
  reads still count toward the token total and are still priced at 0.10x on the
  cost side. This was a badly chosen default, not a wrong rule, and changing
  what the number counts would have rewritten CLAUDE.md, the README table and
  the tests to fix a constant.
- Smaller, all measured: `/api/stream` answered 500 for a malformed cursor where
  `/api/events` answered 400, on the endpoint where `Last-Event-ID` is
  client-supplied on every reconnect; an unknown `/api/*` GET returned 200 and
  the dashboard HTML; static containment was a string *prefix* test rather than
  `is_relative_to`, so a `dist`-prefixed sibling passed; `TaskDetail` used
  `events.length` as its refetch trigger and that array is capped, so after 300
  events the detail pane froze forever, showing a stale status directly above
  the Approve button; `.card:hover`/`.card.selected` set the `border-color`
  shorthand at higher specificity than the status map, so the *selected* card
  lost its status colour; a malformed `test_command` raised `ValueError` into
  `_with_retry` and became three `infra_error` retries (now refused at
  construction, where `cli.main` renders it as `error: …`); the child `PATH`
  omitted the running interpreter's script directory, so the default `pytest -q`
  could not resolve when `agentloop` was invoked by path as the README offers,
  so the command failed every round and burned `max_revisions` on a gap no
  worker could close (see the round-2 note below: an earlier version of this
  sentence claimed `status="error"` falls back to the validator's `TESTS:`
  claim, which `TestResult.passed` disproves); `run_metrics`'s `by_model` rollup counted unfinished
  attempts while the headline totals filtered them, so the two never reconciled;
  `config.load` reported an unknown key (a typo'd budget cap) with `print`,
  which is strictly weaker than the `warnings.warn` this project already calls
  insufficient; six `run_task` escalation exits returned the in-hand `Task`
  rather than re-reading, the pattern `human_approve` documents as wrong;
  `git init` could reinitialise another repository through a gitfile at
  `<ws>/.git`, contradicting its own docstring's containment claim; `serve
  --runner` was an inert flag that read as "the dashboard will drive real work";
  and CLI output raised `UnicodeEncodeError` when redirected on Windows, which
  `main`'s `(KeyError, ValueError)` handler swallowed into `error: charmap`.

**A second review round, and what it caught in the first round's own fixes.**
The fixes above were themselves put through two independent reviewers, and that
round found three defects *introduced by the repairs* — which is the argument for
the round, not against it.
- **The widened verdict parser had turned a fail-safe non-match into a
  fail-open maximum.** The pattern accepts any magnitude, and the first version
  *clamped* out-of-range values instead of rejecting them — so `CONFIDENCE: 95`
  (a percentage with the sign dropped) became `1.0`, the top of the scale,
  clearing both thresholds and marking a task DONE with no human. `CONFIDENCE:
  40` rewrote a validator's severe-threshold judgement into certainty the same
  way. A clamp is not a rejection: it substitutes the **most permissive legal
  value** for one the model never wrote. Out-of-range is now an unparseable
  verdict, exactly as it was before the widening. The test that let this
  through asserted `v.confidence <= 1.0`, which cannot fail for a clamped
  value — a hollow assertion is worse than none, because it reads as coverage.
  Its replacement asserts against the **decision thresholds** and was watched
  going red on all five inputs with the clamp restored.
- **The same-origin guard accepted `Origin: null`.** Measured on the first
  version: a cross-origin `POST /api/charter` carrying `null` returned 200 and
  replaced the charter — the very attack the guard was written to close. A
  browser sends the literal `null` for an *opaque* origin (a sandboxed iframe,
  and any redirect chain that crossed origins, which a 307 survives with method
  and body intact), so it is a real cross-origin request that declines to name
  itself, not an absent one. An absent `Host` passed for the same reason and is
  closed with it.
- **The tool-capability exemption was a subset test where it needed to be an
  intersection.** `LOGICAL_TOOL_MAP` has *partial* overlaps as well as exact
  collisions: `file_io` -> `[Read, Write, Edit]`, `file_read` -> `[Read]`, and
  the shipped **planner** declares `file_read`. So an undecided, optional
  `TOOL_REQUEST: file_io` still stripped the planner's `Read` — the same
  self-revocation, one collision pair over from the `git`/`shell` case that had
  been measured. A capability held through a human's **grant** was not exempt
  either, so with `gate_declared_tools=True` a pending ask could revoke what a
  human had just approved. The reviewer also proved the tool-policy suite could
  not see any of this: with the exemption removed entirely, all 215 tests still
  passed. It now fails in *both* directions — too permissive and too strict —
  and that was verified by neutering each way.

Three more from the same round, each a place where a degradation existed and
nothing recorded it — the project's own standing rule is that a warning nobody
sees in `agentloop events` is unrecorded. A garbage **cache**-token field was
coerced to 0 and written as a *measurement*: nothing estimates the cache fields,
so `estimated` stayed empty, `note` stayed empty, `usage_estimated` stayed False
and no `runner_warning` fired — and per the decision rules the token total
includes cache reads, so on a cache-heavy run that is the dominant term of the
budget cap. `coerced_usage_fields` now reports it (and note the failure mode
worth remembering: the reporter was written, and then *not called* — dead code
that reads as a fix, which is why its test was written to fail against exactly
that state). A typo'd `loopconfig.json` key now logs a `config_warning` event
from `Loop.__init__`, the first place in that path with a store. And
`_run_parallel` recorded only `errors[0]`, discarding every other worker's
exception on the one path whose whole purpose is that a dying worker must not be
silent; each is now logged as `worker_failed` before the first is raised.

Two smaller ones from the same round, both places the code and its own prose had
drifted apart: the `` added to stop `TESTS: nap` reading as `na` had silently
narrowed `TESTS: passed` / `failed` out of the grammar, on a slice whose stated
purpose is surviving ordinary formatting; and the executor comment justifying the
`PATH` fix claimed `status="error"` falls back to the validator's `TESTS:` claim,
when `TestResult.passed` returns `False` for it — the honest consequence is that
an unresolvable command fails every round and burns `max_revisions`. The
documented "Windows job kill" is `taskkill /F /T`, which walks the live
parent-PID chain and therefore misses a reparented orphan; that residual is now
named rather than claimed away.

**A third round, from the integration verifier**, which ran 18 scenarios against
the finished tree — reproducing the pre-fix `vcs` failure, driving the
cross-origin attacks over raw sockets against a real server, and neutering all
three round-2 mechanisms to confirm the suite goes red in each direction. 16
passed; the two that failed were both honesty gaps rather than exposure, and
both are the same shape as everything else this slice found.
- **The cache-field reporter was wired into both backends and could only see
  one of them.** `coerced_usage_fields` scanned top-level keys ending in
  `tokens`. Anthropic reports its cache counts there; **OpenAI nests them**
  under `prompt_tokens_details.cached_tokens`, and `prompt_tokens_details` does
  not end in `tokens` — so on that backend a garbage cached count was invisible
  while `extract_openai_usage` coerced it to 0 and recorded that as measured.
  The docs said the reporter covered both, and a reporter present on a path but
  structurally unable to read that path's data shape *reads as coverage*. It now
  descends one level and names the field as `parent.child`. Note the direction,
  because it decides the severity: with `cached` at 0, `tokens_in = prompt -
  cached` becomes the **full** prompt at the full rate, so this over-billed and
  tripped the cap early rather than under-measuring.
- **A correction reached the code and not the prose.** The executor comment
  wrongly claiming `status="error"` falls back to the validator's `TESTS:` claim
  was fixed in round 2 — but the same sentence survived in `README.md` and in
  CLAUDE.md's own round-1 list, so this document contradicted its own
  correction. `TestResult.passed` returns `False` for `"error"` as well as
  `"fail"`; only `"na"` falls back. The real consequence, and the one an
  operator needs, is that an unresolvable test command fails *every* round and
  burns `max_revisions`.

The verifier's own caveat is worth keeping: all of this remains mock-driven.
The framing at the top of this section — 0 tasks, 0 attempts, three criticals on
the one path 740 tests could not see — applies to the verification too. The
nested-cache gap is precisely that class of bug: a data shape nobody had a live
sample of. Watch the first few real runs' `attempts` rows and `runner_warning`
events.

**What was checked and found sound**, because a review naming no confirmed
property is not a review: all 43 `store.py` write sites are transactionally
paired and the `events` table is genuinely append-only (verified by AST walk);
the claim is a real compare-and-swap; every decision rule in the section above
is enforced where it is documented; the tests gate reads executed truth; the
OpenAI auth path (call-time key, no redirect, https-or-loopback) and its
permanent-vs-transient classification are correct; prompt assembly matches the
spec and an absent charter is byte-identical; `_invoke`'s coercion barrier is
above the closing transaction; `parse_coverage`, `parse_tool_requests` and
`_extract_findings` are total. **No refactor was recommended**: `loop.py` and
`store.py` were both judged deep modules earning their size, and splitting them
would scatter the transaction, CAS and lease discipline across four callers.

**No decision rule changed.** The thresholds, revision counting, the budget-cap
rule, the tests gate, the tool-gate direction and the provider rule all read the
same fields they did before. What changed is that several of them can now
actually be reached: a verdict that parses, a token count that is measured, a
budget cap that is not tripped by its own default, and a durability layer that
runs at all.

## Slice 9: existing-repository workspaces

`executor.workspace_for` did `mkdir` and returned; nothing ever put the
operator's code in a task's workspace, and `ClaudeSDKRunner.build_options`
never passed `cwd` even though the SDK accepts it — so the worker's prompt
said "write under the workspace" while its actual working directory was the
orchestrator's own repository. Measured consequence: the tests gate silently
disabled itself, because an empty workspace makes `executor._has_any_file`
return False, the result is `status='na'`, and `na` is the one value that
falls back to the validator's unverified `TESTS:` claim — the single gate
that reads executed truth stopped reading anything. Three phases (P1-P3) built
the pieces with `loop.py` either untouched or touched only for the
scratch-mode-reachable half (P1's `cwd` fix, P2's H3 detection scaffold); this
phase (P4) is what turns worktree mode on by threading `repo_root` through
every `vcs.*` call site `loop.py` already had.

**Approach: one git worktree per task**, created on its own branch from the
configured base ref, sharing the object store — chosen over a clone because it
does not copy history and the result is already a branch of the operator's
repository, no export step. `workspace_mode: "scratch"` (default) is
unchanged and is a **proven** no-op in the register of `vcs_enabled=False`: a
whole-state differential
(`test_scratch_mode_is_identical_whether_or_not_worktree_knobs_are_set`,
[NEUTER]) runs the identical scripted task with every worktree knob
(`repo_root`/`worktree_root`/`vcs_base_ref`/`vcs_branch_prefix`) filled in with
real, reachable values against a run with none of them set, and diffs the
whole observable state — plus a control proving the same knobs, with
`workspace_mode` flipped to `'worktree'`, genuinely touch the operator repo
(a branch is created), so the equality above is a real differential and not
two runs that both ignored everything.

**Four measured probes shaped the design** (`vcs.py`'s own docstrings carry
the detail; this is the summary). Worktrees of one repository share **one ref
namespace** — an update to the base ref written inside one worktree is
immediately visible from every sibling and the main repo — so refs are now
per-task functions (`vcs.base_ref(task_id)`, `vcs.approved_ref(task_id)`,
`vcs.discarded_ref_prefix(task_id)`), constants only when `task_id` is `None`
(scratch mode). The local config setting command run from inside a worktree
writes the **main** repository's config, not a config under the worktree at
all — so the config pin fingerprints `<repo_root>/.git/config`, and because
every task of one repository shares that one file, P2's remediation made it a
**repository-level baseline** (`Store.vcs_repo_pin`/`vcs_repo_pins`, keyed by
`repo_root`) rather than a per-task mint: a per-task pin let an ordinary
later task's `init_repo` re-bless a config an earlier task had poisoned,
measured as arbitrary command execution outside every workspace under
`allow_test_exec=False`. A worktree's `.git` is a regular **file** (a
`gitdir:` pointer), not a directory, which defeats every one of the
scratch-mode guard's conditions outright — `_guard` therefore grows a second,
worktree-shaped branch (`_guard_worktree`) rather than a patch to the first,
identity-anchored on `repo_root` (operator config, never agent-writable): HEAD
must resolve to the derived task branch (a worker's own `git` tool can
otherwise move the operator's real branch, unguarded by anything the first
four conditions check), and the worktree's admin directory is verified by
git's own back-pointer rather than by containment in a shared parent
(containment there repeated slice 6's own prior escape one directory up — a
sibling task's worktree already existing was enough to let task 2 hijack task
1's real branch). A workspace **outside** the repository needs no exclusion
mechanism at all (the main repo's status stayed clean with a worktree
elsewhere on disk, measured), which is the *smaller* half of why worktrees
live outside `repo_root`; the larger half is residual 2 below.

**Base is the worktree's starting commit, never an empty one** — the single
most dangerous difference from scratch mode, where base is deliberately empty
because a rollback must not resurrect whatever a throwaway directory happened
to hold. Here the same choice would delete the operator's entire checkout on
the task branch, so `rollback` returning to the starting commit *is* the
recovery contract rather than a detail of it. `commit` drops the
force-add flag in worktree mode: force-adding ignored files is what makes a
scratch rollback fully recoverable, but here it would commit `node_modules`/
`.venv` into the task branch every round — the cost is real and named per
rollback (`ignored_unrecoverable`), never hidden. `remove_worktree` removes
the checkout **then** prunes the admin entry, never a bare `rmtree`, because
deleting the directory alone leaves the admin entry under
`<repo_root>/.git/worktrees/<name>` registered, and a listing keeps reporting
a workspace that is gone.

**P4: threading `repo_root` through `loop.py`.** `Loop._worktree_repo_root()`
computes the mode once per call (`None` in scratch mode, the absolute
`repo_root` in worktree mode — `os.path.abspath`, lexically, matching
`vcs._git`'s own `-C` handling, not `Path.resolve()`, which would follow a
junction and answer a question about a different repository than the one
`config.repo_root` names) and every one of `run_task`'s existing `vcs.*` call
sites reads the same answer rather than re-deriving it, so scratch mode
staying a **proven** no-op does not depend on getting the same derivation
right in six different places. `Loop._worktree_pin(task_id, repo_root)`
chooses the matching pin table (`Store.vcs_repo_pin` in worktree mode,
`Store.vcs_pin` in scratch mode) the same way. The AST guard's own inventory
grew from 6 to 12 `vcs.*` call expressions in this phase, across 10 distinct
names — a third `working_tree_state` was **not** added for residual 2's
snapshot (see below); a second `init_repo` (`human_redo`'s worktree fresh
start), `remove_worktree`/`remove_task_branch` (also `human_redo`), and
`base_ref`/`discarded_ref_prefix` (`_vcs_rollback_to_base` now selects the
per-task ref names instead of reading the scratch-mode module constants
directly) account for the rest, and the control that asserts the count is
changed in the same commit as the count itself, per this project's own
standing rule. **No new decision rule**: every one of these calls stays
*call, log, discard*, and the AST guard
(`test_no_status_write_is_downstream_of_a_vcs_result`) still walks `loop.py`
and fails if a `VcsResult` ever reaches a status write — extended with two
more planted violations in the new call shapes (a `repo_root=`/`task_id=`
call assigned to a tainted name, and the same shape reaching a status write
through `_vcs_rollback_to_base`) to prove the walker catches the worktree
shape and not only the scratch one it was built against.

**`run_planner` gets `cwd=repo_root`** (read-only: the planner declares
`file_read`, not `file_io`) in worktree mode and `None` in scratch mode — a
plan row has no task workspace to point it at, which is why this seam was left
open rather than filled in P1 alongside the worker/validator.

**A worktree survives `DONE`.** The loop never removes one on success — settled
in interview, and the opposite of `_clear_worktree`'s pre-existing,
unrelated-by-coincidence name (that helper is the *scratch*-mode redo residue
fallback, predating this slice, and still only ever called on a `.git`
*directory*). Removal happens only on `human_redo` and the explicit
`agentloop workspace prune`. **`human_redo`'s worktree-mode fresh start is
"remove and recreate"** the checkout, not reset-in-place:
`_vcs_rollback_to_base` still runs first and, in the shape that actually
happened, writes the per-task discarded ref at whatever the branch's prior
tip was (or records nothing when there was nothing to discard) —
recreation only follows *after* that history is safely reachable. **Not
unconditionally** (round-4 remediation, found independently by two
mutually-blind reviewers via two different attacks — one monkeypatching the
ref write, one a real git ref-namespace collision needing no monkeypatch at
all): the remove-and-recreate step is gated on
`result.ok and result.reason != "residue"`, the same discriminator the
scratch-mode fallback already used correctly a few lines below it. A
rollback that fails outright, or a genuine git-level failure such as the
discarded ref's own path being pre-occupied by an ordinary ref, returns
`ok=False` with **nothing** written — the earlier unconditional version
still deleted the branch in that case, permanently orphaning the round's
commits the moment an ordinary `git gc` ran, since `git show <sha>`
resolving an orphan proves nothing about survivability. When the gate is not
met, the worktree and its branch are left exactly as they are — the
`vcs_unavailable`/degraded `vcs_rollback` event `_vcs_rollback_to_base`
already logs is the record — and a later run retries. This closes the gap
P2 explicitly deferred:
`init_repo`'s worktree branch always creates without allowing an overwrite of
an existing branch of the same name (a branch that already exists means a
previous incarnation of the task, and force-resetting it would discard
commits no human asked to discard), so a *second* redo of the same task hit
that refusal outright — measured, the worktree-add step failing because the
derived branch name already existed, and falsified by neutering the fix
(removing the branch-delete call reproduces exactly that failure text). The
decision, per the reasoning the plan asked for verified rather than assumed:
**delete the stale branch first**. `human_redo`'s whole contract is a fresh
start with no carried-over context, and by the time the branch is deleted its
commits are already reachable from the discarded ref the rollback wrote one
step earlier (reachability, never the single-commit inspection command the
project's history already warns is insufficient — the same rule slice 6
established, since an orphaned commit still resolves under a direct lookup) —
so deleting the branch *name* costs nothing recoverable, only the name the
next worktree-add needs to reuse. New `vcs.remove_task_branch` (pinned like
every other repo-level mutation) is the one function this phase added to
`vcs.py` beyond threading existing ones.

**Residual 2, measured both ways rather than assumed** (tests 27-29). The
executor sandbox's already-documented `..`/absolute-path escape gets a far
worse target under worktree mode — the operator's real checkout instead of a
throwaway directory — and putting `worktree_root` outside `repo_root`
(enforced at config load, `LoopConfig.__post_init__`) only *reduces* it:
relative traversal from the workspace now has to climb out of an
agentloop-owned directory first (measured: the identical relative traversal
that reaches the operator's real repo when `worktree_root` sits inside it
lands inside `worktree_root` instead when it sits outside), but an
**absolute** path reaches the repository regardless — a test written to PASS
*because the write succeeds*, with a comment saying so, so a later isolation
slice closing this is a visible flip rather than a silent one. Detection is
the second, independent leg: a status snapshot of `repo_root` itself (not the
task workspace) is taken before and after each test command runs, and a
difference is logged as `worktree_out_of_branch_write` with
`prevented: False` — the same H3 posture `_vcs_detect_validator_writes`
already established, reused rather than duplicated. This needed a **new**
narrow entry point, `vcs.repo_status(repo_root, config, pin)`, rather than
calling `vcs.working_tree_state(repo_root, ...)` directly: measured, both of
`_guard`'s existing shapes refuse `repo_root` with `not-a-workspace-repo` —
the scratch branch requires the resolved workspace to sit inside
`config.workspace_root`, which the operator's own repository is not, and the
worktree branch requires the workspace's own git pointer to be a file, which
`repo_root`'s is not (it is the ordinary directory every worktree's pointer
points back to). `repo_status` is pinned like its siblings (it still spawns
git under `<repo_root>/.git/config`) but skips `_guard` entirely, because
`repo_root` is the one path in this whole module that is operator config read
directly, never a workspace an agent's own tools resolve into.

**`sandbox_env_allowlist` can re-admit a credential-shaped variable with no
denylist** (test 31) — true since slice 8 shipped the allowlist, tolerable
while the sandbox barely ran; not once worktree mode runs the operator's real
suite every round and a real suite has a real reason to widen the knob
(`DATABASE_URL`, a service token). `executor.credential_like_names` matches a
small, explicitly-documented-as-a-heuristic pattern set (`*_API_KEY`,
`*_TOKEN`, `*_SECRET`, `*_PASSWORD`, `AWS_*`) and `Loop.__init__` logs a
`config_warning` event naming any match, on the same channel the unknown-key
warning already uses — **a warning, not a refusal**: an operator may
genuinely need a provider key in a real test suite, and this project's rule
(stated once already, for the tool gate) is that an unjustifiable refusal
becomes a knob someone disables. The matched variable is still actually
admitted to the child process — the warning does not become a silent removal.

**`agentloop status <id>` prints the task's workspace path** — the operator
half of the trade residual 4 names (a worktree living outside `repo_root` is
no longer somewhere an operator finds by looking beside their project
directory), the other half being `agentloop workspace prune`, already built
in P3.

**Residuals restated from the plan, not solved:**

1. A worker can write the operator's *real* local git config from inside the
   worktree. The pin detects this before the next side-effecting call and
   refuses (`config-changed`); detection is not prevention, and `agentloop
   workspace rebless` (P3) is the human-only recovery for a *legitimate* edit,
   never an automatic one.
2. The executor sandbox's escape is reduced, not closed — see above.
3. Ignored files are not recoverable after a worktree rollback (the dropped
   force-add), named per-rollback (`ignored_unrecoverable`).
4. Workspaces are no longer visible beside the repository they work on —
   mitigated, not removed, by `agentloop status` and `agentloop workspace
   prune`.
5. Merge conflicts between two tasks' branches are the operator's problem;
   the planner's DAG expresses ordering, not file-level disjointness.

**What was verified rather than assumed**, per the [RED-FIRST]/[NEUTER]
discipline this whole slice was built under: the workspace-starts-with-
tracked-files claim (test 15) was watched failing against the pre-P4 tree —
a genuine behavioral RED (a missing tracked file), not a collection error —
before `run_task`'s call sites were wired; the branch-collision fix was
falsified by removing the branch-delete call and reproducing the exact
worktree-add failure text; the out-of-branch detection event was falsified by
removing its one call site and confirming the assertion goes red with zero
events recorded; and the AST guard's own extension was pinned by two new
planted violations rather than trusted to generalize from the scratch-mode
ones. **No decision rule changed** — the thresholds, revision counting, the
budget cap, the tests gate and the tool gate all read the same fields they
did before slice 8; what changed is that the loop can now be pointed at a
real repository at all.
