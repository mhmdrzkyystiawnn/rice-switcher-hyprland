"""`rice-switcher keybind-install` (Slice 3).

Writes a `hl.bind` line for `SUPER+SHIFT+R` to
`~/.config/hypr/custom/keybinds.lua` (or a user-supplied --path).

Slice 2 deferred this; Slice 3 implements it. The keybind targets
`rice-switcher apply`, which itself lands in Slice 3 — the bind is
written as a string literal and never executed by this module.

Design contract (matches the koreksi-terakhir audit):

- Default target path: ``~/.config/hypr/custom/keybinds.lua``.
- The target path must be under ``~/.config/hypr/`` unless the user
  passes ``--allow-any-path`` (opt-in escape hatch).
- Idempotent: if a `hl.bind` for `SUPER+SHIFT+R` already exists, the
  function is a no-op and returns ``"already_present"``.
- Conflict: if a `hl.bind` for `SUPER+SHIFT+R` exists but with a
  different ``exec_cmd``, return ``"conflict"`` and DO NOT modify.
- Detection regex: ``r'hl\\.bind\\s*\\(\\s*"([^"]+)"'`` — extracts
  the keysym regardless of whitespace/formatting variations.
- Backup: any existing target file is backed up to
  ``<target>.bak-<ts>`` in the same directory. The backup is NEVER
  auto-deleted.
- Lock: a sidecar ``<dir>/.keybind.lock`` with ``flock(LOCK_EX)``
  serialises all writers.
- Audit: each invocation emits a JSON Lines audit event via
  ``append_audit`` (Slice 2 API).
- Dry-run: ``--dry-run`` prints the plan without writing any file or
  emitting an audit event.
"""

from __future__ import annotations

import errno
import fcntl
import os
import re
import time
from dataclasses import dataclass
from typing import Literal

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The keysym we install.
KEYSYM = "SUPER+SHIFT+R"

# The default command the bind invokes. Slice 3 will provide
# `rice-switcher apply`; this constant is the string literal we
# embed in the config file. We do NOT shell out to it.
DEFAULT_COMMAND = "rice-switcher apply"

# Default target path: ~/.config/hypr/custom/keybinds.lua
DEFAULT_TARGET = os.path.expanduser("~/.config/hypr/custom/keybinds.lua")

# Allowed root for the target path (when --allow-any-path is not set).
ALLOWED_ROOT = os.path.realpath(os.path.expanduser("~/.config/hypr"))

# Regex to extract the keysym + exec_cmd from a `hl.bind(...)`
# expression. We use a multi-line aware pattern that tolerates
# arbitrary whitespace and newlines between `hl.bind` and the keysym
# string. We deliberately anchor the search to `hl.bind` so that
# occurrences inside string literals (e.g. local x = "hl.bind(...)")
# are NOT matched — that would be a false positive.
_HLBIND_OPEN_RE = re.compile(r'(?:^|[\n;])\s*hl\.bind\s*\(', re.MULTILINE)
_KEYSYM_RE = re.compile(r'"([^"]+)"')
_EXEC_CMD_RE = re.compile(r'exec_cmd\s*\(\s*"([^"]*)"')

# Lines that are clearly comments (Lua) or continuations of a bind
# expression started on a previous line. We keep a small set of heuristic
# patterns to ignore.
_COMMENT_RE = re.compile(r'^\s*--')


@dataclass(frozen=True)
class InstallResult:
    path: str
    result: Literal["added", "already_present", "conflict", "dry_run", "error"]
    detail: str = ""


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------

def _check_path_allowed(target: str, allow_any: bool) -> tuple[bool, str]:
    """Verify the target path is under the allowed root unless the
    user explicitly opts in to arbitrary paths. Returns (ok, error_msg)."""
    if allow_any:
        return True, ""
    real = os.path.realpath(target)
    if not real.startswith(ALLOWED_ROOT + os.sep) and real != ALLOWED_ROOT:
        return False, (
            f"target path {target!r} resolves to {real!r}, which is outside "
            f"the allowed root {ALLOWED_ROOT!r}. Pass --allow-any-path to override."
        )
    return True, ""


# ---------------------------------------------------------------------------
# Parse + classify existing keybinds
# ---------------------------------------------------------------------------

def _parse_binds(content: str) -> list[tuple[int, str, str, int, int]]:
    """Return a list of (line_no, keysym, exec_cmd, start_col, end_col)
    for every `hl.bind("KEYSYM", ..., exec_cmd("CMD"))` expression in
    `content`.

    `line_no` is the line where `hl.bind` was found. `start_col`/
    `end_col` are byte offsets into the source so we can locate the
    expression precisely.

    Multi-line expressions are tolerated: the search regex finds
    `hl.bind(` at the start of an expression and then we look for the
    first two string-literal arguments (keysym, then exec_cmd). String
    literals inside Lua comments or local variables (which do not start
    with `hl.bind(` at line start) are NOT matched.
    """
    out: list[tuple[int, str, str, int, int]] = []
    for m in _HLBIND_OPEN_RE.finditer(content):
        start = m.start()
        # Compute 1-based line number for the start of the match.
        line_no = content.count("\n", 0, start) + 1
        # Slice from the open paren onwards and look for the two
        # string literals (keysym, then exec_cmd). To avoid spanning
        # into the next bind, we cap the scan at the next `hl.bind(`
        # or 500 characters, whichever comes first.
        after = content[m.end():]
        next_open = after.find("hl.bind(")
        if next_open == -1 or next_open > 500:
            scan = after[:500]
        else:
            scan = after[:next_open]
        # First string literal = keysym
        km = _KEYSYM_RE.search(scan)
        if not km:
            continue
        keysym = km.group(1)
        # Second string literal = exec_cmd; look for exec_cmd(
        em = _EXEC_CMD_RE.search(scan, km.end())
        exec_cmd = em.group(1) if em else ""
        out.append((line_no, keysym, exec_cmd, start, m.end() + len(scan)))
    return out


def _classify_target(
    content: str, keysym: str, target_command: str
) -> tuple[str, str]:
    """Inspect an existing keybinds file.

    Returns (status, detail):
      - ("present", "") if a bind for `keysym` already invokes `target_command`
      - ("conflict", "<line>: <raw>") if a bind for `keysym` invokes something else
      - ("absent", "") if no bind for `keysym` exists
    """
    for lineno, ks, exec_cmd, _, _ in _parse_binds(content):
        if ks != keysym:
            continue
        if exec_cmd == target_command:
            return "present", ""
        return "conflict", f"line {lineno} already binds {keysym!r} to {exec_cmd!r}"
    return "absent", ""


# ---------------------------------------------------------------------------
# Lock
# ---------------------------------------------------------------------------

def _keybind_lock(target: str, timeout: float = 5.0) -> int:
    """Acquire a flock on a sidecar lockfile next to the target.
    Returns the file descriptor (caller closes it)."""
    lock_path = os.path.join(os.path.dirname(target), ".keybind.lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError as e:
            if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                os.close(fd)
                raise
            if time.monotonic() >= deadline:
                os.close(fd)
                raise TimeoutError(
                    f"keybind lock not acquired within {timeout}s at {lock_path}"
                )
            time.sleep(0.05)


# ---------------------------------------------------------------------------
# Render the new bind line
# ---------------------------------------------------------------------------

def render_bind_line(keysym: str, command: str, description: str) -> str:
    """Render one `hl.bind(...)` line.

    Output format matches the existing line in
    ``~/.config/hypr/custom/keybinds.lua``::

        hl.bind("SUPER+SHIFT+R", hl.dsp.exec_cmd("rice-switcher apply"), {description = "Apply current rice / open rice menu"} )
    """
    return (
        f'hl.bind("{keysym}", '
        f'hl.dsp.exec_cmd("{command}"), '
        f'{{description = "{description}"}} )'
    )


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

def _backup_existing(target: str) -> str | None:
    """If `target` is a regular file, copy it to ``target.bak-<ts>`` and
    return the backup path. The original is left in place so the
    caller can decide what to do (e.g. rename-then-rewrite)."""
    if not os.path.isfile(target):
        return None
    ts = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    backup = f"{target}.bak-{ts}"
    # Avoid clobbering an existing backup: append milliseconds.
    if os.path.exists(backup):
        ms = int(time.time() * 1000) % 1000
        backup = f"{target}.bak-{ts}-{ms:03d}"
    # Read source, write to backup atomically via temp + rename.
    tmp = backup + ".tmp"
    with open(target, "rb") as src, open(tmp, "wb") as dst:
        dst.write(src.read())
    os.rename(tmp, backup)
    return backup


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

def install(
    roots,
    *,
    target_path: str = DEFAULT_TARGET,
    command: str = DEFAULT_COMMAND,
    description: str = "Apply current rice / open rice menu",
    allow_any_path: bool = False,
    dry_run: bool = False,
) -> InstallResult:
    """Install (or report on) the SUPER+SHIFT+R keybind.

    Returns an InstallResult describing the action taken. The caller
    is responsible for emitting the audit event (this function
    intentionally does NOT call append_audit, so the caller can attach
    additional fields or skip auditing in tests)."""
    target = os.path.expanduser(target_path)
    ok, err = _check_path_allowed(target, allow_any_path)
    if not ok:
        return InstallResult(path=target, result="error", detail=err)

    # Read existing content (if any).
    existing = ""
    if os.path.isfile(target):
        with open(target, "r", encoding="utf-8") as f:
            existing = f.read()

    status, detail = _classify_target(existing, KEYSYM, command)

    if status == "present":
        return InstallResult(path=target, result="already_present", detail=detail)
    if status == "conflict":
        return InstallResult(path=target, result="conflict", detail=detail)

    # status == "absent" — we need to write.
    new_line = render_bind_line(KEYSYM, command, description)

    if dry_run:
        return InstallResult(path=target, result="dry_run", detail=new_line)

    fd = _keybind_lock(target)
    try:
        # Re-check inside the lock (another writer may have raced us).
        existing = ""
        if os.path.isfile(target):
            with open(target, "r", encoding="utf-8") as f:
                existing = f.read()
        status, detail = _classify_target(existing, KEYSYM, command)
        if status == "present":
            return InstallResult(path=target, result="already_present", detail=detail)
        if status == "conflict":
            return InstallResult(path=target, result="conflict", detail=detail)

        backup_path = _backup_existing(target)

        # Append the new line. We always start on a new line so the
        # bind is well-separated from any pre-existing content.
        mode = "a" if existing else "w"
        # If existing content does not end with a newline, add one.
        prefix = ""
        if existing and not existing.endswith("\n"):
            prefix = "\n"
        with open(target, mode, encoding="utf-8") as f:
            f.write(prefix + new_line + "\n")
            f.flush()
            os.fsync(f.fileno())

        return InstallResult(
            path=target,
            result="added",
            detail=(
                f"appended keybind to {target}"
                + (f" (backup: {backup_path})" if backup_path else "")
            ),
        )
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)
