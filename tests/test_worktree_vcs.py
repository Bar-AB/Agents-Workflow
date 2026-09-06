"""Slice 9 P2 - the second legal repository shape: a git worktree.

Real `git` subprocesses, exactly as `test_vcs.py`: the risk this module
contains *is* git's own resolution of a path, and a mocked git could not
exhibit it. Every fixture builds its own "operator repository" under
`tmp_path`; the real project repo is never a subject.

Two shapes now share one guard, so every test here either pins the new
worktree branch or pins that the scratch branch did not move.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agentloop import vcs
from agentloop.config import LoopConfig


# -- fixtures and helpers ----------------------------------------------------


def make_config(root: Path, **kw) -> LoopConfig:
    kw.setdefault("vcs_enabled", True)
    return LoopConfig(workspace_root=str(root), **kw)


def git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    """Read-only git run by the *test*, never by `vcs.py`."""
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, shell=False
    )


_WRITE_PINS = (
    "-c",
    "user.name=t",
    "-c",
    "user.email=t@localhost",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "core.hooksPath=",
)


def git_write(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return subprocess.run(
        ["git", *_WRITE_PINS, *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        shell=False,
        env=env,
    )


def make_repo(path: Path, files: dict[str, str] | None = None) -> Path:
    """An "operator repository" with one commit and real tracked content."""
    path.mkdir(parents=True, exist_ok=True)
    assert git_write(path, "init", "-q", "-b", "main").returncode == 0
    for name, body in (files or {"README.md": "the operators work\n"}).items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    assert git_write(path, "add", "-A").returncode == 0
    assert git_write(path, "commit", "-q", "-m", "initial").returncode == 0
    return path


def _link_dir(link: Path, target: Path) -> bool:
    """A directory link, or False where this platform needs privileges."""
    try:
        if os.name == "nt":
            proc = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True,
                text=True,
                shell=False,
            )
            return proc.returncode == 0 and link.exists()
        os.symlink(target, link, target_is_directory=True)
        return True
    except Exception:
        return False


@pytest.fixture()
def repo_root(tmp_path) -> Path:
    return make_repo(tmp_path / "operator_repo")


@pytest.fixture()
def ws_root(tmp_path) -> Path:
    """Worktrees live OUTSIDE the repository (plan, Decisions)."""
    root = tmp_path / "wt_root"
    root.mkdir()
    return root


@pytest.fixture()
def config(ws_root) -> LoopConfig:
    return make_config(ws_root)


@pytest.fixture()
def worktree(repo_root, ws_root, config):
    """A worktree created through `vcs.init_repo`, with its minted pin."""
    ws = ws_root / "task-1"
    result = vcs.init_repo(ws, config, repo_root=str(repo_root), task_id=1)
    assert result.ok is True, result
    assert result.pin, result
    return ws, result.pin


# -- 1. the guard accepts the new shape --------------------------------------


def test_guard_accepts_a_genuine_worktree(repo_root, ws_root, config):
    """Test 1 [RED-FIRST]. P4: a worktree's `.git` is a *file*, its
    `--absolute-git-dir` is `<repo>/.git/worktrees/<name>` and its
    `--git-common-dir` is `<repo>/.git`, so every scratch-mode condition
    fails on it."""
    ws = ws_root / "task-1"
    assert (
        git_write(
            repo_root, "worktree", "add", str(ws), "-b", "agentloop/task-1"
        ).returncode
        == 0
    )

    # The shape really is the one P4 measured - otherwise this could pass for
    # the wrong reason.
    assert (ws / ".git").is_file()
    assert (repo_root / "README.md").exists() and (ws / "README.md").exists()

    assert vcs._guard(ws, config, repo_root=str(repo_root), task_id=1) is True
    assert vcs.is_repo(ws, config, repo_root=str(repo_root), task_id=1) is True
    # ... and the scratch branch still refuses it: a worktree is not a
    # workspace-owned repository, and nothing may reach the scratch branch by
    # forgetting to pass `repo_root`.
    assert vcs._guard(ws, config) is False


# -- 2-4. the guard refuses every redirection of the new shape ---------------


def test_guard_rejects_a_worktree_of_another_repository(tmp_path, ws_root, config):
    """Test 2 [NEUTER]. The anchor is `repo_root` - operator config, not
    agent-writable. A genuine worktree of some *other* repository satisfies
    every structural property (gitfile `.git`, toplevel == ws) and must still
    be refused, or one operator's `reset --hard` aims at another's history."""
    ours = make_repo(tmp_path / "ours")
    theirs = make_repo(tmp_path / "theirs")
    ws = ws_root / "task-1"
    assert (
        git_write(
            theirs, "worktree", "add", str(ws), "-b", "agentloop/task-1"
        ).returncode
        == 0
    )

    assert (ws / ".git").is_file()
    assert vcs._guard(ws, config, repo_root=str(theirs), task_id=1) is True  # control
    assert vcs._guard(ws, config, repo_root=str(ours), task_id=1) is False


def test_guard_rejects_a_gitfile_pointing_outside_repo_root(
    tmp_path, repo_root, ws_root, config
):
    """Test 3. `<ws>/.git` is a file any worker can rewrite; a `gitdir:`
    pointer at someone else's repository must not pass."""
    victim = make_repo(tmp_path / "victim")
    ws = ws_root / "task-1"
    ws.mkdir()
    (ws / ".git").write_text(f"gitdir: {victim / '.git'}\n", encoding="utf-8")

    assert vcs._guard(ws, config, repo_root=str(repo_root), task_id=1) is False
    assert vcs.is_repo(ws, config, repo_root=str(repo_root), task_id=1) is False


def test_guard_rejects_a_gitfile_pointing_at_the_main_git_dir(
    repo_root, ws_root, config
):
    """Test 3, second shape, and the one the *git-dir* condition exists for -
    the common-dir condition cannot see it. A gitfile reading `gitdir:
    <repo_root>/.git` (rather than `.../worktrees/<name>`) makes the workspace
    a second checkout of the main repository sharing its **HEAD and index**,
    so `--git-common-dir` is legitimately `<repo_root>/.git` and only the
    admin-directory identity refuses it. Measured pre-guard: a `reset --hard`
    from there moves the operator's own branch and rewrites their real
    checkout."""
    ws = ws_root / "task-1"
    ws.mkdir()
    (ws / ".git").write_text(f"gitdir: {repo_root / '.git'}\n", encoding="utf-8")

    # The controls: this really is the shape described, and the condition that
    # catches it is the git-dir one and not the common-dir one.
    located = git(ws, "rev-parse", "--show-toplevel", "--git-common-dir").stdout.split()
    assert vcs._same_path(located[0], ws) is True
    assert vcs._same_path(located[1], repo_root / ".git") is True

    assert vcs._guard(ws, config, repo_root=str(repo_root), task_id=1) is False


def test_guard_rejects_a_commondir_write_inside_a_worktree(
    tmp_path, repo_root, config, worktree
):
    """Test 4. The escape that defeated four path conditions in slice 6: an
    ordinary file write to `commondir` redirects every ref and object write.
    In a worktree the per-worktree git dir lives in the main repo, so the file
    is not workspace-writable *by path* - but the guard must refuse the
    redirection wherever it is written from, because reading the location git
    *resolved* is the only check that survives the next shape of redirect."""
    ws, _pin = worktree
    victim = make_repo(tmp_path / "victim")
    git_dir = Path(git(ws, "rev-parse", "--absolute-git-dir").stdout.strip())

    assert (
        vcs._guard(ws, config, repo_root=str(repo_root), task_id=1) is True
    )  # control
    (git_dir / "commondir").write_text(str(victim / ".git"), encoding="utf-8")

    assert vcs._guard(ws, config, repo_root=str(repo_root), task_id=1) is False


# -- 5. the scratch branch did not move --------------------------------------


def _scratch_repo(ws_root: Path, config: LoopConfig, name: str = "task-1") -> Path:
    ws = ws_root / name
    ws.mkdir()
    assert vcs.init_repo(ws, config).ok is True
    return ws


def test_every_measured_scratch_escape_stays_closed(tmp_path, ws_root, config):
    """Test 5. Regression over the existing suite rather than new cases: all
    five previously measured escapes, re-run with the worktree branch present.
    A `repo_root=None` call must take exactly the path it took before."""
    victim = make_repo(tmp_path / "victim")

    # (a) a junction at `<ws>` pointing at another repository.
    linked_ws = ws_root / "task-a"
    if _link_dir(linked_ws, victim):
        assert vcs._guard(linked_ws, config) is False

    # (b) a junction at `<ws>/.git` pointing at another repository's `.git`.
    ws_b = ws_root / "task-b"
    ws_b.mkdir()
    if _link_dir(ws_b / ".git", victim / ".git"):
        assert vcs._guard(ws_b, config) is False

    # (c) a `commondir` file write, (d) aimed at a sibling directory inside
    # the workspace, and (e) aimed at a sibling *task's* repository.
    ws_c = _scratch_repo(ws_root, config, "task-c")
    sibling_task = _scratch_repo(ws_root, config, "task-d")
    inside = ws_c / "inside"
    inside.mkdir()
    assert git_write(inside, "init", "-q").returncode == 0

    assert vcs._guard(ws_c, config) is True  # control
    (ws_c / ".git" / "commondir").write_text(str(victim / ".git"), encoding="utf-8")
    assert vcs._guard(ws_c, config) is False
    (ws_c / ".git" / "commondir").write_text("../inside/.git", encoding="utf-8")
    assert vcs._guard(ws_c, config) is False
    (ws_c / ".git" / "commondir").write_text(
        str(sibling_task / ".git"), encoding="utf-8"
    )
    assert vcs._guard(ws_c, config) is False


# -- 6-8. the pin ------------------------------------------------------------


def test_config_pin_fingerprints_the_common_config(repo_root, ws_root, config):
    """Test 6 [RED-FIRST]. P2: `git config --local` from inside a worktree
    writes the MAIN repository's config, so the pin has to fingerprint
    `<repo_root>/.git/config`. `<ws>/.git` is a file - there is no config
    under it to fingerprint at all."""
    ws = ws_root / "task-1"
    assert (
        git_write(
            repo_root, "worktree", "add", str(ws), "-b", "agentloop/task-1"
        ).returncode
        == 0
    )

    expected = vcs.config_pin(repo_root)
    assert expected != ""
    assert vcs.config_pin(ws, repo_root=str(repo_root)) == expected
    # And the scratch derivation genuinely finds nothing here, so the two are
    # not accidentally equal.
    assert vcs.config_pin(ws) == ""


def test_poisoning_the_common_config_refuses_the_next_side_effecting_call(
    repo_root, config, worktree
):
    """Test 7 [NEUTER]. P2 end to end: the measured attack. `filter.<name>.clean`
    names a program git runs on an ordinary `add`, and from inside a worktree
    `git config --local` writes it into the operator's real config."""
    ws, pin = worktree
    (ws / "work.txt").write_text("round 1", encoding="utf-8")
    assert vcs.commit(
        ws, "round 1", config, pin=pin, repo_root=str(repo_root), task_id=1
    ).ok

    # The attack, run the way a worker would: from inside its own workspace.
    assert (
        git_write(ws, "config", "--local", "filter.pwn.clean", "echo pwned").returncode
        == 0
    )
    # It really did land in the operator's config (this is the escalation the
    # plan names in Residuals, not something the pin prevents).
    assert "filter" in (repo_root / ".git" / "config").read_text(encoding="utf-8")

    (ws / "work.txt").write_text("round 2", encoding="utf-8")
    result = vcs.commit(
        ws, "round 2", config, pin=pin, repo_root=str(repo_root), task_id=1
    )
    assert result.ok is False
    assert result.reason == "config-changed"
    for call in (
        lambda: vcs.mark_approved(
            ws, config, pin=pin, repo_root=str(repo_root), task_id=1
        ),
        lambda: vcs.rollback(
            ws, vcs.base_ref(1), config, pin=pin, repo_root=str(repo_root), task_id=1
        ),
        lambda: vcs.remove_worktree(
            ws, config, pin=pin, repo_root=str(repo_root), task_id=1
        ),
    ):
        assert call().reason == "config-changed"


def test_a_pin_is_minted_only_when_the_worktree_is_created(repo_root, config, worktree):
    """Test 8. Re-pinning a worktree found already there would bless an
    attacker's edit on the next round and turn the gate into their signature."""
    ws, pin = worktree

    again = vcs.init_repo(ws, config, pin=pin, repo_root=str(repo_root), task_id=1)
    assert again.ok is True
    assert again.reason == "already"
    assert again.pin == ""

    # Even an edited config gets no fresh pin: it is refused, not re-minted.
    assert (
        git_write(ws, "config", "--local", "filter.pwn.clean", "echo pwned").returncode
        == 0
    )
    refused = vcs.init_repo(ws, config, pin=pin, repo_root=str(repo_root), task_id=1)
    assert refused.ok is False
    assert refused.reason == "config-changed"
    assert refused.pin == ""


def test_an_unpinned_worktree_call_refuses(repo_root, config, worktree):
    """The `""` pin is the "cannot tell" value, never a match."""
    ws, _pin = worktree
    result = vcs.commit(
        ws, "round 1", config, pin="", repo_root=str(repo_root), task_id=1
    )
    assert result.ok is False
    assert result.reason == "config-unpinned"


# -- C3: the pin is a REPO-level baseline, not a per-task mint ---------------
#
# Slice 9 P2 remediation. `_init_worktree` minted a fresh pin per workspace,
# against `init_repo`'s own contract ("never re-pinned on one found already
# there"). That contract is stated per-*workspace*, and in scratch mode
# workspace and config were 1:1 so per-task minting was sound. In worktree mode
# every task shares one `<repo_root>/.git/config`, which is pre-existing and
# agent-writable, so "never re-pinned" was true of the worktree and false of the
# config -- the thing the pin is actually about.
#
# Measured before the fix: task 1 poisons `filter.pwn.clean`, task 1's next
# round is correctly refused `config-changed`, and then task 2 -- a different
# task, an ordinary `init_repo`, no attack of its own -- mints a fresh pin over
# the poison and executes it on its first round commit. Arbitrary command
# execution outside every workspace, reported as `ok=True, reason=""`.


def _poison_filter(repo_root: Path, canary: Path) -> None:
    """`filter.<name>.clean` names a program git runs on an ordinary `add`.

    Written through `git config --local` from inside the operator's repository,
    which is exactly what a worker holding the shipped `git` tool can do from
    inside its own worktree (probe 2: `--local` from a worktree writes the main
    repo's config)."""
    script = repo_root.parent / "pwn.py"
    script.write_text(
        "import sys\n"
        f"open({str(canary)!r}, 'w', encoding='utf-8').write('pwned')\n"
        "sys.stdout.write(sys.stdin.read())\n",
        encoding="utf-8",
    )
    import sys as _sys

    cmd = f'"{_sys.executable}" "{script}"'
    assert (
        git_write(repo_root, "config", "--local", "filter.pwn.clean", cmd).returncode
        == 0
    )
    (repo_root / ".gitattributes").write_text("* filter=pwn\n", encoding="utf-8")
    assert git_write(repo_root, "add", "-A").returncode == 0
    assert git_write(repo_root, "commit", "-q", "-m", "attrs").returncode == 0


def test_a_poisoned_common_config_is_not_laundered_into_the_next_task(
    repo_root, ws_root, config, tmp_path
):
    """C3, the measured attack. Task 1 poisons the shared config; task 2 must
    refuse rather than mint a fresh pin over it.

    [NEUTER] by restoring the per-task mint (`minted = config_pin(ws,
    repo_root)` unconditionally on the create branch): task 2's `init_repo`
    goes back to `ok=True` and the canary fires."""
    canary = tmp_path / "canary.txt"

    # Task 1 is the first task on this repository, so it blesses the operator's
    # pre-existing config. That is the honest residual and it is stated: only
    # the *first* task establishes the baseline.
    first = vcs.init_repo(
        ws_root / "task-1", config, repo_root=str(repo_root), task_id=1
    )
    assert first.ok is True and first.pin
    baseline = first.pin

    _poison_filter(repo_root, canary)
    # CONTROL: the test's own `git add -A` above ran the filter, which proves
    # the execution channel is live rather than assumed. Clear it, so every
    # later assertion is about what `vcs.py` spawned.
    assert canary.exists() is True
    canary.unlink()

    # Within task 1 the pin already worked, and still must.
    within = vcs.commit(
        ws_root / "task-1",
        "round 2",
        config,
        pin=baseline,
        repo_root=str(repo_root),
        task_id=1,
    )
    assert within.ok is False
    assert within.reason == "config-changed"
    assert canary.exists() is False

    # The defect: a *different* task, replaying the same recorded baseline.
    second = vcs.init_repo(
        ws_root / "task-2", config, pin=baseline, repo_root=str(repo_root), task_id=2
    )
    assert second.ok is False, second
    assert second.reason == "config-changed"
    assert second.pin == ""
    # And nothing spawned: `git worktree add` itself runs a checkout, which
    # runs smudge filters, so refusing after the spawn would be no refusal.
    assert (ws_root / "task-2").exists() is False
    assert canary.exists() is False


def test_human_redo_cannot_relaunder_the_config_within_one_task(
    repo_root, ws_root, config, tmp_path
):
    """The same laundering inside one task: `human_redo` removes and re-inits
    the worktree, so the one recovery action an operator takes on a
    `config-changed` refusal was the action that armed it."""
    canary = tmp_path / "canary.txt"
    ws = ws_root / "task-1"
    first = vcs.init_repo(ws, config, repo_root=str(repo_root), task_id=1)
    assert first.ok is True and first.pin
    baseline = first.pin

    _poison_filter(repo_root, canary)
    assert canary.exists() is True  # CONTROL: the channel is live.
    canary.unlink()

    removed = vcs.remove_worktree(
        ws, config, pin=baseline, repo_root=str(repo_root), task_id=1
    )
    # The removal itself runs under the poisoned config and must refuse.
    assert removed.ok is False
    assert removed.reason == "config-changed"

    # Even with the directory gone by other means, re-init must not re-mint.
    import shutil

    shutil.rmtree(ws, ignore_errors=True)
    assert git_write(repo_root, "worktree", "prune").returncode == 0
    again = vcs.init_repo(ws, config, pin=baseline, repo_root=str(repo_root), task_id=1)
    assert again.ok is False
    assert again.reason == "config-changed"
    assert canary.exists() is False


# -- C2: the admin directory is identity, not containment --------------------


def _repoint_gitfile(ws: Path, target: Path) -> None:
    """Rewrite `<ws>/.git`, which is an ordinary (read-only) file inside the
    agent-writable workspace and so is exactly what a worker can rewrite."""
    import stat as _stat

    gitfile = ws / ".git"
    os.chmod(gitfile, _stat.S_IWRITE)
    gitfile.unlink()
    gitfile.write_text(f"gitdir: {target}\n", encoding="utf-8")


def test_guard_rejects_a_worktree_pointed_at_a_sibling_tasks_admin_dir(
    repo_root, ws_root, config
):
    """C2, the reviewer's measured attack, and it needs nothing fabricated:
    **a sibling worktree already exists.**

    `<repo_root>/.git/worktrees` is the parent of *every* task's admin
    directory, exactly as `workspace_root` is the parent of every task
    workspace -- and CLAUDE.md records that substitution as the slice-6 escape
    verbatim ("containment admitted ... a sibling task's repository, aiming one
    task's `reset --hard` / `clean -ffdqx` at another's history"). Asserting
    the *parent* reintroduced the same shape one directory up.

    [NEUTER] by dropping the `<admin>/gitdir` back-pointer condition: the guard
    goes True and task 2's commit moves task 1's branch."""
    ws1, ws2 = ws_root / "task-1", ws_root / "task-2"
    first = vcs.init_repo(ws1, config, repo_root=str(repo_root), task_id=1)
    assert first.ok is True
    baseline = first.pin
    second = vcs.init_repo(
        ws2, config, pin=baseline, repo_root=str(repo_root), task_id=2
    )
    assert second.ok is True, second

    (ws1 / "task1_work.py").write_text("keep me\n", encoding="utf-8")
    assert vcs.commit(
        ws1, "task 1 round", config, pin=baseline, repo_root=str(repo_root), task_id=1
    ).ok
    before = git(repo_root, "rev-parse", "agentloop/task-1").stdout.strip()
    assert before

    # The attack: task 2's worker aims its own workspace at task 1's admin
    # directory. Both are genuine worktrees of this repository, so every
    # *containment* condition still holds.
    _repoint_gitfile(ws2, repo_root / ".git" / "worktrees" / "task-1")

    assert vcs._guard(ws2, config, repo_root=str(repo_root), task_id=2) is False

    (ws2 / "evil.py").write_text("pwn\n", encoding="utf-8")
    hijack = vcs.commit(
        ws2, "task 2 round", config, pin=baseline, repo_root=str(repo_root), task_id=2
    )
    assert hijack.ok is False
    assert hijack.reason == "not-a-workspace-repo"

    # Task 1's branch and its work are untouched.
    assert git(repo_root, "rev-parse", "agentloop/task-1").stdout.strip() == before
    tree = git(repo_root, "ls-tree", "--name-only", "agentloop/task-1").stdout.split()
    assert "task1_work.py" in tree
    assert "evil.py" not in tree

    # ... and a rollback aimed through the same redirect is refused too, which
    # is the half that would have rewritten another task's history.
    rolled = vcs.rollback(
        ws2,
        vcs.base_ref(2),
        config,
        pin=baseline,
        repo_root=str(repo_root),
        task_id=2,
    )
    assert rolled.ok is False
    assert rolled.reason == "not-a-workspace-repo"
    assert git(repo_root, "rev-parse", "agentloop/task-1").stdout.strip() == before


# -- C1: the guard must constrain which BRANCH is checked out ----------------
#
# Every other condition establishes *where git's object and ref stores are*. In
# scratch mode that was the same fact as "which history is at risk", because the
# store was the throwaway repo. In worktree mode the ref store is legitimately
# the operator's, so nothing constrained the ref a `commit` moves or a `reset
# --hard` rewrites.


def test_guard_rejects_a_worktree_switched_onto_the_operators_branch(
    repo_root, ws_root, config
):
    """C1, the hunter's measured attack, using **only** the shipped worker
    spec's declared `git` tool from inside its own workspace.

    `git switch` and `git checkout` are refused by git's own worktree lock
    ("'main' is already used by worktree at ..."). `git symbolic-ref HEAD` is
    not, and it is one plumbing call: measured rc=0, guard True, commit ok,
    rollback ok, and the operator's `main` moved to the task's work -- with
    `reason == ""` throughout, so `_vcs_degraded` never fired and the audit log
    called it a clean rollback.

    [NEUTER] by removing the HEAD condition from `_guard_worktree`."""
    ws = ws_root / "task-1"
    first = vcs.init_repo(ws, config, repo_root=str(repo_root), task_id=1)
    assert first.ok is True
    pin = first.pin
    main_before = git(repo_root, "rev-parse", "main").stdout.strip()

    # The controls: git's own lock refuses the two porcelain routes.
    assert git_write(ws, "switch", "main").returncode != 0
    assert git_write(ws, "checkout", "main").returncode != 0
    # ... and does not refuse the plumbing one.
    assert git_write(ws, "symbolic-ref", "HEAD", "refs/heads/main").returncode == 0
    assert (
        git(ws, "symbolic-ref", "--quiet", "HEAD").stdout.strip() == "refs/heads/main"
    )

    assert vcs._guard(ws, config, repo_root=str(repo_root), task_id=1) is False

    (ws / "evil.py").write_text("pwn\n", encoding="utf-8")
    hijack = vcs.commit(
        ws, "round", config, pin=pin, repo_root=str(repo_root), task_id=1
    )
    assert hijack.ok is False
    assert hijack.reason == "not-a-workspace-repo"

    rolled = vcs.rollback(
        ws, vcs.base_ref(1), config, pin=pin, repo_root=str(repo_root), task_id=1
    )
    assert rolled.ok is False
    assert rolled.reason == "not-a-workspace-repo"

    assert git(repo_root, "rev-parse", "main").stdout.strip() == main_before


def test_guard_rejects_a_detached_head_in_a_worktree(repo_root, ws_root, config):
    """A detached HEAD names no branch, so the condition cannot be satisfied
    and must refuse rather than be skipped."""
    ws = ws_root / "task-1"
    assert vcs.init_repo(ws, config, repo_root=str(repo_root), task_id=1).ok is True
    sha = git(ws, "rev-parse", "HEAD").stdout.strip()
    assert git_write(ws, "checkout", "--detach", sha).returncode == 0
    assert vcs._guard(ws, config, repo_root=str(repo_root), task_id=1) is False


def test_guard_rejects_a_worktree_claiming_another_tasks_branch(
    repo_root, ws_root, config
):
    """The branch must be *this task's*. A workspace on `agentloop/task-1`
    acting as task 2 is the cross-task ref collision one rename away."""
    ws = ws_root / "task-1"
    assert vcs.init_repo(ws, config, repo_root=str(repo_root), task_id=1).ok is True
    assert vcs._guard(ws, config, repo_root=str(repo_root), task_id=1) is True
    assert vcs._guard(ws, config, repo_root=str(repo_root), task_id=2) is False


def test_a_worktree_call_without_a_task_id_refuses(repo_root, ws_root, config):
    """ "Which task owns this workspace" is part of containment in worktree
    mode, so the id is not optional. `no-task-id` is the documented reason and
    it is reachable from every ref-writing path."""
    ws = ws_root / "task-1"
    first = vcs.init_repo(ws, config, repo_root=str(repo_root), task_id=1)
    assert first.ok is True
    assert vcs._guard(ws, config, repo_root=str(repo_root)) is False
    assert vcs.is_repo(ws, config, repo_root=str(repo_root)) is False
    result = vcs.commit(ws, "round", config, pin=first.pin, repo_root=str(repo_root))
    assert result.ok is False
    assert result.reason == "no-task-id"


def test_guard_rejects_a_fabricated_admin_directory(repo_root, ws_root, config):
    """The hunter's second variant: a worker fabricates
    `<repo_root>/.git/worktrees/evil/{commondir,gitdir,HEAD}` and aims its own
    gitfile at it. The back-pointer condition (C2) passes here *by
    construction* -- the attacker writes `gitdir` -- so what closes it is the
    HEAD condition, which is why both exist."""
    ws = ws_root / "task-1"
    first = vcs.init_repo(ws, config, repo_root=str(repo_root), task_id=1)
    assert first.ok is True
    main_before = git(repo_root, "rev-parse", "main").stdout.strip()

    evil = repo_root / ".git" / "worktrees" / "evil"
    evil.mkdir(parents=True)
    (evil / "commondir").write_text("../..\n", encoding="utf-8")
    (evil / "gitdir").write_text(f"{ws / '.git'}\n", encoding="utf-8")
    (evil / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    _repoint_gitfile(ws, evil)

    assert vcs._guard(ws, config, repo_root=str(repo_root), task_id=1) is False
    (ws / "evil.py").write_text("pwn\n", encoding="utf-8")
    hijack = vcs.commit(
        ws, "round", config, pin=first.pin, repo_root=str(repo_root), task_id=1
    )
    assert hijack.ok is False
    assert git(repo_root, "rev-parse", "main").stdout.strip() == main_before


def test_guard_rejects_an_admin_dir_whose_back_pointer_lies(repo_root, ws_root, config):
    """The inverse of `test_guard_rejects_a_fabricated_admin_directory`, and the
    only shape that isolates C2.

    That test writes a truthful `gitdir` and a hostile `HEAD`, so C2 passes by
    construction and **C1** is what refuses it. Every other worktree test is the
    same way round. Measured consequence: with the back-pointer condition
    deleted entirely, all 933 tests still passed -- C2 was correct and
    unfalsifiable, which is this project's documented "reads as coverage"
    failure, not a missing edge case.

    So this fabricates the mirror image: `HEAD` names *this* task's own branch,
    satisfying C1 exactly, while `gitdir` names a directory that is not
    `<ws>/.git`. Toplevel, HEAD and the common dir are all what the guard wants;
    only the back-pointer disagrees. If C2 is removed, this goes green.
    """
    ws = ws_root / "task-1"
    first = vcs.init_repo(ws, config, repo_root=str(repo_root), task_id=1)
    assert first.ok is True
    main_before = git(repo_root, "rev-parse", "main").stdout.strip()

    evil = repo_root / ".git" / "worktrees" / "evil"
    evil.mkdir(parents=True)
    (evil / "commondir").write_text("../..\n", encoding="utf-8")
    (evil / "HEAD").write_text("ref: refs/heads/agentloop/task-1\n", encoding="utf-8")
    # The lie, and the only condition that is wrong: this is not `<ws>/.git`.
    (evil / "gitdir").write_text(
        f"{ws_root / 'somewhere-else' / '.git'}\n", encoding="utf-8"
    )
    real_admin = repo_root / ".git" / "worktrees" / "task-1"
    for name in ("index", "ORIG_HEAD"):
        src = real_admin / name
        if src.exists():
            shutil.copy2(src, evil / name)
    _repoint_gitfile(ws, evil)

    # Controls: prove C1's conditions really are all satisfied, so a refusal
    # below can only have come from the back-pointer.
    assert git(ws, "symbolic-ref", "--quiet", "HEAD").stdout.strip() == (
        "refs/heads/agentloop/task-1"
    )
    assert os.path.normcase(
        os.path.abspath(git(ws, "rev-parse", "--show-toplevel").stdout.strip())
    ) == os.path.normcase(os.path.abspath(str(ws)))
    assert os.path.normcase(
        os.path.abspath(git(ws, "rev-parse", "--git-common-dir").stdout.strip())
    ) == os.path.normcase(os.path.abspath(str(repo_root / ".git")))

    assert vcs._guard(ws, config, repo_root=str(repo_root), task_id=1) is False
    (ws / "evil.py").write_text("pwn\n", encoding="utf-8")
    hijack = vcs.commit(
        ws, "round", config, pin=first.pin, repo_root=str(repo_root), task_id=1
    )
    assert hijack.ok is False
    assert git(repo_root, "rev-parse", "main").stdout.strip() == main_before
