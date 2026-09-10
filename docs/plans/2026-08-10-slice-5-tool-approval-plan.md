# Slice 5 — Agent-requested tools with an auto-approval policy (execution plan)

## Metadata

- Created: 2026-08-10
- Revised: 2026-08-10 (second pass — fresh-review findings resolved; **no open
  decisions**)
- Revised: 2026-08-10 (third pass — second fresh review's 2 blocking + 2 advisory
  findings resolved as a narrow edit; **released to build, no further review pass**)
- Status: ready to build
- Verification Rigor: **critical_path**
- Plan Mode: **execution_plan**
- Design file (authoritative, do not re-litigate):
  `C:/Coding/Projects/Agents-Workflow/docs/plans/2026-08-10-slice-5-tool-approval-design.md`
  — **re-read it**; it was corrected between the two passes on two points
  (`REAL` timestamps in the `tool_requests` DDL, and a narrowed statement of what
  "context intact" means under Approach Chosen → Human decision).

### What the second pass changed (read before building)

A fresh-context reviewer audited pass 1 against the design and the code. Four
blocking findings, all factually verified, are resolved here:

1. **The release predicate was wider than the design.** Pass 1 released on
   `status == NEEDS_HUMAN`; the design says "parked on **THIS** request". Nothing
   persisted which request parked a task, so approving a stale `optional` request
   would have silently reverted an unrelated fail-safe escalation (exhausted
   revisions, severe disagreement, budget cap). Resolved by Finding 3 + ADR-5:
   a `parked` column on `tool_requests`, a three-part release predicate, and
   named tests for all three escalation paths.
2. **Two of the slice's own signatures did not close.** `agents._invoke` has no
   `config` parameter, and `tools_for` had no way to derive the `NOT NULL`
   `agent_kind` column. Resolved by Findings 4 and 5 + ADR-6 + ADR-7, and the
   Consumes/Produces self-review is redone honestly at the bottom.
3. **Timestamp type.** `REAL`, not `TEXT`, propagated to Phases 1, 2 and 8 with
   an affinity test.
4. **Acceptance criterion 5 was satisfiable only in appearance.** `Task` has no
   `feedback` field, so "feedback intact" was unassertable. Restated to what is
   true, load-bearing and directly assertable: the row, the audit trail and
   **the workspace** survive — the last being the real difference from a redo,
   since `clear_workspace` is called in exactly one place (`loop.py:891`).

Plus four advisories: `release_claim` now logs a `claim_released` event
(ADVISORY 5 / ADR-1); Phase 6 patches in the **consumer's** namespace and proves
the patch took effect (ADVISORY 6); E13 is restated to the real control flow and
split into three rows (ADVISORY 7); and "a `declared`-source request is always
non-blocking" now has a contract line, an edge case and two tests (ADVISORY 8).

### What the third pass changed (narrow edit — read alongside the above)

A second fresh review found two more blocking defects, both verified against the
code and both created *by* the second pass's own fix:

1. **`parked` was cleared only by a firing release** (`tool_requests_clear_parked`
   had exactly one call site). `human_redo` and `pause`+`resume` end the parked state
   without touching a `tool_requests` row, and **four** escalation gates fire
   *upstream* of the park check — budget cap (`loop.py:591-592`→`1029-1033`), worker
   `ESCALATE:` (`:629-635`), empty output (`:636-665`), `_ConfigError`/`_InfraError`
   (`:696-711`). So park → redo → unrelated escalation left `parked=1` standing and
   made that escalation revertible by approving the old request. P8's three tests
   *structurally* cannot reach it (`severe` and `exhausted` only exist after the park
   check). Fixed in ADR-5 + Phase 2b (clear in `human_redo` and in `resume`'s
   `PAUSED` branch) with a named end-to-end test in Phase 5.
2. **The `resume` lease fix would have stripped a live worker's lease.** `resume`
   accepts any non-terminal status, in-flight ones included, and a lease-less
   in-flight row matches neither disjunct of the claim SELECT and is invisible to
   `stranded_claims`, while `next_pending_task` still reports it. Both new calls are
   now specified **inside** the `if task.status == TaskStatus.PAUSED:` branch, with
   `test_resuming_a_live_in_flight_task_keeps_its_lease` as the non-vacuity control.
   Phase 2's `claim_released` justification, which had leaned on the wrong-path
   reasoning, is restated on ADR-1's own two grounds.

Plus two advisories: P2's security property now carries its missing
`baseline_tools(spec.role)` term and Phase 3 writes the resolution order out with the
baseline as a numbered step; and three tests that the flow map named but no phase
budgeted (`test_optional_side_effecting_request_leaves_the_task_running`,
`test_readonly_request_does_not_change_the_outcome`,
`test_blocking_request_is_queued_as_blocking`) are now budgeted in Phases 5 and 4b.

What the second review re-verified as **sound and released**: every signature
(`_invoke`'s missing `config`, the four call sites, `config=None` as total absence at
every caller, with no live-loop path reaching `_invoke` with `config=None` once
Phase 4a passes `self.config`); ADR-4 and the paid-transaction path; E13a/b/c against
`loop.py:734-778`; every multi-request release ordering; the five event kinds;
`clear_workspace`'s sole call site as P10's control. It hunted for a thirteenth
undisclosed *design* difference and found none.

What the first reviewer re-verified as **sound and not to be churned**: Finding 1 (the
`claimed_by` lease), Finding 2 (transaction nesting, including that
`PRAGMA foreign_keys=ON` at `store.py:373` makes the no-FK DDL load-bearing and
that ADR-4's prohibition on `try/except` is right), the four-call-site census,
the phase dependency graph, the defaulted-parameter strategy in Phase 4a, and
that no existing test asserts a closed key set on `task_metrics`/`run_metrics`.

### Why `critical_path` (the router's pre-recorded choice is correct)

This slice is a **permission gate** on a system whose stated threat model is
arbitrary AI-generated code (`executor.py`, `README.md` §Sandboxing). Three of
its properties are the kind a green suite does not evidence:

1. It decides what capability an agent receives (`tools` → `resolve_tools()` →
   SDK `allowed_tools`). A gate that fails open grants a capability nobody
   approved.
2. It writes inside `agents._invoke`'s **closing transaction**, which holds an
   already-paid `finish_attempt`. A raise there is re-paid by `_with_retry` —
   a money path.
3. It adds a state transition (`NEEDS_HUMAN` ⇄ `PENDING`) to a state machine
   whose claim path is a compare-and-swap. Getting the release wrong produces a
   task that looks runnable and never runs.

So this plan carries the `critical_path` extras: a behavior contract, an
edge-case catalog, provable properties, a purity-boundary map, and a
verification strategy. Confirmed, not corrected.

---

## Agreement Snapshot

- **Goal:** Agents may request tools at run time; read-only requests
  auto-approve, side-effecting ones queue for human sign-off, and every request
  and decision is audited as `events` rows and surfaced in the CLI and dashboard.
- **Constraints (all inherited and binding, from `CLAUDE.md`):**
  - stdlib-only core, Python ≥ 3.10. No new runtime dependency.
  - A whole new table needs no `_migrate()` entry; new **columns** on existing
    tables do. This slice adds a table and **no** column.
  - Every state change and agent I/O gets an append-only `events` row; never
    UPDATE/DELETE `events`.
  - Each row change is paired with its audit event inside one
    `Store.transaction()`; every unbatched write goes through
    `_LockedConnection.write()`; never a bare `execute()` + `commit()`.
  - Telemetry must never fail an attempt; anything logged is coerced to a
    bounded string first.
  - Fail safe toward `NEEDS_HUMAN`; never guess-approve.
  - Dataclasses + str-Enums for domain types; type hints everywhere.
  - No vendor SDK outside `runner.py`.
  - Tests end-to-end through the `Loop` with a scripted `MockRunner`, in
    `tests/`. No API keys, no network, nothing skipped.
  - `ruff format .` clean. Tests run with `.venv\Scripts\python.exe -m pytest -q`.
  - `README.md` + `CLAUDE.md` updated for the new decision rule, knobs, table
    and event kinds.
  - `web/src/types.ts` mirrors the server's JSON shapes.
- **In Scope:** `config.py` knobs · `models.py` types · one `tool_requests`
  table + accessors · new `agentloop/toolpolicy.py` seam · enforcement at the
  `tools` list in `agents.py` · marker parsing in `_invoke`'s closing
  transaction · one added decision rule in `loop.py` (blocking park) + two human
  decision methods · a **narrow, persisted** "parked on this request" predicate,
  cleared by every exit from the parked state ·
  lease release so a released task is genuinely claimable, **including the two
  pre-existing paths `Loop.human_redo` and `Loop.resume`** (D-1 = B, Phase 2b —
  `resume`'s release confined to its `PAUSED` branch) ·
  CLI `tools` subcommand · REST + SSE surface · `ToolRequestPanel.tsx` ·
  README/CLAUDE.md updates.
- **Out of Scope:** executing agent tools inside agentloop · the SDK's live
  `can_use_tool` callback · argument-level policy · cross-task promotion of
  grants · changing `LOGICAL_TOOL_MAP`'s contents or `OpenAICompatRunner`'s
  tools-dropped-with-a-warning behavior · any change to an existing decision
  rule other than **adding** the blocking tool-request escalation.
- **Open Decisions:** **none.** D-1 is answered (below). Every design question the
  interview covered is settled and is not reopened anywhere in this plan.

### D-1 — RESOLVED (human decision: **option B**)

**Scope of the `claimed_by` lease repair.** Verification (Finding 1) proves that
returning *any* task to `PENDING` while its `claimed_by` lease is still set makes
it permanently unclaimable — and worse, starves every other pending task in the
queue, because the stuck row still matches the claim SELECT and burns all
`_CLAIM_ATTEMPTS = 100` iterations.

**Decision (human, recorded):** slice 5 adds **one** `Store.release_claim(task_id)`
and uses it in **three** places — the new tool-approval release path (Phase 5),
`Loop.human_redo` and `Loop.resume` (Phase 2b). Phase 2b is therefore **in
scope**, unblocked, and `AFK`. It is strictly a bug fix: it changes no decision
rule, and it makes `README.md`'s existing promise that `agentloop redo <id>`
recovers a stranded claim true rather than aspirational.

Rejected: the narrow option (slice-5 path only), which would have left a
documented-but-false README promise and a live queue-starvation bug in place
while adding the very accessor that fixes them.

---

## Findings (1-2: the items the design flagged · 3-5: the fresh review's blockers)

### Finding 1 — the `claimed_by` lease (design §Error Handling, Doubt Pass #2)

**The design's premise is false, and the plan must not "match the precedent".**

The design says: *"Read what `human_redo` does about the lease (it also returns
a task to `PENDING`) and match that precedent exactly."* `human_redo` does
**nothing** about the lease. Nothing in the codebase ever clears `claimed_by` —
grep confirms the only writer is `claim_next_task`'s stamp. `update_task` and
`set_status` both deliberately omit the column.

Two secondary corrections to the framing in the task brief:

- `_UNBLOCKED` does **not** require `claimed_by IS NULL`. It is three
  predicates: `kind='task'`, every dependency `done`, plan approved
  (`store.py:548-555`). The `claimed_by IS NULL` guard lives only in
  `claim_next_task`'s compare-and-swap UPDATE (`store.py:617-622`).
- The consequence is therefore worse than "this task can't run": the stuck row
  still **matches the SELECT** (`status='pending'`), so the claim loop
  re-selects it, fails the CAS, and burns all `_CLAIM_ATTEMPTS = 100` iterations
  before returning `None` — starving every other claimable task behind it.

Verified empirically against the real store (throwaway probe, `.venv` python):

```
claimed: 1 TaskStatus.IN_PROGRESS 'loop'
after release (set_status -> PENDING): TaskStatus.PENDING claimed_by= 'loop'
re-claim result: None          <-- task 1 unclaimable
next_pending_task peek: 1      <-- and task 2, pending+unclaimed, never reached
```

**Plan decision (settled):** every path that returns a task to `PENDING` must
clear the lease, through one new store accessor
`Store.release_claim(task_id: int) -> None`. It is the sole writer of the
release, mirroring how `set_control` is the sole writer of `control`. Phase 2
adds it; Phase 5 calls it in the tool-request release; Phase 2b (D-1 = B) adds the
two pre-existing call sites. It logs a `claim_released` event in the same
transaction (ADVISORY 5 / ADR-1), so this accessor is not an exemption to the
row+event rule.

**Second settled detail on the same path.** `set_status(task, PENDING, reason="")`
does **not** clear `escalation_reason` — `store.py:696` only assigns when
`reason` is truthy. So the release must set `task.escalation_reason = ""`
explicitly first, exactly as `human_redo` (`loop.py:888`) and `approve_plan`
(`loop.py:573`) already do. Without it a released task reads "parked awaiting
tool approval" forever.

### Finding 2 — nesting inside `_invoke`'s closing transaction

**It nests safely, but a defensive `try/except` around it would be actively
harmful — and that is the trap `CLAUDE.md` predicted for this slice.**

Mechanics, verified by reading `_LockedConnection.transaction`
(`store.py:292-339`):

- `transaction()` is reentrant on an `RLock` with depth bookkeeping that
  decrements on **both** the success and error paths, and rolls back once at the
  outermost boundary. `store.tool_request_add(...)` opening its own
  `with self.transaction():` inside `_invoke`'s closing `with store.transaction():`
  therefore joins the outer transaction and defers its commit. Identical to what
  `finish_attempt` and `log_event` already do there (both route through
  `_conn.write()`, which is itself a `transaction()`). **Nesting is safe.**
- **The trap:** if the inner transaction raises and an enclosing `try/except`
  swallows it, `_txn_aborted` stays set, the outer block exits normally, the
  whole group is rolled back — including the already-paid `finish_attempt` — and
  `transaction()` raises `TransactionAborted` at the outermost exit. `_with_retry`
  then re-pays for the completion. `CLAUDE.md` says of exactly this:
  *"Unreachable until something nests a `try` around an inner transaction;
  slices 5-6 do."* This slice must not be the slice that makes it reachable.

**Plan decision (settled) — raise-sources are eliminated by construction, not
caught:**

1. `toolpolicy.parse_tool_requests(text)` is called **before** the closing
   `with store.transaction():` opens, and is total (its own internal
   `except Exception: return []` is safe precisely because it touches no store
   and no transaction). It is a pure function; the `_extract_findings` precedent.
2. Every value handed to `tool_request_add` is a **bounded plain `str`, `int` or
   `bool` before the call**: `tool` through `agents._tool_name_repr`, `reason`
   through `agents._plain_str(value, _MAX_TOOL_REASON_CHARS)` (200), `blocking`
   through `bool()`. So `log_event`'s single `json.dumps` cannot fail.
3. The `UNIQUE(task_id, role, tool)` collision is handled with an explicit
   read-then-`UPDATE`/`INSERT` inside the accessor's own transaction — never a
   bare `INSERT` that can raise `IntegrityError`. The upgrade rule needs the
   read anyway (see Phase 2).
4. `tool_requests` declares **no** `REFERENCES tasks(id)` / `attempts(id)` (the
   design's DDL, kept verbatim). With `PRAGMA foreign_keys=ON` a FK would be a
   new raise-source inside the paid transaction; its absence is load-bearing,
   not an oversight, and the builder must not "fix" it.
5. **No `try/except` anywhere between `_invoke`'s closing `with` and the
   `tool_request_add` calls.** A residual catastrophic store failure (disk full)
   fails the attempt exactly as a failing `finish_attempt` already would — which
   is today's behavior, not a regression.

`tools_for` (the *declared*-source path) writes rows too, but it is called from
`run_worker`/`run_validator`/`run_planner` **before** `_invoke`, outside any
transaction and before any model call is paid for. Its failure mode is a plain
raise on an unpaid attempt → `_with_retry` → `infra_error`, which is correct.

### Finding 3 — "parked on THIS request" has to be **persisted**, or the release reverts unrelated escalations

**This is the replan reason, and pass 1 had it wrong.** The design's §Data Flow
says the release fires `if task parked on THIS request`. Pass 1 stated the
release body unconditionally, which makes the operative predicate
`status == NEEDS_HUMAN` — strictly wider than the design, and nothing in the
design's DDL records *which* request parked a task.

Verified against the code, the concrete failure: round 1's worker emits
`TOOL_REQUEST: shell (optional) - …`, creating a `pending`, **non-blocking** row
that correctly parks nothing. Three revisions later the loop escalates at
`loop.py:769-775` with `escalation_reason="Exhausted 3 revisions without
approval."` A human clearing the tool queue approves that stale optional request.
Under the wide predicate the reason is blanked, the lease dropped and the status
set to `PENDING`; the next run pays a full worker + validator round and
re-escalates at the same line with `revision_count` already at the cap. The same
path silently reverts a **severe-disagreement** escalation (`loop.py:745-755`)
and a **budget-cap** escalation (`loop.py:1029-1033`). A tool decision overriding
three unrelated fail-safe escalations inverts "fail safe toward NEEDS_HUMAN",
which is the one direction this project never trades away.

**Plan decision (settled) — the park is recorded on the row that caused it.**
`tool_requests` carries `parked INTEGER NOT NULL DEFAULT 0` (a column on a *new*
table, so still no `_migrate()` entry). The park sets it to 1 on exactly the rows
it named in the escalation reason, inside the same transaction as the
`set_status(NEEDS_HUMAN)`. The release then fires only when **all three** hold:

1. `request.parked` is true — the loop parked *on this row*;
2. `task.status == NEEDS_HUMAN` — the park has not already been resolved another
   way (a redo, an abort, a human approve/reject of the task);
3. **no other** `pending` + `blocking` + `parked` row remains on the task after
   this decision — otherwise the release would pay a full worker round only to
   re-park at the next boundary.

Anything else records the grant and leaves the status alone (E12). The release
also clears the task's `parked` flags, so a later park is a fresh fact rather
than a residue. See ADR-5 for the alternatives rejected (a `tasks` column; a
reason-string match; deriving it from `pending_blocking_tool_requests` alone —
the last one is what still lets a severe-disagreement escalation be reverted, so
it is not sufficient).

### Finding 4 — `agent_kind` is not derivable from `role`, and `tools_for` was handed neither

`tool_requests.agent_kind` is `NOT NULL`, and `agent_kind` and `role` are
**different strings**: `_invoke(store, runner, task, kind, role, …)` receives
`kind` as the caller's literal `"worker"` / `"validator"` / `"planner"` /
`"summarizer"` and `role` as `spec.role` (`agents.py:443`, `:553`, and
`run_validator`'s `registry.get(task.validator_role)`). A custom
`task.worker_role` makes them differ. Pass 1's `tools_for(store, config, spec,
task_id, role)` received only `role`, so a builder would most plausibly have
passed `role` as `agent_kind`, corrupting the one column that answers "which
agent asked for this" — and
`test_marker_parsing_covers_worker_validator_and_planner` would then assert
against a field that no longer distinguishes them.

**Plan decision (settled):** `tools_for` takes the **agent kind** explicitly and
**derives** the role from the spec it was already given:

```python
def tools_for(store, config, spec, task_id, agent_kind) -> list[str]:
    role = spec.role   # never a separate parameter: two names for one fact drift
```

Deriving rather than passing removes the drift class entirely — `role` is
`spec.role` at every one of the three call sites today (that is exactly what
`_invoke` is handed), so there is no case where the caller could legitimately
pass something else, and a parameter that can only hold one value is a parameter
that can hold the wrong one. The marker path in `_invoke` has both facts already
(`kind` and `role`) and passes them straight through. See ADR-7.

### Finding 5 — `_invoke` has no `config`, and `classify` needs one

`agents._invoke` (`agents.py:82-94`) takes no `config`, yet the classification
that decides a marker row's status runs **inside** its closing transaction. So
`_invoke` must gain the parameter; pass 1 consumed `classify(tool, config)` there
without producing the signature that supplies it. `_invoke` is also called by
`run_summarizer` (`agents.py:491`) and reached from `eval.py:325`, neither of
which has a config.

**Plan decision (settled):** `_invoke` gains `config: LoopConfig | None = None`,
and **`config is None` means the slice is entirely inert on that invocation** —
no gating in `tools_for` (already pass 1's rule) *and* no marker parsing in
`_invoke`. `classify` therefore never receives `None` and keeps a non-optional
`config` parameter: a policy function whose config may be absent would have to
invent a default risk judgment, which is precisely the "silent degrade to a
working default" `retrieval.get_backend` refuses to do. The two callers this
affects are correct under the rule: `run_summarizer` is excluded from marker
parsing anyway (ADR-2), and `eval` is a calibration harness whose scratch store
must not accumulate a tool queue. See ADR-6.

---

## Codebase Reality Check

Every claim below was verified against the repo during planning, not assumed. The
verification method is named so a reviewer can repeat it.

| What the slice needs | What the code actually does | Verified by |
| --- | --- | --- |
| A place where a gate can bite before a tool runs | `agents._invoke` passes `tools` to `runner.run`, which hands it to `resolve_tools()` -> SDK `allowed_tools`. The SDK executes tools *inside* `runner.run()`, so nothing after it can gate. | Read `agents.py:129`, `runner.py:328-335`, `runner.py:234-256` |
| Logical tool names to classify | `LOGICAL_TOOL_MAP` holds `file_io`, `file_read`, `search`, `git`, `shell`, `task_state`, `web`; `resolve_tools` drops an unknown name rather than passing it through | Read `runner.py:314-335` |
| A shipped baseline to derive, not transcribe | `DEFAULT_AGENTS` gives worker `["file_io","git","search","task_state"]`, validator `["file_io","search","task_state"]`, planner `["file_read","search","task_state"]`, summarizer `[]` | Read `registry.py:111-165` |
| `tools` order to be observable (so order is part of the byte-for-byte promise) | `_invoke` logs `{"role":…, "prompt":…, "tools": list(tools or [])}` as the `{kind}_prompt` event, and `MockRunner` records `"tools": list(tools or [])` per call | Read `agents.py:124-128`, `runner.py:288-295` |
| A new table without a `_migrate()` entry | `_SCHEMA` runs through `executescript` on every open; `_migrate` handles **columns** only. A new table is covered by `CREATE TABLE IF NOT EXISTS`. | Read `store.py:378-431` |
| Row+event atomicity | `Store.transaction()` is reentrant, defers inner commits, decrements depth on both paths, rolls back once at the outermost boundary, and raises `TransactionAborted` when an inner failure was swallowed | Read `store.py:292-339` |
| One `json.dumps` to protect | `log_event` has exactly one, on the whole payload | Read `store.py:1056-1060` |
| Coercion helpers to reuse | `_tool_name_repr`, `_plain_str`, `_tool_input_repr` already exist and already document the re-pay hazard | Read `agents.py:209-278` |
| A release-to-`PENDING` precedent | `human_redo` and `resume` both exist — **and both leave the lease set** (see the mismatch table) | Read `loop.py:797-807, 877-894` |
| What actually distinguishes a release from a redo | `clear_workspace` is called in **exactly one place**, `human_redo` (`loop.py:891`). A release therefore keeps every file the worker already wrote under `.agentloop/ws/task-{id}/`. | Grep `clear_workspace`: `loop.py:68` (import), `loop.py:891` (sole call), `tests/test_executor.py:105,109` |
| A stored `feedback` to claim survives a release | **There is none.** `Task` has no `feedback` field, nothing persists validator feedback, `run_task` starts each entry with `feedback = ""` (`loop.py:578`), and `run_worker` gates its `## Your previous output` block on `feedback` being truthy (`agents.py:430-435`). | Read `models.py` `Task`, `loop.py:578`, `agents.py:430-435` |
| A `config` to classify a marker with, inside `_invoke` | `_invoke` has no `config` parameter; `run_summarizer` and `eval.py:325` reach it without one | Read `agents.py:82-94`, `agents.py:491`, `eval.py:318-327` |
| A way to tell which agent asked | `kind` (literal `"worker"`/`"validator"`/`"planner"`) and `role` (`spec.role`) are two different `_invoke` arguments and differ under a custom `task.worker_role` | Read `agents.py:82-94, 438-450, 548-560` |
| Which verdict paths can leave a round without re-entering `run_task`'s top | Four terminate the task: severe (`loop.py:745-755`), approved → DONE or high-risk NEEDS_HUMAN (`:757-767`), revisions exhausted (`:769-775`). Only the revise path (`:776-778`) loops back to the boundary. | Read `loop.py:734-778` |
| Whether an added event kind can break a green suite | No test asserts a full event-kind sequence; the only `kinds ==` assertions are on verdict kinds (`tests/test_loop.py:81`, `tests/test_cross_validator.py:354`) | Grep `kind for e in`, `kinds ==` |
| The sole-writer + paired-event pattern a `release_claim` should copy | `set_control` — one `transaction()`, one UPDATE, one `log_event`; and the mirror-image acquire logs `task_claimed` | Read `store.py:675-684`, `store.py:626` |
| Import style in the consumer of the new seam | `agents.py` binds names directly (`from .config import estimate_cost_usd`, `agents.py:9-22`), so a monkeypatch on the *module object* would be inert | Read `agents.py:9-22` |
| Call sites of the three functions whose signature changes | Exactly four: `eval.py:325`, `loop.py:432`, `loop.py:617`, `loop.py:686`. `run_summarizer` (`loop.py:1003`) is unchanged. | Grep for `run_worker|run_validator|run_planner` |
| A CLI error path for a bad id | `main` catches `KeyError`/`ValueError` -> `error: …` on stderr, exit 1, no traceback | Read `cli.py:272-283` |
| A REST shape to copy for decisions | `POST /api/memory/{id}/{action}` returns the refreshed list; `do_POST` maps `ValueError`->400, `KeyError`->404 | Read `server.py:209-233` |
| A test harness with no API keys | No `conftest.py`; each test file defines its own `store` fixture on `tmp_path`. `MockRunner` + `allow_test_exec=False` is the standing pattern. | `ls tests/`, read `tests/test_loop.py:28-43` |
| Frontend check commands | `web/package.json` scripts: `typecheck` = `tsc --noEmit`, `build` = `tsc --noEmit && vite build` | Read `web/package.json` |
| Settled-decision records to honor | No `docs/adr/`, `docs/decisions/`, `docs/rfcs/` or `CONTEXT.md`. `CLAUDE.md` is the record; `docs/superpowers/specs/` holds one prior design. | Glob |

### Plan-vs-code mismatches (the contradictions, stated not hidden)

| # | The design/brief says | The code actually does | How this plan resolves it |
| --- | --- | --- | --- |
| M1 | "Match `human_redo`'s precedent on the lease exactly." | `human_redo` does **nothing** about the lease. Nothing in the repo ever clears `claimed_by`. Matching it ships an unclaimable task. | Do **not** match it. Add `Store.release_claim` (ADR-1). Repair scope for the two pre-existing paths was **D-1**, answered "B": Phase 2b fixes both. |
| M2 | "`_UNBLOCKED` requires `claimed_by IS NULL`." | It requires `kind='task'`, deps `done`, plan approved — **no** `claimed_by` clause. The guard is in the CAS UPDATE. | Corrected; it changes where a fix may legitimately go (ADR-1 rejects the `_UNBLOCKED` route explicitly). |
| M3 | Releasing with `set_status(task, PENDING, reason="")` clears the parked reason. | `set_status` only assigns `escalation_reason` when `reason` is **truthy**, so `""` leaves the old reason in place. | Phase 5 sets `task.escalation_reason = ""` explicitly first, as `human_redo` and `approve_plan` already do. |
| M4 | Put the request write in `_invoke`'s closing transaction (implying defensive care there). | A `try/except` around a nested transaction there triggers `TransactionAborted`, rolling back the paid `finish_attempt`. `CLAUDE.md` predicts slices 5-6 would hit this. | ADR-4: eliminate raise-sources; **forbid** the `try/except`. |
| M5 | `tools_for(...)` is called from the `run_*` functions. | Those three functions have no `config` parameter today. | Phase 4a adds `config=None` (= no gating) and updates all four call sites; Phase 4b does the same for `_invoke` (M9). |
| M6 | A grant makes a tool reach the next invocation's `tools`. | `tools` is built from `spec.tools` alone, so a grant would change nothing for a read-only tool already permitted by policy rule 1. | ADR-3: grants **add** to the list; the gate **removes**. Without this, acceptance criterion 1 is unsatisfiable. |
| M7 | The release fires "if the task is parked **on THIS request**" (design §Data Flow). | Nothing records which request parked a task, so the only predicate the design's own DDL can express is `status == NEEDS_HUMAN` — which also matches three unrelated fail-safe escalations. | Finding 3 + ADR-5: a `parked` column on the new table, a three-part predicate, and a test per escalation path. Recorded as difference #7 — pass 1 dropped the qualifier without disclosing it. |
| M8 | `tools_for(store, config, spec, task_id, role)` supplies everything the row needs. | `tool_requests.agent_kind` is `NOT NULL` and is **not** `role`: `role` is `spec.role`, `agent_kind` is the caller's literal kind, and a custom `task.worker_role` makes them differ. | Finding 4 + ADR-7: `tools_for(store, config, spec, task_id, agent_kind)`, with `role = spec.role` derived inside. |
| M9 | The `classify`-derived status is computed inside `_invoke`'s closing transaction. | `_invoke` has no `config` parameter, and two of its callers (`run_summarizer`, `eval.py:325`) have no config to give it. | Finding 5 + ADR-6: `_invoke` gains `config=None`; `None` means fully inert (no gating, no marker parsing), so `classify` keeps a non-optional config. |
| M10 | Acceptance criterion 5's "context intact" (as first written) includes feedback. | `Task` has no `feedback` field and nothing persists validator feedback. On the park path there *is* no feedback (the validator never ran), and the worker's own partial text is not restored either. | The design was **corrected** to a narrowed promise; the plan follows it. Phase 5 asserts what is true and load-bearing — the row, the audit trail and **the workspace** — and states the prompt limitation rather than testing a field that does not exist. |

---

## Assumption Ledger

Every important claim this plan rests on, classified. Nothing critical is left
unclassified, and after D-1's answer there is **no** `needs_user_confirmation`
item left.

| Claim | Class | Evidence / what would falsify it |
| --- | --- | --- |
| A task returned to `PENDING` with `claimed_by` set is unclaimable, and starves other pending tasks | **proven_by_code** | Ran a probe against the real `Store`: `claim_next_task` returned `None` for both the released task and an unrelated pending task |
| Nothing in the repo clears `claimed_by` | **proven_by_code** | Grep: the only writer is `claim_next_task`'s stamp |
| `_UNBLOCKED` contains no `claimed_by` clause | **proven_by_code** | Read `store.py:548-555` |
| `set_status(..., reason="")` does not clear `escalation_reason` | **proven_by_code** | Read `store.py:694-697` |
| A nested `transaction()` joins the outer one and defers its commit | **proven_by_code** | Read `store.py:292-339` |
| A swallowed inner-transaction failure rolls back the outer group and raises `TransactionAborted` | **proven_by_code** | Read `store.py:316-339`; `CLAUDE.md` documents the same conclusion |
| `log_event` has exactly one `json.dumps`, so an un-encodable field fails the whole transaction | **proven_by_code** | Read `store.py:1056-1060` |
| `tools` order is observable via the `{kind}_prompt` event and `MockRunner.calls` | **proven_by_code** | Read `agents.py:124-128`, `runner.py:288-295` |
| A new table needs no `_migrate()` entry | **proven_by_code** | Read `store.py:378-431` |
| Exactly four call sites need updating for the Phase 4a signature change | **proven_by_code** | Grep enumerated `eval.py:325`, `loop.py:432/617/686` |
| A defaulted `config=None` parameter cannot break the existing `eval` call site | **proven_by_code** | Read `eval.py:325`; a defaulted keyword is additive |
| `run_summarizer` falls back to the worker's spec when the role is missing | **proven_by_code** | Read `agents.py:472-477` |
| `OpenAICompatRunner` drops `tools` with a `RuntimeWarning` and executes none | **proven_by_code** | Read `runner.py:615-632` |
| The five new event kinds reach the SSE feed with no server change | **proven_by_code** | `events` *is* the feed: `events_since` + `_stream` read the table (`server.py:305-338`) |
| Appending granted tools in ascending `tool_requests.id` order is deterministic | **inferred** | A design choice, not forced by code. Falsified if two requests could share an id, which AUTOINCREMENT prevents |
| `clear_workspace` runs only in `human_redo`, so a release preserves the workspace | **proven_by_code** | Grep: `loop.py:891` is the sole call site; `tests/test_executor.py` is the only other caller |
| Nothing persists validator feedback, so "feedback intact" is unassertable | **proven_by_code** | `Task` has no such field; `loop.py:578` re-initialises `feedback = ""` per entry |
| `agent_kind` and `role` are different strings and can diverge | **proven_by_code** | Read `agents.py:82-94, 438-450`; `role` is `spec.role`, `kind` is a literal |
| `_invoke` has no `config`, and two callers cannot supply one | **proven_by_code** | Read `agents.py:82-94`, `agents.py:491`, `eval.py:325` |
| A validator-authored blocking request parks only on the **revise** path; the other four verdict outcomes terminate the round with the row left `pending` (E13a-c) | **proven_by_code** | Read `loop.py:734-778`: severe, approve→DONE, approve→high-risk, exhausted all `return task`; only `:776-778` loops back to the boundary where the park check lives |
| Adding a `claim_released` event kind cannot redden the existing suite | **proven_by_code** | Grep: no test asserts a full event-kind sequence (`tests/test_loop.py:81` and `tests/test_cross_validator.py:354` assert verdict kinds) |
| A monkeypatch on `agentloop.toolpolicy.<name>` would be **inert** if `agents.py` binds the names directly | **proven_by_code** | Read `agents.py:9-22` — direct `from .x import y` throughout; Phase 6 therefore patches `agentloop.agents.<name>` and proves the patch bit (P1-control-b) |
| The three-part release predicate cannot revert an unrelated escalation | **inferred** | Follows from requiring `request.parked`, which only the park writes. Phase 5's three escalation-path tests are what convert it to proven |
| The per-task cap can refuse "once" by leaning on the UNIQUE row for repeats | **inferred** | Behavior of an accessor not yet written; Phase 2's `test_the_per_task_cap_refuses_once` is what converts it to proven |
| Each phase as sequenced leaves the suite green | **inferred** | Follows from store/config/policy landing before the loop rule; only build-time execution proves it |
| Phase 8's 6-step manual checklist matches the real dashboard flow | **inferred** | Read `App.tsx`, `MemoryPanel.tsx`, `StatBar.tsx`; not executed against a running server |
| This slice also repairs `human_redo`/`resume` (D-1) | **settled by the human** | Answered: option B. Phase 2b is in scope and unblocked |

---

## Durable Decisions (all phases reference these)

Foundational choices every phase must honor. Changing one invalidates phases, not
just lines.

| Area | Decision |
| --- | --- |
| **Schema** | One table, `tool_requests`, DDL **verbatim as quoted in Phase 2** (the design's, with `REAL` timestamps and one column the design's DDL lacks: `parked`), `UNIQUE(task_id, role, tool)`, **no foreign keys** (M4/Finding 2). No second `tool_grants` table: an `auto`/`approved` row *is* the grant. No new columns on any **existing** table, therefore no `_migrate()` entry — and `parked` is in the DDL from Phase 2, never bolted on later, so no database ever sees the table without it. |
| **Domain model** | `ToolRequest` dataclass; `ToolRequestStatus` and `ToolRequestSource` as str-Enums in `models.py`; `ToolClass` and `ParsedToolRequest` stay in `toolpolicy.py` (policy results, never persisted). Every enum reaching SQL or JSON does so as `.value`. |
| **Enforcement point** | The `tools` list passed to `runner.run()`, computed in `run_worker`/`run_validator`/`run_planner` via `toolpolicy.tools_for`. Never post-hoc on observed `tool_calls`; never the SDK's live `can_use_tool`. |
| **Grant semantics** | Task-scoped and role-scoped. Grants **add** to the tools list; the gate **removes** (ADR-3). No cross-task promotion, ever. |
| **Request sources** | `marker` (parsed from agent output, worker/validator/planner only — ADR-2) and `declared` (behind `gate_declared_tools`, default `False`, baseline derived from `DEFAULT_AGENTS`). |
| **Transaction discipline** | Every row change pairs with its event in one `Store.transaction()`. Marker parsing happens **before** `_invoke`'s closing transaction; the write happens **inside** it; **no `try/except` between them** (ADR-4). |
| **State machine** | Exactly one added rule: a `pending` + `blocking` request parks the task at `NEEDS_HUMAN`, output kept, `revision_count` untouched, validator skipped; the park stamps `parked=1` on the rows it named. No new event kind for the park. **Release is conditional, never unconditional** — only when `request.parked` **and** `task.status == NEEDS_HUMAN` **and** no other `pending`+`blocking`+`parked` row remains (Finding 3, ADR-5). When it does fire: clear the task's `parked` flags + clear `escalation_reason` + `release_claim` + `set_status(PENDING)`, in one transaction, on a freshly loaded `Task`. Otherwise: grant recorded, `released: false`, **status and `escalation_reason` untouched**. **`parked` is additionally cleared by `human_redo` and by `resume`'s `PAUSED → PENDING` branch** — every exit from the parked state clears it, or a stale flag makes the four pre-park-check escalations revertible (ADR-5). |
| **Declared source is never blocking** | `tools_for` calls `tool_request_add(..., blocking=False)` unconditionally on the declared path and never computes the flag. `gate_declared_tools=True` must withhold and continue — it is a knob, not an escalation engine (design §Significance, third bullet; contract line + E21 + two tests). |
| **Inertness switch** | `config is None` at the `run_*` / `_invoke` layer means the slice is entirely absent from that invocation: no gating, no marker parsing, no rows, no events (ADR-6). `classify` therefore never sees `None` and keeps a required `config`. |
| **Routes** | `GET /api/tool_requests[?task_id=&status=]`; `POST /api/tool_requests/{id}/approve`; `POST /api/tool_requests/{id}/reject`. Each POST returns `{"tool_requests": [...]}`, mirroring `/api/memory`. `/api/config` gains the two knobs. |
| **Third-party boundary** | None added. stdlib only; no vendor SDK outside `runner.py`; no new runtime dependency; no new `--runner` choice. |

---

## Behavior Contract (critical_path)

`toolpolicy.tools_for(store, config, spec, task_id, agent_kind) -> list[str]`

| Guarantee | Statement |
| --- | --- |
| Inertness | With `gate_declared_tools=False` and no `tool_requests` row for `(task_id, spec.role)`, returns a list **equal in content and order** to `spec.tools`. No row is written, no event logged. |
| Role is derived, never passed | `role` is `spec.role`, computed inside. `agent_kind` is the caller's literal kind and is the *only* other identity argument, so the two can never be transposed (Finding 4, ADR-7). |
| Declared is never blocking | Every row this function writes is created with `blocking=False`, unconditionally. `tools_for` cannot park a task, so `gate_declared_tools=True` withholds and continues. |
| Order | Declared tools keep `spec.tools` order. Granted-but-undeclared tools are appended in ascending `tool_requests.id` (request-creation) order. Never sorted, never de-ordered. |
| Monotonicity of the gate | The gate only ever **removes** a declared tool; a grant only ever **adds** a tool. Neither reorders the other's contribution. |
| Grants add | A tool with an `auto`/`approved` row for `(task_id, spec.role)` is present in the returned list even when absent from `spec.tools`. This is the mechanism behind acceptance criterion 1. |
| No unknown grant | A tool absent from `runner.LOGICAL_TOOL_MAP` is never returned, whatever its row says. |
| Purity | Pure with respect to `spec` and `config`; its only writes are `tool_request_add` calls on the declared path. Never touches task status, and never writes `parked`. |
| Idempotence | Called twice for the same `(task_id, spec.role)` with the same inputs, the second call writes nothing new (the UNIQUE row absorbs it) and returns an equal list. |

`agents._invoke(..., config=None)` and `toolpolicy.classify(tool, config)`

| Guarantee | Statement |
| --- | --- |
| `config is None` is total absence | No marker parsing, no rows, no events, and `tools` passed through as the caller built it. The two callers that cannot supply a config (`run_summarizer`, `eval.py:325`) therefore behave exactly as they do today. |
| `classify` never sees `None` | Its `config` parameter is required. A policy function with an optional config would have to invent a risk judgment, which is what `retrieval.get_backend` refuses to do by raising rather than degrading. |
| Identity fields are the ones `_invoke` already holds | `agent_kind` is `_invoke`'s `kind`; `role` is `_invoke`'s `role`. Neither is recomputed. |

`Loop.run_task`'s added rule:

| Guarantee | Statement |
| --- | --- |
| Park is conditional on `blocking` | Only a `pending` + `blocking=1` row parks the task. `optional`, `auto`, `approved`, `rejected`, `refused` never park it. |
| Park preserves | `task.output` holds the worker's reply verbatim (marker included); `revision_count` unchanged; no validator attempt; no `test_run` row; the workspace untouched. |
| Park is recorded on its cause | The park stamps `parked=1` on exactly the rows named in the escalation reason, in the same transaction as the status change. Nothing else ever writes `parked=1`. |
| Park is one event | The transition is `set_status(NEEDS_HUMAN, reason=…)`'s existing `status:needs_human` event. **No** new event kind for the park — a second event for the same fact would double-count escalations. The `parked` stamp rides that same transaction, so the row change is still paired with an event. |
| Park fires only at the boundary | The check sits in `run_task` after the output is stored and before `TESTING`. A request created by the **validator** therefore parks only if the round ends on the *revise* path; severe, approve→DONE, approve→high-risk and exhausted-revisions all `return task` first (E13a-c). |
| Release is conditional | `approve_tool_request` releases **only** when `request.parked` and `task.status == NEEDS_HUMAN` and no other `pending`+`blocking`+`parked` row remains. Otherwise the grant is recorded, the event carries `released: false`, and `status`/`escalation_reason` are not written at all. |
| Release cannot revert an unrelated escalation | An escalation the loop did not raise through the park leaves every row's `parked` at 0, so no tool decision can return the task to `PENDING`. **This holds only because `parked` is cleared by every exit from the parked state, not merely by a firing release** — see the next row. Four of those escalations (budget cap `loop.py:591-592`→`1029-1033`, worker `ESCALATE:` `:629-635`, empty output `:636-665`, `_ConfigError`/`_InfraError` `:696-711`) fire *before* the park check's position, so a task that was once parked and left that state by any other route could otherwise reach them with a stale `parked=1` row still standing. |
| `parked` is cleared on **every** exit from the parked state | Not just the release. `Loop.human_redo` and `Loop.resume`'s `PAUSED → PENDING` branch both call `tool_requests_clear_parked` (Phase 2b), because both return a once-parked task to a runnable state without deciding the request. The row itself stays `pending` + `blocking`, so the park re-stamps it at the next boundary *if that round reaches the park check* — which is what preserves E20. Terminal exits (`human_approve` → DONE, `human_reject` → FAILED, `abort` → ABORTED) need no clear: release condition 2 requires `NEEDS_HUMAN`, which a terminal status is not. |
| Release is claimable | When it does fire, `store.claim_next_task(...)` returns the task, and the task's `parked` flags are cleared so a later park is a fresh fact. |
| Rejection parks | After `reject_tool_request`, the task is still `NEEDS_HUMAN` and no status write occurred. |
| A terminal task's pending request is moot, not auto-decided | A `pending` row left on a task that reached `DONE`/`FAILED`/`ABORTED` stays `pending`. Nothing auto-refuses it: that would be a second new decision rule, and a machine-made `refused` would destroy a grant a human may legitimately want before a `redo` (E13c). |

---

## Provable Properties (critical_path)

Each is asserted by the named test; none is left to inspection.

- **P1 (inertness is structural, proven by neutering).** On default config with
  marker-free scripts, the **full observable state** of a run is identical to
  the same run with the slice's two hooks neutered **in the consumer's
  namespace** (`agentloop.agents.tools_for` → `spec.tools`,
  `agentloop.agents.parse_tool_requests` → `[]`). Test:
  `test_marker_free_default_run_is_byte_for_byte_the_pre_slice_5_run`.
- **P1-control-a (the harness compares something).** The same differential
  harness, given a marker-bearing script, reports a **non-empty** diff. Test:
  `test_the_differential_harness_detects_a_difference_when_one_exists`.
  Without this, P1 could pass because the harness compares nothing.
- **P1-control-b (the neutering provably takes effect).** Live vs neutered on the
  **same marker-bearing** script reports a **non-empty** diff. This is the
  ADVISORY-6 guard: `agents.py` binds imported names directly, so a patch on the
  `toolpolicy` module object would be inert, both runs would be the live run, and
  P1 would pass by comparing a run to itself. Varying the *patch* rather than the
  *script* is the only thing that detects that. Test:
  `test_the_neutering_patch_provably_takes_effect`.
- **P2 (the gate cannot fail open).** With `gate_declared_tools=True`, no tool
  outside
  `config.tool_readonly_allowlist ∪ baseline_tools(spec.role) ∪ granted_tools(task_id, spec.role)`
  reaches `runner.run`'s `tools` argument. **The `baseline_tools` term is part of the
  property, not an exception to it** — `file_io` and `git` are in the shipped
  worker's `DEFAULT_AGENTS` list (`registry.py:116`) and deliberately *not* in the
  read-only allowlist, and the design's whole reason for deriving a baseline is that
  gating them on a fresh install would gut the shipped worker. Stating P2 without
  the term made it contradict the two tests that pin the behavior
  (`test_the_shipped_baseline_is_never_gated`,
  `test_the_shipped_baseline_reaches_the_runner_under_gating`), and Phase 9's docs
  quote this property. Tests: `test_gated_declared_tool_is_withheld_and_queued` plus
  those two.
- **P3 (a grant is a task-scoped row, not a standing capability).** A grant for
  task A's worker does not appear in task B's worker `tools`, nor in task A's
  validator `tools`. Test: `test_a_grant_is_scoped_to_one_task_and_one_role`.
- **P4 (the paid attempt survives its telemetry).** A marker whose tool name and
  reason are pathological (4 KB reason, non-ASCII, `{}`-bearing) still records
  `finish_attempt` with the run's tokens and cost, and creates exactly one
  bounded row. Test: `test_a_pathological_marker_never_rolls_back_a_paid_attempt`.
- **P5 (a decided row is final).** An agent re-emitting the marker for a
  `rejected` tool creates no row, logs no `tool_requested`, and does not reopen
  the decision. Test: `test_a_decided_request_is_never_reopened_by_a_re_request`.
- **P6 (release is genuinely claimable).** Post-release
  `store.claim_next_task("loop")` returns the task, and a second unrelated
  pending task is not starved. Test:
  `test_approving_a_blocking_request_returns_a_claimable_task`.
- **P7 (the summarizer is never a request source).** A summarizer reply quoting
  `TOOL_REQUEST:` verbatim creates zero rows. Test:
  `test_summarizer_output_quoting_a_marker_creates_no_request`.
- **P8 (a tool decision can never revert an escalation it did not cause).** For
  each of the three escalation paths a human is most likely to meet with a stale
  request in the queue — exhausted revisions, severe disagreement, budget cap —
  approving that request leaves `status == NEEDS_HUMAN` and `escalation_reason`
  byte-identical. Tests:
  `test_approving_an_unrelated_request_does_not_revert_an_exhausted_escalation`,
  `test_approving_a_blocking_request_the_task_did_not_park_on_leaves_the_escalation`,
  `test_approving_a_request_does_not_revert_a_budget_cap_escalation`.
- **P9 (the declared path cannot park a task).** With `gate_declared_tools=True`
  and a hand-added gated declared tool, the task runs to completion without the
  tool and every row the declared path wrote has `blocking=0`. Tests:
  `test_a_declared_source_request_is_never_blocking`,
  `test_gate_declared_tools_never_parks_a_task`.
- **P10 (the workspace is what a release preserves).** After a release the files
  the worker wrote under `.agentloop/ws/task-{id}/` are still present; after
  `human_redo` on the same fixture they are gone. The contrast is the proof —
  asserted alone, "the files exist" would also pass if nothing ever deleted them.
  Test: `test_a_release_keeps_the_workspace_that_a_redo_wipes`.

---

## Purity Boundary Map (critical_path)

| Layer | File | Purity | May raise? |
| --- | --- | --- | --- |
| Policy classification | `agentloop/toolpolicy.py` `classify`, `baseline_tools` | Pure | Yes (config-error class only; called outside any transaction) — **but `classify` is called inside `_invoke`'s closing transaction on the marker path, so its `config` is required and its body does nothing but dict/list membership tests on `str`s** |
| Marker parsing | `agentloop/toolpolicy.py` `parse_tool_requests` | Pure | **No — total by contract** |
| Enforcement | `agentloop/toolpolicy.py` `tools_for` | Reads store + writes declared-source rows | Yes; runs before the model call, on an unpaid attempt |
| Coercion | `agentloop/agents.py` `_tool_name_repr`, `_plain_str` | Pure | **No** |
| Row + event | `agentloop/store.py` `tool_request_*` | Writes, one `transaction()` each | Only catastrophically (no FK, no conflict-raise, no `json.dumps` risk) |
| Decision rule | `agentloop/loop.py` | Writes status | Yes; outside `_invoke` |

---

## Context References (MUST READ before starting)

**Read in this order.** Reference by path; nothing here is a substitute for the file.

1. `docs/plans/2026-08-10-slice-5-tool-approval-design.md` — the approved design.
   Every decision in it is settled. **Two passages were corrected after pass 1 of
   this plan and must be read in their current form:** the `tool_requests` DDL
   (`REAL` timestamps, with the rationale inline) and the "Human decision" bullet
   under Approach Chosen (the narrowed statement of what "context intact" means,
   including that `clear_workspace` has exactly one call site).
2. `CLAUDE.md` — the authoritative design spec: architecture, every decision
   rule, store invariants, conventions.
3. `README.md` — the user-facing contract; its decision-rule table gains a row.

**Patterns to follow (each is the nearest precedent for one piece of this slice):**

- `agentloop/retrieval.py` — the register `toolpolicy.py` must match: a small
  interface, all classification behind it, unit-testable with no store.
- `agentloop/agents.py:82-206` (`_invoke`) — the two-transaction shape and where
  the closing transaction's contents go.
- `agentloop/agents.py:209-278` (`_tool_name_repr`, `_plain_str`,
  `_tool_input_repr`) — the coercion the new fields reuse verbatim.
- `agentloop/agents.py:728-758` (`_extract_findings`) — the total,
  degrade-to-empty parser the marker parser mirrors.
- `agentloop/store.py:1107-1159` (`memory_read`) — a read-then-conditional-write
  in one transaction, with the rowcount check that makes it honest.
- `agentloop/store.py:1341-1387` (`memory_list`, `memory_set_approved`) — the
  list + human-decision accessor shape the CLI and REST already consume.
- `agentloop/loop.py:877-894` (`human_redo`) — the release-to-`PENDING`
  precedent, **including the defect Finding 1 documents**.
- `agentloop/loop.py:540-575` (`approve_plan`) — the "one transaction: event +
  flag + status, and clear the stale reason" shape the release copies.
- `tests/test_charter.py:96-136` — the existing byte-for-byte differential test,
  the direct model for Phase 6.
- `tests/test_cross_validator.py:490-545` — the AST control-case pattern (a
  mechanical check proved non-vacuous by also running without its exclusion).
- `tests/test_planner.py:651-665` — "the default is unchanged" asserted by
  measurement rather than assertion.
- `tests/test_executor.py:100-110` — how a test asserts on workspace files before
  and after `clear_workspace`; the mechanics P10 reuses.
- `agentloop/store.py:1107-1159` (`memory_read`) again, for `release_claim`'s
  rule that the event is logged only when the gated UPDATE matched (E24).
- `web/src/components/MemoryPanel.tsx` + `web/src/api.ts:75-78` — the gating
  panel and typed client call `ToolRequestPanel` mirrors.
- `tests/test_cli.py:15-35` and `tests/test_server.py:23-60` — the CLI bad-id and
  live-HTTP harnesses Phase 7 extends.

**Configuration files:** `pyproject.toml` (dev/claude extras), `web/package.json`,
`web/tsconfig.json`.

**Compounded knowledge:** `docs/superpowers/specs/2026-07-28-memory-promotion-transition-design.md`
— the prior slice where "a copy instead of a transition" produced a duplicate
that re-fired its event forever. Read it before designing the upgrade rule; it is
the same shape of mistake `INSERT OR IGNORE` would make here.

**No `docs/adr/`, `docs/decisions/`, `docs/rfcs/` or `CONTEXT.md` exists** —
`CLAUDE.md` is the settled-decision record. Nothing in this plan contradicts it;
the one addition (a new decision rule) is explicitly sanctioned by the intent
contract and lands in `CLAUDE.md` in Phase 9.

---

## Durability Horizon

| Piece | Horizon | Consequence |
| --- | --- | --- |
| `toolpolicy.py` seam (4 functions) | **stable** — architectural, matches `retrieval.py`/`ModelRunner` | Worth the module and the interface discipline |
| `tool_requests` table + `UNIQUE(task_id, role, tool)` | **stable** — append-mostly ledger, Postgres-portable | Worth getting the DDL right once |
| The `parked` column + the three-condition release predicate | **stable** — it is the decision rule, and the column can never be retyped or removed additively | Worth an ADR (ADR-5), a contract line, and a test per escalation path |
| The blocking-park decision rule | **stable** — enters `CLAUDE.md`/`README.md` decision tables | Needs its test, per convention |
| `Store.release_claim` | **stable** — a store invariant (a `PENDING` task holds no lease) | Worth being the sole release writer |
| Marker grammar regex | **near-term-refactor** — a stricter grammar may follow | Keep it one module constant, bounded, total |
| `ToolRequestPanel.tsx` | **near-term-refactor** — the office-metaphor slice 7 reworks the dashboard | Mirror `MemoryPanel`, do not abstract |
| The differential harness in `tests/test_tool_policy.py` | **stable** — the pattern is reused each slice | Write it as a reusable local helper |

---

## Functionality Flow Mapping

```
Flow A: an agent asks for a read-only tool it was not given
1. worker reply carries `TOOL_REQUEST: file_read (optional) - need to read config`
   → parse_tool_requests in _invoke (pre-transaction)   → test: test_parse_marker_variants
2. classify('file_read') = AUTO                          → test: test_classify_readonly_is_auto
3. tool_request_add(status='auto') + tool_auto_approved   → test: test_readonly_request_is_auto_approved_and_audited
4. next invocation's tools include 'file_read'            → test: test_an_auto_row_reaches_the_next_invocations_tools  [Phase 4b]
5. task completes normally                               → test: test_readonly_request_does_not_change_the_outcome  [Phase 5 — needs the park to exist to be non-vacuous]

Flow B: an agent asks for a side-effecting tool, optional
1. reply carries `TOOL_REQUEST: shell (optional) - ...`   → test: test_parse_marker_variants
2. classify('shell') = GATED                             → test: test_classify_side_effecting_is_gated
3. row pending + tool_requested event                    → test: test_optional_side_effecting_request_is_queued_and_audited
4. tool ABSENT from this and the next invocation's tools  → test: test_optional_request_never_reaches_the_tools_list  [Phase 4b]
5. task completes normally, without the tool             → test: test_optional_side_effecting_request_leaves_the_task_running  [Phase 5 — design acceptance criterion 3]

Flow C: an agent asks for a side-effecting tool, blocking
1..3 as Flow B, blocking=1                               → test: test_blocking_request_is_queued_as_blocking  [Phase 4b — the row only]
4. run_task reads pending_blocking_tool_requests          → test: test_blocking_request_parks_the_task_at_needs_human
5. NEEDS_HUMAN; output kept; revision_count untouched;
   no validator attempt; no test_run row                 → test: test_a_parked_task_keeps_its_partial_output_and_revision_count
                                                          → test: test_the_validator_never_runs_on_a_parked_task
6. the rows it parked on are stamped parked=1              → test: test_the_park_stamps_the_rows_it_parked_on

Flow D: the human approves a blocking request the task IS parked on
1. `agentloop tools approve 1` / POST .../approve         → test: test_cli_tools_approve, test_approve_tool_request_over_http
2. row -> approved, decided_by/at, tool_request_decided
   {released: true}                                       → test: test_approval_is_recorded_with_released_true
3. task -> PENDING, escalation_reason cleared, parked
   flags cleared, lease released, row intact
   (output + revision_count), audit trail intact          → test: test_the_release_keeps_the_row_and_clears_the_reason
4. the workspace survives (unlike a redo)                 → test: test_a_release_keeps_the_workspace_that_a_redo_wipes
5. task is genuinely claimable again                      → test: test_approving_a_blocking_request_returns_a_claimable_task
6. next invocation's tools include the granted tool;
   task completes                                         → test: test_a_grant_applies_to_the_next_invocation

Flow D': the human approves a request the task is NOT parked on
1. task is NEEDS_HUMAN for an unrelated reason
   (exhausted revisions / severe / budget cap)            → test: test_approving_an_unrelated_request_does_not_revert_an_exhausted_escalation
                                                          → test: test_approving_a_blocking_request_the_task_did_not_park_on_leaves_the_escalation
                                                          → test: test_approving_a_request_does_not_revert_a_budget_cap_escalation
2. grant recorded, tool_request_decided {released:false}   → test: test_approving_a_request_on_an_unparked_task_leaves_status_alone
3. status and escalation_reason byte-identical             → same three tests as step 1
4. task parked on TWO requests: approving one is not
   enough; the last one releases                          → test: test_a_task_parked_on_two_requests_is_released_only_by_the_last_one

Flow E: the human rejects
1. `agentloop tools reject 1`                             → test: test_cli_tools_reject
2. row -> rejected, tool_request_decided {released:false} → test: test_rejection_is_audited_and_leaves_the_task_parked
3. task stays NEEDS_HUMAN; no new release path            → test: test_rejection_is_audited_and_leaves_the_task_parked

Flow F: the operator opts into declared-tool gating
1. gate_declared_tools=True                               → test: test_gate_declared_tools_defaults_to_false
2. a hand-added side-effecting declared tool is withheld
   and queued                                             → test: test_gated_declared_tool_is_withheld_and_queued
3. the shipped baseline (DEFAULT_AGENTS) is NOT gated      → test: test_the_shipped_baseline_is_never_gated
4. baseline is derived, not transcribed                    → test: test_baseline_tools_is_derived_from_default_agents
5. the row is non-blocking, so the task CONTINUES —
   a knob, not an escalation engine                       → test: test_a_declared_source_request_is_never_blocking
                                                          → test: test_gate_declared_tools_never_parks_a_task
6. the row records the asking agent's kind, not its role   → test: test_a_declared_request_records_agent_kind_and_role_separately

Flow G: the operator inspects the queue
1. `agentloop tools list [--task ID] [--pending]`          → test: test_cli_tools_list
2. GET /api/tool_requests[?task_id=&status=]               → test: test_tool_requests_over_http
3. task detail carries its own requests                    → test: test_task_metrics_exposes_tool_requests
4. the 5 event kinds arrive on the SSE feed                → test: test_tool_request_events_reach_the_sse_feed
5. StatBar shows N waiting                                 → test: test_run_metrics_counts_pending_tool_requests

Error paths (every one has a test):
- malformed / partial marker → ignored, []                 → test: test_malformed_markers_are_ignored
- missing (blocking|optional) flag → optional               → test: test_a_missing_flag_means_optional
- unknown logical tool → refused + event, never granted     → test: test_an_unknown_logical_tool_is_refused
- over max_tool_requests_per_task → refused + event, once   → test: test_the_per_task_cap_refuses_once
- duplicate marker in one reply → collapses, strongest wins → test: test_duplicates_collapse_and_blocking_beats_optional
- optional then blocking across rounds → one row upgraded   → test: test_a_pending_row_upgrades_from_optional_to_blocking
- already-decided row → never upgraded                      → test: test_a_decided_row_is_never_upgraded
- re-request of a decided tool → nothing created            → test: test_a_decided_request_is_never_reopened_by_a_re_request
- summarizer quotes a marker → no row (control case)        → test: test_summarizer_output_quoting_a_marker_creates_no_request
- approve/reject on an already-decided request → ValueError → test: test_deciding_twice_is_refused, test_cli_tools_bad_id
- approve a request whose task is not parked → grant only    → test: test_approving_a_request_on_an_unparked_task_leaves_status_alone
- validator blocking marker, round ends on approve → DONE,
  row stays pending and moot, nothing auto-decides it       → test: test_a_validator_blocking_request_on_the_approve_path_never_parks
- validator blocking marker, round ends on revise → parks    → test: test_a_validator_blocking_request_parks_only_on_the_revision_path
- pathological marker payload → attempt survives            → test: test_a_pathological_marker_never_rolls_back_a_paid_attempt
- no config (eval / summarizer) → slice fully inert          → test: test_a_none_config_parses_no_markers_and_gates_nothing
- bad request id on CLI → clean error, exit 1, no traceback  → test: test_cli_tools_bad_id
```

---

## Edge-Case Catalog (critical_path)

Each row is settled behavior, taken from the design's Error Handling table or
derived from code the design required verification against. None is left to the
builder's judgment.

| # | Case | Settled behavior | Named test |
| --- | --- | --- | --- |
| E1 | Marker with no flag | `optional` | `test_a_missing_flag_means_optional` |
| E2 | Separator `-`, em-dash, `:` or none | all accepted; reason may be empty | `test_parse_marker_variants` |
| E3 | Reason over 200 chars | truncated to `_MAX_TOOL_REASON_CHARS` | `test_a_long_reason_is_bounded` |
| E4 | Tool name outside `[A-Za-z0-9_.-]{1,40}` | no match, no row | `test_malformed_markers_are_ignored` |
| E5 | Two markers, same tool, one blocking | one request, `blocking=True` | `test_duplicates_collapse_and_blocking_beats_optional` |
| E6 | `optional` round 1, `blocking` round 2 | one row, `UPDATE blocking=1`, `tool_requested` with `upgraded: true` | `test_a_pending_row_upgrades_from_optional_to_blocking` |
| E7 | Upgrade attempt on `approved`/`rejected`/`refused` | ignored; no row change, no event | `test_a_decided_row_is_never_upgraded` |
| E8 | Tool not in `LOGICAL_TOOL_MAP` | row `refused`, `tool_request_refused` + `why`, never granted | `test_an_unknown_logical_tool_is_refused` |
| E9 | 11th request on a task at cap 10 | `refused` + event, **once** (the UNIQUE row absorbs repeats) | `test_the_per_task_cap_refuses_once` |
| E10 | Read-only tool already in `spec.tools` | still gets an `auto` row + `tool_auto_approved`; the audit trail must show the decision, not the absence of a gate | `test_readonly_request_is_auto_approved_and_audited` |
| E11 | Deciding an already-decided request | `ValueError` -> clean CLI error / REST 400 | `test_deciding_twice_is_refused` |
| E12 | Approving a request whose task is not parked (any status) | grant recorded, `released: false`, status **and `escalation_reason`** untouched | `test_approving_a_request_on_an_unparked_task_leaves_status_alone` |
| E13a | Validator emits a blocking marker and the round ends on **revise** | the row is created mid-round; the park fires at the **next** `run_task` boundary, where the check lives. This is the **only** path on which a validator-authored request parks anything. | `test_a_validator_blocking_request_parks_only_on_the_revision_path` |
| E13b | Validator emits a blocking marker and the round ends on **severe / exhausted / high-risk sign-off** | the task goes `NEEDS_HUMAN` for **that** reason; the row stays `pending` with `parked=0`, so approving it records a grant and does **not** revert the escalation (P8) | `test_approving_a_blocking_request_the_task_did_not_park_on_leaves_the_escalation` |
| E13c | Validator emits a blocking marker and the round ends on **approve** | the task goes `DONE` (releasing graph dependents) and the row stays `pending` in the human queue. **Settled: no treatment.** Nothing auto-decides it — auto-refusing would be a second new decision rule the design excludes, and a machine-made `refused` would destroy a grant a human may legitimately want to place before a `redo`. The queue shows the `task_id`, so a moot request is traceable to a finished task. | `test_a_validator_blocking_request_on_the_approve_path_never_parks` |
| E14 | Task parked while `control='pause'` | release does **not** touch `control` (only `set_control` writes it), so a human's pause survives | `test_release_does_not_clobber_a_pause` |
| E15 | Marker text in `task.output` | kept **verbatim**, never stripped (the `FINDINGS:` precedent) | `test_the_marker_is_never_stripped_from_the_output` |
| E16 | Missing `summarizer` role **and** `gate_declared_tools=True` | the summarizer falls back to the worker's spec and its declared tools are not gated, because `run_summarizer` is outside the enforcement set. Documented residual limitation; not fixed here. | documented in `CLAUDE.md` (Phase 9); no test |
| E17 | A role absent from `DEFAULT_AGENTS` | `baseline_tools(role)` returns `[]` — no shipped baseline, so everything it declares is gated. Fail-safe direction. | `test_baseline_tools_is_derived_from_default_agents` |
| E18 | Task escalated for an **unrelated** reason (exhausted revisions / severe disagreement / budget cap) while a `pending` request sits in the queue | approving it records the grant with `released: false` and leaves `status` and `escalation_reason` byte-identical. The `parked` flag is what makes this hold, not the status (Finding 3). | `test_approving_an_unrelated_request_does_not_revert_an_exhausted_escalation`, `test_approving_a_request_does_not_revert_a_budget_cap_escalation` |
| E19 | Task parked on **two** blocking requests | approving the first records the grant and leaves the task parked (`released: false`); approving the second releases it. Otherwise the release would pay a full worker round and re-park at the next boundary. | `test_a_task_parked_on_two_requests_is_released_only_by_the_last_one` |
| E20 | Task parked, then the human `redo`s it (or `pause`s + `resume`s it) instead of deciding the request | Output/`revision_count` reset (redo only), workspace wiped (redo only), lease released, **and the task's `parked` flags cleared** (Phase 2b). The row itself stays `pending` + `blocking`, so the next run parks again *at the park check* — the blocking need is still unmet, which is the correct answer, and the re-park re-stamps `parked`. Clearing is what stops the four **pre-park-check** escalations (budget cap, `ESCALATE:`, empty output, infra/config error) from being reachable with a stale `parked=1` row, which would let a tool approval blank an unrelated diagnosis. | `test_a_redo_of_a_parked_task_parks_again_until_the_request_is_decided`, `test_a_redo_clears_the_parked_flags`, `test_a_resume_from_paused_clears_the_parked_flags`, `test_a_stale_parked_flag_cannot_release_a_pre_park_escalation` |
| E21 | `gate_declared_tools=True` on a role declaring a gated tool | the row is created with `blocking=False` **unconditionally**, so the tool is withheld and the task **continues**. `tools_for` has no code path that can park a task. | `test_a_declared_source_request_is_never_blocking`, `test_gate_declared_tools_never_parks_a_task` |
| E22 | `config is None` (`eval.py:325`, `run_summarizer`) | no gating, no marker parsing, no rows, no events; `tools` passed through as the caller built it | `test_a_none_config_parses_no_markers_and_gates_nothing` |
| E23 | A custom `task.worker_role` makes `agent_kind != role` | the row records `agent_kind="worker"` and `role="<custom>"` as two distinct fields; `granted_tools` keys on `role` | `test_a_declared_request_records_agent_kind_and_role_separately` |
| E24 | `release_claim` on a task holding no lease | the UPDATE is guarded `WHERE id=? AND claimed_by IS NOT NULL` and logs `claim_released` **only when `rowcount == 1`** — the `memory_read` discipline (record the fact only if the gated UPDATE matched), so a `resume` of an unclaimed task adds no event noise and the log never claims a release that did not happen. Never raises. | `test_release_claim_on_an_unclaimed_task_is_a_silent_no_op` |

---

## ADRs (recorded inline; the next session inherits them)

### ADR-1: The lease release is a store accessor, not a status-write side effect

**Context.** Returning a task to `PENDING` while `claimed_by` is set makes it
permanently unclaimable and starves the claim loop (Finding 1, verified against
the real store). Three call sites want the release (`approve_tool_request`,
`human_redo`, `resume`), and `update_task` deliberately omits the column.

**Decision.** Add `Store.release_claim(task_id: int) -> None`, the sole writer of
`claimed_by = NULL`, called inside the same `transaction()` as the status change.
It **logs a `claim_released` event** in that transaction (ADVISORY 5): the
mirror-image acquire logs `task_claimed` (`store.py:626`), Phase 2's own exit
criterion is that no accessor writes a row without its event, and this is the
accessor every future release path will call — an undocumented exemption here
would be inherited silently. The UPDATE is guarded on `claimed_by IS NOT NULL`
and the event is logged only when `rowcount == 1`, so a release that released
nothing records nothing (E24). Verified green-safe: no existing test asserts a
closed event-kind sequence.

**Rejected alternatives.**
- *Have `update_task` write `claimed_by` from the in-memory `Task`.* Rejected for
  the same reason `update_task` already omits `control`: the loop holds a task
  object loaded at the start of a run, so a stale field would clobber a
  concurrent claim. Symmetric with `set_control`.
- *Add `claimed_by IS NULL` to `_UNBLOCKED`.* Rejected: that hides the stuck row
  from the SELECT rather than releasing it — the task would silently never run
  instead of loudly never running — and `_UNBLOCKED` is about dependency and plan
  state, not ownership.
- *Let `claim_next_task` steal a lease from a `PENDING` row.* Rejected on
  `stranded_claims`' own documented reasoning: a retired claim id is
  indistinguishable from a live second process, and stealing is worse than
  stranding.

**Consequences.** Enables "a `PENDING` task holds no lease" as a store invariant,
testable in one place. Prevents a future release path forgetting the lease,
because there is now one obvious accessor to call.
**Reversibility:** reversible (one method, three call sites).

### ADR-2: Marker parsing is an explicit role **allowlist**, not `kind != "summarizer"`

**Context.** The design forbids parsing the summarizer's output (it compresses a
transcript that may quote a marker verbatim). `_invoke` is shared by all four
roles.

**Decision.** A module constant
`_MARKER_AGENT_KINDS = ("worker", "validator", "planner")` in `agents.py`;
`_invoke` parses only when `kind` is in it.

**Rejected alternative.** `if kind != "summarizer"`. Rejected because a role added
later would get marker parsing **silently**, reintroducing exactly the
manufactured-request-from-a-quotation bug. With an allowlist the failure of
omission is "a new role's genuine request is ignored" -> the tool is withheld ->
the safe direction. Mirrors how `_charter_block` is injected per role and
enumerated by a test rather than applied blindly inside `_invoke`.

**Consequences.** Enables a cheap enumeration test. Prevents silent scope creep to
new roles. **Reversibility:** reversible.

### ADR-3: Grants **add** to the tools list; the gate **removes** from it

**Context.** Acceptance criterion 1 requires an auto-approved read-only request to
reach the *next* invocation's `tools`. A read-only tool is already allowed by
policy rule 1, so an `auto` row would change nothing unless grants can widen the
list beyond `spec.tools`.

**Decision.** `tools_for` returns `[t for t in spec.tools if allowed]` in
`spec.tools` order, then appends every `granted_tools(task_id, role)` entry not
already present, in ascending `tool_requests.id` order.

**Rejected alternative.** *Grants only unlock already-declared tools.* Rejected:
it makes acceptance criterion 1 unsatisfiable and makes "an agent discovers
mid-task that it needs a capability its role was not given" — the slice's stated
purpose — impossible to express.

**Consequences.** Enables the whole marker feature. Prevents nothing that worked
before, because with no rows the list is `spec.tools` unchanged (P1). Forces the
ordering rule to be explicit, since `{kind}_prompt` records `tools`.
**Reversibility:** reversible.

### ADR-4: No `try/except` around the request write inside `_invoke`'s closing transaction

**Context.** Finding 2: a swallowed inner-transaction failure sets `_txn_aborted`,
so the outer block rolls back and raises `TransactionAborted` at the outermost
boundary — discarding an already-paid `finish_attempt`, which `_with_retry` then
re-pays. `CLAUDE.md` names slices 5-6 as the ones that would make this reachable.

**Decision.** Eliminate the raise-sources instead of catching them: parse before
the transaction opens, coerce every value to a bounded `str`/`int`/`bool` before
the call, use conflict-safe SQL, and declare no foreign keys on `tool_requests`.

**Rejected alternative.** *Wrap it defensively.* Rejected — it converts a
one-in-a-million telemetry hiccup into a guaranteed double-charge, which is
strictly worse than the failure it purports to protect against.

**Consequences.** Enables the design's placement (a paid attempt never loses its
requests) at no money risk. Prevents the builder from "hardening" this path with a
`try`, which is why it is an ADR rather than a comment.
**Reversibility:** reversible, but only by moving the write out of the
transaction, which forfeits the design's guarantee.

### ADR-5: "Parked on this request" is a column on `tool_requests`, not a derived guess

**Context.** The design's release fires "if the task is parked on **THIS**
request". Its DDL cannot express that, and the only predicate available without
new state — `status == NEEDS_HUMAN` — also matches six escalations the tool queue
has nothing to do with (exhausted revisions, severe disagreement, budget cap,
high-risk sign-off, worker `ESCALATE:`, empty output, infra/config error). Pass 1
shipped the wide predicate (Finding 3).

**Decision.** `tool_requests` carries `parked INTEGER NOT NULL DEFAULT 0`, set
only by the park, in the park's own transaction, on exactly the rows named in the
escalation reason. The release predicate is `request.parked` **and**
`task.status == NEEDS_HUMAN` **and** no other `pending`+`blocking`+`parked` row
remains.

**Setting it is one place; clearing it is every exit.** A firing release clears the
task's flags — and so do `Loop.human_redo` and `Loop.resume`'s `PAUSED → PENDING`
branch (Phase 2b). Clearing only on release was a real hole, not a tidiness point:
`human_redo` touches no `tool_requests` row, so a park → redo → *pre-park-check*
escalation (budget cap, worker `ESCALATE:`, empty output, infra/config error — all
four fire before the park check's position in `run_task`) leaves the task
`NEEDS_HUMAN` for an unrelated reason with `parked=1` still set, which satisfies all
three release conditions. Approving that stale request then blanks the very
diagnosis the human was being asked to act on. `pause` + `resume` reaches the same
state without a redo. P8's three tests structurally cannot catch it: `severe` and
`exhausted` are only reachable *after* the park check, so a parked row cannot
coexist with them inside one round.

The invariant to hold in mind while reading the code: **`parked=1` means "the loop
is holding this task at `NEEDS_HUMAN` right now, on this row"** — a *live* fact, not
a historical one. The audit trail already records the history; the flag records the
present. Anything that ends that state must therefore clear it.

**Rejected alternatives.**
- *Derive it from `pending_blocking_tool_requests(task_id)` alone* — i.e. "the
  task is `NEEDS_HUMAN` and holds a pending blocking row". Rejected because it is
  still wrong in the case that matters: E13b's validator-authored blocking row
  exists on a task escalated for **severe disagreement**, so approving it would
  blank a severe-disagreement escalation. Narrower than pass 1, still an
  inversion of fail-safe.
- *A `tasks.parked_tool_request_id` column.* Rejected on two counts: it is a new
  column on an **existing** table, so it needs a `_migrate()` entry and becomes
  permanent the moment any database opens; and it holds one id, while a task can
  park on several requests at once (E19), so it would have to be a list in a
  scalar column.
- *Match the escalation reason string.* Rejected outright — a decision rule that
  reads a human-readable message is one wording change away from silently
  failing, in the release direction.
- *Nothing; keep the wide predicate and document it.* Rejected: it contradicts
  the design and inverts "fail safe toward NEEDS_HUMAN", which is the one
  property this project does not trade for simplicity.
- *Clear `parked` centrally inside `Store.set_status` whenever a task moves off
  `NEEDS_HUMAN`.* Rejected, though it is the most complete-sounding option: it puts
  a `tool_requests` write inside the store's generic status writer, which every
  status transition in the system goes through (several per round per task), so one
  feature's bookkeeping would ride every state change in the loop — and `set_status`
  would have to know the difference between a park it should forget and the release
  that is already forgetting it. The two `Loop` methods that end a parked state
  without deciding the request are enumerable (`human_redo`, `resume`'s `PAUSED`
  branch) and both are already being edited in Phase 2b for the lease, so the
  narrow fix costs two lines in the phase that is already touching those lines.

**Consequences.** Enables a narrow, testable predicate and P8's three
escalation-path tests. Prevents a tool decision from ever moving a task the tool
queue did not stop. Costs one column on a brand-new table (no `_migrate()` entry)
and one accessor pair (mark / clear).
**Reversibility:** reversible in code; the column, once shipped, is permanent —
which is exactly why it goes into the DDL in Phase 2 rather than being added
later.

### ADR-6: `config is None` means the slice is absent, not the slice with defaults

**Context.** `agents._invoke` has no `config`, and two of its callers
(`run_summarizer`, `eval.py:325`) have none to give (Finding 5). Something has to
happen when it is absent.

**Decision.** `_invoke(..., config: LoopConfig | None = None)`. `None` means no
gating **and** no marker parsing — the invocation behaves exactly as it does
today. `classify(tool, config)` keeps a **required** config and is never called
with `None`.

**Rejected alternatives.**
- *`classify(tool, None)` falls back to `LoopConfig()`'s defaults.* Rejected:
  it makes a policy decision — what this project considers read-only — out of an
  absent argument, in the one function whose whole job is that judgment. It is
  the "silent degrade to a working default" `retrieval.get_backend` refuses by
  raising, and it would give `eval`'s scratch store a tool queue nobody asked
  for.
- *Make `config` required on `_invoke`.* Rejected: it forces a config into
  `run_summarizer` and `eval`, widening a signature change from three functions
  to five and handing the calibration harness the loop's policy.

**Consequences.** Enables the P1 inertness proof to cover the no-config path
too (E22). Prevents `classify` from ever having to guess.
**Reversibility:** reversible.

### ADR-7: `tools_for` takes the agent **kind** and derives the **role** from the spec

**Context.** `tool_requests.agent_kind` is `NOT NULL` and is a different string
from `role` (`role` is `spec.role`; `agent_kind` is the caller's literal). Pass 1
passed only `role`, so the builder had no `agent_kind` to write and the plausible
repair — passing `role` as `agent_kind` — corrupts the one column that answers
"which agent asked for this" (Finding 4).

**Decision.** `tools_for(store, config, spec, task_id, agent_kind)`, with
`role = spec.role` computed inside.

**Rejected alternatives.**
- *Take both `role` and `agent_kind`.* Rejected: `role` is `spec.role` at every
  call site by construction (that is precisely what `_invoke` is handed), so the
  parameter can only ever hold one correct value — and a parameter that can only
  be right one way is a parameter that can be wrong. Two adjacent `str`
  parameters carrying near-synonyms is the transposition bug waiting to happen.
- *Derive `agent_kind` from `spec.role` too.* Impossible: a custom
  `task.worker_role` makes them differ, and the mapping role → kind does not
  exist anywhere.

**Consequences.** Enables E23's assertion that the two fields stay distinct.
Prevents the corruption pass 1 would have shipped. Deviates from the design's
`tools_for` sketch — disclosed as difference #10.
**Reversibility:** reversible.

---

## Phase Dependency Map

Depends-on is a hard prerequisite; enables is what becomes possible. No phase
forward-references a later one.

| Phase | Depends on | Enables |
| --- | --- | --- |
| 1 — config + types | — | 2 (types to persist), 3 (knobs to read) |
| 2 — table + accessors + `release_claim` | 1 | 3 (`granted_tools`, `tool_request_add`), 2b + 5 (`release_claim`, `pending_blocking_tool_requests`), 7 (`tool_requests`) |
| 2b — repair redo/resume: lease **and** `parked` clear *(D-1 = B, unblocked)* | 2 | Phase 5's `test_a_stale_parked_flag_cannot_release_a_pre_park_escalation` — that one test drives `human_redo` on a parked task, so it needs 2b's clear to have landed. Otherwise a leaf; it can land any time after Phase 2 and must land before Phase 5. |
| 3 — `toolpolicy.py` | 1, 2 | 4a (`tools_for`), 4b (`parse_tool_requests`, `classify`), 6 (both hooks, to neuter) |
| 4a — enforce at the `tools` list | 3 | 4b (the `config` parameter exists), 5 (grants reach a runner), 6 |
| 4b — marker parsing in `_invoke` | 3, 4a | 5 (blocking rows exist to park on), 6, 7 |
| 5 — the decision rule | 4b | 6 (a full park/release cycle to diff), 7 (the two `Loop` methods) |
| 6 — byte-for-byte differential | 5 | 9 (the residual-risk wording it proves) |
| 7 — CLI + REST | 5 | 8 (endpoints to call) |
| 8 — dashboard | 7 | 9 (the surface it documents) |
| 9 — docs | 1-8 (+2b if taken) | — (terminal) |

**Critical path:** 1 → 2 → 3 → 4a → 4b → 5 → 7 → 8 → 9, with 6 hanging off 5 and
2b hanging off 2 — and **2b before 5**, since one Phase 5 test drives a `human_redo`
of a parked task and therefore needs 2b's `parked` clear. **No phase is blocked on a
decision** — D-1 is answered (B), so the build starts at Phase 1 immediately.

---

## Phases

Every phase leaves `.venv\Scripts\python.exe -m pytest -q` green and
`ruff format .` clean. Store/config/policy land before the loop rule that depends
on them; the dashboard lands last; docs last of all.

### Phase 1: Config knobs + domain types

**Objective:** Add the three `LoopConfig` knobs and the three `models.py` types, so
every later phase has a name to import.
**Inputs:** design §Config, §Models; `agentloop/config.py`; `agentloop/models.py`.
**Files/Surfaces:** `agentloop/config.py`, `agentloop/models.py`,
`tests/test_tool_policy.py` (new file).
**Dependencies:** none.
**Allowed Scope:** the three fields, two enums, one dataclass, their comments
explaining *why* (`web` is not read-only because it egresses the prompt; the
allowlist is a project risk judgment; the cap bounds a human's queue), and tests.
**Out-of-Scope Drift:** touching `MODEL_PRICING`; adding a knob the design does not
list; any store or policy code.
**Expected Artifacts:** `tool_readonly_allowlist == ["file_read", "search",
"task_state"]`, `gate_declared_tools is False`, `max_tool_requests_per_task == 10`;
`ToolRequest` with `.status`/`.source` typed as the enums and **both timestamps
typed `float`** (`created_at: float`, `decided_at: float | None`) to match the
`REAL` columns Phase 2 declares — every other timestamp in `_SCHEMA` is
`REAL NOT NULL` written from `time.time()` (`store.py:78, 90, 105, 114, 143, 168,
198, 211`), and `_migrate()` only *adds* columns, so a `TEXT` affinity here could
never be corrected additively afterwards.
**Required Checks:** `test_tool_policy_config_defaults`,
`test_gate_declared_tools_defaults_to_false`,
`test_web_is_not_in_the_readonly_allowlist`,
`test_loopconfig_round_trips_the_new_knobs` (through `to_json` + `load`, which is
where an unknown-key warning would expose a typo),
`test_tool_request_defaults_are_the_safe_ones` (`status` is `PENDING`, `source` is
`MARKER`, `blocking` is `False`, **`parked` is `False`** — every default is the
one that grants nothing and moves no task).
**Validation Level:** Deterministic — `.venv\Scripts\python.exe -m pytest -q tests/test_tool_policy.py`.
**Checkpoint Type:** none (AFK).
**Exit Criteria:** the five tests pass; full suite green; `ruff format .` clean.
**Test Seams:** unit seam (`LoopConfig` dataclass; `models` types).
**Consumes:** none.
**Produces:**
- `LoopConfig.tool_readonly_allowlist: list[str]`
- `LoopConfig.gate_declared_tools: bool`
- `LoopConfig.max_tool_requests_per_task: int`
- `class ToolRequestStatus(str, Enum)` with members `AUTO = "auto"`,
  `PENDING = "pending"`, `APPROVED = "approved"`, `REJECTED = "rejected"`,
  `REFUSED = "refused"`
- `class ToolRequestSource(str, Enum)` with members `MARKER = "marker"`,
  `DECLARED = "declared"`
- `@dataclass class ToolRequest` with fields in this order: `id: int | None`,
  `task_id: int`, `role: str`, `agent_kind: str`, `tool: str`,
  `status: ToolRequestStatus = ToolRequestStatus.PENDING`,
  `source: ToolRequestSource = ToolRequestSource.MARKER`, `reason: str = ""`,
  `blocking: bool = False`, `parked: bool = False`,
  `attempt_id: int | None = None`,
  `decided_by: str = ""`, `decided_note: str = ""`, `created_at: float = 0.0`,
  `decided_at: float | None = None`
  (`parked` is "the loop parked its task on this row" — ADR-5. It sits beside
  `blocking` because the pair is one fact in two tenses: `blocking` is what the
  agent asked for, `parked` is what the loop did about it.)

---

### Phase 2: The `tool_requests` table, its accessors, and `release_claim`

**Objective:** Persist requests as a ledger whose `auto`/`approved` row *is* the
grant, with the pending-only blocking upgrade, plus the lease-release accessor
Finding 1 requires.
**Inputs:** design §Store (as corrected: `REAL` timestamps) and its accessor
table; Findings 1, 2 and 3; ADR-1; ADR-4; ADR-5; `agentloop/store.py`.
**Files/Surfaces:** `agentloop/store.py` (`_SCHEMA`, a `# -- tool requests`
section, `release_claim`, `task_metrics`, `run_metrics`),
`tests/test_tool_policy.py`.
**Dependencies:** Phase 1.

**The DDL, verbatim — build exactly this:**

```sql
CREATE TABLE IF NOT EXISTS tool_requests (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id      INTEGER NOT NULL,
  attempt_id   INTEGER,
  role         TEXT NOT NULL,
  agent_kind   TEXT NOT NULL,
  tool         TEXT NOT NULL,
  reason       TEXT NOT NULL DEFAULT '',
  blocking     INTEGER NOT NULL DEFAULT 0,
  parked       INTEGER NOT NULL DEFAULT 0,
  source       TEXT NOT NULL,   -- 'marker' | 'declared'
  status       TEXT NOT NULL,   -- 'auto'|'pending'|'approved'|'rejected'|'refused'
  decided_by   TEXT NOT NULL DEFAULT '',
  decided_note TEXT NOT NULL DEFAULT '',
  created_at   REAL NOT NULL,
  decided_at   REAL,
  UNIQUE(task_id, role, tool)
);
```

Three points a builder must not "tidy":
- `REAL`, not `TEXT`, on both timestamps. `TEXT` affinity would store
  `'1780000000.123456'`, hand a `str` back through a field annotated `float`,
  make `ORDER BY created_at` lexicographic, and be uncorrectable additively.
- **No `REFERENCES`.** `PRAGMA foreign_keys=ON` is live (`store.py:373`), so an FK
  would be a new raise-source inside `_invoke`'s already-paid closing transaction
  (Finding 2 item 4, ADR-4). Its absence is load-bearing.
- `parked` is in the DDL **from this phase**, not added in Phase 5 — a new table
  gets no `_migrate()` entry, so a database that saw the table without the column
  could never gain it (ADR-5).

**Allowed Scope:** the DDL above; the accessors listed under Produces;
`release_claim` (with its `claim_released` event, ADR-1); `task_metrics` gains
`"tool_requests"`; `run_metrics` gains `"pending_tool_requests"`; tests.
**Out-of-Scope Drift:** a `_migrate()` entry (a whole new table needs none —
`CREATE TABLE IF NOT EXISTS` covers it, and adding one teaches the wrong pattern);
a second `tool_grants` table; adding foreign keys; touching `update_task` or
`_UNBLOCKED`; calling `release_claim` or the `parked` accessors from anywhere yet
(Phases 2b and 5 do that); letting `tool_request_add` write `parked` (only the
park does).
**Expected Artifacts:** each accessor pairs its row change with its event inside one
`self.transaction()` — **including `release_claim`**, which logs `claim_released`
(ADR-1), so the phase has no exemption to its own exit criterion;
`tool_request_add` returns `None` when nothing was inserted (already decided, or
cap reached); the cap refuses **once** by leaning on the UNIQUE row for repeats;
every enum reaching SQL or JSON does so as `.value`.
**Required Checks:** `test_a_grant_is_an_auto_or_approved_row`,
`test_tool_request_add_pairs_the_row_with_its_event`,
`test_a_pending_row_upgrades_from_optional_to_blocking`,
`test_a_decided_row_is_never_upgraded`,
`test_an_unknown_logical_tool_is_refused` (store half: a `refused` row is never
returned by `granted_tools`), `test_the_per_task_cap_refuses_once`,
`test_granted_tools_is_scoped_to_task_and_role`,
`test_pending_blocking_tool_requests_ignores_optional_and_decided_rows`,
`test_pending_blocking_tool_requests_parked_only_filters_unparked_rows` (the
accessor half of ADR-5's predicate),
`test_marking_and_clearing_parked_is_scoped_to_the_named_rows` (mark two of three
rows; assert the third stays 0; clear and assert all three are 0),
`test_tool_request_add_never_writes_parked` (a fresh row is always `parked=0`,
whatever its status or blocking flag),
`test_deciding_twice_is_refused`,
`test_release_claim_clears_the_lease_and_the_task_is_claimable`,
`test_release_claim_logs_claim_released`,
`test_release_claim_on_an_unclaimed_task_is_a_silent_no_op` (E24: no event, no
raise),
`test_tool_request_timestamps_are_real_not_text` (`PRAGMA
table_info(tool_requests)` reports `REAL` for `created_at` and `decided_at`, and a
round-tripped row hands back `float`s — the affinity error `_migrate()` could
never repair),
`test_task_metrics_exposes_tool_requests`,
`test_run_metrics_counts_pending_tool_requests`,
`test_a_new_table_needs_no_migrate_entry` (drop the table on a populated db, reopen
the `Store`, assert it is recreated and the accessors work).
**Validation Level:** Deterministic — `pytest -q tests/test_tool_policy.py`.
**Checkpoint Type:** none (AFK).
**Exit Criteria:** all 19 tests pass; full suite green; `ruff format .` clean; **no
accessor writes a row without its event in the same `transaction()`, with no
exemption** — `release_claim` included.
**Test Seams:** integration seam (`Store` accessors against a real temp-file SQLite
db — the `tests/test_store_atomicity.py` register).
**Consumes:** `ToolRequestStatus`, `ToolRequestSource`, `ToolRequest`,
`LoopConfig.max_tool_requests_per_task`.
**Produces:**
- `tool_requests` table, DDL exactly as quoted above (`REAL` timestamps, a
  `parked` column, no foreign keys)
- `Store.tool_request_add(self, task_id: int, role: str, agent_kind: str, tool: str, status: str, source: str, reason: str = "", blocking: bool = False, attempt_id: int | None = None, why: str = "", max_per_task: int | None = None) -> int | None`
- `Store.tool_requests(self, task_id: int | None = None, status: str | None = None) -> list[ToolRequest]`
- `Store.tool_request_get(self, request_id: int) -> ToolRequest | None`
- `Store.tool_request_decide(self, request_id: int, approved: bool, by: str = "human", note: str = "", released: bool = False) -> ToolRequest`
- `Store.granted_tools(self, task_id: int, role: str) -> list[str]`
- `Store.pending_blocking_tool_requests(self, task_id: int, parked_only: bool = False) -> list[ToolRequest]`
- `Store.tool_requests_mark_parked(self, task_id: int, request_ids: list[int]) -> None`
- `Store.tool_requests_clear_parked(self, task_id: int) -> None`
- `Store.release_claim(self, task_id: int) -> None`
- event kinds `"tool_requested"`, `"tool_auto_approved"`,
  `"tool_request_refused"`, `"tool_request_decided"`, `"claim_released"`
  (five, not the design's four: `claim_released` is ADR-1/ADVISORY 5, the audit
  event for the lease write, and is documented as such in Phase 9)
- `task_metrics(...)["tool_requests"]: list[dict]`
- `run_metrics()["pending_tool_requests"]: int`

**Note on the two `parked` accessors and the row+event rule.** Neither logs an
event of its own: they are called *inside* the park's and the release's
transactions, whose event is the `status:needs_human` / `status:pending` that
`set_status` already writes. That is the design's explicit "no new event for the
park" rule (a second event would double-count escalations), and the pairing still
holds — the row change and an event share one commit.

`release_claim` is the opposite case and **does** log, on ADR-1's own two grounds,
neither of which depends on where it is called from: it is the mirror image of the
acquire, which logs `task_claimed` (`store.py:626`) — a lease taken is audited, so a
lease dropped must be too; and it is the **sole writer** of `claimed_by = NULL`,
called from three sites, so making each caller remember to pair an event is exactly
the exemption-by-inheritance Phase 2's exit criterion refuses. (An earlier draft
justified it by claiming `resume` may call it without a status event. That reasoning
is withdrawn — per BLOCKING 2, the call belongs *inside* `resume`'s `PAUSED` branch,
which does write a status. The event stands on the two grounds above.)

---

### Phase 2b: Repair the two pre-existing release paths — **IN SCOPE (D-1 = B)**

**Objective:** Make `Loop.human_redo` and `Loop.resume` release the lease — so
`README.md`'s existing promise that `agentloop redo <id>` recovers a stranded claim
becomes true — **and clear the task's `parked` flags**, so ending a parked state by
redo/resume cannot leave a stale flag behind (ADR-5). Two one-line additions per
method, at the placements specified below.
**Inputs:** Finding 1; ADR-1; ADR-5 (the clearing rule and its rejected central
alternative); D-1's recorded answer (option B); `agentloop/loop.py:784-807, 877-894`;
`store.py:565-571, 602-611, 640-645` (why `resume`'s placement matters);
`tests/test_control.py:135-137`.
**Files/Surfaces:** `agentloop/loop.py` (`human_redo`, `resume`),
`tests/test_loop.py`, `tests/test_control.py`.
**Dependencies:** Phase 2.

**Exact placement — both calls, and `resume`'s is inside the branch:**

```python
def human_redo(self, task_id: int, note: str = "") -> Task:   # signature unchanged
    ...
    clear_workspace(self.config.workspace_root, task_id)      # unchanged
    self.store.release_claim(task_id)                          # NEW
    self.store.tool_requests_clear_parked(task_id)             # NEW (ADR-5)
    self.store.update_task(task)
    self.store.set_status(task, TaskStatus.PENDING, reason="")

def resume(self, task_id: int) -> Task:      # signature unchanged (loop.py:797)
    ...
    self.store.set_control(task_id, "run")
    if task.status == TaskStatus.PAUSED:          # <-- BOTH calls go INSIDE this
        self.store.release_claim(task_id)         # NEW
        self.store.tool_requests_clear_parked(task_id)   # NEW (ADR-5)
        self.store.set_status(task, TaskStatus.PENDING, reason="")
```

**Why `resume`'s placement is load-bearing, not stylistic.** `resume` accepts **any**
non-terminal task (`loop.py:797-807`), including `in_progress`/`testing`/
`validating`/`revising` rows a **live worker owns** — `tests/test_control.py:135-137`
documents that the API accepts these regardless of what the dashboard hides.
Stripping that lease would make the row match **neither** disjunct of the claim
SELECT (`store.py:602-611`: `status='pending'` is false, and `t.claimed_by=?` cannot
match `NULL`) *and* invisible to `stranded_claims` (`store.py:640-645` requires
`claimed_by IS NOT NULL`), while `next_pending_task` still reports it
(`store.py:565-571`) — so `agentloop status` would show actionable work the loop can
never hand out. That is the exact "looks runnable, never runs" failure class ADR-1
exists to remove, reintroduced in a new shape by its own fix. The `PAUSED` branch is
the only one that produces the `PENDING` state whose invariant Phase 9's
decision-rule statement #5 asserts, so it is the only branch that may release.

**Allowed Scope:** the two `release_claim` calls and the two
`tool_requests_clear_parked` calls at the placements above (four lines, two methods);
five regression tests.
**Out-of-Scope Drift:** any other change to redo/resume semantics (output,
`revision_count`, workspace wipe and control-clear all stay exactly as they are);
`release_claim` or `tool_requests_clear_parked` **outside** `resume`'s `PAUSED`
branch; touching `abort`/`pause`; making `resume` call `set_status` on a
non-`PAUSED` task; clearing `parked` centrally in `Store.set_status` (ADR-5's
rejected alternative); deleting the `tool_requests` **row** rather than its flag —
the row staying `pending`+`blocking` is what makes E20's re-park correct.
**Expected Artifacts:** a redone task and a resumed-from-`PAUSED` task are both
claimable and neither starves a second pending task; a resumed **in-flight** task
keeps its lease; both methods leave `parked=0` on every row of the task.
**Required Checks:** `test_a_redone_task_is_claimable_again`,
`test_a_resumed_task_is_claimable_again`,
`test_a_stuck_lease_does_not_starve_other_pending_tasks` (two tasks; release the
low-id one; assert `claim_next_task` returns a task rather than `None` — the
control case proving the starvation was real and is fixed),
`test_resuming_a_live_in_flight_task_keeps_its_lease` (claim a task so it is
`in_progress` with `claimed_by` set, call `resume`, and assert `claimed_by` is
**unchanged** and `claim_next_task` with that same worker id still returns it — the
non-vacuity control for the branch placement above; without it the placement is a
comment),
`test_a_redo_clears_the_parked_flags` + `test_a_resume_from_paused_clears_the_parked_flags`
(set the flag directly with Phase 2's `tool_requests_mark_parked` — available here,
since the park itself does not exist until Phase 5 — then assert both methods clear
it while leaving `status='pending'` and `blocking=1` on the row).
**Validation Level:** Deterministic — `pytest -q tests/test_loop.py tests/test_control.py tests/test_tool_policy.py`.
**Checkpoint Type:** none (AFK) — D-1 is answered, so there is nothing to wait on.
**Exit Criteria:** six tests pass; full suite green; `ruff format .` clean; a read of
`resume` shows both new calls inside the `if task.status == TaskStatus.PAUSED:`
block. (The *end-to-end* proof that clearing closes the stale-flag hole lives in
Phase 5 — `test_a_stale_parked_flag_cannot_release_a_pre_park_escalation` — because
it needs a real park, which does not exist until then.)
**Test Seams:** e2e seam (`Loop` + `Store.claim_next_task`); integration seam for the
two flag tests (`Loop` + the Phase 2 accessors).
**Consumes:** `Store.release_claim(self, task_id: int) -> None`,
`Store.tool_requests_clear_parked(self, task_id: int) -> None`,
`Store.tool_requests_mark_parked(self, task_id: int, request_ids: list[int]) -> None`
(test-side, to create the state the fix clears).
**Produces:** none (behavioral fix only).

---

### Phase 3: `agentloop/toolpolicy.py` — the policy seam

**Objective:** One small interface holding all classification, marker parsing,
baseline derivation and the single enforcement call — unit-testable with no runner
and, for three of the four functions, no store.
**Inputs:** design §Architecture/new module, §Marker grammar, §Policy resolution,
§Significance (third bullet: a `declared`-source request is **always
non-blocking**); ADR-2; ADR-3; ADR-6; ADR-7; the Behavior Contract above;
`agentloop/retrieval.py` as the register to match.
**Files/Surfaces:** `agentloop/toolpolicy.py` (new), `tests/test_tool_policy.py`.
**Dependencies:** Phase 1, Phase 2.
**Allowed Scope:** exactly the four documented functions plus `ToolClass` and
`ParsedToolRequest`; the marker regex and its bounds as module constants; the
policy-resolution order below, first match wins; the ordering rule from ADR-3;
`role = spec.role` derived inside `tools_for` (ADR-7).

**The resolution order `tools_for` applies to each declared tool** — the design's four
rules with the baseline step written out, because the baseline is *derived* in the
design's prose but never appears as a numbered rule, and P2 quotes this order:

1. `tool ∈ config.tool_readonly_allowlist` → **allowed** (no row, no event).
2. `tool ∈ baseline_tools(spec.role)` → **allowed** (no row, no event). This is the
   step that keeps `gate_declared_tools=True` from gutting the shipped worker, whose
   `DEFAULT_AGENTS` list carries `file_io` and `git` (`registry.py:116`) — neither of
   which is read-only, and neither of which a fresh install has a grant for.
3. An `auto`/`approved` row exists for `(task_id, spec.role, tool)` → **allowed**.
4. `tool ∉ runner.LOGICAL_TOOL_MAP` → **refused** (`tool_request_refused`).
5. Otherwise → **gated**: withheld from this invocation, `pending` row created with
   `blocking=False`, `tool_requested` event.

Steps 2-5 are reached only when `gate_declared_tools` is `True`; with it `False`
every declared tool passes through untouched, which is what makes the inertness
guarantee structural. The baseline step is **not** consulted by `classify`, which
stays a pure allowlist/`LOGICAL_TOOL_MAP` judgment on a *marker*-named tool: a marker
for a baseline tool queues a row and the tool reaches the runner anyway via step 2 —
audited, harmless, and not a grant.
**Out-of-Scope Drift:** a fifth public function; changing `LOGICAL_TOOL_MAP`;
importing `loop.py` or `agents.py` (would cycle); any status write; writing
`parked`; **passing anything but the literal `False` for `blocking`** on the
declared path; giving `classify` an optional `config` (ADR-6); caching.
**Expected Artifacts:** `parse_tool_requests` is total (an internal
`except Exception: return []`, safe precisely because the function touches no store
and no transaction); `tools_for` returns `spec.tools`' content *and order* when
nothing is gated and nothing granted, derives `role` from `spec.role`, and calls
`tool_request_add(..., blocking=False)` unconditionally; `baseline_tools` reads
`registry.DEFAULT_AGENTS` at call time rather than a transcribed copy.
**Required Checks:** `test_classify_readonly_is_auto`,
`test_classify_side_effecting_is_gated`,
`test_classify_unknown_logical_name_is_unknown`,
`test_parse_marker_variants` (flag present/absent/mixed case; four separator
forms; empty reason; label case-insensitive; line-anchored and multiline),
`test_malformed_markers_are_ignored`, `test_a_missing_flag_means_optional`,
`test_a_long_reason_is_bounded`,
`test_duplicates_collapse_and_blocking_beats_optional`,
`test_parse_tool_requests_is_total` (~20 hostile inputs — empty, 100 KB, only the
label, NUL bytes, a marker inside a fenced block — every one returns a list and
none raises), `test_baseline_tools_is_derived_from_default_agents` (equality with
`registry.DEFAULT_AGENTS["worker"].tools`, so a registry edit moves the baseline
with it; plus an unknown role -> `[]`),
`test_tools_for_returns_spec_tools_unchanged_by_default` (**content and order**,
and asserts zero rows and zero events written),
`test_tools_for_appends_granted_tools_in_request_order`,
`test_tools_for_never_returns_an_unknown_logical_tool`,
`test_gated_declared_tool_is_withheld_and_queued`,
`test_the_shipped_baseline_is_never_gated`,
`test_a_declared_source_request_is_never_blocking` (E21/P9, unit half: every row
`tools_for` writes has `blocking is False`, including when the same tool already
has a `blocking` marker row from another role — the flag is never copied),
`test_a_declared_request_records_agent_kind_and_role_separately` (E23: a spec whose
`role` is `"custom-worker"` invoked with `agent_kind="worker"` stores both, and
`granted_tools(task_id, "custom-worker")` — not `"worker"` — finds the grant).
**Validation Level:** Deterministic — `pytest -q tests/test_tool_policy.py`.
**Checkpoint Type:** none (AFK).
**Exit Criteria:** all 17 tests pass; full suite green; `ruff format .` clean;
`toolpolicy.py` imports nothing from `agents.py` or `loop.py`; `classify`'s
`config` parameter has no default.
**Test Seams:** unit seam for `classify` / `parse_tool_requests` /
`baseline_tools`; integration seam for `tools_for` (real `Store`, real `AgentSpec`,
no runner).
**Consumes:** `Store.granted_tools(self, task_id: int, role: str) -> list[str]`,
`Store.tool_request_add(...)` (full Phase 2 signature),
`LoopConfig.tool_readonly_allowlist`, `LoopConfig.gate_declared_tools`,
`LoopConfig.max_tool_requests_per_task`, `ToolRequestStatus`, `ToolRequestSource`.
**Produces:**
- `class ToolClass(str, Enum)` with members `AUTO = "auto"`, `GATED = "gated"`,
  `UNKNOWN = "unknown"`
- `@dataclass(frozen=True) class ParsedToolRequest` with fields `tool: str`,
  `blocking: bool`, `reason: str`
- `def classify(tool: str, config: LoopConfig) -> ToolClass`
  (`config` is **required** — no default, never `None`; ADR-6)
- `def parse_tool_requests(text: str) -> list[ParsedToolRequest]`
- `def baseline_tools(role: str) -> list[str]`
- `def tools_for(store: Store, config: LoopConfig, spec: AgentSpec, task_id: int | None, agent_kind: str) -> list[str]`
  (**`agent_kind`, not `role`** — the role is `spec.role`, derived inside; ADR-7,
  which is also why there is no `role` parameter to transpose it with)

---

### Phase 4a: Enforce at the `tools` list (the declared path)

**Objective:** Route the `tools` argument of every gated agent invocation through
`toolpolicy.tools_for`, so the gate bites before `runner.run` — the only place it
can, since the SDK executes tools *inside* `runner.run()`.
**Inputs:** design §Data Flow; ADR-3; ADR-6; ADR-7; `agentloop/agents.py:389-451,
516-561, 686-725`; `agentloop/agents.py:9-22` (the import style this phase must
follow, because Phase 6's neutering patch depends on it).
**Files/Surfaces:** `agentloop/agents.py` (`run_worker`, `run_validator`,
`run_planner` gain a `config` parameter and compute `tools`), `agentloop/loop.py`
(pass `self.config` at the three call sites), `agentloop/eval.py` (its
`run_validator` call site — **left as it is**: no config, so the slice is inert
there, E22), `tests/test_tool_policy.py`.
**Dependencies:** Phase 3.
**Allowed Scope:** the new parameter, the three `tools_for` calls, updating every
existing caller so the suite stays green, and tests.

**Import style is mandated, not incidental.** `agents.py` must import the seam as
bound names —

```python
from .toolpolicy import tools_for                # this phase; matches agents.py:9-22
# Phase 4b extends the same statement:
# from .toolpolicy import parse_tool_requests, tools_for
```

(Import only what the phase uses — an unused import is a `ruff` F401 and this
phase's exit criterion is a clean tree.)

— because Phase 6 neuters the slice by patching **`agentloop.agents.tools_for`**
and **`agentloop.agents.parse_tool_requests`**. `agents.py` already binds every
import this way (`from .config import estimate_cost_usd`), so a patch on the
`toolpolicy` module object would be inert, the "neutered" run would silently be
the live run, and P1 would pass by comparing a run to itself (ADVISORY 6). If a
later refactor switches to `from . import toolpolicy`, Phase 6's
`monkeypatch.setattr` fails loudly with `AttributeError` rather than passing
vacuously — that is the intended failure mode, and it is why the patch target is
named here in the phase that creates it.

Each of the three functions calls, at the point it builds `tools`:

```python
tools = tools_for(store, config, spec, task.id, "worker")   # / "validator" / "planner"
```

with `tools = spec.tools` untouched when `config is None`.
**Out-of-Scope Drift:** calling `tools_for` from `run_summarizer` (outside the
design's three-role enforcement set; the gap is E16 and is documented in Phase 9);
marker parsing (Phase 4b); moving the computation into `_invoke` — the `run_*`
layer must stay the place a caller can override, which is what lets `eval` drive
one agent with a backend of its own.
**Expected Artifacts:** with default config `runner.calls[i]["tools"]` is identical
in content **and order** to today; with `gate_declared_tools=True` a hand-added
side-effecting declared tool is absent from `tools` and present as a `pending` row.
**Required Checks:** `test_default_config_leaves_every_tools_list_unchanged`
(assert on `MockRunner.calls[*]["tools"]` for worker, validator **and** planner),
`test_gated_declared_tool_is_withheld_from_the_runner_call`,
`test_the_shipped_baseline_reaches_the_runner_under_gating`,
`test_gate_declared_tools_never_parks_a_task` (E21/P9, e2e half: with
`gate_declared_tools=True` and a hand-added gated declared tool, the task reaches
`DONE`, not `NEEDS_HUMAN`, and every row is `blocking=0`, `parked=0` — the knob
withholds and continues, and this is the test that stops a refactor turning it
into an escalation engine),
`test_a_none_config_parses_no_markers_and_gates_nothing` (E22: call `run_validator`
with no `config` on a gated-config store, and assert `tools` is `spec.tools` and
`store.tool_requests()` is empty),
`test_eval_still_runs_after_the_signature_change`.
**Validation Level:** Deterministic — `pytest -q` (the whole suite: this phase
changes three public signatures, so the whole suite *is* the check).
**Checkpoint Type:** none (AFK).
**Exit Criteria:** six new tests pass; the entire pre-existing suite passes with no
change beyond signature-forced call-site edits; `ruff format .` clean; `agents.py`
binds `tools_for` and `parse_tool_requests` as names (grep for
`from .toolpolicy import`).
**Test Seams:** e2e seam (`Loop.run_task` + `MockRunner.calls`) — the seam
`tests/test_loop.py` and `tests/test_charter.py` already use.
**Consumes:** `toolpolicy.tools_for(store: Store, config: LoopConfig, spec: AgentSpec, task_id: int | None, agent_kind: str) -> list[str]`.
**Produces:**
- `agents.run_worker(store: Store, runner: ModelRunner, registry: Registry, task: Task, feedback: str = "", memory: MemoryService | None = None, workspace: str | None = None, test_result: TestResult | None = None, handoff_summary: str | None = None, config: LoopConfig | None = None) -> RunResult`
- `agents.run_validator(store: Store, runner: ModelRunner, registry: Registry, task: Task, worker_output: str, memory: MemoryService | None = None, test_result: TestResult | None = None, config: LoopConfig | None = None) -> tuple[Verdict, int]`
- `agents.run_planner(store: Store, runner: ModelRunner, registry: Registry, plan_task: Task, memory: MemoryService | None = None, config: LoopConfig | None = None) -> RunResult`
  (`config=None` means the slice is **absent** — no gating *and* no marker parsing,
  ADR-6/E22 — keeping `eval` and any other direct caller working and the default
  path byte-identical)
- the import form `from .toolpolicy import tools_for` in `agentloop/agents.py`,
  i.e. the module attribute **`agents.tools_for`** that Phase 6 patches
  (Phase 4b adds `parse_tool_requests` to the same statement)

---

### Phase 4b: Parse markers in `_invoke`'s closing transaction

**Objective:** Turn an agent-authored `TOOL_REQUEST:` line into a row, without ever
putting a paid attempt at risk.
**Inputs:** Finding 2 (all five items); Finding 5; ADR-2; ADR-4; ADR-6; design
§Marker grammar; `agentloop/agents.py:82-278`.
**Files/Surfaces:** `agentloop/agents.py` (`_invoke` gains `config`;
`_MARKER_AGENT_KINDS`, `_MAX_TOOL_REASON_CHARS`, the parse call **before** the
closing transaction and the `tool_request_add` loop **inside** it; the
`from .toolpolicy import` statement gains `parse_tool_requests`),
`tests/test_tool_policy.py`.
**Dependencies:** Phase 3, Phase 4a.
**Allowed Scope:** the `config: LoopConfig | None = None` parameter on `_invoke`
and the three `run_*` functions passing it through; the role allowlist; the
pre-transaction parse; the in-transaction add loop with `classify`-derived status;
coercion of `tool` through `_tool_name_repr` and `reason` through
`_plain_str(value, _MAX_TOOL_REASON_CHARS)`.

**The exact shape (both facts `_invoke` already holds are used as-is):**

```python
# BEFORE the closing `with store.transaction():` — total, pure, unpaid.
parsed = (
    parse_tool_requests(result.output)
    if config is not None and kind in _MARKER_AGENT_KINDS
    else []
)
with store.transaction():
    store.finish_attempt(...)
    ...
    for p in parsed:                       # no try/except anywhere in here (ADR-4)
        cls = classify(p.tool, config)     # config is not None by construction
        store.tool_request_add(
            task.id,
            role=role,                     # _invoke's `role` (= spec.role)
            agent_kind=kind,                # _invoke's `kind` literal — NOT role
            tool=_tool_name_repr(p.tool),
            status=<from cls>,
            source=ToolRequestSource.MARKER.value,
            reason=_plain_str(p.reason, _MAX_TOOL_REASON_CHARS),
            blocking=bool(p.blocking),
            attempt_id=attempt_id,
            max_per_task=config.max_tool_requests_per_task,
        )
```

`role` and `agent_kind` are two distinct arguments carrying two distinct facts
(Finding 4). `parked` is not passed at all — only the park writes it (ADR-5).

**Out-of-Scope Drift:** **any `try`/`except` between the closing
`with store.transaction():` and the `tool_request_add` calls** (ADR-4 — the single
most important prohibition in this plan); stripping the marker from
`result.output`; parsing the summarizer; parsing when `config is None`; passing
`role` as `agent_kind` or vice versa; writing `parked`; moving the write out of the
closing transaction.
**Expected Artifacts:** a read-only marker -> `auto` row + `tool_auto_approved`; a
gated marker -> `pending` row + `tool_requested`; an unknown tool -> `refused` row +
`tool_request_refused`; nothing at all for a summarizer reply.
**Required Checks:** `test_readonly_request_is_auto_approved_and_audited`,
`test_an_auto_row_reaches_the_next_invocations_tools`,
`test_optional_side_effecting_request_is_queued_and_audited`,
`test_optional_request_never_reaches_the_tools_list`,
`test_an_unknown_logical_tool_is_refused` (e2e half),
`test_summarizer_output_quoting_a_marker_creates_no_request` (drives a **real**
context handoff, the `tests/test_charter.py` handoff pattern, so the summarizer is
genuinely invoked rather than merely absent — the control case that makes the rule
non-vacuous), `test_marker_parsing_covers_worker_validator_and_planner` (the role
enumeration ADR-2 buys), `test_the_marker_is_never_stripped_from_the_output`,
`test_a_pathological_marker_never_rolls_back_a_paid_attempt` (4 KB reason,
non-ASCII, braces; assert `task_metrics["attempts"]`, tokens and cost are recorded
and exactly one bounded row exists — P4),
`test_a_decided_request_is_never_reopened_by_a_re_request` (P5),
`test_a_marker_row_records_agent_kind_and_role_separately` (E23, marker half: a
task with a custom `worker_role` stores `agent_kind="worker"` and
`role="<custom>"`; the two columns are asserted to differ, so a builder passing one
for the other fails here rather than in a dashboard six months later),
`test_a_marker_row_is_never_created_parked` (`parked` is 0 on every fresh row —
only Phase 5's park writes it),
`test_blocking_request_is_queued_as_blocking` (the **row** half of Flow C step 1-3:
`blocking=1`, `status='pending'`, `source='marker'`. The park it causes is Phase 5's
— asserting it here would forward-reference a rule that does not exist yet).
**Validation Level:** Deterministic — `pytest -q`.
**Checkpoint Type:** none (AFK).
**Exit Criteria:** thirteen tests pass; full suite green; `ruff format .` clean; a
read of `_invoke` shows no `try` inside its closing transaction and shows `role=`
and `agent_kind=` passed as two distinct arguments.
**Test Seams:** e2e seam (`Loop.run_task` + `MockRunner` + `store.events`).
**Consumes:** `toolpolicy.parse_tool_requests(text: str) -> list[ParsedToolRequest]`,
`toolpolicy.classify(tool: str, config: LoopConfig) -> ToolClass`, `ToolClass`,
`ParsedToolRequest`, `ToolRequestSource`,
`Store.tool_request_add(self, task_id: int, role: str, agent_kind: str, tool: str, status: str, source: str, reason: str = "", blocking: bool = False, attempt_id: int | None = None, why: str = "", max_per_task: int | None = None) -> int | None`,
`agents._tool_name_repr`, `agents._plain_str`,
`LoopConfig.max_tool_requests_per_task`.
**Produces:**
- `agents._invoke(store: Store, runner: ModelRunner, task: Task, kind: str, role: str, model: str, system: str, prompt: str, tools: list[str] | None = None, retrieval: dict | None = None, charter_version: int | None = None, config: LoopConfig | None = None) -> tuple[RunResult, int]`
  (the parameter Finding 5 showed was missing; `None` = the slice is absent)
- `agents._MARKER_AGENT_KINDS: tuple[str, ...] = ("worker", "validator", "planner")`
- `agents._MAX_TOOL_REASON_CHARS: int = 200`
- the extended import `from .toolpolicy import parse_tool_requests, tools_for` in
  `agentloop/agents.py`, i.e. the module attribute
  **`agents.parse_tool_requests`** that Phase 6 patches

---

### Phase 5: The decision rule — blocking park, approve, reject

**Objective:** Add the one new decision rule (a pending blocking request parks the
task at `NEEDS_HUMAN` without spending a revision) and the two human decision
methods, with a release that leaves the task genuinely claimable.
**Inputs:** design §Significance, §Human decision (**as corrected** — read the
narrowed statement of what "context intact" means), §Data Flow; Finding 1 (both
settled details: the lease **and** the stale `escalation_reason`); Finding 3; ADR-1;
ADR-5; `agentloop/loop.py:577-780` and `:847-894`.
**Files/Surfaces:** `agentloop/loop.py` (the check in `run_task` after the output is
stored and before `TESTING`; `approve_tool_request`; `reject_tool_request`; the
module docstring's decision-rule list), `tests/test_tool_policy.py`.
**Dependencies:** Phase 4b, **and Phase 2b** — `test_a_stale_parked_flag_cannot_release_a_pre_park_escalation`
drives a `human_redo` of a parked task, so it needs 2b's `parked` clear in place.

**The park**, in one `self.store.transaction()`. **Order matters, and this
paragraph originally had it backwards** — it prescribed the stamp first, which is
the shape that produced this phase's worst class of defect twice. `set_status`
comes **first** and the stamp is **gated on whether it actually wrote**:

```python
with self.store.transaction():
    if self.store.set_status(task, NEEDS_HUMAN, reason=<names the tools, the
                             request ids and the asking agent>):
        self.store.tool_requests_mark_parked(task.id, [r.id for r in pending])
```

`set_status` is lease-predicated, so it returns `False` when the worker no longer
owns the row. Stamping unconditionally then leaves `parked=1` against a status the
row never took — and the mirror of the same mistake, an unconditional *clear*
beside a predicated `set_status`, is what stranded a task at `NEEDS_HUMAN` with no
release route from `abort`, `human_approve` and `human_reject`. The rule
generalises: **making one half of a paired write conditional opens a hole at every
site whose companion stayed unconditional.**

The `parked` stamp and the status event still share that one commit, which is what
keeps the row+event pairing intact without a second event kind.

**The release predicate (ADR-5) — this is the part pass 1 got wrong, and the
block below is what pass 3 corrected after the code diverged from it.** The
predicate is a **callable handed to `tool_request_decide`** and evaluated *inside*
that method's transaction, *after* its compare-and-swap has flipped this row —
never computed before the write:

```python
req = self.store.tool_request_get(request_id)      # KeyError if None

def release_predicate() -> bool:
    current_req = self.store.tool_request_get(request_id)  # fresh: a concurrent
                                                          # redo clears `parked`
    task = self._require(req.task_id)              # fresh; set_status writes
                                                   # output/revision_count from it
    others = [
        r
        for r in self.store.pending_blocking_tool_requests(req.task_id, parked_only=True)
        if r.id != request_id
    ]
    return (
        bool(current_req and current_req.parked)
        and task.status == TaskStatus.NEEDS_HUMAN
        and task.escalation_reason.startswith(_PARK_REASON_PREFIX)
        and not others
    )
```

Computed *before* the write, every term is a check-then-act the store spends four
accessors refusing: two humans clearing a two-row queue each read the other's row
as still pending, both concluded "another park stands", and both granted —
stranding the task at `NEEDS_HUMAN` with no release route, reproduced with two
`Store` connections. And condition 2 needs the **reason** as well as the status:
four escalation gates fire upstream of the park check and all four leave
`NEEDS_HUMAN` behind, so a status-only test lets a stale `parked` flag blank an
unrelated diagnosis. The positive `_PARK_REASON_PREFIX` test is deliberate — the
rejected alternative, clearing `parked` at each of the four upstream escalations,
is the paired-write shape that has already bitten this slice twice, and a fifth
escalation added later would silently reopen it.

Then, in one `self.store.transaction()`: `tool_request_decide(request_id,
approved=True, by="human", note=note, released=released)` and — **only if
`released`** — `tool_requests_clear_parked(task.id)`, `task.escalation_reason = ""`,
`release_claim(task.id)`, `set_status(task, TaskStatus.PENDING, reason="")`.

When `released` is false, **nothing** touches `status` or `escalation_reason`. That
single condition is what stops a tool decision reverting an exhausted-revisions,
severe-disagreement or budget-cap escalation (P8) — the failure pass 1 shipped.

**Allowed Scope:** the park check and its `parked` stamp; the two methods with the
predicate above; the docstring entry.
**Out-of-Scope Drift:** a new event kind for the park (`set_status` already audits
it with the reason; a second event would let anyone counting escalations
double-count); **releasing on `status == NEEDS_HUMAN` alone, or on a pending
blocking row alone** (both are wider than the design and both revert unrelated
escalations — ADR-5's two rejected derivations); touching `control`; a new release
path on rejection; auto-deciding a request on a terminal task (E13c); counting the
park against `max_revisions`; changing any existing decision rule.
**Expected Artifacts:** parked task at `NEEDS_HUMAN` with a reason naming the
tool(s) and request id(s), the named rows stamped `parked=1`, partial output kept,
`revision_count` unchanged, no validator attempt and no `test_run` row; approval of
a request the task **is** parked on releases and grants; approval of any other
request grants only; rejection records and leaves the task parked.
**Required Checks:** `test_blocking_request_parks_the_task_at_needs_human`,
`test_a_parked_task_keeps_its_partial_output_and_revision_count`,
`test_the_validator_never_runs_on_a_parked_task` (assert
`task_metrics["attempts"] == 1` and `verdicts == []`),
`test_the_park_reason_names_the_tool_and_request_id`,
`test_the_park_stamps_the_rows_it_parked_on` (the named rows are `parked=1`; an
`optional` row on the same task stays `parked=0`),
`test_the_park_logs_no_new_event_kind` (the event-kind sequence contains
`status:needs_human` and no park-specific kind — the "no double-count" rule),
`test_approval_is_recorded_with_released_true`,
`test_the_release_keeps_the_row_and_clears_the_reason` (**what is actually true and
assertable**: `output` is byte-identical to the worker's reply, `revision_count`
unchanged, `escalation_reason == ""`, `claimed_by is None`, every pre-release
`events` row still present with its id, and the task's `parked` flags cleared. It
deliberately does **not** assert "feedback intact" — `Task` has no `feedback` field
and nothing persists validator feedback, so that claim was unassertable; on the
park path there is no feedback to restore because the validator never ran, and the
worker's own partial text is not restored either. That limitation is documented in
Phase 9, not tested as though it were a feature),
`test_a_release_keeps_the_workspace_that_a_redo_wipes` (P10: write a file into the
task workspace, release, assert it is still there; then `human_redo` the same task
and assert it is gone. `clear_workspace` runs only in `human_redo`, `loop.py:891`,
and the contrast is what makes the first half non-vacuous),
`test_approving_a_blocking_request_returns_a_claimable_task` (P6: assert
`store.claim_next_task("loop")` returns the task),
`test_a_grant_applies_to_the_next_invocation`,
`test_rejection_is_audited_and_leaves_the_task_parked`,
`test_approving_a_request_on_an_unparked_task_leaves_status_alone` (E12),
`test_approving_an_unrelated_request_does_not_revert_an_exhausted_escalation`
(P8/E18: drive a task to `Exhausted 3 revisions without approval.` with a stale
`pending` **optional** row in the queue, approve it, and assert `status` is still
`NEEDS_HUMAN`, `escalation_reason` is byte-identical, the event carries
`released: false`, and no `status:pending` event was written),
`test_approving_a_blocking_request_the_task_did_not_park_on_leaves_the_escalation`
(P8/E13b: severe disagreement + a validator-authored `pending`+`blocking` row with
`parked=0`),
`test_approving_a_request_does_not_revert_a_budget_cap_escalation` (P8/E18, budget
path),
`test_a_task_parked_on_two_requests_is_released_only_by_the_last_one` (E19),
`test_release_does_not_clobber_a_pause` (E14),
`test_a_validator_blocking_request_parks_only_on_the_revision_path` (E13a),
`test_a_validator_blocking_request_on_the_approve_path_never_parks` (E13c: task
`DONE`, row still `pending` with `parked=0`, nothing auto-decided),
`test_a_redo_of_a_parked_task_parks_again_until_the_request_is_decided` (E20),
`test_a_stale_parked_flag_cannot_release_a_pre_park_escalation` (**the hole P8's
three tests structurally cannot reach.** Drive: a blocking marker parks the task →
`human_redo` (which now clears the flags, Phase 2b) → the next round escalates at a
gate *upstream of the park check* — script the worker's reply as `""` so
`loop.py:636-665` fires with "Worker returned an empty output …"; a
`max_cost_usd_per_task` set low enough to trip `loop.py:1029-1033` is the equivalent
alternate — → approve the still-`pending` blocking request. Assert `status` is still
`NEEDS_HUMAN`, `escalation_reason` is byte-identical to the empty-output message, the
event carries `released: false`, and no `status:pending` event exists after the
escalation. `severe` and `exhausted` cannot substitute: both are only reachable
*after* the park check, so a parked row cannot coexist with them in one round —
which is precisely why this test is named separately rather than folded into P8),
`test_a_grant_is_scoped_to_one_task_and_one_role` (P3),
`test_the_park_does_not_consume_a_revision` (`max_revisions` untouched, and a
subsequent approved release still has its full revision budget),
`test_optional_side_effecting_request_leaves_the_task_running` (design acceptance
criterion 3, and it can only be non-vacuous **here**: Phase 4b asserts the tool is
withheld, but "the task still completes" is only meaningful once the park exists to
*not* fire. Assert `DONE`, the tool absent from every `tools` list, and the row still
`pending`),
`test_readonly_request_does_not_change_the_outcome` (the same shape for the
auto-approved path: an `auto` row and a `tool_auto_approved` event, and the task
reaches `DONE` exactly as it would with no marker at all).
**Validation Level:** Deterministic — `pytest -q`.
**Checkpoint Type:** none (AFK).
**Exit Criteria:** 26 tests pass; full suite green; `ruff format .` clean; the
`loop.py` module docstring lists the new rule, **states the release predicate's three
conditions, and states that `parked` is cleared by every exit from the parked state**;
a read of `approve_tool_request` shows no status write outside the `released` branch.
**Test Seams:** e2e seam (`Loop.run_task`, `Loop.approve_tool_request`,
`Store.claim_next_task`) plus the filesystem for P10's workspace assertion.
**Consumes:** `Store.pending_blocking_tool_requests(self, task_id: int, parked_only: bool = False) -> list[ToolRequest]`,
`Store.tool_request_decide(self, request_id: int, approved: bool, by: str = "human", note: str = "", released: bool = False) -> ToolRequest`,
`Store.tool_request_get(self, request_id: int) -> ToolRequest | None`,
`Store.tool_requests_mark_parked(self, task_id: int, request_ids: list[int]) -> None`,
`Store.tool_requests_clear_parked(self, task_id: int) -> None`,
`Store.release_claim(self, task_id: int) -> None`,
`ToolRequest.parked`.
**Produces:**
- `Loop.approve_tool_request(self, request_id: int, note: str = "") -> Task`
- `Loop.reject_tool_request(self, request_id: int, note: str = "") -> Task`

---

### Phase 6: The byte-for-byte differential + the `ModelRunner` contract note

**Objective:** Prove acceptance criterion 7 by measurement rather than assertion,
with a control case that proves the harness can detect a difference at all.
**Inputs:** design Doubt Pass #5 and §Residual risk; `tests/test_charter.py:96-136`
(the differential precedent); `tests/test_cross_validator.py:490-545` (the control-
case precedent); `agentloop/agents.py:9-22` (the import style that decides where the
patch must go); P1, P1-control-a and P1-control-b above.
**Files/Surfaces:** `tests/test_tool_policy.py` (a local
`_observable_state(store, runner) -> dict` helper), `agentloop/runner.py`
(`ModelRunner.run`'s docstring gains the contract requirement that an
implementation must honor its `tools` argument).
**Dependencies:** Phase 5.
**Allowed Scope:** the differential harness, its three tests, and the docstring
sentence.
**Out-of-Scope Drift:** changing any runner's behavior; adding a runtime assertion
that a backend honored `tools`; mirrored assertions instead of a state diff;
**patching `agentloop.toolpolicy.*` instead of the consumer's namespace** (see
below).

**Where the neutering patch goes, and why it is not a detail.** `agents.py` binds
imported names directly, so the live call resolves `agents.tools_for`, **not**
`toolpolicy.tools_for`. The patch is therefore:

```python
monkeypatch.setattr(agentloop.agents, "tools_for", lambda store, config, spec, task_id, agent_kind: list(spec.tools))
monkeypatch.setattr(agentloop.agents, "parse_tool_requests", lambda text: [])
```

Patching the `toolpolicy` module object instead would be **inert**: the "neutered"
run would be the live run, and P1 would pass by comparing a run to itself
(ADVISORY 6). P1-control-a cannot catch that, because it varies the *script*, not
the patch — so P1-control-b varies the **patch** on a fixed marker-bearing script
and requires a non-empty diff. This is the project's own non-vacuity rule (the AST
walk that proves itself by also running without its exclusion: 0 hits vs 7) applied
to the slice's flagship proof.

**Expected Artifacts:** `_observable_state` captures, for every task: `status`,
`revision_count`, every verdict column, attempt count, total tokens and cost, the
ordered event-kind sequence, every prompt, and **every `tools` list with its
order**. The marker-free live run and the marker-free neutered run produce equal
dicts; the marker-bearing live and neutered runs do not.
**Required Checks:**
`test_marker_free_default_run_is_byte_for_byte_the_pre_slice_5_run` (P1),
`test_the_differential_harness_detects_a_difference_when_one_exists`
(P1-control-a: the same harness, live both sides, marker-free vs marker-bearing,
reports a non-empty diff — without this, P1 could pass by comparing nothing),
`test_the_neutering_patch_provably_takes_effect` (P1-control-b: live vs neutered on
the **same marker-bearing** script reports a non-empty diff — without this, P1
could pass because the patch never bit),
`test_model_runner_documents_the_tools_contract` (the docstring names honoring
`tools` as a requirement, so the residual risk is recorded where an implementer
reads it).
**Validation Level:** Deterministic — `pytest -q tests/test_tool_policy.py`.
**Checkpoint Type:** none (AFK).
**Exit Criteria:** four tests pass; full suite green; `ruff format .` clean; both
`monkeypatch.setattr` targets are `agentloop.agents` attributes.
**Test Seams:** e2e seam (two `Store`s sharing one `workspace_root`, the
`tests/test_charter.py` construction).
**Consumes:** `agents.tools_for` and `agents.parse_tool_requests` (the module
attributes Phases 4a and 4b produce — the patch targets),
`toolpolicy.tools_for(store: Store, config: LoopConfig, spec: AgentSpec, task_id: int | None, agent_kind: str) -> list[str]`,
`toolpolicy.parse_tool_requests(text: str) -> list[ParsedToolRequest]`.
**Produces:** none.

---

### Phase 7: CLI + REST surface

**Objective:** Make the queue and its decisions reachable without opening the
database: `agentloop tools …`, the REST endpoints, and the config fields that let
the panel explain *why* something is gated.
**Inputs:** design §Surfaces; `agentloop/cli.py:36-63` (`_memory_cmd` as the
model), `agentloop/server.py:209-223` (the `/api/memory` POST shape),
`tests/test_cli.py:15-35`, `tests/test_server.py:23-60`.
**Files/Surfaces:** `agentloop/cli.py` (a `_tools_cmd` + the `tools` subparser with
`list`/`approve`/`reject`), `agentloop/server.py`
(`GET /api/tool_requests[?task_id=&status=]`,
`POST /api/tool_requests/{id}/{approve|reject}` returning the refreshed list, a
`_tool_request_json` converter using `.value`, `/api/config` gains
`tool_readonly_allowlist` and `gate_declared_tools`), `tests/test_cli.py`,
`tests/test_server.py`.
**Dependencies:** Phase 5.
**Allowed Scope:** the subcommand, the two endpoints, the converter, the two config
fields, and tests.
**Out-of-Scope Drift:** a DELETE endpoint; a `--runner` change (this slice adds no
backend, so the four `--runner` `choices` lists stay as they are); auth; any new
mutation not in the design.
**Expected Artifacts:** a bad request id reaches `main`'s existing
`KeyError`/`ValueError` handler as `error: …` on stderr with exit 1 and no
traceback; an already-decided request returns REST 400 through `do_POST`'s existing
`ValueError` branch. `_tool_request_json` serves **every** column —
`task_id`, `role`, `agent_kind`, `tool`, `reason`, `blocking`, **`parked`**,
`source`, `status` (enums as `.value`), `decided_by`, `decided_note`,
`created_at`/`decided_at` as **numbers** — because a column stored but not served
is "stored, exposed nowhere, and looks implemented", the same trap `task_metrics`'
`findings` documents; and `parked` is what tells the panel which request is
actually holding a task.
**Required Checks:** `test_cli_tools_list`, `test_cli_tools_list_filters`
(`--task`, `--pending`), `test_cli_tools_approve`, `test_cli_tools_reject`,
`test_cli_tools_bad_id` (clean error, exit 1, `"Traceback" not in err`),
`test_tool_requests_over_http`, `test_approve_tool_request_over_http`,
`test_reject_tool_request_over_http`,
`test_deciding_twice_over_http_is_a_400`,
`test_config_endpoint_exposes_the_tool_policy_knobs`,
`test_tool_request_json_exposes_every_column` (asserts the served keys against the
`ToolRequest` dataclass's own field names, so a column added later cannot be
silently unexposed, and that both timestamps arrive as JSON numbers),
`test_tool_request_events_reach_the_sse_feed`.
**Validation Level:** Deterministic — `pytest -q tests/test_cli.py tests/test_server.py`.
**Checkpoint Type:** none (AFK).
**Exit Criteria:** 12 tests pass; full suite green; `ruff format .` clean.
**Test Seams:** CLI seam (`cli.main(argv)`); HTTP seam (the `live` fixture's real
server on an ephemeral port).
**Consumes:** `Loop.approve_tool_request(self, request_id: int, note: str = "") -> Task`,
`Loop.reject_tool_request(self, request_id: int, note: str = "") -> Task`,
`Store.tool_requests(self, task_id: int | None = None, status: str | None = None) -> list[ToolRequest]`,
`LoopConfig.tool_readonly_allowlist`, `LoopConfig.gate_declared_tools`.
**Produces:**
- CLI `agentloop tools list [--task ID] [--pending]`,
  `agentloop tools approve ID [--note ...]`, `agentloop tools reject ID [--note ...]`
- `GET /api/tool_requests`, `POST /api/tool_requests/{id}/approve`,
  `POST /api/tool_requests/{id}/reject` (each POST returns
  `{"tool_requests": [...]}`)
- `/api/config` fields `tool_readonly_allowlist`, `gate_declared_tools`

---

### Phase 8: Dashboard

**Objective:** Surface the queue where the approve button already lives, so a
waiting request is visible without opening a task.
**Inputs:** design §Surfaces/Dashboard; `web/src/components/MemoryPanel.tsx`;
`web/src/api.ts`; `web/src/types.ts`; `web/src/App.tsx`;
`web/src/components/StatBar.tsx`.
**Files/Surfaces:** `web/src/types.ts` (`ToolRequest`, `ToolRequestStatus`,
`ToolRequestSource`; `TaskMetrics.tool_requests`;
`RunMetrics.pending_tool_requests`; `LoopConfigView` gains the two knobs),
`web/src/api.ts` (`toolRequests`, `decideToolRequest`),
`web/src/components/ToolRequestPanel.tsx` (new, modeled on `MemoryPanel`),
`web/src/App.tsx` (a `tools` tab with a pending count),
`web/src/components/StatBar.tsx` (an "N tool requests waiting" tile).
**Dependencies:** Phase 7.
**Allowed Scope:** the type mirrors, the two client calls, the panel, the tab, the
tile.
**Out-of-Scope Drift:** restyling other panels; a new state library; the
office-metaphor visualization (roadmap slice 7); mirroring server state into a
second store.
**Expected Artifacts:** `npm run typecheck` and `npm run build` both clean; the
panel lists pending requests with tool, **`task_id`**, role, source, reason,
blocking flag and **parked flag**, and Approve/Reject buttons; the 5 event kinds
already flow through the existing SSE `EventFeed` with no change.

**The `types.ts` mirror must match the DDL exactly**, `REAL` included:

```ts
created_at: number;          // REAL, not a date string
decided_at: number | null;   // REAL, nullable
blocking: boolean;
parked: boolean;             // "the loop parked its task on this row"
agent_kind: string;          // NOT the same field as `role`
role: string;
```

`task_id` and `parked` are rendered because they are what tells an operator whether
a request still matters: a `pending` row on a `DONE` task is moot (E13c), and a
`parked` row is the one holding its task at `NEEDS_HUMAN`.
**Required Checks:** `cd web && npm run typecheck` (exit 0);
`cd web && npm run build` (exit 0). **Manual checklist** (the only manual item in
this plan): (1) start `agentloop serve`; (2) create a task and drive a blocking
request; (3) the Tools tab shows the request with a pending count in the tab label;
(4) the StatBar tile shows 1 waiting; (5) Approve returns the task to `pending` in
the Task board; (6) the event feed shows `tool_requested` and
`tool_request_decided`.
**Validation Level:** Deterministic for typecheck/build; **Manual** for the
6-step checklist above.
**Checkpoint Type:** **human_verify** (the manual checklist).
**Exit Criteria:** typecheck and build exit 0; the 6-step checklist passes;
`types.ts` matches every field `server.py` now serves.
**Test Seams:** compiler seam (`tsc` via `npm run typecheck`) plus the manual
checklist. No new JS test harness — the repo has none, and adding one is out of
scope.
**Consumes:** `GET /api/tool_requests`, `POST /api/tool_requests/{id}/approve`,
`POST /api/tool_requests/{id}/reject`, `task_metrics(...)["tool_requests"]`,
`run_metrics()["pending_tool_requests"]`, `/api/config` fields
`tool_readonly_allowlist`, `gate_declared_tools`.
**Produces:** `web/src/components/ToolRequestPanel.tsx`;
`api.toolRequests`, `api.decideToolRequest`.

---

### Phase 9: Documentation sync

**Objective:** Make `README.md` and `CLAUDE.md` true about the new decision rule,
knobs, table, event kinds, CLI and residual limitations — the project's standing
requirement that a decision rule never changes without both files changing.
**Inputs:** every phase above; design §Decisions/ADR notes and §Doubt Pass
(residual risk); the E16 residual limitation; Findings 1 and 2.
**Files/Surfaces:** `README.md` (a decision-rule table row for the blocking park
**and its three-condition release predicate**; an "Agent-requested tools" section;
the roadmap checkbox; **and — D-1 = B — the stranded-claim paragraph near line 439
becomes true rather than aspirational**), `CLAUDE.md` (current-state paragraph;
`config.py`, `models.py`, `store.py`, `agents.py`, `loop.py`, `toolpolicy.py`,
`cli.py`, `server.py`, `web/` architecture entries; the decision-rules section; the
roadmap item 5).
**Dependencies:** Phases 1-8 (2b included).

**The decision-rule text both files must carry** (five statements, because five
things a future reader could otherwise get wrong):

1. A `pending` + `blocking` tool request parks its task at `NEEDS_HUMAN` with the
   partial output kept, `revision_count` untouched and the validator skipped. Not
   a revision; no new event kind (the `status:needs_human` event is the record).
2. **Release is conditional on all three:** the request is one the loop actually
   parked on (`tool_requests.parked`), the task is still `NEEDS_HUMAN`, and no
   other parked blocking request remains. Anything else records the grant and
   leaves the status alone — a tool decision must never revert an
   exhausted-revisions, severe-disagreement or budget-cap escalation, which is
   what a bare `status == NEEDS_HUMAN` predicate would do.
2b. **`parked` is set only by the park and cleared by *every* exit from the parked
   state** — the release, `human_redo`, and `resume`'s `PAUSED → PENDING` branch. It
   is a live fact ("the loop is holding this task right now, on this row"), not
   history; the audit log keeps the history. Clearing it only on release leaves a
   stale flag that makes the four escalations upstream of the park check
   (budget cap, worker `ESCALATE:`, empty output, infra/config error) revertible by
   approving an old request.
3. A `declared`-source request is **always non-blocking**: `gate_declared_tools`
   withholds and continues, and cannot escalate.
4. A `pending` request left on a task that reached a terminal status stays pending
   and moot. Nothing auto-decides it.
5. A `PENDING` task holds no lease: `Store.release_claim` is the sole writer of
   `claimed_by = NULL`, it logs `claim_released`, and all three release paths call
   it — tool approval, `human_redo`, and `resume` **only on its `PAUSED → PENDING`
   branch**. The qualifier is the rule, not a detail: `resume` also accepts
   in-flight statuses a live worker owns, and releasing there would produce a row
   the claim can never match and `stranded_claims` can never see, while
   `next_pending_task` still reports it.

Plus the **five** event kinds (`tool_requested`, `tool_auto_approved`,
`tool_request_refused`, `tool_request_decided`, `claim_released`) and the three
residual limitations below.
**Allowed Scope:** documentation only.
**Out-of-Scope Drift:** any code change; rewriting unrelated sections; softening the
residual-risk statements.
**Expected Artifacts:** the new decision rule appears in **both** decision tables;
the three knobs, the `tool_requests` table and the five event kinds are documented;
the **three** residual limitations are stated plainly: (a) a backend that ignores
its `tools` argument defeats the gate; (b) a missing `summarizer` role under
`gate_declared_tools=True` leaves that role's declared tools ungated (E16); (c) a
released worker is **not** handed its own partial text back — the workspace
survives, the prompt does not, because `Task` has no `feedback` field and seeding a
synthetic block would add a branch to the one function the byte-for-byte guarantee
is measured on.
**Required Checks:** `pytest -q` still green (docs-only, so this is a regression
guard); a manual read-through confirming: (1) the five decision-rule statements
above appear in `README.md`'s decision table and `CLAUDE.md`'s decision-rules
section; (2) `CLAUDE.md` documents all **five** event kinds by name; (3) all three
residual limitations are stated; (4) the roadmap item is checked in both files;
(5) `README.md`'s stranded-claim paragraph now describes what the code does.
**Validation Level:** **Manual** — the 5-point read-through above, plus
`pytest -q`.
**Checkpoint Type:** **human_verify**.
**Exit Criteria:** the 5-point read-through passes; `pytest -q` green;
`ruff format .` clean.
**Test Seams:** none (documentation). The regression guard is the existing suite.
**Consumes:** every `Produces` above (as documentation subjects).
**Produces:** none.

---

## Risk-Based Testing Matrix

| Risk | Probability | Impact | Test Required |
| --- | --- | --- | --- |
| Release-to-`PENDING` leaves the lease set -> task unclaimable and the queue starves | **high** (it is today's behavior) | **high** | `test_approving_a_blocking_request_returns_a_claimable_task` + `test_release_claim_clears_the_lease_and_the_task_is_claimable` + `test_a_stuck_lease_does_not_starve_other_pending_tasks` |
| **The release predicate is wider than the design, so a tool decision reverts an unrelated fail-safe escalation** (exhausted revisions, severe disagreement, budget cap) | **high** (it is what pass 1 specified, and `status == NEEDS_HUMAN` is the obvious implementation) | **high** (inverts "fail safe toward NEEDS_HUMAN" and re-pays a worker round) | `test_approving_an_unrelated_request_does_not_revert_an_exhausted_escalation` + `test_approving_a_blocking_request_the_task_did_not_park_on_leaves_the_escalation` + `test_approving_a_request_does_not_revert_a_budget_cap_escalation` + `test_the_park_stamps_the_rows_it_parked_on` |
| A release fires while another parked blocking request is unmet -> a paid worker round that re-parks immediately | med | med | `test_a_task_parked_on_two_requests_is_released_only_by_the_last_one` |
| **A stale `parked=1` survives a redo/resume, so a later *pre-park-check* escalation (budget cap, `ESCALATE:`, empty output, infra/config error) is revertible by approving the old request** | **high** (clearing only on release is the natural implementation, and four gates sit upstream of the park check) | **high** (blanks the diagnosis the human was asked to act on; the budget variant also re-pays a round) | `test_a_stale_parked_flag_cannot_release_a_pre_park_escalation` + `test_a_redo_clears_the_parked_flags` + `test_a_resume_from_paused_clears_the_parked_flags` |
| **The `resume` lease fix strips a live worker's lease, making an in-flight task unclaimable *and* invisible to `stranded_claims` while `next_pending_task` still reports it** | **high** (the obvious placement is at the top of the method, and `resume` accepts any non-terminal status) | **high** (the same "looks runnable, never runs" class ADR-1 exists to remove) | `test_resuming_a_live_in_flight_task_keeps_its_lease` |
| `gate_declared_tools=True` parks tasks (a "withhold and continue" knob turned into an escalation engine by a refactor) | med | **high** (every gated role's tasks stop) | `test_a_declared_source_request_is_never_blocking` + `test_gate_declared_tools_never_parks_a_task` |
| `agent_kind` written as `role`, corrupting the only column that says which agent asked | **high** (the field was unreachable in pass 1's signature, so the plausible repair is the wrong one) | med | `test_a_declared_request_records_agent_kind_and_role_separately` + `test_a_marker_row_records_agent_kind_and_role_separately` |
| `TEXT` timestamp affinity, uncorrectable because `_migrate()` only adds columns | med | med | `test_tool_request_timestamps_are_real_not_text` |
| The inertness proof is vacuous because the neutering patch targets the wrong namespace | **high** (`agents.py` binds names directly; patching the module object is the natural first guess) | **high** (the slice's flagship security/inertness proof would prove nothing) | `test_the_neutering_patch_provably_takes_effect` (P1-control-b) |
| Acceptance criterion 5 asserted against a field that does not exist, so the release test passes while proving nothing | med | med | `test_the_release_keeps_the_row_and_clears_the_reason` + `test_a_release_keeps_the_workspace_that_a_redo_wipes` (the redo contrast is the non-vacuity control) |
| A raise inside `_invoke`'s closing transaction rolls back a paid `finish_attempt` and `_with_retry` re-pays | med | **high** (money) | `test_a_pathological_marker_never_rolls_back_a_paid_attempt` + `test_parse_tool_requests_is_total` |
| `INSERT OR IGNORE` silently drops an `optional -> blocking` upgrade, so a task that should park never parks | **high** (the obvious implementation) | **high** | `test_a_pending_row_upgrades_from_optional_to_blocking` + `test_a_decided_row_is_never_upgraded` |
| The gate fails open — a side-effecting tool reaches `runner.run` without a grant | med | **high** (security) | `test_gated_declared_tool_is_withheld_from_the_runner_call` + `test_tools_for_never_returns_an_unknown_logical_tool` |
| The slice is not inert on a default marker-free run (prompt or `tools` order drift) | med | **high** (every existing test's premise) | `test_marker_free_default_run_is_byte_for_byte_the_pre_slice_5_run` + `test_the_differential_harness_detects_a_difference_when_one_exists` |
| Summarizer output manufactures a request from a quoted marker | med | med | `test_summarizer_output_quoting_a_marker_creates_no_request` (with a real handoff) |
| A grant leaks across tasks or roles | med | **high** | `test_a_grant_is_scoped_to_one_task_and_one_role` |
| `escalation_reason` survives the release, so a running task reads as parked | **high** (`set_status` ignores `reason=""`) | med | `test_the_release_keeps_the_row_and_clears_the_reason` |
| Stale in-memory `Task` in the release clobbers `output` / `revision_count` | med | **high** | `test_the_release_keeps_the_row_and_clears_the_reason` |
| A hand-written baseline drifts from `DEFAULT_AGENTS` and withholds a shipped tool | med | med | `test_baseline_tools_is_derived_from_default_agents` + `test_the_shipped_baseline_is_never_gated` |
| A rejected request ping-pongs on every revision round | med | med | `test_a_decided_request_is_never_reopened_by_a_re_request` |
| Cap bypassed, so a human faces an unbounded queue | low | med | `test_the_per_task_cap_refuses_once` |
| The park double-counts as an escalation in the feed | low | low | `test_the_park_logs_no_new_event_kind` |
| `task_metrics` stores `tool_requests` but never exposes them (the "looks implemented" trap) | med | med | `test_task_metrics_exposes_tool_requests` |
| `types.ts` drifts from the server's JSON | med | low | `npm run typecheck` + Phase 8 manual checklist |
| A future backend ignores its `tools` argument, defeating the gate | low | high | Documented residual risk only (`ModelRunner` docstring + README); not testable in-repo — no second executing backend exists |

---

## Verification Strategy (critical_path)

1. **Per phase:** `.venv\Scripts\python.exe -m pytest -q` green and
   `ruff format .` clean before the phase is considered done. No phase may leave the
   suite red "to be fixed by the next phase".
2. **Non-vacuity is mandatory, not optional.** Four checks in this plan carry an
   explicit control case, because the house rule is that a check proves itself by
   also being run in the configuration where it must fail:
   - the differential (P1) is paired with **P1-control-a**, which feeds the same
     harness a marker-bearing script and requires a non-empty diff (proves the
     harness compares something);
   - and with **P1-control-b**, which varies the *patch* rather than the script and
     requires a non-empty diff (proves the neutering took effect — the ADVISORY-6
     hole, invisible to P1-control-a);
   - the summarizer exclusion is paired with a **real** context handoff, so the
     summarizer is genuinely invoked rather than merely absent from the script;
   - the workspace-survives claim (P10) is paired with a `human_redo` on the same
     fixture, so "the file is still there" is proved against something that
     deletes it rather than against nothing.
3. **The money path gets an adversarial input, not a happy one.** P4 drives a 4 KB
   non-ASCII brace-bearing reason through the closing transaction and then asserts
   the attempt's tokens and cost survived.
4. **The security property is asserted at the seam that bites** — `runner.run`'s
   `tools` argument via `MockRunner.calls`, not at `toolpolicy`'s return value
   alone. A unit assertion on `tools_for` would not prove the value reached the
   runner.
5. **Whole-suite regression is the gate for signature changes.** Phase 4a changes
   three public signatures and Phase 4b a fourth (`_invoke`); the exit criterion is
   the entire pre-existing suite passing with no edits beyond the forced call sites.
5b. **The fail-safe direction is tested per escalation path, not in general.** P8
   drives three *different* escalations (exhausted revisions, severe disagreement,
   budget cap) and asserts each survives a tool approval. One generic test would
   pass on whichever path it happened to script — which is exactly how pass 1's
   wide predicate would have shipped green.
6. **Frontend:** `npm run typecheck` + `npm run build`, plus the 6-step manual
   checklist in Phase 8 (the repo has no JS test harness and this slice does not add
   one).
7. **No API keys, no network, nothing skipped** — every test runs on `MockRunner`
   and temp-file SQLite.

---

## Differences from Agreement

| # | Difference | Why | Status |
| --- | --- | --- | --- |
| 1 | The design says "match `human_redo`'s precedent exactly" on the lease. This plan does **not** match it — it clears the lease, which `human_redo` does not do. | The precedent is broken (Finding 1, verified empirically): matching it would ship a task that looks pending and can never run, failing the design's own acceptance criterion 5. The design's second clause ("with a test that proves the released task can be claimed") is the binding one. | **Resolved in the plan.** The scope of the *repair* was D-1, now answered "B": all three release paths use `Store.release_claim` (Phases 2b and 5). |
| 2 | The task brief states `_UNBLOCKED` requires `claimed_by IS NULL`. It does not. | Factual correction: the guard is in `claim_next_task`'s compare-and-swap UPDATE (`store.py:617-622`), not in `_UNBLOCKED` (`store.py:548-555`). This changes where a fix can legitimately go, so it is recorded rather than silently absorbed. | **Corrected in the plan** (Finding 1, ADR-1). |
| 3 | The design implies but does not state that grants **widen** the tools list beyond `spec.tools`. | Without it, acceptance criterion 1 is unsatisfiable, since a read-only tool is already allowed by policy rule 1. Made explicit as ADR-3 with a deterministic append order, because `{kind}_prompt` records `tools` and order is part of the byte-for-byte guarantee. | **Made explicit**, not changed. |
| 4 | The design's `tools_for` sketch takes no `config` at the `run_*` layer; those three functions currently have no `config` parameter. | A signature change is unavoidable to reach the design's own data flow. Defaulted to `config=None` ("no gating") so `eval` and any direct caller keep working and the default path stays byte-identical. | **Mechanical consequence**, recorded. |
| 5 | The design does not say whether `run_summarizer`'s declared tools are gated. This plan leaves them ungated. | Staying inside the design's three-role enforcement set rather than expanding it unilaterally. The resulting gap is narrow (a hand-edited `agents.json` missing the `summarizer` role, with `gate_declared_tools=True`) and is recorded as E16 and documented in `CLAUDE.md`. | **Documented gap**, deliberately not fixed. |
| 6 | `tool_requests` declares no foreign keys, unlike most tables in `_SCHEMA`. | The design's DDL, kept verbatim — and Finding 2 shows why it is load-bearing: with `PRAGMA foreign_keys=ON` live at `store.py:373`, an FK would be a new raise-source inside the already-paid closing transaction. Flagged so a builder does not "fix" it for consistency. | **Design honored**, rationale added. |
| **7** | **Previously undisclosed (this is the pass-1 defect).** The design's release fires "if the task is parked **on THIS request**". Pass 1 stated the release body unconditionally, making the operative predicate `status == NEEDS_HUMAN` — strictly wider — and never recorded the dropped qualifier here. | The qualifier is now honored, and honoring it needs state the design's DDL does not have: `tool_requests` gains a `parked` column, and the release requires `request.parked` **and** `NEEDS_HUMAN` **and** no other parked blocking row (Finding 3, ADR-5). Without it, approving a stale `optional` request silently reverts an exhausted-revisions, severe-disagreement or budget-cap escalation — an inversion of "fail safe toward NEEDS_HUMAN". | **Deviation from the design's DDL, disclosed.** One added column on a **new** table (no `_migrate()` entry), in service of a design rule the design could not otherwise express. Tested by P8's three escalation-path tests + E19. |
| **8** | The design's `tool_requests` DDL originally typed both timestamps `TEXT`. | **The design was corrected to `REAL`** between passes (author's error, not a plan deviation). Propagated here to Phase 1 (`float` fields), Phase 2 (the DDL is quoted inline so there is nothing to re-derive), Phase 8 (`number` / `number | null`), plus `test_tool_request_timestamps_are_real_not_text`. Every other `_SCHEMA` timestamp is `REAL NOT NULL` from `time.time()`, and `_migrate()` only adds columns, so a wrong affinity would be permanent. | **Design corrected upstream**, plan follows. |
| **9** | Acceptance criterion 5 originally read as "output/feedback/`revision_count` intact". | **The design was corrected** to a narrowed promise. `Task` has no `feedback` field, nothing persists validator feedback, and `run_worker` gates its `## Your previous output` block on `feedback` being truthy — so "feedback intact" was unassertable and a builder would have quietly asserted `output`/`revision_count` and called the criterion met. The plan now tests what is true and load-bearing: the row, the audit trail and **the workspace** (`clear_workspace` runs only in `human_redo`, `loop.py:891`), with the redo contrast as the control. The prompt is documented as *not* restored. | **Design corrected upstream**, plan follows; the claim the code cannot support is dropped, not restated. |
| **10** | The design sketches `tools_for(store, config, spec, task_id, role)`. | `tool_requests.agent_kind` is `NOT NULL` and is **not** `role`, so that signature cannot write a valid row (Finding 4). Changed to `tools_for(store, config, spec, task_id, agent_kind)` with `role = spec.role` derived inside — one identity argument instead of two near-synonyms that can be transposed (ADR-7). | **Deviation from the design's sketch, disclosed.** Forced by the design's own DDL. |
| **11** | The design's data flow puts the `classify`-derived status inside `_invoke`'s closing transaction. | `_invoke` has no `config` parameter, and `run_summarizer` / `eval.py:325` have none to supply (Finding 5). `_invoke` gains `config: LoopConfig | None = None`, and `None` means the slice is **absent** — no gating and no marker parsing — so `classify` keeps a required `config` rather than inventing a default risk judgment (ADR-6). | **Mechanical consequence**, recorded, with the `None` semantics specified and tested (E22). |
| **12** | The design lists **four** new event kinds. | This plan ships **five**: `release_claim` logs `claim_released` (ADVISORY 5). The mirror-image acquire logs `task_claimed` (`store.py:626`), Phase 2's own exit criterion forbids an unaudited row write, and this is the accessor every future release path will call — a silent exemption would be inherited. Verified green-safe: no existing test asserts a closed event-kind sequence. | **Addition beyond the design, disclosed**, and documented in Phase 9. |

| **13** | D-1's recorded answer was "one `Store.release_claim` used in the tool-approval release, `human_redo` and `resume`". Phase 2b now adds a **second** call to those same two methods: `tool_requests_clear_parked`. | Forced by this plan's own mechanism, not by the design: `parked` is a live fact, and `human_redo` / `resume` end the parked state without deciding the request, so clearing only on release leaves a stale flag that makes the four pre-park-check escalations revertible (ADR-5). Disclosed because it widens what the human approved under D-1 — from one accessor call per method to two — even though the direction (make a once-parked task genuinely runnable) is the same one D-1 answered. `resume`'s calls are confined to the `PAUSED` branch. | **Widened scope, disclosed.** Four lines across two methods, five tests in Phase 2b plus one end-to-end in Phase 5. |

Nothing else in the approved design was altered, softened, reordered or
reinterpreted. Where the design itself was corrected between passes (#8, #9), the
plan follows the corrected text and says so rather than presenting it as a plan
change. **The second fresh review hunted for a further undisclosed design deviation
and found none**; #13 is the only addition this third pass makes to this table, and
it is a widening of a plan-internal decision rather than of the design.

---

## Plan Completeness Gate — self-check before save

1. Every phase names at least one test that verifies its completion. ✅ (Phase 9 is
   documentation; its guard is the existing suite plus a 4-point read-through.)
2. Every phase lists exact file paths. ✅
3. Every phase has exit criteria stated as commands and observable conditions. ✅
4. Dependencies are explicit phase ids. ✅
5. Out-of-scope drift is named per phase. ✅
6. Consumes/Produces verbatim-matched across phases — checked below. ✅
7. Validation level stated for every phase. ✅
8. Risk matrix complete, with the Probability × Impact mapping honored (every
   high/high and high/med row has a deterministic test; the one low/high row is an
   explicitly untestable residual risk and is documented instead). ✅
9. No placeholders or TBD. ✅
10. Open decisions: **none**. D-1 is answered (option B) and recorded at the top,
    not buried in prose. ✅

### Self-review: cross-phase reference drift (re-run honestly)

Pass 1's self-review certified two breaks as matched, so this pass names **what it
found broken** before listing what closes. A certification that misses a real break
is worse than none.

**Breaks found in pass 1 and fixed in this revision:**

| # | Break | Fix |
| --- | --- | --- |
| B1 | Phase 4b consumed `toolpolicy.classify(tool, config)` inside `_invoke`, but **no phase produced an `_invoke` signature carrying a `config`** — the real one (`agents.py:82-94`) has none, and Phase 4a produced only the three `run_*` signatures. | Phase 4b now **produces** the full `_invoke` signature with `config: LoopConfig | None = None`, and ADR-6 defines `config is None` (no gating, no marker parsing), so `classify` is never called with `None`. |
| B2 | Phase 3 produced `tools_for(..., role)` while Phase 2 produced `tool_request_add(..., agent_kind: str, ...)` writing a `NOT NULL` column `tools_for` had no way to supply. | Phase 3 now produces `tools_for(..., agent_kind: str)` with `role = spec.role` derived inside (ADR-7); Phases 4a and 6 consume the new spelling verbatim. |
| B3 | Phase 5 consumed a release predicate no phase produced state for ("parked on THIS request"). | Phase 1 produces `ToolRequest.parked`; Phase 2 produces the column, `tool_requests_mark_parked`, `tool_requests_clear_parked` and `pending_blocking_tool_requests(..., parked_only=False)`; Phase 5 consumes all four. |
| B4 | Phase 6 consumed `toolpolicy.tools_for` / `parse_tool_requests` as **patch targets**, but the patch has to bind in `agents.py`'s namespace, which no phase produced. | Phase 4a produces the module attribute `agents.tools_for`, Phase 4b `agents.parse_tool_requests`, and Phase 6 consumes those names as its `monkeypatch.setattr` targets. |

**Every `Consumes` matched character-for-character against its `Produces`:**

- `Store.release_claim(self, task_id: int) -> None` — produced Phase 2; consumed
  Phase 5 (the release) and Phase 2b (`human_redo`, and `resume` **inside its
  `PAUSED` branch only** — the third pass's placement fix). ✅
- `Store.tool_request_add(self, task_id: int, role: str, agent_kind: str, tool: str, status: str, source: str, reason: str = "", blocking: bool = False, attempt_id: int | None = None, why: str = "", max_per_task: int | None = None) -> int | None`
  — produced Phase 2; consumed Phases 3, 4b (both now quote it in full). ✅
- `Store.granted_tools(self, task_id: int, role: str) -> list[str]` — produced
  Phase 2; consumed Phase 3. ✅
- `Store.pending_blocking_tool_requests(self, task_id: int, parked_only: bool = False) -> list[ToolRequest]`
  — produced Phase 2; consumed Phase 5 (both spellings now carry `parked_only`). ✅
- `Store.tool_requests_clear_parked(self, task_id: int) -> None` — produced
  Phase 2; consumed Phase 5 (the release) **and Phase 2b** (`human_redo`, and
  `resume`'s `PAUSED` branch — the third pass's two new call sites). ✅
- `Store.tool_requests_mark_parked(self, task_id: int, request_ids: list[int]) -> None`
  — produced Phase 2; consumed Phase 5 (the park) **and Phase 2b test-side**, to
  create the parked state the fix clears without needing Phase 5's park. ✅
- `Store.tool_request_decide(self, request_id: int, approved: bool, by: str = "human", note: str = "", released: bool = False) -> ToolRequest`
  — produced Phase 2; consumed Phase 5. ✅
- `Store.tool_request_get(self, request_id: int) -> ToolRequest | None` — produced
  Phase 2; consumed Phase 5. ✅
- `Store.tool_requests(self, task_id: int | None = None, status: str | None = None) -> list[ToolRequest]`
  — produced Phase 2; consumed Phase 7. ✅
- `ToolRequestStatus`, `ToolRequestSource`, `ToolRequest` (incl. `ToolRequest.parked`)
  — produced Phase 1; consumed Phases 2, 3, 4b, 5. ✅
- `LoopConfig.tool_readonly_allowlist`, `LoopConfig.gate_declared_tools`,
  `LoopConfig.max_tool_requests_per_task` — produced Phase 1; consumed Phases 2, 3,
  4b, 7. ✅
- `toolpolicy.tools_for(store: Store, config: LoopConfig, spec: AgentSpec, task_id: int | None, agent_kind: str) -> list[str]`
  — produced Phase 3; consumed Phases 4a, 6, both quoting the `agent_kind`
  spelling. ✅
- `toolpolicy.parse_tool_requests(text: str) -> list[ParsedToolRequest]` — produced
  Phase 3; consumed Phases 4b, 6. ✅
- `toolpolicy.classify(tool: str, config: LoopConfig) -> ToolClass` (`config`
  required, never `None`) — produced Phase 3; consumed Phase 4b. ✅
- `ToolClass`, `ParsedToolRequest` — produced Phase 3; consumed Phase 4b. ✅
- `agents._invoke(store: Store, runner: ModelRunner, task: Task, kind: str, role: str, model: str, system: str, prompt: str, tools: list[str] | None = None, retrieval: dict | None = None, charter_version: int | None = None, config: LoopConfig | None = None) -> tuple[RunResult, int]`
  — produced Phase 4b; consumed by Phase 4b's own three `run_*` bodies (the only
  callers that pass `config`; `run_summarizer` keeps the default). ✅
- `agents.run_worker(...)`, `agents.run_validator(...)`, `agents.run_planner(...)`
  with `config: LoopConfig | None = None` — produced Phase 4a; consumed by
  `loop.py`'s three call sites in Phase 4a itself and unchanged at `eval.py:325`. ✅
- `agents.tools_for` (Phase 4a) and `agents.parse_tool_requests` (Phase 4b) as
  module attributes — consumed Phase 6 as `monkeypatch.setattr` targets. ✅
- `agents._MARKER_AGENT_KINDS`, `agents._MAX_TOOL_REASON_CHARS` — produced
  Phase 4b; consumed within Phase 4b. ✅
- `agents._tool_name_repr`, `agents._plain_str` — pre-existing
  (`agents.py:209-251`); consumed Phase 4b. ✅
- `Loop.approve_tool_request(self, request_id: int, note: str = "") -> Task`,
  `Loop.reject_tool_request(self, request_id: int, note: str = "") -> Task` —
  produced Phase 5; consumed Phase 7. ✅
- `task_metrics(...)["tool_requests"]`, `run_metrics()["pending_tool_requests"]`,
  `/api/config` fields, the three REST endpoints — produced Phases 2 and 7;
  consumed Phase 8. ✅
- Event kinds `"tool_requested"`, `"tool_auto_approved"`,
  `"tool_request_refused"`, `"tool_request_decided"`, `"claim_released"` — produced
  Phase 2; consumed Phases 2b, 4b, 5, 7, 8, 9. ✅

**Third-pass re-run, over exactly what the narrow edit touched:**

| Touched | Check | Result |
| --- | --- | --- |
| Phase 2b's two new `tool_requests_clear_parked` call sites | Produced by Phase 2, which Phase 2b already depends on. No new dependency edge, no forward reference. | ✅ |
| Phase 2b's test-side `tool_requests_mark_parked` | Same phase-2 origin; lets Phase 2b test the clear without Phase 5's park, so Phase 2b stays a leaf and does not become blocked on Phase 5. | ✅ |
| Phase 2b's widened Allowed Scope / Out-of-Scope Drift | Consistent with ADR-5's rejected central-`set_status` option and with E20's "the row survives, the flag does not". | ✅ |
| Phase 5's `test_a_stale_parked_flag_cannot_release_a_pre_park_escalation` | Consumes only Phase 2b's behavior (which precedes it in the dependency map: 2 → 2b, 2 → 3 → 4a → 4b → 5) and Phase 5's own park. **New ordering constraint recorded:** this test requires Phase 2b to have landed, so Phase 2b is no longer merely a leaf — it is a prerequisite of that one Phase 5 test. The Phase Dependency Map row for 2b is updated accordingly. | ✅ |
| P2's added `baseline_tools(spec.role)` term | `baseline_tools(role: str) -> list[str]` is produced by Phase 3 and now appears as step 2 of Phase 3's written resolution order; the two baseline tests that pin it are already budgeted in Phases 3 and 4a. | ✅ |
| The three newly budgeted tests | `test_optional_side_effecting_request_leaves_the_task_running` and `test_readonly_request_does_not_change_the_outcome` → Phase 5 (needs the park to be non-vacuous); `test_blocking_request_is_queued_as_blocking` → Phase 4b (row only, no forward reference to the park). No flow-map row now names a test no phase budgets. | ✅ |
| Phase 2's restated `claim_released` justification | Rests on `store.py:626` (`task_claimed`) and the sole-writer discipline; no longer on the withdrawn `resume` reasoning. No signature changed. | ✅ |

**Self-review: no cross-phase reference drift remains.** Four breaks (B1-B4) were
found and closed in the second pass; the third pass adds no new signature and closes
one ordering gap (Phase 2b before Phase 5's stale-flag test). Every `Consumes` names
a `Produces` from an earlier or the same phase, in the same spelling.
