# Slice 10: multi-project dashboard

One running `agentloop serve` / one `agentloop.db`, several projects,
switchable from the UI and the CLI. Design:
`docs/plans/2026-09-08-multi-project-dashboard-design.md`. Plan:
`docs/plans/2026-09-08-multi-project-dashboard-plan.md` (8 phases). Built
phase-by-phase with a fresh code-reviewer + failure-hunter pair per phase and
a regression test for every finding, including a TOCTOU race in
`Store.archive_project` reproduced under real `ThreadingHTTPServer`
concurrency, and two React stale-response races in the project-switcher UI.

**No decision rule changed** — same register as slices 4-9's own inertness
claims. `project_id=None` is byte-for-byte the pre-slice-10 behavior
everywhere it's read as a *filter*: `list_tasks`, `claim_next_task`,
`events`, `events_since`, `run_metrics` and `memory_list` all treat an absent
project as "every project," which is what the single-repository loop already
did. The one place `None` resolves to something concrete is a *write*:
`Store.resolve_project(None)` lands an omitted `--project` on the default
project, matching what every pre-slice-10 call site already did implicitly.

## What was added

- `projects` table: `id`, `name` (unique), `repo_root`, `workspace_mode`,
  `is_default`, `archived`, timestamps. `_ensure_default_project()` runs at
  `Store.__init__`: an existing default wins; an empty table bootstraps one
  race-safely (`INSERT OR IGNORE`); a table with rows but no default promotes
  the lowest-id row rather than minting a second bootstrap project.
- `Store.resolve_project(project: int | str | None) -> int` — the one seam
  every later caller (CLI, server, memory, loop) goes through. `None` = the
  default project (never raises); an `int` or `str` must resolve or raises.
- `archive_project` refuses the default project or any project holding a
  non-terminal task, check-and-write inside one `transaction()` (closing the
  TOCTOU gap above).
- `add_dependency` refuses an edge across two different projects' tasks.
- The `memory` table's uniqueness moved from `UNIQUE(tier, key)` to
  `UNIQUE(project_id, tier, key)` — the **one** deliberate exception to
  "`_migrate()` only ever adds a column," since SQLite can't `ALTER` a
  `UNIQUE` constraint in place. `_rebuild_memory_unique_constraint` recreates
  the table id-preserving and `AUTOINCREMENT`-preserving (an original version
  of this rebuild missed the autoincrement half — a reviewer-found critical).
- Server: `GET/POST /api/projects`, `POST /api/projects/{id}/{rename|repoint|archive|use}`,
  `?project=` scoping on tasks/metrics/memory/tool_requests/stream.
  `_parse_project_query`: absent = unfiltered, malformed = 400, well-formed
  but unknown = 404 (not an empty 200, which used to be indistinguishable
  from "this real project has nothing yet").
- CLI: `--project` on `add`/`plan`/`run`/`status`/`events`/`memory`, plus
  `agentloop project add|rename|repoint|list|archive|use`. `run` deliberately
  does *not* resolve an omitted `--project` (it spans every project); every
  other command resolves an omission to the default.
- `web/ProjectSwitcher.tsx`: a header dropdown driving `?project=` on every
  fetch and the SSE subscription, never a client-side filter.
  `useLiveLoop(projectId)` guards every fetch with a "latest ref" check
  before `setState`, closing a real race where a slower response from an
  abandoned project could overwrite the new one's state.

Phase 7's UI (`ProjectSwitcher`, the demo seed) was reviewed by the operator
against a seeded three-project demo (`temp/seed_slice10_demo.py` +
`temp/slice10-ui-checklist.md`) and confirmed correct by a human — see
`CLAUDE.md`'s note that no agent here can verify rendered `web/` output.
