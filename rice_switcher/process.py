"""Process identity and /proc scan.

PLAN.md §2g. We do NOT use `pkill -f` (too broad). Instead, we scan
`/proc/<pid>/cmdline` for an exact match of the executable basename plus
a subset match on `argv_match` tokens.

This module is read-only: it never sends a signal. The actual `kill` is
performed by `cli.py` (Slice 1 dry-run path) or a future real-execution
path; the slice 1 implementation only enumerates identities and reports
them.

Note for the executor: `kill` is performed by the caller; this module
returns the list of PIDs. The caller decides whether to actually send a
signal (Slice 1: never).
"""

from __future__ import annotations

import os
import re


def _is_pid_dir(name: str) -> bool:
    return name.isdigit()


def list_all_pids() -> list[int]:
    try:
        return [int(n) for n in os.listdir("/proc") if _is_pid_dir(n)]
    except FileNotFoundError:
        return []


def read_cmdline(pid: int) -> list[str] | None:
    """Return argv of a process as a list of strings, or None on access error.

    Trailing NUL is stripped; an empty cmdline is returned as []. This
    matches the semantics expected by `find_pids_by_identity` below.
    """
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            data = f.read()
    except (FileNotFoundError, ProcessLookupError, PermissionError, IsADirectoryError):
        return None
    if not data:
        return []
    parts = data.split(b"\0")
    # argv is NUL-separated; trailing empty field from the final NUL is
    # conventional but we strip empties to be safe.
    return [p.decode("utf-8", errors="replace") for p in parts if p]


def identity_from_argv(argv: list[str]) -> tuple[str, tuple[str, ...]] | None:
    """Extract the (executable_basename, argv_tokens) from an argv list.

    Returns None if argv is empty (kernel thread or zombie).
    """
    if not argv:
        return None
    exe = os.path.basename(argv[0])
    return (exe, tuple(argv[1:]))


def find_pids_by_identity(
    executable: str,
    argv_match: tuple[str, ...] | list[str] = (),
    *,
    exclude_self: bool = True,
    extra_exclude_pids: tuple[int, ...] = (),
) -> list[int]:
    """Return PIDs whose argv[0] basename == `executable` AND every token
    in `argv_match` is present in their argv (subset match, order-independent).

    Per PLAN.md §2g, this is the disambiguator for processes like
    `qs -c ii` vs `qs -c minimal`: both have basename `qs`, but their
    argv_match differ.

    Caller-supplied PIDs (`extra_exclude_pids`) are filtered out, in
    addition to self/parent when `exclude_self=True`. We exclude self
    and parent by default because rice-switcher itself runs Python and
    might be the caller's own process.
    """
    argv_match = tuple(argv_match)
    exclude = set(extra_exclude_pids)
    if exclude_self:
        try:
            exclude.add(os.getpid())
            exclude.add(os.getppid())
        except OSError:
            pass

    out: list[int] = []
    for pid in list_all_pids():
        if pid in exclude:
            continue
        argv = read_cmdline(pid)
        if argv is None:
            continue
        ident = identity_from_argv(argv)
        if ident is None:
            continue
        exe, tokens = ident
        if exe != executable:
            continue
        if not all(tok in tokens for tok in argv_match):
            continue
        out.append(pid)
    return out


# Reject pkill-style patterns in user-supplied start/stop fields.
# PLAN.md §2g + §2h: `pkill -f <pattern>` is too broad. We refuse to
# load manifests that contain such patterns.
_PKILL_F_PATTERNS = [
    re.compile(r"\bpkill\s+(-\w*f\w*|--full)\b"),
    re.compile(r"\bpkill\b\s+-f\b"),
    re.compile(r"\bpkill\b\s+--full\b"),
]


def looks_like_dangerous_pkill(command: str) -> bool:
    """Return True if the command string uses `pkill -f` / `pkill --full`.

    Slice 1 keeps this conservative: we flag any `-f` or `--full` flag
    on a `pkill` call. The manifest loader in `validate.py` raises if
    this returns True, unless the user has explicitly allowed it (not
    exposed in Slice 1 CLI; it stays a hard error).
    """
    if not command:
        return False
    for pat in _PKILL_F_PATTERNS:
        if pat.search(command):
            return True
    return False


# ---------------------------------------------------------------------------
# Slice 2: tri-state probe (running / stopped / unknown)
# ---------------------------------------------------------------------------

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ProbeResult:
    pids: tuple[int, ...]
    status: Literal["running", "stopped", "unknown"]
    error: str = ""


def probe_pids_by_identity(
    executable: str,
    argv_match: tuple[str, ...] | list[str] = (),
    *,
    exclude_self: bool = True,
    extra_exclude_pids: tuple[int, ...] = (),
) -> ProbeResult:
    """Slice 2 tri-state probe. Returns:
    - ("running", pids)        if at least one PID matches
    - ("stopped", ())          if probe succeeded and no match
    - ("unknown", (), error)   if /proc is unreadable

    The status is determined solely by whether `list_all_pids()` could
    read /proc. Per-PID read errors are silently skipped and do not
    change the overall status. The wrapper has the SAME argv_match
    semantics as `find_pids_by_identity` above.
    """
    try:
        all_pids = list_all_pids()
    except OSError as e:
        return ProbeResult(pids=(), status="unknown", error=str(e))
    argv_match = tuple(argv_match)
    exclude = set(extra_exclude_pids)
    if exclude_self:
        try:
            exclude.add(os.getpid())
            exclude.add(os.getppid())
        except OSError:
            pass
    out: list[int] = []
    for pid in all_pids:
        if pid in exclude:
            continue
        argv = read_cmdline(pid)
        if argv is None:
            continue
        ident = identity_from_argv(argv)
        if ident is None:
            continue
        exe, tokens = ident
        if exe != executable:
            continue
        if not all(tok in tokens for tok in argv_match):
            continue
        out.append(pid)
    return ProbeResult(
        pids=tuple(out),
        status="running" if out else "stopped",
    )
