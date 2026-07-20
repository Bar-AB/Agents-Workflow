# Phase 2: live dashboard, real test execution, wired memory

Date: 2026-07-20
Status: approved

## Context

Phase 1 shipped the vertical slice: sequential loop, SQLite source of truth,
independent validator, bounded revisions, budget caps, audit log, two-tier
memory. An audit against the seed spec found several items declared but not
wired. This design closes the ones the user selected and adds the Phase 2
visualization.

### Phase 1 audit result

Verified working (12/12 tests green): single source of truth, task lifecycle,
independent validator context, thresholds (0.70 approve / 0.40 severe / max 3
revisions), per-attempt metrics, append-only audit log, approved-gated memory,
budget caps, resumability, provider seam.

Declared but not wired:

1. Registry `tools` never reach `ClaudeAgentOptions` — agents have no tools.
2. Test results are validator self-reports; nothing executes.
3. Memory tables are never read or written by the loop; no promotion.
4. `context_budget_tokens` is dead data; no confidence handoff.
5. `config.py` docstring claims per-task `Task.overrides`; no such field.
6. Absent: tool-request policy, batch evaluation, second-provider validator,
   planner, git rollback, infra retry/backoff, sandboxing, coverage.

Two defects that block or corrupt Phase 2:

- `Store.__init__` uses default `sqlite3.connect`, bound to one thread. A web
  server touching it from a request thread raises `ProgrammingError`.
- `memory_write`'s `ON CONFLICT ... approved=excluded.approved` silently
  un-approves an approved fact when rewritten with the default `approved=False`.

## Decisions

| Question | Decision |
|---|---|
| Viz scope | Live functional dashboard; no character sprites this pass |
| Phase 1 gaps | Fix blockers/bugs + wire real tool use + wire memory |
| Frontend | Vite + React + TypeScript, Python serves the built bundle |
| Transport | REST for reads/actions, SSE for live push |
| Sandbox | Allowlisted command, per-task workspace, timeout, no `shell=True` |
| Memory | SQLite keyword + `hit_count` auto-promotion; RAG deferred |
| Test authority | Executed results override the validator's self-report |
| Workspace | Per-task `.agentloop/ws/task-{id}/`, wiped on redo |

## Architecture

### New modules

- **`agentloop/executor.py`** — `TestExecutor.run(workspace) -> TestResult`.
  Splits the configured command with `shlex.split` (never `shell=True`), runs
  it as a subprocess with `cwd` pinned to the task workspace, a timeout, and a
  captured-output cap. Returns `TestResult(status, exit_code, summary,
  stdout_tail, duration_s)` where status is `pass | fail | na | error`. `na`
  when the workspace is absent or execution is disabled.

- **`agentloop/memory.py`** — `MemoryService`. Reads approved facts for a tier
  into prompt context, writes candidate facts as unapproved, and promotes
  `project` facts to `loop` once `hit_count >= memory_promote_threshold`.
  Wraps the store's accessors so a vector store can slot in behind the same
  interface later.

- **`agentloop/server.py`** — stdlib `http.server` REST + SSE. No runtime deps.

- **`web/`** — Vite + React + TypeScript dashboard.

### Modified

- **`store.py`** — `check_same_thread=False` plus a `threading.Lock` around
  writes; fix the memory un-approve conflict clause; add a `test_runs` table;
  add `events_since(id)` for the SSE cursor.
- **`config.py`** — add `test_command`, `workspace_root`, `test_timeout_s`,
  `allow_test_exec`, `memory_promote_threshold`, `server_host`, `server_port`;
  remove the dead `Task.overrides` claim.
- **`runner.py`** — pass registry `tools` through as `allowed_tools`.
- **`loop.py`** — execute tests after the worker, feed real results to the
  validator, apply the real-result gate, wire memory read/write.
- **`agents.py`** — inject approved facts and real test results into prompts.
- **`cli.py`** — add `serve` and `memory` subcommands.

### The events table is the change feed

`events` is already append-only with a monotonic `id`. SSE therefore needs no
new pub/sub: the endpoint selects `WHERE id > ?` on a short interval and emits
frames carrying the row id; the client tracks the last id and reconnects with
`Last-Event-ID`. A reconnecting browser replays exactly what it missed. The
dashboard reads the same source of truth the loop writes — satisfying "no
divergent copies" rather than mirroring state into a second store.

### API

```
GET  /api/tasks                 list + status + rollup metrics
GET  /api/tasks/{id}            detail, metrics, verdicts, attempts, test runs
GET  /api/events?since={id}     audit log page
GET  /api/agents                registry: role, model, tools, budget, version
GET  /api/memory                facts, both tiers, pending + approved
GET  /api/metrics               run-level rollup: tokens, cost, wall, verdicts
GET  /api/stream                text/event-stream, resumable via Last-Event-ID
POST /api/tasks                 define a task
POST /api/tasks/{id}/approve    human sign-off
POST /api/tasks/{id}/reject
POST /api/tasks/{id}/redo
POST /api/memory/{id}/approve   gate a candidate fact
```

Writes are POST-only and the server binds to localhost by default.

### Test authority

Executed results become authoritative. The real result is injected into the
validator prompt, and the loop's approval gate consults the executed status,
not the validator's `TESTS:` claim. A validator claiming `pass` against an
executed `fail` produces a `test_disagreement` event — a direct measure of
validator reliability. A validator can no longer approve past failing tests.
This changes a documented decision rule, so CLAUDE.md and README must be
updated alongside.

## Testing

All tests run without API keys or network.

- `test_executor.py` — real subprocess pass/fail, timeout, missing workspace,
  output cap, and that command strings are never shell-interpreted.
- `test_memory.py` — approved gating, promotion at threshold, and that
  rewriting an approved fact preserves approval.
- `test_server.py` — REST endpoints against a live server on an ephemeral
  port, SSE frame format, and cursor-based resume.
- `test_loop.py` — new cases for the real-test gate and `test_disagreement`.
- Frontend — `tsc --noEmit` plus a production build.

## Out of scope

Character/office animation, vector RAG, planner and task graph, parallel
workers, second-provider cross-validator, git-commit-per-task rollback,
context-budget handoff, Docker isolation. Each remains on the roadmap.

## Considerations

- `claude_agent_sdk` is not installed and no credentials are present, so the
  `allowed_tools` pass-through is verified by asserting on constructed options,
  not by a live model call. Stated explicitly in the delivery report.
- The SSE poll interval trades latency against idle DB reads; 500ms default,
  configurable.
- Per-task workspaces are gitignored and wiped on redo, which is what makes
  redo a genuine fresh start rather than a rerun over dirty state.
