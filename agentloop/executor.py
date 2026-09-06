"""Sandboxed test execution (spec §5).

Tests are part of validation, not separate: the worker's output is exercised by
really running the project's test command, and that executed result — not the
validator's opinion of it — decides the tests gate in the loop.

Threat model: this module runs **arbitrary, AI-generated code** — whatever the
worker wrote, invoked by the test command. Command hijack is the *lesser*
concern; the code the command runs is the real exposure. The defenses are
layered accordingly:

- The command comes from LoopConfig, never from model output. It is split with
  `split_command` and run without a shell, so `pytest -q; rm -rf /` is passed
  as literal argv, not interpreted.
- cwd is pinned to the task's own workspace directory.
- A timeout bounds runtime **and is enforced against the whole process tree**,
  and output is read into a bounded ring buffer rather than truncated after the
  fact. Both were promises this docstring made and the code did not keep: see
  `_run_bounded`, which replaced `subprocess.run(capture_output=True,
  timeout=...)` after a child was measured writing 331 MB in 4 s into the
  orchestrator's heap, and a surviving grandchild holding the inherited pipe was
  measured defeating a 3 s timeout for 20.3 s (indefinitely, in the general
  case).
- **The child environment is scrubbed to a minimal allowlist** (`_child_env`).
  The parent env — which holds `ANTHROPIC_API_KEY` and every other secret — is
  never passed wholesale, so generated code cannot read credentials from it.
- `sandbox_isolation='strict'` requests stronger isolation (a no-network,
  read-only-fs / container tier) when a backend is available, and degrades to
  the env-scrub tier with a loud warning when it is not (see
  `_strict_isolation_available`). **Residual risk in the env-scrub tier:**
  generated code still runs with this process's filesystem write access
  (absolute / `..` paths escape the workspace) and network access. Only the
  environment is contained. Run under `strict` (with a real backend) or an
  external sandbox when executing untrusted code.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import warnings
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING

from .models import TestResult

if TYPE_CHECKING:
    from .config import LoopConfig

# Keep stored output small — the tail is for humans debugging a failure, and
# the whole thing is also fed into a validator prompt where tokens cost money.
_MAX_TAIL_CHARS = 4000

# The only environment variables copied into the child. Everything else —
# secrets included — is dropped. This is the allowlist the interpreter and a
# typical test runner need to start and resolve paths on both platforms; a
# project needing more passes them explicitly via LoopConfig.sandbox_env_allowlist.
# Matched case-insensitively because Windows env keys vary in case.
_BASE_ENV_ALLOWLIST: tuple[str, ...] = (
    # POSIX + interpreter basics
    "PATH",
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONUNBUFFERED",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "HOME",
    "TMPDIR",
    "SHELL",
    "USER",
    # Windows: the interpreter needs these to start and resolve temp/DLL paths.
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "SYSTEMDRIVE",
    "HOMEDRIVE",
    "HOMEPATH",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
)

# Slice 9 P4, test 31. `_child_env` copies `_BASE_ENV_ALLOWLIST |
# LoopConfig.sandbox_env_allowlist` with **no denylist** — an operator who
# writes `"ANTHROPIC_API_KEY"` into the config knob hands it to arbitrary
# generated code, silently. Tolerable while the sandbox barely ran (an empty
# scratch workspace made most rounds report `status='na'`); not once worktree
# mode runs the operator's real suite every round and a real suite has a real
# reason to widen this knob (`DATABASE_URL`, a service token).
#
# A heuristic, documented as one: these are *name shapes*, not a registry of
# real secrets, so it can both miss (`SOME_CUSTOM_CRED`) and over-match (an
# app's own `FEATURE_TOKEN` flag that carries no secret). That is why the
# response is a warning naming the variable, never a refusal — an operator may
# have a genuine reason to pass a provider key into a test suite, and this
# project's rule (CLAUDE.md: "a refusal it cannot justify becomes a knob
# someone disables") is that silence is the only unacceptable outcome here,
# not permissiveness.
_CREDENTIAL_NAME_PATTERNS: tuple[str, ...] = (
    "*_API_KEY",
    "*_TOKEN",
    "*_SECRET",
    "*_PASSWORD",
    "AWS_*",
)


def credential_like_names(allowlist: list[str]) -> list[str]:
    """Which entries in `allowlist` look credential-shaped, by
    `_CREDENTIAL_NAME_PATTERNS`. Pure and total: never raises, and an
    unmatched entry is simply absent from the result — this never refuses
    anything, it only names what a caller may want to warn about."""
    import fnmatch

    matched = []
    for name in allowlist:
        upper = str(name).upper()
        if any(fnmatch.fnmatchcase(upper, p) for p in _CREDENTIAL_NAME_PATTERNS):
            matched.append(name)
    return matched


# Coverage totals, as the two tools that report one actually print them.
#
# `re.MULTILINE` is named explicitly and is load-bearing: without it `^`/`$`
# anchor to the whole captured output, so every real multi-line test log would
# miss while a single-line corpus still passed — the corpus could not tell the
# working parser from the broken one.
#
# The lookbehind in the first pattern keeps `Total coverage: 87.5%` from also
# matching it as "5%": a second, disagreeing value would make the honest answer
# `None` for a line that is not ambiguous at all.
#
# `.{0,200}?` rather than `.*?` bounds the backtracking on a pathologically long
# TOTAL line. A coverage row is short; a longer one simply reports nothing,
# which is the safe direction.
#
# The first pattern matches the *line*, not the value: every percentage on it
# is then collected into the same ambiguity check as every other match. The
# anchored-at-the-end version elected the **last** of two percentages on one
# line ("TOTAL 100 10 90% 50%" measured 50.0), because the ambiguity rule works
# across matches and could not see inside one — a wrong number, which is the
# single thing this function contracts never to produce.
_TOTAL_LINE_RE = re.compile(
    r"^[ \t]*TOTAL\b(?P<rest>.{0,200})$", re.MULTILINE | re.IGNORECASE
)
# The lookbehind excludes `-` as well as a digit or dot. Without the `-`,
# "TOTAL 1 0 -5%" captures `5` and reports 5.0 — a *sign error*, which is a
# wrong number rather than a missing one. No coverage tool emits a negative
# total, so the input is implausible; the fix costs one character and the
# failure it removes is the expensive direction, which is the trade this
# function makes everywhere else too.
_PERCENT_RE = re.compile(r"(?<![\d.-])(\d{1,3}(?:\.\d+)?)\s*%")
_NUMERIC_COLUMN_RE = re.compile(r"^\d+(?:\.\d+)?$")
# Both real coverage.py shapes put at least two numeric columns (statements and
# misses, or more under branch coverage) before the percentage. Requiring them
# is what keeps `^TOTAL\s+` with IGNORECASE from reading an English sentence —
# "TOTAL of 3 tests failed, 20%" measured 20.0, and the parsed corpus is
# model-written test output, so prose beginning with the word is the common
# input rather than an exotic one.
_MIN_NUMERIC_COLUMNS = 2

# a labelled total: "Total coverage: 87.5%"
_LABELLED_TOTAL_RE = re.compile(
    r"^\s*total\s+coverage\s*[:=]\s*(\d{1,3}(?:\.\d+)?)\s*%",
    re.MULTILINE | re.IGNORECASE,
)


def _total_row_values(output: str) -> list[float]:
    """Every percentage on every coverage-table TOTAL row, unfiltered.

    Unfiltered on purpose: two percentages on one row are two candidate
    answers, and handing both to the caller is what makes the ambiguity check
    see them. A row that clears neither the numeric-column bar nor the
    percentage pattern contributes nothing."""
    values: list[float] = []
    for match in _TOTAL_LINE_RE.finditer(output):
        rest = match.group("rest")
        percents = list(_PERCENT_RE.finditer(rest))
        if not percents:
            continue
        columns = rest[: percents[0].start()].split()
        if sum(bool(_NUMERIC_COLUMN_RE.match(c)) for c in columns) < (
            _MIN_NUMERIC_COLUMNS
        ):
            continue
        values.extend(float(p.group(1)) for p in percents)
    return values


def parse_coverage(output: str) -> float | None:
    """The coverage percentage the test output reported, or None (slice 6).

    Pure, total, and deliberately **conservative**: it returns `None` or a float
    in [0.0, 100.0] and cannot raise, for any input. `None` means "no coverage
    was reported" — never "0% coverage".

    **Ambiguity resolves toward `None`, the opposite direction from
    `agents._extract_findings`**, on purpose. Over-reading findings stores a
    little stray prose a human reading a verdict can discount; a fabricated
    coverage number renders on the dashboard as a *measurement*. A wrong metric
    is worse than an absent one.

    So **two disagreeing totals are ambiguity**: a multi-suite run, or
    coverage.py invoked per package, prints more than one TOTAL row, and there
    is no defensible rule for picking one of them — taking the first would store
    one suite's sub-total as *the* coverage. Several matches that all agree are
    not ambiguous and are kept.

    The parsed text is the workspace test command's output, which includes
    **model-written output**: a worker can fabricate this number by printing a
    `TOTAL ... 100%` line. That is tolerable only because no decision rule reads
    `coverage_percent` (DD-8) — it is a display value, not evidence, and must
    not be promoted to evidence without a different source.
    """
    try:
        values = set(_total_row_values(output))
        values |= {float(m.group(1)) for m in _LABELLED_TOTAL_RE.finditer(output)}
        if len(values) != 1:
            return None
        value = values.pop()
        if not 0.0 <= value <= 100.0:
            return None
        return value
    except Exception:
        return None


def split_command(command: str) -> list[str]:
    """Split a command string into argv, correctly on both platforms.

    shlex's POSIX mode treats backslash as an escape, so a Windows path like
    `C:\\venv\\Scripts\\python.exe` would be mangled into `C:venvScriptspython.exe`
    and fail as "command not found". Non-POSIX mode preserves separators but
    keeps the quotes around quoted arguments, so strip those back off.
    """
    if os.name == "nt":
        return [_unquote(tok) for tok in shlex.split(command, posix=False)]
    return shlex.split(command)


def _unquote(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1]
    return token


def _kill_tree(proc: subprocess.Popen) -> bool:
    """Kill the child **and everything it started**. Never raises.

    `Popen.kill()` signals only the direct child, so a test that shells out
    leaves its grandchildren running — and they hold the inherited stdout pipe,
    which is what makes the read below never end. On Windows `taskkill /T`
    walks the tree; elsewhere the child is its own session leader (see
    `start_new_session` at the call site) so one `killpg` reaches all of it.

    Returns **whether the child is confirmed gone**, and the return value is the
    point rather than a convenience. Both arms below can fail for ordinary
    reasons — `taskkill` answers "Access is denied" for an elevated or
    job-held child, `os.getpgid` raises `ProcessLookupError` — and swallowing
    that is correct (a failed kill must not raise into a paid round) only if
    somebody upstream can still tell. Returning `None` and then rendering
    "killed the process tree" was a string asserting more than its inputs
    proved, which is the one thing this project's conventions forbid outright.
    """
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=10,
                shell=False,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        pass  # last resort below; a failed kill must not raise into the loop
    try:
        proc.kill()
    except Exception:
        pass
    # `returncode` is set only once the process has actually been reaped, so
    # this is the observation rather than a hope about the two calls above.
    try:
        proc.wait(timeout=10)
    except Exception:
        pass
    return proc.returncode is not None


def _run_bounded(
    argv: list[str], *, cwd: str, timeout_s: int, env: dict[str, str]
) -> tuple[str, int, bool, float]:
    """Run one command, keeping only the tail of its output and enforcing a
    real wall-clock bound. Returns `(tail, exit_code, timed_out, waited_s)`.

    Replaces `subprocess.run(capture_output=True, timeout=...)`, which was
    wrong on this threat model in two independent ways. The stated threat is
    *arbitrary AI-generated code*, and the module docstring promised "a timeout
    bounds runtime; captured output is truncated to bound memory". Neither
    held:

    * **Memory.** `capture_output=True` materialises the *whole* stream and the
      truncation to `_MAX_TAIL_CHARS` happened afterwards, on a string that was
      already in memory. Measured: a child printing in a loop produced 331 MB in
      4 s (662 MB peak heap, bytes→str doubling); at the default 120 s timeout
      that extrapolates to ~9.8 GB held inside the orchestrator, to store 4000
      characters. Three lines of generated test code could OOM-kill the loop —
      and `agents._invoke` reaches `finish_attempt` only on a clean return, so
      the tokens and cost of the completion already paid for die with it.

    * **Time.** `subprocess.run`'s `timeout` kills only the direct child and
      then blocks in `communicate()` until every inherited pipe handle closes.
      Measured: a child spawning a 20 s grandchild that inherits stdout, with a
      3 s timeout, raised `TimeoutExpired` after 20.3 s. A grandchild that never
      exits blocks here **forever**, inside `_with_retry`, holding the task
      claim, with no recovery but killing the process.

    So: read in a reader thread into a bounded ring, and enforce the deadline
    ourselves by killing the whole process tree. The reader is a daemon, so even
    an orphan that somehow survives the kill cannot keep the interpreter alive.
    """
    # `stderr=STDOUT` so the two streams interleave in the order they were
    # written, which is what a human reading a failure tail wants, and what
    # `_summarize` and `parse_coverage` already assume of `combined`.
    popen_kwargs: dict = {
        "cwd": cwd,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
        "shell": False,  # never; argv is passed through literally
        "env": env,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    else:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(argv, **popen_kwargs)

    # Only the tail is ever retained, so peak memory is bounded by this deque
    # regardless of how much the child writes. Bytes, decoded once at the end:
    # decoding per chunk can split a multi-byte character across a boundary.
    chunks: deque[bytes] = deque()
    held = 0
    # Bytes, not characters: a bound in characters cannot be enforced before
    # decoding. 4x `_MAX_TAIL_CHARS` is enough that even 4-byte codepoints
    # cannot leave the tail short.
    cap = _MAX_TAIL_CHARS * 4

    def pump() -> None:
        nonlocal held
        try:
            assert proc.stdout is not None
            for block in iter(lambda: proc.stdout.read(65536), b""):
                chunks.append(block)
                held += len(block)
                while held > cap and len(chunks) > 1:
                    held -= len(chunks.popleft())
        except Exception:
            pass  # a closed pipe on kill is the normal end of this thread
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass

    reader = threading.Thread(target=pump, name="agentloop-testout", daemon=True)
    reader.start()

    started = time.time()
    timed_out = False
    killed = True
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        killed = _kill_tree(proc)
    waited = time.time() - started

    # The pump ends when the pipe closes, which happens when the last holder of
    # the inherited stdout handle exits — *not* when the direct child does. So a
    # bounded join, and its result is read.
    reader.join(timeout=5)
    lingering = reader.is_alive()

    if lingering and not timed_out:
        # The child exited normally within its timeout while a grandchild it
        # spawned kept the pipe open. `_kill_tree` used to run only on the
        # timeout branch, so this case leaked the grandchild (holding workspace
        # files open on Windows, which is what `clear_workspace`'s rmtree needs
        # released) and leaked one daemon thread per round — and `raw` below was
        # whatever had arrived by that instant, returned as though it were the
        # whole output. Kill it here too, then re-join.
        killed = _kill_tree(proc)
        reader.join(timeout=5)
        lingering = reader.is_alive()

    raw = b"".join(chunks)[-cap:]
    # `degraded` is "" exactly when nothing degraded — the same convention
    # `VcsResult.reason` uses, so a caller can render the honest sentence
    # instead of one that assumes the happy path.
    degraded = ""
    if not killed:
        degraded = "could not confirm the process tree was killed"
    elif lingering:
        degraded = "output may be incomplete: a child kept the pipe open"
    return (
        raw.decode("utf8", "replace"),
        proc.returncode if proc.returncode is not None else -1,
        timed_out,
        waited,
        degraded,
    )


class TestExecutor:
    """Runs a task's tests in its workspace and reports what actually happened."""

    # Not a pytest test class, despite the name.
    __test__ = False

    def __init__(
        self,
        command: str = "pytest -q",
        timeout_s: int = 120,
        enabled: bool = True,
        env_allowlist: list[str] | None = None,
        isolation: str = "env",
    ):
        self.command = command
        # Split here, at construction, and *not* inside `run()`. An unbalanced
        # quote in `loopconfig.json` (`pytest -q "C:\\my tests`) makes
        # `split_command` raise `ValueError: No closing quotation`, and `run()`
        # is called from inside `_with_retry`, whose handler treats every
        # exception as a transient infra failure — so a config typo became three
        # identical retries, three `infra_error` events, and a `needs_human`
        # reason pointing the operator at their network. Exactly the
        # misclassification `RunnerConfigError` was introduced to prevent one
        # module over. Raised from `__init__` it reaches `cli.main`'s handler and
        # renders as `error: ...`, which is where a config mistake belongs.
        split_command(command)
        self.timeout_s = timeout_s
        self.enabled = enabled
        self.env_allowlist = list(env_allowlist or [])
        self.requested_isolation = isolation
        self.effective_isolation = isolation
        # 'strict' asks for a container / no-network / read-only-fs tier. No
        # such backend ships in the stdlib-only core yet, so honor the request
        # when one is available and otherwise degrade to env-scrub — loudly, so
        # the residual risk (see module docstring) is never assumed away.
        if isolation == "strict" and not _strict_isolation_available():
            self.effective_isolation = "env"
            warnings.warn(
                "sandbox_isolation='strict' requested but no container/network "
                "isolation backend is available; degrading to env-scrub only. "
                "Residual risk: generated test code still runs with this "
                "process's filesystem and network access — only environment "
                "secrets are stripped.",
                RuntimeWarning,
                stacklevel=2,
            )

    def run(self, workspace: str | Path | None) -> TestResult:
        if not self.enabled:
            return TestResult(status="na", summary="Test execution disabled.")
        if workspace is None:
            return TestResult(status="na", summary="No workspace for this task.")

        ws = Path(workspace)
        if not ws.is_dir():
            return TestResult(status="na", summary=f"Workspace {ws} does not exist.")
        if not _has_any_file(ws):
            return TestResult(
                status="na", summary="Workspace is empty — nothing to test."
            )

        argv = split_command(self.command)
        if not argv:
            return TestResult(status="na", summary="No test command configured.")

        started = time.time()
        try:
            combined, code, timed_out, waited, degraded = _run_bounded(
                argv,
                cwd=str(ws),
                timeout_s=self.timeout_s,
                env=self._child_env(),
            )
        except FileNotFoundError:
            return TestResult(
                status="error",
                summary=f"Test command not found: {argv[0]}",
                duration_s=round(time.time() - started, 3),
            )
        except OSError as exc:
            return TestResult(
                status="error",
                summary=f"Could not run tests: {exc}",
                duration_s=round(time.time() - started, 3),
            )

        duration = round(time.time() - started, 3)

        # A degradation is stated in the `summary`, which `Store.add_test_run`
        # persists and the REST API and dashboard both render — so it reaches a
        # human the way every other recorded degradation in this project does,
        # rather than living only in a warning nobody sees. It also warns, for
        # the operator watching a terminal.
        #
        # It deliberately does **not** touch `status`. `status` feeds the tests
        # gate, and no decision rule may start reading a durability signal; an
        # unconfirmed kill is a statement about our cleanup, not about whether
        # the tests passed.
        if degraded:
            warnings.warn(
                f"test execution degraded ({degraded}); "
                f"the recorded result for this round says so.",
                RuntimeWarning,
                stacklevel=2,
            )
        note = f" [{degraded}]" if degraded else ""

        if timed_out:
            return TestResult(
                status="error",
                # The measured wait, not the requested one. `subprocess.run`'s
                # handler reported `self.timeout_s` unconditionally, so a call
                # that had actually blocked for an hour still said "120s" — a
                # rendered string asserting more than its inputs prove.
                #
                # The kill is reported the same way: `_kill_tree` answers
                # whether the child was actually reaped, and this sentence says
                # what happened rather than what was attempted. `taskkill`
                # answers "Access is denied" for an elevated or job-held child
                # and `os.getpgid` raises `ProcessLookupError`; swallowing those
                # is right, claiming success after them is not.
                summary=(
                    f"Tests timed out after {self.timeout_s}s "
                    f"(gave up at {waited:.1f}s; "
                    f"{'process tree killed' if not degraded else degraded})."
                ),
                stdout_tail=combined[-_MAX_TAIL_CHARS:],
                duration_s=duration,
            )
        return TestResult(
            status="pass" if code == 0 else "fail",
            exit_code=code,
            summary=_summarize(combined, code) + note,
            stdout_tail=combined[-_MAX_TAIL_CHARS:],
            duration_s=duration,
            # `None` when the output may be truncated: a coverage number parsed
            # from a partial stream is a fabricated measurement, which is the
            # one thing `parse_coverage` contracts never to produce.
            coverage_percent=None if degraded else parse_coverage(combined),
        )

    def _child_env(self) -> dict[str, str]:
        """A minimal environment for the subprocess.

        The parent env is *not* passed wholesale: it holds ANTHROPIC_API_KEY and
        every other secret, and this subprocess runs arbitrary generated code.
        Only the allowlist (base + LoopConfig.sandbox_env_allowlist) is copied.
        Matching is case-insensitive because Windows env keys vary in case
        (SystemRoot vs SYSTEMROOT)."""
        allow = {n.upper() for n in _BASE_ENV_ALLOWLIST}
        allow |= {n.upper() for n in self.env_allowlist}
        env = {k: v for k, v in os.environ.items() if k.upper() in allow}
        # Keep child output stable and unbuffered for readable tails.
        env["PYTHONUNBUFFERED"] = "1"
        # The running interpreter's script directory is prepended to PATH, and
        # this is a correctness fix rather than a convenience. The README offers
        # `.venv\\Scripts\\agentloop.exe` as an equal alternative to activating
        # the venv, and taken up, the venv's `Scripts` is not on `PATH` — so the
        # default `test_command` of `pytest -q` could not resolve. Measured on a
        # real run: `Test command not found: pytest`.
        #
        # That failure is quiet, though not in the way an earlier version of
        # this comment claimed. `TestResult.passed` returns `False` for both
        # `"fail"` and `"error"` (only `"na"` returns `None` and falls back to
        # the validator's claim), so an unresolvable command does **not** become
        # auto-approvable — it fails *every* round and burns `max_revisions` on
        # a gap no worker can close, escalating with a reason about test
        # failures that never ran. The correction matters because in this repo
        # the comments are the spec: a future reader trusting the old wording
        # would conclude that a timeout or a missing binary is auto-approvable.
        #
        # The interpreter running the loop is the one whose tools the default
        # command means. Prepended, not appended, so a venv's `pytest` wins over
        # a stale global one — the same interpreter/tool pairing the operator
        # gets from activating.
        scripts_dir = str(Path(sys.executable).resolve().parent)
        existing = env.get("PATH", "")
        if scripts_dir and scripts_dir not in existing.split(os.pathsep):
            env["PATH"] = scripts_dir + (os.pathsep + existing if existing else "")
        return env


def _worktree_dir_name(repo_root: str | Path) -> str:
    """`<repo-name>-<short-hash>` — the directory one repository's worktrees
    share under `worktree_root`.

    Hashed over the **resolved** `repo_root` (P3 introduced the hash;
    remediation-5 fixed what it resolved over), not the basename alone, so two
    checkouts of the same repository at different paths — or two unrelated
    repositories that happen to share a basename — cannot collide in one
    shared root and silently mix one repository's task branches with
    another's.

    `realpath` before `normcase`, **not** lexical `abspath` — this is exactly
    `Store._repo_key`'s distinction (see its docstring) and resolves the same
    way for the same reason: this computation has no subprocess to spawn, it
    is a pure key derivation over a string feeding a directory-name decision,
    which is `config._is_within`'s category, not `vcs._git`'s `-C` argument
    (which stays lexical on purpose because *that* value becomes a
    subprocess's cwd, and a junction substituting the directory git actually
    runs in is the escape `vcs._guard`'s identity checks exist to close). A
    pure key has nothing for a junction to substitute, so staying lexical here
    bought nothing and cost the property this key exists to hold:
    `worktree_root_for` is where every task's worktree for one repository is
    supposed to live, and `agentloop workspace prune` recomputes this same
    path from whatever spelling of `repo_root` the current invocation was
    given. Measured with a real Windows junction: a lexical hash produced a
    different `<name>-<hash>` for the target directory and a junction alias of
    it, so a workspace created under one spelling was never found — and so
    never reclaimed — by `prune` invoked with the other, silently, with no
    error, no event and no warning. `os.path.normcase` after resolving so
    Windows' case-insensitive paths and a `/` vs `\\` spelling still hash
    identically; POSIX is unaffected (`normcase` is identity there). An
    unresolvable path (a dangling reference, a mixed-drive path) falls back to
    plain `abspath` rather than raising — a key must always be computable, and
    failing to resolve a spelling is not evidence the two spellings differ.

    The name prefix is taken from the **resolved** path too, not the given
    spelling — a junction or symlink alias can carry an arbitrary basename of
    its own (e.g. a symlink named `alias-of-it` pointing at a directory named
    `target-directory`), and deriving the name from the unresolved spelling
    would leave two aliases sharing an identical hash but disagreeing on the
    human-readable prefix, so the two still could not produce the one grouping
    key this function exists to guarantee."""
    try:
        resolved = os.path.realpath(str(repo_root))
    except Exception:
        resolved = os.path.abspath(str(repo_root))
    abs_root = os.path.normcase(resolved)
    name = Path(resolved).name or "repo"
    digest = hashlib.sha256(abs_root.encode("utf-8")).hexdigest()[:8]
    return f"{name}-{digest}"


def worktree_root_for(config: "LoopConfig", repo_root: str | Path) -> Path:
    """`<config.worktree_root>/<repo-name>-<hash>`, absolute. The parent
    `workspace_for` places `task-<id>` under in worktree mode."""
    base = Path(os.path.abspath(os.path.expanduser(config.worktree_root)))
    return base / _worktree_dir_name(repo_root)


def workspace_for(
    root: str | Path,
    task_id: int,
    create: bool = False,
    *,
    config: "LoopConfig | None" = None,
    repo_root: str | Path | None = None,
    pin: str = "",
) -> Path:
    """Per-task workspace, always **absolute**. Isolated so a redo can wipe it
    for a true fresh start rather than rerunning over dirty state.

    Absolutised here, at the one place a workspace path is produced, because
    since slice 9's P1 that path is read from *two different directories*: the
    orchestrator's, when the loop resolves it, and the workspace itself, when
    the agent reads the `## Workspace` instruction naming it after `cwd` has
    put it there. `workspace_root` ships relative (`.agentloop/ws`), so the
    same string denoted two directories — the agent created
    `<ws>/.agentloop/ws/task-N`, pytest's `norecursedirs` skipped the dotted
    directory, nothing was collected, and the tests gate blocked approval every
    round until the revisions ran out, escalating with a reason about tests
    rather than about a path.

    `os.path.abspath`, **lexically, and deliberately not `Path.resolve()`** —
    the same choice `vcs._git` documents for its `-C` value: resolve follows a
    junction at the workspace and would quietly take over the one decision
    `vcs._guard` exists to make.

    **Worktree mode (P3)**: when `config.workspace_mode == 'worktree'` and
    `repo_root` is given, `root` is ignored for the directory computation — the
    workspace lives at `worktree_root_for(config, repo_root) / f"task-{id}"`
    instead of `<root>/task-{id}`, and `create=True` delegates to
    `vcs.init_repo` (a real checkout on its own branch) rather than `mkdir`.
    `pin` is the caller's recorded repository-level baseline (see
    `Store.vcs_repo_pin`); `""` is the "no baseline recorded yet" case that lets
    `init_repo` mint one — the first task on a repository does this.

    `config` defaulting to `None` is what keeps every one of the loop's six
    existing call sites unchanged and scratch-mode behaviour a **proven**
    no-op: an unset `config` never reads `workspace_mode`, so wiring `config`/
    `repo_root`/`pin` through those call sites is left for P4 to do, not
    something this function forces on them. Total like every `vcs` entry
    point: a failed `init_repo` is not raised here, it is returned by `vcs` and
    the path is handed back regardless — the caller's own retry/escalation
    path (unchanged by this slice) is what acts on it."""
    if (
        config is not None
        and config.workspace_mode == "worktree"
        and repo_root is not None
    ):
        ws = worktree_root_for(config, repo_root) / f"task-{task_id}"
        if create:
            from . import vcs

            vcs.init_repo(
                ws,
                config,
                pin,
                repo_root=repo_root,
                task_id=task_id,
                start_ref=config.vcs_base_ref,
                branch_prefix=config.vcs_branch_prefix,
            )
        return ws
    ws = Path(os.path.abspath(Path(root) / f"task-{task_id}"))
    if create:
        ws.mkdir(parents=True, exist_ok=True)
    return ws


def _on_rm_error(func, path, exc) -> None:
    """Retry one failed removal after clearing the read-only bit (DD-14).

    Git writes loose objects and packfiles read-only, and on Windows that
    attribute blocks unlink outright — measured here: a workspace holding a real
    repo has 5 such files, and `rmtree(..., ignore_errors=True)` left the
    directory standing with 12 entries. Never raises; a removal that still fails
    is swallowed and `clear_workspace` does one ignore_errors sweep after."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception:
        pass


# `onexc` replaced `onerror` in 3.12 (where `onerror` is deprecated); the
# project floor is 3.10. Both callbacks are called with the same three
# positional arguments, so one handler shape serves both.
_RMTREE_KW = (
    {"onexc": _on_rm_error}
    if sys.version_info >= (3, 12)
    else {"onerror": lambda f, p, e: _on_rm_error(f, p, e)}
)


def clear_workspace(
    root: str | Path,
    task_id: int,
    *,
    config: "LoopConfig | None" = None,
    repo_root: str | Path | None = None,
    pin: str = "",
) -> bool:
    """Wipe a task workspace (used by human_redo — no carried-over state) and
    report whether it is gone.

    Total: never raises, whatever the filesystem does — its callers have no
    error handling of their own, and a caller is free to ignore the answer.

    It **is** an answer, though, and that is the point of the return value:
    with an error handler installed `rmtree` swallows per-file failures and
    returns `None` whether it wiped the directory or left it, and slice 6
    promotes this function to the fallback for every failed rollback, where a
    redo that quietly kept the previous round is invisible. The report is the
    observed state of the filesystem afterwards, not a guess from which branch
    ran — a workspace that never existed is `True`, because "not there" is the
    outcome that was asked for.

    **Worktree mode (P3)**: an `rmtree` alone is wrong here — it leaves the
    admin entry under `<repo_root>/.git/worktrees/<name>` registered, so
    `git worktree list` keeps reporting a workspace that is gone and a later
    `worktree add` at the same path is refused as already registered (see
    `vcs.remove_worktree`'s docstring). So when `config.workspace_mode ==
    'worktree'` and `repo_root` is given, this routes to `vcs.remove_worktree`
    instead — never `rmtree` — and reports `True` only when the workspace is
    confirmed gone afterwards (either `remove_worktree` succeeded, or the
    directory was already absent, matching the scratch branch's own "not
    there" reading of `True`). `config=None` (the default) is unchanged from
    before this slice."""
    if (
        config is not None
        and config.workspace_mode == "worktree"
        and repo_root is not None
    ):
        ws = workspace_for(root, task_id, config=config, repo_root=repo_root)
        if not ws.exists():
            return True
        from . import vcs

        result = vcs.remove_worktree(
            ws,
            config,
            pin,
            repo_root=repo_root,
            task_id=task_id,
            branch_prefix=config.vcs_branch_prefix,
        )
        try:
            return bool(result.ok) or not ws.exists()
        except Exception:
            return False

    ws = workspace_for(root, task_id)
    if not ws.is_dir():
        return True
    try:
        shutil.rmtree(ws, **_RMTREE_KW)
    except Exception:
        try:
            shutil.rmtree(ws, ignore_errors=True)
        except Exception:
            pass
    try:
        return not ws.exists()
    except Exception:
        return False


def _has_any_file(ws: Path) -> bool:
    """Whether the workspace holds anything the test command could act on.

    `.git` does not count (D-3). Slice 6 gives every task workspace its own
    repo, so counting git's own objects would stop a genuinely empty workspace
    reporting `status='na'` and would instead run the test command against
    nothing — the one way per-task repos could silently move the tests gate.
    Matched on the path *component*, not a substring, so `src/.gitignore` still
    counts."""
    return any(
        p.is_file() and ".git" not in p.relative_to(ws).parts for p in ws.rglob("*")
    )


def _strict_isolation_available() -> bool:
    """Whether a strong-isolation backend (container / no-network /
    read-only-fs) is wired up. None ships in the stdlib-only core yet, so this
    is False and 'strict' degrades to env-scrub; a later slice can plug a
    backend in here without touching call sites."""
    return False


def _summarize(output: str, returncode: int) -> str:
    """Last non-empty line is the useful one for most runners (pytest's
    '3 passed in 0.1s'); fall back to the exit code."""
    for line in reversed(output.strip().splitlines()):
        if line.strip():
            return line.strip()[:300]
    return f"Exited with code {returncode}."
