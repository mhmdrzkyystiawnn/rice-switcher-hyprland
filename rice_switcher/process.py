"""Process identity and /proc scan.

PLAN.md §2g. We do NOT use `pkill -f` (too broad). Instead, we scan
`/proc/<pid>/cmdline` for an exact match of the executable basename plus
a match on `argv_match` tokens.

Two match modes (Fase 2b Langkah 1):

  * **subset** (default, legacy): every token in `argv_match` must be
    present in argv, order-independent. `argv_match` must be non-empty.
  * **exact** (`exact=True`, declared via the manifest field
    `exact_argv`): argv (after the executable) must equal `argv_match`
    exactly — same tokens, same order, no extras. An **empty**
    `argv_match` with `exact=True` therefore means "the executable with
    no arguments", which is how an argument-less daemon (waybar,
    hypridle, mako, dunst) gets a stoppable identity. This is still not
    a basename-only match: the process's argv must have exactly one
    element.

Enumeration (`find_pids_by_identity`, `probe_pids_by_identity`) is
read-only. The signalling path lives in `terminate_by_identity`
(Prasyarat 0 / DESIGN-onboarding-tui.md §4.2): because the identity can
come from someone else's `rice.toml`, it only signals processes we own,
re-reads `/proc/<pid>/cmdline` right before each signal, and refuses a
basename-only match.
"""

from __future__ import annotations

import functools
import os
import re
import signal
import time
from typing import Callable


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


def argv_matches(
    tokens: tuple[str, ...] | list[str],
    argv_match: tuple[str, ...] | list[str],
    *,
    exact: bool = False,
) -> bool:
    """True if a process's argv tokens satisfy the declared identity.

    `exact=False` (legacy): subset match, order-independent. Every token
    in `argv_match` must be present in `tokens`; extras are allowed.

    `exact=True` (Fase 2b Langkah 1): `tokens` must equal `argv_match`
    exactly (same tokens, same order, no extras). An empty `argv_match`
    matches only a process whose argv is the executable alone.
    """
    if exact:
        return tuple(tokens) == tuple(argv_match)
    return all(tok in tokens for tok in tuple(argv_match))


def find_pids_by_identity(
    executable: str,
    argv_match: tuple[str, ...] | list[str] = (),
    *,
    exact: bool = False,
    exclude_self: bool = True,
    extra_exclude_pids: tuple[int, ...] = (),
) -> list[int]:
    """Return PIDs whose argv[0] basename == `executable` AND whose argv
    tokens satisfy `argv_match`.

    With `exact=False` (default) the match is a subset match
    (order-independent, extras allowed). With `exact=True` the tokens
    must equal `argv_match` exactly; an empty `argv_match` then selects
    only argument-less processes (Fase 2b Langkah 1).

    Per PLAN.md §2g, the subset mode is the disambiguator for processes
    like `qs -c ii` vs `qs -c minimal`: both have basename `qs`, but
    their argv_match differ.

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
        if not argv_matches(tokens, argv_match, exact=exact):
            continue
        out.append(pid)
    return out


def is_own_uid(pid: int, *, uid: int | None = None) -> bool:
    """True if `/proc/<pid>` is owned by `uid` (default: our own uid).

    We only ever signal processes we own. Any error (missing PID, no
    permission) returns False.
    """
    if uid is None:
        uid = os.getuid()
    try:
        st = os.stat(f"/proc/{pid}")
    except OSError:
        return False
    return st.st_uid == uid


def pid_matches_identity(
    pid: int,
    executable: str,
    argv_match: tuple[str, ...] | list[str] = (),
    *,
    exact: bool = False,
) -> bool:
    """Re-read `/proc/<pid>/cmdline` and confirm it still matches.

    Used immediately before signalling to defeat PID reuse: between the
    scan and the kill, the PID may have been recycled by an unrelated
    process. A missing, unreadable or empty (zombie) cmdline returns
    False, as does a different basename or a non-matching argv (subset
    by default, exact with `exact=True`; see `argv_matches`).
    """
    argv = read_cmdline(pid)
    if not argv:
        return False
    ident = identity_from_argv(argv)
    if ident is None:
        return False
    exe, tokens = ident
    if exe != executable:
        return False
    return argv_matches(tokens, argv_match, exact=exact)


def terminate_by_identity(
    executable: str,
    argv_match: tuple[str, ...] | list[str],
    *,
    exact: bool = False,
    timeout: float = 10.0,
    sigterm: int = signal.SIGTERM,
    sigkill: int = signal.SIGKILL,
    find: Callable[..., list[int]] | None = None,
    matches: Callable[[int, str, tuple[str, ...] | list[str]], bool] | None = None,
    owned: Callable[[int], bool] | None = None,
    kill: Callable[[int, int], None] | None = None,
    sleeper: Callable[[float], None] | None = None,
    clock: Callable[[], float] | None = None,
    poll_interval: float = 0.2,
) -> list[int]:
    """Stop every process matching `(executable, argv_match)` that we own.

    Guards (DESIGN-onboarding-tui.md §4.2):

    * own uid only; never PID 1, this process, or its parent;
    * `argv_match` is **required** unless `exact=True` declares an
      identity explicitly: a basename-only match is too broad
      (`qs -c ii` and `qs -c minimal` share the basename `qs`). With
      `exact=True`, an empty `argv_match` is allowed and means "the
      executable with no arguments" — still not basename-only, because
      the argv must have exactly one element (Fase 2b Langkah 1);
    * `/proc/<pid>/cmdline` is re-read right before each signal (PID
      reuse guard), with the same exact/subset semantics;
    * SIGTERM, wait up to `timeout`, then SIGKILL the survivors.

    All side effects go through the injected
    `find`/`matches`/`owned`/`kill`/`sleeper`/`clock` seams, so tests can
    drive it deterministically without touching a real process. Returns
    the PIDs that were sent SIGTERM.
    """
    argv_match = tuple(argv_match)

    # Bind the default seams to the requested match mode, while keeping
    # the injected seams' (executable, argv_match) 2-arg signature.
    if find is None:
        find = (functools.partial(find_pids_by_identity, exact=True)
                if exact else find_pids_by_identity)
    if matches is None:
        matches = (functools.partial(pid_matches_identity, exact=True)
                   if exact else pid_matches_identity)
    if owned is None:
        owned = is_own_uid
    if kill is None:
        kill = os.kill
    if sleeper is None:
        sleeper = time.sleep
    if clock is None:
        clock = time.monotonic

    if not argv_match and not exact:
        raise ValueError(
            "refusing to kill by basename alone: argv_match is required "
            f"(executable={executable!r}); set exact_argv=true to declare "
            f"an explicit no-argument identity"
        )

    forbidden = {1}
    try:
        forbidden.add(os.getpid())
        forbidden.add(os.getppid())
    except OSError:
        pass

    signaled: list[int] = []
    for pid in find(executable, argv_match):
        if pid in forbidden:
            continue
        if not owned(pid):
            continue
        if not matches(pid, executable, argv_match):
            continue  # PID reuse / already gone
        try:
            kill(pid, sigterm)
        except ProcessLookupError:
            continue
        signaled.append(pid)

    if not signaled:
        return []

    start = clock()
    while True:
        alive = [p for p in signaled if matches(p, executable, argv_match)]
        if not alive:
            break
        if clock() - start >= timeout:
            for pid in alive:
                if not matches(pid, executable, argv_match):
                    continue  # ended in the meantime
                try:
                    kill(pid, sigkill)
                except ProcessLookupError:
                    pass
            break
        sleeper(poll_interval)
    return signaled


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
    exact: bool = False,
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
    semantics as `find_pids_by_identity` above (`exact` included).
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
        if not argv_matches(tokens, argv_match, exact=exact):
            continue
        out.append(pid)
    return ProbeResult(
        pids=tuple(out),
        status="running" if out else "stopped",
    )
