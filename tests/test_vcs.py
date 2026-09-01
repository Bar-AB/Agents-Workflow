"""Per-task workspace git repos (roadmap slice 6, Part 1).

Real `git` subprocesses against throwaway repos built inside `tmp_path` — the
risk this module exists to contain *is* the real subprocess's cwd resolution
(in a repo-less directory `git rev-parse --show-toplevel` walks up to the
enclosing repository), and a mocked git could not exhibit it. The operator's
real project repository is never a subject of a test: every fixture builds its
own parent repo under `tmp_path`.
"""

from __future__ import annotations

import json
import os
import subprocess
import shutil
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from agentloop import vcs
from agentloop.config import LoopConfig
from agentloop.executor import clear_workspace


# -- fixtures and helpers ----------------------------------------------------


def make_config(root: Path, **kw) -> LoopConfig:
    kw.setdefault("vcs_enabled", True)
    return LoopConfig(workspace_root=str(root), **kw)


def git(ws: Path, *args: str) -> subprocess.CompletedProcess:
    """Read-only git, run by the *test* (not by vcs.py) to check the result."""
    return subprocess.run(
        ["git", *args], cwd=str(ws), capture_output=True, text=True, shell=False
    )


def write(ws: Path, name: str, body: str = "x") -> None:
    (ws / name).write_text(body, encoding="utf-8")


def _pin(ws: Path) -> str:
    """The config pin, the way the loop supplies it: recorded when the repo was
    created and replayed on every later call.

    Recomputed here rather than captured, so it matches for every test that
    does not touch `.git/config` — which is all of them except the pin tests at
    the bottom of this file, and those capture theirs *before* the attack,
    because a pin taken afterwards would pin the attack."""
    return vcs.config_pin(ws)


def non_git_files(ws: Path) -> list[str]:
    return sorted(
        str(p.relative_to(ws))
        for p in ws.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(ws).parts
    )


@pytest.fixture()
def ws_root(tmp_path) -> Path:
    root = tmp_path / "ws_root"
    root.mkdir()
    return root


@pytest.fixture()
def config(ws_root) -> LoopConfig:
    return make_config(ws_root)


@pytest.fixture()
def ws(ws_root) -> Path:
    d = ws_root / "task-1"
    d.mkdir()
    return d


@pytest.fixture()
def repo(ws, config) -> Path:
    result = vcs.init_repo(ws, config)
    assert result.ok, result
    return ws


def stub_git(tmp_path: Path) -> str:
    """A fake `git` that always exits 1 with a message on stderr.

    Portable across platforms without chmod games on a directory."""
    if os.name == "nt":
        path = tmp_path / "stub_git.bat"
        path.write_text("@echo off\r\necho stub failure 1>&2\r\nexit /b 1\r\n")
    else:
        path = tmp_path / "stub_git.sh"
        path.write_text("#!/bin/sh\necho 'stub failure' 1>&2\nexit 1\n")
        os.chmod(path, 0o755)
    return str(path)


# -- init --------------------------------------------------------------------


def test_init_creates_a_base_ref(ws, config):
    result = vcs.init_repo(ws, config)

    assert result.ok is True
    assert result.reason == ""
    assert len(result.sha) == 40
    assert vcs.is_repo(ws, config) is True
    assert git(ws, "rev-parse", vcs.BASE_REF).returncode == 0
    assert len(git(ws, "log", "--oneline").stdout.strip().splitlines()) == 1


def test_init_is_idempotent(repo, config):
    before = git(repo, "log", "--oneline").stdout

    result = vcs.init_repo(repo, config, pin=_pin(repo))

    assert result.ok is True
    assert result.reason == "already"
    assert git(repo, "log", "--oneline").stdout == before


# -- commit ------------------------------------------------------------------


def test_commit_returns_a_sha_and_records_the_files(repo, config):
    write(repo, "out.txt", "worker output")

    result = vcs.commit(repo, "round 1", config, pin=_pin(repo))

    assert result.ok is True
    assert result.reason == ""
    assert len(result.sha) == 40
    assert "out.txt" in git(repo, "ls-tree", "-r", "--name-only", "HEAD").stdout
    assert result.sha == git(repo, "rev-parse", "HEAD").stdout.strip()


def test_commit_succeeds_with_no_ambient_git_identity(ws, config, monkeypatch):
    """#5 — the scrubbed child env has no identity, so `-c` must carry it.

    Also plants a worker-written `pre-commit` hook *inside* the workspace: `-c
    core.hooksPath=` sets an empty value and neither document states what git
    resolves that to. `vcs.commit` fires on every worker round by default, on a
    path that is not behind `allow_test_exec`, so a workspace hook executing
    there would run model-written code outside the sandbox gate."""
    monkeypatch.delenv("GIT_AUTHOR_NAME", raising=False)
    monkeypatch.delenv("GIT_COMMITTER_NAME", raising=False)
    assert vcs.init_repo(ws, config).ok

    canary = ws.parent / "hook-ran.txt"
    hook = "#!/bin/sh\necho hit > '%s'\n" % str(canary).replace("\\", "/")
    hooks_dir = ws / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    (hooks_dir / "pre-commit").write_text(hook)
    os.chmod(hooks_dir / "pre-commit", 0o755)
    (ws / "pre-commit").write_text(hook)
    os.chmod(ws / "pre-commit", 0o755)

    result = vcs.commit(ws, "round 1", config, pin=_pin(ws))

    assert result.ok is True, result.stderr
    assert len(result.sha) == 40
    assert canary.exists() is False, "a workspace-planted hook ran during commit"


def test_commit_without_the_identity_flags_fails(ws, config, monkeypatch):
    """Control for the test above: it varies the *production code* (the
    identity constant), so a passing sibling proves the flags did the work and
    not some ambient git identity leaking through the env scrub."""
    assert vcs.init_repo(ws, config).ok
    monkeypatch.setattr(vcs, "_IDENTITY", ())

    result = vcs.commit(ws, "round 1", config, pin=_pin(ws))

    assert result.ok is False
    assert result.reason == "git-failed"


def plant_hostile_gitconfig(tmp_path: Path, monkeypatch) -> Path:
    """A `~/.gitconfig` that fails every commit and runs a hook outside the
    workspace — the ordinary operator config measured to do both."""
    home = tmp_path / "home"
    home.mkdir()
    hooks = tmp_path / "operator-hooks"
    hooks.mkdir()
    canary = tmp_path / "operator-canary.txt"
    (hooks / "pre-commit").write_text(
        "#!/bin/sh\necho hit > '%s'\n" % str(canary).replace("\\", "/")
    )
    os.chmod(hooks / "pre-commit", 0o755)
    (home / ".gitconfig").write_text(
        "[commit]\n\tgpgsign = true\n[core]\n\thooksPath = %s\n"
        % str(hooks).replace("\\", "/")
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return canary


def test_commit_survives_a_hostile_global_git_config(tmp_path, ws, config, monkeypatch):
    """D-2b / DD-13. GIT_CONFIG_NOSYSTEM suppresses only the *system* config."""
    canary = plant_hostile_gitconfig(tmp_path, monkeypatch)
    assert vcs.init_repo(ws, config).ok
    write(ws, "out.txt")

    result = vcs.commit(ws, "round 1", config, pin=_pin(ws))

    assert result.ok is True, result.stderr
    assert len(result.sha) == 40
    assert canary.exists() is False, "the operator's hook wrote outside the workspace"


def test_hostile_global_git_config_bites_without_the_hardening(
    tmp_path, ws, config, monkeypatch
):
    """Control that varies the production code, not the fixture: with the `-c`
    pins and GIT_CONFIG_GLOBAL removed the same planted config must fail the
    commit *and* create the canary. Without this, the passing half above could
    be passing because the config never reached git at all."""
    canary = plant_hostile_gitconfig(tmp_path, monkeypatch)
    assert vcs.init_repo(ws, config).ok

    monkeypatch.setattr(vcs, "_CONFIG_PINS", ())
    soft_env = dict(vcs._child_env())
    soft_env.pop("GIT_CONFIG_GLOBAL", None)
    monkeypatch.setattr(vcs, "_child_env", lambda: dict(soft_env))

    result = vcs.commit(ws, "round 1", config, pin=_pin(ws))

    assert result.ok is False
    assert result.reason == "git-failed"
    assert canary.exists() is True


def test_commit_accepts_an_empty_and_a_hostile_message(repo, config):
    """argv, not a shell string: a leading `-` can never be read as a flag."""
    empty = vcs.commit(repo, "", config, pin=_pin(repo))
    assert empty.ok is True, empty.stderr

    hostile = vcs.commit(repo, "--amend\nrm -rf /", config, pin=_pin(repo))
    assert hostile.ok is True, hostile.stderr
    assert len(git(repo, "log", "--oneline").stdout.strip().splitlines()) == 3


# -- mark_approved -----------------------------------------------------------


def test_mark_approved_points_the_approved_ref_at_head(repo, config):
    write(repo, "out.txt")
    round_sha = vcs.commit(repo, "round 1", config, pin=_pin(repo)).sha

    result = vcs.mark_approved(repo, config, pin=_pin(repo))

    assert result.ok is True
    assert result.sha == round_sha
    assert git(repo, "rev-parse", vcs.APPROVED_REF).stdout.strip() == round_sha


# -- the containment guard (D-1 / G-1 / DD-16) -------------------------------


@pytest.fixture()
def nested(tmp_path):
    """The D-1 topology, entirely inside tmp_path: a throwaway parent repo with
    a plain (repo-less) subdirectory standing in for a workspace."""
    outer = tmp_path / "outer"
    outer.mkdir()
    assert git(outer, "init", "-q").returncode == 0
    (outer / "tracked.txt").write_text("original", encoding="utf-8")
    assert git(outer, "add", "-A").returncode == 0
    assert (
        git(
            outer,
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@localhost",
            "commit",
            "-m",
            "base",
        ).returncode
        == 0
    )
    (outer / "tracked.txt").write_text("modified", encoding="utf-8")
    inner = outer / "ws"
    inner.mkdir()
    (inner / "keep.txt").write_text("keep", encoding="utf-8")
    return outer, inner


def test_rollback_refuses_a_workspace_that_is_not_its_own_repo(nested):
    outer, inner = nested
    config = make_config(outer)

    result = vcs.rollback(inner, vcs.BASE_REF, config, pin=_pin(inner))

    assert result.ok is False
    assert result.reason == "not-a-workspace-repo"
    assert (inner / "keep.txt").exists()
    assert (outer / "tracked.txt").read_text(encoding="utf-8") == "modified"


def test_rollback_succeeds_once_the_workspace_is_its_own_repo(nested):
    """Control for the refusal: only the missing repo made it refuse."""
    outer, inner = nested
    config = make_config(outer)
    assert vcs.init_repo(inner, config).ok

    result = vcs.rollback(inner, vcs.BASE_REF, config, pin=_pin(inner))

    assert result.ok is True, result.stderr
    assert result.reason == ""
    assert (outer / "tracked.txt").read_text(encoding="utf-8") == "modified"


def test_commit_refuses_a_workspace_that_is_not_its_own_repo(nested):
    """G-1: `git add -A` + `commit` in a repo-less workspace would stage and
    commit the enclosing repository's entire working tree."""
    outer, inner = nested
    config = make_config(outer)

    result = vcs.commit(inner, "round 1", config, pin=_pin(inner))

    assert result.ok is False
    assert result.reason == "not-a-workspace-repo"
    porcelain = git(outer, "status", "--porcelain", "-uall").stdout
    assert "ws/keep.txt" in porcelain.replace("\\", "/")
    assert git(outer, "diff", "--cached", "--name-only").stdout.strip() == ""


def test_commit_succeeds_once_the_workspace_is_its_own_repo(nested):
    outer, inner = nested
    config = make_config(outer)
    assert vcs.init_repo(inner, config).ok

    result = vcs.commit(inner, "round 1", config, pin=_pin(inner))

    assert result.ok is True, result.stderr
    assert len(result.sha) == 40


def test_a_workspace_outside_the_workspace_root_is_refused(tmp_path):
    """DD-16's third condition: conditions 1 and 2 hold (it really is its own
    repo root) and the guard must still refuse, because a junction at
    workspace_root/task-N could point at any repo on disk."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    outside = elsewhere / "task-1"
    outside.mkdir()
    assert vcs.init_repo(outside, make_config(elsewhere)).ok
    write(outside, "out.txt")

    root = tmp_path / "ws_root"
    root.mkdir()
    strict = make_config(root)

    assert vcs.is_repo(outside, strict) is False
    result = vcs.rollback(outside, vcs.BASE_REF, strict, pin=_pin(outside))

    assert result.ok is False
    assert result.reason == "not-a-workspace-repo"
    assert (outside / "out.txt").exists()


def test_the_same_repo_under_the_workspace_root_is_accepted(tmp_path):
    """Control: only its location made the identical call refuse."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    built = elsewhere / "task-1"
    built.mkdir()
    assert vcs.init_repo(built, make_config(elsewhere)).ok
    write(built, "out.txt")

    root = tmp_path / "ws_root"
    root.mkdir()
    moved = root / "task-1"
    shutil.move(str(built), str(moved))
    strict = make_config(root)

    assert vcs.is_repo(moved, strict) is True
    result = vcs.rollback(moved, vcs.BASE_REF, strict, pin=_pin(moved))

    assert result.ok is True, result.stderr
    assert (moved / "out.txt").exists() is False


@pytest.mark.parametrize(
    "child, parent",
    [
        ("Z:/nowhere/x", "C:/nowhere/y"),
        ("", None),
        (None, "C:/nowhere"),
    ],
)
def test_is_within_refuses_an_uncomparable_path_pair(child, parent):
    """Any comparison error refuses. A guard that errors is a guard that says
    no — never one that propagates into a caller documented as total."""
    assert vcs._is_within(child, parent) is False


def test_same_path_matches_gits_casing_rules_on_this_platform(ws):
    assert vcs._same_path(ws, ws) is True
    upper = vcs._same_path(ws, Path(str(ws).upper()))
    assert upper is (os.name == "nt")


# -- rollback (DD-12) --------------------------------------------------------


def test_rollback_leaves_the_repo_and_its_history_intact(repo, config):
    """A4: `clean -ffdqx` removes untracked working-tree paths, including
    nested repos, but never the current repo's own `.git`."""
    write(repo, "out.txt")
    nested_repo = repo / "worker-repo"
    nested_repo.mkdir()
    assert git(nested_repo, "init", "-q").returncode == 0
    (nested_repo / "junk.txt").write_text("junk", encoding="utf-8")

    result = vcs.rollback(repo, vcs.BASE_REF, config, pin=_pin(repo))

    assert result.ok is True, result.stderr
    assert result.reason == ""
    assert (repo / ".git").is_dir()
    assert vcs.is_repo(repo, config) is True
    assert non_git_files(repo) == []


def discarded_refs(ws: Path) -> list[str]:
    out = git(
        ws,
        "for-each-ref",
        "--format=%(refname) %(objectname)",
        vcs.DISCARDED_REF_PREFIX,
    ).stdout
    return [line for line in out.strip().splitlines() if line]


def test_rollback_writes_a_discarded_ref_and_the_round_stays_reachable(repo, config):
    """G-6 / DD-12. `reset --hard` leaves the round commits reachable from no
    ref at all, and `git log --all` walks refs, not the reflog."""
    write(repo, "out.txt", "worker output")
    round_sha = vcs.commit(repo, "round 1", config, pin=_pin(repo)).sha

    result = vcs.rollback(repo, vcs.BASE_REF, config, pin=_pin(repo))

    assert result.ok is True, result.stderr
    refs = discarded_refs(repo)
    assert refs == [f"{vcs.DISCARDED_REF_PREFIX}/{round_sha} {round_sha}"]
    assert "round 1" in git(repo, "log", "--all", "--oneline").stdout
    assert non_git_files(repo) == []


def test_without_the_discarded_ref_the_round_leaves_git_log_all(
    repo, config, monkeypatch
):
    """Control that varies the production code: with step 4 removed, `git show
    <sha>` still resolves the orphan while `git log --all` has lost it — which
    is exactly how the revision-1 defect passed a full review."""
    write(repo, "out.txt", "worker output")
    round_sha = vcs.commit(repo, "round 1", config, pin=_pin(repo)).sha
    monkeypatch.setattr(
        vcs, "_write_discarded_ref", lambda ws, head, cfg: vcs._Run(0, "", "", "")
    )

    assert vcs.rollback(repo, vcs.BASE_REF, config, pin=_pin(repo)).ok is True

    assert discarded_refs(repo) == []
    assert "round 1" not in git(repo, "log", "--all", "--oneline").stdout
    shown = git(repo, "show", f"{round_sha}:out.txt")
    assert shown.returncode == 0
    assert shown.stdout.strip() == "worker output"


def test_a_second_rollback_writes_no_second_discarded_ref(repo, config):
    """The ref is named for the sha it saves, so repeated rollbacks accumulate
    without a read-then-write count that two racing callers could collide on."""
    write(repo, "one.txt")
    sha1 = vcs.commit(repo, "round 1", config, pin=_pin(repo)).sha
    assert vcs.rollback(repo, vcs.BASE_REF, config, pin=_pin(repo)).ok

    write(repo, "two.txt")
    sha2 = vcs.commit(repo, "round 2", config, pin=_pin(repo)).sha
    assert vcs.rollback(repo, vcs.BASE_REF, config, pin=_pin(repo)).ok

    assert sorted(discarded_refs(repo)) == sorted(
        [
            f"{vcs.DISCARDED_REF_PREFIX}/{sha1} {sha1}",
            f"{vcs.DISCARDED_REF_PREFIX}/{sha2} {sha2}",
        ]
    )
    log = git(repo, "log", "--all", "--oneline").stdout
    assert "round 1" in log and "round 2" in log

    # A third rollback with HEAD already at the target discards nothing.
    third = vcs.rollback(repo, vcs.BASE_REF, config, pin=_pin(repo))
    assert third.ok is True
    assert third.files_removed == 0
    assert len(discarded_refs(repo)) == 2

    # Idempotence, which the sha naming buys and a counter did not: writing the
    # ref twice for one tip leaves one ref, not a duplicate of its content.
    vcs._write_discarded_ref(repo, sha2, config)
    assert len(discarded_refs(repo)) == 2


def test_rollback_reports_files_removed_as_a_pre_minus_post_count(repo, config):
    """F11: `clean -q` prints nothing and `reset --hard` has already removed the
    tracked half, so nothing git prints could produce this number."""
    write(repo, "a.txt")
    write(repo, "b.txt")
    vcs.commit(repo, "round 1", config, pin=_pin(repo))
    write(repo, "untracked.txt")

    result = vcs.rollback(repo, vcs.BASE_REF, config, pin=_pin(repo))

    assert result.ok is True, result.stderr
    assert result.files_removed == 3

    again = vcs.rollback(repo, vcs.BASE_REF, config, pin=_pin(repo))
    assert again.files_removed == 0


def test_rollback_recovers_a_removed_file_from_history(repo, config):
    write(repo, "f.txt", "recoverable")
    sha = vcs.commit(repo, "round 1", config, pin=_pin(repo)).sha

    assert vcs.rollback(repo, vcs.BASE_REF, config, pin=_pin(repo)).ok is True

    assert (repo / "f.txt").exists() is False
    shown = git(repo, "show", f"{sha}:f.txt")
    assert shown.returncode == 0
    assert shown.stdout.strip() == "recoverable"


def test_rollback_with_an_unresolvable_ref_removes_nothing(repo, config):
    """The sequence short-circuits on the first non-zero: `clean` never runs,
    which is what the untouched working tree proves."""
    write(repo, "out.txt")
    vcs.commit(repo, "round 1", config, pin=_pin(repo))
    write(repo, "untracked.txt")

    for ref in ("", "refs/agentloop/nope"):
        result = vcs.rollback(repo, ref, config, pin=_pin(repo))
        assert result.ok is False
        assert result.reason == "git-failed"
        assert result.files_removed == 0
        assert non_git_files(repo) == ["out.txt", "untracked.txt"]


# -- disabled, degradation, totality -----------------------------------------


def test_disabled_config_spawns_no_subprocess(repo, monkeypatch):
    disabled = make_config(repo.parent, vcs_enabled=False)

    def explode(*args, **kwargs):
        raise AssertionError("spawned a subprocess with vcs_enabled=False")

    monkeypatch.setattr(vcs.subprocess, "run", explode)

    assert vcs.is_repo(repo, disabled) is False
    for result in (
        vcs.init_repo(repo, disabled),
        vcs.commit(repo, "m", disabled, pin=_pin(repo)),
        vcs.mark_approved(repo, disabled, pin=_pin(repo)),
        vcs.rollback(repo, vcs.BASE_REF, disabled, pin=_pin(repo)),
    ):
        assert result.ok is False
        assert result.reason == "disabled"


def test_git_missing_is_reported_not_raised(ws, ws_root):
    config = make_config(ws_root, vcs_command="agentloop-not-a-real-git")

    result = vcs.init_repo(ws, config)

    assert result.ok is False
    assert result.reason == "git-missing"
    assert vcs.is_repo(ws, config) is False


def test_init_on_an_unwritable_directory_degrades(ws, ws_root, tmp_path):
    config = make_config(ws_root, vcs_command=stub_git(tmp_path))

    result = vcs.init_repo(ws, config)

    assert result.ok is False
    assert result.reason == "git-failed"
    assert "stub failure" in result.stderr


def test_vcs_command_is_an_executable_path_not_a_command_line(ws, ws_root):
    """`vcs_command` is an executable path, deliberately unlike `test_command`,
    which is a command line and goes through `split_command`."""
    for command in ("", "git --no-pager"):
        config = make_config(ws_root, vcs_command=command)
        result = vcs.init_repo(ws, config)
        assert result.ok is False
        assert result.reason == "git-missing"
        assert vcs.is_repo(ws, config) is False


def test_zero_timeout_degrades_to_a_timeout_result(ws, ws_root):
    config = make_config(ws_root, vcs_timeout_s=0)

    result = vcs.init_repo(ws, config)

    assert result.ok is False
    assert result.reason == "timeout"


def test_a_nonexistent_workspace_is_refused_not_created(ws_root, config):
    """F13: `subprocess.run` raises FileNotFoundError for a nonexistent cwd
    exactly as it does for a missing executable, so without the pre-check a
    never-created workspace told the operator to install a git they have."""
    missing = ws_root / "task-9"

    init = vcs.init_repo(missing, config)
    assert init.ok is False
    assert init.reason == "no-workspace"
    assert missing.exists() is False

    assert vcs.is_repo(missing, config) is False
    for result in (
        vcs.commit(missing, "m", config, pin=_pin(missing)),
        vcs.mark_approved(missing, config, pin=_pin(missing)),
        vcs.rollback(missing, vcs.BASE_REF, config, pin=_pin(missing)),
    ):
        assert result.ok is False
        # `no-workspace`, not `not-a-workspace-repo`: the two are distinct
        # terms in a closed vocabulary, and collapsing them made the loop tell
        # the operator a repo was broken when none was ever created.
        assert result.reason == "no-workspace"


def test_a_missing_binary_is_still_distinguishable_from_a_missing_workspace(
    ws, ws_root
):
    """Control for the row above: the two refusals are distinct terms in a
    vocabulary documented as closed, not both spelled `git-missing`."""
    config = make_config(ws_root, vcs_command="agentloop-not-a-real-git")

    assert vcs.init_repo(ws, config).reason == "git-missing"


def test_a_workspace_path_that_is_a_file_is_refused(ws_root, config):
    path = ws_root / "task-2"
    path.write_text("not a directory", encoding="utf-8")

    assert vcs.is_repo(path, config) is False
    assert vcs.init_repo(path, config).reason == "no-workspace"
    assert vcs.commit(path, "m", config, pin=_pin(path)).reason == "no-workspace"
    assert (
        vcs.rollback(path, vcs.BASE_REF, config, pin=_pin(path)).reason
        == "no-workspace"
    )
    assert path.read_text(encoding="utf-8") == "not a directory"


FAULTS = {
    "file-not-found": lambda: FileNotFoundError("no git"),
    "timeout": lambda: subprocess.TimeoutExpired(cmd=["git"], timeout=1),
    "os-error": lambda: OSError("disk gone"),
    "runtime": lambda: RuntimeError("boom"),
}


def inject(monkeypatch, make_exc):
    def raiser(*args, **kwargs):
        raise make_exc()

    monkeypatch.setattr(vcs.subprocess, "run", raiser)


@pytest.mark.parametrize("fault", sorted(FAULTS))
@pytest.mark.parametrize(
    "entry",
    ["init_repo", "commit", "mark_approved", "rollback"],
)
def test_every_entry_point_is_total_under_fault_injection(
    entry, fault, repo, config, monkeypatch
):
    """Property 2. KeyboardInterrupt is deliberately excluded: a BaseException
    must propagate."""
    inject(monkeypatch, FAULTS[fault])
    call = {
        "init_repo": lambda: vcs.init_repo(repo, config, pin=_pin(repo)),
        "commit": lambda: vcs.commit(repo, "m", config, pin=_pin(repo)),
        "mark_approved": lambda: vcs.mark_approved(repo, config, pin=_pin(repo)),
        "rollback": lambda: vcs.rollback(repo, vcs.BASE_REF, config, pin=_pin(repo)),
    }[entry]

    result = call()

    assert isinstance(result, vcs.VcsResult)
    assert result.ok is False
    assert len(result.stderr) <= vcs._MAX_STDERR_CHARS
    json.dumps(asdict(result))


@pytest.mark.parametrize("fault", sorted(FAULTS))
def test_is_repo_is_total_under_fault_injection(fault, repo, config, monkeypatch):
    """A separate parametrisation because `is_repo` returns a `bool`, for which
    asdict/json.dumps are not applicable — asserting them would have made the
    row unwritable and left the guard shell outside the totality proof."""
    inject(monkeypatch, FAULTS[fault])

    result = vcs.is_repo(repo, config)

    assert result is False
    assert isinstance(result, bool)


def test_a_huge_stderr_is_bounded_before_it_reaches_a_caller(ws, config, monkeypatch):
    """Telemetry must never fail an attempt: the caller puts this in an event
    payload.

    On a *fresh* workspace, so the failing command is git itself: an existing
    repo whose guard fails is refused before anything is spawned (and, since
    the config pin, without the re-`init` that used to write into whatever
    repository the failing guard was pointing at), and a refusal carries no
    stderr because nothing ran."""

    def loud(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=["git"], returncode=1, stdout="", stderr="e" * 10_000
        )

    monkeypatch.setattr(vcs.subprocess, "run", loud)

    result = vcs.init_repo(ws, config)

    assert result.ok is False
    assert 0 < len(result.stderr) <= vcs._MAX_STDERR_CHARS
    json.dumps(asdict(result))


def test_rollback_reports_the_discarded_tip_sha(repo, config):
    """P5 glue: the `vcs_rollback` audit event must name where the discarded
    round went (DD-12), and the sha it names has to come from the result — the
    loop may not run a git command of its own. `sha` is the *pre-rollback*
    tip, and is `""` when HEAD already resolved to the target and nothing was
    discarded."""
    write(repo, "out.txt", "worker output")
    round_sha = vcs.commit(repo, "round 1", config, pin=_pin(repo)).sha

    first = vcs.rollback(repo, vcs.BASE_REF, config, pin=_pin(repo))
    assert first.sha == round_sha

    second = vcs.rollback(repo, vcs.BASE_REF, config, pin=_pin(repo))
    assert second.ok is True
    assert second.sha == ""


# ===========================================================================
# Remediation cycle 1 - findings from two blind reviews, reproduced by the
# router on this machine before routing. Each test below fails against the
# pre-fix module.
# ===========================================================================


def _link_dir(link: Path, target: Path) -> bool:
    """A directory link at `link` pointing at `target`, or False if this
    platform will not make one without privileges. A junction on Windows
    (`mklink /J` needs no elevation and is not reliably reported as a symlink,
    which is why the guard tests containment rather than `is_symlink()`)."""
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


def _throwaway_repo(path: Path) -> str:
    """A real repository with one commit, built inside `tmp_path`. Never the
    operator's project repo - that is the thing these tests defend."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True, shell=False)
    (path / "precious.txt").write_text("the operators work", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(path), check=True, shell=False)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@localhost",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "-m",
            "base",
        ],
        cwd=str(path),
        check=True,
        shell=False,
    )
    return git(path, "rev-parse", "HEAD").stdout.strip()


def test_a_linked_git_dir_pointing_outside_the_workspace_is_refused(
    tmp_path, ws_root, config
):
    """CRITICAL 1. The first two conditions prove "this workspace is its own
    repo root" and say nothing about *where that root lives*: git derives the
    worktree from the directory containing `.git`, so a junction at
    `<ws>/.git` pointing at the operator's repo reports the workspace as the
    toplevel and passes both. Measured pre-fix: the guard passed, `commit`
    returned ok=True, and the victim's branch pointer was rewritten off its
    own work.

    Containment-by-resolved-location, not an `is_symlink()` test: junction
    reparse points are not reliably reported as symlinks, and resolved
    location is the property actually wanted."""
    victim = tmp_path / "victim"
    head_before = _throwaway_repo(victim)

    ws = ws_root / "task-1"
    ws.mkdir()
    if not _link_dir(ws / ".git", victim / ".git"):
        pytest.skip("this platform will not create a directory link unprivileged")

    # The first two conditions really are satisfied - without this the test
    # could pass for the wrong reason (e.g. a link git refuses to follow).
    assert (ws / ".git").is_dir()
    toplevel = git(ws, "rev-parse", "--show-toplevel")
    assert toplevel.returncode == 0
    assert vcs._same_path(toplevel.stdout.strip(), ws) is True

    assert vcs.is_repo(ws, config) is False

    write(ws, "agent_payload.txt", "arbitrary AI-generated code wrote this")
    result = vcs.commit(ws, "payload", config, pin=_pin(ws))
    assert result.ok is False
    assert result.reason == "not-a-workspace-repo"

    assert vcs.rollback(ws, vcs.BASE_REF, config, pin=_pin(ws)).ok is False
    assert vcs.mark_approved(ws, config, pin=_pin(ws)).ok is False
    # `init_repo` too: a `.git` that already exists is never re-`init`ed, so a
    # refusal here writes nothing into the repository the link points at.
    assert vcs.init_repo(ws, config, pin=_pin(ws)).ok is False

    assert git(victim, "rev-parse", "HEAD").stdout.strip() == head_before
    assert (victim / "precious.txt").read_text(encoding="utf-8") == "the operators work"


def test_the_round_commit_captures_ignored_files(repo, config):
    """CRITICAL 2. `git add -A` honours a worker-written `.gitignore` while
    `clean -ffdqx` deletes ignored files, so the discarded ref named by the
    audit log was missing exactly the files the rollback destroyed."""
    write(repo, ".gitignore", "build/\nsecrets.env\n")
    (repo / "build").mkdir()
    write(repo, "build/artifact.bin", "artifact")
    write(repo, "secrets.env", "KEY=1")
    write(repo, "main.py", "print()")

    committed = vcs.commit(repo, "round 1", config, pin=_pin(repo))
    assert committed.ok is True
    carried = set(
        git(repo, "ls-tree", "-r", "--name-only", committed.sha).stdout.split()
    )
    assert {"build/artifact.bin", "secrets.env", "main.py", ".gitignore"} <= carried

    rolled = vcs.rollback(repo, vcs.BASE_REF, config, pin=_pin(repo))
    assert rolled.ok is True
    assert non_git_files(repo) == []

    ref = f"{vcs.DISCARDED_REF_PREFIX}/{rolled.sha}"
    recoverable = set(git(repo, "ls-tree", "-r", "--name-only", ref).stdout.split())
    assert {"build/artifact.bin", "secrets.env"} <= recoverable
    assert git(repo, "show", f"{ref}:secrets.env").stdout.strip() == "KEY=1"


def test_a_plain_add_is_the_control_for_the_forced_one(repo, config):
    """Control for the row above: the ignored file is absent from a commit
    staged the pre-fix way, so `-f` is demonstrably what changed and the
    assertion is not passing for an unrelated reason."""
    write(repo, ".gitignore", "secrets.env\n")
    write(repo, "secrets.env", "KEY=1")
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True, shell=False)
    staged = git(repo, "diff", "--cached", "--name-only").stdout.split()

    assert "secrets.env" not in staged
    assert ".gitignore" in staged


def test_a_nested_repo_is_named_as_a_gap_rather_than_silently_lost(repo, config):
    """CRITICAL 2, residual half. `add -A -f` records a worker-created
    `sub/.git` as a bare gitlink, so its objects are still deleted
    unrecoverably - git cannot nest repos this way. The fix is not to recover
    them but to stop the audit trail asserting a recovery surface that does
    not hold them."""
    sub = repo / "sub"
    _throwaway_repo(sub)
    write(repo, "main.py", "print()")

    assert vcs.commit(repo, "round 1", config, pin=_pin(repo)).ok is True
    rolled = vcs.rollback(repo, vcs.BASE_REF, config, pin=_pin(repo))

    assert rolled.ok is True
    assert rolled.nested_repos == ("sub",)


def test_a_workspace_without_a_nested_repo_names_no_gap(repo, config):
    """Control: the gap field is empty for the ordinary case, so the row above
    is not passing against a field that is always populated."""
    write(repo, "main.py", "print()")
    assert vcs.commit(repo, "round 1", config, pin=_pin(repo)).ok is True

    assert vcs.rollback(repo, vcs.BASE_REF, config, pin=_pin(repo)).nested_repos == ()


def test_init_completes_a_repo_that_has_no_base_ref(ws, config):
    """HIGH 4. A repo whose base commit failed once - a full disk, an
    `index.lock`, a timeout - passes the guard, so the fast path reported
    `ok=True, reason="already"` (never logged, by design) while every later
    rollback failed with `git-failed`, permanently and in silence."""
    subprocess.run(["git", "init", "-q"], cwd=str(ws), check=True, shell=False)
    assert git(ws, "rev-parse", "--verify", "--quiet", vcs.BASE_REF).returncode != 0

    result = vcs.init_repo(ws, config, pin=_pin(ws))

    assert result.ok is True
    assert result.reason == ""
    assert git(ws, "rev-parse", "--verify", "--quiet", vcs.BASE_REF).returncode == 0

    write(ws, "out.txt", "the round's work")
    assert vcs.commit(ws, "round 1", config, pin=_pin(ws)).ok is True
    rolled = vcs.rollback(ws, vcs.BASE_REF, config, pin=_pin(ws))
    assert rolled.ok is True
    assert rolled.reason == ""
    assert non_git_files(ws) == []


def test_init_still_short_circuits_a_fully_initialised_repo(repo, config):
    """Control for the row above: the fast path is not removed, it is
    predicated on the invariant it claims."""
    before = git(repo, "log", "--oneline").stdout

    result = vcs.init_repo(repo, config, pin=_pin(repo))

    assert result.ok is True
    assert result.reason == "already"
    assert len(result.sha) == 40
    assert git(repo, "log", "--oneline").stdout == before


def test_a_missing_workspace_is_never_reported_as_a_broken_repo(ws_root, config):
    """MEDIUM c. `_guard` checked `.git` first, so a nonexistent workspace
    never reached the distinction `_run` was written to preserve: every entry
    point said `not-a-workspace-repo`, and the loop's warning then told the
    operator the task "ran without durability" with an empty stderr."""
    missing = ws_root / "task-9"

    for result in (
        vcs.init_repo(missing, config),
        vcs.commit(missing, "m", config, pin=_pin(missing)),
        vcs.mark_approved(missing, config, pin=_pin(missing)),
        vcs.rollback(missing, vcs.BASE_REF, config, pin=_pin(missing)),
    ):
        assert result.ok is False
        assert result.reason == "no-workspace"
    assert missing.exists() is False


# ===========================================================================
# Remediation cycle 2 - a fresh review reproduced each of these by execution
# against the cycle-1 module. Each test below fails against that code.
# ===========================================================================


def test_a_commondir_pointing_outside_the_workspace_is_refused(
    tmp_path, ws_root, config
):
    """CRITICAL RR-1. `.git` lives inside the agent-writable workspace, so
    every file in it is attacker-controlled - and git resolves its ref and
    object store from `.git/commondir`, not from where `.git` sits. One
    ordinary file write therefore redirected every write into the operator's
    repository while all four path conditions held: measured pre-fix with
    `commit` returning ok=True and the victim's HEAD rewritten.

    A fifth path check is not the fix. Testing filesystem paths guesses at
    what git will do; the guard asks git what it actually resolved."""
    victim = tmp_path / "victim"
    head_before = _throwaway_repo(victim)

    ws = ws_root / "task-1"
    ws.mkdir()
    assert vcs.init_repo(ws, config).ok is True

    (ws / ".git" / "commondir").write_text(
        str(victim / ".git") + "\n", encoding="utf-8"
    )

    # The pre-fix conditions really do still hold - without this the test
    # could pass for the wrong reason (e.g. git rejecting the commondir).
    assert (ws / ".git").is_dir()
    toplevel = git(ws, "rev-parse", "--show-toplevel")
    assert toplevel.returncode == 0
    assert vcs._same_path(toplevel.stdout.strip(), ws) is True
    common = git(ws, "rev-parse", "--git-common-dir").stdout.strip()
    assert vcs._same_path(Path(ws) / common, victim / ".git") is True

    assert vcs.is_repo(ws, config) is False

    write(ws, "agent_payload.txt", "arbitrary AI-generated code wrote this")
    result = vcs.commit(ws, "payload", config, pin=_pin(ws))
    assert result.ok is False
    assert result.reason == "not-a-workspace-repo"
    assert vcs.rollback(ws, vcs.BASE_REF, config, pin=_pin(ws)).ok is False
    assert vcs.mark_approved(ws, config, pin=_pin(ws)).ok is False

    assert git(victim, "rev-parse", "HEAD").stdout.strip() == head_before
    assert (victim / "precious.txt").read_text(encoding="utf-8") == "the operators work"


def test_an_ordinary_repo_still_passes_the_resolved_location_guard(repo, config):
    """Control for the row above: the new conditions are satisfied by a
    normal workspace repo, so the refusal there is the commondir and not the
    guard refusing everything."""
    assert vcs.is_repo(repo, config) is True
    write(repo, "main.py", "print()")
    assert vcs.commit(repo, "round 1", config, pin=_pin(repo)).ok is True


def test_uncommitted_work_is_captured_before_the_clean_deletes_it(repo, config):
    """CRITICAL RR-2. The capture used to be gated on `HEAD != target`, so a
    rollback with no round commit wrote no discarded ref and then let
    `clean -ffdqx` delete the whole working tree: measured `files_removed=1`
    with `sha=""` and `reason=""` - a destruction the audit called a clean
    rollback. The invariant is unconditional: everything the clean deletes is
    reachable from a ref."""
    write(repo, "out.txt", "the worker's uncommitted work")
    assert git(repo, "log", "--oneline").stdout.strip().count("\n") == 0

    rolled = vcs.rollback(repo, vcs.BASE_REF, config, pin=_pin(repo))

    assert rolled.ok is True
    assert rolled.files_removed == 1
    assert rolled.sha != ""
    assert non_git_files(repo) == []
    ref = f"{vcs.DISCARDED_REF_PREFIX}/{rolled.sha}"
    assert git(repo, "rev-parse", "--verify", "--quiet", ref).returncode == 0
    assert (
        git(repo, "show", f"{ref}:out.txt").stdout.strip()
        == "the worker's uncommitted work"
    )
    assert rolled.sha in git(repo, "log", "--all", "--oneline", "--format=%H").stdout


def test_a_clean_tree_at_the_target_still_records_nothing(repo, config):
    """Control: the unconditional capture is conditioned on a *dirty* tree,
    not removed - a second rollback over a clean tree still writes no ref."""
    rolled = vcs.rollback(repo, vcs.BASE_REF, config, pin=_pin(repo))

    assert rolled.ok is True
    assert rolled.sha == ""
    assert rolled.files_removed == 0
    assert git(repo, "for-each-ref", vcs.DISCARDED_REF_PREFIX).stdout.strip() == ""


# -- the config pin: `.git/config` is inside the agent-writable workspace -----
#
# The two containment escapes closed before this one both redirected *where*
# git points. This axis is *what git runs*: `filter.<name>.clean` and
# `core.fsmonitor` name programs git executes on an ordinary `add`/`commit`,
# and `.git/config` is an ordinary file inside the workspace the worker writes
# to. Measured before the fix: guard True before and after the edit,
# `commit` ok=True, canary written outside the workspace.
#
# A denylist cannot close it - filter names are arbitrary, so there is no key
# to pin. The pin is an allowlist of exactly one value: the config git wrote at
# init, fingerprinted into the store, replayed on every later call.


def _canary_payload(ws: Path, canary: Path, *, passthrough: bool) -> str:
    """A program a worker can write **inside its own workspace**, as a git
    command line. `passthrough` because a clean filter must return the file
    content on stdout, while an fsmonitor hook is handed no stdin to read."""
    script = ws / "pwn.py"
    body = f"import pathlib\npathlib.Path({canary.as_posix()!r}).write_text('pwned')\n"
    if passthrough:
        body += "import sys\nsys.stdout.write(sys.stdin.read())\n"
    script.write_text(body, encoding="utf-8")
    return f'"{Path(sys.executable).as_posix()}" "{script.as_posix()}"'


def _attack(ws: Path, canary: Path, vector: str) -> None:
    """A worker writing only inside its own workspace, through `git config
    --local` - which guarantees a *well-formed* config file. A hand-appended
    one can be malformed (`fatal: bad config line`), and git refusing to parse
    it is not this guard refusing: a refusal that proves nothing is exactly how
    a defect of this shape survives a review."""
    if vector == "filter":
        cmd = _canary_payload(ws, canary, passthrough=True)
        assert git(ws, "config", "--local", "filter.pwn.clean", cmd).returncode == 0
        (ws / ".gitattributes").write_text("* filter=pwn\n", encoding="utf-8")
    else:
        cmd = _canary_payload(ws, canary, passthrough=False)
        assert git(ws, "config", "--local", "core.fsmonitor", cmd).returncode == 0
    write(ws, "work.txt", "payload")


@pytest.mark.parametrize("vector", ["filter", "fsmonitor"])
def test_a_worker_rewritten_git_config_cannot_run_a_command(
    ws, config, tmp_path, vector
):
    """The regression proof, asserted on the **canary** and not merely on the
    refusal."""
    canary = tmp_path / f"canary-{vector}.txt"
    init = vcs.init_repo(ws, config)
    assert init.ok is True
    assert init.pin != ""
    assert vcs.is_repo(ws, config) is True

    _attack(ws, canary, vector)

    # The containment guard still sees nothing wrong - it is a different axis,
    # which is why this needed a defence of its own.
    assert vcs.is_repo(ws, config) is True
    result = vcs.commit(ws, "round 1", config, pin=init.pin)

    assert canary.exists() is False, "a worker's git config executed a command"
    assert result.ok is False
    assert result.reason == "config-changed"


@pytest.mark.parametrize("vector", ["filter", "fsmonitor"])
def test_the_same_fixture_without_the_config_edit_still_commits(
    ws, config, tmp_path, vector
):
    """Control. Without it the tests above prove only that the pin refuses
    everything - including every legitimate round commit."""
    canary = tmp_path / f"canary-{vector}.txt"
    init = vcs.init_repo(ws, config)
    assert init.ok is True
    # Everything the attack writes *except* the config edit itself.
    _canary_payload(ws, canary, passthrough=vector == "filter")
    if vector == "filter":
        (ws / ".gitattributes").write_text("* filter=pwn\n", encoding="utf-8")
    write(ws, "work.txt", "payload")

    result = vcs.commit(ws, "round 1", config, pin=init.pin)

    assert result.ok is True, result
    assert result.reason == ""
    assert result.sha != ""
    assert canary.exists() is False


@pytest.mark.parametrize("entry", ["commit", "mark_approved", "rollback", "init_repo"])
def test_every_entry_point_refuses_a_config_it_did_not_pin(ws, config, entry):
    """`commit` is where the measured escape ran, but `rollback` is the one
    that then runs `reset --hard` and `clean -ffdqx`, and `init_repo` is what
    would otherwise re-bless the edit on the next round. All four refuse."""
    init = vcs.init_repo(ws, config)
    assert init.ok is True
    write(ws, "work.txt", "payload")
    assert git(ws, "config", "--local", "filter.pwn.clean", "true").returncode == 0

    calls = {
        "commit": lambda: vcs.commit(ws, "round 1", config, pin=init.pin),
        "mark_approved": lambda: vcs.mark_approved(ws, config, pin=init.pin),
        "rollback": lambda: vcs.rollback(ws, vcs.BASE_REF, config, pin=init.pin),
        "init_repo": lambda: vcs.init_repo(ws, config, pin=init.pin),
    }
    result = calls[entry]()

    assert result.ok is False
    assert result.reason == "config-changed"
    # Nothing ran: the file the rollback would have removed is still there.
    assert non_git_files(ws) == ["work.txt"]


@pytest.mark.parametrize("entry", ["commit", "mark_approved", "rollback", "init_repo"])
def test_every_entry_point_refuses_when_no_pin_was_recorded(ws, config, entry):
    """Fail closed on the *absence* of a pin, not only on a mismatch. A caller
    that never recorded one cannot be told the config is unchanged, and "we
    cannot tell" has to reach the same refusal as "it changed" - otherwise the
    gate opens for exactly the caller that skipped it."""
    assert vcs.init_repo(ws, config).ok is True
    write(ws, "work.txt", "payload")

    calls = {
        "commit": lambda: vcs.commit(ws, "round 1", config),
        "mark_approved": lambda: vcs.mark_approved(ws, config),
        "rollback": lambda: vcs.rollback(ws, vcs.BASE_REF, config),
        "init_repo": lambda: vcs.init_repo(ws, config),
    }
    result = calls[entry]()

    assert result.ok is False
    assert result.reason == "config-unpinned"
    assert non_git_files(ws) == ["work.txt"]


def test_an_unreadable_config_is_a_refusal_and_not_a_match(ws, config):
    """`config_pin` reports `""` when it cannot read the file, and `""` is the
    value an unpinned caller passes: compared naively the two would be *equal*
    and a deleted `.git/config` would open the gate for every caller who never
    pinned anything."""
    init = vcs.init_repo(ws, config)
    assert init.ok is True
    (ws / ".git" / "config").unlink()

    assert vcs.config_pin(ws) == ""
    assert vcs.commit(ws, "round 1", config, pin=init.pin).reason == "config-changed"
    assert vcs.commit(ws, "round 1", config, pin="").reason == "config-unpinned"


def test_a_missing_workspace_still_reads_as_a_missing_workspace(ws_root, config):
    """The pin sits *between* the two containment terms, so it cannot swallow
    either: a workspace that does not exist has no config to read, and would
    report `config-changed` if the order were wrong."""
    missing = ws_root / "task-9"

    assert vcs.commit(missing, "m", config).reason == "no-workspace"
    assert vcs.rollback(missing, vcs.BASE_REF, config).reason == "no-workspace"
    assert vcs.mark_approved(missing, config).reason == "no-workspace"
    assert vcs.init_repo(missing, config).reason == "no-workspace"


def test_git_does_not_rewrite_the_config_it_wrote(ws, config):
    """The pin is only viable because a legitimate run never moves it. Every
    command this module issues, in the order a task issues them, against one
    pin captured at init - measured here rather than asserted in a docstring,
    because the day git starts writing to `.git/config` on commit this test is
    how we find out instead of the operator."""
    init = vcs.init_repo(ws, config)
    pin = init.pin
    assert pin == vcs.config_pin(ws)

    write(ws, "out.txt", "worker output")
    assert vcs.commit(ws, "round 1", config, pin=pin).ok is True
    assert vcs.mark_approved(ws, config, pin=pin).ok is True
    assert vcs.init_repo(ws, config, pin=pin).reason == "already"
    assert vcs.rollback(ws, vcs.BASE_REF, config, pin=pin).ok is True
    assert vcs.is_repo(ws, config) is True

    assert vcs.config_pin(ws) == pin


def test_a_recreated_workspace_mints_a_fresh_pin(ws, config):
    """`human_redo` wipes the workspace, so the next `init_repo` creates a new
    repository with a new config. It mints, and the caller must record the new
    value - the old pin is not a shared secret, it is a fingerprint of a file
    that no longer exists."""
    first = vcs.init_repo(ws, config)
    assert first.pin != ""

    # The production wipe (`human_redo`'s fallback), not `shutil.rmtree`:
    # git's read-only objects are what D-F's error handler exists for.
    clear_workspace(str(ws.parent), 1)
    ws.mkdir()
    second = vcs.init_repo(ws, config)

    assert second.ok is True
    assert second.pin != ""
    assert vcs.commit(ws, "round 1", config, pin=second.pin).ok is True
    # Stale pins are refused like any other mismatch. (The two configs differ
    # only if git wrote something path-dependent, so this asserts the *shape*:
    # whatever the fresh pin is, it is what the fresh repo accepts.)
    assert vcs.config_pin(ws) == second.pin


def test_an_existing_repo_is_never_re_pinned(ws, config):
    """The mint is the whole gate, so it happens on exactly one branch. If
    `init_repo` re-pinned a repository it found, a worker's edit would be
    blessed by the next round's init and the pin would authorise the attack it
    exists to stop."""
    init = vcs.init_repo(ws, config)
    assert git(ws, "config", "--local", "core.fsmonitor", "true").returncode == 0

    again = vcs.init_repo(ws, config, pin=init.pin)

    assert again.ok is False
    assert again.reason == "config-changed"
    assert again.pin == ""


def test_an_oversized_config_is_refused_rather_than_hashed(ws, config):
    """Bounded work in the hot path, and the bound fails closed: a config too
    large to fingerprint is treated as one that cannot be read."""
    init = vcs.init_repo(ws, config)
    (ws / ".git" / "config").write_text("#" * (vcs._MAX_CONFIG_BYTES + 1))

    assert vcs.config_pin(ws) == ""
    assert vcs.commit(ws, "round 1", config, pin=init.pin).reason == "config-changed"


@pytest.mark.parametrize("bad", [None, "", 12, b"x", Path("no-such-dir")])
def test_config_pin_is_total(bad):
    """It runs in front of every side-effecting command, so it may not raise -
    on a `None`, on a bytes path, on anything. It returns a `str` either way,
    and a wrong one is a mismatch, which is a refusal: `""` is included
    deliberately, because a relative path resolves against the *cwd* and would
    otherwise be the one input that raises."""
    assert isinstance(vcs.config_pin(bad), str)


# ===========================================================================
# Remediation cycle 4 - a defect in the cycle-3 fix. The commondir guard was
# written against a commondir pointing *outside* `workspace_root`, and the
# config pin was written against `<ws>/.git/config`. Their union left a gap
# neither owned: git reads `config` from the **common dir**, so a commondir
# pointing at a sibling directory *inside the workspace* satisfies every
# containment condition while owning the config git actually parses - with
# `<ws>/.git/config` left byte-identical to the pinned value.
# ===========================================================================


def test_a_commondir_pointing_inside_the_workspace_is_refused(ws, config, tmp_path):
    """CRITICAL. Containment in `workspace_root` is not location: the worker
    copies its own `.git` aside *within its own workspace*, points
    `.git/commondir` at the copy, and owns the config git parses while the
    pinned file never changes. Measured pre-fix: pin unchanged, guard True,
    `commit` ok=True, canary written outside the workspace.

    Asserted on the **canary**, not on the refusal."""
    canary = tmp_path / "canary-commondir.txt"
    init = vcs.init_repo(ws, config)
    assert init.ok is True and init.pin != ""

    shutil.copytree(ws / ".git", ws / "altgit")
    cmd = _canary_payload(ws, canary, passthrough=True)
    assert (
        subprocess.run(
            [
                "git",
                "config",
                "--file",
                str(ws / "altgit" / "config"),
                "filter.pwn.clean",
                cmd,
            ],
            capture_output=True,
            text=True,
            shell=False,
        ).returncode
        == 0
    )
    (ws / ".gitattributes").write_text("* filter=pwn\n", encoding="utf-8")
    (ws / ".git" / "commondir").write_text("../altgit\n", encoding="utf-8")
    write(ws, "work.txt", "payload")

    # Non-vacuity: the pin still matches, so nothing but the guard can refuse,
    # and git really did follow the redirect.
    assert vcs.config_pin(ws) == init.pin
    assert vcs._same_path(
        vcs._resolved(ws, git(ws, "rev-parse", "--git-common-dir").stdout),
        ws / "altgit",
    )

    result = vcs.commit(ws, "round 1", config, pin=init.pin)

    assert canary.exists() is False, "a redirected common dir ran a command"
    assert vcs.is_repo(ws, config) is False
    assert result.ok is False
    assert result.reason == "not-a-workspace-repo"


def test_a_commondir_pointing_at_a_sibling_workspace_is_refused(ws_root, config):
    """The more surprising half of the same two lines: `workspace_root` is the
    parent of *every* task workspace, so containment also permitted one task's
    `reset --hard` / `clean -ffdqx` to be aimed at another task's repository."""
    victim = ws_root / "task-2"
    victim.mkdir()
    v_init = vcs.init_repo(victim, config)
    assert v_init.ok is True
    write(victim, "victim_work.txt", "task 2's work")
    assert vcs.commit(victim, "round 1", config, pin=v_init.pin).ok is True
    victim_head = git(victim, "rev-parse", "HEAD").stdout.strip()
    victim_branch = git(victim, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()

    attacker = ws_root / "task-1"
    attacker.mkdir()
    a_init = vcs.init_repo(attacker, config)
    assert a_init.ok is True
    (attacker / ".git" / "commondir").write_text(str(victim / ".git"), encoding="utf-8")
    assert vcs.config_pin(attacker) == a_init.pin

    rolled = vcs.rollback(attacker, vcs.BASE_REF, config, pin=a_init.pin)

    # The canary first: what matters is that task-2's repository is untouched,
    # not that task-1's call reported a refusal.
    assert git(victim, "rev-parse", "HEAD").stdout.strip() == victim_head
    assert git(victim, "rev-parse", victim_branch).stdout.strip() == victim_head
    assert (victim / "victim_work.txt").read_text(encoding="utf-8") == "task 2's work"
    assert rolled.ok is False
    assert rolled.reason == "not-a-workspace-repo"


def test_an_unredirected_workspace_still_commits_and_rolls_back(ws, config):
    """Control for both rows above. Identity with `<ws>/.git` is what a
    legitimate repo has, so the refusals there are the redirect and not the
    guard refusing every workspace."""
    init = vcs.init_repo(ws, config)
    assert init.ok is True
    assert vcs.is_repo(ws, config) is True

    write(ws, "work.txt", "the round's work")
    committed = vcs.commit(ws, "round 1", config, pin=init.pin)
    assert committed.ok is True, committed
    assert committed.reason == ""

    rolled = vcs.rollback(ws, vcs.BASE_REF, config, pin=init.pin)
    assert rolled.ok is True, rolled
    assert non_git_files(ws) == []


def test_config_pin_never_reads_a_non_regular_file(ws, config, monkeypatch):
    """MEDIUM. `stat()` on a FIFO reports `st_size == 0`, so the size bound
    passes and `read_bytes()` blocks - with no timeout, unlike every git spawn
    in this module - and `vcs.commit` is called from `run_task` outside
    `_with_retry`, so the task hangs forever and holds its claim.

    Asserted on **whether the read happened**, which is the property that
    prevents the hang, rather than on the return value: an unreadable path
    already returned `""` by raising, so a value-only assertion is vacuous."""
    assert vcs.init_repo(ws, config).ok is True
    cfg = ws / ".git" / "config"
    cfg.unlink()
    if hasattr(os, "mkfifo"):
        os.mkfifo(cfg)  # the real subject; POSIX only
    else:
        cfg.mkdir()  # any non-regular file exercises the same branch

    reads: list[str] = []
    original = Path.read_bytes

    def spy(self):
        reads.append(str(self))
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", spy)

    assert vcs.config_pin(ws) == ""
    assert reads == [], f"read a non-regular .git/config: {reads}"


# ---------------------------------------------------------------------------
# Slice 8: the shipped default is a RELATIVE workspace_root, and every test in
# this file uses `tmp_path`, which is absolute. That gap hid a defect that made
# the entire durability feature inert on every default install:
#
#   `_run` sets `cwd=<ws>` and `_git` also appends `-C <ws>`, so git changed
#   into the workspace and then tried to resolve the same relative path *again*
#   from inside it. Measured on the shipped default `.agentloop/ws`:
#     init_repo -> ok=False reason='git-failed'
#       stderr: fatal: cannot change to '.agentloop\ws\task-1'
#     commit / rollback -> the same
#   with an absolute root, all three returned ok=True.
#
# The only signal an operator got was one `RuntimeWarning` per task saying the
# task "ran without durability", so reject and redo silently lost the round they
# were built to preserve.
# ---------------------------------------------------------------------------


def test_the_full_lifecycle_works_from_a_relative_workspace_root(tmp_path, monkeypatch):
    """The shipped default is relative, so the shipped default is what this
    exercises: `LoopConfig()` untouched, from a cwd that is not the repo."""
    from agentloop.config import LoopConfig
    from agentloop.executor import workspace_for

    monkeypatch.chdir(tmp_path)
    config = LoopConfig()  # workspace_root defaults to the relative ".agentloop/ws"
    assert not Path(config.workspace_root).is_absolute()

    ws = workspace_for(config.workspace_root, 1, create=True)
    init = vcs.init_repo(ws, config, pin="")
    assert init.ok, f"init failed: {init.reason} {init.stderr}"
    assert init.pin

    (ws / "util.py").write_text("def slugify(s):\n    return s\n", encoding="utf8")
    committed = vcs.commit(ws, "round 1", config, pin=init.pin)
    assert committed.ok, f"commit failed: {committed.reason} {committed.stderr}"
    assert committed.sha

    assert vcs.mark_approved(ws, config, pin=init.pin).ok
    assert vcs.is_repo(ws, config)

    rolled = vcs.rollback(ws, vcs.BASE_REF, config, pin=init.pin)
    assert rolled.ok, f"rollback failed: {rolled.reason} {rolled.stderr}"
    # The round is discarded from the tree but still reachable, which is the
    # whole promise of the feature.
    assert not (ws / "util.py").exists()
    # The discarded tip is kept as a ref, so the round stays reachable from
    # `git log --all` — the property that makes a rollback recoverable at all.
    refs = git(ws, "for-each-ref", "--format=%(refname)").stdout
    assert vcs.DISCARDED_REF_PREFIX in refs


def test_a_relative_and_an_absolute_root_reach_the_same_state(tmp_path, monkeypatch):
    """The differential that makes the test above mean something: before the
    fix these two disagreed completely (relative failed every call, absolute
    succeeded), which is precisely why an all-absolute suite stayed green."""
    from agentloop.config import LoopConfig
    from agentloop.executor import workspace_for

    monkeypatch.chdir(tmp_path)
    outcomes = []
    for root in (".agentloop/ws", str((tmp_path / "abs-ws").resolve())):
        config = LoopConfig(workspace_root=root)
        ws = workspace_for(config.workspace_root, 7, create=True)
        init = vcs.init_repo(ws, config, pin="")
        (ws / "f.txt").write_text("x", encoding="utf8")
        got = vcs.commit(ws, "m", config, pin=init.pin)
        outcomes.append((init.ok, init.reason, got.ok, got.reason))
    assert outcomes[0] == outcomes[1] == (True, "", True, "")


def test_init_refuses_a_gitfile_pointing_at_another_repository(tmp_path, config, ws):
    """`init_repo`'s docstring claimed git init "cannot reach outside the
    workspace". A gitfile is not a directory, so it took the create branch, and
    `git init` reinitialised the repo the file names — measured, rc=0.

    Fails closed by design: nothing is created and the workspace is refused."""
    victim = tmp_path / "victim"
    victim.mkdir()
    git(victim, "init", "-q")
    victim_head_before = (victim / ".git" / "HEAD").read_text(encoding="utf8")

    # A worker writes one line of text inside its own workspace.
    for entry in list(ws.iterdir()):
        shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
    (ws / ".git").write_text(
        f"gitdir: {victim / '.git'}\n".replace("\\", "/"), encoding="utf8"
    )

    result = vcs.init_repo(ws, config, pin="")
    assert result.ok is False
    assert result.reason == "not-a-workspace-repo"
    # The control that makes the refusal mean something: the victim is untouched.
    assert (victim / ".git" / "HEAD").read_text(encoding="utf8") == victim_head_before
