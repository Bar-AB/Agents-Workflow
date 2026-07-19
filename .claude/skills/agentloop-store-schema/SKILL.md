---
name: agentloop-store-schema
description: Use when changing agentloop's SQLite store (store.py) — adding or altering a table or column in _SCHEMA, adding accessor methods, or touching the events, memory, attempts, or verdicts tables, or any change affecting the append-only audit log, approved-gated memory, or Postgres-portable schema.
---

# Evolve the store safely

`agentloop/store.py` is the single source of truth — every agent, and later the
Phase 2 UI, read and write here. The schema (`_SCHEMA`) is applied with
`CREATE TABLE IF NOT EXISTS` on every `Store()` init, so it is create-only:
adding a table is free; changing an existing one needs a migration path.

## Invariants you must not break

1. **`events` is append-only.** It is the audit trail (spec §11). Only ever
   `INSERT` via `log_event`. Never `UPDATE` or `DELETE` an events row, and
   never add a code path that does. Debugging the loop and trusting memory both
   depend on this being complete and immutable.
2. **Every state change and agent I/O logs an event.** New store mutations
   should call `log_event(task_id, kind, payload)` so the audit trail stays
   whole. Look at `set_status`, `add_verdict`, `memory_write` for the pattern.
3. **Memory reads are gated on `approved`.** `memory_read(..., approved_only=True)`
   is the default; unapproved rows are invisible to agents (spec §7). Don't add
   a read path that bypasses the gate. Writes are auditable (they log an event).
4. **Keep it boring SQL.** The Postgres migration (spec §9) must stay a
   connection-string change, not a rewrite. No SQLite-only cleverness in schema
   or queries where a portable form exists. `json.dumps` into TEXT columns
   (as `events.payload`, `memory.value`) is the accepted pattern for structured
   blobs.
5. **`attempts` is the metrics source** for budget caps and the future viz
   layer. If you add a per-invocation measurement, it belongs on `attempts`
   (see `start_attempt`/`finish_attempt`), and `task_spend`/`task_metrics`
   should account for it.

## Checklist

1. **Edit `_SCHEMA`.** New table → just add the `CREATE TABLE IF NOT EXISTS`.
   New column on an existing table → `IF NOT EXISTS` won't add it to existing
   dbs; add an idempotent `ALTER TABLE … ADD COLUMN` guarded migration in
   `Store.__init__` after `executescript`, or document that dev dbs are
   disposable (they are gitignored: `*.db`).
2. **Add typed accessor methods**, not raw SQL sprinkled around the codebase.
   Keep `row_factory = sqlite3.Row` usage and the `_row_to_*` conversion style.
3. **`commit()` after writes** — the store commits eagerly per operation; match
   that so a crash leaves a consistent, resumable state (the loop restarts from
   the store).
4. **Log an event** for any new state-changing operation.
5. **Update the `store.py` module docstring** and the `store.py` line in
   CLAUDE.md's Architecture section if you add a table or change its role.
6. **Add an e2e test** if the change affects loop behavior or metrics (see
   `agentloop-loop-test`); `test_memory_tiers_and_audit` is the model for
   store-level assertions on gating + audit.

## Resumability note

`next_pending_task` deliberately picks up in-flight statuses
(`in_progress/validating/revising`) before untouched `pending` ones so a
crashed run resumes. If you add a status or a table that holds in-flight state,
make sure resume still works — all state needed to continue must live in the store.
