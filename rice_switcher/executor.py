"""External command execution (PLAN.md §2h, §3.2, §3.5).

This module is the single choke point for running commands on behalf of a
manifest. Design rules (PLAN.md §2h):

- Never `eval` a string. Short commands are `shlex.split` into an argv list
  and executed with `subprocess.run(argv, ...)`.
- Commands that genuinely need a shell (pipelines, redirection, `VAR=val`
  prefixes) are detected and run through `bash -lc`. This is the same
  escape hatch PLAN.md §2h allows for hooks.
- `pkill -f` / `pkill --full` is refused outright (`process.looks_like_dangerous_pkill`),
  because it matches the full command line, including the switcher itself.
- Every call takes an optional `runner` so tests can inject a fake; the
  default runner is `subprocess.run`.

The module is pure with respect to the desktop in the sense that it does
not decide *what* to run; callers (see `lifecycle.py`) do.
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from typing import Callable, Sequence

from .process import looks_like_dangerous_pkill

# PLAN.md §2h: default 10s per component command; hooks 30s; matugen 60s.
DEFAULT_TIMEOUT = 10.0
HOOK_TIMEOUT = 30.0
MATUGEN_TIMEOUT = 60.0

# Shell metacharacters that mean a plain argv split would be wrong.
_SHELL_META = set("|&;<>()$`\\\"'*?[]{}~!")


@dataclass(frozen=True)
class ExecResult:
    """Outcome of one command execution."""

    argv: tuple[str, ...]
    rc: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.rc == 0 and not self.timed_out


# A runner receives (argv, timeout) and returns an ExecResult.
Runner = Callable[[Sequence[str], float], ExecResult]


def _decode(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def default_runner(argv: Sequence[str], timeout: float = DEFAULT_TIMEOUT) -> ExecResult:
    """Run `argv` with `subprocess.run`. Never uses a shell string."""
    argv = list(argv)
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False,
        )
        return ExecResult(tuple(argv), proc.returncode, proc.stdout or "", proc.stderr or "")
    except subprocess.TimeoutExpired as e:
        return ExecResult(tuple(argv), -1, _decode(e.stdout), _decode(e.stderr), timed_out=True)
    except FileNotFoundError as e:
        return ExecResult(tuple(argv), 127, "", str(e))
    except OSError as e:
        return ExecResult(tuple(argv), 126, "", str(e))


def needs_shell(command: str) -> bool:
    """True if `command` cannot be faithfully represented as a plain argv."""
    if not command:
        return False
    if any(ch in _SHELL_META for ch in command):
        return True
    # A leading `VAR=value` assignment only works in a shell.
    first = command.split()[0]
    return "=" in first and not first.startswith("=")


def split_command(command: str) -> tuple[list[str], str]:
    """Split a manifest command into argv. Returns (argv, error).

    An empty command, or the literal `true`, is a no-op and returns
    `([], "")`. A refused `pkill -f` returns `([], reason)`.
    """
    if not command or command.strip() in ("", "true"):
        return [], ""
    if looks_like_dangerous_pkill(command):
        return [], f"refusing dangerous command (pkill -f / --full): {command!r}"
    try:
        argv = shlex.split(command)
    except ValueError as e:
        return [], f"could not parse command {command!r}: {e}"
    return argv, ""


def run_argv(
    argv: Sequence[str],
    *,
    timeout: float = DEFAULT_TIMEOUT,
    runner: Runner | None = None,
) -> ExecResult:
    run = runner or default_runner
    return run(list(argv), timeout)


def run_command(
    command: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    runner: Runner | None = None,
) -> ExecResult:
    """Run a manifest command string.

    No-op commands return rc=0 without spawning anything. Shell-needing
    commands go through `bash -lc`; everything else is argv-split.
    """
    argv, err = split_command(command)
    if err:
        return ExecResult((), 2, "", err)
    if not argv:
        return ExecResult((), 0, "", "")
    if needs_shell(command):
        return run_argv(["bash", "-lc", command], timeout=timeout, runner=runner)
    return run_argv(argv, timeout=timeout, runner=runner)


def run_hook(
    command: str,
    *,
    timeout: float = HOOK_TIMEOUT,
    runner: Runner | None = None,
) -> ExecResult:
    """Run a hook script through `bash -lc` (PLAN.md §2h)."""
    if not command or command.strip() == "":
        return ExecResult((), 0, "", "")
    if looks_like_dangerous_pkill(command):
        return ExecResult((), 2, "", f"refusing dangerous hook: {command!r}")
    return run_argv(["bash", "-lc", command], timeout=timeout, runner=runner)
