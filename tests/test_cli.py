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
