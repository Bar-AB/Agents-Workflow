# Phase 2: live dashboard, real test execution, wired memory

Phase 1 shipped the vertical slice (sequential loop, SQLite source of truth,
independent validator, bounded revisions, budget caps, audit log, two-tier
memory) — but an audit against the original spec found several pieces
*declared* and never actually wired: the registry's `tools` list never
reached the model (agents had no tools at all), test results were the
validator's unverified self-report (nothing executed), the memory tables
were never read or written by the loop, and `context_budget_tokens` was
dead data with no handoff behavior behind it.

That audit also found **two real defects that would have blocked or
corrupted Phase 2 specifically**, worth remembering because they explain
two choices that otherwise look arbitrary in `store.py`:

- `Store.__init__` used the default `sqlite3.connect`, bound to a single
  thread. A web server touching that connection from a request thread would
  raise `ProgrammingError` the moment the dashboard shipped — this is why
  `Store` opens with `check_same_thread=False` plus its own lock, not
  because concurrency was a nice-to-have added later.
- `memory_write`'s original `ON CONFLICT` clause set
  `approved=excluded.approved` unconditionally, which **silently
  un-approved an already-approved fact** the instant it was rewritten with
  the default `approved=False` — a real, load-bearing bug the "approval is
  approval of a value" rule was written specifically to fix.

## Decisions made at design time

| Question | Decision |
|---|---|
| Visualization scope | A live *functional* dashboard first — no office/character visualization in this pass (that became the still-unmerged Slice 7) |
| Frontend | Vite + React + TypeScript; the Python server serves the built static bundle, not a dev server |
| Transport | REST for reads/actions, Server-Sent Events for the live push — chosen specifically so no new pub/sub system is needed: `events` is already an append-only table with a monotonic id, so SSE is just `WHERE id > cursor` on a short poll interval, and a reconnecting browser resumes losslessly via `Last-Event-ID` |
| Sandbox | Allowlisted command, per-task workspace, timeout, output cap, never `shell=True` — the shape `executor.py` still has today |
| Memory | Plain SQLite + `hit_count` auto-promotion for this phase; a real relevance-ranked retrieval backend was explicitly deferred (it shipped later, as Slice 2) |
| Test authority | The *executed* result overrides the validator's own self-reported claim — this is the origin of the "tests not failing = the executed result" decision rule that still holds today |
| Workspace | One directory per task, wiped clean on redo — what makes a redo a genuine fresh start rather than a rerun over leftover files |

## Why SSE and not a second store

The append-only `events` table already had everything a live feed needs: a
monotonic id and nothing ever mutates or disappears from it. Building a
dashboard that mirrored loop state into a second, purpose-built store would
have created exactly the kind of divergent copy the project's "one SQLite
source of truth" rule exists to prevent — so the dashboard was built to read
the *same* table the loop already writes, never a projection of it.

## What was explicitly out of scope

Office/character-style visualization, vector-backed memory retrieval, the
planner and task graph, parallel workers, a second-provider cross-validator,
per-task git rollback, context-budget handoff, and real container/Docker
isolation — every one of those became its own later roadmap slice.
