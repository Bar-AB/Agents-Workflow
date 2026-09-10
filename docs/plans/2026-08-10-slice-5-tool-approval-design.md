# Slice 5 — Agent-requested tools with an auto-approval policy (design)

Date: 2026-08-10 · Repo: `agentloop` · Status: design complete, ready to plan

## Purpose

Agents may **request** tools at run time. Read-only requests auto-approve;
side-effecting ones queue for human sign-off. Requests and decisions are audited
as `events` rows and surfaced in the CLI and the dashboard.

This mirrors the memory-gating philosophy already in the codebase: agent writes
land unapproved, reads are gated on `approved`, and a human is the only write
path to the gate. A tool request is the same shape applied to capability instead
of to a fact.

## Users

- **The loop / agents** — a worker that discovers mid-task it needs a capability
  its role was not given.
- **The human operator** — reviews the queue (`agentloop tools list`, dashboard
  panel) and grants or denies.
- **The auditor** — reads `events` after the fact and can answer "what did this
  agent ask for, what was it given, and who decided".

## Success Criteria

Each maps to at least one end-to-end test through the `Loop` with a scripted
`MockRunner` (see Testing Strategy).

1. A read-only request is auto-approved, audited, and reaches the **next**
   invocation's `tools` list.
2. A side-effecting request is held for human approval and audited.
3. An `optional` side-effecting request leaves the task running without the tool.
4. A `blocking` side-effecting request parks the task at `NEEDS_HUMAN` with its
   partial output kept and `revision_count` untouched.
5. Human approval of a blocking request returns the task to `pending` with the
   stored row, the audit trail and **the workspace** intact (unlike `human_redo`,
   which wipes the workspace and resets `output`/`revision_count`), and the grant
   applies to the next invocation. The prompt is *not* claimed to be restored —
   see the precise statement under Approach Chosen → Human decision.
6. Human **rejection** of a blocking request records the decision and leaves the
   task parked (no new release path).
7. A marker-free run on default config produces **byte-for-byte** the pre-slice-5
   prompts and `tools` lists, proven by a differential, not by mirrored asserts.
8. Requests and decisions are visible via the CLI and the REST/SSE feed.
9. `pytest -q` green with no API keys or network; `ruff format .` clean;
   `npm run typecheck` + `npm run build` pass.

## Constraints

Inherited from `CLAUDE.md` / `patterns.md`, all binding:

- stdlib-only core, Python ≥ 3.10. No new runtime dependency.
- Schema changes via `_SCHEMA` (+ `_migrate()` only for new **columns**; a whole
  new table needs no entry — `CREATE TABLE IF NOT EXISTS` covers it).
- Every state change and agent I/O gets an append-only `events` row. Never
  UPDATE/DELETE `events`.
- Pair each row change with its audit event inside `Store.transaction()`. Every
  unbatched write goes through `_LockedConnection.write()`.
- **Telemetry must never fail an attempt.** Anything logged is coerced to a
  bounded string first (`_tool_name_repr` / `_plain_str`), because `_invoke`'s
  closing transaction holds an already-paid `finish_attempt`.
- Fail safe toward `NEEDS_HUMAN`; never guess-approve.
- Dataclasses + str-Enums for domain types; type hints everywhere.
- No vendor SDK outside `runner.py`.
- Tests end-to-end through the `Loop` with `MockRunner`; nothing skipped.
- `README.md` + `CLAUDE.md` updated for the new decision rule, knobs and events.
- `web/src/types.ts` mirrors the server's JSON shapes.

## Out of Scope

- **Executing** agent tools. The Claude SDK executes them inside `runner.run()`;
  `OpenAICompatRunner` executes none. agentloop is not gaining an execution loop.
- The SDK's live `can_use_tool` permission callback (would block a paid model
  call waiting on a human).
- Argument-level policy ("Bash, but only `ls`"). A grant is per logical tool.
- Cross-task promotion of grants (the memory `hit_count` analogue). A grant is
  task-scoped, full stop.
- Changing `LOGICAL_TOOL_MAP`'s contents or `OpenAICompatRunner`'s
  tools-dropped-with-a-warning behavior.
- Any change to an existing decision rule other than **adding** the blocking
  tool-request escalation.

## Approach Chosen

### The one hard constraint that shapes everything

agentloop never executes agent tools. The SDK runs them *inside*
`runner.run()`; the OpenAI-compatible backend runs none and reports requests
with `executed: False`. So a gate that fires **after** a `tool_call` lands in the
audit log is too late for the SDK path.

**The only place a gate bites is the `tools` list handed to the runner**:
`run_worker`/`run_validator`/`run_planner` → `agents._invoke(..., tools=…)` →
`runner.run(..., tools)` → `runner.resolve_tools()`. Every enforcement decision
below happens there and nowhere else.

### Two request sources (both approved)

| Source | What it is | Default state |
| --- | --- | --- |
| `marker` | An agent-authored `TOOL_REQUEST:` line in its own reply | Live; inert when no marker appears |
| `declared` | The role's `AgentSpec.tools` list, gated at invoke time | **Off** by default (`gate_declared_tools=False`) |

`declared` is off by default because the built-in `worker` declares `file_io` +
`git`; gating those on a fresh install would gut the shipped worker. Turned on,
a declared side-effecting tool that is **not in the shipped baseline** needs a
grant — where "shipped baseline" is *derived from `registry.DEFAULT_AGENTS`*, not
a hand-copied table in config. Derived on purpose: a hand-written baseline would
be a second source of truth against the registry, and drift there fails silently
in the withholding direction.

### Policy resolution for one (task, role, tool)

Evaluated in order, first match wins:

1. `tool` ∈ `config.tool_readonly_allowlist` → **allowed**, no request row, no
   event. (Read-only is the auto tier; nothing to queue.)
2. An `auto`/`approved` row exists for `(task_id, role, tool)` → **allowed**.
3. `tool` ∉ `runner.LOGICAL_TOOL_MAP` → **refused** (`tool_request_refused`).
   An unknown logical name resolves to no SDK tool today; granting it would
   record a permission that means nothing.
4. Otherwise → **gated**: withheld from this invocation, request row created
   `pending`, `tool_requested` event.

A read-only **request** (a marker naming a tool already in the allowlist) still
creates a row with status `auto` and a `tool_auto_approved` event — the audit
trail must show the auto decision, not just the absence of a gate. That row is
also what makes acceptance criterion 1 checkable.

### Significance (approved)

The marker carries `(blocking)` or `(optional)`:

- `optional` → row queued, task **continues** without the tool.
- `blocking` → row queued, task parks at `NEEDS_HUMAN`
  (reason names the tool(s) and request id(s)), **partial output kept**,
  `revision_count` untouched, validator never runs.
- A `declared`-source request is **always non-blocking**: the agent never asked
  for it, so it cannot have declared it load-bearing.

### Human decision (approved)

- **Approve** → grant recorded; if the task is parked *on this request*, it
  returns to `pending` (**not** a redo).

  **"Context intact" means precisely this, and the promise was overstated on
  first writing.** What survives a release and does *not* survive `human_redo`:
  the task row's `output` and `revision_count`, the audit trail, and — the one
  that actually carries the work — **the workspace**. `human_redo` calls
  `clear_workspace` (`loop.py:891`, its only call site); the release must not, so
  every file the worker already wrote in `.agentloop/ws/task-{id}/` is still there
  for it to read with the tools it now has.

  What does **not** survive, and must not be claimed: the *prompt*. `Task` has no
  `feedback` field, nothing persists validator feedback, and `run_worker` gates
  its `## Your previous output` block on `feedback` being truthy
  (`agents.py:430-435`), so the resumed worker's prompt restates neither. On the
  park path that is *correct for feedback* — the validator never ran, so there is
  no feedback to restore — but it does mean the worker is not handed its own
  partial text back and must re-read the workspace instead. Stated as a
  limitation rather than fixed: seeding a synthetic feedback block would add a
  branch to the one function whose output the byte-for-byte guarantee is measured
  on, to re-say something the workspace already holds.
- **Reject** → decision recorded; the task **stays parked**. The human then uses
  the three choices that already exist (`approve` / `reject` / `redo` the task).
  No new release path, and a denied tool never silently restarts a paid run.
- A decided row is final: an agent re-requesting the same tool on the same task
  hits the UNIQUE row and creates nothing, so denial cannot ping-pong.

## Domain Glossary

| Term | Precise meaning |
| --- | --- |
| **tool request** | One row in `tool_requests`: an ask for one logical tool, for one task, by one role. |
| **grant** | A `tool_requests` row whose status is `auto` or `approved`. There is no separate grant object. |
| **gated tool** | A logical tool not in the read-only allowlist; needs a grant to reach a runner. |
| **read-only allowlist** | `config.tool_readonly_allowlist` — the logical names this project judges safe to auto-approve. |
| **blocking / optional** | Agent-declared significance of a request *for this task*. |
| **source** | `marker` (agent asked in its reply) or `declared` (role's registry list). |
| **refused** | Terminal, machine-made: an unknown logical tool or a request over the per-task cap. Never a human decision. |
| **shipped baseline** | The tools `registry.DEFAULT_AGENTS` gives a role — derived, never transcribed. |

## Architecture

### New module: `agentloop/toolpolicy.py`

A pure policy seam, in the register of `retrieval.py`: small interface, all the
classification behind it, unit-testable without a store or a runner.

```python
def classify(tool: str, config: LoopConfig) -> ToolClass          # auto | gated | unknown
def parse_tool_requests(text: str) -> list[ParsedToolRequest]     # total; never raises
def baseline_tools(role: str) -> list[str]                        # from DEFAULT_AGENTS
def tools_for(store, config, spec, task_id, role) -> list[str]    # the enforcement call
```

`tools_for` is the single enforcement point. **When nothing is gated it returns
`spec.tools` unchanged and in the same order** — the `{kind}_prompt` event records
`tools`, so order is part of the byte-for-byte guarantee, not a detail.

It lives in its own module rather than in `config.py` (which stays declarative),
`agents.py` (whose job is prompts) or `store.py` (whose job is rows).

### Config (`config.py`)

```python
tool_readonly_allowlist: list[str] = field(
    default_factory=lambda: ["file_read", "search", "task_state"]
)
gate_declared_tools: bool = False
max_tool_requests_per_task: int = 10
```

- The allowlist is **config**, not a constant in `runner.py`: what counts as
  read-only is a project's own risk judgment.
- `web` is deliberately **not** read-only. `WebFetch`/`WebSearch` read remotely
  but egress the prompt, and this project scrubs `ANTHROPIC_API_KEY` out of the
  sandbox rather than trusting what runs there — the wire deserves the same
  standard (`_check_base_url`'s reasoning).
- `task_state` maps to no SDK tool at all (served in-process), so listing it
  keeps the allowlist a statement about *logical* names rather than about what
  happens to resolve today.
- `max_tool_requests_per_task` bounds the queue. An agent emitting 500 markers
  must not create 500 rows a human has to clear; over the cap is **refused and
  audited**, never silently dropped.

### Store (`store.py`) — one new table

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
  source       TEXT NOT NULL,   -- 'marker' | 'declared'
  status       TEXT NOT NULL,   -- 'auto'|'pending'|'approved'|'rejected'|'refused'
  decided_by   TEXT NOT NULL DEFAULT '',
  decided_note TEXT NOT NULL DEFAULT '',
  created_at   REAL NOT NULL,
  decided_at   REAL,
  UNIQUE(task_id, role, tool)
);
```

`REAL` timestamps, not the `TEXT` this document first wrote: every other
timestamp column in `_SCHEMA` is `REAL NOT NULL` written from `time.time()`, the
`ToolRequest` dataclass types them `float`, and `web/src/types.ts` mirrors them as
`number`. `TEXT` affinity would silently store `'1780000000.123456'`, hand a `str`
back through a field annotated `float`, make `ORDER BY created_at` lexicographic,
and — because `_migrate()` only *adds* columns — could never be corrected
additively afterwards.

**One table, not two.** A grant *is* an approved request row. A separate
`tool_grants` table would let a grant exist with no request to justify it, and
the ledger would stop being the audit trail.

`UNIQUE(task_id, role, tool)` is the dedupe. It absorbs three real cases at once:
an agent repeating the marker across revision rounds, a validator quoting the
worker's marker in its findings, and a re-request of an already-denied tool.

**Blocking upgrade (do not skip — a plain `INSERT OR IGNORE` is wrong here).**
Round 1 may ask `(optional)` and round 2 `(blocking)`. On conflict:

- existing row `pending` **and** new request blocking **and** row not blocking →
  `UPDATE blocking=1`, log `tool_requested` with `upgraded: true`;
- otherwise → ignore.

Only `pending` rows may be upgraded. An `approved`/`rejected`/`refused` row
carries a decision (human or terminal) that an agent must not reopen.

Accessors (each paired write in one `transaction()`):

| Method | Purpose |
| --- | --- |
| `tool_request_add(...) -> int \| None` | Row + event; `None` when nothing was inserted (already decided, or cap reached). |
| `tool_requests(task_id=None, status=None)` | List for CLI / REST. |
| `tool_request_get(id)` | One row. |
| `tool_request_decide(id, approved, by, note)` | Row + `tool_request_decided` event. |
| `granted_tools(task_id, role) -> list[str]` | Logical names with an `auto`/`approved` row. |
| `pending_blocking_tool_requests(task_id)` | What the loop's escalation check reads. |

`task_metrics` gains `tool_requests`. That `SELECT` is the only verdict/task data
the dashboard gets, and a column missing from it is stored, exposed nowhere, and
looks implemented.

### Event kinds (4 new)

| Kind | When | Payload |
| --- | --- | --- |
| `tool_requested` | A gated request is queued, or an existing pending row is upgraded to blocking | `request_id, attempt_id, agent_kind, role, tool, blocking, source, reason, upgraded` |
| `tool_auto_approved` | A read-only request is auto-granted | same shape, `status: auto` |
| `tool_request_refused` | Unknown logical tool, or over the per-task cap | `+ why` |
| `tool_request_decided` | Human approve/reject | `request_id, approved, by, note, released` |

No new event for the `NEEDS_HUMAN` transition: `set_status` already audits it
with the reason. A second event for the same fact would let anyone counting
escalations double-count.

### Marker grammar

```
TOOL_REQUEST: <tool> (blocking|optional) - <reason>
```

- Line-anchored, `re.MULTILINE`, case-insensitive on both the label and the flag.
- `<tool>` is a bare logical name, `[A-Za-z0-9_.-]{1,40}`.
- The flag is **optional in the grammar; absent means `optional`.** A malformed
  marker must never gain the power to stall a task — the safe direction, in the
  same register as `_extract_findings` degrading to `""`.
- Separator before the reason may be `-`, `—`, `:` or nothing; the reason may be
  empty, and is bounded at 200 chars.
- Within one reply, duplicates collapse and **the strongest wins**: asked twice,
  `blocking` beats `optional`.
- `parse_tool_requests` is **total** — it returns `[]` on anything unexpected and
  never raises. It runs inside `_invoke`'s closing transaction, which holds an
  already-paid `finish_attempt`.
- **The marker is never stripped.** `task.output` and `Verdict.reasoning` keep it
  verbatim — the `FINDINGS:` precedent: added to, never subtracted from. The
  output is what the validator reviews and what dependents consume; a stripped
  copy would be a second, lossy version of what the agent said.
- Parsed for `worker`, `validator` and `planner` — **never the summarizer**. Its
  output is a compression of a transcript that may quote a marker verbatim, so
  parsing it would manufacture a request out of a *quotation*.

### Models (`models.py`)

`ToolRequest` dataclass; `ToolRequestStatus(str, Enum)` and
`ToolRequestSource(str, Enum)` — the project's dataclass + str-Enum convention.

## Data Flow

```
run_worker/run_validator/run_planner
  └─ toolpolicy.tools_for(store, config, spec, task_id, role)
        ├─ readonly allowlist        -> allowed
        ├─ auto/approved row         -> allowed
        ├─ unknown logical name      -> refused (+event)
        └─ else                      -> WITHHELD (+pending row, +event)
  └─ agents._invoke(..., tools=<granted only>)
        └─ runner.run(..., tools) -> resolve_tools() -> SDK allowed_tools
        └─ closing transaction (one commit):
             finish_attempt
           + tool_call events            (slice 2, unchanged)
           + runner_warning              (slice 4, unchanged)
           + parse_tool_requests(output) -> tool_request_add(...) x N
           + {kind}_output

loop.run_task
  ESCALATE: check -> empty-output check -> store output
  └─ pending_blocking_tool_requests(task.id)?
        yes -> set_status(NEEDS_HUMAN, reason naming tools + request ids); return
                (partial output kept; revision_count untouched; no validator run)
        no   -> tests -> validator -> existing verdict path, unchanged

Loop.approve_tool_request(id, note)
  └─ one transaction: decide row
                    + if task parked on THIS request: set_status(PENDING, reason="")
                    + tool_request_decided {released: true}
Loop.reject_tool_request(id, note)
  └─ one transaction: decide row + tool_request_decided {released: false}
```

### Surfaces

- **CLI**: `agentloop tools list [--task ID] [--pending]`,
  `agentloop tools approve ID [--note ...]`, `agentloop tools reject ID [--note ...]`.
  A bad id goes through `main`'s existing `KeyError`/`ValueError` handler → clean
  `error: …` on stderr, exit 1, no traceback.
- **REST**: `GET /api/tool_requests[?task_id=&status=]`,
  `POST /api/tool_requests/{id}/{approve|reject}` returning the refreshed list —
  the exact shape `/api/memory` already uses. Task detail gains its own requests.
  `/api/config` gains `tool_readonly_allowlist` and `gate_declared_tools` so the
  panel can explain why something is gated.
- **Dashboard**: `ToolRequestPanel.tsx` modeled on `MemoryPanel.tsx`, wired into
  `App.tsx`, plus `types.ts` and `api.ts` additions. The 4 event kinds reach the
  event feed over the existing SSE stream for free.

## Error Handling

| Failure | Behavior |
| --- | --- |
| Malformed / partial marker | Ignored; `parse_tool_requests` returns `[]`. Never raises. |
| Missing `(blocking|optional)` flag | Treated as `optional` — never stalls a task on a typo. |
| Unknown logical tool name | Row `refused` + event. Never granted. |
| Over `max_tool_requests_per_task` | Refused + event, once. Never silently dropped. |
| Un-encodable tool name / reason | Coerced by `_tool_name_repr` / `_plain_str` before it reaches `log_event`'s one `json.dumps`. |
| Re-request of a decided tool | UNIQUE row absorbs it; nothing created; no ping-pong. |
| Agent tries to upgrade a decided row | Refused by the pending-only upgrade rule. |
| `approve`/`reject` on a decided request | `ValueError` → clean CLI error / REST 4xx. |
| Approving a request whose task is not parked | Grant recorded; task status untouched. |
| Crash between row and event | Impossible: one `transaction()`. |

**Must-verify integration hazard (call it out in the plan, do not assume):**
`claim_next_task` sets a `claimed_by` lease and `_UNBLOCKED` requires
`claimed_by IS NULL`. Releasing a parked task back to `PENDING` must leave it
genuinely claimable. Read what `human_redo` does about the lease (it also returns
a task to `PENDING`) and match that precedent exactly. Getting this wrong yields
a task that looks pending and can never be claimed again.

## Testing Strategy

New `tests/test_tool_policy.py`, end-to-end through the `Loop` with scripted
`MockRunner` outputs, plus pure-unit coverage of `parse_tool_requests`.

1. Read-only request → `auto` row, `tool_auto_approved` event, tool present in
   the **next** invocation's recorded `tools` (assert on `MockRunner.calls`).
2. Side-effecting `optional` request → `pending` row, `tool_requested` event,
   task completes normally, tool **absent** from the invocation's `tools`.
3. Side-effecting `blocking` request → `NEEDS_HUMAN`, `task.output` non-empty and
   equal to the worker's reply, `revision_count` unchanged, **no validator
   attempt** recorded.
4. `approve_tool_request` → task back to `PENDING` with output/feedback/
   `revision_count` intact, grant applies to the next `tools` list, task then
   completes. Assert the task is actually claimable again (the lease hazard).
5. `reject_tool_request` → decision audited, task **still** `NEEDS_HUMAN`.
6. Unknown tool → `refused`, never granted. Cap exceeded → `refused` once.
7. Blocking upgrade: `(optional)` then `(blocking)` on the same tool → one row,
   `blocking=1`, task parks. And the negative: an already-`rejected` row is not
   upgraded.
8. Summarizer output quoting `TOOL_REQUEST:` creates **no** row — the control
   case that makes the "never parse the summarizer" rule non-vacuous.
9. `gate_declared_tools=True` → a hand-added side-effecting declared tool is
   withheld and queued; the shipped baseline is **not**.
10. **Byte-for-byte differential** (the register `patterns.md` demands): run the
    same scripts with and without markers on default config and diff the full
    observable state — status, `revision_count`, every verdict column, attempt
    count, tokens, the event-kind sequence, and every prompt **and `tools` list**.
11. `tests/test_cli.py`: `tools list/approve/reject`, and a bad id → clean error,
    exit 1, no traceback. `tests/test_server.py`: the GET and both POSTs.

## Observability

- 4 new event kinds, on the SSE feed for free (the `events` table *is* the feed).
- `task_metrics.tool_requests` for task detail.
- `run_metrics` gains a pending-request count so the dashboard `StatBar` can show
  "N tool requests waiting" — the queue is only useful if it is visible without
  opening a task.

## Doubt Pass

**CLAIM:** gating the `tools` list passed to `runner.run()` is sufficient
enforcement, and a single `tool_requests` table with `UNIQUE(task_id, role, tool)`
is sufficient state.

**Findings (all actionable, all folded into the design above):**

1. `INSERT OR IGNORE` on the UNIQUE key silently drops an
   `optional → blocking` upgrade, so a task that should park never parks. →
   pending-only upgrade rule added.
2. Releasing a parked task to `PENDING` interacts with the `claimed_by` lease and
   `_UNBLOCKED`. A release that leaves the lease set produces an unclaimable
   "pending" task. → flagged as a must-verify against `human_redo`'s precedent,
   with an explicit test.
3. Parsing the summarizer's output would manufacture requests from *quoted*
   markers. → summarizer excluded; control-case test added.
4. A hand-written baseline-grants table in config would drift against
   `DEFAULT_AGENTS` and fail in the withholding direction. → baseline derived.
5. `tools` order is recorded in the `{kind}_prompt` event, so "byte-for-byte
   identical" has to include order, not just membership. → stated in `tools_for`'s
   contract and asserted in the differential test.

**Residual risk (documented, not fixed):** enforcement is only as good as the
runner honoring its `tools` argument. `ClaudeSDKRunner` passes it as
`allowed_tools`; `OpenAICompatRunner` drops tools with a warning and has no
execution loop. A future backend that ignores the argument would silently defeat
the gate. This is the same class of residual risk as
`sandbox_isolation='strict'` degrading to env-scrub, and belongs in the
`ModelRunner` protocol docstring as a contract requirement.

## Decisions / ADR notes

| Chosen | Rejected | Why |
| --- | --- | --- |
| Gate the `tools` list before the call | Post-hoc gate on observed `tool_calls` | The SDK already executed them inside `runner.run()`. |
| Gate the `tools` list before the call | SDK live `can_use_tool` callback | Would block a paid model call waiting on a human. |
| One `tool_requests` table; a grant *is* an approved row | A second `tool_grants` table | Two tables let a grant exist with no request behind it, and the ledger stops being the audit trail. |
| Baseline derived from `DEFAULT_AGENTS` | Hand-written baseline grants in config | Second source of truth; drift fails silently in the withholding direction. |
| Task-scoped grants | Role-global standing grants | Everything else here scopes approval to the artifact approved (a value, a plan, a task). A standing grant is already expressible by editing `agents.json` — a deliberate act with a diff. |
| Marker kept in the output verbatim | Strip it before storing | `FINDINGS:` precedent: added to, never subtracted from. |
| `(blocking\|optional)` flag for significance | Reuse `ESCALATE:` | *(User decision.)* `ESCALATE:` fires only when the output **starts** with it, so "partial work, then blocked" had no expression and would burn a revision on a gap only a human can unblock. |
| Rejection leaves the task parked | Rejection returns it to `pending` with denial feedback | *(User decision.)* No new decision rule; the three existing human choices already cover it, and a denial never silently restarts a paid worker run. |
| `gate_declared_tools=False` by default | On by default | The shipped `worker` declares `file_io` + `git`; gating those on a fresh install would gut it. |
| `web` is gated, not read-only | `web` in the read-only allowlist | It reads remotely but egresses the prompt; this project distrusts egress by construction. |

## Questions Resolved

| Question | Answer | Where |
| --- | --- | --- |
| Where do requests come from? | Both: agent marker (live) + declared list (opt-in knob) | User, interview |
| What auto-approves? | The read-only allowlist in config | Slice spec |
| Who decides significance? | The agent, via `(blocking\|optional)` | User, interview |
| What happens on `blocking`? | Park at `NEEDS_HUMAN`, partial output kept, no revision spent | User, interview |
| What does approval do? | Return to `pending` with context intact — not a redo | User, interview |
| What does rejection do? | Records the decision; task stays parked | User, interview |
| Grant scope? | Task-scoped (`UNIQUE(task_id, role, tool)`) | Design, by project analogy |
| One table or two? | One — a grant is an approved row | Design + Doubt Pass |
| Which agents can request? | worker, validator, planner. Never the summarizer | Design + Doubt Pass |

### Brainstorming Handoff (MACHINE-READABLE)

```yaml
DESIGN_FILE: "C:/Coding/Projects/Agents-Workflow/docs/plans/2026-08-10-slice-5-tool-approval-design.md"
DESIGN_SUMMARY: "Agents request tools via a TOOL_REQUEST marker; read-only ones auto-approve and side-effecting ones queue in a single tool_requests table whose approved rows are the grants, enforced at the tools list passed to runner.run(), with a blocking flag that parks the task at NEEDS_HUMAN without spending a revision."
MEMORY_NOTES:
  glossary:
    - term: "tool request"
      meaning: "One row in tool_requests: an ask for one logical tool, for one task, by one role. Its approved/auto status IS the grant - there is no separate grant object."
    - term: "gated tool"
      meaning: "A logical tool outside config.tool_readonly_allowlist; it needs a grant row to reach a runner."
    - term: "blocking vs optional"
      meaning: "Agent-declared significance of a request for this task. blocking parks the task at NEEDS_HUMAN with partial output kept; optional lets it continue without the tool."
    - term: "shipped baseline"
      meaning: "The tools registry.DEFAULT_AGENTS gives a role, derived at runtime rather than transcribed into config, so it cannot drift."
  decisions:
    - decision: "Enforce at the tools list passed to runner.run() (agents._invoke -> resolve_tools)."
      rejected: "A post-hoc gate on observed tool_calls, or the SDK's live can_use_tool callback."
      why: "The SDK executes tools inside runner.run(), so a post-hoc gate is too late; a live callback would block a paid model call waiting on a human."
    - decision: "One tool_requests table with UNIQUE(task_id, role, tool); an approved row IS the grant."
      rejected: "A separate tool_grants table."
      why: "Two tables let a grant exist with no request behind it, and the ledger would stop being the audit trail."
    - decision: "A pending row may be upgraded optional -> blocking; a decided row never may."
      rejected: "Plain INSERT OR IGNORE on the UNIQUE key."
      why: "OR IGNORE silently drops the upgrade, so a task that should park never parks - found by the Doubt Pass."
    - decision: "Never parse the summarizer's output for markers."
      rejected: "Parsing every agent's output uniformly."
      why: "A summary compresses a transcript that may quote a marker verbatim, manufacturing a request out of a quotation."
    - decision: "gate_declared_tools defaults to False; the read-only allowlist lives in config and excludes `web`."
      rejected: "Gating declared tools by default; treating web as read-only."
      why: "The shipped worker declares file_io+git, so gating by default would gut it. web reads remotely but egresses the prompt, and this project distrusts egress by construction."
```
