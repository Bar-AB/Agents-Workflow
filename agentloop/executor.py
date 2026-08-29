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
- A timeout bounds runtime; captured output is truncated to bound memory.
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

import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
import warnings
from pathlib import Path

from .models import TestResult

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
            proc = subprocess.run(
                argv,
                cwd=str(ws),
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                shell=False,  # never; argv is passed through literally
                env=self._child_env(),
            )
        except FileNotFoundError:
            return TestResult(
                status="error",
                summary=f"Test command not found: {argv[0]}",
                duration_s=round(time.time() - started, 3),
            )
        except subprocess.TimeoutExpired:
            return TestResult(
                status="error",
                summary=f"Tests timed out after {self.timeout_s}s.",
                duration_s=round(time.time() - started, 3),
            )
        except OSError as exc:
            return TestResult(
                status="error",
                summary=f"Could not run tests: {exc}",
                duration_s=round(time.time() - started, 3),
            )

        duration = round(time.time() - started, 3)
        combined = (proc.stdout or "") + (proc.stderr or "")
        return TestResult(
            status="pass" if proc.returncode == 0 else "fail",
            exit_code=proc.returncode,
            summary=_summarize(combined, proc.returncode),
            stdout_tail=combined[-_MAX_TAIL_CHARS:],
            duration_s=duration,
            coverage_percent=parse_coverage(combined),
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
        return env


def workspace_for(root: str | Path, task_id: int, create: bool = False) -> Path:
    """Per-task workspace. Isolated so a redo can wipe it for a true fresh
    start rather than rerunning over dirty state."""
    ws = Path(root) / f"task-{task_id}"
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


def clear_workspace(root: str | Path, task_id: int) -> bool:
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
    outcome that was asked for."""
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
