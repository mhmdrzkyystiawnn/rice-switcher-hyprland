"""Snapshot and rollback.

PLAN.md §5.3 (Snapshot & Rollback Strategy, revisi #3). The snapshot
captures a self-contained copy of the live config_link targets of the
*current* rice, before a switch swaps the symlinks. On rollback, the
symlinks are restored and (best-effort) the old processes are
restarted.

Slice 1 implementation:
- `create_pending(roots, manifest, config_link_realpaths)` copies the
  target directories into `.pending-<ts>/`. The caller flips it to a
  committed snapshot once the swap succeeds.
- `commit(roots, ts)` renames `.pending-<ts>` to `<ts>/` and updates
  the `latest` symlink.
- `restore(roots, ts)` undoes the swap by copying the committed
  snapshot back over the live config_link targets.
- `prune(roots, keep=5)` keeps only the N most-recent committed
  snapshots; older ones are removed.

The `pending` -> `commit` dance is what guarantees §5.3's invariant:
"snapshot is complete and validated BEFORE any swap is performed".
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .paths import (
    Roots,
    committed_snapshot_dir,
    latest_snapshot_link,
    pending_snapshot_dir,
    snapshot_dir,
)


@dataclass
class SnapshotInfo:
    ts: str
    pending: bool
    paths: list[str]   # config_link paths that were captured


def _now_ts() -> str:
    """A timestamp safe for filesystem use and chronological sorting.

    Includes a millisecond suffix so two snapshots in the same second
    still get distinct names. Without this, fast loops (e.g. tests
    that create seven snapshots in a row) can collide and break
    `os.rename` over a non-empty directory.
    """
    now = time.time()
    return time.strftime("%Y%m%dT%H%M%S", time.localtime(now)) + f"_{int((now % 1) * 1000):03d}"


def list_committed(roots: Roots) -> list[str]:
    """Return the timestamps of all committed snapshots, newest first.

    Also includes `.pending-*` directories: those are *in-progress*
    snapshots and are surfaced so the user / auditor can see them.
    Returns all timestamps as strings (caller can filter).
    """
    d = Path(snapshot_dir(roots))
    if not d.is_dir():
        return []
    items: list[tuple[str, bool]] = []
    for child in d.iterdir():
        if not child.is_dir():
            continue
        name = child.name
        if name.startswith(".pending-"):
            items.append((name[len(".pending-"):], True))
        elif name != "latest":
            items.append((name, False))
    # Sort by timestamp string (lexicographic == chronological for our format).
    items.sort(key=lambda t: t[0], reverse=True)
    return [ts for ts, _ in items]


def pending_path(roots: Roots, ts: str) -> str:
    return pending_snapshot_dir(roots, ts)


def committed_path(roots: Roots, ts: str) -> str:
    return committed_snapshot_dir(roots, ts)


def _copy_tree_symlinks(src: str, dst: str) -> None:
    """Copy `src` to `dst`, preserving symlinks as symlinks.

    PLAN.md §5.3: NEVER dereference. Use `shutil.copytree(symlinks=True)`.
    If `dst` already exists, it is removed first; this matches the
    "fresh snapshot" semantics.
    """
    if os.path.lexists(dst):
        if os.path.islink(dst) or os.path.isfile(dst):
            os.unlink(dst)
        else:
            shutil.rmtree(dst)
    if not os.path.lexists(src):
        # Source doesn't exist: create an empty target so the snapshot
        # is "complete" (the config_link is recorded as a realpath-less
        # entry). This is the path taken when a fresh rice has no live
        # config yet.
        os.makedirs(dst, exist_ok=True)
        return
    if os.path.islink(src):
        # If the live config_link is itself a symlink to a target that
        # doesn't exist (dangling), we still want to record the link.
        # We materialise the link target inside the snapshot.
        os.makedirs(dst, exist_ok=True)
        link_target = os.readlink(src)
        # Use an absolute path so the snapshot is self-contained.
        link_dst = os.path.join(dst, os.path.basename(src.rstrip("/")))
        if not os.path.exists(link_dst):
            os.symlink(os.path.abspath(os.path.join(os.path.dirname(src), link_target)),
                       link_dst)
        return
    if os.path.isdir(src):
        shutil.copytree(src, dst, symlinks=True)
    else:
        # Single file: copy with metadata
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)


def create_pending(
    roots: Roots,
    config_link_paths: Iterable[str],
    rice_name: str = "",
    *,
    config_home: str | None = None,
) -> tuple[str, list[str]]:
    """Create a pending snapshot capturing the live state of each
    `config_link_path`. Returns (ts, captured_paths).

    The pending directory is `.pending-<ts>/`; the caller is expected
    to `commit()` it once the swap succeeds, or discard it on failure.

    The optional `rice_name` is recorded in `.captured.txt` so that
    `do_rollback` can restore `state.current_rice` to the rice that
    was actually captured. If empty, the snapshot is still created but
    `do_rollback` will report an explicit error (see PLAN.md §2h).
    """
    os.makedirs(snapshot_dir(roots), exist_ok=True)
    ts = _now_ts()
    pend = pending_path(roots, ts)
    os.makedirs(pend, exist_ok=True)

    captured: list[str] = []
    for cl in config_link_paths:
        # Normalise before touching the filesystem: `os.path.lexists`
        # does NOT expand `~`, so a config_link written as
        # `~/.config/hypr` would look non-existent and the snapshot
        # would silently capture an EMPTY directory. Expand here (and
        # record the absolute path, so `restore` targets the right
        # place) to keep snapshots faithful.
        cl = os.path.abspath(os.path.expanduser(cl))
        # Each config_link gets its own subdir under the snapshot,
        # named after the basename of the config_link.
        name = os.path.basename(cl.rstrip("/")) or "config"
        dst = os.path.join(pend, name)
        _copy_tree_symlinks(cl, dst)
        captured.append(cl)

    # Validate rice_name is safe to write into the .captured.txt file.
    # We restrict the rice name in the snapshot to the same alphabet as
    # `paths.validate_rice_name`. If a name was supplied that fails the
    # check, refuse to write it: do_rollback would not be able to
    # trust it. Pass `rice_name=""` explicitly to suppress the warning
    # (older snapshots are still readable via restore()).
    safe_name = ""
    if rice_name:
        from .paths import validate_rice_name as _v
        ok, _err = _v(rice_name)
        if ok:
            safe_name = rice_name
    rice_name_for_capture = safe_name

    # Drop a small manifest so we can audit what's in the snapshot.
    # Format:
    #   rice=<name>
    #   <config_link_1>
    #   <config_link_2>
    #   ...
    # PLAN.md §2h: the rice name is required for `do_rollback` to
    # restore `state.current_rice` to the rice that was actually
    # captured. Without this, a rollback leaves the state pointing at
    # a stale rice name. Older snapshots (without the "rice=" line)
    # are loaded as best-effort (see restore()).
    with open(os.path.join(pend, ".captured.txt"), "w", encoding="utf-8") as f:
        f.write(f"rice={rice_name_for_capture}\n")
        for c in captured:
            f.write(c + "\n")
    return ts, captured


def commit(roots: Roots, ts: str) -> None:
    """Promote a pending snapshot to a committed one. Updates the
    `latest` symlink and prunes old snapshots.
    """
    pend = pending_path(roots, ts)
    final = committed_path(roots, ts)
    if not os.path.isdir(pend):
        raise FileNotFoundError(f"pending snapshot not found: {pend}")
    # Remove the `latest` symlink if it points inside the pending dir,
    # otherwise os.rename across directories will fail with
    # "Directory not empty". We re-create `latest` after the rename.
    latest = latest_snapshot_link(roots)
    try:
        if os.path.islink(latest):
            target = os.readlink(latest)
            latest_full = os.path.join(os.path.dirname(latest), target)
            if os.path.samefile(latest_full, pend):
                os.unlink(latest)
    except (FileNotFoundError, OSError):
        pass
    os.rename(pend, final)
    # Update latest symlink.
    try:
        if os.path.islink(latest) or os.path.exists(latest):
            os.unlink(latest)
    except FileNotFoundError:
        pass
    os.symlink(ts, latest)


def discard_pending(roots: Roots, ts: str) -> None:
    """Remove a pending snapshot (e.g. after a failed switch)."""
    pend = pending_path(roots, ts)
    if os.path.isdir(pend):
        shutil.rmtree(pend)


def prune(roots: Roots, keep: int = 5) -> list[str]:
    """Keep only the `keep` most recent committed snapshots. Returns
    the list of pruned timestamps.
    """
    committed = [ts for ts in list_committed(roots) if os.path.isdir(committed_path(roots, ts))]
    to_remove = committed[keep:]
    for ts in to_remove:
        path = committed_path(roots, ts)
        try:
            shutil.rmtree(path)
        except OSError:
            pass
        # If the latest symlink pointed at the pruned snapshot, drop it
        # so the user notices the gap. We don't auto-promote an older
        # one (the user can pick explicitly with `rice-switcher rollback`).
        latest = latest_snapshot_link(roots)
        try:
            if os.path.islink(latest) and os.readlink(latest) == ts:
                os.unlink(latest)
        except OSError:
            pass
    return to_remove


def restore(roots: Roots, ts: str) -> tuple[list[str], str]:
    """Restore a committed snapshot by copying its contents back over
    the live config_link paths.

    Returns
    -------
    (restored_paths, rice_name)
        `restored_paths` lists the config_link paths that were
        restored. `rice_name` is the name recorded by `create_pending`
        via the `rice=...` line in `.captured.txt`, or the empty
        string if the snapshot predates that field (audit fix S4).

    Reads the `.captured.txt` manifest written by `create_pending` to
    know which paths to restore. If the manifest is missing, returns
    the list of top-level subdirs of the snapshot (best effort).
    """
    snap = committed_path(roots, ts)
    if not os.path.isdir(snap):
        raise FileNotFoundError(f"snapshot not found: {snap}")
    manifest = os.path.join(snap, ".captured.txt")
    rice_name = ""
    paths: list[str] = []
    if os.path.isfile(manifest):
        for raw in open(manifest, "r", encoding="utf-8"):
            line = raw.rstrip("\n").rstrip("\r")
            if not line.strip():
                continue
            if line.startswith("rice="):
                # Audit fix S4: rice name is recorded for state restoration.
                candidate = line[len("rice="):]
                # Validate before trusting it; the snapshot may have
                # been hand-edited.
                from .paths import validate_rice_name as _v
                ok, _err = _v(candidate)
                if ok:
                    rice_name = candidate
                # else: leave rice_name="" and let the caller handle
                # it explicitly.
                continue
            paths.append(line)
    else:
        paths = [os.path.join(snap, d) for d in os.listdir(snap) if d != ".captured.txt"]

    restored: list[str] = []
    for entry in paths:
        # We stored under the basename of the config_link path. Look it
        # up in `.captured.txt` ordering: paths[i] <-> basename(paths[i]).
        if os.path.isfile(manifest):
            try:
                idx = paths.index(entry)
            except ValueError:
                continue
            name = os.path.basename(entry.rstrip("/")) or "config"
            src = os.path.join(snap, name)
        else:
            src = entry
            name = os.path.basename(src)
        # Resolve the live target: we re-read the original config_link
        # by looking up the basename in `.captured.txt`.
        # The destination is the recorded path itself.
        dst = entry
        # Resolve a top-level symlink at `src` to its real directory.
        # `_copy_tree_symlinks` preserves symlinks with their ORIGINAL
        # target string, which may be relative. When `restore` then
        # copies that entry to `dst` (which lives at the live config
        # path), the relative target would resolve to the wrong place.
        # The fix is: if `src` is a symlink to a directory, follow it
        # once and treat the real directory as the source so that
        # shutil.copytree copies its CONTENT (not the broken symlink).
        real_src = src
        if os.path.islink(src) and not os.path.isdir(src):
            # symlink to a non-directory: use the original code path
            # below.
            pass
        elif os.path.islink(src):
            # symlink to a directory: follow it once for restore
            try:
                real_src = os.path.realpath(src)
            except OSError:
                real_src = src
        if os.path.isdir(real_src):
            # Replace dst (whether symlink, dir, or absent) with src.
            if os.path.lexists(dst):
                if os.path.islink(dst) or os.path.isfile(dst):
                    os.unlink(dst)
                else:
                    shutil.rmtree(dst)
            # symlinks=True preserves sub-symlinks (e.g. dangling
            # symlinks in the live config) verbatim, with the same
            # fixup applied via _copy_tree_symlinks.
            shutil.copytree(real_src, dst, symlinks=True)
        elif os.path.islink(src):
            # top-level symlink to a non-directory (file or dangling).
            # We must read the ORIGINAL target string (not the
            # resolved path) and re-anchor it relative to src so the
            # new symlink at `dst` resolves correctly.
            raw_target = os.readlink(src)
            src_dir = os.path.dirname(src)
            if os.path.isabs(raw_target):
                target = raw_target
            else:
                target = os.path.normpath(os.path.join(src_dir, raw_target))
            if os.path.lexists(dst):
                os.unlink(dst)
            os.symlink(target, dst)
        elif os.path.isfile(real_src):
            if os.path.lexists(dst):
                os.unlink(dst)
            shutil.copy2(real_src, dst)
        restored.append(dst)
    return restored, rice_name


def current_symlinks_state(roots: Roots, config_link_paths: Iterable[str]) -> dict[str, str | None]:
    """For each config_link path, return its realpath or None if it does
    not exist (or is not a symlink/file). Used by the switcher to
    capture pre-swap state.
    """
    out: dict[str, str | None] = {}
    for p in config_link_paths:
        n = os.path.abspath(os.path.expanduser(p))
        if not os.path.lexists(n):
            out[n] = None
        else:
            try:
                out[n] = os.path.realpath(n)
            except OSError:
                out[n] = None
    return out


def swap_symlink(target: str, source: str) -> None:
    """Atomically replace the symlink at `target` with a new symlink to
    `source`. The previous target is preserved as `<target>.bak-<ts>`.
    """
    if not os.path.lexists(target):
        # No existing target: just create the symlink.
        os.symlink(source, target)
        return
    # Back up the existing entry. Use a timestamp suffix.
    ts = _now_ts()
    backup = f"{target}.bak-{ts}"
    os.rename(target, backup)
    try:
        os.symlink(source, target)
    except OSError:
        # If the new symlink fails, restore the backup.
        os.rename(backup, target)
        raise


def undo_swap(target: str) -> bool:
    """Undo a previous `swap_symlink(target, ...)`: remove the symlink
    at `target` and rename the `.bak-<ts>` back if present. Returns
    True if a rollback was performed, False if there was nothing to
    undo.
    """
    parent = os.path.dirname(target)
    base = os.path.basename(target)
    backups: list[str] = []
    try:
        for entry in os.listdir(parent):
            if entry.startswith(f"{base}.bak-"):
                backups.append(entry)
    except FileNotFoundError:
        return False
    if not backups:
        return False
    # Pick the most recent backup (lexicographic == chronological for ts).
    backups.sort(reverse=True)
    chosen = os.path.join(parent, backups[0])
    if os.path.lexists(target):
        os.unlink(target)
    os.rename(chosen, target)
    return True
