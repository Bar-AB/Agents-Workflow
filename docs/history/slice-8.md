## Slice 8: hardening and finalization

A full adversarial review of every module (four independent reviewers) before
the project was used against a live provider for the first time, plus every
finding it produced. The framing that matters: **the repo's own database held 0
tasks and 0 attempts**, so nothing here had ever been driven end to end by a
real model — and three of the criticals sat on exactly that path, invisible to
740 passing mock-based tests. 878 tests now; every fix landed with a regression
test that was watched failing first, and the two guards worth doubting were
falsified by neutering the mechanism and confirming the test went red.

**Three criticals.**
- **`server.py` accepted cross-origin mutations.** An unauthenticated mutation
  API on a documented default port with no `Origin` and no `Host` check. A
  browser *simple request* (`Content-Type: text/plain`, no preflight, permitted
  cross-origin) reached every POST route: measured, `POST /api/charter` from
  `Origin: http://evil.example` returned 200 and replaced the charter body —
  which `agents._charter_block` injects verbatim into every worker, validator
  and planner prompt, making it remote prompt injection into an agent holding
  `file_io`, `git` and Bash. `POST /api/tasks` returned 201; `Host:
  attacker.example.com` returned 200 with full task bodies, so DNS rebinding was
  enough to read goals, worker output and `test_command`. Both conditions are
  now checked before routing, on GET as well as POST, and they are two
  conditions because they stop two different attacks: `Origin` catches the
  browser that knows it is elsewhere, `Host` catches rebinding, where the
  browser believes the attacker's name *is* this server and so sends no foreign
  `Origin` at all. An **absent** `Origin` is accepted — only a browser sets one,
  and a browser cannot omit it cross-origin, so requiring it would break curl
  and the CLI in order to stop nothing. An IP-literal `Host` is accepted because
  rebinding needs a *name* to re-resolve, which keeps `--host 0.0.0.0` working.
  Deliberately not a token: an attacker already executing code on this machine
  is out of scope here exactly as they are for `executor.py`'s env scrub.
- **The default runner had neither money guard its sibling shipped with.**
  Every totality and never-zero guard was written for slice 4's
  `OpenAICompatRunner` and never back-fitted to `ClaudeSDKRunner`. So
  `extract_usage` used a bare `int()` and raised on a wrong *type* (`'n/a'`, a
  nested dict, NaN) — **after** the stream completed and the completion was
  billed. `agents._invoke` reaches `finish_attempt` only on a clean return, so
  the tokens and cost were discarded and `loop._with_retry`'s bare
  `except Exception` bought the same completion again, up to
  `infra_max_retries + 1` times, reported as an `infra_error` pointing the
  operator at their network. `extract_tool_calls` had the same shape on
  `message.content`. And with no never-zero guard, four silent zeros reached
  `attempts` as a measured $0.00 with `usage_estimated=False` and no
  `runner_warning` — so `_budget_tripped` could never fire, `_maybe_handoff`
  could never fire, and the dashboard rendered the fabricated number as spend.
  Both extractors are now total, and the never-zero block is **one shared
  helper** (`runner.never_zero_usage`) rather than a second copy, because a
  second copy is what drifted the first time. Found independently by two
  reviewers, which is this project's strongest signal.
- **`vcs` was inert on every default install.** `_run` sets `cwd=<ws>` and
  `_git` also appended `-C <ws>`, so git changed into the workspace and then
  resolved the same *relative* path again from inside itself. The shipped
  `workspace_root` is relative (`.agentloop/ws`), so every side-effecting call
  returned `git-failed` and the whole of slice 6 — round commits, the approved
  ref, recoverable reject and redo — never ran, announced only as one
  `RuntimeWarning` per task saying the task "ran without durability". **Every
  one of ~2900 lines of vcs tests used an absolute `tmp_path`**, which is why a
  green suite proved nothing here. Fixed with `os.path.abspath` on the `-C`
  value — lexical, deliberately not `Path.resolve()`, which would follow a
  junction at `<ws>` and quietly take over the one decision `_guard` exists to
  make. The regression test drives the full lifecycle from a relative root and
  is paired with a relative-vs-absolute differential.

**The rest, in the order they will bite an operator.**
- **`agents._VERDICT_RE` rejected ordinary LLM markdown.** Five of six realistic
  formats — per-field emphasis, comma or pipe separators, a bare `.95`, `n/a`
  for `na`, a percentage — parsed as `ESCALATE` at confidence 0, which is below
  `severe_threshold` and so goes straight to NEEDS_HUMAN with no revision round,
  recording "Unparseable validator output" over a verdict that had actually
  approved. `toolpolicy._MARKER_RE` had already won this argument (LLM output is
  markdown); the parser driving an automatic state transition had none of that
  hardening. **Decoration and separators widened, semantics untouched**: the
  three verdict kinds, the three tests values and the requirement that all three
  labelled fields be present are unchanged, prose that merely sounds like an
  approval still escalates at 0, and nine "not a verdict" controls pin that.
- **An undecided tool request revoked a capability the role already held.**
  `effective_tools` exempted a pending row by *logical name* while
  `subtract_withheld` works over the *concrete* footprint, and
  `LOGICAL_TOOL_MAP` is not injective. Measured with the shipped worker spec: a
  pending, **optional** `shell` — nobody's decision, never shown to a human —
  cost the role its declared `git`, because both resolve to `Bash`. The trigger
  is the shipped system prompt's own worked example (`TOOL_REQUEST: shell
  (blocking)`). The exemption is now decided in the same currency as the
  subtraction. Only the *pending* side widened: a **rejected** row still
  subtracts unconditionally, which is the fail-closed half and what stops an
  agent revoking its own baseline by asking for it. Five existing tests encoded
  the name-level approximation and were rewritten rather than the fix being bent
  to them — CLAUDE.md's rule says "a capability the role already holds", and the
  later E3/G1 remediation block says the same; those five predate it and were
  never reconciled. Each rewrite is annotated with that reasoning, and each now
  asserts a *more* honest answer than it did (the CLI went from promising
  "approving grants Bash" to stating "approving changes nothing — this role
  already has Bash").
- **The sandbox's two stated bounds were not bounds.** `capture_output=True`
  materialised the whole stream and truncated afterwards — measured, 331 MB in
  4 s (662 MB peak), extrapolating to ~9.8 GB at the default timeout, to keep
  4000 characters; three lines of generated test code could OOM-kill the loop
  and take a paid completion with it. And `subprocess.run`'s timeout kills only
  the direct child, then blocks in `communicate()` until every inherited pipe
  handle closes: a grandchild holding stdout defeated a 3 s timeout for 20.3 s,
  and one that never exits blocked forever, inside `_with_retry`, holding the
  claim. `_run_bounded` now reads into a bounded ring buffer in a daemon thread
  and kills the whole **process tree** (`taskkill /F /T` on Windows, a POSIX process group elsewhere).
  The timeout summary reports the *measured* wait as well as the requested one.
- **A missing registry role wedged the whole batch.** `Registry.load` replaces
  the defaults wholesale with no merge and no missing-role check, so a
  hand-edited `agents.json` can leave `worker` undefined; `registry.get` then
  raised a bare `KeyError` from `_maybe_handoff`, matching neither handler and
  escaping `run_task`. Measured: the batch aborted, task 1 sat `in_progress`
  holding its lease with an empty `escalation_reason`, everything behind it
  never ran, and the next `agentloop run` died identically — permanent
  starvation with one stderr line as the only signal. A missing `validator`
  failed differently and no better: three paid retries and an `infra_error`, the
  misdiagnosis CLAUDE.md explicitly names. Both roles are now resolved up front
  exactly as `plan()` resolves `planner`.
- **A claim failure in a parallel worker was swallowed and reported as
  success.** `_run_parallel.drain`'s guard covered `run_task` but not
  `claim_next_task`, which opens a write transaction and so raises
  `OperationalError: database is locked` whenever two `agentloop run` processes
  share a database. Measured at `max_parallel_workers=3`: `run()` returned 1 and
  raised nothing while two threads died and two tasks were silently dropped. The
  sequential path propagates, so the two modes disagreed about what a failed
  batch even looks like. The whole drain body is now guarded.
- **`resume` left the pause message on the row forever.** `set_status(...,
  reason="")` assigns only a *truthy* reason, and `pause` stamps "Paused by
  human; resume to continue." onto every task it touches — measured still
  reading it on a `done` task. Four sibling release-to-PENDING paths already
  blanked it explicitly; `resume` was the fifth and the only one missing the
  line, and it is the one CLAUDE.md names as the *neutral* exit from a
  tool-request park.
- **`max_tokens_per_task` escalated a successful first task.** Measured on a
  real `--runner claude` run of the README's own quick-start task: the validator
  returned `approve` at 0.85 and the task escalated with "Budget cap exceeded
  (tokens=556515, cost=$0.49)" — the *token* cap tripping at under a tenth of
  the cost cap. Raised to 3M. The **rule is deliberately unchanged**: cache
  reads still count toward the token total and are still priced at 0.10x on the
  cost side. This was a badly chosen default, not a wrong rule, and changing
  what the number counts would have rewritten CLAUDE.md, the README table and
  the tests to fix a constant.
- Smaller, all measured: `/api/stream` answered 500 for a malformed cursor where
  `/api/events` answered 400, on the endpoint where `Last-Event-ID` is
  client-supplied on every reconnect; an unknown `/api/*` GET returned 200 and
  the dashboard HTML; static containment was a string *prefix* test rather than
  `is_relative_to`, so a `dist`-prefixed sibling passed; `TaskDetail` used
  `events.length` as its refetch trigger and that array is capped, so after 300
  events the detail pane froze forever, showing a stale status directly above
  the Approve button; `.card:hover`/`.card.selected` set the `border-color`
  shorthand at higher specificity than the status map, so the *selected* card
  lost its status colour; a malformed `test_command` raised `ValueError` into
  `_with_retry` and became three `infra_error` retries (now refused at
  construction, where `cli.main` renders it as `error: …`); the child `PATH`
  omitted the running interpreter's script directory, so the default `pytest -q`
  could not resolve when `agentloop` was invoked by path as the README offers,
  so the command failed every round and burned `max_revisions` on a gap no
  worker could close (see the round-2 note below: an earlier version of this
  sentence claimed `status="error"` falls back to the validator's `TESTS:`
  claim, which `TestResult.passed` disproves); `run_metrics`'s `by_model` rollup counted unfinished
  attempts while the headline totals filtered them, so the two never reconciled;
  `config.load` reported an unknown key (a typo'd budget cap) with `print`,
  which is strictly weaker than the `warnings.warn` this project already calls
  insufficient; six `run_task` escalation exits returned the in-hand `Task`
  rather than re-reading, the pattern `human_approve` documents as wrong;
  `git init` could reinitialise another repository through a gitfile at
  `<ws>/.git`, contradicting its own docstring's containment claim; `serve
  --runner` was an inert flag that read as "the dashboard will drive real work";
  and CLI output raised `UnicodeEncodeError` when redirected on Windows, which
  `main`'s `(KeyError, ValueError)` handler swallowed into `error: charmap`.

**A second review round, and what it caught in the first round's own fixes.**
The fixes above were themselves put through two independent reviewers, and that
round found three defects *introduced by the repairs* — which is the argument for
the round, not against it.
- **The widened verdict parser had turned a fail-safe non-match into a
  fail-open maximum.** The pattern accepts any magnitude, and the first version
  *clamped* out-of-range values instead of rejecting them — so `CONFIDENCE: 95`
  (a percentage with the sign dropped) became `1.0`, the top of the scale,
  clearing both thresholds and marking a task DONE with no human. `CONFIDENCE:
  40` rewrote a validator's severe-threshold judgement into certainty the same
  way. A clamp is not a rejection: it substitutes the **most permissive legal
  value** for one the model never wrote. Out-of-range is now an unparseable
  verdict, exactly as it was before the widening. The test that let this
  through asserted `v.confidence <= 1.0`, which cannot fail for a clamped
  value — a hollow assertion is worse than none, because it reads as coverage.
  Its replacement asserts against the **decision thresholds** and was watched
  going red on all five inputs with the clamp restored.
- **The same-origin guard accepted `Origin: null`.** Measured on the first
  version: a cross-origin `POST /api/charter` carrying `null` returned 200 and
  replaced the charter — the very attack the guard was written to close. A
  browser sends the literal `null` for an *opaque* origin (a sandboxed iframe,
  and any redirect chain that crossed origins, which a 307 survives with method
  and body intact), so it is a real cross-origin request that declines to name
  itself, not an absent one. An absent `Host` passed for the same reason and is
  closed with it.
- **The tool-capability exemption was a subset test where it needed to be an
  intersection.** `LOGICAL_TOOL_MAP` has *partial* overlaps as well as exact
  collisions: `file_io` -> `[Read, Write, Edit]`, `file_read` -> `[Read]`, and
  the shipped **planner** declares `file_read`. So an undecided, optional
  `TOOL_REQUEST: file_io` still stripped the planner's `Read` — the same
  self-revocation, one collision pair over from the `git`/`shell` case that had
  been measured. A capability held through a human's **grant** was not exempt
  either, so with `gate_declared_tools=True` a pending ask could revoke what a
  human had just approved. The reviewer also proved the tool-policy suite could
  not see any of this: with the exemption removed entirely, all 215 tests still
  passed. It now fails in *both* directions — too permissive and too strict —
  and that was verified by neutering each way.

Three more from the same round, each a place where a degradation existed and
nothing recorded it — the project's own standing rule is that a warning nobody
sees in `agentloop events` is unrecorded. A garbage **cache**-token field was
coerced to 0 and written as a *measurement*: nothing estimates the cache fields,
so `estimated` stayed empty, `note` stayed empty, `usage_estimated` stayed False
and no `runner_warning` fired — and per the decision rules the token total
includes cache reads, so on a cache-heavy run that is the dominant term of the
budget cap. `coerced_usage_fields` now reports it (and note the failure mode
worth remembering: the reporter was written, and then *not called* — dead code
that reads as a fix, which is why its test was written to fail against exactly
that state). A typo'd `loopconfig.json` key now logs a `config_warning` event
from `Loop.__init__`, the first place in that path with a store. And
`_run_parallel` recorded only `errors[0]`, discarding every other worker's
exception on the one path whose whole purpose is that a dying worker must not be
silent; each is now logged as `worker_failed` before the first is raised.

Two smaller ones from the same round, both places the code and its own prose had
drifted apart: the `` added to stop `TESTS: nap` reading as `na` had silently
narrowed `TESTS: passed` / `failed` out of the grammar, on a slice whose stated
purpose is surviving ordinary formatting; and the executor comment justifying the
`PATH` fix claimed `status="error"` falls back to the validator's `TESTS:` claim,
when `TestResult.passed` returns `False` for it — the honest consequence is that
an unresolvable command fails every round and burns `max_revisions`. The
documented "Windows job kill" is `taskkill /F /T`, which walks the live
parent-PID chain and therefore misses a reparented orphan; that residual is now
named rather than claimed away.

**A third round, from the integration verifier**, which ran 18 scenarios against
the finished tree — reproducing the pre-fix `vcs` failure, driving the
cross-origin attacks over raw sockets against a real server, and neutering all
three round-2 mechanisms to confirm the suite goes red in each direction. 16
passed; the two that failed were both honesty gaps rather than exposure, and
both are the same shape as everything else this slice found.
- **The cache-field reporter was wired into both backends and could only see
  one of them.** `coerced_usage_fields` scanned top-level keys ending in
  `tokens`. Anthropic reports its cache counts there; **OpenAI nests them**
  under `prompt_tokens_details.cached_tokens`, and `prompt_tokens_details` does
  not end in `tokens` — so on that backend a garbage cached count was invisible
  while `extract_openai_usage` coerced it to 0 and recorded that as measured.
  The docs said the reporter covered both, and a reporter present on a path but
  structurally unable to read that path's data shape *reads as coverage*. It now
  descends one level and names the field as `parent.child`. Note the direction,
  because it decides the severity: with `cached` at 0, `tokens_in = prompt -
  cached` becomes the **full** prompt at the full rate, so this over-billed and
  tripped the cap early rather than under-measuring.
- **A correction reached the code and not the prose.** The executor comment
  wrongly claiming `status="error"` falls back to the validator's `TESTS:` claim
  was fixed in round 2 — but the same sentence survived in `README.md` and in
  CLAUDE.md's own round-1 list, so this document contradicted its own
  correction. `TestResult.passed` returns `False` for `"error"` as well as
  `"fail"`; only `"na"` falls back. The real consequence, and the one an
  operator needs, is that an unresolvable test command fails *every* round and
  burns `max_revisions`.

The verifier's own caveat is worth keeping: all of this remains mock-driven.
The framing at the top of this section — 0 tasks, 0 attempts, three criticals on
the one path 740 tests could not see — applies to the verification too. The
nested-cache gap is precisely that class of bug: a data shape nobody had a live
sample of. Watch the first few real runs' `attempts` rows and `runner_warning`
events.

**What was checked and found sound**, because a review naming no confirmed
property is not a review: all 43 `store.py` write sites are transactionally
paired and the `events` table is genuinely append-only (verified by AST walk);
the claim is a real compare-and-swap; every decision rule in the section above
is enforced where it is documented; the tests gate reads executed truth; the
OpenAI auth path (call-time key, no redirect, https-or-loopback) and its
permanent-vs-transient classification are correct; prompt assembly matches the
spec and an absent charter is byte-identical; `_invoke`'s coercion barrier is
above the closing transaction; `parse_coverage`, `parse_tool_requests` and
`_extract_findings` are total. **No refactor was recommended**: `loop.py` and
`store.py` were both judged deep modules earning their size, and splitting them
would scatter the transaction, CAS and lease discipline across four callers.

**No decision rule changed.** The thresholds, revision counting, the budget-cap
rule, the tests gate, the tool-gate direction and the provider rule all read the
same fields they did before. What changed is that several of them can now
actually be reached: a verdict that parses, a token count that is measured, a
budget cap that is not tripped by its own default, and a durability layer that
runs at all.

