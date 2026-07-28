"""0d: the CLI turns expected user-input errors (bad ids) into a clean stderr
message + exit 1, instead of leaking a raw Python traceback."""

import pytest

from agentloop.cli import main


@pytest.fixture(autouse=True)
def _in_tmp(tmp_path, monkeypatch):
    # Keep the throwaway agentloop.db out of the repo.
    monkeypatch.chdir(tmp_path)


def test_bad_task_id_prints_clean_error_and_exits_1(capsys):
    rc = main(["approve", "9999"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "9999" in err
    assert "Traceback" not in err  # no leaked stack trace


@pytest.mark.parametrize("cmd", ["reject", "redo", "pause", "resume", "abort"])
def test_all_id_commands_handle_a_bad_id(cmd, capsys):
    rc = main([cmd, "9999"])
    assert rc == 1
    assert "Traceback" not in capsys.readouterr().err


def test_bad_memory_id_prints_clean_error(capsys):
    rc = main(["memory", "approve", "9999"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "9999" in err
    assert "Traceback" not in err


def test_valid_command_still_returns_0(capsys):
    rc = main(["add", "A task", "--goal", "do it", "--criteria", "works"])
    assert rc == 0
    assert "defined" in capsys.readouterr().out


# -- planner CLI (slice 3) ----------------------------------------------------


def test_approve_plan_on_a_bad_id_is_a_clean_error(capsys):
    rc = main(["approve-plan", "9999"])
    assert rc == 1
    assert "Traceback" not in capsys.readouterr().err


def test_approve_plan_on_an_ordinary_task_is_a_clean_error(capsys):
    """A wrong-command-for-this-id mistake is user input, not a crash: the
    ValueError must render like a bad id does."""
    main(["add", "A task", "--goal", "do it", "--criteria", "works"])
    capsys.readouterr()
    rc = main(["approve-plan", "1"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "not a plan" in err
    assert "Traceback" not in err


def test_plan_command_prints_the_graph_and_gates_it(capsys, monkeypatch):
    """The mock runner is scripted through the CLI's own runner factory, so this
    exercises the real `agentloop plan` path with no API key."""
    import agentloop.cli as cli
    from agentloop.runner import MockRunner
    from tests.test_planner import PLAN_JSON

    monkeypatch.setattr(cli, "get_runner", lambda name: MockRunner([PLAN_JSON]))
    rc = main(
        ["plan", "Build a slugify library", "--criteria", "tested", "--runner", "mock"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Plan 1 created: 3 task(s)" in out
    assert "Write slugify()" in out
    assert "depends on 2" in out  # the graph's one dependency, by task id
    assert "approve-plan 1" in out  # gated by default

    rc = main(["approve-plan", "1"])
    assert rc == 0
    assert "3 task(s) released" in capsys.readouterr().out


def test_plan_that_cannot_be_parsed_exits_1_with_no_tasks(capsys, monkeypatch):
    import agentloop.cli as cli
    from agentloop.runner import MockRunner

    monkeypatch.setattr(
        cli, "get_runner", lambda name: MockRunner(["I'd start with the parser."])
    )
    rc = main(["plan", "Build a thing", "--criteria", "works", "--runner", "mock"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "produced no tasks" in err
    assert "Traceback" not in err
