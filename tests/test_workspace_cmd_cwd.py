"""Slice 9 P3 remediation cycle 3, HIGH 1 — `agentloop workspace rebless`
(and `prune`, sharing the same resolution line) trusted a relative
`repo_root` ('.', the config default) resolved against *this process's*
cwd. Two independent CLI invocations of the loop and of `rebless` can run
from two different cwds, so the same relative `repo_root` resolves to two
different `Store._repo_key` values — silently reblessing a **different**
repository (if the operator happens to be standing inside one) while the
real, blocked repository's baseline is left untouched, printed as an
unqualified success.

Measured live before this fix: an operator standing inside an unrelated git
repository, running `agentloop workspace rebless` with the (relative,
default) `repo_root: "."` from their `loopconfig.json`, got `rc=0` and
"Repository baseline recorded for .: ... (was unpinned)." — while the real
target repository's stale, `config-changed`-blocking pin was never touched.

Fix: `_workspace_cmd` refuses a **relative** `repo_root` unless the operator
names the repository explicitly via `--repo-root` — the whole point of a
human-only, security-relevant re-bless surface is that it must not silently
guess which repository "wherever I happen to be standing" means. Given an
absolute `repo_root` (via `--repo-root` or an absolute value in
`loopconfig.json`), resolution is `os.path.abspath` once at the CLI
boundary and is then cwd-independent for the rest of the call.
"""

from __future__ import annotations

import json
from pathlib import Path

from agentloop.cli import main
from agentloop.store import Store
from agentloop import vcs
from tests.test_worktree_vcs import git_write, make_repo


def _config_file(tmp_path: Path, **overrides) -> str:
    cfg = tmp_path / "loopconfig.json"
    data = {"db_path": str(tmp_path / "agentloop.db")}
    data.update(overrides)
    cfg.write_text(json.dumps(data), encoding="utf-8")
    return str(cfg)


def test_rebless_with_relative_repo_root_and_wrong_cwd_does_not_silently_bless_the_wrong_repo(
    tmp_path, monkeypatch, capsys
):
    """[RED-FIRST] Watched failing against the pre-fix tree: the command
    returned rc=0, printed success, and recorded a baseline for the
    *unrelated* repository the operator happened to be standing in, while
    the real target's stale pin was left exactly as it was."""
    real_repo = make_repo(tmp_path / "operator_repo")
    unrelated_repo = make_repo(tmp_path / "unrelated_repo")

    db_path = tmp_path / "agentloop.db"
    store = Store(str(db_path))
    old_pin = vcs.config_pin(real_repo, real_repo)
    store.set_vcs_repo_pin(str(real_repo), old_pin)
    store.close()

    # A legitimate operator edit to the REAL repo's config — this is the
    # situation `rebless` exists to fix.
    assert (
        git_write(real_repo, "config", "--local", "core.autocrlf", "false").returncode
        == 0
    )
    new_pin = vcs.config_pin(real_repo, real_repo)
    assert new_pin != old_pin

    # loopconfig.json uses the relative default, exactly as a fresh install
    # ships it.
    cfg_path = _config_file(
        tmp_path, workspace_mode="worktree", repo_root=".", vcs_enabled=True
    )

    # The operator runs `agentloop workspace rebless` from a DIFFERENT,
    # unrelated repository — a very plausible mistake (wrong terminal tab).
    monkeypatch.chdir(str(unrelated_repo))
    rc = main(["--config", cfg_path, "workspace", "rebless"])
    out, err = capsys.readouterr()

    # The real repository's stale pin must NOT have been silently left
    # behind while a bogus "success" was reported for the wrong directory.
    store2 = Store(str(db_path))
    real_pin_after = store2.vcs_repo_pin(str(real_repo))
    store2.close()

    if rc == 0:
        # Only acceptable if the command actually reblessed the REAL repo,
        # not some other one.
        assert real_pin_after == new_pin, (
            "false success: command reported rc=0 without correcting the "
            "real repository's stale baseline "
            f"(stdout={out!r} stderr={err!r})"
        )
    else:
        # A loud, honest refusal is acceptable too, but must say something
        # useful and never a bare traceback.
        assert "Traceback" not in err
        assert real_pin_after == old_pin  # untouched either way


def test_rebless_with_explicit_absolute_repo_root_is_cwd_independent(
    tmp_path, monkeypatch
):
    """An operator who names the repository explicitly (`--repo-root`, an
    absolute path) must get the identical result regardless of which
    directory they happened to invoke the command from."""
    real_repo = make_repo(tmp_path / "operator_repo2")
    unrelated_repo = make_repo(tmp_path / "unrelated_repo2")

    db_path = tmp_path / "agentloop.db"
    store = Store(str(db_path))
    old_pin = vcs.config_pin(real_repo, real_repo)
    store.set_vcs_repo_pin(str(real_repo), old_pin)
    store.close()

    assert (
        git_write(real_repo, "config", "--local", "core.autocrlf", "false").returncode
        == 0
    )
    new_pin = vcs.config_pin(real_repo, real_repo)

    cfg_path = _config_file(tmp_path, workspace_mode="worktree", vcs_enabled=True)

    monkeypatch.chdir(str(unrelated_repo))
    rc = main(
        [
            "--config",
            cfg_path,
            "workspace",
            "rebless",
            "--repo-root",
            str(real_repo),
        ]
    )
    assert rc == 0

    store2 = Store(str(db_path))
    assert store2.vcs_repo_pin(str(real_repo)) == new_pin
    store2.close()
