"""Phase 4 of the multi-project dashboard slice: `--project` on every
project-aware command, and the `agentloop project` management surface.
"""

import pytest

from agentloop.cli import main


@pytest.fixture(autouse=True)
def _in_tmp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)


# -- agentloop project add|rename|repoint|list|archive|use -----------------


def test_project_add_list_rename_repoint_use_archive(capsys, tmp_path):
    repo_a = tmp_path / "repo-a"
    repo_a.mkdir()
    repo_b = tmp_path / "repo-b"
    repo_b.mkdir()

    rc = main(["project", "add", "Alpha", "--repo-root", str(repo_a)])
    assert rc == 0
    assert "registered: Alpha" in capsys.readouterr().out

    rc = main(["project", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Default" in out
    assert "Alpha" in out

    rc = main(["project", "rename", "Alpha", "Beta"])
    assert rc == 0
    assert "Alpha -> Beta" in capsys.readouterr().out

    rc = main(
        [
            "project",
            "repoint",
            "Beta",
            "--repo-root",
            str(repo_b),
            "--workspace-mode",
            "worktree",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert str(repo_b) in out and "worktree" in out

    rc = main(["project", "use", "Beta"])
    assert rc == 0
    assert "now the active default: Beta" in capsys.readouterr().out

    rc = main(["project", "list"])
    out = capsys.readouterr().out
    # The default marker ('*') now sits on Beta, not Default.
    beta_line = next(line for line in out.splitlines() if "Beta" in line)
    assert beta_line.startswith("*")

    rc = main(["project", "archive", "Default"])
    assert rc == 0
    assert "archived: Default" in capsys.readouterr().out

    rc = main(["project", "list"])
    assert "Default" not in capsys.readouterr().out
    rc = main(["project", "list", "--archived"])
    assert "Default" in capsys.readouterr().out


def test_project_add_default_flag_sets_active_default(capsys, tmp_path):
    repo = tmp_path / "repo-c"
    repo.mkdir()
    rc = main(["project", "add", "Gamma", "--repo-root", str(repo), "--default"])
    assert rc == 0
    capsys.readouterr()
    rc = main(["project", "list"])
    out = capsys.readouterr().out
    gamma_line = next(line for line in out.splitlines() if "Gamma" in line)
    assert gamma_line.startswith("*")


def test_project_archive_refuses_with_active_tasks(capsys, tmp_path):
    repo = tmp_path / "repo-d"
    repo.mkdir()
    main(["project", "add", "Delta", "--repo-root", str(repo)])
    capsys.readouterr()
    main(
        [
            "add",
            "A task",
            "--goal",
            "do it",
            "--criteria",
            "works",
            "--project",
            "Delta",
        ]
    )
    capsys.readouterr()
    rc = main(["project", "archive", "Delta"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "non-terminal" in err
    assert "Traceback" not in err


# -- --project resolution success/failure -----------------------------------


def test_add_with_unknown_project_is_a_clean_error(capsys):
    rc = main(
        [
            "add",
            "A task",
            "--goal",
            "do it",
            "--criteria",
            "works",
            "--project",
            "NoSuchProject",
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "unknown project 'NoSuchProject'" in err
    assert "Traceback" not in err


def test_add_with_project_by_name_scopes_the_task(capsys, tmp_path):
    repo = tmp_path / "repo-e"
    repo.mkdir()
    main(["project", "add", "Epsilon", "--repo-root", str(repo)])
    capsys.readouterr()
    rc = main(
        [
            "add",
            "Scoped task",
            "--goal",
            "do it",
            "--criteria",
            "works",
            "--project",
            "Epsilon",
        ]
    )
    assert rc == 0
    capsys.readouterr()

    # Visible under status --project Epsilon...
    rc = main(["status", "--project", "Epsilon"])
    assert rc == 0
    assert "Scoped task" in capsys.readouterr().out
    # ...but not under the (Default project's) unfiltered status.
    rc = main(["status"])
    assert "Scoped task" not in capsys.readouterr().out


# -- differential: no --project on a single-project install is unchanged ----


def test_add_and_status_with_no_project_flag_are_unchanged_on_single_project(capsys):
    """The whole point of "resolves to the active default when omitted": a
    single-project install (only "Default" exists) must produce output
    byte-for-byte identical to a pre-slice-10 tree — no visible sign
    project scoping exists at all."""
    rc = main(["add", "Plain task", "--goal", "do it", "--criteria", "works"])
    assert rc == 0
    assert "Task 1 defined: Plain task" in capsys.readouterr().out

    rc = main(["status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[1]" in out and "Plain task" in out
    assert "project" not in out.lower()  # no project noise on a single-project install


# -- agentloop run --project ---------------------------------------------


def test_run_with_project_flag_processes_only_that_project(
    capsys, tmp_path, monkeypatch
):
    import agentloop.cli as cli
    from agentloop.runner import MockRunner

    approve = "VERDICT: approve CONFIDENCE: 0.92 TESTS: pass\nMeets all criteria."
    repo = tmp_path / "repo-f"
    repo.mkdir()
    main(["project", "add", "Zeta", "--repo-root", str(repo)])
    capsys.readouterr()
    main(
        [
            "add",
            "Zeta task",
            "--goal",
            "do it",
            "--criteria",
            "works",
            "--project",
            "Zeta",
        ]
    )
    main(["add", "Default task", "--goal", "do it", "--criteria", "works"])
    capsys.readouterr()

    monkeypatch.setattr(cli, "get_runner", lambda name: MockRunner(["out", approve]))
    rc = main(["run", "--project", "Zeta", "--runner", "mock"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Processed 1 task(s)." in out
    assert "Zeta task" in out
    assert "Default task" not in out


# -- agentloop events --project / no task_id -------------------------------


def test_events_with_project_flag_and_no_task_id(capsys, tmp_path):
    repo = tmp_path / "repo-g"
    repo.mkdir()
    main(["project", "add", "Eta", "--repo-root", str(repo)])
    capsys.readouterr()
    main(
        [
            "add",
            "Eta task",
            "--goal",
            "do it",
            "--criteria",
            "works",
            "--project",
            "Eta",
        ]
    )
    main(["add", "Default task", "--goal", "do it", "--criteria", "works"])
    capsys.readouterr()

    rc = main(["events", "--project", "Eta"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "task_defined" in out
    # Only Eta's task_defined event, not the Default project's.
    assert out.count("task_defined") == 1
    # [RED-FIRST, HIGH regression] task-less global events (this project's
    # own registration) must stay visible under a project filter, matching
    # the plan's own rule that task_id-NULL events (project_*, config_
    # warning, charter_*, memory_*) are never hidden by --project -- an
    # INNER JOIN would silently drop this line.
    assert "project_created" in out


# -- repoint --workspace-mode omitted must NOT reset the mode --------------


def test_repoint_with_no_workspace_mode_flag_preserves_the_current_one(
    capsys, tmp_path
):
    """[RED-FIRST, CRITICAL regression] `agentloop project repoint NAME
    --repo-root PATH` with no `--workspace-mode` must never silently
    downgrade an existing worktree-mode project to scratch — scratch mode
    is a proven no-op for the whole vcs durability layer (round commits,
    recoverable reject/redo), so a silent downgrade here is a silent loss
    of that guarantee."""
    repo1 = tmp_path / "repo-h1"
    repo1.mkdir()
    repo2 = tmp_path / "repo-h2"
    repo2.mkdir()

    main(
        [
            "project",
            "add",
            "Theta",
            "--repo-root",
            str(repo1),
            "--workspace-mode",
            "worktree",
        ]
    )
    capsys.readouterr()

    # Repoint the repo_root ONLY -- no --workspace-mode at all.
    rc = main(["project", "repoint", "Theta", "--repo-root", str(repo2)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "worktree" in out
    assert "scratch" not in out

    rc = main(["project", "list"])
    out = capsys.readouterr().out
    theta_line = next(line for line in out.splitlines() if "Theta" in line)
    assert "(worktree)" in theta_line


def test_repoint_with_explicit_workspace_mode_still_changes_it(capsys, tmp_path):
    repo1 = tmp_path / "repo-i1"
    repo1.mkdir()
    repo2 = tmp_path / "repo-i2"
    repo2.mkdir()

    main(
        [
            "project",
            "add",
            "Iota",
            "--repo-root",
            str(repo1),
            "--workspace-mode",
            "worktree",
        ]
    )
    capsys.readouterr()

    rc = main(
        [
            "project",
            "repoint",
            "Iota",
            "--repo-root",
            str(repo2),
            "--workspace-mode",
            "scratch",
        ]
    )
    assert rc == 0
    assert "(scratch)" in capsys.readouterr().out


# -- status <task_id> for a non-default worktree-mode project's workspace --


def test_status_shows_the_tasks_own_project_workspace_not_the_global_config(
    capsys, tmp_path
):
    """[RED-FIRST] A task belonging to a DIFFERENT project than whatever
    this process's loopconfig.json names must show ITS OWN project's
    worktree_root-derived workspace path -- reproduces the same bug class
    Phase 3 fixed in executor.workspace_for, one layer up in the CLI's own
    display code."""
    repo = tmp_path / "repo-kappa"
    repo.mkdir()
    (repo / "MARKER.txt").write_text("kappa\n")
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "x"],
        cwd=repo,
        check=True,
    )

    main(
        [
            "project",
            "add",
            "Kappa",
            "--repo-root",
            str(repo),
            "--workspace-mode",
            "worktree",
        ]
    )
    capsys.readouterr()
    main(
        [
            "add",
            "Kappa task",
            "--goal",
            "do it",
            "--criteria",
            "works",
            "--project",
            "Kappa",
        ]
    )
    capsys.readouterr()

    rc = main(["status", "1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "workspace:" in out
    ws_line = next(line for line in out.splitlines() if "workspace:" in line)
    # The process's own (default) loopconfig.json is scratch-mode and knows
    # nothing about Kappa's repo -- the printed path must be Kappa's OWN
    # worktree_root_for(repo_root) resolution, which names the repo
    # ("repo-kappa-<hash>") in its path -- never a bare scratch-shaped
    # `<workspace_root>/task-1` path with no repo identity in it at all.
    assert "repo-kappa" in ws_line
    assert ws_line.rstrip().endswith("task-1")


# -- agentloop memory --project (Phase 6) ------------------------------------


def test_memory_list_with_project_flag_scopes_facts(capsys, tmp_path):
    repo = tmp_path / "repo-lambda"
    repo.mkdir()
    main(["project", "add", "Lambda", "--repo-root", str(repo)])
    capsys.readouterr()

    main(["memory", "add", "style", "use tabs", "--project", "Lambda"])
    main(["memory", "add", "style", "use spaces"])  # goes to Default
    capsys.readouterr()

    rc = main(["memory", "list", "--project", "Lambda"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "use tabs" in out
    assert "use spaces" not in out


def test_memory_add_with_unknown_project_is_a_clean_error(capsys):
    rc = main(["memory", "add", "k", "v", "--project", "NoSuchProject"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "unknown project 'NoSuchProject'" in err
    assert "Traceback" not in err


def test_memory_list_with_no_project_flag_shows_every_project(capsys, tmp_path):
    """[RED-FIRST, HIGH regression] `Store.memory_list`'s own docstring names
    this the one place `project_id=None` ("every project") and
    `resolve_project(None)` ("the default project") would collide if a
    caller isn't careful -- an omitted `--project` on `memory list` must
    show every registered project's facts, exactly as it did before this
    slice, never silently narrow to just the default."""
    repo = tmp_path / "repo-mu"
    repo.mkdir()
    main(["project", "add", "Mu", "--repo-root", str(repo)])
    capsys.readouterr()

    main(["memory", "add", "style", "use tabs", "--project", "Mu"])
    main(["memory", "add", "style", "use spaces"])  # goes to Default
    capsys.readouterr()

    rc = main(["memory", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "use tabs" in out
    assert "use spaces" in out


# -- numeric `--project`/positional project ids (hunter HIGH #2) -----------


def test_project_flag_accepts_a_numeric_id_not_only_a_name(capsys, tmp_path):
    """[RED-FIRST, HIGH regression] `Store.resolve_project` has always
    accepted an `int` id directly, but every `--project`/positional project
    argument on this CLI is typed `str` by argparse -- so
    `resolve_project("3")` took the name-lookup branch and raised
    `unknown project '3'` even when project 3 existed, making the id half of
    "Project name or id" (the CLI's own `--project` help text) unreachable.
    Before the fix (`_project_ref`), this failed with exactly that error."""
    repo = tmp_path / "repo-nu"
    repo.mkdir()
    rc = main(["project", "add", "Nu", "--repo-root", str(repo)])
    assert rc == 0
    out = capsys.readouterr().out
    # "Project {pid} registered: {name}"
    pid = int(out.split("Project ", 1)[1].split(" registered", 1)[0])

    # --project as a numeric id (memory list/add, and `add`/`plan`/`status`
    # all resolve through the same _project_ref path).
    rc = main(["memory", "add", "style", "use tabs", "--project", str(pid)])
    assert rc == 0
    err = capsys.readouterr().err
    assert "Traceback" not in err

    rc = main(["memory", "list", "--project", str(pid)])
    assert rc == 0
    assert "use tabs" in capsys.readouterr().out


def test_project_positional_name_or_id_accepts_a_numeric_id(capsys, tmp_path):
    """[RED-FIRST, HIGH regression] `project rename/repoint/archive/use` take
    a positional NAME_OR_ID; before the fix this only ever resolved a name,
    identically to the --project flag case above."""
    repo = tmp_path / "repo-xi"
    repo.mkdir()
    rc = main(["project", "add", "Xi", "--repo-root", str(repo)])
    assert rc == 0
    out = capsys.readouterr().out
    pid = int(out.split("Project ", 1)[1].split(" registered", 1)[0])

    rc = main(["project", "rename", str(pid), "Xi2"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "Traceback" not in err

    rc = main(["project", "use", str(pid)])
    assert rc == 0
    assert "Traceback" not in capsys.readouterr().err
