## Slice 9: existing-repository workspaces

`executor.workspace_for` did `mkdir` and returned; nothing ever put the
operator's code in a task's workspace, and `ClaudeSDKRunner.build_options`
never passed `cwd` even though the SDK accepts it — so the worker's prompt
said "write under the workspace" while its actual working directory was the
orchestrator's own repository. Measured consequence: the tests gate silently
disabled itself, because an empty workspace makes `executor._has_any_file`
return False, the result is `status='na'`, and `na` is the one value that
falls back to the validator's unverified `TESTS:` claim — the single gate
that reads executed truth stopped reading anything. Three phases (P1-P3) built
the pieces with `loop.py` either untouched or touched only for the
scratch-mode-reachable half (P1's `cwd` fix, P2's H3 detection scaffold); this
phase (P4) is what turns worktree mode on by threading `repo_root` through
every `vcs.*` call site `loop.py` already had.

**Approach: one git worktree per task**, created on its own branch from the
configured base ref, sharing the object store — chosen over a clone because it
does not copy history and the result is already a branch of the operator's
repository, no export step. Measured on this repository to size the trade-off:
`.git` itself is 3.1 MB, tracked files are 1.7 MB, and the full working
directory is 441 MB — the difference being `.venv`/`node_modules`, both
untracked, which a worktree never materializes. Worktree cost is the tracked
checkout only, not the working directory's full footprint.

A separate design question — put the worktree *inside* `repo_root` or
entirely outside it — was settled by measuring both. Inside, a worktree left
`?? .agentloop/` in `git status` for the main repo, cleanable only by adding
an exclusion entry to `.git/info/exclude` (a local, untracked file, so it
doesn't touch the tracked `.gitignore`). Outside — a worktree created at a
path off the repo tree entirely, which is what `worktree_root` defaulting
outside `repo_root` gives — the main repo stayed clean with **no exclusion
mechanism needed at all**, and git's own reported common-dir was unaffected
by the move. The slice took the outside option, which deletes the
`info/exclude` mechanism from the design rather than making it optional:
fewer moving parts is the smaller half of the reason, the larger half being
that a workspace inside the repo would turn `executor.py`'s already-documented
path-escape bug into a write on the operator's real working tree — see
`worktree_root`'s entry in `CLAUDE.md`.

`workspace_mode: "scratch"` (default) is
unchanged and is a **proven** no-op in the register of `vcs_enabled=False`: a
whole-state differential
(`test_scratch_mode_is_identical_whether_or_not_worktree_knobs_are_set`,
[NEUTER]) runs the identical scripted task with every worktree knob
(`repo_root`/`worktree_root`/`vcs_base_ref`/`vcs_branch_prefix`) filled in with
real, reachable values against a run with none of them set, and diffs the
whole observable state — plus a control proving the same knobs, with
`workspace_mode` flipped to `'worktree'`, genuinely touch the operator repo
(a branch is created), so the equality above is a real differential and not
two runs that both ignored everything.

**Four measured probes shaped the design** (`vcs.py`'s own docstrings carry
the detail; this is the summary). Worktrees of one repository share **one ref
namespace** — an update to the base ref written inside one worktree is
immediately visible from every sibling and the main repo — so refs are now
per-task functions (`vcs.base_ref(task_id)`, `vcs.approved_ref(task_id)`,
`vcs.discarded_ref_prefix(task_id)`), constants only when `task_id` is `None`
(scratch mode). The local config setting command run from inside a worktree
writes the **main** repository's config, not a config under the worktree at
all — so the config pin fingerprints `<repo_root>/.git/config`, and because
every task of one repository shares that one file, P2's remediation made it a
**repository-level baseline** (`Store.vcs_repo_pin`/`vcs_repo_pins`, keyed by
`repo_root`) rather than a per-task mint: a per-task pin let an ordinary
later task's `init_repo` re-bless a config an earlier task had poisoned,
measured as arbitrary command execution outside every workspace under
`allow_test_exec=False`. A worktree's `.git` is a regular **file** (a
`gitdir:` pointer), not a directory, which defeats every one of the
scratch-mode guard's conditions outright — `_guard` therefore grows a second,
worktree-shaped branch (`_guard_worktree`) rather than a patch to the first,
identity-anchored on `repo_root` (operator config, never agent-writable): HEAD
must resolve to the derived task branch (a worker's own `git` tool can
otherwise move the operator's real branch, unguarded by anything the first
four conditions check), and the worktree's admin directory is verified by
git's own back-pointer rather than by containment in a shared parent
(containment there repeated slice 6's own prior escape one directory up — a
sibling task's worktree already existing was enough to let task 2 hijack task
1's real branch). A workspace **outside** the repository needs no exclusion
mechanism at all (the main repo's status stayed clean with a worktree
elsewhere on disk, measured), which is the *smaller* half of why worktrees
live outside `repo_root`; the larger half is residual 2 below.

**Base is the worktree's starting commit, never an empty one** — the single
most dangerous difference from scratch mode, where base is deliberately empty
because a rollback must not resurrect whatever a throwaway directory happened
to hold. Here the same choice would delete the operator's entire checkout on
the task branch, so `rollback` returning to the starting commit *is* the
recovery contract rather than a detail of it. `commit` drops the
force-add flag in worktree mode: force-adding ignored files is what makes a
scratch rollback fully recoverable, but here it would commit `node_modules`/
`.venv` into the task branch every round — the cost is real and named per
rollback (`ignored_unrecoverable`), never hidden. `remove_worktree` removes
the checkout **then** prunes the admin entry, never a bare `rmtree`, because
deleting the directory alone leaves the admin entry under
`<repo_root>/.git/worktrees/<name>` registered, and a listing keeps reporting
a workspace that is gone.

**P4: threading `repo_root` through `loop.py`.** `Loop._worktree_repo_root()`
computes the mode once per call (`None` in scratch mode, the absolute
`repo_root` in worktree mode — `os.path.abspath`, lexically, matching
`vcs._git`'s own `-C` handling, not `Path.resolve()`, which would follow a
junction and answer a question about a different repository than the one
`config.repo_root` names) and every one of `run_task`'s existing `vcs.*` call
sites reads the same answer rather than re-deriving it, so scratch mode
staying a **proven** no-op does not depend on getting the same derivation
right in six different places. `Loop._worktree_pin(task_id, repo_root)`
chooses the matching pin table (`Store.vcs_repo_pin` in worktree mode,
`Store.vcs_pin` in scratch mode) the same way. The AST guard's own inventory
grew from 6 to 12 `vcs.*` call expressions in this phase, across 10 distinct
names — a third `working_tree_state` was **not** added for residual 2's
snapshot (see below); a second `init_repo` (`human_redo`'s worktree fresh
start), `remove_worktree`/`remove_task_branch` (also `human_redo`), and
`base_ref`/`discarded_ref_prefix` (`_vcs_rollback_to_base` now selects the
per-task ref names instead of reading the scratch-mode module constants
directly) account for the rest, and the control that asserts the count is
changed in the same commit as the count itself, per this project's own
standing rule. **No new decision rule**: every one of these calls stays
*call, log, discard*, and the AST guard
(`test_no_status_write_is_downstream_of_a_vcs_result`) still walks `loop.py`
and fails if a `VcsResult` ever reaches a status write — extended with two
more planted violations in the new call shapes (a `repo_root=`/`task_id=`
call assigned to a tainted name, and the same shape reaching a status write
through `_vcs_rollback_to_base`) to prove the walker catches the worktree
shape and not only the scratch one it was built against.

**`run_planner` gets `cwd=repo_root`** (read-only: the planner declares
`file_read`, not `file_io`) in worktree mode and `None` in scratch mode — a
plan row has no task workspace to point it at, which is why this seam was left
open rather than filled in P1 alongside the worker/validator.

**A worktree survives `DONE`.** The loop never removes one on success — settled
in interview, and the opposite of `_clear_worktree`'s pre-existing,
unrelated-by-coincidence name (that helper is the *scratch*-mode redo residue
fallback, predating this slice, and still only ever called on a `.git`
*directory*). Removal happens only on `human_redo` and the explicit
`agentloop workspace prune`. **`human_redo`'s worktree-mode fresh start is
"remove and recreate"** the checkout, not reset-in-place:
`_vcs_rollback_to_base` still runs first and, in the shape that actually
happened, writes the per-task discarded ref at whatever the branch's prior
tip was (or records nothing when there was nothing to discard) —
recreation only follows *after* that history is safely reachable. **Not
unconditionally** (round-4 remediation, found independently by two
mutually-blind reviewers via two different attacks — one monkeypatching the
ref write, one a real git ref-namespace collision needing no monkeypatch at
all): the remove-and-recreate step is gated on
`result.ok and result.reason != "residue"`, the same discriminator the
scratch-mode fallback already used correctly a few lines below it. A
rollback that fails outright, or a genuine git-level failure such as the
discarded ref's own path being pre-occupied by an ordinary ref, returns
`ok=False` with **nothing** written — the earlier unconditional version
still deleted the branch in that case, permanently orphaning the round's
commits the moment an ordinary `git gc` ran, since `git show <sha>`
resolving an orphan proves nothing about survivability. When the gate is not
met, the worktree and its branch are left exactly as they are — the
`vcs_unavailable`/degraded `vcs_rollback` event `_vcs_rollback_to_base`
already logs is the record — and a later run retries. This closes the gap
P2 explicitly deferred:
`init_repo`'s worktree branch always creates without allowing an overwrite of
an existing branch of the same name (a branch that already exists means a
previous incarnation of the task, and force-resetting it would discard
commits no human asked to discard), so a *second* redo of the same task hit
that refusal outright — measured, the worktree-add step failing because the
derived branch name already existed, and falsified by neutering the fix
(removing the branch-delete call reproduces exactly that failure text). The
decision, per the reasoning the plan asked for verified rather than assumed:
**delete the stale branch first**. `human_redo`'s whole contract is a fresh
start with no carried-over context, and by the time the branch is deleted its
commits are already reachable from the discarded ref the rollback wrote one
step earlier (reachability, never the single-commit inspection command the
project's history already warns is insufficient — the same rule slice 6
established, since an orphaned commit still resolves under a direct lookup) —
so deleting the branch *name* costs nothing recoverable, only the name the
next worktree-add needs to reuse. New `vcs.remove_task_branch` (pinned like
every other repo-level mutation) is the one function this phase added to
`vcs.py` beyond threading existing ones.

**Residual 2, measured both ways rather than assumed** (tests 27-29). The
executor sandbox's already-documented `..`/absolute-path escape gets a far
worse target under worktree mode — the operator's real checkout instead of a
throwaway directory — and putting `worktree_root` outside `repo_root`
(enforced at config load, `LoopConfig.__post_init__`) only *reduces* it:
relative traversal from the workspace now has to climb out of an
agentloop-owned directory first (measured: the identical relative traversal
that reaches the operator's real repo when `worktree_root` sits inside it
lands inside `worktree_root` instead when it sits outside), but an
**absolute** path reaches the repository regardless — a test written to PASS
*because the write succeeds*, with a comment saying so, so a later isolation
slice closing this is a visible flip rather than a silent one. Detection is
the second, independent leg: a status snapshot of `repo_root` itself (not the
task workspace) is taken before and after each test command runs, and a
difference is logged as `worktree_out_of_branch_write` with
`prevented: False` — the same H3 posture `_vcs_detect_validator_writes`
already established, reused rather than duplicated. This needed a **new**
narrow entry point, `vcs.repo_status(repo_root, config, pin)`, rather than
calling `vcs.working_tree_state(repo_root, ...)` directly: measured, both of
`_guard`'s existing shapes refuse `repo_root` with `not-a-workspace-repo` —
the scratch branch requires the resolved workspace to sit inside
`config.workspace_root`, which the operator's own repository is not, and the
worktree branch requires the workspace's own git pointer to be a file, which
`repo_root`'s is not (it is the ordinary directory every worktree's pointer
points back to). `repo_status` is pinned like its siblings (it still spawns
git under `<repo_root>/.git/config`) but skips `_guard` entirely, because
`repo_root` is the one path in this whole module that is operator config read
directly, never a workspace an agent's own tools resolve into.

**`sandbox_env_allowlist` can re-admit a credential-shaped variable with no
denylist** (test 31) — true since slice 8 shipped the allowlist, tolerable
while the sandbox barely ran; not once worktree mode runs the operator's real
suite every round and a real suite has a real reason to widen the knob
(`DATABASE_URL`, a service token). `executor.credential_like_names` matches a
small, explicitly-documented-as-a-heuristic pattern set (`*_API_KEY`,
`*_TOKEN`, `*_SECRET`, `*_PASSWORD`, `AWS_*`) and `Loop.__init__` logs a
`config_warning` event naming any match, on the same channel the unknown-key
warning already uses — **a warning, not a refusal**: an operator may
genuinely need a provider key in a real test suite, and this project's rule
(stated once already, for the tool gate) is that an unjustifiable refusal
becomes a knob someone disables. The matched variable is still actually
admitted to the child process — the warning does not become a silent removal.

**`agentloop status <id>` prints the task's workspace path** — the operator
half of the trade residual 4 names (a worktree living outside `repo_root` is
no longer somewhere an operator finds by looking beside their project
directory), the other half being `agentloop workspace prune`, already built
in P3.

**Residuals restated from the plan, not solved:**

1. A worker can write the operator's *real* local git config from inside the
   worktree. The pin detects this before the next side-effecting call and
   refuses (`config-changed`); detection is not prevention, and `agentloop
   workspace rebless` (P3) is the human-only recovery for a *legitimate* edit,
   never an automatic one.
2. The executor sandbox's escape is reduced, not closed — see above.
3. Ignored files are not recoverable after a worktree rollback (the dropped
   force-add), named per-rollback (`ignored_unrecoverable`).
4. Workspaces are no longer visible beside the repository they work on —
   mitigated, not removed, by `agentloop status` and `agentloop workspace
   prune`.
5. Merge conflicts between two tasks' branches are the operator's problem;
   the planner's DAG expresses ordering, not file-level disjointness.

**What was verified rather than assumed**, per the [RED-FIRST]/[NEUTER]
discipline this whole slice was built under: the workspace-starts-with-
tracked-files claim (test 15) was watched failing against the pre-P4 tree —
a genuine behavioral RED (a missing tracked file), not a collection error —
before `run_task`'s call sites were wired; the branch-collision fix was
falsified by removing the branch-delete call and reproducing the exact
worktree-add failure text; the out-of-branch detection event was falsified by
removing its one call site and confirming the assertion goes red with zero
events recorded; and the AST guard's own extension was pinned by two new
planted violations rather than trusted to generalize from the scratch-mode
ones. **No decision rule changed** — the thresholds, revision counting, the
budget cap, the tests gate and the tool gate all read the same fields they
did before slice 8; what changed is that the loop can now be pointed at a
real repository at all.
