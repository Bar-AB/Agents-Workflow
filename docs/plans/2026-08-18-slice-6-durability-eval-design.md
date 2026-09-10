# Slice 6 — Durability & evaluation hardening (design)

Date: 2026-08-18
Workflow: `wf-20260818T083816Z-ae2428a2`
Baseline: `main` @ `73f5787`, 573 passed / 0 failed, tree clean.

## Purpose

Close roadmap item 6, the operational grab-bag. Four independent additions:

1. **git-commit-per-task rollback** — a task's workspace gets save points, so
   `redo` stops destroying the agent's work irrecoverably and `reject` rolls the
   workspace back instead of leaving a failed attempt's files lying around.
2. **Infra retry/backoff distinct from a "revise"** — already shipped in Slice 0
   (`loop._with_retry`). Scope here is *verification and coverage*, not new code.
3. **Batch whole-loop evaluation** — `eval.py` measures one validator verdict at
   a time. Extend it to measure a whole task's journey through the Loop.
4. **Coverage captured in `test_runs`** — the executed test result records a
   coverage percentage when the configured test command reports one.

## Users

The operator running `agentloop`, and the dashboard reading the same store.
No agent has a write path to any of this; nothing here is visible in a prompt.

## Success Criteria

- `agentloop reject <id>` rolls the task's workspace back to its base state, and
  the discarded work remains recoverable in that workspace's git history.
- `agentloop redo <id>` still leaves an empty workspace (the documented "fresh
  start" contract) but no longer destroys the previous attempt irrecoverably.
- A transient runner failure retried to success does not consume a revision, at
  **every** stage that calls `_with_retry` — not only the worker.
- `agentloop eval --mode batch` runs whole tasks through a real `Loop` and
  persists one `eval_runs` row whose `kind` is `batch`.
- `test_runs.coverage_percent` holds a number when the test output reports one,
  and `NULL` when it does not — never a wrong number, never an exception.
- Full suite green; `ruff format .` / `ruff check` clean.

## Constraints

- **stdlib-only core.** `git` is an *external executable*, not a Python runtime
  dependency — the same category as the configured `test_command`. Its absence
  degrades the feature; it never breaks a run.
- **No new decision rule.** Nothing in `loop.py` may read a commit, a coverage
  number, or a batch eval result to decide a status. Same register as Slice 4's
  provider axis and Slice 5's tool gate: this changes what is *recorded* and what
  can be *recovered*, never what an answer *means*.
- **Durability must never fail an attempt.** A git failure is logged and the loop
  continues, exactly as `warnings`-plus-audit-event degradation already works for
  `sandbox_isolation='strict'` and `RunResult.usage_estimated`.
- **Append-only audit log**; row change paired with its event inside
  `Store.transaction()`.
- Schema changes must go through `_migrate()`'s add-column path.

## Out of Scope

- Committing into the **user's real project repository**. The worker writes only
  into its sandboxed workspace; reaching outside it would break the threat model
  `executor.py` exists to enforce. Recorded as a rejected alternative below.
- A cross-task shared timeline (see ADR-1).
- Wiring a strict-isolation backend (`_strict_isolation_available` stays False).
- Coverage *gating* — no decision rule reads `coverage_percent`.

## Approach Chosen

### Part 1 — per-task workspace repos (`agentloop/vcs.py`)

A new module, a thin total wrapper over the `git` executable, mirroring
`executor.py`'s discipline: argv (never a shell string), `shell=False`, cwd
pinned to the workspace, a timeout, a scrubbed child environment, and **it never
raises** — every call returns a result object the caller may ignore.

Why its own module and not `executor.py`: the executor runs *the project's test
command*, which is operator config exercising model-written code. `vcs.py` runs
*our own* command against our own scratch directory. Different threat model,
different lifecycle, and folding them together would put a durability failure on
the path that decides the tests gate.

Lifecycle inside one task's workspace `.agentloop/ws/task-{id}/`:

| Moment | Action |
| --- | --- |
| workspace created | `git init`, then an empty base commit; ref `refs/agentloop/base` |
| worker round returns | commit everything as `round {n}` |
| task reaches DONE | commit (`--allow-empty`); ref `refs/agentloop/approved` |
| `human_redo` | roll back to `refs/agentloop/base` (files gone, history kept) |
| `human_reject` | roll back to `refs/agentloop/base` |

Rollback is: **write `refs/agentloop/discarded/<n>` at the current tip**, then
`git reset --hard <ref>`, then `git clean -ffdqx`, which leaves the directory
empty apart from `.git`.

**The discarded ref is not bookkeeping — it is what makes "history kept" true.**
Corrected 2026-08-18 after a planning review; the original text was wrong, and
the correction is measured, not reasoned. `reset --hard` moves the branch off the
round commits, and with only `refs/agentloop/base` and `refs/agentloop/approved`
in the repo those commits become reachable from no ref at all. `git log` and
`git log --all` walk refs, not the reflog. Measured in a scratch repo with the
exact command sequence above: before the rollback `git log --all --oneline`
listed `base` **and** `round 1`; after it, **`base` only**. `git show
<round-sha>:out.txt` still returned the content — which is why the acceptance
test that only asserts recoverability-by-sha passed while the property it exists
to protect was already gone. Writing the ref first fixes it, also measured: with
`git update-ref refs/agentloop/discarded/<n> HEAD` inserted before the reset,
`git log --all --oneline` afterwards listed **`round 1` and `base`**, and a
second rollback on the same workspace produced `discarded/2` beside a surviving
`discarded/1`.

`<n>` is `1 + the number of refs already under refs/agentloop/discarded/`
(`git for-each-ref --format=%(refname) refs/agentloop/discarded`), so repeated
rollbacks on one workspace never collide. The ref is written only when `HEAD`
does not already resolve to the rollback target — a rollback that discards
nothing records nothing.

### Part 2 — infra retry (verification only)

`loop._with_retry` already implements bounded exponential backoff, logs an
`infra_error` event per failure, converts `RunnerConfigError` to `_ConfigError`
without retry, and is invoked from the worker, validator, executor, planner and
summarizer stages. `test_infra_error_is_not_a_revision` already passes.

The gap is coverage, not behavior: the existing test proves it only for the
**worker** stage. Add tests that prove it at the **validator** and **executor**
stages too, and that backoff is actually applied (bounded, injected clock).
No production change is expected. If one turns out to be needed, it is a defect
fix, and it will be called that.

### Part 3 — batch whole-loop evaluation

`eval.py` gains a second fixture type and a second entry point. Where a verdict
fixture scores *one validator reply* against a gold `VerdictKind`, a **batch
fixture** scores *one whole task* against a gold `TaskStatus`.

A batch fixture carries the task definition (title/goal/criteria/risk), a
scripted sequence of runner replies covering every round the task will take, and
the gold final status. `run_batch_eval` drives each fixture through a real
`Loop` over a scratch in-memory store with a `MockRunner`, then reports:

- **outcome agreement** — fraction whose final `TaskStatus` matched gold;
- a **confusion matrix** over final statuses (gold rows × measured cols);
- per-fixture detail: measured status, `revision_count`, whether an escalation
  reason was produced, attempts, and tokens.

This measures what per-verdict calibration cannot: that a `revise` at 0.55
actually produces a revision round rather than an escalation, that an exhausted
revision budget lands at NEEDS_HUMAN, that a severe verdict skips the revision
loop entirely. It is a regression harness for the decision rules themselves.

### Part 4 — coverage in `test_runs`

`TestResult` gains `coverage_percent: float | None`. `executor.run` runs
`parse_coverage(combined_output)` over the output it already captures — no second
subprocess, no new timeout, no new failure mode. `store.add_test_run` persists it
into a new nullable `test_runs.coverage_percent` column, and `server.py` already
returns the whole row, so the dashboard gets it for free.

`parse_coverage` is **total** and **conservative**: it recognises the anchored
forms real coverage tools emit (`coverage.py`'s `TOTAL ... 87%` line, and a
`Total coverage: 87.5%` style line), and returns `None` on anything it is not
sure about. Ambiguity resolves toward `None`, deliberately in the opposite
direction from `_extract_findings`: over-reading findings stores stray prose that
a human can discount, while a fabricated coverage number is rendered on a
dashboard as a measurement. A wrong metric is worse than an absent one.

## Domain Glossary

| Term | Meaning |
| --- | --- |
| **workspace repo** | The git repository living inside one task's workspace directory. One per task; never shared. |
| **base ref** | `refs/agentloop/base` — the empty commit a workspace starts from. The rollback target. |
| **approved ref** | `refs/agentloop/approved` — the commit made when a task reached DONE. "Each approved task = a commit," scoped to that task's own history. |
| **discarded ref** | `refs/agentloop/discarded/<n>` — written at the pre-rollback tip *before* the reset. Without it the discarded round commits are reachable from no ref and disappear from `git log --all` (measured). Numbered from the count of refs already in the namespace, so repeated rollbacks accumulate rather than collide. |
| **rollback** | write the **discarded ref** at the current tip, then `reset --hard` to a ref plus `clean -ffdqx`, leaving the working tree at that ref's state **with the discarded work still reachable from a ref**. Distinct from a **wipe** (`rmtree`), which destroys history. History is intact *because of the discarded ref*, not because `reset` is non-destructive — it is not. |
| **verdict fixture** | An eval fixture scoring one validator reply against a gold `VerdictKind` (pre-existing). |
| **batch fixture** | An eval fixture scoring one whole task's run through the Loop against a gold `TaskStatus` (new). |

## Decisions / ADR notes

**ADR-1 — one repo per task workspace, not one repo over all of them.**
*Rejected:* a single repo at `workspace_root` where each approved task is one
commit on a shared timeline (which is what the roadmap wording literally
suggests).
*Why:* tasks share no files — each workspace is created empty and only that
task's worker writes into it — so a shared timeline would order unrelated
directories. Worse, `max_parallel_workers > 1` puts two workers on one git index
concurrently; git fails with `index.lock exists`, so it would need a global lock
serialising exactly the parallelism Slice 3 added. Per-task repos have no shared
index and are concurrency-safe by construction, matching the isolation the
workspace model already guarantees.
*Cost, stated plainly:* "roll the whole project back to before task 7" is not a
thing this buys, because there is no whole project inside the workspaces.

**ADR-2 — `reject` always rolls back to base, never to the approved ref.**
*Rejected:* "roll back to the last approved commit if one exists, else base."
*Why:* rejecting means the work is not wanted. A task that was approved and is
then rejected would, under the rejected rule, roll back to the very state being
rejected — a silent no-op wearing the name of a rollback. Base is unambiguous,
and the discarded work is still in history either way.

**ADR-3 — `redo` rolls back instead of `rmtree`, when a workspace repo exists.**
The documented contract ("a redo wipes the workspace, so 'fresh start' means it")
is about *files*, and a rollback to base leaves zero files. History surviving is
strictly additional — and it survives only because rollback writes a **discarded
ref** first (see the rollback definition above; without it the redone round is
reachable from no ref). With no repo — feature off, git missing, older workspace
— `redo` falls back to today's `clear_workspace`, unchanged.

*Corrected 2026-08-18:* `clear_workspace` as shipped is
`shutil.rmtree(ws, ignore_errors=True)`, and git writes objects read-only. On
Windows the read-only attribute blocks deletion, so the fallback measurably does
**not** wipe a workspace that contains a real repo: measured on this machine, 5
read-only files under `.git` and, after the call, the workspace directory
surviving with 12 leftover entries. The fallback therefore needs an error handler
that clears the read-only attribute and retries — verified to remove the
workspace completely — before it can carry the contract this ADR hands it.

**ADR-4 — `eval_runs` is reused with a `kind` column rather than a new table.**
The row shape is already exactly right (runner, n_fixtures, agreement, summary,
detail). `agreement` means "fraction of fixtures whose measured outcome matched
gold"; **what counts as the outcome is what `kind` says** — a verdict kind for
`kind='verdict'`, a final task status for `kind='batch'`. That is one number with
one meaning and a discriminator, not two meanings sharing a column.

**ADR-5 — coverage is parsed from output already captured, not separately run.**
*Rejected:* a `coverage_command` knob running a second subprocess per round.
*Why:* a second sandboxed subprocess doubles the timeout surface and adds a
failure path to the round, to obtain a number no decision rule reads.

## Architecture

```
config.LoopConfig
  vcs_enabled: bool = True
  vcs_command: str = "git"
  vcs_timeout_s: int = 30
        |
        v
agentloop/vcs.py  (NEW — total, never raises)
  init_repo(ws, cfg)      -> VcsResult   git init + base commit + base ref
  commit(ws, msg, cfg)    -> VcsResult   add -A; commit --allow-empty; -> sha
  mark_approved(ws, cfg)  -> VcsResult   commit + write approved ref
  rollback(ws, ref, cfg)  -> VcsResult   GUARDED discarded-ref + reset --hard + clean
  is_repo(ws)             -> bool        toplevel == ws, exactly
        ^
        |  called by (never on a decision path)
        |
    loop.py                          executor.py                store.py
      run_task: init + round commit    parse_coverage()           test_runs.coverage_percent
      DONE:     mark_approved          TestResult.coverage_percent  eval_runs.kind
      human_redo / human_reject:       skip .git in _has_any_file
                rollback(base)
```

## Data Flow

1. `run_task` creates the workspace → `vcs.init_repo` → base commit + base ref.
   Failure logs `vcs_unavailable` and sets a per-task "no repo" fact; the loop
   proceeds identically to today.
2. Worker round returns output → `vcs.commit(ws, f"round {n}")` → `vcs_commit`
   event carrying `{sha, round}`.
3. Tests run → `executor.run` captures output → `parse_coverage` → `TestResult`
   → `store.add_test_run` writes the column and the existing `test_run` event.
4. Task reaches DONE → `vcs.mark_approved` → `vcs_commit` event with
   `{sha, ref: "approved"}`.
5. `human_redo` / `human_reject` → `vcs.rollback(ws, base)` → `vcs_rollback`
   event with `{ref, sha, files_removed}`; on no repo, redo falls back to
   `clear_workspace` and reject does nothing (today's behavior).
6. `agentloop eval --mode batch` → `run_batch_eval` → scratch Loop per fixture →
   one `eval_runs` row with `kind='batch'` → `eval_run` event.

## Error Handling

Every `vcs.py` entry point returns `VcsResult(ok, sha, stderr, reason)` and never
raises. Callers ignore the result except to log it. The three failure classes:

| Failure | Handling |
| --- | --- |
| `git` not installed (`FileNotFoundError`) | `vcs_unavailable` event once per task, `RuntimeWarning`, feature inert for that task |
| a git command exits non-zero | `VcsResult(ok=False, stderr=...)`, logged, loop continues |
| the toplevel guard refuses | `VcsResult(ok=False, reason='not-a-workspace-repo')`, logged, **nothing destructive runs** |

**Three defects found by the doubt pass, each now a hard requirement:**

**D-1 (critical) — a destructive git command must never be able to reach the
user's real repository.** Verified live: inside `.agentloop/ws/probe-7/` with no
`.git` of its own, `git rev-parse --show-toplevel` returns
`C:/Coding/Projects/Agents-Workflow` — the project repo. `.agentloop/` is
gitignored, so `git clean -fdx` there would run against the project's working
tree with ignore rules disabled. Therefore `rollback` **must** first run
`git rev-parse --show-toplevel` and refuse unless it resolves to exactly the
workspace path (realpath-compared, case-insensitively on Windows). This is
mandatory, not defensive, and it needs a test whose control is a workspace with
no `.git` — that call must refuse and remove nothing.

**D-2 — `git commit` fails when no author identity is configured.** Verified
live: under a scrubbed environment `git commit` returns
`Author identity unknown … Please tell me who you are`. The workspace child env
is scrubbed by design, and CI commonly has no global identity. Every commit must
therefore pass identity explicitly on the command line
(`git -c user.name=agentloop -c user.email=agentloop@localhost commit …`) rather
than relying on ambient config. Never write into the user's global git config.

**D-2b (added 2026-08-18 after a planning review; measured, not reasoned) — the
operator's *global* git config is still read, and it can both fail the commit and
write files outside the workspace.** `HOME` and `USERPROFILE` are on
`executor._BASE_ENV_ALLOWLIST`, and `GIT_CONFIG_NOSYSTEM` suppresses only the
*system* config, so `~/.gitconfig` stays live. Measured in a scratch repo with a
planted `~/.gitconfig`: with `GIT_CONFIG_NOSYSTEM=1` alone the commit **failed**
(`gpg failed to sign the data`, exit 128) under `commit.gpgsign=true` — which in
production means every commit fails, or blocks on pinentry until `vcs_timeout_s`
— and the operator's `core.hooksPath` pre-commit hook **ran and created a file
outside the workspace**, directly falsifying "no file outside `workspace` is
created, modified or deleted". Two independent fixes were each measured to close
both channels, and both are adopted:

- **`GIT_CONFIG_GLOBAL=os.devnull`** in the child env (`nul` on Windows,
  `/dev/null` on POSIX; git ≥ 2.32), alongside `GIT_CONFIG_NOSYSTEM=1`. Measured:
  commit exit 0, hook did not run, `git config --get commit.gpgsign` finds
  nothing. `os.devnull` rather than a nonexistent path because it is never
  agent-writable.
- **`-c commit.gpgsign=false -c core.hooksPath=`** on every invocation, and
  `-c init.templateDir=` additionally on `git init` (measured: `.git/hooks` is
  then not created at all). This is what carries the guarantee on a git older
  than 2.32, where `GIT_CONFIG_GLOBAL` is ignored.

**D-3 — `.git/` makes an empty workspace look non-empty.**
`executor._has_any_file` uses `rglob("*")`, which counts files under `.git/`. A
workspace with a repo but no worker output would stop returning
`status='na', summary='Workspace is empty — nothing to test.'` and would instead
run the test command against nothing. `_has_any_file` must skip `.git`. This is
the one place Part 1 could have silently changed the tests gate — the one thing
this slice promised not to touch.

## Testing Strategy

`verification_rigor: critical_path` — irreversible filesystem operations, a
state machine, and concurrency.

Acceptance tests named in the slice:

1. **rollback-on-reject** — task runs, worker writes a file, `human_reject`,
   assert the workspace is empty **and** the discarded file is recoverable from
   git history (`git show`) **and the discarded round commit is still listed by
   `git log --all`**. The `git show` half alone is *not* sufficient: it passes
   against an orphaned commit, which is exactly the state the pre-review design
   produced (measured). Reachability from a ref is the property; `git show` by
   sha is only a symptom of it.
2. **retry-not-counted-as-revision** — extended to the validator and executor
   stages, not only the worker.
3. **a batch eval report row** — `run_batch_eval` produces exactly one
   `eval_runs` row with `kind='batch'`, a reconciling `n_fixtures`, and an
   `agreement` consistent with the per-fixture detail.

Additional required tests:

4. **D-1 control** — `rollback` on a workspace with no `.git` refuses, removes
   nothing, and the project repo is untouched. Non-vacuous: the same call on a
   real workspace repo does remove files.
5. **D-2** — a commit succeeds with no ambient git identity available, **and
   with a hostile global config present**: a planted `~/.gitconfig` carrying
   `commit.gpgsign=true` and a `core.hooksPath` hook that writes a canary file
   outside the workspace must leave the commit at exit 0 and the canary absent.
   Testing the identity under an environment production never has (no `HOME`)
   would leave the production path untested — `HOME`/`USERPROFILE` are on the
   executor's allowlist and do reach the child.
6. **D-3** — a workspace holding only `.git` still reports `status='na'`.
7. **git-missing degradation** — `vcs_command` pointed at a nonexistent binary:
   the whole suite of loop behaviors is unchanged, one `vcs_unavailable` event.
8. **behavioral inertness differential** — the project's own standard from
   `patterns.md`: run identical scripts with `vcs_enabled` True and False and
   diff the full observable state (status, `revision_count`, every verdict
   column, attempt count, tokens, event-kind sequence excluding `vcs_*`). They
   must be identical.
9. **redo keeps history** — after redo the workspace is empty and the previous
   round's commit is still listed by `git log --all` (via its discarded ref),
   not merely fetchable by sha.
10. **coverage parsing** — a table of real coverage-tool outputs plus adversarial
    near-misses (`87% of tests passed`) that must yield `None`.
11. **parallel safety** — two tasks at `max_parallel_workers=2` each get their
    own repo and neither sees the other's commits.

## Observability

Three new event kinds, all `task_id`-scoped: `vcs_commit`, `vcs_rollback`,
`vcs_unavailable`. `eval_run` gains `kind` in its payload. No new table.

`vcs_rollback`'s `files_removed` is a **pre-count minus post-count** of
non-`.git` files, not a parse of git's output: `clean -q` suppresses the listing
and `reset --hard` removes tracked files before `clean` ever runs, so nothing git
prints could produce that number.

## Questions Resolved

- *Where does the repo live?* Per task workspace (ADR-1) — user decision.
- *How is coverage measured?* Parsed from output already captured (ADR-5) —
  user decision.
- *What does rollback target on reject?* Base, always (ADR-2).
- *Does redo still wipe?* Files yes, history no (ADR-3).
- *New table for batch eval?* No — `eval_runs` + `kind` (ADR-4).
- *Is Part 2 new code?* No. Verification and test coverage only.

### Brainstorming Handoff (MACHINE-READABLE)

```yaml
DESIGN_FILE: "C:/Coding/Projects/Agents-Workflow/docs/plans/2026-08-18-slice-6-durability-eval-design.md"
DESIGN_SUMMARY: "Per-task git workspace repos give agentloop recoverable redo/reject rollback, plus batch whole-loop eval, coverage in test_runs, and verification of the already-shipped infra retry — none of it readable by any decision rule."
MEMORY_NOTES:
  glossary:
    - term: "workspace repo"
      meaning: "The git repository inside one task's workspace directory. One per task, never shared, concurrency-safe by construction."
    - term: "rollback vs wipe"
      meaning: "Rollback = reset --hard + clean, history intact. Wipe = rmtree, history destroyed. Slice 6 converts redo/reject from wipe to rollback."
  decisions:
    - decision: "One git repo per task workspace"
      rejected: "One repo at workspace_root with a shared commit timeline"
      why: "Tasks share no files, and max_parallel_workers>1 would put two workers on one git index (index.lock), needing a global lock that serialises exactly the parallelism Slice 3 added."
    - decision: "reject always rolls back to the base ref"
      rejected: "Roll back to the last approved commit if one exists"
      why: "An approved-then-rejected task would roll back to the state being rejected — a no-op named 'rollback'."
    - decision: "Coverage parsed from output already captured"
      rejected: "A coverage_command knob running a second subprocess per round"
      why: "Doubles the timeout surface and adds a loop failure path for a number no decision rule reads."
    - decision: "eval_runs reused with a kind column"
      rejected: "A separate batch_eval_runs table"
      why: "Row shape is already correct; kind discriminates what 'agreement' is the agreement of."
  gotchas:
    - "A workspace with no .git resolves `git rev-parse --show-toplevel` to the PROJECT repo — any destructive git command there targets the user's real working tree. Verified live. Guard is mandatory."
    - "`git commit` fails with 'Author identity unknown' under a scrubbed env; pass -c user.name/-c user.email explicitly, never rely on ambient config."
    - "executor._has_any_file counts files under .git/, so a bare repo would flip an empty workspace from 'na' to running tests."
    - "`git reset --hard <ref>` ORPHANS the commits it moves off: with only base/approved refs present, `git log --all` no longer lists them (measured). Rollback must write `refs/agentloop/discarded/<n>` at the tip FIRST. `git show <sha>` still works on an orphan, so a test asserting only recoverability-by-sha passes against a broken implementation."
    - "GIT_CONFIG_NOSYSTEM suppresses only the SYSTEM config. HOME/USERPROFILE are on the executor allowlist, so ~/.gitconfig stays live: commit.gpgsign=true fails every commit (measured, exit 128) and core.hooksPath runs the operator's hooks inside the workspace (measured, wrote a file outside it). Set GIT_CONFIG_GLOBAL=os.devnull and pin -c commit.gpgsign=false -c core.hooksPath= (+ -c init.templateDir= on init)."
    - "clear_workspace is rmtree(ignore_errors=True); git writes objects read-only, so on Windows it silently leaves the workspace behind (measured: 12 leftover entries). The redo fallback needs an onexc handler that chmods and retries."
```
