# Code notes — every "why" that used to live in a comment

The code itself now keeps a comment **only** where deleting it could let a
real bug or security hole come back (a "safety-net" comment — usually one
line, right above the check it explains). Everything else — design
reasoning, trade-offs, bugs that were found and fixed, non-obvious-but-not-
dangerous context — moved here, organized by file. **Read this before
changing a function whose behavior looks simpler than it is** — that is
usually a sign the reasoning moved here rather than that the complexity was
unnecessary. `docs/GUIDE.md` is the higher-level, narrative version of the
same material; `docs/history/` has the full incident-by-incident account for
the largest slices.

---

## agentloop/agents.py — prompt building, verdict/plan parsing, `_invoke`

**The money-safety theme (appears many times in this file, stated once
here):** `_invoke` is the function that calls the model and records the
attempt. The API call is billed the moment it returns — so from that instant
until `finish_attempt` commits, *nothing may raise*, or a retry would pay for
the same completion twice. That's why: `result.output` is coerced to a safe
string before anything else runs; a non-text reply is blanked (not `str()`'d,
which would hand the validator an object repr); a lone unpaired surrogate
character (half of a truncated emoji from a malformed provider JSON body) is
escaped rather than left to crash sqlite's insert; tool names/inputs/notes
are all coerced to bounded strings before they reach `log_event`'s one
`json.dumps`; `classify()` and the tool-status lookup are computed *before*
the closing transaction opens, not inside it; and there is deliberately no
`try/except` anywhere in the tool-request loop — a swallowed failure of a
*nested* transaction would still roll back the outer one and then raise
`TransactionAborted`, turning a one-in-a-million telemetry hiccup into a
guaranteed double charge. The fix pattern throughout is the same: remove the
thing that can raise, don't catch it after the fact.

**Verdict regex (`_VERDICT_RE`).** Deliberately tolerant of decoration
(markdown emphasis, commas, a bare `%`) and strict about meaning. The
original strict pattern rejected five out of six real-world formattings a
model actually produces. What's *never* widened: the three verdict kinds,
requiring all three fields present, and the confidence range — a bare `95`
is ambiguous (percentage missing its `%`? Typo?) so it's refused rather than
guessed. An earlier version *clamped* an out-of-range number instead of
refusing it — which mapped `CONFIDENCE: 95` to `1.0` and auto-approved a task
nobody reviewed. Rejecting beats "fixing" by substituting the most
permissive legal value.

**Findings marker (`FINDINGS:`).** Soft — a miss just yields no findings,
never an error. The end-of-findings detector requires a *blank line* before
a markdown heading, because a bare heading pattern also matches a `# TODO:`
line quoted *inside* a finding (a code reviewer quoting a comment is common),
which silently dropped everything after it.

**Which agents can author a tool request** is an explicit allowlist
(`worker`, `validator`, `planner`), not "everyone except the summarizer" —
the summarizer's job is compressing a transcript that may *quote* a marker
verbatim, and parsing that would manufacture a request out of a quotation.

**Which ledger status a policy verdict maps to** is a dict lookup, not
if/elif branches, specifically so a new `ToolClass` added later raises a
`KeyError` at the lookup (cheap, outside the transaction) instead of falling
through to whichever branch happened to be written last.

**Context-budget handoff, planner, charter:** the worker's system prompt
includes the workspace path in prose even though `cwd` is also set, because
`cwd` tells a tool *where* to operate but can't tell the model *what the
directory is for* — that's the `## Workspace` block's job. A hand-edited
`agents.json` predating the `summarizer` role falls back to the worker's own
spec, so a handoff degrades gracefully instead of crashing. The planner and
validator are both "chartered" (told the house rules) because the planner
writes the acceptance criteria the validator later judges against — a
charter violation baked into the criteria would reproduce the conflict one
level up if the planner didn't see it too.

---

## agentloop/cli.py

Error handling throughout follows one rule: user input (a bad `--project`
name, a malformed `loopconfig.json`, an already-decided tool request) always
renders as a clean `error: ...` line, never a raw Python traceback — bad
ids raise `KeyError`, an already-decided row raises `ValueError`, both get
caught and rendered by `main()`'s handler.

`--project` on `agentloop run` is deliberately **not** resolved to the
default when omitted — `Loop.run(project_id=None)` means "every registered
project," and resolving a bare `agentloop run` to just the default would
make that capability unreachable from the CLI. Every *other* project-aware
command (`add`/`plan`/`status`/`events`) **does** resolve an omission to the
default, since a task must belong to exactly one concrete project.

`agentloop eval --mode batch` refuses a non-mock `--runner` the same way
`--runner openai` used to fall through to the mock branch and print scripted
numbers as a real calibration report — a silent wrong answer is worse than a
refusal.

`--workspace-mode` omitted on `project repoint` means "keep the current
mode" — never a silent reset to `scratch`, which would quietly throw away a
worktree-mode project's durability guarantees the moment an operator only
meant to change the repo path.

---

## agentloop/config.py

`bool` is checked before `int` when coercing a config field, because `bool`
is a Python subclass of `int` — an unguarded int check would silently accept
`True` as a valid count.

`max_tokens_per_task`'s default was raised because the token total includes
prompt-cache reads (a cached round re-counts the whole prompt), and the
original default tripped on the README's own quick-start example before the
*cost* cap ever did.

`vcs_command` is an executable **path**, not a command line — unlike
`test_command`, which goes through shell-style argument splitting. Passing
`"git --no-pager"` here is one literal (wrong) executable name.

`worktree_root` defaults **outside** `repo_root` on purpose, enforced not
just defaulted: a workspace living inside the repository it's supposed to be
sandboxed from would turn a documented path-escape bug in the executor into
a write on the operator's actual working tree, bypassing every recovery path
this whole feature exists to add.

`memory_retrieval_backend` and `workspace_mode`: an unrecognized value
**raises**, it never silently falls back to a working default — which
backend/mode ran is part of how the run behaved, and substituting one
silently is exactly the kind of drift the audit log exists to prevent.

`tool_readonly_allowlist` excludes `web` on purpose — a web-fetch tool
egresses the prompt to an external destination, and this project already
treats "don't hand external code the credentials/data it doesn't need" as a
hard rule elsewhere (the sandbox env scrub); the same standard applies here.

`gate_declared_tools` defaults to **off** because the shipped worker
declares `file_io` and `git` — neither read-only — so turning this on by
default on a fresh install would gut the worker immediately.

A typo'd config key (e.g. `"max_cost_per_task"` instead of
`"max_cost_per_task_usd"`) is logged with `warnings.warn`, which is
deliberately treated as *not enough*: a warning never reaches `agentloop
events`, the REST API, or the dashboard — the stakes are a budget the
operator believes they lowered and didn't, so it's also recorded where a
human actually looks.

Cache-price multipliers (`CACHE_MULTIPLIERS`) had to stop being one global
pair once a second provider existed — Anthropic and OpenAI discount cached
reads by completely different factors per model family (as low as 0.10x on
some, 0.50x on others). Left global, a cross-provider run would under-bill a
cached read on one provider by up to 5x, and the budget cap is only as
honest as the worst-priced attempt running under it.

---

## agentloop/eval.py

Fixtures are grouped by how hard they should be: clearly good, subtly wrong,
and ambiguous/unsalvageable — each exercising a different corner of the
confusion matrix (an over-harsh validator, a high-confidence wrong verdict,
a validator picking the wrong wrong-answer). Confidence bucket edges
straddle the two live thresholds (0.40, 0.70) so the calibration table
directly answers "is there signal right at the boundaries a real decision
uses?"

Batch fixtures measure the loop's *final status*, not just the validator's
*verdict kind* — the difference matters because a validator returning the
"right" verdict kind doesn't prove the revision-budget logic, the severe-
threshold short-circuit, or the risk-level gate actually fired correctly. A
fixture that runs off the end of its scripted replies is **not** scored as
agreement even when it happens to land on the gold status — `MockRunner`
improvising past its script parses as an unparseable verdict, which
escalates, which happens to be the gold status for most escalation
fixtures, so an unbounded regression in the severe-verdict rule could
silently "pass" by running the script dry rather than by being correct.

---

## agentloop/executor.py

The sandboxed env allowlist has **no denylist** — an operator who adds
`"ANTHROPIC_API_KEY"` to `sandbox_env_allowlist` hands it to generated test
code, and that's accepted as a real (if risky) operator choice, warned about
by name rather than silently allowed or hard-refused.

Coverage-percentage parsing (`parse_coverage`) is defended on several
independent fronts because it's parsing arbitrary text a worker's test
output could contain: a lookbehind stops "Total coverage: 87.5%" from also
matching as "5%"; the match length is bounded to stop backtracking blowing
up on a pathologically long line; a `-` is excluded from the captured digits
so "TOTAL 1 0 -5%" can't be misread as `5.0` instead of "no valid total
here"; and two numeric columns are required before the percentage so a
sentence like "TOTAL of 3 tests failed, 20%" doesn't get read as a coverage
number.

The subprocess reaper was fixed after a real leak: a child that exits
normally within its timeout, while a grandchild it spawned keeps the output
pipe open, used to leak that grandchild (holding files open, which is
exactly what blocks a later `rmtree` on Windows) and leak a reader thread
per round — the kill routine now runs on both the timeout path and the
normal-exit path.

The running interpreter's own `Scripts`/`bin` directory is prepended to
`PATH` for the child process — this is a correctness fix, not a convenience:
calling `agentloop` by its full path instead of activating the virtualenv
leaves the venv's own tools off `PATH`, so the default `pytest -q` test
command silently can't resolve at all.

---

## agentloop/loop.py

**The state machine's core invariant:** every exit from `run_task`'s main
loop re-reads the task row from the store rather than trusting the in-memory
object, because `set_status` is *lease-predicated* — it can silently no-op
if a human took the task mid-round — and returning the in-hand object would
then claim a status transition that never actually happened on the row.

**Claiming and parallel workers.** `claim_next_task` is an atomic SQL
compare-and-swap, never a bare `SELECT` — two workers racing for the same
row must not both win. The parallel-worker guard originally covered only
`run_task`, not `claim_next_task` itself — but claiming also opens a write
transaction and can raise "database is locked" when two `agentloop run`
processes share one file, and an uncaught raise there used to kill a worker
thread silently: `errors` stayed empty, `run()` reported success, and tasks
were quietly dropped with only an unlogged traceback on stderr.

**Config/registry problems are config errors, never infra errors.** A model
pinned to the wrong provider, a missing `worker`/`validator`/`planner` role
in a hand-edited `agents.json`, a runner name that doesn't exist — all of
these are resolved *before* the retry loop and escalate immediately with no
retry and no `infra_error` event, because retrying a typo with exponential
backoff only burns the clock to reach the identical conclusion, and reports
would otherwise point a human at "the network" instead of at their own
config file. A missing `worker`/`validator` role specifically used to raise
a bare `KeyError` from inside the context-handoff check, which matched
neither the config-error nor the infra-error exception handler and escaped
the function entirely — leaving the task stuck `in_progress`, its lease
held, no escalation reason, permanently unclaimable (every subsequent
`agentloop run` re-claimed it and died the same way).

**Empty worker output escalates immediately, never revises.** An empty
output is the *absence* of work, but every downstream step (test run,
validator review) would treat it as real work if let through — a validator
approving a blank diff marks the task DONE, and under the task-graph feature
that releases dependents to run against output that doesn't exist. Treated
exactly like `human_approve` already refuses a still-`pending` task, one
step earlier in the pipeline. It's an escalation and *not* a revision
because emptiness isn't a quality gap a worker can fix by trying again — the
model gave back nothing, so re-prompting the same way just burns a revision
round on a call that already failed silently.

**Tool-request parking.** When a worker or validator says (via the
`TOOL_REQUEST:` marker) that it genuinely can't finish without a capability
it doesn't have, the task parks at NEEDS_HUMAN with its partial output kept
and the revision count untouched. Of the ways out, only "pause, then
resume" is truly neutral (releases the lease, clears the park flag, decides
nothing, discards nothing) — `human_redo` wipes the output/workspace/
revision count, and `reject_tool_request` deliberately does **not** release
the task (a denial must never silently restart a paid worker run against a
gap the human just confirmed will stay unfilled). The park's own status
write and the `parked=1` stamp happen in one transaction and are gated on
the write actually landing — because `set_status` can legitimately no-op
if a human already took the task, and an unconditional stamp would then
leave a *stale* `parked=1` flag that a later, unrelated escalation could
accidentally be "undone" by simply approving the old tool request.

**Durability (git) call sites are "call, log, discard" everywhere** — none
of them run inside an open `Store.transaction()` (a 30-second git timeout in
there would stall every dashboard reader sharing that connection, and a
raise would roll back the audit event it's paired with), and no decision
rule anywhere reads the result of a git call. `human_reject` rolls the
workspace back to the task's base ref; `human_redo`'s "fresh start" now
means *emptied*, not *destroyed* — the discarded round stays reachable via
a git ref written *before* anything else moves, specifically so a caller
that then falls back to a full wipe never destroys history that ref was
just written to protect.

**Context handoff and vcs-readiness state are deliberately kept as local
variables inside `run_task`, never persisted** — because both are cheap to
recompute from scratch (re-measure context usage from zero, re-initialize an
idempotent git repo), so a crash mid-task just means the next attempt
recomputes them rather than trusting stale saved state.

**Test execution happens in a "clean" snapshot window**, specifically taken
*after* the tests ran and *not* before, so that anything the validator later
sees attributed to it (like stray pytest cache files) is actually the
validator's own doing and not test-run noise misattributed to it — noise in
a detection signal is how a real detection stops being trusted.

**The validator can review the worker's output under a different model
family entirely** (the cross-provider validator) — nothing below that point
in the code changes at all; the verdict-handling path is byte-identical
either way, which is the whole point of putting the provider choice behind
one seam instead of threading a branch through the decision logic.

---

## agentloop/memory.py

Injection is capped so memory can't crowd out the actual task in the
prompt. Pinned facts get their own *smaller* ceiling **above** the main cap
— they're facts a human explicitly said must always be present, so they
skip the ordinary alphabetical cutoff — but they're still bounded, so
pinning everything can't reintroduce the exact crowding problem the cap
exists to prevent.

`maybe_promote`'s target project must be threaded through explicitly by the
caller, never left to resolve to "whatever project is currently active" —
a project-scoped memory read has to promote a fact against *its own*
project, which is not necessarily the "active" one.

---

## agentloop/models.py

`Task.kind`: `'task'` is work a worker executes; `'plan'` is a
planner-owned container row holding the goal that got decomposed — it's
**never claimed by the loop**. Handing a raw goal statement to a worker as
though it were an ordinary task (skipping the planner-owned container
distinction) is exactly the bug this field prevents.

`Task.project_id` starts `None` on a bare, not-yet-persisted `Task(...)` a
caller constructs in memory — `Store.add_task` resolves it to the real
default project's id before the actual `INSERT`, so every existing call
site that never explicitly sets it keeps working unchanged. Once a row is
actually in the database, this is *always* a real project id, never `None`.

`ToolRequest.parked`: a **live** fact about right now ("this task is
currently being held open because of this row"), not a historical one — the
audit log already keeps history — so every exit from the parked state
clears it, which is what stops a tool decision from accidentally reverting
an escalation the tool-request queue didn't even cause.

`RunResult.usage_estimated`: without this flag, an estimated token count
reaches the `attempts` table and the dashboard **indistinguishable from a
real, provider-measured number** — a fabricated cost displayed as though it
were a real one.

`TestResult.coverage_percent`: parsed only when the test command actually
reported one; `None` means "nothing was reported," never "0% covered" —
deliberately never a decision-rule input, only a display value.

---

## agentloop/registry.py

The tool-request grammar taught to agents is **one shared string constant**,
injected verbatim into three different system prompts (worker, validator,
planner — never the summarizer), specifically so there is exactly one
canonical spelling of the marker syntax. Three hand-written copies would
drift into three slightly different grammars, and the parser would silently
reject whichever one drifted.

System prompts are built by string concatenation, not an f-string, because
the planner's prompt contains literal JSON curly braces that an f-string
would try to interpret as format fields.

The planner is taught to name *what a request costs*, not encouraged to make
one — it has no write tools by construction (`file_read` only) and must not
be nudged toward asking for one.

The `worker`/`validator`/`planner` prompts moved through two real, meaningful
prompt-version bumps: v2 added charter-awareness (an unchartered validator
prompt used to explicitly say "judge strictly against acceptance criteria,"
which instructs it to *disregard* anything else — so just injecting a
charter block without changing that sentence would have been completely
inert). v3 taught the `TOOL_REQUEST:` grammar — also not cosmetic, since
before it nothing told an agent the marker existed, so the only markers real
traffic would ever produce were quoted ones inside other text, never live
requests.

---

## agentloop/retrieval.py

The embedding vector width is deliberately small — brute-force cosine
similarity over a few thousand facts is free at that size, and it's wide
enough that unrelated short facts rarely collide by chance. The stopword
list is deliberately tiny, not a "real" NLP stopword list — an aggressive
one mostly discards vocabulary that would have actually mattered for
matching a fact to a task.

---

## agentloop/runner.py

**Usage must come from the terminal message only, never summed across the
stream** — the terminal message's usage field is already the whole run's
running total, so summing it from every message in the stream double-counts
everything. (This was a real, fixed bug.)

**The "never report zero usage silently" guard exists on both providers now**
because a stream that never produces a terminal usage-bearing message (an
SDK version bump changing its message shape, say) used to silently record
all-zero usage with no warning at all — meaning `$0.00` recorded spend, the
budget cap could never trip, the context-handoff check could never trip, and
the dashboard rendered a fabricated zero as though it were a real
measurement.

A field that's *present but unreadable* (garbage type) is treated
differently from a field that's simply *absent* — particularly for the two
cache-token fields, where an unguarded coercion used to silently turn
garbage into `0` and get recorded as a clean measurement rather than a
degraded estimate. Since cache reads are often the dominant cost on a
cache-heavy run, that silently zeroed out the dominant term of the budget
check.

The tool-name-to-vendor-tool translation is one-directional and fails
closed: an unrecognized *logical* name maps to *nothing*, never passed
through blind — an agent silently gaining an unintended real tool is judged
worse than an agent being denied a tool it asked for by name.

The OpenAI-compatible backend accepts a `cwd` parameter (for interface
symmetry with the Claude SDK backend) but silently ignores it — a
chat-completions call has no filesystem at all, so there is no tool to
resolve a path against; warning about it would fire on every single attempt
with nothing an operator could actually do about it. It does *not* silently
ignore a missing API key or an unusable model — those are checked before the
network call and raise a proper config error rather than surfacing as a
retried, misleading `infra_error`.

The maximum SDK turn count was raised after being measured too low for a
real worker round (write files, run tests, fix, re-run) — the SDK would cut
the turn off mid-task and the *next* attempt's resumed session then reported
a confusing secondary error on top of the real one.

Content is read from the reply **before** usage, and in that order for a
reason: content parsing may legitimately raise (a genuinely bad reply is
worth nothing), but usage parsing may **not** raise past that point — by
then the completion is in hand and has already been billed, so any usage
schema surprise degrades to an estimate rather than discarding a result that
would then get retried and paid for twice.

---

## agentloop/server.py

**Same-origin enforcement** guards every mutating endpoint on what is an
unauthenticated API by design. Two independent checks catch two different
attacks: `Origin` catches an ordinary cross-site request; `Host` catches DNS
rebinding, where the attacker's domain *is* what the browser believes this
server's name to be, so no `Origin` header disagreeing with anything is ever
sent. `Host` deliberately does **not** treat an *absent* header as
acceptable — that used to be a real fail-open hole, the same shape as
treating a literal `Origin: null` as acceptable. Not solved with an auth
token, deliberately — the cheapest fix that closes the remote attacker,
without threading a secret through every client (frontend, CLI, every README
curl example).

**A hostname bind resolves to a literal IP for `server_address`**, which
can't answer "is this the name the operator actually chose to be reachable
at" — so the operator's own hostname is tracked separately so
`serve --host 0.0.0.0` reached over the LAN as `http://devbox:8765` still
passes the same-origin check without weakening it (an attacker's domain is
still neither loopback, nor this literal host, nor an IP literal).

**Static file serving checks real path containment, not a string prefix** —
`str.startswith()` treats the parent directory as a text prefix, so a
sibling directory whose name merely *starts with* the same characters used
to pass (`/../dist-backup/secret.txt` resolving outside the intended `dist/`
folder was measured to actually get served).

An unmatched `/api/*` route returns a proper 404 rather than falling through
to the static-file handler, which otherwise answers literally anything that
isn't a real file with `index.html` — so a typo'd endpoint used to return
200 and a page of HTML instead of a clean error a client could detect.

A malformed numeric query parameter (a bad SSE cursor, a bad task id)
returns 400, not the 500 an uncaught `int()` conversion produces — a 500
reads as "the server itself is broken," which is the wrong signal for what
is actually just a bad request; `Last-Event-ID` in particular is client-
supplied on every single reconnect, so a malformed one is routine client
state, not an exotic edge case.

---

## agentloop/store.py

**`git`/`shell` both mapping to the same underlying concrete capability
(`Bash`) is a real UX trap, not a bug** — a human shown "approve `git` —
commit the fix" is actually granting unrestricted shell execution, while
every screen and the ledger itself say `git`. Fixed by making the *ask*
self-describing (it names its concrete footprint) rather than by narrowing
the map, since two logical names legitimately sharing one concrete tool is
a real, intentional relationship, and narrowing it would silently change
what every existing registry entry means.

**`Store.transaction()` is reentrant, and only the outermost boundary
actually commits or rolls back.** An inner `commit()` call is deferred; a
failure anywhere in the nest sets a flag the outermost boundary checks, so a
paired row-write-plus-audit-event either lands together or not at all,
never half of one.

**The claim CAS's `rowcount`** (how many rows an `UPDATE` actually changed)
is the signal that tells "this process won the race" from "another process
already claimed this row a moment earlier" — not a separate read-then-check.

**Schema migrations that add a foreign-key column can't add the constraint
retroactively** — SQLite's `ALTER TABLE ADD COLUMN` rejects a foreign key
with a non-`NULL` default. Fresh databases get the real constraint from the
schema; databases upgrading from an older version get the bare nullable
column plus an explicit backfill, and the ordering between migration phases
matters (a later phase's helper functions sometimes depend on an earlier
phase's column already existing).

**The one-time `memory` table rebuild (moving its uniqueness constraint) has
to capture the *true* historical high-water mark of the id sequence before
dropping the old table** — a naive "carry forward `max(surviving ids)`"
undercounts if a row holding a *higher* id had already been deleted at any
point before the migration runs (an ordinary occurrence), and skipping this
capture would let a later insert reissue an id that already meant something
else once, breaking any old audit-log reference to it.

**A task is "blocked" as a *predicate evaluated at claim time*, not a
status** — an unfinished dependency or an unapproved plan. This means
nothing ever has to be explicitly un-set when the blocker clears, and a
human reading the task list can never mistake "waiting" for "finished."

**Memory approval is approval *of a value*, not of a key.** A rewrite that
changes an approved fact's content drops it back to unapproved — otherwise
an agent could rewrite an already-vetted key and have brand-new,
unreviewed content silently inherit the trust that was only ever given to
the old content. `pinned` is different — it's sticky across a value change,
because it's a statement about the *key* ("always show agents this"), not
about one particular value, and it never grants a read on its own regardless.

**A `hit_count` bump on a memory fact requires both a ranked-above-zero
injection *and* a distinct task id** — the second half exists because
worker + validator + a revision round are three prompts inside *one* task,
and counting prompts (rather than distinct tasks) used to promote a fact to
shared memory on the strength of a single task using it three times.

**The tool-request per-task cap counts only `pending` rows** — the queue a
human actually has to clear — because counting already-decided rows (`auto`,
`refused`, `approved`, `rejected`) made the cap self-fulfilling: once a task
accumulated enough decided history, *nothing further* could ever be
recorded on it again, including a harmless auto-approved read-only request.

**A refused tool request is refused *and audited*, never silently dropped**
— an agent spamming 500 distinct tool-name markers would otherwise hand a
human 500 rows to clear, but a request that vanished with zero trace is
judged worse than one that's cleanly denied and visible.

---

## agentloop/toolpolicy.py

**The tool-request marker regex was fixed for line-ending handling *twice*,
and the second fix closed the whole class instead of one more case.** The
first fix added `\r?$` to handle Windows CRLF line endings (this project is
developed on Windows). That still missed other line terminators Python's own
`str.splitlines()` already knows about (a lone classic-Mac `\r`, several
Unicode line-separator characters) — each one silently dropped a
well-formed marker with zero signal (no row written, no event logged,
capability silently withheld). The actual fix: normalize line endings via
`splitlines()` before matching at all, so "which terminators did we
remember to handle" stops being a question with an incomplete answer.

**Fail-closed tool subtraction, the load-bearing rule of this whole
module:** `LOGICAL_TOOL_MAP` is not one-to-one — `git` and `shell` both
resolve to the same underlying `Bash` capability. So denying/withholding one
*logical name* has to subtract the whole *concrete capability*, which also
silently removes every other logical name that happens to share it. The
alternative (subtracting only the exact name asked about) would let a
rejected `shell` request leave `Bash` fully available through a role's
separately-declared `git` — a permission gate that looks closed to the human
who clicked reject but isn't, which this project treats as strictly worse
than an over-broad, inconvenient gate.

**An *undecided* (pending) tool request must never cost a role a capability
it already holds** — only a *decided* (rejected) one should. Without this
exemption, an agent's own innocent, non-blocking `TOOL_REQUEST: shell` (the
registry's own worked example!) from a role that had only ever declared
`git` would silently strip `Bash` from that role for the rest of the task,
with zero human ever asked to decide anything. The exemption is evaluated in
the same currency as the subtraction (the actual concrete capability
footprint, via intersection — not a simpler subset check, which misses a
*partially* overlapping pending ask). Only the pending side gets this
leniency; a genuinely rejected row still subtracts unconditionally, which is
what stops an agent from revoking its own baseline permissions just by
asking for something it already has.

**A gated tool with no request row on file is withheld and nothing is
recorded** — failing open here (granting it anyway) would hand over exactly
the capability this whole gate exists to hold back.

---

## agentloop/vcs.py — the interesting one

Read the "war story" version of this in `temp/INTERVIEW_PREP.md` §7 — this
is the fuller technical version.

**Threat model:** this module runs its own fixed, hardcoded git commands —
but *inside a directory that AI-written code can write to*. So two
completely different axes are both live attack surfaces at once: which repo
git actually believes it's operating on (*aim*), and what programs git ends
up executing along the way (*execution*). Both were measured, not assumed.

**Aim — four defeats of increasingly careful guards, in the order they were
found:**
1. Containment ("is `<ws>/.git` inside `workspace_root`?") passed for a
   directory a worker made *inside its own workspace*, and also for a
   *sibling task's* real repository — because `workspace_root` is the
   parent of every task's workspace, containment alone can't distinguish
   "this task's own repo" from "some other task's repo" or "a subdirectory
   I made myself."
2. Fixed by checking *identity* with `<ws>/.git` instead of mere
   containment — but a `.git` that's actually a gitfile (one text line
   reading `gitdir: <somewhere else>`) or a junction pointing elsewhere
   passes an existence check while git resolves the *real* repo from
   wherever it points. Measured: `git init` run against a worktree whose
   `.git` pointed at a victim repository actually *reinitialized that
   victim repository*.
3. Fixed by resolving where `.git` *points to* — but a single ordinary file
   write, `<ws>/.git/commondir`, redirects where git reads its actual
   config, refs and objects from, without touching `.git`'s own reported
   location or the pinned config file's contents at all. Measured, with
   every earlier check still reporting "safe."
4. Final fix: stop reasoning about filesystem paths entirely and just ask
   git itself — `git rev-parse --show-toplevel --absolute-git-dir
   --git-common-dir`. `--show-toplevel` must literally *be* the workspace
   directory itself; `--absolute-git-dir` and `--git-common-dir` must
   literally *be* the workspace's own `.git`. All three by identity, not
   containment. A path-based check is structurally always one redirection
   trick behind; asking the tool what it actually believes closes the whole
   class at once instead of the one instance that was just found.

Worktree mode (existing-repository workspaces, where the task workspace is a
real `git worktree` of the operator's own repo) has a fifth condition beyond
these four: HEAD must resolve to *this task's own* branch
(`<branch_prefix><task_id>`), since nothing else in the guard's shape
constrains which ref a worktree-mode command could move — an absent or
mismatched task id refuses (`no-task-id`) rather than defaulting to "trust
it." `--git-common-dir` in worktree mode must equal `<repo_root>/.git`
(the shared object store), not the per-worktree `.git` file.

**Execution — the config-pin mechanism.** Git config can name arbitrary
external programs it runs during an ordinary `commit` (`filter.<name>.clean`,
`core.fsmonitor`, and others) — there's no denylist for this because the
names are arbitrary and unenumerable; "block the dangerous config keys"
isn't a reachable design. The fix instead: right after creating a repo,
fingerprint (hash) the `.git/config` file git itself just wrote, store that
fingerprint somewhere the AI has zero write access to (the SQL database, not
the filesystem), and re-check the live file's hash against it before
*every* later git operation — refuse if they don't match, **before any
subprocess spawns**. Measured before this existed: a worker writing
`filter.pwn.clean = <python>` plus a matching `.gitattributes` executed
arbitrary code outside its own workspace on the very next ordinary commit.

**Recoverability.** A rejected or redone round doesn't delete history — it
writes a ref pointing at the discarded commit **before** anything else
moves (never a counter-based name, which two concurrent rollbacks could
silently collide and overwrite on). The round commit force-adds even
git-ignored files specifically so that everything a later rollback's
aggressive clean step deletes is provably recoverable from that ref — at the
literal cost of committing build artifacts every round in scratch mode.
Worktree mode can't do that (forcing `node_modules`/`.venv` into the
operator's own branch every round would be worse), so it explicitly reports
which ignored files are *not* recoverable after a worktree rollback, rather
than silently claiming full recovery it can't back up. Nested git
repositories a worker creates are a similarly named, permanent gap — git can
only record one as a reference (a "gitlink"), never its actual contents, so
a rollback can't bring those contents back either.

**Everything in this module is total** — every function always returns a
result object (or a plain bool), never raises — because a durability feature
that itself crashes the loop would be strictly worse than the loop simply
running with no durability at all.
