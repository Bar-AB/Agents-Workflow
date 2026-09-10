# Multi-project dashboard — design

Slice 10 of `CLAUDE.md`'s roadmap: one running `agentloop serve` / one
`agentloop.db`, several projects, switchable from the UI (and the CLI).

## Purpose

Today `repo_root` is a single `LoopConfig` field read once at `Loop`
construction. A `Task` row carries no project identity, so `run_metrics()`,
the task list, and every dashboard number are unscoped across whatever repos
a given db has ever pointed at — and `memory` is equally unscoped, so an
unscoped switcher would rank/inject Project A's approved facts into Project
B's worker/validator prompts. This slice lets one agentloop instance manage
several independent projects (repos) at once, each with its own tasks,
memory, and metrics, switchable without restarting the server or editing a
config file.

## Users

The operator running agentloop against more than one of their own repos —
today that means either running a separate `agentloop serve` per repo (extra
processes, extra ports) or repointing `loopconfig.json`'s single `repo_root`
back and forth. This slice replaces both with one dashboard, several
registered projects.

## Success Criteria

- A `projects` table (id, name UNIQUE, repo_root, workspace_mode,
  created_at, updated_at). A project can be renamed or re-pointed to a new
  `repo_root` without losing its task/memory history — the row's `id` is the
  stable identity, not the path.
- Existing databases auto-migrate on first load: one "Default" project is
  created from today's `loopconfig.json` `repo_root`/`workspace_mode`, and
  every pre-existing `tasks`/`memory` row with no `project_id` is backfilled
  to it. No row is ever left unscoped.
- `tasks.project_id` and `memory.project_id` columns (FK to `projects.id`).
  `memory`'s uniqueness becomes `UNIQUE(project_id, tier, key)` — the same
  key text in two different projects is two different facts.
- CLI: `agentloop add|run|status|events|...` accept `--project <name>`.
  Omitting it uses the current "active" default (same project migration
  created, or whichever was last selected) — a single-project install
  behaves exactly as it does today.
- New `agentloop project add|rename|repoint|list|archive` CLI surface,
  mirroring `agentloop charter`'s human-only write pattern.
- Dashboard REST API (`/api/tasks`, `/api/metrics`, memory endpoints, the SSE
  stream) accept `?project=<id>` and scope every query **server-side** —
  never load-everything-then-filter in the browser.
- New `/api/projects` CRUD (list/create/rename/re-point/archive).
- `Loop`/`LoopConfig` resolve `repo_root`/`workspace_mode` per-task from the
  task's `project_id` row instead of solely from `loopconfig.json`.
- `Store.vcs_repo_pin`/`vcs_repo_pins` (already keyed per resolved
  `repo_root` path since slice 9 P2) need no change — a rename/re-point just
  changes which pin row a project's tasks read.
- **No decision rule in `loop.py` changes** — same register as slices 4-9's
  own inertness claims. This slice only threads a project scope through
  storage/API/CLI.

## Constraints

- stdlib-only core stays true; no new runtime dependency.
- Every existing SQLite invariant holds: `Store.transaction()` still pairs
  row+event writes; `events` stays append-only; `claim_next_task`'s CAS is
  unaffected — `project_id` changes which rows a query returns, never
  claimability.
- A fresh single-project install must behave byte-for-byte as it does today
  (proven as a differential, not just documented — same discipline as
  `vcs_enabled=False` and scratch-mode's own no-op proofs).

## Out of Scope

- **Per-project `agents.json`/`loopconfig.json` overrides.** Config
  (agent prompts, budget caps, thresholds) stays global/shared across every
  project in this slice — a deliberate, documented follow-up if ever needed,
  not a gap discovered later.
- Any change to decision rules, thresholds, or budget-cap semantics.
- Cross-project task dependencies — a task's `depends_on` graph stays
  entirely inside its own project.
- New parallel-execution scheduling across projects — `max_parallel_workers`
  behavior is unchanged; this slice does not add "run N projects at once."
- Slice 7's office-view UI (unrelated, separately blocked on a human
  eyeball pass).

## Approach Chosen

Add a `projects` table plus `project_id` foreign keys on `tasks` and
`memory`, threaded through `store.py`/`cli.py`/`server.py`/`web/`, with a
one-time migration that backfills a "Default" project from today's config.

**Rejected: `repo_root` string as the project's own identity** (no separate
`projects` table). A rename or a repo move (new clone path, drive-letter
change) would silently start a *different* "project" with no history — the
exact trap `vcs_repo_pins` already avoids at the security layer by keying on
a resolved path rather than by raw identity, and one this design shouldn't
reintroduce one layer up.

## Domain Glossary

- **Project** — a named, `repo_root`-backed unit of work registered in
  agentloop's own database. Distinct from the git repository it points at: a
  project's row (its id, name, task/memory history) persists across a
  `repo_root` re-point.
- **Active project** — the project a given CLI invocation or dashboard view
  is scoped to. CLI defaults to it when `--project` is omitted, mirroring
  today's single-`repo_root` behavior exactly.

## Decisions (ADR notes)

- **Project identity is a first-class row (name → repo_root), not the
  `repo_root` string itself.** Settled in interview. Rejected: repo_root-as-id.
  Why: supports rename and re-point without orphaning history.
- **Config stays global/shared across all projects.** Settled in interview.
  Rejected: per-project config. Why: smaller slice — only `repo_root`/
  `workspace_mode` meaningfully differ per project today; per-project
  overrides can be layered on later without reshaping this design.
- **CLI gets `--project` in this same slice, not deferred to a later one.**
  Settled in interview (reversed an initial "dashboard-only" draft). Why:
  without it, using the CLI against more than one project means editing
  `loopconfig.json` between every command — not workable for real terminal
  use.
- **Dashboard project switching is a real server-side scoped query on every
  affected endpoint**, never a client-side filter over an all-projects
  payload. Settled in interview. Why: matches CLAUDE.md's own standing
  concern that memory reads must not cross project boundaries (token cost +
  real correctness risk, not a cosmetic one), and keeps the dashboard's data
  footprint independent of how many projects are registered. Cost of this
  choice: a brief loading state on switch instead of an instant re-render —
  negligible against a local SQLite read.
- **Pre-existing rows are migrated forward, never left unscoped.** Settled in
  interview. Rejected: leave `project_id` NULL on old rows. Why: an unscoped
  row would vanish from every project-scoped view the moment scoping ships.
- **Project deletion is an archive, never a hard delete.** Matches this
  project's existing philosophy (append-only events, memory tier moves,
  charter versions never truly erased) — an archived project is hidden from
  active pickers; its tasks/memory rows and history are preserved and still
  reachable.

## Architecture

- **`store.py`** — new `projects` table; `tasks.project_id` and
  `memory.project_id` columns added via `_migrate()` (which only ever adds);
  `memory`'s `UNIQUE(tier, key)` becomes `UNIQUE(project_id, tier, key)`. A
  one-time backfill in `_migrate()`/`Store.__init__` creates "Default" from
  the current `LoopConfig` on first load against a pre-slice-10 db and sets
  `project_id` on every row still NULL.
- **`config.py`/`loop.py`** — `Loop`/`run_task` resolve `repo_root`/
  `workspace_mode` from the task's `project_id` row; falls back to the
  config value when nothing new is registered, preserving today's behavior
  byte-for-byte on a single-project install.
- **`cli.py`** — every task-creating/querying subcommand takes
  `--project <name>`; new `agentloop project add|rename|repoint|list|archive`
  surface.
- **`server.py`** — `/api/projects` CRUD; `/api/tasks`, `/api/metrics`,
  memory endpoints, and the SSE stream all take `?project=`, filtering the
  underlying query — never post-filtering an unscoped result set.
- **`web/`** — a project switcher drives `?project=` on every fetch and the
  SSE subscription; `types.ts` gains the `Project` shape.
- **`memory.py`** — `facts_for_prompt`/`memory_read`/promotion all resolve a
  `project_id` and scope every query and `UNIQUE` check by it — the exact
  gap CLAUDE.md's own roadmap note already named as a correctness risk.

## Data Flow

1. Operator registers a project (`/api/projects` POST, or
   `agentloop project add`) → a `projects` row.
2. `agentloop add "..." --project X` (or the dashboard's task-create form,
   scoped to the active project) → new `tasks` row carries `project_id`.
3. `Loop.run_task` reads the task's `project_id`, resolves that project's
   `repo_root`/`workspace_mode`, and proceeds exactly as today (vcs pin
   lookups unchanged, keyed on the resolved path).
4. Every dashboard/CLI read carries `?project=`/`--project`; the store layer
   never returns cross-project rows to a scoped query.
5. Worker/validator prompts inject memory facts read through the task's
   `project_id`, so Project A's approved facts never reach Project B.

## Error Handling

- Unknown `--project`/`?project=` → CLI: `error: unknown project '<name>'`
  (mirrors `charter_set`'s ValueError-to-`error:` rendering); API: 404 with a
  clear body, never a silent empty list.
- `--project` omitted → falls back to the active default project, never
  silently mixes rows from more than one.
- Re-pointing a project's `repo_root` to a path that doesn't exist or isn't a
  directory → refused up front, no partial write (mirrors the existing
  `POST /api/config/repo` validation).
- Archiving a project with in-flight (non-terminal) tasks → refused, or
  requires an explicit confirm; never silently strands a running task's
  worker without a resolvable project.

## Testing Strategy

- Migration backfill: an old-shape db → one Default project, every prior row
  scoped to it.
- `memory`'s new `UNIQUE(project_id, tier, key)`: same key text, two
  projects, both approved independently without colliding.
- A scoped query never returns another project's rows — enforced as a real
  differential (two projects, two tasks, assert the response never contains
  the other project's task id), the same discipline as slice 9's
  `workspace_mode` no-op proof.
- A single-default-project run is behaviorally identical to today's
  pre-slice-10 loop (status transitions, revision counting, event sequence)
  — proven, not just documented.
- `--project` plumbed and defaulted correctly in the CLI; unknown project
  name refused loudly.
- No decision-rule test changes expected in `test_loop.py` beyond the
  scoping itself.

## Observability

Project create/rename/re-point/archive gets its own audit event, mirroring
`charter_set`/`charter_cleared`'s existing pattern for config-shaping writes.
Task-level events already carry `task_id`; joining to `project_id` via
`tasks` needs no new event kind there.

## UI Mockup

Out of scope for this design doc (not a build); the dashboard switcher is a
dropdown in the existing header bar, following `web/`'s existing component
conventions. Left to the planner/builder phase.

## Questions Resolved

1. Project identity: a friendly name mapped to a `repo_root` (renamable /
   re-pointable), not the raw path.
2. Pre-existing data: backfilled into one "Default" project on migration,
   never left unscoped.
3. Config scope: stays global/shared across all projects in this slice.
4. CLI scope: `--project` flag included in this slice, not deferred.
5. Switch mechanism: real server-side scoped queries, not client-side
   filtering over an all-projects payload.
