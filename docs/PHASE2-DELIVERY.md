# Phase 2 delivery report

Date: 2026-07-20
Branch: `phase2-live-dashboard`

Verification at time of writing:

| Check | Command | Result |
|---|---|---|
| Python tests | `pytest -q` | **61 passed** (12 pre-existing, 49 new) |
| Frontend types | `npm run typecheck` | clean (`strict`, `noUnusedLocals`) |
| Frontend build | `npm run build` | built, 156 kB JS / 50 kB gzipped |
| Manual end-to-end | CLI + browser | verified, see §4 |

No API keys, credentials, or network access are needed for any of it.

---

## 1. Phase 1 audit

### Verified working as documented

Single source of truth (SQLite); task lifecycle define → execute → validate →
approve/revise/escalate; independent validator in a separate context;
thresholds 0.70 approve / 0.40 severe / max 3 revisions; per-attempt metrics
(tokens, cost, wall time); append-only audit log; two-tier approved-gated
memory; hard budget caps; resumability from the store; the `ModelRunner`
provider seam.

The README's Phase 1 claims were accurate. The 12 original tests passed before
any change and still pass unmodified in behavior.

### Declared but not wired (found by audit)

| # | Finding | Status |
|---|---|---|
| 1 | Registry `tools` never reached `ClaudeAgentOptions` — agents had no tools | **fixed** |
| 2 | Test results were validator self-reports; nothing executed | **fixed** |
| 3 | Memory tables never read or written by the loop; no promotion | **fixed** |
| 4 | `context_budget_tokens` stored but never enforced; no confidence handoff | open (roadmap) |
| 5 | `config.py` documented a `Task.overrides` field that does not exist | **fixed** (removed) |
| 6 | No tool-request policy, batch evaluation, second-provider validator, planner, git rollback, infra retry/backoff, coverage | open (roadmap) |

### Defects found and fixed

- **`Store` was single-thread-bound.** Default `sqlite3.connect` refuses
  cross-thread use, so any web server reading it would raise. Would have
  blocked Phase 2 entirely.
- **`memory_write` silently un-approved vetted facts.** `ON CONFLICT ... SET
  approved=excluded.approved` copied the incoming default `False` over an
  existing `True`, so any agent rewrite made an approved fact unreadable.
- **`shlex.split` corrupted Windows paths.** POSIX mode treats `\` as an
  escape, turning `C:\venv\Scripts\python.exe` into
  `C:venvScriptspython.exe`. Any absolute-path test command would have failed
  as "command not found". Caught by a test on this machine.
- **Cost was misattributed in the per-model rollup.** `attempts.model` stored
  the *requested* model while `cost_usd` was computed from the model that
  actually served, so spend was attributed to a model that never ran.
- **A BOM in `loopconfig.json` crashed the CLI.** Notepad and PowerShell's
  `Out-File` both write one; the tool died with a raw `JSONDecodeError`. Now
  read as `utf-8-sig`, with unknown keys warned about rather than silently
  ignored.

---

## 2. What was implemented

### Real, sandboxed test execution — `agentloop/executor.py`

Tests genuinely run. The command is allowlisted in `LoopConfig`, never taken
from model output; it is split to argv and run with `shell=False`, cwd pinned
to `.agentloop/ws/task-{id}/`, with a timeout and a 4 kB output cap. Results
are stored in a new `test_runs` table and injected into the validator prompt.

**Behavior change (approved before implementing):** executed results are
authoritative. The approval gate consults the real status, not the validator's
`TESTS:` claim, so a validator can no longer approve past failing tests. A
mismatch is recorded as a `test_disagreement` event, making validator
reliability measurable. `na` (no workspace, or execution disabled) falls back
to the validator's claim — absence of tests is not failure.

### Two-tier memory, wired — `agentloop/memory.py`

Approved facts are injected into worker and validator prompts. Agent writes
land unapproved and surface for human gating in the dashboard or via
`agentloop memory`. A project fact read 3 times is promoted to the `loop`
tier, and the promotion is logged. A vector store can replace retrieval behind
`facts_for_prompt` without the loop noticing.

### Tool pass-through — `agentloop/runner.py`

Registry tools now reach the SDK. The registry stays vendor-neutral (`file_io`,
`git`, `search`); `LOGICAL_TOOL_MAP` translates to concrete names (`Read`,
`Bash`, `Grep`) at the provider seam. Unknown logical names map to nothing
rather than passing through blind — an agent silently gaining an unintended
tool is worse than one missing a tool it asked for.

### Dashboard backend — `agentloop/server.py`

REST + SSE on stdlib `http.server`, preserving the zero-runtime-deps rule.
Endpoints: tasks (list/detail/create), human decisions, agents, memory (+
gating), metrics rollup, config, events, and `/api/stream`.

**The append-only `events` table is the change feed.** Because it already has
monotonic ids, SSE is a `WHERE id > cursor` query: a reconnecting browser
resumes from `Last-Event-ID` and replays exactly what it missed. The dashboard
reads the same rows the loop writes rather than mirroring state into a second
store — the spec's "no divergent copies" holds by construction, not by
convention.

### Dashboard frontend — `web/`

Vite + React + TypeScript. Task board along the lifecycle, agent panel with
model/tools/context budget and derived busy state, cost/token/time/progress
tiles, verdict history, executed test runs, live colour-coded audit feed,
memory gating, task definition form, and approve/reject/redo. Theme-aware
(light and dark), responsive, no page-level horizontal scroll.

---

## 3. Test coverage added (49 new)

- **`test_executor.py` (11)** — real subprocess pass/fail, missing/empty
  workspace, disabled executor, unknown command, timeout actually killing the
  process, output cap, workspace create/clear. Includes a shell-injection
  guard asserting that `&&` in a command string never executes a second
  command.
- **`test_memory.py` (12)** — gating, tier ordering, promotion at threshold,
  no promotion when cold, promotion logged, auditability, and a named
  regression test for the un-approve bug.
- **`test_server.py` (17)** — every REST endpoint over real HTTP on an
  ephemeral port, validation and 404/400 paths, SSE frame format, cursor
  replay, resume-without-replay, and a test that runs the loop on one thread
  while a client reads the stream on another.
- **`test_loop.py` (+9)** — executed failure blocking approval, executed pass
  allowing it, `test_disagreement` recording, results reaching the validator
  prompt, `na` fallback, redo wiping the workspace, memory injection with
  gating held end-to-end, tools reaching the runner, workspace in the prompt.

---

## 4. Manual end-to-end verification

Run against a real server with a real database, in a browser:

1. Seeded two tasks and two memory facts through the CLI.
2. Drove one task through **revise → approve** (verdict history showed both;
   `revision_count` 1) and a high-risk task to **needs_human sign-off**.
3. Started `agentloop serve`; dashboard loaded, SSE connected ("streaming").
4. Clicked **Approve** on the high-risk task → moved to Done, tiles updated to
   2/2 "none blocked", and `human_approve` + `status:done` appeared in the
   audit feed. Confirmed the change reached the database.
5. Memory panel showed pending facts with Approve/Discard and the approved
   `test_command` at **6 hits** — proving approved memory was really injected
   into agent prompts during those runs.
6. Defined a task **through the dashboard form**; it appeared in Pending.
7. Ran `agentloop run` as a **separate OS process** while the browser watched:
   the board updated live with no refresh (task → Needs you, tokens 872 →
   1.1k, calls 6 → 8), proving cross-process, cross-thread streaming.
8. Browser console clean — no errors or React warnings.

Two layout defects were found and fixed this way: the board wrapped across
three rows, and fixing that pushed the page into horizontal scroll (grid items
default to `min-width: auto`). Both are corrected and rebuilt.

---

## 5. Considerations and honest caveats

1. **The SDK tool pass-through is not verified against a live model.**
   `claude_agent_sdk` is not installed here and there are no credentials, so
   `allowed_tools` is verified by construction (`build_options`, split out to
   be testable) and by asserting the tool list reaches the runner — not by an
   actual tool-using call. **This is the one deliverable I could not prove
   end-to-end.** Worth a manual run with `--runner claude` before trusting it.

2. **Workers cannot yet write to their workspace under `MockRunner`.** The
   executor is fully wired and tested, but until a real tool-using runner
   populates the workspace, live runs report `na`. The plumbing is ready; the
   agent side needs the SDK.

3. **The loop is still sequential.** `Store` is now thread-safe, so the
   storage layer is ready for parallel workers, but `Loop.run` processes one
   task at a time. Concurrency was one of your open questions — I kept the
   Phase 1 answer (sequential) rather than deciding it silently.

4. **Memory retrieval is keyword/key-based, not semantic.** Per your choice,
   Chroma/LanceDB is deferred. `facts_for_prompt` caps at 20 facts × 400 chars
   so memory cannot crowd out the task; with many facts you will want real
   retrieval before that cap starts biting.

5. **SSE polls the DB every 500 ms.** Fine for a local single-user loop and
   trivially tunable via `stream_poll_seconds`. For Postgres later, `LISTEN
   /NOTIFY` would replace polling without changing the client.

6. **The server binds to localhost and has no auth.** Mutations are POST-only
   and static paths are confined against traversal, but do not expose the port
   on an untrusted network as-is.

7. **`context_budget_tokens` is still unenforced.** The ~70 % confidence
   summarize-and-handoff from §7 of the spec is the largest remaining spec gap
   and I'd suggest it as the next slice.

8. **Coverage is not collected.** `test_runs` stores status, exit code,
   summary, and duration, but not coverage percentage — the schema has room.

---

## 6. Open questions from your spec, and where they stand

| Question | Current answer |
|---|---|
| Confidence thresholds — global or per task type? | Global (0.70/0.40) in `LoopConfig`, plus a per-task `risk_level` gate. Per-task-type overrides are not implemented; the old doc claim to the contrary was removed. |
| What "redo the whole task" means | Same task definition, fresh agent, no carried context — now also wiping the workspace. Not a re-plan. |
| Auto-approval policy for agent-requested tools | **Not implemented.** Agents cannot request tools yet; the registry is fixed. Still open. |
| Concurrency | Sequential, unchanged. Storage layer is now ready for parallelism. |

---

## 7. Suggested next slice

Context-budget handoff (spec §7) — it is the biggest remaining gap, it is
self-contained, and the metrics needed to trigger it are already recorded per
attempt.
