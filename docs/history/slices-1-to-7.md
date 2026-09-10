# Slices 1-7: how the loop got its current shape

Narrative history of the early roadmap slices — what each added, why, and
what broke along the way — kept here so `CLAUDE.md` doesn't have to carry it
on every session read. See `docs/GUIDE.md` for the current-state rationale
behind these decisions, and `CLAUDE.md`'s Architecture section for what each
module does today.

## Slice 1 — context-budget handoff

Once a worker's accumulated context on a task reaches `context_handoff_ratio`
(default 0.70) of its `AgentSpec.context_budget_tokens`, a dedicated
`summarizer` agent compacts the working state and the worker is restarted
from that summary in place of the raw transcript (a `context_handoff` event).
Not counted as a revision. Enforced in `loop.py` (`_maybe_handoff`), checked
at the iteration boundary alongside the budget/control checks. An in-loop
watermark advances so only newly accumulated context can retrip it. A
hand-edited `agents.json` predating the `summarizer` role degrades
gracefully — `run_summarizer` falls back to the worker's spec rather than
crashing the handoff.

## Slice 2 — relevance retrieval + provenance

Approved memory facts are ranked against the task at hand through a new
`retrieval.py` seam (a stdlib hashed bag-of-words backend, no dependencies)
instead of being selected alphabetically. The audit log gained `retrieval`
and `tool_call` events, each attributed to the attempt and agent they belong
to. `HashingBackend` is the only backend; a `ChromaBackend` + `[rag]` extra
shipped briefly and was removed — with the same `embed()` seam on both sides,
it produced identical ordering at any fact count this store holds, so it was
an optional, CI-untested path buying nothing.

**Design decisions from the original interview, for context on why this
shape:** a `RetrievalBackend` protocol mirroring `ModelRunner`'s seam was
chosen specifically so the core install gains relevance ranking with *no*
dependency at all — Chroma (while it existed) was an index behind the same
protocol, never a second embedder, so tests stayed deterministic with no
model download or network call. Ranking was deliberately kept out of
*inclusion* decisions: there is no `min_score` knob, because relevance
should decide order and the cap should decide what's dropped — a fact that
fits under the cap must never be silently excluded for scoring low. An
empty/whitespace query (or no backend configured) was required to produce
byte-identical output to the pre-slice-2 alphabetical selection with zero
`retrieval` event logged, both to keep every prior memory test passing
unchanged and because that's the honest behavior when there's nothing to
rank against.

A later corrective pass on this slice's memory code found every item in the
original design was a documented rule quietly not holding:
- **Promotion is a transition, not a copy.** `Store.memory_promote` moves the
  row (`UPDATE ... SET tier='loop'`) instead of duplicating it — a copy left
  both rows approved, both injected, `memory_promoted` re-fired on every
  later read, and revoking the original left the copy approved.
- **A hit is a distinct task.** The new `memory_hits` table exists because
  worker + validator + one revision used to promote a fact on the strength
  of a single task (three prompts, one task) — `hit_count` now counts
  distinct `task_id`s, not prompts.
- Plus: a one-transaction, approval-gated `memory_read`; ranking that pads
  candidates a backend didn't return rather than dropping them; tool-call
  telemetry coerced to a bounded string so it can never roll back a paid
  attempt; and `Store.transaction()` depth bookkeeping that decrements on the
  error path too, so it can no longer go negative and silently stop
  committing.

## Slice 3 — planner + task graph + parallel workers

A `planner` agent decomposes a goal into a DAG of tasks (`agentloop plan`),
persisted in the new `task_deps` table alongside `tasks.kind` / `plan_id` /
`plan_approved`. Being blocked (an unfinished dependency, or a plan a human
hasn't signed off) is a *predicate inside the atomic claim*, not a status, so
dependency order holds identically for one worker and for
`max_parallel_workers` of them (default 1 = the original sequential loop,
unchanged).

## Slice 3c — project charter + validator findings

Two independent additions that change no decision rule. The charter is
human-authored, project-wide prose in its own append-only `charter` table
(one row per version, `id` *is* the version), injected as a
`## Project charter` block above the memory block in worker/validator/planner
prompts (not the summarizer), with `attempts.charter_version` recording which
version each invocation ran under. Agents have no write path to it — only the
CLI (`agentloop charter`) and the dashboard do. The validator now also
enumerates what it checked under a soft `FINDINGS:` marker, stored in
`verdicts.findings` — added to `Verdict.reasoning`, never subtracted from it,
since `reasoning` is what the loop feeds back as revision feedback.

The `worker`, `validator` and `planner` registry entries moved to version
"2" for this: the old `VALIDATOR_SYSTEM` prompt said "judge it strictly
against the task's acceptance criteria", which instructs a validator to
*disregard* a charter, so injection alone would have been inert without the
prompt change.

**Two alternatives were rejected before building the charter as its own
table, with reasons worth keeping.** Storing it as ordinary pinned memory
facts (reusing the existing approval/pinning/injection machinery for free)
was rejected because it couldn't actually enforce "human-authored only": an
agent writing to the same key through the existing agent-write path
(`MemoryService.remember`) would silently replace the charter's content
without raising anything — the exact failure the charter exists to prevent,
via the exact mechanism meant to gate it. It also inherited memory's two
silent-truncation limits (a pinned-facts cap, and a per-value character
cap) that would quietly cut a charter mid-sentence — ruled out on the same
"never truncate at inject time" ground the shipped design uses. And under
the default ranked retrieval backend, charter rows would get re-sorted by
relevance to each task's own vocabulary, which is nonsense for a document
whose sentences are meant to read in order. The second alternative — a
config field or file path referencing charter text on disk — was rejected
mainly because it splits run-affecting state across two sources of truth
(the database and the filesystem) when everything else the loop reads lives
in one SQLite file; a charter loaded from a path a human could edit outside
any audit trail undermines the same guarantee the rest of the system is
built around.

## Slice 4 — second-provider cross-validator

The `ModelRunner` seam gained a second provider: `OpenAICompatRunner` POSTs
to any OpenAI-compatible `/v1/chat/completions` endpoint over stdlib
`urllib.request` — no SDK, no runtime dependency. "Second provider" means
"second base_url". Which backend serves a role is a registry decision
(`AgentSpec.runner`, `None` = the loop's default), resolved per role in
`loop._runner_for` — a provider axis, not a new gate: no decision rule reads
it, and an unpinned run is the pre-slice-4 loop exactly.

A follow-up hardening pass found the money paths were the weak ones: usage
parsing became total (a wrong *type* used to raise *after* the completion was
billed, and the retry then paid for it again); the never-zero usage guard
checks each field independently and falls back to `total_tokens`; pricing
normalizes the dated snapshot id providers actually echo
(`gpt-4o-mini-2024-07-18`) to its family, without which every new pricing row
was unreachable on real traffic; a permanent provider failure (missing key,
400/401/403/404) became a `RunnerConfigError` that escalates as a config
error carrying the provider's own body, instead of three paid retries
reported as `infra_error`; a `claude-*` model pinned to the OpenAI backend is
refused before the call; the bearer token cannot follow a redirect and a
non-loopback `base_url` must be https; a degraded run (estimated usage) is
recorded as a `runner_warning` event rather than printed to stdout.

## Slice 5 — agent-requested tools

`TOOL_REQUEST: <tool> (blocking|optional) - reason` marker parsing in
`agents.py`, policy in `toolpolicy.py`, the `tool_requests` ledger table, a
CLI `agentloop tools` surface, and a dashboard tools panel. Read-only
requests auto-approve; side-effecting ones queue for a human. A blocking
request parks the task at NEEDS_HUMAN with partial output kept. This slice
also fixed `Store.release_claim` so `agentloop redo <id>` recovers stranded
claims — before the fix, `human_redo` and `resume` left the `claimed_by`
lease set, making the task unclaimable forever and starving every pending
task behind it.

## Slice 6 — durability and whole-loop evaluation

Four additions. The `vcs.py` containment story (why the guard has five
checks, how each earlier version was defeated) is load-bearing for anyone
touching that module, so it stays in `CLAUDE.md`'s `vcs.py` section directly
rather than living only here. Summary: every task workspace became its own
git repo (`vcs.py`);
infra retry/backoff got test coverage (the behavior pre-dated this slice);
`agentloop eval --mode batch` measures final `TaskStatus` against gold
through a real `Loop`, mock-only; `test_runs.coverage_percent` is parsed from
test output already captured, `NULL` when nothing was reported. No decision
rule reads any of it, enforced by an AST test
(`test_vcs_loop.py::test_no_status_write_is_downstream_of_a_vcs_result`) and
a full-state differential proving `vcs_enabled=False` is a behavioral no-op.

## Slice 7 — office-metaphor visualization

Built and verified on branch `slice-7-office-view`, unmerged pending a human
eyeball pass (`temp/slice7-ui-checklist.md` + the seeded
`temp/demo-slice7.db`). No agent in this project can verify rendered `web/`
output — see `CLAUDE.md`'s note on `user_eyeballs_rendered_ui`.
