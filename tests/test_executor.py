"""Sandboxed test execution (spec §5). Real subprocesses, no mocks — the whole
point of this module is that something actually runs."""

import os
import shutil
import sys
from pathlib import Path

import pytest

from agentloop import vcs
from agentloop.config import LoopConfig
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


# -- slice 6 (D-3 / DD-14): a workspace that contains a git repo -------------


def repo_config(root: Path) -> LoopConfig:
    """Config for a fixture that builds a real repo under `root`.

    `workspace_root` must be the workspace's parent: DD-16's third guard
    condition refuses a workspace outside it, and a refused guard makes no base
    commit, so no read-only git objects exist and the control below would remove
    the directory cleanly while proving nothing.
    """
    return LoopConfig(workspace_root=str(root), vcs_enabled=True)


def init_repo_or_fail(ws: Path, config: LoopConfig) -> str:
    """Creates the fixture repo and returns its config pin - what the loop
    records in the store and replays on every later `vcs` call."""
    result = vcs.init_repo(ws, config)
    assert result.ok, f"fixture repo not created: {result.reason} {result.stderr}"
    return result.pin


def test_a_workspace_holding_only_a_git_dir_is_still_na(tmp_path):
    """A freshly initialised workspace is still empty (D-3). Without this the
    tests gate silently moves: the test command would run against nothing."""
    ws = tmp_path / "task-1"
    ws.mkdir()
    init_repo_or_fail(ws, repo_config(tmp_path))

    result = TestExecutor(command=f"{sys.executable} -c pass").run(ws)
    assert result.status == "na"
    assert result.summary == "Workspace is empty — nothing to test."

    # Control: one ordinary file in the same workspace flips it away from na.
    write(ws, "test_ok.py", "def test_ok():\n    assert True\n")
    assert TestExecutor(command=f"{sys.executable} -c pass").run(ws).status != "na"


def test_a_file_under_a_directory_named_dot_git_elsewhere_still_counts(tmp_path):
    """The exclusion is on the path component `.git`, not on a substring."""
    ws = tmp_path / "task-2"
    (ws / "src").mkdir(parents=True)
    (ws / "src" / ".gitignore").write_text("*.pyc\n", encoding="utf-8")

    result = TestExecutor(command=f"{sys.executable} -c pass").run(ws)
    assert result.status == "pass"


def test_clear_workspace_removes_a_workspace_containing_a_git_repo(tmp_path):
    """G-8 / DD-14. The control varies the *production code* (a naive
    `rmtree(ignore_errors=True)`, which is what shipped) against the identical
    fixture, so a pass proves the handler did work rather than the filesystem
    being lenient."""
    config = repo_config(tmp_path)

    subject = workspace_for(tmp_path, 1, create=True)
    pin = init_repo_or_fail(subject, config)
    write(subject, "out.txt", "work")
    assert vcs.commit(subject, "round 1", config, pin=pin).ok

    control = workspace_for(tmp_path, 2, create=True)
    control_pin = init_repo_or_fail(control, config)
    write(control, "out.txt", "work")
    assert vcs.commit(control, "round 1", config, pin=control_pin).ok
    read_only = [
        p for p in control.rglob("*") if p.is_file() and not os.access(p, os.W_OK)
    ]

    shutil.rmtree(control, ignore_errors=True)  # the pre-DD-14 production line
    clear_workspace(tmp_path, 1)

    assert not workspace_for(tmp_path, 1).exists()
    if os.name == "nt":
        # Only Windows enforces the read-only attribute on unlink; elsewhere the
        # naive call succeeds and the control is vacuous, so it is not asserted.
        assert read_only, "fixture built no read-only git objects"
        assert control.exists(), "control removed the repo — fixture proves nothing"


def test_clear_workspace_never_raises(tmp_path, monkeypatch):
    """Its callers (human_redo) have no error handling of their own. It now
    reports the outcome instead of `None` (see the MEDIUM-f row below), so the
    totality assertion is on the report rather than on its absence."""
    assert clear_workspace(tmp_path, 4242) is True  # never existed

    ws = workspace_for(tmp_path, 5, create=True)
    write(ws, "out.txt", "work")

    def boom(*args, **kwargs):
        raise OSError("rmtree refused")

    monkeypatch.setattr(shutil, "rmtree", boom)
    assert clear_workspace(tmp_path, 5) is False


# -- coverage parsing (slice 6, P7) -------------------------------------------


# A realistic passing-pytest tail, used as a negative: an ordinary run reports
# no coverage at all, and must not be made to look like it reported 0%.
PLAIN_PYTEST_TAIL = """============================= test session starts =====\
========================
platform win32 -- Python 3.12.1, pytest-8.1.1, pluggy-1.4.0
collected 3 items

test_ok.py ...                                                           [100%]

============================== 3 passed in 0.04s ==============================
"""


def _adversarial_blob() -> str:
    """100 KB of deterministic junk, seeded so a pass is reproducible."""
    import random

    rng = random.Random(6)
    alphabet = "abcTOTAL0123456789 %:=.-" + " " * 6 + chr(10) + chr(9)
    return "".join(rng.choice(alphabet) for _ in range(100_000))


COVERAGE_POSITIVES = [
    ("TOTAL                    124     16    87%", 87.0),
    ("TOTAL   1   0  100%", 100.0),
    ("Total coverage: 87.5%", 87.5),
    ("total coverage = 0%", 0.0),
]

COVERAGE_NEGATIVES = [
    "87% of tests passed",
    "Coverage: unknown",
    "TOTAL 12 3",
    "TOTALS  ...  87%",
    "-87%",
    "187%",
    "",
    "total coverage: %",
    _adversarial_blob(),
    PLAIN_PYTEST_TAIL,
]


def test_parse_coverage_table():
    """Two-sided: the positives are the control for the negatives. Without
    them, a parser hard-wired to `return None` would pass every negative."""
    from agentloop.executor import parse_coverage

    for text, expected in COVERAGE_POSITIVES:
        assert parse_coverage(text) == expected, text
    for text in COVERAGE_NEGATIVES:
        assert parse_coverage(text) is None, text[:60]


def test_parse_coverage_never_raises():
    """Total by contract: it runs on the path that builds the TestResult the
    tests gate reads, so a raise here would fail an attempt over telemetry."""
    from agentloop.executor import parse_coverage

    corpus = [t for t, _ in COVERAGE_POSITIVES] + COVERAGE_NEGATIVES
    corpus += [
        None,  # type: ignore[list-item]
        123,  # type: ignore[list-item]
        b"TOTAL 1 0 87%",  # type: ignore[list-item]
        "TOTAL " + "x" * 100_000,  # a pathological single line
        "\x00TOTAL\t9\t9\t50%\n",
        "TOTAL 1 0 87%\r\n",
    ]
    for text in corpus:
        result = parse_coverage(text)
        assert result is None or 0.0 <= result <= 100.0


def test_parse_coverage_prefers_nothing_when_two_totals_disagree():
    """F5. A multi-suite run prints more than one TOTAL row; storing the first
    would render one suite's sub-total as *the* coverage. The equal-totals half
    is the control — without it, "give up on any repeat" would pass too."""
    from agentloop.executor import parse_coverage

    two_suites = (
        "---------- coverage: platform win32 ----------\n"
        "Name         Stmts   Miss  Cover\n"
        "TOTAL          124     16    87%\n"
        "\n"
        "---------- coverage: platform win32 ----------\n"
        "Name         Stmts   Miss  Cover\n"
        "TOTAL           50     18    64%\n"
    )
    assert parse_coverage(two_suites) is None
    assert parse_coverage(two_suites.replace("64%", "87%")) == 87.0


def test_parse_coverage_refuses_a_negative_total():
    """A negative total must not be read as its absolute value.

    Found by an independent router probe after this phase reported PASS: the
    lookbehind excluded a preceding digit or dot but not a minus, so
    "TOTAL 1 0 -5%" captured `5` and reported 5.0. That is a *sign error* — a
    wrong number rather than a missing one — and not producing one is this
    function's entire contract. The `>= 0.0` range check could not catch it
    because the sign was already gone by then.

    No coverage tool emits a negative total, so the input is implausible; the
    point is the direction of the failure, not its likelihood. The control
    below is the same line without the minus, which must still parse — so this
    pins the sign specifically and not "TOTAL rows ending in 5% are ambiguous".
    """
    from agentloop.executor import parse_coverage

    assert parse_coverage("TOTAL      1      0    -5%") is None
    assert parse_coverage("TOTAL      1      0     5%") == 5.0


def test_parse_coverage_is_multiline_anchored():
    """F5. This is the case a single-line corpus cannot express: it is what
    distinguishes `re.MULTILINE` present from absent."""
    from agentloop.executor import parse_coverage

    embedded = (
        "============================= test session starts ================\n"
        "collected 3 items\n"
        "\n"
        "---------- coverage: platform win32, python 3.12.1 ----------\n"
        "Name             Stmts   Miss  Cover\n"
        "------------------------------------\n"
        "agentloop/x.py     124     16    87%\n"
        "TOTAL              124     16    87%\n"
        "\n"
        "============================== 3 passed in 0.04s ================\n"
    )
    assert parse_coverage(embedded) == 87.0
    assert parse_coverage("... TOTAL 124 16 87% ...") is None


def test_coverage_reaches_the_test_run_row(tmp_path):
    """Integration seam: executor -> TestResult -> store column, with a real
    subprocess. Control: the same stub without the coverage line stores NULL,
    not 0.0 — `None` means "nothing was reported", never "0% covered"."""
    from agentloop.models import Task
    from agentloop.store import Store

    store = Store(tmp_path / "s.db")
    try:
        task_id = store.add_task(
            Task(id=None, title="T", goal="g", acceptance_criteria="c")
        )

        ws = workspace_for(tmp_path, task_id, create=True)
        write(ws, "cov.py", 'print("TOTAL   124   16   87%")')
        result = TestExecutor(command=f"{sys.executable} cov.py").run(ws)
        assert result.status == "pass", result.summary
        assert result.coverage_percent == 87.0
        store.add_test_run(task_id, None, result)
        assert store.test_runs(task_id)[0]["coverage_percent"] == 87.0

        other = store.add_task(
            Task(id=None, title="T2", goal="g", acceptance_criteria="c")
        )
        ws2 = workspace_for(tmp_path, other, create=True)
        write(ws2, "cov.py", 'print("3 passed in 0.04s")')
        plain = TestExecutor(command=f"{sys.executable} cov.py").run(ws2)
        assert plain.status == "pass", plain.summary
        assert plain.coverage_percent is None
        store.add_test_run(other, None, plain)
        assert store.test_runs(other)[0]["coverage_percent"] is None
    finally:
        store.close()


# ===========================================================================
# Remediation cycle 1 - executor findings from two blind reviews.
# ===========================================================================


def test_parse_coverage_refuses_two_percentages_on_one_line():
    """MEDIUM a. The end anchor silently elected the *last* of two percentages
    on one line: the ambiguity rule worked across matches and could not see
    inside one. `TOTAL 100 10 90% 50%` measured 50.0 - a wrong number, which
    is the one thing this function contracts never to produce."""
    from agentloop.executor import parse_coverage

    assert parse_coverage("TOTAL   100   10   90%   50%") is None
    # Control: the same shape with one percentage is still read.
    assert parse_coverage("TOTAL   100   10   90%") == 90.0


def test_parse_coverage_refuses_prose_that_starts_with_total():
    """MEDIUM a. `^TOTAL\\s+` with IGNORECASE matches an English sentence, and
    the parsed corpus is model-written test output. Two whitespace-separated
    numeric columns before the percentage is what both real coverage.py shapes
    emit and what prose does not."""
    from agentloop.executor import parse_coverage

    assert parse_coverage("TOTAL of 3 tests failed, 20%") is None
    assert parse_coverage("Total of the 4 runs, 55% were slow") is None


def test_parse_coverage_reads_an_indented_total_row():
    """MEDIUM a. Pattern 1 lacked pattern 2's leading `\\s*`, so an indented
    row - what a nested or captured report prints - reported nothing while the
    identical unindented row reported 90."""
    from agentloop.executor import parse_coverage

    assert parse_coverage("   TOTAL 10 1 90%") == 90.0
    assert parse_coverage("\tTOTAL\t10\t1\t90%") == 90.0


def test_parse_coverage_reads_a_row_with_missing_line_numbers():
    """coverage.py under `-m` prints the missing ranges after the percentage,
    so the value is no longer at the end of the line."""
    from agentloop.executor import parse_coverage

    assert parse_coverage("TOTAL      124     16    87%   12-15, 20") == 87.0


def test_clear_workspace_reports_whether_the_workspace_is_gone(tmp_path, monkeypatch):
    """MEDIUM f. With an error handler installed `rmtree` swallows per-file
    failures, so the function returned the same `None` whether it wiped the
    workspace or left it. Slice 6 promotes it to the fallback for every failed
    rollback, where a redo that silently kept the previous round is invisible."""
    from agentloop.executor import clear_workspace, workspace_for

    ws = workspace_for(tmp_path, 11, create=True)
    write(ws, "out.txt", "work")
    assert clear_workspace(tmp_path, 11) is True
    assert not ws.exists()

    # Never created: there is nothing there, which is the outcome asked for.
    assert clear_workspace(tmp_path, 4243) is True

    stubborn = workspace_for(tmp_path, 12, create=True)
    write(stubborn, "out.txt", "work")
    monkeypatch.setattr(shutil, "rmtree", lambda *a, **k: None)
    assert clear_workspace(tmp_path, 12) is False
    assert stubborn.exists()
