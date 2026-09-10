# CLAUDE.md — project context for agentloop

## What this is

A general-purpose agentic development loop. Worker agents execute tasks; an
independent validator reviews output against acceptance criteria; humans stay
in the loop at task definition and review. One SQLite database is the single
source of truth for state, metrics, audit trail, and memory.

New to this project? Read **[docs/GUIDE.md](docs/GUIDE.md)** first — it has
the walkthrough, the trade-offs behind the big design decisions, and how this
compares to other agentic-coding setups. This file is the working reference
for anyone changing code here: the module map, the decision rules, and the
conventions to follow. Code comments here are kept to safety-net one-liners
only (see Conventions); **before changing a function whose behavior looks
simpler than it is, check [docs/CODE_NOTES.md](docs/CODE_NOTES.md)** — the
per-function design reasoning that used to sit inline now lives there,
organized by file. Detailed slice-by-slice history (what broke, how it was
found, how it was fixed) lives in `docs/history/`.

Current state: Phase 1 (sequential loop) + Phase 2 (live dashboard,
`docs/history/phase2-dashboard.md`) are done.
Roadmap slices 1-6, 8, 9 and 10 are done — see "Roadmap" below for the list
and pointers into `docs/history/`. Slice 7 (office-metaphor dashboard view)
is built on an unmerged branch pending a human UI check.

## Commands

- Install: `pip install -e ".[dev]"` (add `.[claude]` for the real runner)
- Format: `ruff format .` — **run before every commit and push**
- Tests: `pytest -q` (no API keys or network needed; one test skips when the
  environment lacks symlink/junction privileges)
- Run: `agentloop add "Title" --goal ... --criteria ... [--risk 0|1|2]`,
  or `agentloop plan "Goal" --criteria ...` (planner → task graph) then
  `agentloop approve-plan ID` to release it,
  then `agentloop run --runner claude|mock`, `agentloop status [ID]`,
  `agentloop events ID`, `agentloop approve|reject|redo ID`,
  `agentloop pause|resume|abort ID` (mid-run control),
  `agentloop memory list|approve|reject|add|pin|unpin` (`add --pinned`),
  `agentloop charter show [--version N]|set --file|--text [--note]|clear|history`,
  `agentloop eval --runner mock|claude --mode verdict|batch`,
  `agentloop project add|rename|repoint|list|archive|use`
- Dashboard: `cd web && npm install && npm run build`, then `agentloop serve`
  (frontend checks: `npm run typecheck`, `npm run build`)

## Architecture (agentloop/)

- **`config.py`** — `LoopConfig`: `approve_threshold` (0.70), `severe_threshold`
  (0.40), budget caps (tokens/cost). `MODEL_PRICING` + `CACHE_MULTIPLIERS`
  (per-model cache read/write multipliers; unset = Anthropic's
  `CACHE_WRITE_MULTIPLIER`=1.25/`CACHE_READ_MULTIPLIER`=0.10). Both keyed
  through `pricing_key(model)`: exact match, then the id with a trailing
  `-YYYY-MM-DD` stripped, then the longest table key the id extends at a `-`
  boundary — needed because OpenAI echoes the resolved snapshot id
  (`gpt-4o-mini-2024-07-18`), never the alias requested. `OPENAI_MODELS`
  lists every id `OpenAICompatRunner` can emit, for a test asserting pricing
  coverage; an unlisted model silently prices at `DEFAULT_PRICING`.
  `memory_retrieval_backend`: `'hash'` (default) or `'none'`, anything else
  raises. Sandbox knobs: `sandbox_env_allowlist`, `sandbox_isolation`. Infra
  retry: `infra_max_retries`, `infra_retry_backoff_s`. Context handoff:
  `context_handoff_ratio` (0.70). Planner/parallel: `plan_requires_approval`
  (True), `max_plan_tasks` (20), `max_parallel_workers` (1 = sequential).
  Tool requests: `tool_readonly_allowlist` (`file_read`/`search`/`task_state`
  auto-approve; `web` excluded — it egresses the prompt), `gate_declared_tools`
  (False), `max_tool_requests_per_task` (10, pending rows only). Durability:
  `vcs_enabled` (True), `vcs_command` (`"git"` — an executable **path**, not a
  command line; unlike `test_command` it does not go through `split_command`),
  `vcs_timeout_s` (30). Existing-repository workspaces: `workspace_mode`
  (`"scratch"` default — a proven no-op — or `"worktree"`, a real `git
  worktree` checkout of `repo_root` on its own branch), `repo_root` (`"."`,
  unread in scratch mode), `worktree_root` (deliberately outside `repo_root`,
  enforced not just defaulted — a workspace inside the repo would turn
  `executor.py`'s documented `..`-escape into a write on the operator's real
  working tree), `vcs_base_ref` (`"HEAD"`), `vcs_branch_prefix`
  (`"agentloop/task-"`, never stored — derived from the task id). See
  `executor.workspace_for` and `loop._worktree_repo_root` below for how a
  task resolves *which* mode/repo it's actually running against.
  `LoopConfig.__post_init__` normalizes field types by
  declared annotation (a `null` list becomes `[]`; a bare string raises,
  since `x in "string"` is a substring test that fails open on a permission
  allowlist) — catches a bad `loopconfig.json` before it reaches a paid call.

- **`models.py`** — `Task` (`kind`: `'task'`|`'plan'`, `plan_id`, `project_id`
  resolved to a concrete project by `Store.add_task` before the row is
  written), `TaskStatus`, `Verdict` (`findings`: a copy of part of
  `reasoning`, never removed from it), `VerdictKind`, `AgentSpec` (`runner`:
  which backend serves this role, `None` = loop default; defaulted so an old
  `agents.json` still loads), `RunResult`, `PlannedTask` (a planner-proposed
  graph node with a local `ref` expressing edges before db ids exist),
  `TestResult.coverage_percent` (`float | None`, `None` = "not reported",
  never `0.0`).

- **`store.py`** — SQLite source of truth. Tables: `tasks` (`control`:
  run/pause/abort, written only by `set_control`; `claimed_by` lease),
  `attempts` (per-invocation metrics incl. cache tokens, `charter_version`),
  `verdicts` (incl. `findings`), `events` (append-only audit log — never
  UPDATE/DELETE), `memory` (project/loop tiers; reads gated on `approved`;
  `pinned` flag; `UNIQUE(project_id, tier, key)`), `memory_hits`
  (`memory_id, task_id` — backs `hit_count` as *distinct tasks*, not
  prompts), `charter` (append-only, `project_id` scoped — `id` is a version
  counter shared across every project's rows, so "current" always means
  highest id *for this project*, never highest id overall), `test_runs`
  (`coverage_percent` nullable), `eval_runs` (`kind`:
  `'verdict'`|`'batch'`), `vcs_pins` (`task_id` PK — the git-config
  fingerprint the workspace guard checks; no agent write path),
  `tool_requests` (capability ledger, `UNIQUE(task_id, role, tool)`, no FKs
  — an FK would raise inside a paid transaction), `task_deps`, `projects`
  (multi-project registry). Schema is plain SQL; `_migrate()` only ever adds
  columns (the one exception: slice 10's memory-table rebuild, since SQLite
  can't `ALTER` a `UNIQUE` constraint in place).
  `transaction()` pairs a row change with its audit event in one commit
  (all-or-nothing) — route every paired row+event write through it. Every
  unbatched write goes through `_LockedConnection.write()`, which holds the
  lock across execute *and* commit; never pair a bare `execute()` with a
  following `commit()`. `claim_next_task(worker_id)` is an atomic
  compare-and-swap (`WHERE status='pending' AND claimed_by IS NULL`, checked
  via `rowcount`) — the connection lock only serializes one process, and
  sqlite3 opens no write transaction for a bare `SELECT`. `stranded_claims()`
  finds work held by a retired claim id; `Loop.run` logs `claim_stranded`
  rather than reclaiming it. `attempt_tokens(id, kind)` sums one role's
  tokens (incl. cache) — backs the context-handoff check.
  `memory_promote(id)` **moves** a project row to `loop` tier
  (`UPDATE ... SET tier='loop'`) rather than copying it; on a
  `UNIQUE(project_id, tier, key)` collision, `_merge_into_loop` picks a
  surviving row (id-preserving) and decides approval against the *surviving
  value*, not a particular row — two approved rows with different values
  drop to unapproved and log `memory_revoked`. `charter_set`/`charter_active`/
  `charter_clear`/`charter_history`/`charter_version` all take `project_id`,
  resolved through `resolve_project` like every other project-scoped write —
  a charter read always targets exactly one project's active charter, never
  `memory_list`'s "`None` means every project" convention. `charter_version`
  filters on `project_id` too, not just `id`, since `id` alone would let a
  caller in one project read another project's charter text by version
  number. `charter_set` refuses a body over 4000 chars or whitespace-only;
  `charter_active` returns `None` for both never-set and cleared,
  deliberately indistinguishable, so a cleared charter restores a
  byte-for-byte pre-charter prompt. `task_deps` +
  `add_dependency` (refuses any edge that would close a cycle, incl.
  self-edges) back the claimability predicate, evaluated *inside*
  `claim_next_task`'s transaction so two workers finishing two dependencies
  at once can't both conclude a shared dependent is still blocked.
  `resolve_project(project: int | str | None) -> int` is the one seam every
  caller (CLI, server, memory, loop) goes through: `None` = the default
  project's id (never raises), an `int`/`str` must resolve or raises.

- **`registry.py`** — agent registry: role, model, system prompt, tools,
  context budget, version, and the `runner` a role is pinned to. Defaults:
  `worker`, `validator`, `summarizer`, `planner`. `agentloop init-registry` →
  `agents.json`. The planner's tools are `file_read` only — it proposes work,
  it doesn't do it. A missing `planner` role escalates the plan row to
  NEEDS_HUMAN up front (config error, not retried) rather than falling
  through to a worker-shaped prompt.

- **`runner.py`** — `ModelRunner` protocol, the provider seam; the loop never
  imports a vendor SDK directly. Backends: `ClaudeSDKRunner` (default),
  `OpenAICompatRunner` (any OpenAI-compatible `/v1/chat/completions`
  endpoint via stdlib `urllib.request`, no SDK), `MockRunner` (scripted, for
  tests). `extract_usage` reads only the terminal message — summing per
  message double-counts. `extract_openai_usage` subtracts cached tokens from
  OpenAI's `prompt_tokens` (which is *inclusive* of the cached share),
  clamped at zero. Usage parsing is total — never raises — because
  `runner.run()` is called outside any transaction, and a raise after a
  provider has already billed the call would discard that attempt's tokens
  and cost, and `_with_retry` would pay for the completion again. A degraded
  (estimated) usage read logs a `runner_warning` event rather than printing
  to stdout. `_check_base_url` refuses a non-https `base_url` off loopback;
  the bearer token cannot follow a redirect. A missing/invalid API key or a
  400/401/403/404 raises `RunnerConfigError` (config error, no retry, no
  `infra_error`); 408/429/5xx are transient and retried. Registry `tools`
  are dropped (with a warning) on this backend — it has no tool-execution
  loop, so forwarding them would let the model emit calls nobody runs.
  `get_runner` knows `claude`/`openai`/`mock`.

- **`eval.py`** — validator calibration harness. Fixtures run through
  `run_validator`; reports agreement, confusion matrix, calibration table.
  `claude`/`openai` paths are real measurements, gated on their own
  credential and skipped by name without it. `run_batch_eval`
  (`--mode batch`) drives `BATCH_FIXTURES` through a real `Loop`, measuring
  final `TaskStatus` against gold rather than one verdict — mock-only, and
  refuses a non-mock runner loudly (an earlier silent fallback printed
  scripted-fixture numbers as a live measurement).

- **`agents.py`** — prompt building for worker/validator/summarizer/planner;
  verdict and plan parsing. `run_planner` decomposes a goal; `parse_plan`
  validates a reply *whole* before anything is written (unique refs, every
  `depends_on` present, DAG via Kahn's algorithm, size cap) and raises
  `PlanError` on any failure — never a partial graph. `run_summarizer`
  compacts a task's working state for a context handoff. Validator first
  line: `VERDICT: <kind> CONFIDENCE: <0-1> TESTS: <pass|fail|na>`. Injects
  approved memory facts and real test results into prompts.
  `_charter_block(store, project_id)` returns `(block, version)`, `("", None)`
  when there is none — injected above the memory block in worker/validator/
  planner prompts, never the summarizer (whose output a worker rebuilds fresh
  anyway). `project_id` is always the calling task's own project (never left
  to default-resolve), same reason `_memory_block` threads it through — a
  worker on one project must never see another registered project's charter.
  `parse_verdict` extracts a soft, line-anchored `FINDINGS:` marker
  into `Verdict.findings`, degrading to `""` on anything unexpected — never
  raises.

- **`executor.py`** — sandboxed test execution. Threat model is arbitrary
  AI-generated code, not just command hijack: command is allowlisted in
  config (never model output), split to argv via `split_command` (not bare
  `shlex.split` — POSIX mode eats Windows path separators), run with
  `shell=False`, cwd pinned to the task workspace, timeout + output cap. The
  child env is scrubbed to an allowlist, never the parent env wholesale.
  `sandbox_isolation='strict'` requests a container/no-network tier and
  degrades to env-scrub with a warning when none is wired in (documented
  residual: fs/network still open in the env-scrub tier). `parse_coverage`
  is pure and total: returns `None` or a float in `[0, 100]`; `None` means
  "not reported," never "0%" — the text it parses can include model-written
  output, so no decision rule may read it as evidence. `workspace_for`'s
  worktree-vs-scratch dispatch reads the calling **task's own resolved
  project**, never the process-global `LoopConfig` directly — a past bug
  read the global config's mode instead, which could land a worktree-mode
  project's checkouts inside the orchestrator's own repository when a task
  belonged to a different project than the process's own `loopconfig.json`.

- **`vcs.py`** — per-task workspace git repos (one per
  `.agentloop/ws/task-{id}/`, no remote, no shared timeline), so `redo`/
  `reject` recover a discarded round instead of destroying it, and parallel
  workers never share one repo's index. This module runs its own fixed git
  commands, but *inside a directory AI-written code can write to* — so both
  "which repo does git think this is" and "what programs does git run" are
  live attack surfaces. `_guard` refuses any side-effecting call unless
  `<ws>/.git` is a directory **and** git's own
  `rev-parse --show-toplevel --absolute-git-dir --git-common-dir` all
  resolve to `<ws>/.git` **by identity** — not merely "inside
  `workspace_root`" (a weaker, earlier version of this check: that directory
  contains every task's workspace, so it let through a directory a worker
  made inside its own workspace, and a sibling task's real repository).
  Testing git's *reported* locations rather than filesystem paths matters
  because a `.git/commondir` file write redirects git's ref/object
  resolution while leaving `.git/config` byte-identical, which a path-only
  guard cannot see. The execution axis — git config naming programs it runs
  (`filter.<name>.clean`, `core.fsmonitor`) — has no denylist, because filter
  names are arbitrary; it's closed by a **config pin** instead: `init_repo`
  fingerprints (blake2b) the `.git/config` it just wrote, the fingerprint
  lives in the store (`vcs_pins` — the one place an agent has no write path),
  and every later side-effecting call re-hashes the file and refuses on a
  mismatch (`"config-changed"`/`"config-unpinned"`) before any subprocess
  runs. `rollback` writes `refs/agentloop/discarded/<sha>` **before**
  anything moves (keyed by sha, not a counter, to avoid a lost-update race
  between two rollbacks). In scratch mode the round commit force-adds
  ignored files (`add -A -f`) so everything a rollback's `clean -ffdqx`
  deletes is genuinely recoverable; **worktree mode drops the `-f`** (forcing
  `node_modules`/`.venv` into the operator's own branch every round would be
  worse), so ignored files there are *not* recoverable after a rollback —
  reported via `VcsResult.ignored_unrecoverable`, which (unlike
  `nested_repos`) doesn't yet reach an audit event or the dashboard. A
  worker-created *nested* git repo is a documented residual either way: git
  can only record it as a bare gitlink, so its own history doesn't survive a
  rollback (`unrecoverable_nested_repos` names this per rollback rather than
  claiming full recovery). Every entry point (`init_repo`/`commit`/
  `mark_approved`/`rollback`/`is_repo`, plus the worktree-mode
  `remove_worktree`/`remove_task_branch`/`prune_worktrees`) is total —
  always returns a JSON-encodable `VcsResult` (or `bool` for `is_repo`),
  never raises, because durability must never fail an attempt.
  `VcsResult.reason` is a closed vocabulary, `""` exactly when nothing
  degraded. Full incident narrative in `docs/history/slices-1-to-7.md`
  (slice 6).

- **`memory.py`** — two-tier policy: approved-only reads, unapproved agent
  writes, `hit_count`-based project→loop promotion. `_record_reads` bumps
  only facts a ranked injection scored above zero, once per `task_id`.
  `facts_for_prompt(query=…, task_id=…, project_id=…)` ranks approved
  candidates through `retrieval.py` when a query is given and returns
  `(block, provenance)`; with no query it's the pre-retrieval alphabetical
  selection and no `retrieval` event. `project_id` (the registered-repo
  kind, from `store.projects` — a different axis than the tier name above)
  is resolved once via `Store.resolve_project` so a worker's prompt only
  ever sees its own project's facts.

- **`retrieval.py`** — `RetrievalBackend` protocol, the memory-relevance
  seam. `embed()` is a stdlib hashed bag-of-words vector (blake2b, not
  builtin `hash` — that's salted per process); `HashingBackend` brute-forces
  cosine over the candidates and is the only backend. `get_backend` resolves
  `'hash'`/`'none'`, raises on anything else. The approval gate lives
  upstream: callers pass in already-approved rows, so no backend can surface
  an unvetted fact.

- **`toolpolicy.py`** — tool capability gate. agentloop never executes an
  agent's tools itself (the SDK runs them inside `runner.run()`), so the only
  place a gate can bite is the `tools` list handed to the runner. `tools_for`
  is the single enforcement point, called before every model call — no
  post-hoc check on observed `tool_calls`. `classify(tool, config)`: auto
  (allowlisted read-only) / gated (needs a human) / unknown. A grant *adds*
  (even a tool the role never declared); the gate *removes* — and removes
  the whole **concrete** capability, not just the requested logical name,
  since `LOGICAL_TOOL_MAP` is not injective (rejecting `shell` also disables
  `git`). Fail closed on purpose, audited as `tool_capability_withheld`.
  `parse_tool_requests` is total — never raises, even inside a paid
  transaction.

- **`loop.py`** — orchestration state machine + human decision methods. See
  the module docstring for the full decision-rule table (mirrored below).
  `_maybe_handoff` runs at the iteration boundary; `plan()`/`approve_plan()`
  persist and release a task graph; `run()` is sequential at
  `max_parallel_workers=1`, `_run_parallel` above that (atomic claims, no
  in-memory schedule). `_runner_for(role)` resolves a role's provider pin,
  or falls back to the loop's own runner object — the same object, not an
  equivalent, which is what makes an unpinned run structurally identical to
  a single-provider one. `_worktree_repo_root(task)` resolves a task's own
  project's `repo_root`/`workspace_mode` (falling back to the process's own
  `LoopConfig` only for the bootstrap "Default" project) — the source of
  truth `executor.workspace_for` dispatches on.

- **`server.py`** — REST + SSE dashboard backend, stdlib `http.server` only.
  The append-only `events` table *is* the change feed: SSE is a
  `WHERE id > cursor` query, so reconnects resume losslessly via
  `Last-Event-ID`. `?project=` scopes tasks/metrics/memory/tool_requests/
  stream; `_parse_project_query`: absent = unfiltered, malformed = 400,
  well-formed but unknown = 404.

- **`cli.py`** — argparse CLI, structured plain output. `--runner
  claude|openai|mock` appears on `plan`/`run`/`serve`/`eval` and sets the
  loop's *default* backend only — per-role pins are an `agents.json`
  decision, applied on top of it.

- **`web/`** — Vite + React + TypeScript dashboard. `types.ts` mirrors the
  server's JSON shapes — keep them in sync when changing an endpoint.
  `ProjectSwitcher.tsx` drives `?project=` on every fetch and the SSE
  subscription, never a client-side filter. No agent in this project can
  verify rendered `web/` output — UI changes get a seeded demo database and
  a written checklist reviewed by a human, every time.

## Decision rules (do not change without updating tests + README)

- Approve + confidence ≥ `approve_threshold` (0.70) + tests not failing →
  DONE, unless `risk_level` ≥ 2 → NEEDS_HUMAN sign-off first.
- **"Tests not failing" = the executed result**, not the validator's own
  `TESTS:` claim. A validator claiming pass over an executed fail is logged
  as `test_disagreement` and cannot approve the task. `na` (no workspace or
  execution disabled) falls back to the validator's claim.
- Revise, or approve below threshold → revision with validator feedback,
  bounded by `max_revisions` (3); exhausted → NEEDS_HUMAN.
- Escalate verdict, or confidence < `severe_threshold` (0.40) → NEEDS_HUMAN
  immediately (no revision loop).
- Worker output starting `ESCALATE:` → NEEDS_HUMAN.
- **Empty worker output → NEEDS_HUMAN** (checked before anything downstream
  treats the empty output as real work — a validator approving a blank
  output under the task graph would release dependents against nothing).
  Escalate, not revise: emptiness isn't a quality gap, so `max_revisions`
  isn't touched.
- Budget cap (tokens or cost) exceeded → NEEDS_HUMAN. Totals include
  prompt-cache tokens (write ×1.25, read ×0.10 on Anthropic; per-model on
  OpenAI via `CACHE_MULTIPLIERS`).
- Transient infra failure → retried with exponential backoff up to
  `infra_max_retries`; persists → NEEDS_HUMAN with `infra_error` (each
  attempt logs its own `infra_error` event). Not counted against
  `max_revisions`.
- Context-budget handoff: at the iteration boundary, once a worker's
  accumulated context on the task reaches `context_handoff_ratio` (0.70) of
  its `AgentSpec.context_budget_tokens`, the `summarizer` compacts state and
  the worker restarts from that summary (`context_handoff` event). Not a
  revision.
- Planner (`Loop.plan`): every failure mode (`ESCALATE:`, unparseable JSON, a
  `depends_on` outside the plan, a cycle, over `max_plan_tasks`) escalates
  the **plan row** to NEEDS_HUMAN and creates **zero** child tasks — never
  repaired, truncated, or partially applied. A missing `planner` role
  escalates up front (config error, not retried).
- A plan's tasks aren't claimable until it's approved when
  `plan_requires_approval` (default True) — a claimability predicate on the
  plan row, not a child status.
- A task is claimable only when every dependency is DONE. A dependent of an
  escalated/failed task is *skipped, not failed* — stays `pending`, becomes
  claimable once the dependency resolves. Cycles are refused at
  `Store.add_dependency` and again in `parse_plan`.
- `max_parallel_workers` (default 1) bounds concurrent tasks; the atomic
  claim guarantees one task = one worker at any value.
- `human_approve` refuses a task still `pending` (no attempt/verdict/output
  to sign off on). `approve_plan` refuses a plan with zero tasks.
- Mid-run control, read each iteration boundary: `pause` → PAUSED (no
  auto-resume), `abort` → ABORTED (terminal, audit preserved). Written only
  by `set_control`.
- **Which provider serves a role is not a gate.** `AgentSpec.runner` changes
  who is asked, never what the answer means — every threshold/count/cap
  reads the same fields regardless of backend. Both providers' spend lands
  in **one** task budget (`task_spend` sums every attempt whatever ran it).
- A pin naming a nonexistent runner, a missing API key, a revoked one, an
  unserved model, or a malformed request → config error → NEEDS_HUMAN, no
  retry, no `infra_error` (retrying only delays reaching the same
  conclusion). 408/429/5xx stay transient and retried.
- Validator findings and the project charter are **evidence, not gates** —
  neither one directly flips a task's status; only the verdict does. An
  empty findings section is "no findings recorded," not a reason to revise.
- Unparseable validator verdict → escalate at confidence 0 (never
  guess-approve). A `FINDINGS:` section never rescues one.
- `human_redo` = same task definition, fresh start, NO carried context
  (output/feedback/revision_count reset, workspace emptied — not destroyed:
  `refs/agentloop/discarded/<sha>` keeps the round reachable); audit trail
  preserved.
- Memory: approval is approval **of a value** — `memory_write` keeps
  `approved` across a rewrite only while the value is unchanged; `pinned`
  stays sticky across a value change. Reads are gated on `approved`; agent
  writes land unapproved. A project fact hit on `memory_promote_threshold`
  (3) *distinct tasks* promotes to `loop` tier. Injection is capped (20);
  pinned approved facts sort first under a separate ceiling (10) but never
  bypass `approved`. A `hit_count` bump requires a ranked injection scoring
  > 0 **and** a distinct `task_id` — not merely an injection (every approved
  fact injects under the cap) and not merely a prompt (worker + validator +
  a revision are 3 prompts in 1 task). Ranking (`retrieval.py`) decides
  order only; the approved gate and the caps are unchanged around it. Every
  memory injection with a query logs a `retrieval` event; every tool an
  agent actually invokes logs a `tool_call` event (`executed` distinguishes
  a tool that ran from one the model merely asked for — the SDK path
  defaults True, `OpenAICompatRunner` always False, since it has no
  execution loop).
- `Store.transaction()` decrements its depth counter on the error path too,
  and raises `TransactionAborted` if an enclosing block swallows an inner
  transaction's error.
- Agent-requested tool gate: a `TOOL_REQUEST: <tool> (blocking|optional) -
  reason` marker in an agent's output. Read-only requests auto-approve
  (`tool_auto_approved`); side-effecting ones queue for a human
  (`tool_request_decided`). An **optional** request queues and the task
  continues without it; a **blocking** request parks the task at
  NEEDS_HUMAN with partial output kept, `max_revisions` untouched — not a
  revision or escalation reason code, just prose. Worker/validator rounds
  can park; the planner never does (planning isn't production work). An
  undecided request for a capability the role already holds is a no-op for
  subtraction (nobody was asked); a **rejected** row subtracts
  unconditionally; a machine-refused row (unknown tool, over the per-task
  cap) subtracts nothing. **Approving a parked request is the only decision
  that releases the task** — `reject_tool_request` does not release it,
  and `pause`+`resume`/`human_redo` requeue with the request still
  undecided (parks again next round). `human_approve` on a parked task is
  *not* refused — it marks the task DONE (signing off the partial output the
  park preserved); `human_reject` fails it; both clear `parked` and leave
  the request undecided. No threshold/count/cap reads a tool request.
- **Slice 6 adds no decision rule** (enforced by an AST test walking
  `loop.py`, plus a full-state differential proving `vcs_enabled=False` is a
  behavioral no-op) — every `vcs.*` call is *call, log, discard*. What it
  *does* change: `human_reject` rolls the workspace back to
  `refs/agentloop/base`; `human_redo`'s "fresh start" now means *emptied*,
  not *destroyed* (the discarded round stays reachable via
  `refs/agentloop/discarded/<sha>`).
- **Slice 10 adds no decision rule.** `project_id=None` is byte-for-byte the
  pre-slice-10 "every project" (as a filter) / "the default project" (as a
  write target) behavior everywhere it's read.

## Conventions

- Python ≥ 3.10, stdlib-only core (no runtime deps); `claude-agent-sdk` and
  `pytest` are optional extras. The dashboard uses `http.server` + SSE
  rather than FastAPI/websockets for exactly this reason.
- `Store` serializes all SQL through a lock and opens with
  `check_same_thread=False`: the server reads from request threads while the
  loop writes. Go through `self._conn`; don't reach for a raw connection.
- Dataclasses + str-Enums for domain types; type hints everywhere.
- Every state change and agent I/O gets a row in `events` — the audit log is
  load-bearing (debugging the loop, trusting memory contents).
- Tests are end-to-end through the `Loop` with `MockRunner` scripted
  outputs; add a test for any new decision rule.
- **Always run `ruff format .` before any commit and push** — the whole tree
  must be ruff-clean, so formatting never rides along in an unrelated diff.
- **Telemetry and human-facing text must never fail an attempt, and must
  never assert more than their inputs prove.** Telemetry is logged inside a
  paid transaction, so a value the encoder rejects rolls back the
  `finish_attempt` it was attached to, and the retry buys the completion
  again. Every string rendered on a screen must be computed from the gate's
  actual outputs, never from a map or heuristic that knows only part of the
  state.
- **Comments explain *why*, not *what happened*.** State the current
  contract and the non-obvious trade-off behind it; leave the "who found
  this, when, how many rounds it took" story to `docs/history/`.
- CLAUDE.md is tracked in git (it's the project's working design reference,
  not a session file). `*.db` and local config (`loopconfig.json`,
  `agents.json`) are gitignored. `temp/` is gitignored — personal working
  notes, never committed. `docs/` is gitignored except `docs/GUIDE.md`,
  `docs/history/` and `docs/plans/`, which ship with the project (CLAUDE.md
  points into both, so they can't be local-only without leaving dead links).

## Roadmap (from the seed spec)

1. ✅ Context-budget handoff — `docs/history/slices-1-to-7.md`
2. ✅ RAG-style retrieval + provenance — `docs/history/slices-1-to-7.md`
3. ✅ Planner + task graph + parallel workers — `docs/history/slices-1-to-7.md`
4. ✅ Second-provider cross-validator — `docs/history/slices-1-to-7.md`
5. ✅ Agent-requested tools + auto-approval — `docs/history/slices-1-to-7.md`
6. ✅ git-commit-per-task rollback, infra retry, batch eval, coverage —
   `docs/history/slices-1-to-7.md`
7. 🚧 Office-metaphor dashboard view — built on branch `slice-7-office-view`,
   unmerged pending a human UI check.
8. ✅ Hardening/finalization pass — `docs/history/slice-8.md`
9. ✅ Run against an existing repo on its own branch/tests (`workspace_mode:
   "worktree"`) — `docs/history/slice-9.md`
10. ✅ Multi-project dashboard — `docs/history/slice-10.md`,
    design/plan in `docs/plans/2026-09-08-multi-project-dashboard-*.md`

Read `docs/history/slice-8.md` and `slice-9.md` before touching `server.py`'s
origin/host checks, `runner.py`'s usage-parsing money paths, or `vcs.py`'s
guard — their closing "what was checked and found sound" notes are the
load-bearing summary if you don't have time for the full incident log.
