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
import shlex
import subprocess
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


def clear_workspace(root: str | Path, task_id: int) -> None:
    """Wipe a task's workspace (used by human_redo — no carried-over state)."""
    import shutil

    ws = workspace_for(root, task_id)
    if ws.is_dir():
        shutil.rmtree(ws, ignore_errors=True)


def _has_any_file(ws: Path) -> bool:
    return any(p.is_file() for p in ws.rglob("*"))


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
