# Multi-project dashboard — implementation plan (roadmap slice 10)

## Metadata
- Created: 2026-09-08
- Status: draft
- Verification Rigor: critical_path (real SQLite schema change + a real data
  migration over existing users' `agentloop.db` files — a botched backfill or
  a botched `UNIQUE` constraint rebuild silently orphans task/memory history,
  which is exactly the failure class this rigor exists for)
- Plan Mode: execution_plan
- Design File: `docs/plans/2026-09-08-multi-project-dashboard-design.md`
  (5 decisions settled by interview, treated as binding — see Agreement
  Snapshot)
- Revised 2026-09-08 against a fresh independent review (pass 1): fixed 2
  BLOCKING findings (`_migrate()`'s call order relative to the legacy
  `_reconcile_memory_hits()` branch — Phase 1/Phase 2; `Loop.__init__`'s
  Default-project reconciliation silently downgrading a relative-`repo_root`
  worktree-mode install to scratch mode — Phase 3) and 2 ADVISORY findings
  (`MemoryService.read()`'s nested `maybe_promote()` call not carrying
  `project_id` — Phase 5; the "Open Decisions: none" wording read as
  contradicting the Recommended Defaults section beneath it — Agreement
  Snapshot). One additional self-review gap found and fixed while revising
  (`Store._ensure_default_project` was consumed by Phase 2's `Consumes` but
  never actually listed in Phase 1's own `Produces` — see the Self-Review
  section's revision note).
- Revised again 2026-09-08 (pass 2, a third human-approved revision pass
  beyond the normal 2-pass automated review budget — pass 1's fix was
  independently re-verified correct and left as-is): fixed 3 new BLOCKING
  findings the second review pass surfaced while checking the rest of the
  plan — (1) Phase 2's `memory_new` rebuild DDL dropped `AUTOINCREMENT`
  relative to the live `memory` table it replaces, which SQLite could let a
  deleted row's id be silently reissued to an unrelated later fact,
  contradicting the ADR's own "id preserved verbatim" guarantee — fixed in
  the ADR, Phase 2's DDL, its idempotency-detection check (now requires
  *both* the `UNIQUE` and `AUTOINCREMENT` substrings), and a new named test
  `test_memory_rebuild_preserves_autoincrement`; (2) Phase 5 threaded
  `project_id` into `MemoryService.read()`'s internal `maybe_promote()` call
  but never into `facts_for_prompt`'s own `_record_reads(...)` call — the
  **primary** path every worker/validator/planner prompt actually goes
  through via `agents._memory_block` — reproducing the same cross-project
  promotion/hit-recording leak on the dominant path; fixed in
  `facts_for_prompt`'s Allowed Scope bullet and a new named test
  `test_facts_for_prompt_promotes_against_the_task_project_not_the_active_
  one`; (3) Phase 4's declared Files/Surfaces line named only `cli.py` while
  Phase 4's own body/Sequencing-note/Produces-list required and claimed
  `Store.events(task_id=None, project_id=None)` as a Phase 4 change to
  `store.py` — fixed by adding `agentloop/store.py` to Phase 4's
  Files/Surfaces line. Also fixed the accompanying ADVISORY finding
  (`MemoryService.read()`'s new `project_id` parameter was inserted before
  `task_id`, an inconsistent ordering convention against Phase 2's own
  `Store.memory_read` idiom) by reordering Phase 5's `read()` signature to
  append `project_id` after `task_id`, and updated every reference to that
  signature (Interfaces' Produces list, the revision note above) to match.
  No other section changed; this is a targeted revision, not a rewrite.

## Agreement Snapshot

**Goal:** one running `agentloop serve` / one `agentloop.db` manages several
independent projects (repos) at once — each with its own tasks, memory, and
metrics — switchable from the CLI and the dashboard without a restart.

**Constraints** (from the design doc, binding):
- stdlib-only core; no new runtime dependency.
- Every existing SQLite invariant holds: `Store.transaction()` pairs row+event
  writes, `events` stays append-only, `claim_next_task`'s CAS is unaffected —
  `project_id` changes which rows a query returns, never claimability.
- A fresh single-project install behaves byte-for-byte as it does today,
  proven as a differential (same discipline as `vcs_enabled=False` and
  scratch-mode's own no-op proofs).
- No decision rule in `loop.py` changes (same register as slices 4-9's own
  inertness claims).

**In Scope** (design doc Success Criteria, verbatim intent):
- `projects` table (id, name UNIQUE, repo_root, workspace_mode, created_at,
  updated_at); a project is renamable/re-pointable without losing history —
  the row's `id` is the stable identity, never the path.
- Existing databases auto-migrate on open: a "Default" project is created and
  every pre-existing `tasks`/`memory` row with no `project_id` is backfilled
  to it. No row is ever left unscoped.
- `tasks.project_id` and `memory.project_id` (FK to `projects.id`);
  `memory`'s uniqueness becomes `UNIQUE(project_id, tier, key)`.
- CLI: `add|plan|run|status|events` accept `--project <name>`; omitting it
  uses the active default project.
- `agentloop project add|rename|repoint|list|archive` CLI surface.
- Dashboard REST API (`/api/tasks`, `/api/metrics`, memory endpoints, the SSE
  stream) accept `?project=<id>` and scope every query **server-side**.
- New `/api/projects` CRUD.
- `Loop`/`LoopConfig` resolve `repo_root`/`workspace_mode` per-task from the
  task's `project_id` row instead of solely from `loopconfig.json`.
- `Store.vcs_repo_pin`/`vcs_repo_pins` need no change (already keyed per
  resolved `repo_root` path since slice 9 P2).

**Out of Scope** (design doc, verbatim):
- Per-project `agents.json`/`loopconfig.json` overrides — config stays
  global/shared across every project.
- Any change to decision rules, thresholds, or budget-cap semantics.
- Cross-project task dependencies.
- New parallel-execution scheduling across projects.
- Slice 7's office-view UI (unrelated).

**Additionally out of scope for this plan** (small, low-blast-radius
narrowing beyond the design doc — see Recommended Defaults):
- `agentloop workspace prune|rebless` stay unscoped by project (they already
  take an explicit `--repo-root` override; an operator managing several
  projects passes the right one per invocation).
- The project charter (`store.charter_*`) stays global across every project
  in this slice, in the same register as `agents.json`/`loopconfig.json` —
  it is operator-authored governance applied uniformly, not a per-repo fact
  like memory.

**Open Decisions:** none requiring re-litigation of the design doc's 5 settled
decisions — see Recommended Defaults below for the narrow, reversible choices
this plan made on its own to stay buildable (a reader should not skim past
those as if nothing was decided; they are real judgment calls, just not ones
that reopen the interview).

## Codebase Reality Check

Read in full before planning (see file-by-file findings folded into the
phases below): `agentloop/store.py` (`_SCHEMA`, `_migrate`, `_row_to_task`,
`claim_next_task`/`_UNBLOCKED`, `add_task`/`list_tasks`, the full
`memory_write`/`memory_read`/`memory_promote`/`_merge_into_loop`/`memory_list`
block, `run_metrics`, `events_since`), `agentloop/config.py` (`LoopConfig`,
`repo_root`/`workspace_mode` fields and their `__post_init__` validation),
`agentloop/loop.py` (`Loop.__init__`, `_worktree_repo_root`/`_worktree_pin`
and all seven call sites, `run_task`, `plan`, `run`/`_run_serial`/
`_run_parallel`), `agentloop/cli.py` (`_build`, `main`, `_dispatch`, every
subparser), `agentloop/server.py` (`do_GET`/`do_POST` routing, `_task_json`,
`_set_config_repo`, `_stream`), `agentloop/memory.py` (`MemoryService`, all
methods), `agentloop/agents.py` (`_memory_block`, `run_worker`/`run_validator`
/`run_planner` signatures), `agentloop/models.py` (`Task` dataclass),
`web/src/types.ts`, `web/src/api.ts`, `web/src/useLiveLoop.ts`. Test
conventions read from `tests/test_loop.py`, `tests/test_migration.py`,
`tests/test_cli.py`, `tests/test_server.py`.

**Existing ADRs / prior decisions treated as SETTLED** (no glob hit for
`docs/adr/`; CLAUDE.md itself is the project's ADR log): `vcs_repo_pins`
already keys per resolved `repo_root` path — untouched by this slice.
`_migrate()` only ever *adds* (never destructively rewrites) — this slice's
one deliberate exception is the `memory` table's `UNIQUE` constraint rebuild
(Phase 2), which is flagged explicitly below as a documented departure from
that rule, not a silent one, because SQLite cannot alter a `UNIQUE`
constraint via `ALTER TABLE ADD COLUMN`.

**Plan-vs-code gaps found by reading, not assumed:**
1. `Loop._worktree_repo_root()` takes no arguments and reads only
   `self.config` — every one of its exactly 5 call sites in `loop.py`
   (verified by grep: lines 689 in `plan()`, 1367 in `run_task()`, 2082 in
   `human_approve()`, 2122 in `human_reject()`, 2300 in `human_redo()`) must
   be updated to pass the task whose project it should resolve.
2. `Store.claim_next_task(worker_id)` has no project filter — today's single
   query already claims across every task in the db, which is *why* per-task
   `repo_root` resolution is the mechanism this slice needs: one running
   `agentloop run` claims across every registered project, resolving each
   task's own repo per iteration.
3. `MemoryService`/`Store`'s memory methods have **no project concept at
   all** today; `memory.value`'s own `UNIQUE(tier, key)` constraint is
   table-level DDL, not an index that can be dropped/recreated — changing it
   for an existing db needs SQLite's documented create-copy-drop-rename
   recipe, not a `_migrate()` `ALTER TABLE ADD COLUMN`.
4. `cli._build()` is the one bootstrap point every CLI subcommand (except
   `init-registry`) passes through — `Store(config.db_path)` then
   `Loop(store, runner, registry, config)` — so `Loop.__init__` is the
   natural, already-established seam for config-aware, once-per-process
   bootstrapping (it already does this for `config_warning` events).
5. `events_since`/`events` return raw dicts with `task_id: int | None` —
   `memory_write`/`charter_set`/`config_warning`/project-management events
   are logged with `task_id=None` today and have no project attribution at
   all.
6. `Store._migrate()` (store.py:562-609) calls `self._reconcile_memory_hits()`
   at its **current end** (lines 608-609) whenever a database predates the
   `memory_hits` table — and `_reconcile_memory_hits()` → `_merge_into_loop()`
   → `self.memory_write(...)` (store.py:611-660, 1525-1618/1598). Once Phase 2
   makes `memory_write` require the `memory.project_id` column
   (`ON CONFLICT(project_id, tier, key)`), that column must already exist
   by the time this legacy call runs on a pre-memory_hits db — so Phase 1's
   `_ensure_default_project()` and Phase 2's
   `_rebuild_memory_unique_constraint()` must both execute **before** line
   608's `if "memory" in existing ...` check, not after it. Phase 1 and
   Phase 2 below fix this at the source (an explicit reorder of `_migrate()`'s
   body, not a workaround) and Phase 2 adds the regression test that would
   have caught it.

## Hidden-Assumption Pass

| Assumption | Classification | Resolution |
|---|---|---|
| `Store(db_path)` alone (no `LoopConfig`) must never leave a row unscoped, since `eval.py` and raw store users construct it without a `Loop` | proven_by_code (`eval.py` constructs `Store` directly; CLAUDE.md's own "no row is ever left unscoped" is unconditional) | Bootstrap the Default project inside `Store._migrate()` itself, not in `Loop.__init__` — see Phase 1. |
| `agentloop run` (no `--project`) must keep claiming across every registered project in one pass | proven_by_code (per-task `repo_root` resolution only has a reason to exist if one loop instance spans projects — see gap 2 above) | `Loop.run(project_id=None)` — `None` is unrestricted, matching today's exact query; `--project` narrows it. |
| `MemoryService.facts_for_prompt`/`remember`/`read` callers that never pass `project_id` (agents.py already calls `facts_for_prompt`; ~60 existing test call sites across 4 files never will) must keep working unchanged | proven_by_code (grep of every call site) | `project_id: int \| None = None` everywhere in the memory stack, meaning "the active/default project" — no existing call site needs editing. |
| A pre-existing db's `memory` table cannot have its `UNIQUE(tier, key)` constraint altered in place | proven_by_code (SQLite `ALTER TABLE` cannot modify a table-level constraint; this is documented SQLite behavior, not project-specific) | Phase 2 uses SQLite's own documented create-copy-drop-rename recipe, id-preserving, `PRAGMA foreign_keys=OFF` around it (so `memory_hits`' FK survives), inside one `Store.transaction()`. |

## Differences from Agreement

Everything the design doc's 5 interview-settled decisions and Success
Criteria state is implemented as stated, unchanged. The differences below
are additions/narrowings this plan makes to be buildable, none of them
touching a decision rule, data integrity, or security boundary — each is
listed again, unapproved, under Recommended Defaults immediately below:
- `agentloop project use NAME` CLI verb (not in the design doc's literal
  CLI list) — added because the design doc's own stated CLI default rule
  ("omitting `--project` uses whichever was last selected") has no other
  mechanism to change what "last selected" means.
- Project archive refuses outright rather than accepting a confirm
  override (design doc poses this as an open either/or).
- SSE/`events` filtering treats `task_id IS NULL` events (memory/charter/
  config/project-management) as always-visible regardless of `?project=`
  (design doc does not specify this; the alternative — hiding them per
  project — has no natural project attribution to filter on without
  inventing one).
- The project charter stays global/unscoped in this slice (design doc's
  Architecture section names memory as needing scoping and is silent on
  charter; charter is treated as config-like, matching the doc's own
  Out-of-Scope framing for config).
- `agentloop workspace prune|rebless` stay unscoped by project (design doc
  does not mention them at all).

## Recommended Defaults (explicitly unapproved — narrow, reversible, low blast radius)

- `agentloop project use NAME` → sets the store's "active/default" project.
  The design doc's CLI list (`add|rename|repoint|list|archive`) has no verb
  for changing which project `--project`-omitted commands target; without
  one, a multi-project CLI user could never stop typing `--project` on every
  command. Reversible (just another CLI verb); no data-model impact.
- Archiving a project with non-terminal tasks is **refused outright, no
  confirm override** (the design doc poses this as an open either/or in its
  own Error Handling section). Picked for symmetry with `human_approve`
  refusing a pending task and `approve_plan` refusing a zero-task plan —
  explicit refusal, never a force flag, is this codebase's existing pattern.
- SSE (`/api/stream?project=`) and `agentloop events` filter **task-scoped**
  events by the task's `project_id`; events with `task_id IS NULL`
  (`memory_*`, `charter_*`, `config_warning`, `project_*`) stay visible
  regardless of the filter — same register as charter/config staying global.
- The project charter stays global across every project in this slice (see
  Out of Scope above) — not named in the design doc's Architecture section
  at all, unlike memory, which is named explicitly as needing scoping.
- `agentloop workspace prune|rebless` stay unscoped by project (already take
  an explicit `--repo-root`).

None of these touch a decision rule, are irreversible, or block correctness
— each is easily widened in a later slice without a migration.

## Architecture Decision Records

### ADR: `memory`'s `UNIQUE(tier, key)` → `UNIQUE(project_id, tier, key)` via table rebuild, not `ALTER TABLE`
**Context:** SQLite cannot alter a table-level `UNIQUE` constraint with
`ALTER TABLE ADD COLUMN`; `_migrate()`'s "only ever adds" convention has no
tool for this.
**Decision:** Phase 2 performs SQLite's own documented recipe — `PRAGMA
foreign_keys=OFF`, `CREATE TABLE memory_new(id INTEGER PRIMARY KEY
AUTOINCREMENT, ... UNIQUE(project_id, tier, key))`, `INSERT INTO memory_new
SELECT <explicit columns incl. id> FROM memory`, `DROP TABLE memory`, `ALTER
TABLE memory_new RENAME TO memory`, `PRAGMA foreign_keys=ON`, all inside one
`Store.transaction()`. `id` values are preserved via an explicit column list
(never re-autoincremented on the *copy* — a value already present in the
`id` column of an `INSERT ... SELECT` is inserted verbatim, `AUTOINCREMENT`
only governs an *omitted* id on a future insert), so `memory_hits.memory_id`
and every past `retrieval` event's recorded memory id stay valid. The rebuilt
table itself must carry `AUTOINCREMENT`, not just `INTEGER PRIMARY KEY`: the
live `memory` table this rebuild replaces already has it (`store.py:224-225`)
specifically because rows are genuinely deleted in normal operation
(`_merge_into_loop`'s `DELETE FROM memory WHERE id=?` on a collision merge,
and `memory_delete` behind the dashboard's `POST /api/memory/{id}/reject` and
`agentloop memory reject`) — without `AUTOINCREMENT`, SQLite is free to reuse
a deleted row's id on the very next unspecified-id `INSERT` (exactly how
`memory_write` inserts) whenever that row held the table's then-current max
id, which would silently point a stale `memory_hits.memory_id` or a past
`retrieval` event's recorded id at an unrelated, later-created fact — the
precise failure this ADR's own "id preserved verbatim" guarantee claims not
to have. `_rebuild_memory_unique_constraint`'s idempotency-detection substring
check (Phase 2's Allowed Scope) widens accordingly: it checks for both the
literal `"UNIQUE(project_id, tier, key)"` substring and the literal
`"AUTOINCREMENT"` substring in `sqlite_master.sql` for the `memory` table,
so a hypothetical prior rebuild that got the `UNIQUE` clause right but
dropped `AUTOINCREMENT` is not mistaken for "already done."
**Rejected alternatives:** (1) Leave `UNIQUE(tier, key)` unchanged and
enforce project-uniqueness only in application code — rejected because a
"same key" race between two projects would still throw a genuine SQLite
`UNIQUE` violation from the *old* constraint, and `memory_write`'s upsert
(`ON CONFLICT(tier, key) DO UPDATE`) would silently overwrite Project A's
value with Project B's on the very first cross-project name collision. (2)
Add a separate `project_memory` junction table instead of a column —
rejected, over-engineered for a 1:1 fact-to-project relationship and it would
have made every existing `memory.*` query a join for no benefit.
**Consequences:** This is the one deliberate exception to "`_migrate()` only
ever adds," and is called out as such rather than silently bent.

### ADR: project resolution defaults to `None` = "the active project," never a required parameter
**Context:** ~60 existing test call sites across `test_memory.py`,
`test_memory_promotion.py`, `test_pinned_memory.py`, `test_retrieval.py`
call `MemoryService.remember`/`.read`/`.facts_for_prompt` with no project
concept; hundreds of `test_loop.py`/etc. tests call `store.add_task(task)`
directly.
**Decision:** `project_id: int | None = None` at every new parameter in
`Store`/`MemoryService`/`Loop`, resolved internally to the default project
when omitted. This is the existing codebase's own idiom (`task_id: int |
None = None` on `memory_read`, `approved_only: bool = True`), applied
consistently.
**Rejected alternative:** Require `project_id` everywhere and rewrite every
existing call site — rejected as unnecessary churn against files this slice
has no other reason to touch, and it would make "single-project install
behaves byte-for-byte" a much larger surface to prove identical.
**Consequences:** A single-project install is trivially byte-for-byte
identical (every call resolves to the same one project every time). A test
proving genuine cross-project isolation must pass `project_id=` explicitly.

## Phase 1 — Store: `projects` table, `tasks.project_id`, migration/backfill

**Objective:** Add the `projects` table, `tasks.project_id`, the
`_ensure_default_project()` bootstrap + backfill, and the project CRUD
accessors — the schema and store-level foundation every later phase reads.
`memory.project_id` is deliberately **not** in this phase (see Phase 2 ADR).

**Files/Surfaces:** `agentloop/store.py`, `agentloop/models.py` (`Task.
project_id` field).

**Dependencies:** none.

**Allowed Scope:**
- `_SCHEMA`: new `CREATE TABLE IF NOT EXISTS projects (id INTEGER PRIMARY
  KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, repo_root TEXT NOT NULL,
  workspace_mode TEXT NOT NULL DEFAULT 'scratch', is_default INTEGER NOT
  NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0, created_at REAL NOT
  NULL, updated_at REAL NOT NULL)`, declared before `tasks` (FK target must
  exist when the script runs, matching the existing `charter`-before-
  `attempts` convention).
- `_SCHEMA`'s `tasks` table gains `project_id INTEGER NOT NULL REFERENCES
  projects(id)` for fresh installs.
- `_migrate()`'s `additions` list gains `("tasks", "project_id", "INTEGER")`
  — bare, nullable, no FK (same trap and same handling as `plan_id`/
  `charter_version`: SQLite's `ALTER TABLE ADD COLUMN` rejects a foreign key
  with a non-NULL default and cannot add one retroactively).
- New private `Store._ensure_default_project(self) -> int`: if `projects` is
  empty, INSERT `('Default', '.', 'scratch', is_default=1, archived=0, now,
  now)` via a raw internal INSERT (bypassing the public `create_project`'s
  validation — this is bootstrap machinery, not a human action, matching
  `_reconcile_memory_hits`'s own precedent for bypassing public APIs).
  Returns the resolved default project's id whether it was just created or
  already existed. In the same call, backfills `UPDATE tasks SET project_id=?
  WHERE project_id IS NULL` using that id, and logs
  `project_migration_backfill` with `{"project_id": ..., "tasks_backfilled":
  <rowcount>}` — only when `rowcount > 0`, matching `_reconcile_memory_hits`'s
  "only log what actually changed" discipline. Both writes are one
  `Store.transaction()`.
- **Ordering (load-bearing — see Plan-vs-code gap #6 above and Phase 2's ADR
  addendum):** `_migrate()`'s call to `self._ensure_default_project()` is
  inserted immediately after the existing `additions` loop and **strictly
  before** the pre-existing `if "memory" in existing and "memory_hits" not in
  existing: self._reconcile_memory_hits()` block at store.py:608-609 — never
  at the literal end of the function. On its own, in this phase, that is an
  inert relocation (Phase 1 does not touch `memory_write`, so
  `_reconcile_memory_hits()` behaves identically wherever
  `_ensure_default_project()` sits relative to it); the placement exists so
  Phase 2 can slot `_rebuild_memory_unique_constraint()` into the same
  already-correct spot without a second reorder. `_migrate()`'s new body
  order is therefore: (1) the `additions` loop, unchanged; (2)
  `_ensure_default_project()` + task backfill; (3) [Phase 2]
  `_rebuild_memory_unique_constraint()`; (4) the pre-existing
  `_reconcile_memory_hits()` legacy branch, unchanged in its own logic.
- `models.Task` gains `project_id: int | None = None`.
- `_row_to_task` reads `project_id=row["project_id"]`.
- `Store.add_task(task)` resolves `task.project_id =
  self.resolve_project(task.project_id)` before the INSERT (so every existing
  caller passing a bare `Task(...)` with no `project_id` set gets the default
  transparently — no existing call site needs editing) and includes
  `project_id` in the INSERT column list; `task_defined`'s event payload
  gains `"project_id": task.project_id`.
- New: `Store.resolve_project(self, project: int | str | None) -> int` —
  `None` → the default project's id (never raises, bootstrap guarantees one
  exists); an `int` → verified to exist, else `KeyError(f"unknown project
  {project!r}")`; a `str` → looked up by name, else the same `KeyError`
  (message text is what `cli.main`'s existing `except (KeyError, ValueError)`
  handler renders as `error: unknown project '<name>'`).
- New: `Store.default_project_id(self) -> int` (reads the `is_default=1`
  row's id; calls `_ensure_default_project()` first as a safety net, though
  it is a no-op after `__init__`).
- New: `Store.create_project(self, name, repo_root, workspace_mode="scratch",
  is_default=False) -> int` — validates `name` not already taken
  (`ValueError`), `workspace_mode in ("scratch", "worktree")` (`ValueError`,
  mirrors `LoopConfig.__post_init__`), `repo_root` absolute + `os.path.isdir`
  (`ValueError`, mirrors `_set_config_repo`'s existing validation). Logs
  `project_created`.
- New: `Store.get_project(self, project_id) -> dict | None`,
  `Store.get_project_by_name(self, name) -> dict | None`,
  `Store.list_projects(self, include_archived=False) -> list[dict]`.
- New: `Store.rename_project(self, project_id, new_name) -> None` — refuses
  a name collision with another project. Logs `project_renamed`.
- New: `Store.repoint_project(self, project_id, repo_root, workspace_mode)
  -> None` — same validation as `create_project`. Logs `project_repointed`.
- New: `Store.set_default_project(self, project_id) -> None` — one
  transaction, clears `is_default` on every row then sets it on the target
  (enforces "exactly one default" without a trigger, matching this schema's
  existing no-trigger convention). Logs `project_default_changed`.
- New: `Store.archive_project(self, project_id) -> None` — refuses
  (`ValueError`) if `is_default=1` or the project has any task not in
  `{done, failed, aborted}`. Logs `project_archived`.
- `Store.list_tasks(self, project_id: int | None = None) -> list[Task]` —
  `None` (default) is unfiltered, byte-for-byte today's query; an int
  filters `WHERE project_id=?`.
- `Store.claim_next_task(self, worker_id, project_id: int | None = None) ->
  Task | None` — `None` unrestricted (today's exact query, ordering
  unchanged); an int appends `AND t.project_id=?` to `_UNBLOCKED`'s query.
- `Store.add_dependency` refuses a cross-project edge: `ValueError` when the
  two tasks' `project_id` differ (mirrors the existing cycle refusal — "a
  store invariant, not a hope about the planner").

**Out-of-Scope Drift:** No `memory` changes (Phase 2). No `Loop`/`cli.py`/
`server.py` changes (Phases 3+). No `run_metrics`/`events_since` project
filtering (Phase 6).

**Expected Artifacts:** Updated `_SCHEMA`, `_migrate`, and the new accessor
methods in `agentloop/store.py`; `Task.project_id` in `agentloop/models.py`;
new `tests/test_projects.py` (CRUD + resolve_project + archive-refusal +
cross-project dependency refusal) and a migration differential added to
`tests/test_migration.py`.

**Required Checks:**
- `.venv\Scripts\python.exe -m pytest -q tests/test_projects.py
  tests/test_migration.py tests/test_store_atomicity.py tests/test_loop.py`
- `ruff format --check .`

**Checkpoint Type:** none (AFK — mechanical schema/store work with a
committed differential test proving the migration).

**Exit Criteria:** All required checks green. The migration differential
(below) passes. `store.resolve_project(None)` on a freshly-opened *brand
new* db returns a valid id whose row has `name == "Default"`.

**Test Seams:** integration (real SQLite file via `tmp_path`, matching
`test_migration.py`'s and `test_store_atomicity.py`'s existing seam — no new
seam introduced).

**Interfaces:**
- Consumes: none.
- Produces (verbatim, later phases must match): `Store._ensure_default_project()
  -> int` (Phase 2 consumes this exact name — added here during the
  fresh-review revision, since Phase 2's `Consumes` already named it without
  it ever appearing in this list, a self-review gap fixed alongside the
  ordering fix above); `Store.resolve_project(project:
  int | str | None) -> int`; `Store.default_project_id() -> int`;
  `Store.create_project(name, repo_root, workspace_mode="scratch",
  is_default=False) -> int`; `Store.get_project(project_id) -> dict | None`;
  `Store.get_project_by_name(name) -> dict | None`;
  `Store.list_projects(include_archived=False) -> list[dict]`;
  `Store.rename_project(project_id, new_name) -> None`;
  `Store.repoint_project(project_id, repo_root, workspace_mode) -> None`;
  `Store.set_default_project(project_id) -> None`;
  `Store.archive_project(project_id) -> None`;
  `Store.list_tasks(project_id: int | None = None) -> list[Task]`;
  `Store.claim_next_task(worker_id, project_id: int | None = None) -> Task |
  None`; `Task.project_id: int | None`; events `project_created`,
  `project_renamed`, `project_repointed`, `project_default_changed`,
  `project_archived`, `project_migration_backfill`.

## Phase 2 — Store: `memory.project_id` and the `UNIQUE` constraint rebuild

**Objective:** Give `memory` a `project_id` column and rebuild its `UNIQUE`
constraint to `(project_id, tier, key)`, id-preserving and `memory_hits`-safe,
per the ADR above. This is the highest-risk phase in the slice.

**Files/Surfaces:** `agentloop/store.py` only.

**Dependencies:** Phase 1 (`Store._ensure_default_project()`).

**Allowed Scope:**
- `_SCHEMA`'s `memory` table gains `project_id INTEGER NOT NULL REFERENCES
  projects(id)` and its `UNIQUE(tier, key)` becomes `UNIQUE(project_id, tier,
  key)` for fresh installs.
- New `Store._rebuild_memory_unique_constraint(self, default_project_id:
  int) -> None`, called from `_migrate()` immediately after
  `_ensure_default_project()` and, per Phase 1's ordering note,
  **before** the pre-existing `if "memory" in existing and "memory_hits" not
  in existing: self._reconcile_memory_hits()` block. This is the fix for
  Plan-vs-code gap #6: `_reconcile_memory_hits()` → `_merge_into_loop()` →
  `memory_write()`, and this phase's `memory_write` requires
  `memory.project_id` to already exist — so on a pre-memory_hits legacy db,
  the column must land before that legacy branch runs, not after it. No new
  call site is introduced in this phase; the position `_ensure_default_
  project()` already occupies (Phase 1) is exactly where this call belongs,
  so this phase adds one line at an already-correct point rather than
  reordering `_migrate()` a second time. Detects whether the rebuild already
  ran by reading `sqlite_master.sql` for the `memory` table and checking for
  **both** the literal substring `"UNIQUE(project_id, tier, key)"` **and**
  the literal substring `"AUTOINCREMENT"` — idempotent, no-ops on an
  already-rebuilt or already-fresh db; requiring both substrings (not just
  the `UNIQUE` one) is what stops a hypothetical partially-correct prior
  rebuild — right `UNIQUE` clause, missing `AUTOINCREMENT` — from being
  mistaken for "already done" and skipped (see the ADR above). When it has
  not run:
  1. `PRAGMA foreign_keys=OFF` (outside any transaction — SQLite ignores a
     PRAGMA-foreign_keys change made mid-transaction).
  2. `with self.transaction():` — `CREATE TABLE memory_new (id INTEGER
     PRIMARY KEY AUTOINCREMENT, project_id INTEGER NOT NULL REFERENCES
     projects(id), tier TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT
     NULL, hit_count INTEGER NOT NULL DEFAULT 0, approved INTEGER NOT NULL
     DEFAULT 0, pinned INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL,
     last_used_at REAL, UNIQUE(project_id, tier, key))` — `AUTOINCREMENT` is
     load-bearing, not decoration: the live `memory` table this replaces
     already has it (store.py:224-225) because rows are genuinely deleted in
     normal operation (`_merge_into_loop`'s `DELETE FROM memory WHERE id=?`
     on a collision merge; `memory_delete` behind the dashboard's `POST
     /api/memory/{id}/reject` and `agentloop memory reject`), and without it
     SQLite may reissue a deleted row's id to the next unspecified-id
     `INSERT` whenever that row held the table's then-current max id —
     silently pointing a stale `memory_hits.memory_id` or a past `retrieval`
     event's recorded id at an unrelated, later-created fact; `INSERT INTO
     memory_new (id,
     project_id, tier, key, value, hit_count, approved, pinned, created_at,
     last_used_at) SELECT id, ?, tier, key, value, hit_count, approved,
     pinned, created_at, last_used_at FROM memory` (explicit column list,
     `id` preserved verbatim on the copy itself — an `INSERT` supplying its
     own `id` value is stored as given regardless of `AUTOINCREMENT`, which
     only governs a *future*, id-omitting insert such as `memory_write`'s —
     so `memory_hits.memory_id` and any past `retrieval` event's recorded id
     stay valid both immediately after this copy and against every fact
     written afterward); `DROP TABLE memory`; `ALTER TABLE memory_new RENAME
     TO memory`; log
     `memory_project_unique_migrated` with `{"rows": <count>}`.
  3. `PRAGMA foreign_keys=ON`.
- `_migrate()`'s bare `("memory", "pinned", ...)`/`("memory",
  "last_used_at", ...)` additions still run first (unchanged) — the rebuild
  reads/copies those columns too, so it must run after them, not before.
- `Store.memory_write(self, tier, key, value, approved=False, pinned=False,
  project_id: int | None = None) -> None` — resolves `project_id =
  self.resolve_project(project_id)`; INSERT gains the `project_id` column;
  `ON CONFLICT(tier, key)` becomes `ON CONFLICT(project_id, tier, key)`.
- `Store.memory_read(self, tier, key, approved_only=True, task_id=None,
  project_id: int | None = None) -> str | None` — resolves `project_id` the
  same way; `WHERE tier=? AND key=?` gains `AND project_id=?`.
- `Store.memory_list(self, tier=None, approved_only=False, project_id: int |
  None = None) -> list[dict]` — `project_id=None` here means "every project"
  (matches `list_tasks`'s "None = unfiltered" convention, **not**
  "resolve_project(None) = the default" — this is the one method where the
  two meanings would otherwise collide, so it is documented explicitly in the
  docstring and covered by a test that a `None` list includes both projects'
  facts).
- `Store.memory_promote(mem_id)` — signature unchanged; the collision lookup
  (`SELECT id FROM memory WHERE tier='loop' AND key=?`) gains `AND
  project_id=?`, reading the row's own `project_id` (a promoted fact never
  crosses projects).
- `Store._merge_into_loop` — signature unchanged; every `memory_write`/
  `SELECT`/`INSERT`/`UPDATE` inside it is scoped by the rows' own (shared)
  `project_id`. Concretely, its internal `self.memory_write(...)` call gains
  an explicit `project_id=project["project_id"]` argument (never left to
  default-resolve) — the merged pair's own project, not necessarily whatever
  project happens to be "active" when the merge runs (relevant once
  `memory_promote`'s live collision path can fire from any project's task,
  not only during migration bootstrap where there is exactly one project).
- `Store._find` is `memory.py`'s helper, not `store.py`'s — untouched here
  (Phase 5).

**Out-of-Scope Drift:** No `MemoryService`/`agents.py` changes (Phase 5).

**Expected Artifacts:** Updated `_SCHEMA`, `_migrate`, and the memory
accessor methods in `agentloop/store.py`; a dedicated migration differential
in `tests/test_migration.py` (see below); cross-project memory isolation
tests in `tests/test_projects.py` or a new `tests/test_memory_projects.py`;
a named regression test in `tests/test_migration.py` for the exact scenario
Plan-vs-code gap #6 identifies —
`test_legacy_pre_memory_hits_db_migrates_without_project_id_error`: hand-write
an old-shape db (per `test_migration.py`'s existing convention) with a
`memory` table, **no** `memory_hits` table, and at least one `project`/`loop`
key collision (so `_reconcile_memory_hits` → `_merge_into_loop` →
`memory_write` actually fires on the legacy path), then open it through
`Store.__init__` and assert it succeeds with no `sqlite3.OperationalError`
and the merged row carries the correct `project_id`.
A second named regression test in `tests/test_migration.py`,
`test_memory_rebuild_preserves_autoincrement`: hand-write an old-shape db
with several `memory` rows, run the rebuild, delete the row currently
holding the table's max `id` via `Store.memory_delete` (or the raw
`_merge_into_loop` collision path), write a new fact via
`Store.memory_write` with no explicit id, and assert the new row's `id` is
strictly greater than every id that has ever existed in the table (i.e. it
is **not** the just-deleted max id reissued) — falsified by temporarily
reverting the DDL to `id INTEGER PRIMARY KEY` (no `AUTOINCREMENT`) and
confirming the assertion goes red, per this phase's RED-FIRST discipline.

**Required Checks:**
- `.venv\Scripts\python.exe -m pytest -q tests/test_migration.py
  tests/test_memory.py tests/test_memory_promotion.py
  tests/test_pinned_memory.py tests/test_retrieval.py tests/test_projects.py`
  (the four pre-existing memory test files must pass **unmodified** — they
  never pass `project_id`, so this is the "byte-for-byte on a single-project
  install" proof for the memory subsystem specifically).
- `ruff format --check .`

**Checkpoint Type:** none (AFK — but see the Critical-Path Verification
Strategy section below for the required RED-FIRST proof before this phase
may be marked done).

**Exit Criteria:** All required checks green, including all four pre-existing
memory test files passing with zero edits. The rebuild differential (below)
passes. A `memory_hits` row written before the rebuild still resolves to the
correct fact after it. `test_memory_rebuild_preserves_autoincrement` passes,
proving a deleted row's id is never reissued to a later fact.

**Test Seams:** integration (real SQLite file via `tmp_path`; the rebuild
must be tested against a real on-disk db written with the pre-slice-10
schema, not `:memory:`, matching `test_migration.py`'s existing convention of
hand-writing an old-shape schema rather than reading today's `_SCHEMA`).

**Interfaces:**
- Consumes: `Store.resolve_project(project) -> int`,
  `Store._ensure_default_project() -> int` (Phase 1, verbatim).
- Produces: `Store.memory_write(tier, key, value, approved=False,
  pinned=False, project_id: int | None = None) -> None`;
  `Store.memory_read(tier, key, approved_only=True, task_id=None, project_id:
  int | None = None) -> str | None`; `Store.memory_list(tier=None,
  approved_only=False, project_id: int | None = None) -> list[dict]`; event
  `memory_project_unique_migrated`.

## Phase 3 — Loop: per-task `repo_root`/`workspace_mode` resolution, `run`/`plan` project scope

**Objective:** Thread `Task.project_id` through `Loop._worktree_repo_root`
and every call site; add project scope to `Loop.run`/`Loop.plan`; bootstrap
the Default project's real `repo_root`/`workspace_mode` from `LoopConfig` on
first genuine load.

**Files/Surfaces:** `agentloop/loop.py` only.

**Dependencies:** Phase 1.

**Allowed Scope:**
- `Loop.__init__` gains, after the existing `config_warning`/credential-like
  warning blocks: a reconciliation step — if `self.store.list_projects()`
  has exactly one row, its name is `"Default"`, and its `(repo_root,
  workspace_mode)` still equal the bootstrap placeholder `(".",
  "scratch")` while `self.config.(repo_root, workspace_mode)` differ from
  that placeholder (compared against the **raw** `config.repo_root` string,
  unchanged from today — an operator who genuinely configured `"."` is
  indistinguishable from the placeholder and correctly stays a no-op), the
  step resolves `resolved_repo_root = os.path.abspath(config.repo_root)`
  **before** validating or storing it — the same lexical `os.path.abspath`
  convention `_worktree_repo_root` already uses at use time (never
  `Path.resolve()`, for the same junction-safety reason), and load-bearing
  here specifically because `LoopConfig.repo_root` is not required to be
  absolute anywhere in `config.py` (its default is the literal relative
  string `"."`, and slice 9's own `_worktree_repo_root` exists precisely
  because a relative `repo_root` is a real, supported worktree-mode
  configuration — see BLOCKING #2 of the fresh review). It then calls
  `self.store.repoint_project(default_id, resolved_repo_root,
  config.workspace_mode)`. This is what makes "one 'Default' project is
  created from today's `loopconfig.json` `repo_root`/`workspace_mode`" true
  for an operator upgrading a pre-slice-9 worktree-mode install — including,
  after this fix, one whose `repo_root` is relative — and it is a no-op for
  the common case (scratch mode, `repo_root="."`, matching the placeholder
  exactly). The call is wrapped in `try: ... except Exception as exc: ...`
  exactly like the two existing warning blocks above it — bootstrapping the
  project record must never break loop construction — but unlike a bare
  `except Exception: pass`, the except clause logs a `config_warning` event
  (same channel, same try/except-around-log_event pattern the two existing
  blocks use) naming `resolved_repo_root`, `config.workspace_mode` and
  `str(exc)`, so a *genuine* reconciliation failure (e.g. `repoint_project`'s
  `os.path.isdir` check failing because the configured directory no longer
  exists) is degraded-but-visible rather than degraded-and-silent — the
  residual case this fix does not eliminate, only makes honest. This does
  not widen scope beyond the review's fix request: resolving the path
  absolute first is what the review asked for, and reusing the existing
  `config_warning` channel for the remaining genuine-failure case is the
  same pattern already used twice in this exact constructor, not a new
  mechanism.
- `Loop._worktree_repo_root(self, task: Task | None = None) -> Path | None`
  — new optional `task` parameter (was zero-arg). When `task` is given and
  `task.project_id` resolves to a project row, reads that row's own
  `workspace_mode`/`repo_root` (`None` if the row's `workspace_mode !=
  "worktree"`, else `Path(os.path.abspath(row["repo_root"]))` — same lexical
  `os.path.abspath` convention as today, never `Path.resolve()`, for the same
  junction-safety reason the existing docstring gives). When `task` is
  `None` (the plan-row call site, before any task workspace exists) or the
  project lookup fails, falls back to `self.config.workspace_mode`/
  `self.config.repo_root` exactly as today — this is the byte-for-byte
  fallback that keeps a bare `Loop(store, runner, registry, config)` with no
  `projects` row set up behaving identically to before this slice.
- All 5 call sites updated to pass the task in scope: `plan()`'s (line 689,
  passing `plan_task` — called after `self.store.add_task(plan_task)` so
  `plan_task.id`/`project_id` are populated), `run_task()`'s (line 1367,
  passing `task`), `human_approve()`'s (line 2082, passing `fresh`),
  `human_reject()`'s (line 2122, passing `task`), `human_redo()`'s (line
  2300, passing `task`).
- `plan_task = Task(..., project_id=self.store.resolve_project(project_id))`
  in `Loop.plan`; new parameter `Loop.plan(self, goal, acceptance_criteria,
  title="", risk_level=1, project_id: int | str | None = None) -> Task`.
  Children created in the plan-persistence transaction gain `project_id=
  plan_task.project_id`.
- `Loop.run(self, max_tasks: int | None = None, project_id: int | None =
  None) -> int` — threads `project_id` into `self.store.claim_next_task
  (worker_id, project_id=project_id)` in both `_run_serial` and
  `_run_parallel` (every thread in the parallel case shares the same
  filter — no per-project scheduling, matching Out of Scope).

**Out-of-Scope Drift:** No `cli.py` changes yet (Phase 4) — this phase adds
the `Loop` parameters `cli.py` will call in Phase 4, but does not wire the
CLI itself. No new decision rule — `_worktree_repo_root`'s fallback shape is
identical to today's function, just parameterized by which config source it
reads.

**Expected Artifacts:** Updated `agentloop/loop.py`; new
`tests/test_loop_projects.py` covering: (a) two tasks in two different
worktree-mode projects each resolve their own `repo_root` inside one
`Loop.run()` call with no `--project` filter; (b) `Loop.run(project_id=X)`
claims only project X's tasks even when project Y has pending work; (c) a
`Loop` built with no project awareness exercised (single default project)
reproduces `test_worktree_loop.py`'s existing assertions unmodified; (d)
**BLOCKING #2 regression** —
`test_default_project_reconciliation_preserves_relative_worktree_repo_root`:
construct a `Store` whose lone "Default" project row still sits at the
bootstrap placeholder `(".", "scratch")`, build a `LoopConfig` with
`workspace_mode="worktree"` and a **relative** `repo_root` (e.g.
`"..\\some-repo"` / `"./some-repo"`, pointed at a real `tmp_path` directory
via a relative path from the test's cwd), construct `Loop(store, ..., config)`,
and assert the Default project's row afterward has `workspace_mode ==
"worktree"` (not silently dropped to `"scratch"`) and `repo_root` equal to
`os.path.abspath` of the configured value. A second case constructs the same
setup with a `repo_root` pointing at a directory that does not exist, and
asserts loop construction still succeeds (no raise) **and** a
`config_warning` event was logged naming the failure — proving the
degradation is visible, not merely non-fatal.

**Required Checks:**
- `.venv\Scripts\python.exe -m pytest -q tests/test_loop.py
  tests/test_loop_projects.py tests/test_worktree_loop.py
  tests/test_planner_worktree_cwd.py tests/test_worktree_refs.py
  tests/test_worktree_vcs.py tests/test_vcs_loop.py`
- `ruff format --check .`

**Checkpoint Type:** none (AFK).

**Exit Criteria:** All required checks green, including every pre-existing
worktree/vcs-loop test file passing unmodified (proves the single-project
fallback is byte-for-byte). `test_no_status_write_is_downstream_of_a_vcs_
result`'s AST guard (in `test_vcs_loop.py`) still passes unmodified — this
phase adds no new `vcs.*` call shape, only changes what feeds an existing
one.

**Test Seams:** integration (`Loop` + `MockRunner`, matching `test_loop.py`'s
existing seam exactly — no new seam).

**Interfaces:**
- Consumes: `Store.resolve_project`, `Store.get_project`,
  `Store.list_projects`, `Store.claim_next_task(worker_id, project_id=)`
  (Phase 1, verbatim); `Task.project_id` (Phase 1).
- Produces: `Loop._worktree_repo_root(task: Task | None = None) -> Path |
  None`; `Loop.run(max_tasks: int | None = None, project_id: int | None =
  None) -> int`; `Loop.plan(goal, acceptance_criteria, title="",
  risk_level=1, project_id: int | str | None = None) -> Task`.

## Phase 4 — CLI: `--project`, `agentloop project` subcommands

**Objective:** Give every project-aware CLI command a `--project` flag and
add the `agentloop project` management surface.

**Files/Surfaces:** `agentloop/cli.py`, `agentloop/store.py` (the new
`Store.events(task_id=None, project_id=None)` overload only — see the
`events` bullet below and the Interfaces block's Produces list; BLOCKING
finding from the fresh review's second pass: this phase's own body and its
Sequencing note already required this `store.py` change and its own
Produces list already named `Store.events(...)` as a Phase 4 output, but
this line previously named only `cli.py`, contradicting the phase's own
text and this plan's own rule — stated in every other phase's Out-of-Scope
Drift section — that Files/Surfaces is a hard boundary).

**Dependencies:** Phase 1, Phase 3.

**Allowed Scope:**
- `--project NAME_OR_ID` added to the `add`, `plan`, `run`, `status`,
  `events` subparsers. Resolution happens once, right after `_build()`
  succeeds, before `_dispatch`: `project_id = store.resolve_project
  (getattr(args, "project", None))` — a bad name/id raises `KeyError`,
  already caught by `main`'s existing `except (KeyError, ValueError)`
  handler and rendered as `error: unknown project '<name>'` (no new
  exception-handling code needed).
- `add`: `Task(..., project_id=project_id)`.
- `plan`: `loop.plan(args.goal, args.criteria, args.title, args.risk,
  project_id=project_id)`.
- `run`: `loop.run(max_tasks=args.max_tasks, project_id=project_id)`.
- `status` (no `task_id`): `store.list_tasks(project_id=project_id)`. When a
  bare `--project` is omitted, `project_id` resolves to the active default —
  the same list `store.list_tasks()` returns today on a single-project
  install (identical rows, since there is only one project).
- `events` (no `task_id`): `task_id` becomes optional
  (`e.add_argument("task_id", nargs="?", type=int)`); when absent, lists
  every event for `project_id` via a new `Store.events(task_id=None,
  project_id=None)` overload — see Phase 6 for the underlying store change
  (this phase only wires the CLI argument; Phase 6 adds the store-level
  filter it calls). *(Sequencing note: this creates a forward reference —
  resolved by moving the `Store.events(project_id=)` addition into this
  phase instead of Phase 6, since `events` is a CLI-visible command in this
  phase's scope. Phase 6 does not repeat it.)*
- New `project` subcommand:
  - `agentloop project add NAME --repo-root PATH [--workspace-mode
    scratch|worktree] [--default]` → `store.create_project(...)`; if
    `--default`, follow with `store.set_default_project(id)`.
  - `agentloop project rename OLD NEW` → `store.rename_project(...)`
    (resolve `OLD` via `store.resolve_project`).
  - `agentloop project repoint NAME --repo-root PATH [--workspace-mode ...]`
    → `store.repoint_project(...)`.
  - `agentloop project list [--archived]` → `store.list_projects
    (include_archived=args.archived)`, printed one line per project
    (id, name, default marker, repo_root, workspace_mode, archived marker).
  - `agentloop project archive NAME` → `store.archive_project(...)`.
  - `agentloop project use NAME` → `store.set_default_project(...)`
    (Recommended Default from the Agreement Snapshot).

**Out-of-Scope Drift:** No `agentloop memory *` changes here (Phase 6, since
memory's project scoping lands in Phase 2/5, and CLI memory commands should
land alongside the server memory-endpoint scoping for one coherent review).
No `agentloop workspace prune|rebless` changes (explicitly out of scope).

**Expected Artifacts:** Updated `agentloop/cli.py`; new
`tests/test_cli_projects.py` covering every new subcommand, `--project`
resolution success and failure (`error: unknown project 'X'`, exit 1), and a
differential proving `agentloop status`/`agentloop add` with no `--project`
on a single-project install produce identical output to before this slice.

**Required Checks:**
- `.venv\Scripts\python.exe -m pytest -q tests/test_cli.py
  tests/test_cli_projects.py`
- `ruff format --check .`

**Checkpoint Type:** none (AFK).

**Exit Criteria:** All required checks green, including `test_cli.py`
passing unmodified.

**Test Seams:** integration (`cli.main(argv)` against a real tmp-path db,
matching `test_cli.py`'s existing seam exactly).

**Interfaces:**
- Consumes: `Store.resolve_project`, `create_project`, `rename_project`,
  `repoint_project`, `list_projects`, `archive_project`,
  `set_default_project`, `default_project_id` (Phase 1, verbatim);
  `Loop.run(project_id=)`, `Loop.plan(..., project_id=)` (Phase 3, verbatim).
- Produces: `Store.events(self, task_id: int | None = None, project_id: int
  | None = None) -> list[dict]` (new overload on the existing `events`
  method — `task_id` and `project_id` are mutually exclusive filters;
  `task_id` given wins, matching the CLI's own "task_id present → per-task,
  absent → per-project" branching); CLI subcommands `project add|rename|
  repoint|list|archive|use`.

## Phase 5 — `memory.py` + `agents.py`: project-aware retrieval and prompts

**Objective:** Thread `project_id` through `MemoryService` and into the
worker/validator/planner prompt-building functions, closing the exact gap
CLAUDE.md's own roadmap note names as a real correctness risk (Project A's
facts reaching Project B's prompts).

**Files/Surfaces:** `agentloop/memory.py`, `agentloop/agents.py`.

**Dependencies:** Phase 2.

**Allowed Scope:**
- `MemoryService.facts_for_prompt(self, limit=_MAX_FACTS_IN_PROMPT,
  pinned_limit=_MAX_PINNED_FACTS, query="", task_id=None, project_id: int |
  None = None) -> tuple[str, dict | None]` — passes `project_id` into
  `self.store.memory_list(approved_only=True, project_id=project_id)`
  (Phase 2's "`None` = every project" meaning would be wrong here — this
  call site must pass the *resolved* project, so `facts_for_prompt` resolves
  `project_id = self.store.resolve_project(project_id)` itself before
  calling `memory_list`, exactly once, so a caller passing `None` still gets
  one project's facts, not every project's). **Its existing call site
  `self._record_reads(rows, scores, ranked, task_id)` (memory.py:109) also
  gains the resolved `project_id`, becoming `self._record_reads(rows,
  scores, ranked, task_id, project_id=project_id)`** — BLOCKING finding from
  the fresh review's second pass: `facts_for_prompt` is the **primary**
  path every worker/validator/planner prompt actually goes through
  (`agents._memory_block` calls it, not the narrower `read()` method below),
  and without this the resolved `project_id` computed one line above would
  be discarded at the one call site that matters most — `_record_reads`'s
  internal `store.memory_read(...)`/`maybe_promote(...)` calls (memory.py:
  244-247) would then default `project_id` to "the active project" rather
  than the task's own project, reproducing on the dominant path the exact
  cross-project promotion/hit-recording leak the `read()`-side fix below
  already closes on the narrower one.
- `MemoryService.read(self, tier, key, task_id=None, project_id: int | None
  = None) -> str | None` — `project_id` appended after `task_id`, matching
  `Store.memory_read`'s own parameter order from Phase 2 (an ordering fix
  from the fresh review's advisory finding: an earlier draft placed
  `project_id` before `task_id`, an inconsistent convention within the same
  plan and a latent trap for a future positional caller, even though every
  existing call site here passes `task_id=` as a keyword). Resolves and
  threads `project_id` into `store.memory_read`, **and into its own
  internal `self.maybe_promote(tier, key, project_id=project_id)` call**
  (advisory finding from the fresh review's first pass: this nested call is
  easy to miss since `read()`'s own signature change reads as "done"
  without it — `maybe_promote`'s default `project_id` resolves to "the
  active project," which is not necessarily the project `read()` was just
  asked about, so leaving this call unthreaded would let a project-scoped
  read promote a fact against the wrong project's memory).
- `MemoryService.remember(self, tier, key, value, approved=False,
  pinned=False, project_id: int | None = None) -> None` — threads
  `project_id` into `store.memory_write`.
- `MemoryService.maybe_promote(self, tier, key, project_id: int | None =
  None) -> bool` — resolves `project_id`, passes it to `self._find` and
  reads the located row's own `id` for `store.memory_promote(id)` (unchanged
  signature per Phase 2).
- `MemoryService._find(self, tier, key, project_id: int | None = None) ->
  dict | None` — filters `self.store.memory_list(tier=tier,
  project_id=project_id)`.
- `MemoryService._record_reads(self, rows, scores, ranked, task_id=None,
  project_id: int | None = None) -> None` — gains `project_id` and threads
  it into the `store.memory_read(...)`/`maybe_promote(...)` calls it makes
  per fact (memory.py:244-247), so both of `facts_for_prompt`'s and
  `read()`'s calls into this method carry the resolved project through to
  the hit-recording and promotion machinery underneath it.
- `agents._memory_block(memory, query="", task_id=None, project_id: int |
  None = None) -> tuple[str, dict | None]` — passes `project_id` through to
  `memory.facts_for_prompt`.
- `run_worker`, `run_validator`, `run_planner`: each already receives a
  `task`/`plan_task: Task` — update their `_memory_block(...)` call to add
  `project_id=task.project_id` (`plan_task.project_id` in `run_planner`). No
  new function parameters on `run_worker`/`run_validator`/`run_planner`
  themselves — the project comes off the `Task` object they already take.

**Out-of-Scope Drift:** No `Store` changes here (done in Phase 2). No CLI/
server memory-command changes (Phase 6).

**Expected Artifacts:** Updated `agentloop/memory.py`, `agentloop/agents.py`;
new `tests/test_memory_project_isolation.py` proving the real differential:
two projects, each with an approved fact under the *same* key, ranked
against the same query text — assert Project A's worker prompt never
contains Project B's fact value and vice versa (the design doc's own stated
correctness risk, made into an executable test, not just documentation); a
further case in the same file,
`test_read_promotes_against_the_read_project_not_the_active_one` — set
Project B as the store's active/default project, then call
`MemoryService.read(..., project_id=A)` on a project-A fact sitting at
`promote_threshold - 1` hits across enough distinct `task_id`s to cross the
threshold on this read, and assert the fact promotes inside **Project A**
(never Project B, and never raises), closing the first-pass advisory finding
from the fresh review. A third case in the same file,
`test_facts_for_prompt_promotes_against_the_task_project_not_the_active_one`
— set Project B as the store's active/default project, then call
`memory.facts_for_prompt(query=..., task_id=<a project-A task>,
project_id=A)` (or, at the `agents.run_worker` seam, drive a project-A task
through a worker whose prompt is built by `_memory_block`) on a project-A
fact sitting at `promote_threshold - 1` hits across enough distinct
`task_id`s to cross the threshold on this call, and assert the fact
promotes inside **Project A** — closing BLOCKING #2 of the fresh review's
second pass, the dominant-path counterpart to the `read()`-side test above
(this one exercises `facts_for_prompt`'s own `_record_reads` call, which
`agents._memory_block` — and therefore every worker/validator/planner
prompt — actually goes through).

**Required Checks:**
- `.venv\Scripts\python.exe -m pytest -q tests/test_memory.py
  tests/test_memory_promotion.py tests/test_pinned_memory.py
  tests/test_retrieval.py tests/test_memory_project_isolation.py
  tests/test_loop.py`
- `ruff format --check .`

**Checkpoint Type:** none (AFK).

**Exit Criteria:** All required checks green, including all pre-existing
memory test files passing unmodified (proves single-project prompts are
byte-for-byte unchanged). The cross-project isolation differential passes,
including `test_facts_for_prompt_promotes_against_the_task_project_not_the_
active_one` — the dominant-path promotion-isolation proof, not only the
narrower `read()`-side one.

**Test Seams:** integration (`MemoryService` + real `Store`, matching
`test_memory.py`'s existing seam; `agents.run_worker` + `MockRunner` +
`Store` for the prompt-content differential, matching `test_loop.py`'s
existing seam for prompt-content assertions).

**Interfaces:**
- Consumes: `Store.memory_write/read/list(project_id=)` (Phase 2, verbatim);
  `Store.resolve_project` (Phase 1, verbatim); `Task.project_id` (Phase 1,
  verbatim).
- Produces: `MemoryService.facts_for_prompt(..., project_id: int | None =
  None)`; `MemoryService.read(tier, key, task_id=None, project_id: int |
  None = None)`; `MemoryService.remember(tier, key, value, approved=False,
  pinned=False, project_id: int | None = None)`;
  `agents._memory_block(memory, query="", task_id=None, project_id: int |
  None = None)`.

## Phase 6 — CLI memory `--project` + Server: `/api/projects` + `?project=` scoping

**Objective:** Wire `agentloop memory *` to `--project`, add `/api/projects`
CRUD, and scope every listed REST/SSE endpoint by `?project=`.

**Files/Surfaces:** `agentloop/cli.py` (`_memory_cmd` + its subparser only),
`agentloop/server.py`, `agentloop/store.py` (`run_metrics`, `events_since`
only — small, mechanical additions consumed exclusively by this phase's
routes).

**Dependencies:** Phase 1, Phase 2, Phase 3, Phase 4, Phase 5.

**Allowed Scope:**
- `cli.py`: `memory` subparser gains `--project NAME_OR_ID` on `list|
  approve|reject|add|pin|unpin`; `_memory_cmd` resolves it once and threads
  it into `store.memory_list(project_id=...)` / `store.memory_write(...,
  project_id=...)`. `approve`/`reject`/`pin`/`unpin` operate on a `memory_id`
  that already identifies its project (no `--project` needed to *resolve*
  them, but the flag stays available for symmetry/discoverability with
  `list`/`add`, unused by those four).
- `store.py`: `Store.run_metrics(self, project_id: int | None = None) ->
  dict` — `None` unfiltered (today's exact query, unchanged); an int scopes
  every aggregate via a join: `attempts` joined to `tasks` on `task_id`
  filtered `tasks.project_id=?`; `tasks_by_status` filtered `WHERE
  project_id=?`; `pending_tool_requests` joined `tool_requests` to `tasks` on
  `task_id` filtered the same way; `by_model` follows the same join as the
  headline totals.
- `store.py`: `Store.events_since(self, event_id, limit=500, project_id: int
  | None = None) -> list[dict]` — `None` unfiltered (today's exact query,
  unchanged); an int does `SELECT e.* FROM events e LEFT JOIN tasks t ON
  t.id = e.task_id WHERE e.id > ? AND (e.task_id IS NULL OR
  t.project_id = ?) ORDER BY e.id LIMIT ?` — task-scoped events filtered by
  the join, `task_id IS NULL` events (memory/charter/config/project-mgmt)
  always pass through (Recommended Default, stated above). Cursor
  advancement is correct by construction: the `LIMIT` applies to the
  already-filtered result set, so the caller's `cursor = row["id"]` loop in
  `server._stream` needs no change.
- `server.py`:
  - New GET `/api/projects` → `{"projects": store.list_projects(include_
    archived=True)}` (dashboard needs archived projects to render them
    struck-through, not hide them).
  - New POST `/api/projects` → `store.create_project(...)` from the body
    (`name`, `repo_root`, `workspace_mode`), same validation-before-write
    shape as `_set_config_repo`.
  - New POST `/api/projects/{id}/rename` (body: `{"name": ...}`), `/repoint`
    (body: `{"repo_root": ..., "workspace_mode": ...}`), `/archive` (no
    body), `/use` (no body) — following the existing `/api/tasks/{id}/
    {verb}` routing shape in `do_POST` exactly.
  - `GET /api/tasks` → `?project=` parsed via the same `int(...)` /
    `_error(400, ...)` pattern `_tool_requests_list` already uses for
    `task_id`; when present, `store.list_tasks(project_id=parsed)`.
  - `GET /api/metrics` → `?project=` → `store.run_metrics(project_id=
    parsed)`.
  - `GET /api/memory` → `?project=` → `store.memory_list(project_id=
    parsed)`.
  - `GET /api/tool_requests` → `?project=` in addition to the existing
    `task_id`/`status` filters — `store.tool_requests` gains no new
    parameter; instead the handler cross-references
    `store.list_tasks(project_id=parsed)`'s ids when `project` is given with
    no explicit `task_id` (a small, self-contained filter in the handler,
    not a new store method, since `tool_requests` is already keyed by
    `task_id` and the set of a project's task ids is cheap to compute here).
  - `GET /api/stream` → `?project=` parsed the same way, threaded into
    `store.events_since(cursor, project_id=parsed)` inside `_stream`'s loop,
    and into the periodic `run_metrics()` push
    (`store.run_metrics(project_id=parsed)`).
  - `POST /api/tasks` → optional `project_id` in the body (defaults to
    `store.resolve_project(None)` when absent) — the dashboard's task-create
    form target project.
  - `_task_json` gains `"project_id": task.project_id`.
  - `GET /api/config` gains `"default_project_id": store.default_project_id()`
    (so the frontend knows which project to select on first load).

**Out-of-Scope Drift:** No `web/` changes (Phase 7). `GET /api/tasks/{id}`
(single-task detail) is not project-filtered — a task id already names its
own project; there is nothing to scope.

**Expected Artifacts:** Updated `agentloop/cli.py`, `agentloop/server.py`,
`agentloop/store.py`; new `tests/test_cli_projects.py` additions for
`memory --project`; new `tests/test_server_projects.py` covering `/api/
projects` CRUD and the real cross-project-leak differential (two projects,
two tasks — assert `GET /api/tasks?project=A` never contains project B's
task id, and the same for `/api/metrics`, `/api/memory`, `/api/stream`).

**Required Checks:**
- `.venv\Scripts\python.exe -m pytest -q tests/test_server.py
  tests/test_server_projects.py tests/test_cli_projects.py
  tests/test_cli.py`
- `ruff format --check .`

**Checkpoint Type:** none (AFK).

**Exit Criteria:** All required checks green, including `test_server.py`
passing unmodified (proves unscoped requests — no `?project=` — are
byte-for-byte today's behavior). The cross-project-leak differential passes
for every listed endpoint.

**Test Seams:** integration (real `DashboardServer` on an ephemeral port +
`urllib.request`, matching `test_server.py`'s existing seam exactly).

**Interfaces:**
- Consumes: everything produced by Phases 1-5 (verbatim): `Store.
  list_projects`, `create_project`, `rename_project`, `repoint_project`,
  `archive_project`, `set_default_project`, `resolve_project`,
  `default_project_id`, `list_tasks(project_id=)`, `memory_list
  (project_id=)`, `memory_write(..., project_id=)`, `Task.project_id`.
- Produces: `Store.run_metrics(project_id: int | None = None) -> dict`;
  `Store.events_since(event_id, limit=500, project_id: int | None = None) ->
  list[dict]`; REST routes `GET/POST /api/projects`, `POST /api/projects/
  {id}/rename|repoint|archive|use`; `?project=` query parameter on `GET
  /api/tasks`, `/api/metrics`, `/api/memory`, `/api/tool_requests`,
  `/api/stream`; `_task_json`'s `"project_id"` field; `GET /api/config`'s
  `"default_project_id"` field.

## Phase 7 — `web/`: project switcher UI

**Objective:** Add the `Project` type, project API client methods, and a
switcher component that drives `?project=` on every fetch and the SSE
subscription.

**Files/Surfaces:** `web/src/types.ts`, `web/src/api.ts`,
`web/src/useLiveLoop.ts`, `web/src/App.tsx`, new
`web/src/components/ProjectSwitcher.tsx`.

**Dependencies:** Phase 6 (exact endpoint/query-param shapes).

**Allowed Scope:**
- `types.ts`: new `export interface Project { id: number; name: string;
  repo_root: string; workspace_mode: string; is_default: boolean; archived:
  boolean; created_at: number; updated_at: number }`; `Task.project_id:
  number` added; `LoopConfigView.default_project_id: number` added.
- `api.ts`: `api.projects()`, `api.createProject(...)`,
  `api.renameProject(id, name)`, `api.repointProject(id, repo_root,
  workspace_mode)`, `api.archiveProject(id)`, `api.useProject(id)`; every
  existing `tasks()/metrics()/memory()/toolRequests()/events()` gains an
  optional `projectId?: number` parameter appending `?project=${projectId}`
  when present — same optional-trailing-param pattern `toolRequests(taskId?)`
  already uses.
- `useLiveLoop.ts`: accepts a `projectId: number | null` argument; every
  `refresh()` call passes it through; the `EventSource` is closed and
  reopened against `/api/stream?project=${projectId}` (or unscoped when
  `null`) whenever `projectId` changes — a `useEffect` dependency, not a
  manual re-subscribe call, matching the existing effect's own teardown
  pattern.
- New `ProjectSwitcher.tsx`: a dropdown in the header bar (matching
  `web/`'s existing component conventions — same file shape as
  `RepoConfigPanel.tsx`), listing `api.projects()`, persisting the selected
  id in `localStorage` (client-side "which project am I viewing" state —
  the actual filtering stays server-side per decision #5; this is only which
  id gets appended to the query string) and calling back up to `App.tsx` to
  update `projectId` state.
- `App.tsx`: holds `projectId` state (initialized from `localStorage`, or
  `null` until `/api/config`'s `default_project_id` resolves it), passes it
  into `useLiveLoop(projectId)` and `ProjectSwitcher`.

**Out-of-Scope Drift:** No changes to slice 7's office-view UI (unrelated,
separately blocked). No rendered-output verification in this phase — see
Exit Criteria.

**Expected Artifacts:** Updated `web/src/types.ts`, `web/src/api.ts`,
`web/src/useLiveLoop.ts`, `web/src/App.tsx`; new
`web/src/components/ProjectSwitcher.tsx`.

**Required Checks:**
- `cd web && npm run typecheck`
- `cd web && npm run build`

**Checkpoint Type:** human_verify — reason category `manual-verification`.
No agent in this project can verify rendered `web/` output (established
precedent, slices 7 and 9). Scoped narrowly: this phase's *automated*
acceptance is typecheck + build passing; the rendered switcher itself needs
a human eyeball pass with a seeded two-project demo db and a per-screen
checklist, delivered as a **separate** follow-up artifact
(`temp/slice10-ui-checklist.md` + a seeded demo db), not blocking this
phase's own completion.

**Exit Criteria:** `npm run typecheck` and `npm run build` both pass with
zero errors. `types.ts`'s `Project`/`Task.project_id`/`LoopConfigView.
default_project_id` match Phase 6's exact server JSON shapes field-for-field.

**Test Seams:** none (no test runner in `web/` per this project's
established constraint) — deterministic checks are typecheck + build only;
rendered-UI verification is manual, per the Validation Levels table (Live/
Manual), same as slices 7 and 9.

**Interfaces:**
- Consumes: `GET/POST /api/projects`, `POST /api/projects/{id}/rename|
  repoint|archive|use`, `?project=` query param shape, `_task_json`'s
  `"project_id"`, `GET /api/config`'s `"default_project_id"` (Phase 6,
  verbatim).
- Produces: `Project` TypeScript interface; `api.projects()` etc. (consumed
  by the human verification pass only, not by another build phase).

## Phase 8 — Documentation: CLAUDE.md + README

**Objective:** Record the slice in CLAUDE.md's roadmap (matching the file's
own established prose conventions for slices 1-9) and update README's CLI
surface / config knob tables.

**Files/Surfaces:** `CLAUDE.md`, `README.md`.

**Dependencies:** Phases 1-7 complete (documentation describes what was
actually built, not what was planned).

**Allowed Scope:**
- CLAUDE.md: mark roadmap item 10 done, in the same voice/rigor as items
  1-9 — must explicitly state the "no decision rule changed" claim (matching
  every prior slice's own closing paragraph) and name the one deliberate
  departure from "`_migrate()` only ever adds" (the `memory` table rebuild,
  Phase 2's ADR).
- CLAUDE.md's `store.py`/`config.py`/`loop.py`/`cli.py`/`server.py`/`web/`
  per-file summary paragraphs gain the new surfaces this slice added
  (`projects` table, `Store.resolve_project`, `Loop._worktree_repo_root`'s
  new `task` parameter, the CLI `project` subcommand, `?project=` on the
  listed REST routes) — following the existing per-file paragraph structure,
  not a new section.
- README: CLI command list gains `--project`, `agentloop project add|
  rename|repoint|list|archive|use`; the knob/config table is unaffected
  (`repo_root`/`workspace_mode` in `loopconfig.json` are documented as
  seeding the bootstrap Default project only — a note added at their
  existing table entries, not a new table).

**Out-of-Scope Drift:** No code changes in this phase.

**Expected Artifacts:** Updated `CLAUDE.md`, `README.md`.

**Required Checks:** none beyond a read-through for consistency with what
Phases 1-7 actually built (docs describe reality, not aspiration — CLAUDE.md's
own standing rule).

**Checkpoint Type:** none (AFK).

**Exit Criteria:** CLAUDE.md's roadmap item 10 reads as done; every new
public surface named in Phases 1-7's "Produces" sections is mentioned
somewhere in CLAUDE.md's per-file summaries.

**Test Seams:** none (documentation-only phase).

**Interfaces:**
- Consumes: every "Produces" list from Phases 1-7 (verbatim names, used as
  the checklist for what must be documented).
- Produces: none (terminal phase).

## Critical-Path Verification Strategy (Phase 1 & Phase 2 — the migration)

**Behavior contract:**
1. Opening a pre-slice-10 db never leaves a `tasks` or `memory` row with a
   NULL `project_id` after `Store.__init__` returns.
2. Every pre-existing row backfills to exactly one "Default" project; no
   second project is ever created by the backfill itself.
3. `memory`'s `id` values are stable across the Phase 2 rebuild — any
   `memory_hits` row or `retrieval` event payload recorded against a memory
   id before the rebuild still resolves to the same fact's row after it.
4. Re-opening an already-migrated db (running `Store.__init__` a second
   time against the same file) is a no-op on both migrations — no duplicate
   "Default" project, no `memory` table rebuilt twice, no duplicate
   backfill/rebuild events logged.
5. A fresh (never-before-existing) db produces byte-for-byte the same
   `_SCHEMA`-defined shape whether opened once or many times.

**Edge-case catalog:**
- A pre-existing db with `memory` rows but zero `tasks` rows (backfill has
  nothing to do on `tasks`, still must create the Default project and
  rebuild `memory`).
- A pre-existing db with `memory_hits` rows referencing memory ids that must
  survive the Phase 2 rebuild verbatim.
- A pre-existing db already containing a `projects` table from a prior,
  partially-applied migration attempt (idempotency — `_ensure_default_
  project` and `_rebuild_memory_unique_constraint` must both detect
  "already done" and no-op, not double-insert or double-rebuild).
- A brand-new db (no pre-existing tables at all) — the bootstrap and rebuild
  code paths must both still run correctly against `_SCHEMA`'s own
  already-correct fresh shape (the rebuild's "already migrated" detection
  must recognize `_SCHEMA`'s fresh `UNIQUE(project_id, tier, key)` and
  no-op, never attempt to rebuild a table that already has the target
  shape).
- **A pre-memory_hits legacy db** (`memory` table present, `memory_hits`
  table absent — the exact condition that trips `_migrate()`'s existing
  `if "memory" in existing and "memory_hits" not in existing:` branch at
  store.py:608-609) **routed through migration**, with at least one
  `project`/`loop` key collision so `_reconcile_memory_hits()` →
  `_merge_into_loop()` → `memory_write()` actually executes against the
  newly project-id-aware schema. This is Plan-vs-code gap #6 and the subject
  of BLOCKING #1 from the fresh review: without Phase 1/2's explicit
  reordering of `_migrate()`'s body (`_ensure_default_project()` and
  `_rebuild_memory_unique_constraint()` before this legacy branch, never
  after), this scenario raises `sqlite3.OperationalError: no such column:
  project_id` and aborts migration. Named test:
  `test_legacy_pre_memory_hits_db_migrates_without_project_id_error` (Phase
  2's Expected Artifacts).

**Provable properties (each backed by a named test in Phase 1/2's Required
Checks):**
- P1: `∀` task/memory row in a migrated db, `project_id IS NOT NULL`.
- P2: `∀` migrated db, `COUNT(*) FROM projects WHERE name='Default'` is
  exactly 1, even after `Store.__init__` runs twice against the same file.
- P3: `∀` `memory_hits` row present before the Phase 2 rebuild, the same row
  (same `memory_id`, same `task_id`) is present and resolves to a `memory`
  row with the same `value` after the rebuild; and the rebuilt `memory`
  table itself carries `AUTOINCREMENT`, so a row deleted after the rebuild
  (`memory_delete`, or a losing `_merge_into_loop` collision) never has its
  id reissued to a later, unrelated fact
  (`test_memory_rebuild_preserves_autoincrement`).
- P4 (cross-project isolation, Phase 5/6): `∀` two projects A, B each with
  an approved fact under the same `(tier, key)`, `facts_for_prompt
  (project_id=A)`'s block never contains B's fact value, and vice versa.
- P5: opening a pre-memory_hits legacy db (the edge case above) through
  `Store.__init__` never raises, and every row `_reconcile_memory_hits()`
  touches ends up with a non-NULL, correctly-resolved `project_id`.

**Purity boundary map:** `Store._ensure_default_project`,
`Store._rebuild_memory_unique_constraint`, and every new `Store.*` accessor
are impure by construction (they are the store's own write layer) but are
each wrapped in `Store.transaction()` exactly like every existing paired
row+event write in this file — no new impurity shape is introduced, so no
new class of partial-write failure is possible that the existing
`transaction()` discipline doesn't already close.

**Verification strategy:** RED-FIRST for both migrations — Phase 1's
migration test is written and watched failing against the pre-Phase-1 tree
(no `projects` table exists, so the backfill assertion cannot pass) before
`_migrate()` is touched, matching `test_migration.py`'s own stated
convention of hand-writing an old-shape schema fixture rather than deriving
one from today's `_SCHEMA` (a fixture that read today's schema would be
migrated by construction and could never fail). Phase 2's rebuild test is
watched failing the same way against a hand-written pre-slice-10 `memory`
table shape with real `memory_hits` rows. Both differentials are then
falsified once by neutering the mechanism (temporarily reverting
`_ensure_default_project`/`_rebuild_memory_unique_constraint` to a no-op)
and confirming the test goes red for the *right* reason (missing rows /
FK breakage), not for an unrelated setup error — same discipline this
project's own memory records ("A refusal is not a defence": assert the
payload never fired; a green test can be hollow, so neuter the mechanism
and check something goes red).

## Risk-Based Testing Matrix

| Risk | Probability | Impact | Test Required |
|---|---|---|---|
| Migration backfill leaves a task/memory row unscoped | low | high | Deterministic: Phase 1/2 migration differentials (P1, P2 above) |
| `memory` `UNIQUE` rebuild loses rows, breaks `memory_hits` FK linkage, or drops `AUTOINCREMENT` (letting a deleted row's id be reissued to an unrelated later fact) | low | high | Deterministic: Phase 2 rebuild differential (P3 above), id-preservation assertion, and `test_memory_rebuild_preserves_autoincrement` |
| `_migrate()`'s legacy `_reconcile_memory_hits()` branch runs before `memory.project_id` exists on a pre-memory_hits db, aborting migration with `OperationalError` | medium (real on any db old enough to predate `memory_hits`) | high (migration aborts entirely) | Deterministic: `test_legacy_pre_memory_hits_db_migrates_without_project_id_error` (P5 above), watched RED against the pre-reorder call order before the fix lands |
| `Loop.__init__`'s Default-project reconciliation silently downgrades an existing worktree-mode install to scratch mode when `repo_root` is relative | medium (relative `repo_root` is a documented, supported `LoopConfig` value) | high (silent, no error/warning/event — a real install's durability guarantees quietly disappear) | Deterministic: Phase 3's relative-repo_root reconciliation test (below) |
| Cross-project data leak (task list, metrics, memory injected into wrong project's prompt) | medium | high | Deterministic: Phase 5/6 cross-project-leak differentials (P4 above) on every listed endpoint |
| Single-project install behavior changes (byte-for-byte regression) | medium | high | Deterministic: every pre-existing test file in `test_loop.py`, `test_memory*.py`, `test_cli.py`, `test_server.py`, `test_worktree_*.py`, `test_vcs_loop.py` must pass **unmodified** at the end of every phase that could affect them |
| `claim_next_task`'s CAS breaks under a project filter (two workers claim the same row) | low | high | Deterministic: existing `test_store_atomicity.py` CAS tests re-run unmodified in Phase 1; a project-filtered CAS race test added to `test_projects.py` |
| `agentloop run` (no `--project`) stops spanning multiple projects in one pass | low | medium | Deterministic: Phase 3's `test_loop_projects.py` case (a) |
| SSE cursor gets stuck behind a burst of unrelated-project events | low | low | Manual/documented residual — noted in Phase 6, not blocking (cursor still advances correctly per-poll; only responsiveness, not correctness, is affected) |
| `web/` switcher renders incorrectly / is unusable | low | medium | Manual: Phase 7's human eyeball pass with a seeded two-project demo db (no agent can verify rendered UI) |
| A CLI command silently defaults to the wrong project | low | high | Deterministic: Phase 4's `--project` resolution + omission tests |

## Functionality Flow Mapping

```
Flow: operator registers a second project and runs both from one loop
1. agentloop project add "site-b" --repo-root C:\repos\site-b --workspace-mode worktree
   → Store.create_project → test: tests/test_cli_projects.py::test_project_add
2. agentloop add "Task X" --project site-b --goal ... --criteria ...
   → Task.project_id resolved and persisted → test: tests/test_cli_projects.py::test_add_with_project
3. agentloop run (no --project)
   → Loop.run(project_id=None) claims across both projects, each task
     resolving its own repo_root via _worktree_repo_root(task)
   → test: tests/test_loop_projects.py::test_run_spans_multiple_projects
4. agentloop status --project site-b
   → store.list_tasks(project_id=site_b_id) shows only site-b's tasks
   → test: tests/test_cli_projects.py::test_status_scoped_by_project
Error paths:
- agentloop add --project nonexistent → error: unknown project 'nonexistent', exit 1
  → test: tests/test_cli_projects.py::test_unknown_project_errors
- agentloop project archive site-b (with a pending task) → refused
  → test: tests/test_projects.py::test_archive_refuses_with_nonterminal_tasks
- agentloop project repoint site-b --repo-root /does/not/exist → refused, no partial write
  → test: tests/test_projects.py::test_repoint_refuses_invalid_repo_root

Flow: dashboard operator switches projects
1. Dashboard loads, GET /api/config → default_project_id selects the initial project
   → test: tests/test_server_projects.py::test_config_includes_default_project
2. Operator selects "site-b" in ProjectSwitcher
   → every subsequent fetch appends ?project=<site-b id>; SSE reconnects to
     /api/stream?project=<site-b id>
   → test: tests/test_server_projects.py::test_tasks_scoped_by_project,
     ::test_metrics_scoped_by_project, ::test_memory_scoped_by_project,
     ::test_stream_scoped_by_project (server-side only; the client wiring
     itself is verified by Phase 7's typecheck/build + human pass)
Error paths:
- GET /api/tasks?project=999999 (unknown id) → 404 with a clear body
  → test: tests/test_server_projects.py::test_unknown_project_404
```

## Self-Review: Cross-Phase Reference Drift

**Revision note (fresh-review pass 1):** this section's first draft asserted
`Store._ensure_default_project` was "produced verbatim in Phase 1" while
Phase 1's own `Produces` list never actually named it — a real drift the
first self-review pass missed. Fixed by adding it to Phase 1's `Produces`
list (see that phase) rather than by weakening this section's claim, and
this pass re-checked every other `Consumes`/`Produces` pair below against
the current (post-revision) phase text, not the pre-revision one.

**Revision note (fresh-review pass 2):** the second review pass found a
drift of a different shape than this section checks for — not a
`Consumes`/`Produces` name mismatch, but a phase's declared **Files/
Surfaces** line contradicting its own body: Phase 4's Files/Surfaces named
only `cli.py` while its body, Sequencing note, and its own `Produces` list
all required and named a `store.py` change (`Store.events(task_id=None,
project_id=None)`). Fixed by adding `agentloop/store.py` to Phase 4's
Files/Surfaces line (see that phase). This section's Phase 2/5 bullets below
were also re-checked against the pass-2 fixes to `facts_for_prompt` (now
also threading `project_id` into `_record_reads`) and `MemoryService.read`'s
reordered signature — both are internal-call-site and parameter-order fixes
within a phase's own `Produces` name, not a cross-phase name change, so
neither introduces new `Consumes`/`Produces` drift here.

Checked every later-phase `Consumes` against the exact `Produces` spelling
of the phase it names:
- Phase 2 consumes `Store.resolve_project`, `Store._ensure_default_project`
  — both produced verbatim in Phase 1 (the latter added to Phase 1's
  `Produces` list in the pass-1 revision).
- Phase 3 consumes `Store.claim_next_task(worker_id, project_id=)`,
  `Store.get_project`, `Store.list_projects` — all produced verbatim in
  Phase 1.
- Phase 4 consumes `Loop.run(project_id=)`, `Loop.plan(..., project_id=)` —
  produced verbatim in Phase 3; and `Store.create_project` etc. — produced
  verbatim in Phase 1. Phase 4's own `Produces` (`Store.events(task_id=None,
  project_id=None)`, CLI `project` subcommands) is now consistent with its
  Files/Surfaces line (pass-2 fix).
- Phase 5 consumes `Store.memory_write/read/list(project_id=)` — produced
  verbatim in Phase 2.
- Phase 6 consumes every Phase 1-5 `Produces` name used — all verified
  present and spelled identically (`resolve_project`, `list_tasks
  (project_id=)`, `memory_list(project_id=)`, `memory_write(...,
  project_id=)`, `Task.project_id`), including Phase 5's reordered
  `MemoryService.read(tier, key, task_id=None, project_id=None)` (Phase 6
  does not call `read()` directly, so the reorder has no downstream
  reference to fix).
- Phase 7 consumes Phase 6's exact route/field names
  (`GET/POST /api/projects`, `?project=`, `_task_json`'s `"project_id"`,
  `"default_project_id"`) — all produced verbatim in Phase 6.
- Phase 8 consumes the union of every prior phase's `Produces` list as its
  documentation checklist — no spelling requirement beyond "name it
  somewhere," so no drift is possible by construction.

No dangling references found. Self-review: no cross-phase reference drift.
