"""Per-task workspace version control (roadmap slice 6, Part 1).

Every task workspace is its own throwaway git repository, so `redo` and
`reject` *recover* the discarded work instead of destroying it. There is no
shared timeline and no remote: one repo per `.agentloop/ws/task-{id}/`, created
by the loop, never seen by a human unless they go looking.

**Slice 9 adds a second legal repository shape, and it is a second shape rather
than a widened first one.** In *worktree* mode — selected per call by passing
`repo_root=`, never by a module-level flag — the workspace is a `git worktree`
of the operator's own repository on branch `agentloop/task-<id>`, so the task
starts from their code instead of an empty directory. Four things differ, and
each was measured before it was written rather than assumed:

- **`_guard` gets its own branch** (`_guard_worktree`). A worktree's `.git` is
  a regular *file*, its `--absolute-git-dir` is
  `<repo_root>/.git/worktrees/<name>` and its `--git-common-dir` is
  `<repo_root>/.git`, so **every** scratch condition fails on the shape. The
  discipline is unchanged — four conditions, all by identity after resolution,
  all fail-closed, any error refuses — but the anchor moves from `<ws>` (inside
  the agent-writable workspace) to `repo_root` (operator config), which is
  strictly the stronger anchor.
- **The pin fingerprints the *common* config.** Measured: `git config --local`
  run from inside a worktree writes `<repo_root>/.git/config`, which is where
  `filter.<name>.clean` and `core.fsmonitor` — programs git runs on an ordinary
  `add` — would land. `<ws>/.git` is a file and has no config to fingerprint.
  The escalation this implies is real and is named in the plan's residuals: a
  worker can write the operator's actual `.git/config`. The pin *detects* that
  before the next side-effecting call and refuses; detection is not prevention.
- **Refs are per task** (`base_ref`/`approved_ref`/`discarded_ref_prefix`).
  Worktrees share one ref namespace, so the fixed names that cannot collide
  when every task owns a repository collide on *every* task here.
- **Base is the starting commit and the round commit does not force ignored
  files in.** Both are recovery-contract changes, named on the result
  (`ignored_unrecoverable`) rather than argued away.

`init_repo`'s worktree parameters (`repo_root`, `task_id`, `start_ref`,
`branch_prefix`) are **arguments, not config**: P3 of slice 9 is what reads
`workspace_mode`/`repo_root`/`vcs_base_ref`/`vcs_branch_prefix` out of
`LoopConfig` and passes them down, so the mode decision lives in one place.
A call that passes no `repo_root` is the pre-slice-9 module exactly.

Threat model — and the reason this is not in `executor.py`. `executor.py` runs
**arbitrary AI-generated code**; this module runs its own fixed commands, but
it runs them *inside a directory that code writes to*, so both axes are in
scope. **Aim**: in a workspace that is not a repository, `git rev-parse
--show-toplevel` walks up and resolves to the enclosing project repository, so
an unguarded `git add -A && git commit` would stage and commit the operator's
entire working tree, and an unguarded `reset --hard` / `clean` would destroy
it. **Execution**: a fixed argv is not a fixed program list — `.git/config`
lives inside the workspace, and git config names programs git runs
(`filter.<name>.clean`, `core.fsmonitor`, and more), so a worker writing only
inside its own workspace could make an ordinary round commit execute its code
outside it. Measured, before the config pin below existed: guard True before
and after the edit, `commit` ok=True, canary written outside the workspace,
on `allow_test_exec=False` — the operator's explicit "execute nothing the
agent wrote" switch. The defenses:

- **Containment.** No command with a side effect runs unless every one of
  `_guard`'s conditions holds: `workspace/.git` is a directory, git's own
  toplevel for that cwd *is* the workspace, the workspace lies inside
  `config.workspace_root`, and **the resolved location of `workspace/.git`
  does too**. The fourth is not a restatement of the third: git derives the
  worktree from the directory *containing* `.git`, not from where `.git`
  resolves to, so a junction or symlink at `<ws>/.git` pointing at the
  operator's real repository reports the workspace as the toplevel and
  satisfies the first three — measured, with the victim's branch pointer
  rewritten off its own work. Any error while computing any of them refuses —
  a guard that errors is a guard that says no.

  **And then it asks git where it actually is.** Those are all tests of
  filesystem *paths*, which is a guess at what git will make of them, and
  `.git` sits inside the agent-writable workspace — so a single ordinary file
  write to `.git/commondir` (no junction, no gitfile, no environment variable)
  redirected every ref and object write into the operator's repository with all
  four path conditions holding: measured, guard True, `commit` ok=True, victim
  HEAD rewritten. A fifth path check is the same mistake one level deeper, so
  the guard's `rev-parse` asks for `--show-toplevel --absolute-git-dir
  --git-common-dir` and requires both resolved git locations to **be
  `<workspace>/.git` itself**. Identity, not containment in `workspace_root`:
  containment pins a *region*, and `config_pin` fingerprints
  `<ws>/.git/config` while git reads `config` from the **common dir**, so a
  commondir aimed at a sibling directory *inside the workspace* satisfied
  containment, left the pinned file byte-identical and owned the config git
  parses (measured: pin unchanged, guard True, commit ok=True, canary written
  outside the workspace). Containment also let `<ws>/.git` resolve into a
  **sibling task's** workspace, aiming one task's `reset --hard` /
  `clean -ffdqx` at another task's repository. Reading the location git
  resolved rather than the one the filesystem suggests, and requiring it to be
  the one location this module created, closes commondir, gitfiles, `GIT_DIR`,
  worktree links and the next redirect at once.
- **The workspace's own git config cannot act on our behalf either.** The two
  containment escapes above both redirected *where git points*; this axis is
  *what git runs*, and no denylist can close it — a filter name is arbitrary,
  so there is no key to enumerate. So the config is **pinned**: `init_repo`
  fingerprints the `.git/config` that `git init` itself wrote and hands that
  fingerprint back, the caller records it where an agent has no write path
  (the `Store`), and every side-effecting entry point replays it and refuses on
  any mismatch, unreadable config or missing pin (`reason="config-changed"` /
  `"config-unpinned"`). An allowlist of exactly one value. It is checked
  *before any subprocess* — a bounded file read and a hash — so it is cheap
  enough for the per-round hot path and nothing runs under a config we did not
  write. Git was measured never to rewrite `.git/config` during any command
  this module issues (`init`, `add`, `commit`, `update-ref`, `status`,
  `rev-parse`, `reset --hard`, `clean`), so a change is always somebody else's.
- **The operator's git config cannot act on our behalf.** `GIT_CONFIG_NOSYSTEM`
  suppresses only the *system* config, and `HOME`/`USERPROFILE` legitimately
  reach the child, so `~/.gitconfig` stays live: an ordinary `commit.gpgsign`
  fails every commit, and a `core.hooksPath` pre-commit hook runs *inside* the
  workspace and can write outside it. Both channels are closed twice over —
  `GIT_CONFIG_GLOBAL=os.devnull` in the env (git >= 2.32) and `-c` pins on
  every invocation (below that floor).
- **Never raises.** Every entry point is total: `init_repo`, `commit`,
  `mark_approved` and `rollback` always return a JSON-encodable `VcsResult`,
  and `is_repo` always returns a `bool`. Durability must never fail an attempt,
  so a caller may ignore the result entirely.
- argv as a list, `shell=False`, cwd pinned (plus `-C` for defence in depth),
  `timeout=config.vcs_timeout_s`, child env scrubbed to this module's own
  allowlist, stderr bounded to `_MAX_STDERR_CHARS` before it can reach an event
  payload.

Residual gap in the recovery surface — nested repositories. A round commit
stages ignored files too (`add -A -f`), so everything `clean -ffdqx` deletes is
reachable from `refs/agentloop/discarded/<sha>` afterwards. One thing is not:
a repository the worker created *inside* its own workspace is recorded as a
bare gitlink (`160000 commit <sha> sub`) and its objects are not carried, so
the rollback deletes them unrecoverably. Git cannot nest repositories this way
and no flag changes that. What this module does instead is refuse to overclaim:
`VcsResult.nested_repos` names every such directory found before the reset, and
the loop's `vcs_rollback` event carries the names rather than asserting a
recovery surface that does not hold the work. In the register of `executor.py`'s
"Residual risk in the env-scrub tier" — stated, not designed away. Beside it:
`clean -d` also removes untracked **empty** directories, which no commit can
capture in the first place — git tracks files, not directories.

Residual in the same register, on the other side of the guard — `.git` lives
*inside* the agent-writable workspace, so the repository is as writable as the
work is. A worker that deletes `.git/refs/agentloop/base` leaves a repo whose
guard still passes, so `init_repo`'s fast path correctly falls through and
writes a **new** base on top of whatever that worker's HEAD is — silently
neutering rollback for that task, since `reset --hard base` then returns to the
worker's own state. Asking git for its resolved locations does not fix this and
no further guard is proposed for it: it is named here rather than designed away.

Not covered: two concurrent calls on the *same* workspace. `human_redo` and
`human_reject` take no claim and are reachable from the dashboard mid-round, so
a rollback can overlap a commit; both racers target the same workspace, so the
outcome is a lost commit or an `index.lock` surfacing as `reason="git-failed"`
— total and logged, never an escape of containment. An accepted residual in the
register of `sandbox_isolation='strict'`.
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from .config import LoopConfig

BASE_REF = "refs/agentloop/base"
APPROVED_REF = "refs/agentloop/approved"
# The ref is named for the sha it saves, not for a counter: a counter is a
# read-then-write two rollbacks on one workspace could collide on, and
# `update-ref` overwrites without error — a silent loss of exactly the history
# this ref exists to keep, reported as ok=True.
DISCARDED_REF_PREFIX = "refs/agentloop/discarded"

# Worktree mode's branch names. `<prefix><task id>`, derived and never stored,
# so there is no schema migration and no second source of truth for it.
DEFAULT_BRANCH_PREFIX = "agentloop/task-"


def _task_ns(task_id: int | None) -> str:
    return f"refs/agentloop/task-{task_id}"


def base_ref(task_id: int | None = None) -> str:
    """The ref a rollback returns to. Per task in worktree mode; the shared
    constant in scratch mode, where every task owns its own repository and the
    fixed name cannot collide.

    Measured (slice 9, probe 1): worktrees share **one** ref namespace, and a
    `update-ref refs/agentloop/base` written inside one is immediately visible
    from the main repo and from every sibling worktree. With the fixed name,
    task 2's `init_repo` would overwrite task 1's base and task 1's rollback
    would reset onto task 2's starting commit."""
    return BASE_REF if task_id is None else f"{_task_ns(task_id)}/base"


def approved_ref(task_id: int | None = None) -> str:
    """The bookmark a human's approval leaves behind. Per task in worktree
    mode, for the reason `base_ref` documents."""
    return APPROVED_REF if task_id is None else f"{_task_ns(task_id)}/approved"


def discarded_ref_prefix(task_id: int | None = None) -> str:
    """Where a rollback saves the tip it is about to discard. Still named for
    the sha rather than a counter — the namespacing changes *whose* history it
    is, not how the ref is named."""
    return DISCARDED_REF_PREFIX if task_id is None else f"{_task_ns(task_id)}/discarded"


# Bounded before it can reach an event payload: telemetry must never fail an
# attempt, and `log_event` has one `json.dumps` for every payload in the system.
_MAX_STDERR_CHARS = 500

# Identity per invocation. The child env is scrubbed and the global config is
# neutralised, so git has no identity to find and would fail with "Author
# identity unknown". Never `git config --global` — nothing outside the
# workspace is ever written.
_IDENTITY = (
    "-c",
    "user.name=agentloop",
    "-c",
    "user.email=agentloop@localhost",
)
# Neutralise the operator's own git config on the command line too, because
# GIT_CONFIG_GLOBAL needs git >= 2.32 and this must hold below that floor.
_CONFIG_PINS = ("-c", "commit.gpgsign=false", "-c", "core.hooksPath=")
_INIT_PINS = ("-c", "init.templateDir=")  # git init only

# The interpreter/OS basics git needs to start and resolve paths. Never
# `os.environ` wholesale — that carries ANTHROPIC_API_KEY. Matched
# case-insensitively because Windows env keys vary in case. HOME/USERPROFILE
# are kept, not dropped: git wants them on both platforms, and the answer to a
# hostile `~/.gitconfig` is to neutralise the config rather than to test an
# environment production never has. `config.sandbox_env_allowlist` is
# deliberately *not* admitted: that knob widens the test sandbox, and letting it
# widen this env would re-admit exactly what the pins above remove.
_GIT_ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "SYSTEMDRIVE",
    "HOMEDRIVE",
    "HOMEPATH",
    "HOME",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
)


@dataclass(frozen=True)
class VcsResult:
    """What happened. `ok=True` means the requested effect happened; `reason`
    is drawn from a closed vocabulary and is `""` exactly when nothing
    degraded: `"disabled"`, `"git-missing"`, `"no-workspace"`, `"git-failed"`,
    `"timeout"`, `"not-a-workspace-repo"`, `"config-unpinned"`,
    `"config-changed"`, `"already"`, `"residue"`, `"unrecorded"`,
    `"no-task-id"`.

    `"no-task-id"` is slice 9's one addition, and it is deliberate rather than
    convenient: worktree mode namespaces every ref by task (probe 1), so a
    worktree call with no task id has no ref to write. Falling back to the
    shared scratch constants would produce exactly the cross-task collision the
    namespacing exists to prevent, silently, so it refuses instead."""

    ok: bool
    sha: str = ""
    stderr: str = ""
    reason: str = ""
    files_removed: int = 0
    # Set by `init_repo`, and **only** when this call created the repository:
    # the fingerprint of the `.git/config` git itself just wrote. The caller
    # records it (`Store.set_vcs_pin`) and replays it on every later call. It
    # is carried on failed results too — a repo whose base commit failed still
    # has a config that needs pinning, and an unrecorded one is unpinnable
    # forever after.
    pin: str = ""
    # Workspace-relative directories holding a repository of their own, found
    # before a rollback reset. A commit records them as bare gitlinks, so their
    # contents are *not* in the discarded ref: this names the gap so no caller
    # has to claim a recovery surface that does not hold them.
    nested_repos: tuple[str, ...] = ()
    # Worktree mode only: ignored paths present before a rollback's reset. The
    # round commit there stages with `add -A` and **not** `-f` (forcing them in
    # would put `node_modules`/`.venv` — 441 MB on this repository — into the
    # operator's task branch every round), so `clean -ffdqx` deletes these and
    # no ref carries them. Same register as `nested_repos`: the gap is named
    # per-rollback rather than left for the audit log to overclaim.
    ignored_unrecoverable: tuple[str, ...] = ()
    # `working_tree_state` only: porcelain **entries** for the workspace —
    # `"<XY> <path>"`, the two-character status code and the path together,
    # bounded because they reach an event payload. The code is carried and not
    # dropped because a caller diffing two snapshots is asking "what did this
    # step write", and a path-only diff cannot see an *overwrite* of a path
    # that was already dirty (`?? x` -> ` M x`, ` M x` -> `MM x`): same name,
    # real write, invisible.
    changed_entries: tuple[str, ...] = ()
    # Which of the bounded lists above hit their cap on this call, by field
    # name. **Silence must never mean "nothing appeared".** Every list here is
    # capped so telemetry cannot flood an event payload, and a cap that does
    # not announce itself turns a detection into a quiet blind spot — over a
    # real checkout more than `_MAX_REPORTED_PATHS` changed paths is ordinary,
    # not exotic.
    truncated: tuple[str, ...] = ()


class _Run(NamedTuple):
    """One git invocation. `reason` is non-empty only when the *spawn* failed,
    which is a different thing from git exiting non-zero."""

    code: int
    out: str
    err: str
    reason: str


def _clip(text: str) -> str:
    return (text or "")[:_MAX_STDERR_CHARS]


def _child_env() -> dict[str, str]:
    """This module's own scrubbed env. Not `executor._child_env`, which is a
    *method* on `TestExecutor` (it reads `self.env_allowlist`).

    Takes no config on purpose: the one knob it could plausibly read is
    `config.sandbox_env_allowlist`, and admitting it would let an operator
    re-add exactly what this env removes. A parameter that is never read is an
    invitation to make that edit, so there is no parameter."""
    allow = {name.upper() for name in _GIT_ENV_ALLOWLIST}
    env = {k: v for k, v in os.environ.items() if k.upper() in allow}
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    # Measured: NOSYSTEM alone leaves ~/.gitconfig live. os.devnull rather than
    # a nonexistent path because it is never agent-writable.
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _run(workspace: str | Path, argv: list[str], config: LoopConfig) -> _Run:
    """Spawn one git command. Converts every failure into a `reason`; the only
    thing it lets through is a `BaseException`."""
    if not argv or not argv[0]:
        # An empty vcs_command is not a program: refuse without spawning.
        return _Run(-1, "", "", "git-missing")
    try:
        ws = Path(workspace)
        if not ws.is_dir():
            # subprocess.run raises FileNotFoundError for a nonexistent cwd
            # exactly as it does for a missing executable, so without this the
            # two are conflated and a never-created workspace tells the
            # operator to install a git they already have.
            return _Run(-1, "", "", "no-workspace")
    except Exception as exc:
        return _Run(-1, "", _clip(str(exc)), "no-workspace")

    try:
        proc = subprocess.run(
            argv,
            cwd=str(ws),
            capture_output=True,
            text=True,
            timeout=config.vcs_timeout_s,
            shell=False,  # never; argv is passed through literally
            env=_child_env(),
        )
    except FileNotFoundError as exc:
        return _Run(-1, "", _clip(str(exc)), "git-missing")
    except subprocess.TimeoutExpired as exc:
        return _Run(-1, "", _clip(str(exc)), "timeout")
    except Exception as exc:  # OSError and anything else
        return _Run(-1, "", _clip(str(exc)), "git-failed")
    return _Run(proc.returncode, proc.stdout or "", _clip(proc.stderr or ""), "")


def _git(
    config: LoopConfig, *args: str, workspace: str | Path | None = None
) -> list[str]:
    """argv for one invocation: the executable, the identity, the config pins,
    optionally `-C <ws>` (defence in depth on top of the pinned cwd).

    The `-C` value is made **absolute**, and that is load-bearing rather than
    tidy. `_run` already sets `cwd=<ws>`, so git changes into the workspace and
    only *then* resolves `-C`; a relative path is therefore resolved a second
    time from inside itself. The shipped default `workspace_root` is relative
    (`.agentloop/ws`), so before this every side-effecting call on a default
    install failed with `fatal: cannot change to '.agentloop\\ws\\task-1'` and
    the whole durability feature was inert — visible only as one
    `RuntimeWarning` per task. Every test in `test_vcs.py` used an absolute
    `tmp_path`, so none of them could see it.

    `os.path.abspath`, deliberately, not `Path.resolve()`: `abspath` is purely
    lexical, so it fixes the double-resolution without following a single link.
    `resolve()` would silently walk a junction at `<ws>`, which is exactly the
    redirection `_guard` exists to catch — the guard must be the only thing in
    this module that decides where a link may point."""
    argv = [config.vcs_command, *_IDENTITY, *_CONFIG_PINS]
    if workspace is not None:
        argv += ["-C", os.path.abspath(str(workspace))]
    return argv + list(args)


def _same_path(a, b) -> bool:
    """`normcase(realpath(...))` on both sides. `normcase` lowercases and
    normalises separators on Windows and is identity on POSIX; `realpath`
    resolves the junctions and symlinks git resolves and a raw string compare
    would not. Any error refuses."""
    try:
        return os.path.normcase(os.path.realpath(str(a))) == os.path.normcase(
            os.path.realpath(str(b))
        )
    except Exception:
        return False


def _is_within(child, parent) -> bool:
    """Whether the realpath'd child lies inside the realpath'd parent. Returns
    False on any exception — a mixed-drive `ValueError`, an OS error resolving
    a UNC path, a `None` root."""
    try:
        c = Path(os.path.normcase(os.path.realpath(str(child))))
        p = Path(os.path.normcase(os.path.realpath(str(parent))))
        return c.is_relative_to(p)
    except Exception:
        return False


def _resolved(ws: Path, value: str) -> Path:
    """A location git printed, as an absolute path. `--git-common-dir` is
    reported relative to the cwd it was asked from (`.git`, or whatever
    `.git/commondir` says); `--absolute-git-dir` is already absolute."""
    value = value.strip()
    path = Path(value)
    return path if path.is_absolute() else ws / path


def _rev_parse_locations(ws: Path, config: LoopConfig) -> tuple[str, str, str] | None:
    """`(toplevel, absolute-git-dir, git-common-dir)` as git resolved them from
    inside `ws`, or None if anything about the question failed. One spawn, and
    the only source of truth either branch of `_guard` consults about where git
    actually is — every filesystem path test is a *guess* at what git will make
    of the path, which is the mistake slice 6 made twice."""
    run = _run(
        ws,
        _git(
            config,
            "rev-parse",
            "--show-toplevel",
            "--absolute-git-dir",
            "--git-common-dir",
        ),
        config,
    )
    if run.reason or run.code != 0:
        return None
    lines = run.out.splitlines()
    if len(lines) != 3:
        return None
    return lines[0].strip(), lines[1].strip(), lines[2].strip()


# `<admin>/gitdir` is a single line holding an absolute path. Bounding the read
# is the same discipline as `_MAX_CONFIG_BYTES`: it caps the work on the hot
# path and fails closed, and the regularity check is what makes the bound total
# (a FIFO reports `st_size == 0`, so the size test alone would pass one and then
# block forever with no timeout).
_MAX_GITDIR_BYTES = 1 << 12


def _read_gitdir_backpointer(admin: Path) -> Path | None:
    """The path `<admin>/gitdir` names, or None if it cannot be read.

    This is the file **git writes** when it creates a worktree, and it is the
    only thing in this shape that ties an admin directory back to one specific
    workspace. It lives inside `<repo_root>/.git`, which is operator-side: a
    worker can rewrite `<ws>/.git` (an ordinary file in its own workspace) but
    not this. Total -- any error is None, and None never compares equal to
    anything under `_same_path`, so a guard that cannot read it says no."""
    try:
        path = admin / "gitdir"
        st = path.stat()
        if not stat.S_ISREG(st.st_mode) or st.st_size > _MAX_GITDIR_BYTES:
            return None
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        return Path(text) if text else None
    except Exception:
        return None


def _guard_worktree(
    ws: Path,
    repo_root: Path,
    config: LoopConfig,
    task_id: int | None,
    branch_prefix: str,
) -> bool:
    """The second legal repository shape (slice 9): `ws` is a git **worktree**
    of the operator's repository rather than a throwaway repo of its own.

    Every condition the scratch branch checks fails on this shape, measured
    (probe 4): `<ws>/.git` is a regular **file** of ~174 bytes holding a
    `gitdir:` pointer, `--absolute-git-dir` is
    `<repo_root>/.git/worktrees/<name>` and `--git-common-dir` is
    `<repo_root>/.git`. So the branch is separate rather than a widening of the
    old conditions — a condition loosened to admit both shapes would admit
    things that are neither.

    What does *not* change is the discipline. Four conditions, all fail-closed,
    any error refuses, and the two that matter are anchored by **identity**
    against `repo_root`, the operator's own checkout — unlike scratch mode's
    anchor (`<ws>`), which sits *inside* the agent-writable workspace. **M-1
    (slice 9 remediation): this is not "a location the agent cannot write" —
    it is a location the agent cannot *substitute* for another one.**
    `<repo_root>/.git/config` is pre-existing and ordinary, and a worker
    running `git config --local` from inside its worktree writes it, the same
    documented residual `config_pin` exists to catch (see its own docstring,
    and the CRITICAL fix reproducing exactly that path-laundering). This guard
    closes every *unprivileged filesystem* escape — a junction, a symlink, a
    rewritten `.git/commondir`, a sibling worktree's admin directory pointed at
    this one — correctly; it was never meant to, and does not, make
    `repo_root` invulnerable to a command the worker itself is allowed to run
    inside it. Concretely:

    - `<ws>/.git` is a regular file. A directory here is the scratch shape and
      must take the scratch branch.
    - git's toplevel for that cwd **is** `<ws>` — the workspace is the worktree
      it claims to be, not a subdirectory of somebody else's checkout.
    - the resolved `--absolute-git-dir` is a directory
      `<repo_root>/.git/worktrees/<name>`. The leaf is git's to choose (it
      dedups names), so *this* condition is asserted on the parent: the admin
      directory belongs to this repository. A gitfile pointing at another
      repository, or at an ordinary directory, fails here.
    - **and the admin directory points back at this workspace.** The condition
      above is containment, not identity, and containment in a shared parent is
      the escape slice 6 measured twice: `<repo_root>/.git/worktrees` is the
      parent of *every* task's admin directory exactly as `workspace_root` is
      the parent of every task workspace. Measured, with nothing fabricated,
      because **a sibling worktree already exists**: task 2's worker rewrites
      only `<ws2>/.git` to name task 1's admin directory, the guard returned
      True, and task 2's round commit moved `agentloop/task-1` and destroyed
      task 1's work, after which its rollback reset task 1's branch onto task
      2's base. So the identity is anchored on the back-pointer **git itself
      writes**: `<admin>/gitdir` holds the path of `<ws>/.git`, it lives inside
      `<repo_root>/.git` where no agent has a write path, and requiring it to
      *be* `<ws>/.git` closes the sibling case, any future entry under
      `worktrees/`, and reuse of a stale admin directory in one condition. The
      parent check stays: it is what rules out a gitfile aimed at
      `<repo_root>/.git` itself.
    - the resolved `--git-common-dir` **is** `<repo_root>/.git`. This is the
      condition a `commondir` write moves, and it is the reason the guard asks
      git rather than reading `.git`: git resolves its ref and object store
      through that file, so the location it *reports* is the only thing that
      describes where a `commit` or a `reset --hard` will actually land.

    - **and HEAD is this task's own branch.** Every condition above establishes
      *where git's object and ref stores are*. In scratch mode that was the
      same fact as "which history is at risk", because the store *was* the
      throwaway repo; here the ref store is legitimately the operator's, so
      the location conditions say nothing about the ref a `commit` moves or a
      `reset --hard` rewrites. Measured with **only** the shipped worker
      spec's declared `git` tool, from inside its own workspace: `git switch
      main` and `git checkout main` are refused by git's own worktree lock,
      and `git symbolic-ref HEAD refs/heads/main` is not -- one plumbing call,
      rc=0, after which the guard passed, `commit` and `rollback` both
      returned `ok=True, reason=""`, and the operator's `main` was moved to
      the task's work while `_vcs_degraded` never fired and the audit log
      recorded a clean rollback. So HEAD must be a *symbolic* ref (a detached
      HEAD names no branch and refuses) and it must be
      `refs/heads/<branch_prefix><task_id>`. This is what makes `task_id` part
      of containment in this mode rather than a routing argument: "which task
      owns this workspace" is now a thing the guard has to know, so a worktree
      call without one refuses (`no-task-id`).

    Note what is deliberately **not** here: no containment in
    `config.workspace_root`. In this mode the workspace lives outside the
    repository (and, in P3, outside `workspace_root`'s scratch meaning), and
    containment in a shared parent was itself an escape in slice 6 — it admits
    every sibling task. Identity against `repo_root` is strictly stronger."""
    if task_id is None:
        return False
    gitfile = ws / ".git"
    if gitfile.is_dir() or not gitfile.is_file():
        return False
    common = repo_root / ".git"
    if not common.is_dir():
        # A `repo_root` that is not an ordinary repository root cannot anchor
        # anything. Refuse rather than guess at a bare or nested layout.
        return False
    located = _rev_parse_locations(ws, config)
    if located is None:
        return False
    toplevel, git_dir, common_dir = located
    if not _same_path(toplevel, ws):
        return False
    resolved_git_dir = _resolved(ws, git_dir)
    try:
        if not resolved_git_dir.is_dir():
            return False
    except Exception:
        return False
    if not _same_path(resolved_git_dir.parent, common / "worktrees"):
        return False
    back = _read_gitdir_backpointer(resolved_git_dir)
    # Explicitly, not by relying on `_same_path("None", ...)` failing: an
    # unreadable back-pointer is a guard that cannot answer, and a guard that
    # cannot answer says no.
    if back is None or not _same_path(back, gitfile):
        return False
    if not _same_path(_resolved(ws, common_dir), common):
        return False
    # A second spawn rather than a fourth output on `_rev_parse_locations`:
    # that call is shared with the scratch branch, which runs it against an
    # unborn HEAD immediately after `git init`, where asking about HEAD fails
    # and would take the whole guard down with it.
    head = _run(
        ws, _git(config, "symbolic-ref", "--quiet", "HEAD", workspace=ws), config
    )
    if head.reason or head.code != 0:
        return False
    return head.out.strip() == f"refs/heads/{branch_prefix}{task_id}"


def _guard(
    workspace: str | Path,
    config: LoopConfig,
    repo_root: str | Path | None = None,
    task_id: int | None = None,
    branch_prefix: str = DEFAULT_BRANCH_PREFIX,
) -> bool:
    """DD-16: four conditions, all fail-closed.

    `repo_root` selects the repository shape: `None` (the default, and every
    pre-slice-9 caller) is the scratch shape documented below — the workspace
    is its own throwaway repository. A `repo_root` selects `_guard_worktree`,
    where the workspace is a git worktree of that repository. The scratch
    branch below is untouched by slice 9, and that is a requirement rather than
    an accident: all five escapes measured against it stay closed exactly as
    they were.

    The first two prove "this workspace is its own repo root" and say nothing
    about *which* root — a junction at `workspace_root/task-N` pointing at
    another repository satisfies both, and `executor.py`'s threat model already
    concedes that generated code can write outside the workspace. The third is
    what makes the target of that junction fail.

    The fourth is not a restatement of the third. Git derives the worktree from
    the directory *containing* `.git`, not from where `.git` resolves to, so a
    link at `<ws>/.git` pointing at the operator's repository leaves the
    workspace itself perfectly contained while every command aims at the
    operator's history: measured, with `commit` returning ok=True and the
    victim's branch pointer rewritten off its own work. Containment of the
    resolved `.git` rather than an `is_symlink()` test, because a junction's
    reparse point is not reliably reported as a symlink and resolved location
    is the property actually wanted — `_is_within` realpaths both sides, so a
    junction, a symlink, a `.git` *file* carrying a `gitdir:` pointer and a
    `GIT_DIR` redirect all resolve and all fail closed.

    **And then it asks git.** All four conditions above test *filesystem
    paths*, which is a guess at what git will do with them, and `.git` lives
    inside the agent-writable workspace: git reads its ref and object store
    from `.git/commondir`, so one ordinary file write — no junction, no
    gitfile, no environment variable — redirected every write into the
    operator's repository with all four holding (measured: guard True, commit
    ok=True, the victim's branch pointer rewritten). A fifth path check would
    have been the same mistake one level deeper. So the single `rev-parse
    --show-toplevel` is now `--show-toplevel --absolute-git-dir
    --git-common-dir`, and the *resolved* git dir and common dir must both
    **be `<workspace>/.git` itself**. That reads the location git actually
    resolved rather than the one the filesystem suggests, and anchors it by
    *identity* rather than by containment — containment in `workspace_root`
    admitted both a sibling directory inside the same workspace (which owns
    the config git parses, while `config_pin` still fingerprints the untouched
    `<ws>/.git/config`) and a sibling *task's* workspace (one task's
    destructive command aimed at another task's history)."""
    try:
        ws = Path(workspace)
        if repo_root is not None:
            return _guard_worktree(ws, Path(repo_root), config, task_id, branch_prefix)
        if not (ws / ".git").is_dir():
            return False
        if not _is_within(ws, config.workspace_root):
            return False
        if not _is_within(ws / ".git", config.workspace_root):
            return False
        located = _rev_parse_locations(ws, config)
        if located is None:
            return False
        toplevel, git_dir, common_dir = located
        if not _same_path(toplevel, ws):
            return False
        # Identity with `<ws>/.git`, not containment in `workspace_root`.
        # Containment pins a *region*; only identity pins the *location*, and
        # `config_pin` fingerprints `<ws>/.git/config` while git reads `config`
        # from the **common dir** — so a commondir aimed at a sibling directory
        # *inside the workspace itself* satisfied containment, left the pinned
        # file byte-identical, and owned the config git actually parses
        # (measured: pin unchanged, guard True, commit ok=True, canary written
        # outside the workspace). The more surprising half is the other
        # direction: `workspace_root` is the parent of *every* task workspace,
        # so containment also permitted `<ws>/.git` to resolve into a **sibling
        # task's** repository — one task's `reset --hard` / `clean -ffdqx`
        # aimed at another task's history, with every condition holding.
        return _same_path(_resolved(ws, git_dir), ws / ".git") and _same_path(
            _resolved(ws, common_dir), ws / ".git"
        )
    except Exception:
        return False


# A git-written `.git/config` is a few hundred bytes. Refusing to hash a large
# one bounds the work this does in the hot path and fails closed, which is the
# same answer a mismatch gets. The size bound alone is not that guarantee,
# though: `stat()` on a FIFO reports `st_size == 0`, so a worker replacing
# `.git/config` with one passes the bound and makes the read block forever —
# with no timeout, unlike every git spawn here, and `commit` is called from
# `run_task` outside `_with_retry`, so the task would hang and hold its claim.
# The regularity check below is what makes the bound total.
_MAX_CONFIG_BYTES = 1 << 16


def config_pin(workspace: str | Path, repo_root: str | Path | None = None) -> str:
    """Fingerprint the git config this workspace's commands will run under, or
    `""` when there is nothing readable to fingerprint.

    **Which config that is depends on the repository shape**, and getting it
    wrong would leave the execution axis wide open. Scratch mode fingerprints
    `<workspace>/.git/config`. Worktree mode fingerprints
    `<repo_root>/.git/config`: `<ws>/.git` is a *file* there and has no config
    under it at all, and measured (slice 9, probe 2) `git config --local` run
    from inside a worktree writes the **main** repository's config — which is
    where `filter.<name>.clean` and `core.fsmonitor`, the programs git executes
    on an ordinary `add`/`commit`, would land.

    Pure, total and cheap: one bounded file read and a hash, no subprocess, so
    it can sit in front of every side-effecting command on the per-round path.
    `""` is deliberately not a fingerprint — it is the "cannot tell" value, and
    `_pin_refusal` treats it as a refusal rather than as a match.

    **Documented residual, in the register of `executor.py`'s env-scrub
    paragraph:** there is a TOCTOU window between the `stat()` below and
    `read_bytes()`. A process the agent left running in its own workspace could
    replace `.git/config` with a FIFO inside that window and reach the untimed
    read the `S_ISREG` check exists to prevent — `vcs.commit` is called from
    `run_task` outside `_with_retry`, so the task would hang holding its claim.
    POSIX only (Windows has no FIFO at a filesystem path), microseconds wide,
    and strictly better than before the check existed, when the same hang needed
    no race at all. Closing it properly means one handle rather than two
    resolutions: `os.open(..., O_RDONLY | O_NONBLOCK)`, then `os.fstat` for both
    checks, then read from that fd. Named rather than fixed because the fix is
    the kind that is easy to get subtly wrong on a path this hot, and the
    remaining exposure is a self-inflicted hang, not an escape."""
    try:
        anchor = Path(workspace) if repo_root is None else Path(repo_root)
        path = anchor / ".git" / "config"
        st = path.stat()
        if not stat.S_ISREG(st.st_mode):
            return ""
        if st.st_size > _MAX_CONFIG_BYTES:
            return ""
        return hashlib.blake2b(path.read_bytes(), digest_size=16).hexdigest()
    except Exception:
        return ""


def _pin_refusal(
    workspace: str | Path, pin: str, repo_root: str | Path | None = None
) -> str:
    """`""` if this workspace's git config is still the one that was pinned.

    Three ways to fail closed, and no way to pass by accident: no pin recorded
    at all (`"config-unpinned"`), a config that cannot be read, and a config
    whose bytes changed (both `"config-changed"`). An empty `pin` is checked
    *first*, so an unpinned caller can never satisfy an unreadable config by
    matching `""` against `""`."""
    if not pin:
        return "config-unpinned"
    actual = config_pin(workspace, repo_root)
    if not actual or actual != pin:
        return "config-changed"
    return ""


def _refusal(
    workspace: str | Path,
    config: LoopConfig,
    pin: str,
    repo_root: str | Path | None = None,
    task_id: int | None = None,
    branch_prefix: str = DEFAULT_BRANCH_PREFIX,
) -> str:
    """`""` if this workspace may be acted on, else the reason it may not.

    Distinct terms in a vocabulary documented as closed and exhaustive.
    `_guard` checks `.git` first, so a workspace that does not exist at all
    never reached the distinction `_run` was written to preserve: every entry
    point said `not-a-workspace-repo`, and the loop's warning then told the
    operator the task "ran without durability" with an empty stderr — a
    misdiagnosis, not merely a coarse one.

    The pin is checked between the two, and the position is load-bearing in
    both directions: *after* `.git` is known to be a directory, so a missing
    workspace and a non-repo keep the reasons they had; *before* `_guard`,
    which spawns git — the whole point of the pin is that no git command runs
    under a config this module did not write."""
    try:
        ws = Path(workspace)
        if not ws.is_dir():
            return "no-workspace"
        # The shape decides what `<ws>/.git` must be: a directory in scratch
        # mode, a gitfile in worktree mode (probe 4). Neither branch accepts
        # the other's shape — a worktree reaching the scratch checks would be
        # measured against a `.git` that is not the config git parses.
        if repo_root is None:
            if not (ws / ".git").is_dir():
                return "not-a-workspace-repo"
        elif task_id is None:
            # Containment in worktree mode includes *which task owns this
            # workspace* (the HEAD condition), so an absent id is not a
            # defaultable argument -- it is a question the guard cannot
            # answer, and a guard that cannot answer says no.
            return "no-task-id"
        elif not (ws / ".git").is_file():
            return "not-a-workspace-repo"
    except Exception:
        return "no-workspace"
    refusal = _pin_refusal(ws, pin, repo_root)
    if refusal:
        return refusal
    if _guard(ws, config, repo_root, task_id, branch_prefix):
        return ""
    return "not-a-workspace-repo"


def _nested_repos(ws: Path, repo_root: Path | None = None) -> tuple[str, ...]:
    """Workspace-relative directories that hold a repository of their own.

    A commit records these as bare gitlinks, so the discarded ref does not
    carry their contents and the rollback removes them unrecoverably. Named,
    not fixed: git cannot nest repositories this way. Total — an unreadable
    subtree reports nothing rather than raising.

    **The operator's own submodules are excluded** (M-e). A submodule checkout
    has a `.git` *file* whose `gitdir:` resolves to a `modules/` directory
    inside `<repo_root>/.git`, so without this every rollback on a repository
    holding submodules named tracked gitlinks the operator's own history
    carries as unrecoverable. That is the mirror of the overclaim this field
    exists to prevent: an audit log calling recoverable work destroyed is wrong
    in the direction that stops a human going to look for it.

    Measured, and the obvious form of the check is wrong: inside a **worktree**
    git puts a submodule's gitdir at
    `<repo_root>/.git/worktrees/<name>/modules/<sub>`, not at
    `<repo_root>/.git/modules/<sub>`, so anchoring on the latter excluded
    nothing on the one shape this whole slice is about.

    **Bounded** at `_MAX_REPORTED_PATHS`, like `_porcelain` (M-f): this list
    reaches an event payload and its sibling was capped for exactly that
    reason. `rollback` reports the cap on `VcsResult.truncated`, because a cap
    that does not announce itself is a blind spot rather than a bound."""
    try:
        common = None
        if repo_root is not None:
            try:
                common = repo_root / ".git"
            except Exception:
                common = None
        found = []
        for p in sorted(ws.rglob(".git")):
            if p.parent == ws:
                continue
            if common is not None and _is_submodule_gitdir(p, common):
                continue
            found.append(str(p.parent.relative_to(ws)).replace(os.sep, "/"))
        # A nested repo's own `.git` subtree can contain further `.git` paths;
        # only the outermost is a distinct repository the caller can act on.
        outermost = [
            name
            for name in found
            if not any(name.startswith(f"{other}/") for other in found if other != name)
        ]
        return tuple(outermost[:_MAX_REPORTED_PATHS])
    except Exception:
        return ()


def _is_submodule_gitdir(gitpath: Path, common: Path) -> bool:
    """Whether `<dir>/.git` is a **gitfile** naming a submodule's git directory
    inside `common` (`<repo_root>/.git`) — that is, one this repository's own
    history records and a rollback therefore does not destroy.

    Two conditions, and the second is not decoration: the target must lie
    inside `<repo_root>/.git` *and* pass through a `modules` component. Git
    puts submodule gitdirs at `<common>/modules/<sub>` from the main checkout
    and at `<common>/worktrees/<name>/modules/<sub>` from a worktree (measured),
    so neither a fixed prefix nor bare containment says the right thing —
    containment alone would also exclude a `.git` aimed at, say, another
    worktree's admin directory, and that is not a submodule.

    Total; any error is False, which reports the directory *as* a nested repo,
    and over-reporting a gap is the safe direction for a field whose whole job
    is naming what is not recoverable."""
    try:
        if not gitpath.is_file():
            return False
        st = gitpath.stat()
        if not stat.S_ISREG(st.st_mode) or st.st_size > _MAX_GITDIR_BYTES:
            return False
        text = gitpath.read_text(encoding="utf-8", errors="replace").strip()
        if not text.startswith("gitdir:"):
            return False
        target = Path(text.split(":", 1)[1].strip())
        if not target.is_absolute():
            target = gitpath.parent / target
        if not _is_within(target, common):
            return False
        resolved = Path(os.path.normcase(os.path.realpath(str(target))))
        root = Path(os.path.normcase(os.path.realpath(str(common))))
        return "modules" in resolved.relative_to(root).parts
    except Exception:
        return False


def _count_files(ws: Path) -> int:
    """Working-tree files, excluding the repo's own `.git`."""
    return len(
        [
            p
            for p in ws.rglob("*")
            if p.is_file() and ".git" not in p.relative_to(ws).parts
        ]
    )


def _failed(run: _Run, **fields) -> VcsResult:
    return VcsResult(
        ok=False, stderr=run.err, reason=run.reason or "git-failed", **fields
    )


def _head(workspace: Path, config: LoopConfig) -> _Run:
    return _run(
        workspace, _git(config, "rev-parse", "HEAD", workspace=workspace), config
    )


def _write_discarded_ref(
    workspace: Path,
    head: str,
    config: LoopConfig,
    prefix: str,
) -> _Run:
    """Save the tip under a ref named for its own sha, before anything moves.

    `reset --hard` moves the branch off the round commits, and with only the
    base and approved refs present they become reachable from **no** ref —
    `git log` and `git log --all` walk refs, not the reflog. Measured: the
    round commit vanished from `git log --all` while `git show <sha>` still
    resolved it, which is how an acceptance test passed against an
    implementation that had already destroyed the property."""
    return _run(
        workspace,
        _git(
            config,
            "update-ref",
            f"{prefix}/{head}",
            head,
            workspace=workspace,
        ),
        config,
    )


def _status(workspace: Path, config: LoopConfig, *, ignored: bool = False) -> _Run:
    args = ["status", "--porcelain", "--untracked-files=all"]
    if ignored:
        args.append("--ignored")
    return _run(workspace, _git(config, *args, workspace=workspace), config)


def _is_dirty(workspace: Path, config: LoopConfig, *, ignored: bool = True) -> bool:
    """Whether the working tree holds anything the capture below should carry.

    `--ignored` and `-uall` in scratch mode because the clean removes ignored
    and untracked files too, and the whole point of asking is that everything
    it deletes is reachable from a ref afterwards. A status that cannot be
    taken reports **dirty**: the capture that follows is non-destructive, and
    its own failure aborts the rollback before anything is removed.

    The two worktree-mode call sites pass **different** values, and the
    docstring used to claim `ignored=False` unconditionally (M-d). What
    actually holds:

    - `rollback`'s **capture** decision passes `ignored=not worktree`, i.e.
      `False` in worktree mode. The capture there stages with `add -A` and
      cannot carry ignored files at all, so counting them as dirty would commit
      an empty capture and report a recovery that did not happen. What it does
      instead is *name* them (`ignored_unrecoverable`).
    - `rollback`'s post-reset **residue** check passes `ignored=True`. That one
      is asking a different question — "did `clean -ffdqx` actually remove
      everything?" — and the clean removes ignored files, so excluding them
      would report a workspace as clean while the thing the clean was supposed
      to delete is still sitting in it.

    Same helper, two questions; the argument is what distinguishes them, which
    is why it is per-call and not derived from the mode."""
    run = _status(workspace, config, ignored=ignored)
    if run.reason or run.code != 0:
        return True
    return bool(run.out.strip())


# Bounded: this list reaches an event payload, and telemetry must never fail an
# attempt or flood one.
_MAX_REPORTED_PATHS = 50


def _porcelain(out: str, marker: str = "", *, with_code: bool = False):
    """Entries out of `status --porcelain`, optionally only those whose status
    code is `marker` (`"!!"` = ignored). Total: an unparseable line is skipped,
    never raised on. Renames (`a -> b`) report the destination.

    Returns `(entries, truncated)`. `with_code` keeps the two-character status
    code on the front of each entry, which is what lets a caller diffing two
    snapshots see an *overwrite* of an already-dirty path rather than only a
    newly-named one."""
    entries: list[str] = []
    truncated = False
    for line in out.splitlines():
        if len(line) < 4:
            continue
        code, path = line[:2], line[3:].strip()
        if marker and code != marker:
            continue
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        path = path.strip(chr(34))
        entries.append(f"{code} {path}" if with_code else path)
        if len(entries) >= _MAX_REPORTED_PATHS:
            # The list is capped *and says so*: a silent cap made the caller's
            # diff blind past the 50th entry with no signal at all.
            truncated = True
            break
    return tuple(entries), truncated


def _ignored_paths(workspace: Path, config: LoopConfig) -> tuple[str, ...]:
    """Ignored paths a worktree rollback is about to delete unrecoverably.

    Reported rather than prevented: `clean -ffdqx` still removes them, because
    weakening the clean would leave a "fresh start" that is not one. Total — a
    status that cannot be taken names nothing rather than raising."""
    run = _status(workspace, config, ignored=True)
    if run.reason or run.code != 0:
        return ()
    return _porcelain(run.out, "!!")[0]


def is_repo(
    workspace: str | Path,
    config: LoopConfig,
    *,
    repo_root: str | Path | None = None,
    task_id: int | None = None,
    branch_prefix: str = DEFAULT_BRANCH_PREFIX,
) -> bool:
    """Whether this workspace is a repository agentloop may act on. Total: it
    returns a `bool` for every input and never raises.

    Containment only: it takes no pin and answers nothing about the config,
    because it is an observation with no side effect. A True here does not
    promise a later `commit` will be accepted — the pin is checked by the
    entry points that actually run something."""
    try:
        if not config.vcs_enabled:
            return False
        return _guard(workspace, config, repo_root, task_id, branch_prefix)
    except Exception:
        return False


def _init_worktree(
    ws: Path,
    config: LoopConfig,
    pin: str,
    repo_root: Path,
    task_id: int,
    start_ref: str,
    branch_prefix: str,
) -> VcsResult:
    """Worktree mode's half of `init_repo`: `git worktree add <ws> -b
    <prefix><id> <start_ref>`, then this task's own base ref at the commit the
    checkout started from.

    **Base is the starting commit, never an empty one**, and that is the single
    most dangerous difference from scratch mode. There, base is deliberately
    empty because a rollback must not resurrect whatever happened to be in a
    throwaway directory. Here the same choice would make `reset --hard base`
    delete the operator's entire checkout on the task branch — a real branch of
    a real repository — so `rollback` returning to `start_ref` is the *whole*
    recovery contract rather than a detail of it.

    **The pin is a repository-level baseline here, not a per-task mint**, and
    the distinction is the whole of remediation C3. `init_repo`'s mint rule --
    "minted only on the branch that creates the workspace, never on one found
    already there" -- is stated per *workspace*, and in scratch mode workspace
    and config were 1:1, so per-task minting satisfied it. In worktree mode
    every task shares one `<repo_root>/.git/config` (probe 2), which is
    pre-existing and agent-writable: so "never re-pinned" was true of the
    worktree and false of the config, which is the thing the pin is about.

    Measured before the fix, with a control proving the execution channel is
    live: task 1 poisons `filter.pwn.clean` and is correctly refused on its
    next round; task 2 -- a different task, an ordinary `init_repo`, no attack
    of its own -- minted a fresh pin over the poison and executed it on its
    first round commit. Arbitrary command execution outside every workspace,
    under `allow_test_exec=False`, reported as `ok=True, reason=""`. The same
    laundering ran within one task through `human_redo`, so the one recovery
    action an operator takes on a `config-changed` refusal was the action that
    armed it.

    So a caller that has a recorded baseline (`pin`) gets it **verified**, and
    only a caller with none mints one. That makes the residual narrow and
    stateable: the *first* task on a repository blesses the operator's
    pre-existing config, and every task after it is measured against that. A
    human re-blessing a legitimately changed baseline needs a CLI surface,
    which is a P3/P4 item and deliberately not here.

    **Verified before `git worktree add` spawns, not after.** That command runs
    a checkout, and a checkout runs smudge filters -- so a refusal after the
    spawn would be no refusal at all: the first git command of worktree mode
    would be unconditionally unpinned."""
    try:
        preexisting = (ws / ".git").exists()
    except Exception:
        preexisting = False

    if preexisting:
        # Verified before *any* git command runs here, including the
        # `rev-parse` below — the point of the pin is that nothing spawns
        # under a config this module did not watch being written.
        refusal = _refusal(ws, config, pin, repo_root, task_id, branch_prefix)
        if refusal:
            return VcsResult(ok=False, reason=refusal)
        ref = base_ref(task_id)
        found = _run(
            ws,
            _git(config, "rev-parse", "--verify", "--quiet", ref, workspace=ws),
            config,
        )
        if not found.reason and found.code == 0:
            return VcsResult(ok=True, sha=found.out.strip(), reason="already")
        # A worktree that exists without its base ref falls through and
        # *completes* its initialisation, exactly as the scratch branch does —
        # otherwise `reason="already"` reports success while every later
        # rollback fails, permanently and for that task only.
        head = _head(ws, config)
        if head.reason or head.code != 0:
            return _failed(head)
        sha = head.out.strip()
        written = _run(ws, _git(config, "update-ref", ref, sha, workspace=ws), config)
        if written.reason or written.code != 0:
            return _failed(written)
        return VcsResult(ok=True, sha=sha)

    try:
        if not (repo_root / ".git").is_dir():
            # Nothing to make a worktree of. `no-workspace` rather than
            # `git-failed` for the same reason `_run` separates them: it names
            # the missing directory instead of blaming a git that is installed.
            return VcsResult(ok=False, reason="no-workspace")
    except Exception:
        return VcsResult(ok=False, reason="no-workspace")

    # `git worktree add` creates the directory itself and refuses a non-empty
    # existing one, which is the fail-closed answer to a workspace holding
    # something already.
    # C3: nothing spawns before the baseline is settled. A caller replaying a
    # recorded baseline is verified against the config as it is *now*; a caller
    # with none is the first task on this repository and mints it.
    if pin:
        refusal = _pin_refusal(ws, pin, repo_root)
        if refusal:
            return VcsResult(ok=False, reason=refusal)
        minted = pin
    else:
        minted = config_pin(ws, repo_root)
        if not minted:
            # Nothing readable to fingerprint: refuse rather than proceed
            # unpinned, the same answer a mismatch gets.
            return VcsResult(ok=False, reason="git-failed")

    added = _run(
        repo_root,
        _git(
            config,
            "worktree",
            "add",
            os.path.abspath(str(ws)),
            # `-b`, never `-B`: a branch that already exists means a previous
            # incarnation of this task, and force-resetting it would discard
            # commits no human asked to discard. It fails loudly instead.
            "-b",
            f"{branch_prefix}{task_id}",
            start_ref,
            workspace=repo_root,
        ),
        config,
    )
    if added.reason or added.code != 0:
        return _failed(added)

    if not _guard(ws, config, repo_root, task_id, branch_prefix):
        return VcsResult(ok=False, reason="not-a-workspace-repo", pin=minted)

    head = _head(ws, config)
    if head.reason or head.code != 0:
        return _failed(head, pin=minted)
    sha = head.out.strip()
    written = _run(
        ws, _git(config, "update-ref", base_ref(task_id), sha, workspace=ws), config
    )
    if written.reason or written.code != 0:
        return _failed(written, pin=minted)
    return VcsResult(ok=True, sha=sha, pin=minted)


def init_repo(
    workspace: str | Path,
    config: LoopConfig,
    pin: str = "",
    *,
    repo_root: str | Path | None = None,
    task_id: int | None = None,
    start_ref: str = "HEAD",
    branch_prefix: str = DEFAULT_BRANCH_PREFIX,
) -> VcsResult:
    """Create the workspace repo and its empty base commit at `BASE_REF`.

    Slice 9: passing `repo_root` selects the **worktree** shape instead — the
    workspace becomes a checkout of that repository on its own branch, and base
    is the commit it started from (see `_init_worktree`). `repo_root`,
    `task_id`, `start_ref` and `branch_prefix` are **arguments and not config
    knobs** on purpose: P3 of this slice is what reads `workspace_mode`,
    `repo_root`, `vcs_base_ref` and `vcs_branch_prefix` out of `LoopConfig` and
    passes them here. Inventing the knob in this module would put the mode
    decision in two places.

    `git init` runs before the guard because the guard cannot pass before it,
    and because on a workspace with no `.git` at all it only creates
    `workspace/.git`. That claim used to be stated unconditionally and was
    wrong: a **gitfile** at `<ws>/.git` (one line, `gitdir: elsewhere`, writable
    by any worker) is not a directory, so it took the create branch, and `git
    init` then reinitialised the repository the file pointed at. The create
    branch now refuses when `<ws>/.git` exists in any form. The base commit is guarded like everything
    else.

    The fast path verifies the invariant it claims rather than inferring it
    from the presence of `.git`. A repository whose base commit failed once —
    a full disk, an `index.lock`, a timeout — passes the guard, so
    `reason="already"` (deliberately never logged, because an idempotent no-op
    is not a degradation) reported success while every later `rollback` failed
    with `git-failed`, permanently and for that task only, in silence. A repo
    that exists without `BASE_REF` therefore falls through and *completes* its
    initialisation.

    This is the only place a pin is **minted**, and only on the branch that
    creates the repository: the fingerprint of a `.git/config` this module just
    watched `git init` write. An *existing* repo is verified against the pin
    the caller passes in and never re-pinned — re-pinning would launder exactly
    the edit the pin exists to catch, since a worker who rewrote the config
    between two rounds would have it blessed by the next `init_repo`."""
    try:
        if not config.vcs_enabled:
            return VcsResult(ok=False, reason="disabled")
        ws = Path(workspace)
        if repo_root is not None:
            if task_id is None:
                return VcsResult(ok=False, reason="no-task-id")
            return _init_worktree(
                ws,
                config,
                pin,
                Path(repo_root),
                task_id,
                start_ref,
                branch_prefix,
            )
        minted = ""
        try:
            preexisting = (ws / ".git").is_dir()
        except Exception:
            preexisting = False
        if preexisting:
            # Verified before *any* git command runs in it — including the
            # `rev-parse` below, which would otherwise be the one subprocess
            # this module runs under an unvetted config.
            refusal = _refusal(ws, config, pin)
            if refusal:
                return VcsResult(ok=False, reason=refusal)
            base = _run(
                ws,
                _git(
                    config,
                    "rev-parse",
                    "--verify",
                    "--quiet",
                    BASE_REF,
                    workspace=ws,
                ),
                config,
            )
            if not base.reason and base.code == 0:
                return VcsResult(ok=True, sha=base.out.strip(), reason="already")
        else:
            try:
                if not ws.is_dir():
                    return VcsResult(ok=False, reason="no-workspace")
            except Exception:
                return VcsResult(ok=False, reason="no-workspace")
            # `<ws>/.git` exists but is not a directory: a **gitfile**, one line
            # of text reading `gitdir: <somewhere else>`, which any worker can
            # write. `preexisting` tests `is_dir()`, so this fell into the
            # create branch — and `git init` on a worktree whose `.git` points
            # elsewhere *reinitialises the repository it points at*, rc=0.
            # Measured against a victim repo: it ran, and the docstring above
            # claiming init "cannot reach outside the workspace" was wrong.
            #
            # Real impact was small — the victim's `config` came back
            # byte-identical, `config_pin` then returned "" and the flow failed
            # closed — but "small because a later check happens to catch it" is
            # not the same as contained, and `-c init.templateDir=` is the only
            # thing keeping it from copying template files into someone else's
            # repository. In the one module whose deliverable is precisely
            # stated containment, a stated invariant that does not hold is the
            # thing most likely to be built on later.
            try:
                if (ws / ".git").exists():
                    return VcsResult(ok=False, reason="not-a-workspace-repo")
            except Exception:
                return VcsResult(ok=False, reason="not-a-workspace-repo")
            run = _run(ws, _git(config, *_INIT_PINS, "init", "-q"), config)
            if run.reason or run.code != 0:
                return _failed(run)
            minted = config_pin(ws)
            if not minted:
                # git reported success and left no readable config: refuse
                # rather than continue unpinned.
                return VcsResult(ok=False, reason="git-failed")
            if not _guard(ws, config):
                return VcsResult(ok=False, reason="not-a-workspace-repo", pin=minted)

        # Deliberately empty: base is the state a rollback returns to, so it
        # must not carry whatever happened to be in the directory already.
        run = _run(
            ws,
            _git(
                config,
                "commit",
                "--allow-empty",
                "-m",
                "agentloop base",
                workspace=ws,
            ),
            config,
        )
        if run.reason or run.code != 0:
            return _failed(run, pin=minted)

        head = _head(ws, config)
        if head.reason or head.code != 0:
            return _failed(head, pin=minted)
        sha = head.out.strip()

        run = _run(ws, _git(config, "update-ref", BASE_REF, sha, workspace=ws), config)
        if run.reason or run.code != 0:
            return _failed(run, pin=minted)
        return VcsResult(ok=True, sha=sha, pin=minted)
    except Exception as exc:  # totality (DD-6)
        return VcsResult(ok=False, stderr=_clip(str(exc)), reason="git-failed")


def commit(
    workspace: str | Path,
    message: str,
    config: LoopConfig,
    pin: str = "",
    *,
    repo_root: str | Path | None = None,
    task_id: int | None = None,
    branch_prefix: str = DEFAULT_BRANCH_PREFIX,
) -> VcsResult:
    """Commit the whole workspace. `--allow-empty`, so a round that changed
    nothing still produces a sha.

    Slice 9: in worktree mode (`repo_root` given) the stage is `add -A` with
    **no `-f`**. See the comment at the `add` below — this is a real change to
    the recovery contract, not a tidy-up, and `rollback` names the cost on
    every result rather than letting the audit log overclaim.

    `pin` is the fingerprint `init_repo` minted for this workspace, replayed
    from wherever the caller recorded it. There is no default that works: an
    empty one refuses, because a commit under a config nobody vetted is how a
    worker gets `filter.<name>.clean` executed.

    Slice 9 remediation C1: `task_id` and `branch_prefix` are **containment
    arguments** in worktree mode, not routing. The guard requires HEAD to be
    `refs/heads/<branch_prefix><task_id>`, because nothing else in the shape
    constrains which ref this command moves -- measured, a worker's single
    `git symbolic-ref HEAD refs/heads/main` moved the operator's branch through
    an `ok=True, reason=""` commit and rollback. An absent id refuses
    (`no-task-id`)."""
    try:
        if not config.vcs_enabled:
            return VcsResult(ok=False, reason="disabled")
        ws = Path(workspace)
        if repo_root is not None and task_id is None:
            return VcsResult(ok=False, reason="no-task-id")
        refusal = _refusal(ws, config, pin, repo_root, task_id, branch_prefix)
        if refusal:
            return VcsResult(ok=False, reason=refusal)

        # `-f` as well as `-A`: without it `add` honours a worker-written
        # `.gitignore` while the rollback's `clean -ffdqx` deletes ignored
        # files, so the discarded ref the audit log names was missing exactly
        # the files that were destroyed. Measured: `build/artifact.bin` and
        # `secrets.env` deleted, neither recoverable. Capture everything the
        # rollback can delete; do not weaken the clean.
        #
        # **Worktree mode drops the `-f`, and pays for it in the result.** The
        # branch is the operator's, so forcing ignored files in would commit
        # `node_modules` and `.venv` — 441 MB on this repository — every round,
        # forever, into history a human is meant to merge. What that costs is
        # exactly the property `-f` buys above: ignored files are **not**
        # recoverable after a worktree rollback. `rollback` puts them on
        # `VcsResult.ignored_unrecoverable` per call.
        #
        # **That is not yet parity with `nested_repos`, and saying it was is
        # the kind of overclaim this field exists to prevent** (M-g).
        # `nested_repos` reaches an audit event, the REST API and the dashboard
        # through `loop._vcs_rollback_to_base`'s payload;
        # `ignored_unrecoverable` reaches none of them, so today it is a field
        # a caller *may* read and nothing does. Wiring it needs a `loop.py`
        # call-site change, which is P3's scope and not P2's, so the honest
        # state is recorded here rather than repaired by a sentence.
        stage = ("add", "-A") if repo_root is not None else ("add", "-A", "-f")
        run = _run(ws, _git(config, *stage, workspace=ws), config)
        if run.reason or run.code != 0:
            return _failed(run)

        # `message` is a single argv element after `-m`: it can never be read
        # as a flag, whatever it contains.
        run = _run(
            ws,
            _git(
                config,
                "commit",
                "--allow-empty",
                # Measured: git refuses an empty `-m` outright ("Aborting
                # commit due to empty commit message"). A caller may ignore
                # this result, so a message it did not think about must not
                # silently cost the round its commit.
                "--allow-empty-message",
                "-m",
                message,
                workspace=ws,
            ),
            config,
        )
        if run.reason or run.code != 0:
            return _failed(run)

        head = _head(ws, config)
        if head.reason or head.code != 0:
            return _failed(head)
        return VcsResult(ok=True, sha=head.out.strip())
    except Exception as exc:  # totality (DD-6)
        return VcsResult(ok=False, stderr=_clip(str(exc)), reason="git-failed")


def mark_approved(
    workspace: str | Path,
    config: LoopConfig,
    pin: str = "",
    *,
    repo_root: str | Path | None = None,
    task_id: int | None = None,
    branch_prefix: str = DEFAULT_BRANCH_PREFIX,
) -> VcsResult:
    """Point this task's approved ref at the current tip. Additive and
    non-destructive: it moves a ref and nothing else — but it still runs git in
    the workspace, so it is pinned like everything else.

    The ref is `APPROVED_REF` in scratch mode and `approved_ref(task_id)` in
    worktree mode, where every task writes into one shared namespace and a
    fixed name would have task 2's approval overwrite task 1's bookmark."""
    try:
        if not config.vcs_enabled:
            return VcsResult(ok=False, reason="disabled")
        ws = Path(workspace)
        if repo_root is not None and task_id is None:
            return VcsResult(ok=False, reason="no-task-id")
        refusal = _refusal(ws, config, pin, repo_root, task_id, branch_prefix)
        if refusal:
            return VcsResult(ok=False, reason=refusal)

        head = _head(ws, config)
        if head.reason or head.code != 0:
            return _failed(head)
        sha = head.out.strip()

        ref = approved_ref(task_id) if repo_root is not None else APPROVED_REF
        run = _run(ws, _git(config, "update-ref", ref, sha, workspace=ws), config)
        if run.reason or run.code != 0:
            return _failed(run)
        return VcsResult(ok=True, sha=sha)
    except Exception as exc:  # totality (DD-6)
        return VcsResult(ok=False, stderr=_clip(str(exc)), reason="git-failed")


def rollback(
    workspace: str | Path,
    ref: str,
    config: LoopConfig,
    pin: str = "",
    *,
    repo_root: str | Path | None = None,
    task_id: int | None = None,
    branch_prefix: str = DEFAULT_BRANCH_PREFIX,
) -> VcsResult:
    """Return the workspace to `ref`, keeping what it discards.

    In worktree mode the caller passes `base_ref(task_id)` as `ref` and the
    discarded tip is saved under this task's own namespace. Two differences
    from scratch mode, both named on the result rather than argued away: the
    target is the **starting commit** (an empty base would delete the
    operator's checkout on the task branch), and ignored files are not carried
    by the capture, so they are named in `ignored_unrecoverable`.

    The order is the point: the discarded tip is recorded *before* anything
    moves, and a rollback that cannot record what it is about to discard does
    not discard it. `files_removed` is a pre/post count rather than a parse of
    git's output — `clean -q` prints nothing and `reset --hard` has already
    removed the tracked half, so nothing git prints could produce the number."""
    try:
        if not config.vcs_enabled:
            return VcsResult(ok=False, reason="disabled")
        ws = Path(workspace)
        worktree = repo_root is not None
        if worktree and task_id is None:
            return VcsResult(ok=False, reason="no-task-id")
        refusal = _refusal(ws, config, pin, repo_root, task_id, branch_prefix)
        if refusal:
            return VcsResult(ok=False, reason=refusal)

        before = _count_files(ws)

        # Worktree mode: what the clean is about to delete unrecoverably,
        # measured before anything moves. Empty in scratch mode, where `add -A
        # -f` carries ignored files into the discarded ref.
        ignored = _ignored_paths(ws, config) if worktree else ()

        head = _head(ws, config)
        if head.reason or head.code != 0:
            return _failed(head)
        target = _run(ws, _git(config, "rev-parse", ref, workspace=ws), config)
        if target.reason or target.code != 0:
            return _failed(target)
        head_sha, target_sha = head.out.strip(), target.out.strip()

        # Named before anything moves, and reported whatever happens next: a
        # nested repository is recorded as a bare gitlink, so the discarded ref
        # does not carry its objects and `clean -ffdqx` removes them for good.
        nested = _nested_repos(ws, Path(repo_root) if worktree else None)
        # Both bounded lists are diffed or rendered by a caller, so a cap that
        # does not announce itself would read as "there were none".
        capped = tuple(
            name
            for name, values in (
                ("nested_repos", nested),
                ("ignored_unrecoverable", ignored),
            )
            if len(values) >= _MAX_REPORTED_PATHS
        )

        # The capture is **unconditional**, and that is the whole invariant:
        # everything the clean deletes is reachable from a ref. Gated on
        # `head_sha != target_sha` it was not — a worker that wrote files and
        # then returned `ESCALATE:` (or an empty output) is returned to the
        # caller *before* the round commit, so a reject of that ordinary shape
        # had files on disk and zero commits, wrote no discarded ref, and let
        # `clean -ffdqx` delete the working tree: measured `files_removed=1`
        # with `sha=""` and `reason=""`, a destruction the audit called a clean
        # rollback. `add -A -f` makes it likelier still, not less.
        if _is_dirty(ws, config, ignored=not worktree):
            # Nested repositories are excluded by pathspec, because git
            # refuses to index one that has no commit checked out ("unable to
            # index file 'sub/'") and one failed path element would otherwise
            # cost the *whole* workspace its capture. Their contents were
            # already outside the recovery surface — a commit records them as
            # a bare gitlink — and `nested_repos` is what names that gap.
            exclusions = (
                ["--", ".", *[f":(exclude){name}" for name in nested]] if nested else []
            )
            # `-f` in scratch mode only, for the reason `commit` documents:
            # forcing ignored files into the operator's task branch is worse
            # than losing them, and losing them is *named* rather than hidden.
            stage = ("add", "-A") if worktree else ("add", "-A", "-f")
            run = _run(ws, _git(config, *stage, *exclusions, workspace=ws), config)
            if run.reason or run.code != 0:
                return _failed(
                    run,
                    nested_repos=nested,
                    ignored_unrecoverable=ignored,
                    truncated=capped,
                )
            run = _run(
                ws,
                _git(
                    config,
                    "commit",
                    "--allow-empty",
                    "--allow-empty-message",
                    "-m",
                    "agentloop discarded (uncommitted)",
                    workspace=ws,
                ),
                config,
            )
            if run.reason or run.code != 0:
                return _failed(
                    run,
                    nested_repos=nested,
                    ignored_unrecoverable=ignored,
                    truncated=capped,
                )
            head = _head(ws, config)
            if head.reason or head.code != 0:
                return _failed(
                    head,
                    nested_repos=nested,
                    ignored_unrecoverable=ignored,
                    truncated=capped,
                )
            head_sha = head.out.strip()

        preserved = ""
        if head_sha != target_sha:
            written = _write_discarded_ref(
                ws,
                head_sha,
                config,
                discarded_ref_prefix(task_id) if worktree else DISCARDED_REF_PREFIX,
            )
            if written.reason or written.code != 0:
                # Losing the history is the failure this step exists to
                # prevent, so nothing destructive runs.
                return _failed(written)
            # From here the tip is preserved under a ref. Every later failure
            # carries the sha, because the caller's fallback is a wipe and
            # wiping `.git` would destroy exactly the history this step just
            # saved — `sha` on a failed result is how a caller tells "nothing
            # was recorded" from "the ref is written but the tree is dirty".
            preserved = head_sha

        run = _run(ws, _git(config, "reset", "--hard", ref, workspace=ws), config)
        if run.reason or run.code != 0:
            return _failed(
                run,
                sha=preserved,
                nested_repos=nested,
                ignored_unrecoverable=ignored,
                truncated=capped,
            )

        # The second `-f` removes untracked *nested* repositories: a worker
        # that ran `git init` in its own workspace would otherwise leave files
        # behind and silently break redo's "fresh start". It still never
        # touches this repo's own `.git`, which is not working-tree content.
        run = _run(ws, _git(config, "clean", "-ffdqx", workspace=ws), config)
        if run.reason or run.code != 0:
            return _failed(
                run,
                sha=preserved,
                nested_repos=nested,
                ignored_unrecoverable=ignored,
                truncated=capped,
            )

        after = _count_files(ws)
        removed = max(0, before - after)
        # The pre-rollback tip, so a caller can record *where the discarded
        # round went* without running a git command of its own (DD-12 is only
        # useful if the audit trail names the ref). `""` when HEAD already
        # resolved to the target and nothing was discarded.
        discarded = head_sha if head_sha != target_sha else ""
        reason = ""
        if worktree:
            # A worktree's target commit *has* a tree — the operator's tracked
            # files, which surviving is the entire point (see the docstring's
            # "starting commit, never an empty one"). So residue cannot be a
            # file count here; it is whatever the clean failed to remove, asked
            # of git. Counting files would report every healthy worktree
            # rollback as a degradation, and `_vcs_degraded` turns a reason
            # into a warning and an event on every round.
            if _is_dirty(ws, config, ignored=True):
                reason = "residue"
        elif after:
            reason = "residue"
        if removed and not discarded:
            # A floor under the invariant above, and **not** a substitute for
            # it: if files were destroyed and no ref names them, the result
            # must not read as a clean rollback. `reason` is what makes
            # `_vcs_degraded` fire, so the destruction is warned and audited
            # rather than reported as success with an empty `discarded_sha`.
            reason = "unrecorded"
        return VcsResult(
            ok=True,
            sha=discarded,
            files_removed=removed,
            reason=reason,
            nested_repos=nested,
            ignored_unrecoverable=ignored,
            truncated=capped,
        )
    except Exception as exc:  # totality (DD-6)
        return VcsResult(ok=False, stderr=_clip(str(exc)), reason="git-failed")


def remove_worktree(
    workspace: str | Path,
    config: LoopConfig,
    pin: str = "",
    *,
    repo_root: str | Path,
    task_id: int | None = None,
    branch_prefix: str = DEFAULT_BRANCH_PREFIX,
) -> VcsResult:
    """Remove a worktree and prune the admin entry it leaves behind.

    `git worktree remove` and **then** `git worktree prune`, never an `rmtree`:
    deleting the directory alone leaves the entry under
    `<repo_root>/.git/worktrees/<name>`, so `git worktree list` keeps reporting
    a workspace that is gone and a later `worktree add` at the same path is
    refused as already registered. This is why `executor.clear_workspace` may
    not be reused for the worktree shape (P3 routes it here).

    `--force`, because a workspace holds a worker's uncommitted output by
    construction and `remove` refuses a dirty tree; the caller's contract
    (`human_redo`) is precisely that the tree is discarded, and `rollback` is
    what preserves it first when preserving is wanted.

    Pinned like every other side-effecting entry point: it spawns git in the
    operator's repository, so it must not run under a config this module did
    not watch being written. Total, like its siblings.

    The **branch outlives the worktree** — `agentloop/task-<id>` lives in the
    operator's ref store — so a removed workspace is still mergeable. Nothing
    here deletes a branch or a ref."""
    try:
        if not config.vcs_enabled:
            return VcsResult(ok=False, reason="disabled")
        ws = Path(workspace)
        root = Path(repo_root)
        if task_id is None:
            return VcsResult(ok=False, reason="no-task-id")
        refusal = _refusal(ws, config, pin, root, task_id, branch_prefix)
        if refusal:
            return VcsResult(ok=False, reason=refusal)

        removed = _run(
            root,
            _git(
                config,
                "worktree",
                "remove",
                "--force",
                os.path.abspath(str(ws)),
                workspace=root,
            ),
            config,
        )
        if removed.reason or removed.code != 0:
            return _failed(removed)
        pruned = _run(root, _git(config, "worktree", "prune", workspace=root), config)
        if pruned.reason or pruned.code != 0:
            return _failed(pruned)
        return VcsResult(ok=True)
    except Exception as exc:  # totality (DD-6)
        return VcsResult(ok=False, stderr=_clip(str(exc)), reason="git-failed")


def remove_task_branch(
    repo_root: str | Path,
    config: LoopConfig,
    pin: str = "",
    *,
    task_id: int,
    branch_prefix: str = DEFAULT_BRANCH_PREFIX,
) -> VcsResult:
    """Delete a task's branch from the operator's repository. Slice 9 P4.

    P2's `init_repo` always creates a worktree with `-b`, never `-B` (a branch
    that already exists means a previous incarnation of this task, and
    force-resetting it would discard commits no human asked to discard) — a
    deliberate refusal P2 flagged and left for P4 to resolve, because
    `human_redo`'s worktree-mode fresh start (`remove_worktree` then
    `init_repo`) hits that exact refusal on a task's *second* redo: the branch
    `init_repo` created the first time is still there.

    The decision: delete the stale branch first. `human_redo`'s whole contract
    is a fresh start with no carried-over context, and by the time this runs
    `_vcs_rollback_to_base` has already written
    `refs/agentloop/task-<id>/discarded/<sha>` at the branch's prior tip (the
    caller's ordering, not this function's), so the branch's commits stay
    reachable from that ref (`git log --all`) after the branch name pointing
    at them is deleted — recoverability was never a property of the branch
    name, only of what is reachable, which is the same distinction
    `vcs.rollback`'s own docstring rests on. Deleting the branch is therefore
    not a second copy of "discard the work"; the work was already discarded
    (or preserved) by the rollback that ran first, and this only clears the
    name so the next `worktree add -b` can reuse it.

    `-D`, not `-d`: nothing in this project ever merges a task branch, so
    "unmerged" is the normal case, not a warning to respect.

    Pinned like every other repo-level mutation (`_init_worktree`'s baseline
    check): this spawns git under `<repo_root>/.git/config` too, and a branch
    name derived from config is not agent-writable, but the config it runs
    under still is. A branch that does not exist is not treated specially —
    `git branch -D` on a missing name fails, and a caller that only wanted "no
    stale branch in the way" may ignore a failed result exactly as every other
    `vcs` entry point's callers already do."""
    try:
        if not config.vcs_enabled:
            return VcsResult(ok=False, reason="disabled")
        root = Path(repo_root)
        try:
            if not (root / ".git").is_dir():
                return VcsResult(ok=False, reason="no-workspace")
        except Exception:
            return VcsResult(ok=False, reason="no-workspace")
        refusal = _pin_refusal(root, pin, root)
        if refusal:
            return VcsResult(ok=False, reason=refusal)
        run = _run(
            root,
            _git(
                config,
                "branch",
                "-D",
                f"{branch_prefix}{task_id}",
                workspace=root,
            ),
            config,
        )
        if run.reason or run.code != 0:
            return _failed(run)
        return VcsResult(ok=True)
    except Exception as exc:  # totality (DD-6)
        return VcsResult(ok=False, stderr=_clip(str(exc)), reason="git-failed")


def _parse_worktree_list_porcelain(output: str) -> list[dict[str, object]]:
    """`git worktree list --porcelain` into one dict per entry (`path`,
    `branch` when the worktree is on one, `detached`/`bare` flags). Entries
    are blank-line separated; total over malformed input (an unrecognised
    line is ignored, never raised on)."""
    entries: list[dict[str, object]] = []
    current: dict[str, object] = {}
    for line in output.splitlines():
        if not line.strip():
            if current:
                entries.append(current)
                current = {}
            continue
        if line.startswith("worktree "):
            current["path"] = line[len("worktree ") :].strip()
        elif line.startswith("branch "):
            current["branch"] = line[len("branch ") :].strip()
        elif line == "detached":
            current["detached"] = True
        elif line == "bare":
            current["bare"] = True
    if current:
        entries.append(current)
    return entries


def prune_worktrees(
    repo_root: str | Path,
    config: LoopConfig,
    branch_prefix: str = DEFAULT_BRANCH_PREFIX,
) -> VcsResult:
    """Clear the admin entries `git worktree list` still reports for an
    **agentloop** worktree whose directory is already gone (P3, `agentloop
    workspace prune`'s repo-wide half).

    `remove_worktree` correctly *refuses* this case rather than repairing it
    (see `test_remove_worktree_against_a_stale_admin_entry`): with `<ws>`
    already deleted the guard cannot establish anything about the workspace, so
    it answers `no-workspace` and leaves the stale entry standing. That is the
    right shape for a guarded, per-workspace call, but it means the stale entry
    survives every ordinary path and the operator-facing repair is this
    command.

    **P3 remediation cycle 3, HIGH 2.** A bare `git worktree prune` is
    repo-wide by git's own design — it has no branch/owner scoping, so it
    clears the admin entry for *any* worktree whose directory is currently
    unreachable, whether agentloop created it or not. Measured: an operator's
    own, manually-created worktree (`operator/manual-feature`, not
    `agentloop/task-N`) with its directory removed lost its admin entry to a
    bare `worktree prune` exactly as agentloop's own stale entry did — the
    operator's branch survives, but the worktree registration does not, so a
    directory that reappears later (a remounted drive, a restored backup) is
    orphaned: its `.git` gitlink points at a deleted admin directory and every
    git command inside it fails until manually repaired.

    `git worktree prune` itself takes no scoping flag, so the fix is one layer
    up: enumerate `git worktree list --porcelain` first, keep only the entries
    whose branch is `refs/heads/<branch_prefix>...` (agentloop's own, never
    the main worktree — which has no branch matching that prefix) and whose
    directory no longer exists, and remove **only those** admin entries via a
    targeted `git worktree remove --force <path>` rather than the catch-all.
    An entry that does not match the prefix is left untouched, whether or not
    its directory is reachable — this function never decides that an
    operator's own work is stale.

    No `ws` to guard for the *enumeration* — `worktree list --porcelain` only
    reads the admin directory, it does not check anything out or invoke any
    file-triggered git config, so it takes no pin, unlike the targeted
    `worktree remove` below (which spawns inside `<repo_root>`, the same
    execution-axis surface every side-effecting call in this module defends).
    Kept pin-free anyway, in the same register as `is_repo`: a non-mutating
    observation needs no pin, only a mutation does; here that mutation is the
    per-entry `worktree remove`, which is only ever pointed at a *literal
    admin path git itself just reported*, never at anything a worker could
    have written. `config.vcs_enabled=False` still refuses, matching every
    other entry point here. Total (DD-6): any exception during enumeration or
    a single entry's removal is folded into the returned reason rather than
    raised, and one entry's failure does not stop the rest."""
    try:
        if not config.vcs_enabled:
            return VcsResult(ok=False, reason="disabled")
        root = Path(repo_root)
        try:
            if not (root / ".git").is_dir():
                return VcsResult(ok=False, reason="no-workspace")
        except Exception:
            return VcsResult(ok=False, reason="no-workspace")
        listed = _run(
            root,
            _git(config, "worktree", "list", "--porcelain", workspace=root),
            config,
        )
        if listed.reason or listed.code != 0:
            return _failed(listed)
        prefix_ref = f"refs/heads/{branch_prefix}"
        any_failed = False
        last_failed: _Run | None = None
        for entry in _parse_worktree_list_porcelain(listed.out):
            branch = entry.get("branch")
            path = entry.get("path")
            if not isinstance(branch, str) or not branch.startswith(prefix_ref):
                continue  # not agentloop's own — never touched, reachable or not
            if not isinstance(path, str) or not path:
                continue
            try:
                stale = not Path(path).exists()
            except Exception:
                stale = False
            if not stale:
                continue
            removed = _run(
                root,
                _git(
                    config,
                    "worktree",
                    "remove",
                    "--force",
                    os.path.abspath(path),
                    workspace=root,
                ),
                config,
            )
            if removed.reason or removed.code != 0:
                any_failed = True
                last_failed = removed
        if any_failed and last_failed is not None:
            return _failed(last_failed)
        return VcsResult(ok=True)
    except Exception as exc:  # totality (DD-6)
        return VcsResult(ok=False, stderr=_clip(str(exc)), reason="git-failed")


def working_tree_state(
    workspace: str | Path,
    config: LoopConfig,
    pin: str = "",
    *,
    repo_root: str | Path | None = None,
    task_id: int | None = None,
    branch_prefix: str = DEFAULT_BRANCH_PREFIX,
) -> VcsResult:
    """What is currently uncommitted in the workspace, as porcelain entries.

    Read-only — it runs `status` and nothing else — but pinned and guarded like
    the rest, because it still spawns git inside a directory an agent writes
    to. Ignored paths are excluded: they are noise in a real checkout
    (`.venv`, `node_modules`, a build directory) and nothing here is a
    recovery claim.

    This exists for one caller: comparing the tree either side of a step, so
    that a write nobody expected is **detected and recorded**. It detects; it
    prevents nothing, and no caller may describe it as a barrier."""
    try:
        if not config.vcs_enabled:
            return VcsResult(ok=False, reason="disabled")
        ws = Path(workspace)
        if repo_root is not None and task_id is None:
            return VcsResult(ok=False, reason="no-task-id")
        refusal = _refusal(ws, config, pin, repo_root, task_id, branch_prefix)
        if refusal:
            return VcsResult(ok=False, reason=refusal)
        run = _status(ws, config)
        if run.reason or run.code != 0:
            return _failed(run)
        entries, capped = _porcelain(run.out, with_code=True)
        return VcsResult(
            ok=True,
            changed_entries=entries,
            truncated=("changed_entries",) if capped else (),
        )
    except Exception as exc:  # totality (DD-6)
        return VcsResult(ok=False, stderr=_clip(str(exc)), reason="git-failed")


def repo_status(repo_root: str | Path, config: LoopConfig, pin: str = "") -> VcsResult:
    """`git status`, porcelain, of the **operator's own repository** — never a
    task workspace. Slice 9 P4, residual 2.

    Deliberately not `working_tree_state(repo_root, config, pin=pin)`: that
    function's guard has exactly two shapes, and `repo_root` fits neither.
    Scratch mode's `_guard` requires the resolved workspace to sit inside
    `config.workspace_root`, which `repo_root` is not (it is the operator's
    own repository, entirely outside any workspace root); worktree mode's
    `_guard_worktree` requires `<ws>/.git` to be a *file* (a worktree gitfile),
    and `repo_root/.git` is an ordinary directory. Both refuse with
    `"not-a-workspace-repo"` — measured, not assumed, which is why this is a
    third, narrower entry point rather than a third branch bolted onto
    `_guard`: `_guard`'s two shapes are containment checks defending an
    agent-writable directory, and `repo_root` is neither of those things — it
    is the one path in this whole module that is operator config, read
    directly, never a workspace an agent's tools resolve into.

    Still pinned, because it still spawns git under `<repo_root>/.git/config`
    (the same config `_init_worktree`'s baseline defends) — a `git status`
    run does not trigger a smudge/clean filter, but running any command
    under a config nobody vetted is the exact axis this module's pin exists
    to close, and there is no reason to make an exception for a read-only
    one. Read-only otherwise, matching `working_tree_state`: it runs `status`
    and nothing else. **Detects; prevents nothing** — see
    `Loop._vcs_detect_out_of_branch_write`, its one caller."""
    try:
        if not config.vcs_enabled:
            return VcsResult(ok=False, reason="disabled")
        root = Path(repo_root)
        try:
            if not (root / ".git").is_dir():
                return VcsResult(ok=False, reason="no-workspace")
        except Exception:
            return VcsResult(ok=False, reason="no-workspace")
        refusal = _pin_refusal(root, pin, root)
        if refusal:
            return VcsResult(ok=False, reason=refusal)
        run = _status(root, config)
        if run.reason or run.code != 0:
            return _failed(run)
        entries, capped = _porcelain(run.out, with_code=True)
        return VcsResult(
            ok=True,
            changed_entries=entries,
            truncated=("changed_entries",) if capped else (),
        )
    except Exception as exc:  # totality (DD-6)
        return VcsResult(ok=False, stderr=_clip(str(exc)), reason="git-failed")
