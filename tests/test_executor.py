"""Sandboxed test execution (spec §5). Real subprocesses, no mocks — the whole
point of this module is that something actually runs."""

import sys

import pytest

from agentloop.executor import TestExecutor, clear_workspace, workspace_for


@pytest.fixture()
def ws(tmp_path):
    d = tmp_path / "ws"
    d.mkdir()
    return d


def write(ws, name: str, body: str) -> None:
    (ws / name).write_text(body, encoding="utf-8")


def test_passing_tests_report_pass(ws):
    write(ws, "test_ok.py", "def test_ok():\n    assert 1 + 1 == 2\n")
    result = TestExecutor(command=f"{sys.executable} -m pytest -q").run(ws)

    assert result.status == "pass"
    assert result.passed is True
    assert result.exit_code == 0
    assert result.duration_s >= 0


def test_failing_tests_report_fail_with_output(ws):
    write(ws, "test_bad.py", "def test_bad():\n    assert False, 'boom'\n")
    result = TestExecutor(command=f"{sys.executable} -m pytest -q").run(ws)

    assert result.status == "fail"
    assert result.passed is False
    assert result.exit_code != 0
    assert "boom" in result.stdout_tail


def test_missing_workspace_is_na_not_failure(tmp_path):
    """No workspace means 'nothing to say', not 'the work is broken'."""
    result = TestExecutor().run(tmp_path / "nope")
    assert result.status == "na"
    assert result.passed is None


def test_empty_workspace_is_na(ws):
    result = TestExecutor().run(ws)
    assert result.status == "na"


def test_disabled_executor_is_na(ws):
    write(ws, "test_ok.py", "def test_ok():\n    assert True\n")
    result = TestExecutor(enabled=False).run(ws)
    assert result.status == "na"


def test_none_workspace_is_na():
    assert TestExecutor().run(None).status == "na"


def test_unknown_command_is_error_not_crash(ws):
    write(ws, "test_ok.py", "def test_ok():\n    assert True\n")
    result = TestExecutor(command="definitely-not-a-real-binary-xyz").run(ws)
    assert result.status == "error"
    assert result.passed is False  # an unrunnable suite is not a pass


def test_timeout_is_error_not_hang(ws):
    write(ws, "slow.py", "import time\ntime.sleep(30)\n")
    result = TestExecutor(command=f"{sys.executable} slow.py", timeout_s=1).run(ws)
    assert result.status == "error"
    assert "timed out" in result.summary.lower()
    assert result.duration_s < 20  # actually killed, not waited out


def test_command_is_not_shell_interpreted(ws):
    """A shell metacharacter must be argv, never an operator. If this regressed
    to shell=True, the `&&` would run a second command."""
    canary = ws / "pwned.txt"
    write(ws, "test_ok.py", "def test_ok():\n    assert True\n")
    executor = TestExecutor(
        command=f'{sys.executable} -c "pass" && {sys.executable} '
        f"-c \"open(r'{canary}','w').write('x')\""
    )
    result = executor.run(ws)

    assert not canary.exists(), "command was shell-interpreted"
    assert result.status in ("pass", "fail", "error")


def test_stdout_tail_is_capped(ws):
    write(ws, "loud.py", "print('x' * 100000)\n")
    result = TestExecutor(command=f"{sys.executable} loud.py").run(ws)
    assert len(result.stdout_tail) <= 4000


def test_workspace_helpers_create_and_clear(tmp_path):
    ws = workspace_for(tmp_path, 7, create=True)
    assert ws.is_dir() and ws.name == "task-7"

    (ws / "leftover.txt").write_text("stale", encoding="utf-8")
    clear_workspace(tmp_path, 7)
    assert not ws.exists()

    # Clearing a workspace that was never created must not raise.
    clear_workspace(tmp_path, 999)


# -- 0a: the executed code is sandboxed, not just the command ----------------


def test_child_env_scrubs_secrets_and_keeps_essentials(monkeypatch):
    """The parent env carries ANTHROPIC_API_KEY and every other secret; the
    child running arbitrary generated code must not inherit them."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-never-leak")
    monkeypatch.setenv("AGENTLOOP_SENTINEL_SECRET", "leak-me")
    env = TestExecutor()._child_env()

    upper = {k.upper() for k in env}
    assert "ANTHROPIC_API_KEY" not in upper
    assert "AGENTLOOP_SENTINEL_SECRET" not in upper
    assert env.get("PYTHONUNBUFFERED") == "1"
    assert "PATH" in upper  # or the test runner can't even be found


def test_sentinel_secret_absent_from_the_executed_subprocess_env(ws, monkeypatch):
    """Acceptance (0a): a secret in the parent env is absent from the child env
    the executor actually hands the subprocess."""
    monkeypatch.setenv("AGENTLOOP_SENTINEL_SECRET", "leak-me")
    write(
        ws,
        "leaky.py",
        "import os\n"
        "print('SENTINEL=' + os.environ.get("
        "'AGENTLOOP_SENTINEL_SECRET', 'ABSENT'))\n",
    )
    result = TestExecutor(command=f"{sys.executable} leaky.py").run(ws)

    assert "SENTINEL=ABSENT" in result.stdout_tail
    assert "leak-me" not in result.stdout_tail


def test_env_allowlist_can_pass_through_named_vars(monkeypatch):
    """A project that genuinely needs a build flag can allowlist it by name."""
    monkeypatch.setenv("MY_BUILD_FLAG", "on")
    assert TestExecutor()._child_env().get("MY_BUILD_FLAG") is None
    assert (
        TestExecutor(env_allowlist=["MY_BUILD_FLAG"])._child_env().get("MY_BUILD_FLAG")
        == "on"
    )


def test_strict_isolation_degrades_to_env_with_a_warning():
    """No container backend ships yet, so 'strict' degrades to env-scrub and
    says so — the residual risk must be surfaced, not silent."""
    import warnings

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        ex = TestExecutor(isolation="strict")
    assert ex.requested_isolation == "strict"
    assert ex.effective_isolation == "env"
    assert any("degrad" in str(w.message).lower() for w in caught)


def test_env_isolation_is_the_default_and_does_not_warn():
    import warnings

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        ex = TestExecutor()
    assert ex.effective_isolation == "env"
    assert not caught
