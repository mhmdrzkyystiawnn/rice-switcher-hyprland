"""Fase 5: safe listing / planning / removal of `.bak-<ts>` backups.

`snapshot.swap_symlink` preserves the previous live `config_link` as
``<target>.bak-<ts>`` (``snapshot.py:404-421``), where ``ts`` is the
``_now_ts()`` format ``%Y%m%dT%H%M%S_<ms>`` (``snapshot.py:49-58``). The
entry can be a **symlink** (the usual case — the live link pointed at a
rice tree) or a **real directory** (the first swap, when the live path was
a real directory).

This module is pure and read-only except for `remove_backup`, and it is
deliberately paranoid:

* only names matching the exact switch timestamp pattern are considered;
* sorting is by the timestamp **in the name**, never by mtime;
* `lstat` is always used — a symlink is removed with `unlink` (the target
  is never touched), a real directory with `shutil.rmtree`;
* anything whose parent directory is not inside `config_home` is refused
  (containment via `os.path.commonpath` on the realpath of the *parent*,
  leaving the last component unresolved so a symlink entry is not followed).

Nothing here executes anything and nothing uses `os.path.isdir` to decide.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
from dataclasses import dataclass

# `<name>.bak-<8 digits>T<6 digits>_<3 digits>` — the exact `_now_ts()`
# shape. A bare `.bak-`, `.bak-1`, or the keybind variant
# (`%Y%m%d-%H%M%S`) deliberately do NOT match.
_BACKUP_RE = re.compile(r"^.+\.bak-(\d{8}T\d{6}_\d{3})$")


class BackupError(ValueError):
    """Raised when a path is not a removable tool backup."""


@dataclass(frozen=True)
class Backup:
    """A tool-owned `.bak-<ts>` entry."""

    path: str
    ts: str
    kind: str   # "symlink" | "dir"


def _classify(abspath: str) -> str | None:
    """`"symlink"`, `"dir"`, or None for anything else. Never follows."""
    try:
        st = os.lstat(abspath)
    except OSError:
        return None
    if stat.S_ISLNK(st.st_mode):
        return "symlink"
    if stat.S_ISDIR(st.st_mode):
        return "dir"
    return None


def list_backups(config_home: str) -> list[Backup]:
    """Tool-owned backups in `config_home`, newest first (by name ts).

    Non-matching names, regular files, and anything that is neither a
    symlink nor a real directory are ignored.
    """
    out: list[Backup] = []
    try:
        names = os.listdir(config_home)
    except OSError:
        return []
    for name in names:
        m = _BACKUP_RE.match(name)
        if not m:
            continue
        path = os.path.join(config_home, name)
        kind = _classify(path)
        if kind is None:
            continue
        out.append(Backup(path=path, ts=m.group(1), kind=kind))
    # Lexicographic order over the fixed-width ts == chronological.
    out.sort(key=lambda b: b.ts, reverse=True)
    return out


def plan_cleanup(entries: list[Backup], keep: int | None) -> list[Backup]:
    """Return the entries to remove, leaving the `keep` newest.

    `keep` is `None` or `0` to remove every candidate; a positive `keep`
    leaves that many newest entries. Side-effect free. `entries` is
    expected newest-first (from `list_backups`).
    """
    if keep is None or keep <= 0:
        return list(entries)
    return list(entries[keep:])


def remove_backup(path: str, config_home: str) -> str:
    """Remove one tool backup; return the kind actually removed.

    Refuses anything that is not a `.bak-<ts>` entry, that is neither a
    symlink nor a real directory, or whose parent directory is not inside
    `config_home`. A symlink is unlinked (its target is left intact); a
    real directory is removed with `shutil.rmtree`.
    """
    abspath = os.path.abspath(path)
    base = os.path.basename(abspath)
    if not _BACKUP_RE.match(base):
        raise BackupError(f"not a .bak-<ts> backup: {base!r}")

    # Containment on the PARENT (resolved), never on the entry itself: a
    # symlink entry must not be followed to decide whether it is safe.
    cfg_real = os.path.realpath(config_home)
    parent_real = os.path.realpath(os.path.dirname(abspath))
    try:
        inside = os.path.commonpath([cfg_real, parent_real]) == cfg_real
    except ValueError:
        inside = False
    if not inside:
        raise BackupError(
            f"refusing to remove {abspath!r}: parent is outside "
            f"config_home ({cfg_real})"
        )

    kind = _classify(abspath)
    if kind == "symlink":
        os.unlink(abspath)
        return "symlink"
    if kind == "dir":
        shutil.rmtree(abspath)
        return "dir"
    raise BackupError(
        f"refusing to remove {abspath!r}: not a symlink or a directory"
    )
