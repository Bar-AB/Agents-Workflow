"""Per-task workspace version control (roadmap slice 6, Part 1).

Every task workspace is its own throwaway git repository, so `redo` and
`reject` *recover* the discarded work instead of destroying it. There is no
shared timeline and no remote: one repo per `.agentloop/ws/task-{id}/`, created
by the loop, never seen by a human unless they go looking.

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
    `"config-changed"`, `"already"`, `"residue"`, `"unrecorded"`."""

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
    optionally `-C <ws>` (defence in depth on top of the pinned cwd)."""
    argv = [config.vcs_command, *_IDENTITY, *_CONFIG_PINS]
    if workspace is not None:
        argv += ["-C", str(workspace)]
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


def _guard(workspace: str | Path, config: LoopConfig) -> bool:
    """DD-16: four conditions, all fail-closed.

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
        if not (ws / ".git").is_dir():
            return False
        if not _is_within(ws, config.workspace_root):
            return False
        if not _is_within(ws / ".git", config.workspace_root):
            return False
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
            return False
        lines = run.out.splitlines()
        if len(lines) != 3:
            return False
        toplevel, git_dir, common_dir = lines
        if not _same_path(toplevel.strip(), ws):
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


def config_pin(workspace: str | Path) -> str:
    """Fingerprint `<workspace>/.git/config`, or `""` when there is nothing
    readable to fingerprint.

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
        path = Path(workspace) / ".git" / "config"
        st = path.stat()
        if not stat.S_ISREG(st.st_mode):
            return ""
        if st.st_size > _MAX_CONFIG_BYTES:
            return ""
        return hashlib.blake2b(path.read_bytes(), digest_size=16).hexdigest()
    except Exception:
        return ""


def _pin_refusal(workspace: str | Path, pin: str) -> str:
    """`""` if this workspace's git config is still the one that was pinned.

    Three ways to fail closed, and no way to pass by accident: no pin recorded
    at all (`"config-unpinned"`), a config that cannot be read, and a config
    whose bytes changed (both `"config-changed"`). An empty `pin` is checked
    *first*, so an unpinned caller can never satisfy an unreadable config by
    matching `""` against `""`."""
    if not pin:
        return "config-unpinned"
    actual = config_pin(workspace)
    if not actual or actual != pin:
        return "config-changed"
    return ""


def _refusal(workspace: str | Path, config: LoopConfig, pin: str) -> str:
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
        if not (ws / ".git").is_dir():
            return "not-a-workspace-repo"
    except Exception:
        return "no-workspace"
    refusal = _pin_refusal(ws, pin)
    if refusal:
        return refusal
    return "" if _guard(ws, config) else "not-a-workspace-repo"


def _nested_repos(ws: Path) -> tuple[str, ...]:
    """Workspace-relative directories that hold a repository of their own.

    A commit records these as bare gitlinks, so the discarded ref does not
    carry their contents and the rollback removes them unrecoverably. Named,
    not fixed: git cannot nest repositories this way. Total — an unreadable
    subtree reports nothing rather than raising."""
    try:
        found = sorted(
            str(p.parent.relative_to(ws)).replace(os.sep, "/")
            for p in ws.rglob(".git")
            if p.parent != ws
        )
        # A nested repo's own `.git` subtree can contain further `.git` paths;
        # only the outermost is a distinct repository the caller can act on.
        return tuple(
            name
            for name in found
            if not any(name.startswith(f"{other}/") for other in found if other != name)
        )
    except Exception:
        return ()


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


def _write_discarded_ref(workspace: Path, head: str, config: LoopConfig) -> _Run:
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
            f"{DISCARDED_REF_PREFIX}/{head}",
            head,
            workspace=workspace,
        ),
        config,
    )


def _is_dirty(workspace: Path, config: LoopConfig) -> bool:
    """Whether the working tree holds anything `clean -ffdqx` would delete.

    `--ignored` and `-uall` because the clean removes ignored and untracked
    files too, and the whole point of asking is that everything it deletes is
    reachable from a ref afterwards. A status that cannot be taken reports
    **dirty**: the capture that follows is non-destructive, and its own failure
    aborts the rollback before anything is removed."""
    run = _run(
        workspace,
        _git(
            config,
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--ignored",
            workspace=workspace,
        ),
        config,
    )
    if run.reason or run.code != 0:
        return True
    return bool(run.out.strip())


def is_repo(workspace: str | Path, config: LoopConfig) -> bool:
    """Whether this workspace is a repository agentloop may act on. Total: it
    returns a `bool` for every input and never raises.

    Containment only: it takes no pin and answers nothing about the config,
    because it is an observation with no side effect. A True here does not
    promise a later `commit` will be accepted — the pin is checked by the
    entry points that actually run something."""
    try:
        if not config.vcs_enabled:
            return False
        return _guard(workspace, config)
    except Exception:
        return False


def init_repo(workspace: str | Path, config: LoopConfig, pin: str = "") -> VcsResult:
    """Create the workspace repo and its empty base commit at `BASE_REF`.

    `git init` runs before the guard because it is the one command that cannot
    reach outside the workspace — it only creates `workspace/.git` — and the
    guard cannot pass before it. The base commit is guarded like everything
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
    workspace: str | Path, message: str, config: LoopConfig, pin: str = ""
) -> VcsResult:
    """Commit the whole workspace. `--allow-empty`, so a round that changed
    nothing still produces a sha.

    `pin` is the fingerprint `init_repo` minted for this workspace, replayed
    from wherever the caller recorded it. There is no default that works: an
    empty one refuses, because a commit under a config nobody vetted is how a
    worker gets `filter.<name>.clean` executed."""
    try:
        if not config.vcs_enabled:
            return VcsResult(ok=False, reason="disabled")
        ws = Path(workspace)
        refusal = _refusal(ws, config, pin)
        if refusal:
            return VcsResult(ok=False, reason=refusal)

        # `-f` as well as `-A`: without it `add` honours a worker-written
        # `.gitignore` while the rollback's `clean -ffdqx` deletes ignored
        # files, so the discarded ref the audit log names was missing exactly
        # the files that were destroyed. Measured: `build/artifact.bin` and
        # `secrets.env` deleted, neither recoverable. Capture everything the
        # rollback can delete; do not weaken the clean.
        run = _run(ws, _git(config, "add", "-A", "-f", workspace=ws), config)
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
    workspace: str | Path, config: LoopConfig, pin: str = ""
) -> VcsResult:
    """Point `APPROVED_REF` at the current tip. Additive and non-destructive:
    it moves a ref and nothing else — but it still runs git in the workspace,
    so it is pinned like everything else."""
    try:
        if not config.vcs_enabled:
            return VcsResult(ok=False, reason="disabled")
        ws = Path(workspace)
        refusal = _refusal(ws, config, pin)
        if refusal:
            return VcsResult(ok=False, reason=refusal)

        head = _head(ws, config)
        if head.reason or head.code != 0:
            return _failed(head)
        sha = head.out.strip()

        run = _run(
            ws, _git(config, "update-ref", APPROVED_REF, sha, workspace=ws), config
        )
        if run.reason or run.code != 0:
            return _failed(run)
        return VcsResult(ok=True, sha=sha)
    except Exception as exc:  # totality (DD-6)
        return VcsResult(ok=False, stderr=_clip(str(exc)), reason="git-failed")


def rollback(
    workspace: str | Path, ref: str, config: LoopConfig, pin: str = ""
) -> VcsResult:
    """Return the workspace to `ref`, keeping what it discards.

    The order is the point: the discarded tip is recorded *before* anything
    moves, and a rollback that cannot record what it is about to discard does
    not discard it. `files_removed` is a pre/post count rather than a parse of
    git's output — `clean -q` prints nothing and `reset --hard` has already
    removed the tracked half, so nothing git prints could produce the number."""
    try:
        if not config.vcs_enabled:
            return VcsResult(ok=False, reason="disabled")
        ws = Path(workspace)
        refusal = _refusal(ws, config, pin)
        if refusal:
            return VcsResult(ok=False, reason=refusal)

        before = _count_files(ws)

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
        nested = _nested_repos(ws)

        # The capture is **unconditional**, and that is the whole invariant:
        # everything the clean deletes is reachable from a ref. Gated on
        # `head_sha != target_sha` it was not — a worker that wrote files and
        # then returned `ESCALATE:` (or an empty output) is returned to the
        # caller *before* the round commit, so a reject of that ordinary shape
        # had files on disk and zero commits, wrote no discarded ref, and let
        # `clean -ffdqx` delete the working tree: measured `files_removed=1`
        # with `sha=""` and `reason=""`, a destruction the audit called a clean
        # rollback. `add -A -f` makes it likelier still, not less.
        if _is_dirty(ws, config):
            # Nested repositories are excluded by pathspec, because git
            # refuses to index one that has no commit checked out ("unable to
            # index file 'sub/'") and one failed path element would otherwise
            # cost the *whole* workspace its capture. Their contents were
            # already outside the recovery surface — a commit records them as
            # a bare gitlink — and `nested_repos` is what names that gap.
            exclusions = (
                ["--", ".", *[f":(exclude){name}" for name in nested]] if nested else []
            )
            run = _run(
                ws, _git(config, "add", "-A", "-f", *exclusions, workspace=ws), config
            )
            if run.reason or run.code != 0:
                return _failed(run, nested_repos=nested)
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
                return _failed(run, nested_repos=nested)
            head = _head(ws, config)
            if head.reason or head.code != 0:
                return _failed(head, nested_repos=nested)
            head_sha = head.out.strip()

        preserved = ""
        if head_sha != target_sha:
            written = _write_discarded_ref(ws, head_sha, config)
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
            return _failed(run, sha=preserved, nested_repos=nested)

        # The second `-f` removes untracked *nested* repositories: a worker
        # that ran `git init` in its own workspace would otherwise leave files
        # behind and silently break redo's "fresh start". It still never
        # touches this repo's own `.git`, which is not working-tree content.
        run = _run(ws, _git(config, "clean", "-ffdqx", workspace=ws), config)
        if run.reason or run.code != 0:
            return _failed(run, sha=preserved, nested_repos=nested)

        after = _count_files(ws)
        removed = max(0, before - after)
        # The pre-rollback tip, so a caller can record *where the discarded
        # round went* without running a git command of its own (DD-12 is only
        # useful if the audit trail names the ref). `""` when HEAD already
        # resolved to the target and nothing was discarded.
        discarded = head_sha if head_sha != target_sha else ""
        reason = ""
        if after:
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
        )
    except Exception as exc:  # totality (DD-6)
        return VcsResult(ok=False, stderr=_clip(str(exc)), reason="git-failed")
