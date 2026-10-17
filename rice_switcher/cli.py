"""CLI entry for rice-switcher.

Slice 1 subcommands (per PLAN.md §11.1):
  init         — scaffold <rice_home>, state file, log dir
  add <name>   — scaffold <rice_home>/<name>/rice.toml
  list         — list every rice in <rice_home>
  current      — print the current rice (from state.json)
  status       — table: rice × component × running × config_link
  switch <name> [--dry-run | --no-dry-run]
               — the full flow; default --dry-run
  snapshot     — capture a manual snapshot of the current state
  rollback [ts]
               — restore the latest (or specified) committed snapshot

All filesystem writes are confined to:
  * <rice_home>/<name>/                (rice add)
  * <state_home>/rice-switcher/        (state, snapshot, log)
  * <config_home>/...                  (config_link symlinks, ONLY when
                                        switch executes outside --dry-run)

In Slice 1 the default behaviour is dry-run. Real execution requires
explicit --no-dry-run. The implementation honours RICE_DRY_RUN=1.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import textwrap
from typing import Sequence

# Audit fix S1: rice name validation lives in `paths` (no circular
# dependency with `cli`).
from .lock import LockError
from .log import setup_logging
from .managed_set import compute_plan
from .manifest import (
    Component,
    Manifest,
    load_manifest,
    list_manifests,
    parse_manifest,
)
from .paths import (
    Roots,
    manifest_path,
    resolve_roots,
    rice_dir,
    state_file,
    validate_rice_name,
)
from .snapshot import (
    current_symlinks_state,
    list_committed,
)
from .state import read_state
from .switch import do_rollback, do_switch
from .validate import (
    ValidationError,
    validate_manifest,
)


# ---------------------------------------------------------------------------
# Subcommand: init
# ---------------------------------------------------------------------------

def cmd_init(args, roots: Roots, log) -> int:
    """Scaffold the directory layout. Idempotent."""
    from .paths import log_dir
    import shutil
    for d in (roots.rice_home,
              os.path.join(roots.state_home, "rice-switcher"),
              log_dir(roots),
              os.path.join(roots.state_home, "rice-switcher", "snapshot")):
        os.makedirs(d, exist_ok=True)
    # state.json — create if missing.
    if not os.path.isfile(state_file(roots)):
        from .state import State, write_state
        write_state(roots, State())
    print(f"initialized at rice_home={roots.rice_home} state_home={roots.state_home}")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: add
# ---------------------------------------------------------------------------

def cmd_add(args, roots: Roots, log) -> int:
    name = args.name
    # Audit fix S1: validate the rice name BEFORE creating anything. A
    # rejected name must not touch the filesystem in any way.
    ok, err = validate_rice_name(name)
    if not ok:
        print(f"error: {err}", file=sys.stderr)
        return 2
    target_dir = rice_dir(roots, name)
    os.makedirs(target_dir, exist_ok=True)
    manifest_file = manifest_path(roots, name)
    if os.path.isfile(manifest_file):
        print(f"error: {manifest_file} already exists; refusing to overwrite", file=sys.stderr)
        return 2
    # Minimal scaffold manifest
    example = f"""[meta]
name = "{name}"
description = "TODO: describe this rice"

# config_link: at least one entry per rice that needs a swap.
# Examples:
# config_dir = "./"
# wallpaper = "./wallpapers/default.png"

[theme]
# cursor_name = "Bibata-Modern-Classic"
# cursor_size = 24
# gtk_theme = "adw-gtk3-dark"
# icon_theme = "Tela-circle-dracula"

[hooks]
# pre = ["./scripts/pre.sh"]
# post = ["./scripts/post.sh"]

[run]
# config_home = "/tmp/test-cfg"
# state_home = "/tmp/test-state"
# rice_home = "/tmp/test-rice"

[[components]]
name = "hyprland"
type = "compositor"
start = "true"
stop = "true"
reload = "hyprctl reload"
config_link = "~/.config/hypr"
"""
    with open(manifest_file, "w", encoding="utf-8") as f:
        f.write(example)
    print(f"created {manifest_file}")
    print("Edit it to declare components, then run `rice-switcher switch <name> --dry-run`.")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: list
# ---------------------------------------------------------------------------

def cmd_list(args, roots: Roots, log) -> int:
    rices = list_manifests(roots.rice_home)
    if not rices:
        print("(no rice found; run `rice-switcher add <name>`)", file=sys.stderr)
        return 0
    print(f"{'NAME':24} {'MANIFEST'}")
    print(f"{'----':24} {'--------'}")
    for name, path in rices:
        print(f"{name:24} {path}")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: current
# ---------------------------------------------------------------------------

def cmd_current(args, roots: Roots, log) -> int:
    state = read_state(roots)
    if not state.current_rice:
        print("(no current rice; never switched)", file=sys.stderr)
        return 0
    print(state.current_rice)
    return 0


# ---------------------------------------------------------------------------
# Subcommand: status
# ---------------------------------------------------------------------------

def cmd_validate(args, roots: Roots, log) -> int:
    """Slice 2: comprehensive manifest validation."""
    from .validate import extended_validate
    mp = os.path.join(roots.rice_home, args.name, "rice.toml")
    if not os.path.isfile(mp):
        print(f"error: no rice named {args.name!r}", file=sys.stderr)
        return 2
    try:
        m = load_manifest(mp)
    except Exception as e:
        print(f"error: failed to parse manifest: {e}", file=sys.stderr)
        return 1
    res = extended_validate(
        m, roots,
        check_hyprland_verify=True,
        matugen_config_search=("matugen/config.toml",),
    )
    # Format: status line + per-check report
    for e in res.errors:
        print(f"[E] {e}")
    for w in res.warnings:
        print(f"[W] {w}")
    if not res.errors and not res.warnings:
        print("[ok] all checks passed")
    print(f"\n{len(res.errors)} error(s), {len(res.warnings)} warning(s)")
    return 1 if res.errors else 0


def _expected_target(rice_home: str, rice_name: str, config_link: str) -> str:
    """Compute the expected symlink target for a rice.

    Mirrors the formula in switch.py do_switch (slice 1):
        <RICE_HOME>/<rice_name>/<basename(config_link)>
    """
    link_norm = os.path.abspath(os.path.expanduser(config_link))
    rice_root = os.path.abspath(os.path.expanduser(rice_home))
    return os.path.join(rice_root, rice_name, os.path.basename(link_norm))


def _norm_symlink_target(link_path: str, raw_target: str) -> str:
    """Normalize a symlink target for comparison. Resolves relative
    targets against the link's directory, then normpath."""
    if os.path.isabs(raw_target):
        return os.path.normpath(raw_target)
    return os.path.normpath(os.path.join(os.path.dirname(link_path), raw_target))


def _probe_symlink(path: str) -> tuple[bool, str | None]:
    """Return (exists, actual_target_normalized_or_None)."""
    if not os.path.lexists(path):
        return False, None
    if not os.path.islink(path):
        return True, None
    try:
        target = os.readlink(path)
    except OSError:
        return True, None
    return True, _norm_symlink_target(path, target)


def _rice_status(
    rice_name: str,
    config_link: str,
    state_current: str,
    rice_home: str,
    state_trust_names: set[str],
) -> str:
    """Compute the per-rice status label per the Slice 2 contract."""
    path = os.path.abspath(os.path.expanduser(config_link))
    exists, actual = _probe_symlink(path)
    if not exists:
        if rice_name in state_trust_names:
            return "missing"
        return "uninit"
    if actual is None:
        # Path exists but is not a symlink
        if os.path.isdir(path):
            return "dir"
        if os.path.isfile(path):
            return "file"
        return "regular"
    expected = _expected_target(rice_home, rice_name, config_link)
    if actual == expected:
        if state_current == rice_name:
            return "current"
        return "idle"
    # Symlink target != expected for this rice
    if state_current == rice_name:
        return "current-mismatch"
    return "idle-foreign"


def _owner_label(rice_home: str, all_rice_names: list[str], config_link: str) -> str:
    """Compute the OWNER column: rice whose expected_target matches
    the actual symlink target, or '<unmanaged:/abs/path>'."""
    path = os.path.abspath(os.path.expanduser(config_link))
    exists, actual = _probe_symlink(path)
    if not exists or actual is None:
        return "-"
    for other in all_rice_names:
        if _expected_target(rice_home, other, config_link) == actual:
            return other
    return f"<unmanaged:{actual}>"


def cmd_status(args, roots: Roots, log) -> int:
    """Slice 2 real-time status. Per rice: state, owner (who the
    symlink points to), and PID probe per unique identity."""
    from .process import ProbeResult, probe_pids_by_identity
    rices = list_manifests(roots.rice_home)
    if not rices:
        print("(no rice)", file=sys.stderr)
        return 0
    state = read_state(roots)
    rice_names = [n for n, _ in rices]
    trust_names = set(state.trust.keys())

    # Manifests
    manifests: dict[str, object] = {}
    for name, path in rices:
        try:
            manifests[name] = load_manifest(path)
        except Exception as e:
            manifests[name] = e  # marker

    # Probe symlink + PID per unique config_link / identity
    unique_links: dict[str, None] = {}  # path -> None, just for ordering
    unique_idents: dict[tuple[str, tuple[str, ...]], None] = {}
    for name, m in manifests.items():
        if isinstance(m, Exception):
            continue
        for c in m.components:
            if c.config_link:
                link = os.path.abspath(os.path.expanduser(c.config_link))
                unique_links[link] = None
            ident = c.identity() if hasattr(c, "identity") else None
            if ident is not None:
                unique_idents[ident] = None

    link_probe: dict[str, tuple[bool, str | None]] = {}
    for link in unique_links:
        link_probe[link] = _probe_symlink(link)

    ident_probe: dict[tuple[str, tuple[str, ...]], ProbeResult] = {}
    for ident in unique_idents:
        ident_probe[ident] = probe_pids_by_identity(*ident)

    print(f"current: {state.current_rice or '(none)'}")
    print(f"{'NAME':24} {'STATE':18} {'OWNER':30} {'PIDS':16} {'DRIFT'}")
    print(f"{'----':24} {'-----':18} {'-----':30} {'----':16} {'-----'}")
    import datetime
    now = datetime.datetime.now()
    for name in rice_names:
        m = manifests.get(name)
        if isinstance(m, Exception):
            print(f"{name:24} {'load error':18} {'-':30} {'-':16} -")
            continue
        # STATE
        # Find the first config_link in the manifest; if multiple, use
        # the first; composite is out of scope for Slice 2.
        first_link = next((c.config_link for c in m.components if c.config_link), None)
        if first_link:
            state_label = _rice_status(
                name, first_link, state.current_rice or "",
                roots.rice_home, trust_names,
            )
            owner_label = _owner_label(roots.rice_home, rice_names, first_link)
        else:
            state_label = "no-link"
            owner_label = "-"
        # PIDS: first component with identity
        pids_label = "-"
        for c in m.components:
            ident = c.identity() if hasattr(c, "identity") else None
            if ident is None:
                continue
            pr = ident_probe.get(ident)
            if pr is None:
                continue
            if pr.status == "unknown":
                pids_label = f"{ident[0]}:?"
            elif pr.status == "running":
                pids_label = f"{ident[0]}:{','.join(str(p) for p in pr.pids[:3])}"
            else:
                pids_label = f"{ident[0]}:stopped"
            break
        # DRIFT: only for current/current-mismatch
        drift = ""
        if state_label in ("current-mismatch", "idle-foreign"):
            drift = "drift!"
        # Last-switched hint (compact, in last column after DRIFT)
        trust = state.trust.get(name)
        last = ""
        if trust and trust.last_switched:
            last = datetime.datetime.fromtimestamp(trust.last_switched).strftime("%Y-%m-%d %H:%M")
        print(f"{name:24} {state_label:18} {owner_label:30} {pids_label:16} {drift} {last}")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: switch
# ---------------------------------------------------------------------------

def _should_require_confirmation(roots: Roots, target_name: str,
                                 target_sha256: str) -> tuple[bool, str]:
    """Return (require_confirm, reason). Implements PLAN.md §2h: a rice
    requires confirmation when it has never been switched on this
    system, OR when the manifest hash differs from the cached trust
    record.

    Reason is empty when require_confirm is False.
    """
    state = read_state(roots)
    trust = state.trust.get(target_name)
    if trust is None:
        return True, "rice has never been switched on this system"
    if not trust.manifest_sha256:
        return True, "no manifest hash recorded for this rice"
    if trust.manifest_sha256 != target_sha256:
        return True, "manifest hash differs from the cached trust record"
    return False, ""


def _confirm_prompt(message: str) -> bool:
    """Print a confirmation prompt to stderr and read a line from stdin.

    Returns True if the user typed exactly "yes" (case-insensitive, with
    optional surrounding whitespace). Any other input (including EOF)
    returns False. We write the prompt to stderr so the plan output
    on stdout stays clean.
    """
    print(message, file=sys.stderr)
    print("Type 'yes' to continue, anything else to abort: ", file=sys.stderr, end="")
    sys.stderr.flush()
    try:
        line = sys.stdin.readline()
    except (EOFError, KeyboardInterrupt):
        return False
    return line.strip().lower() == "yes"


def cmd_switch(args, roots: Roots, log) -> int:
    rices_data = list_manifests(roots.rice_home)
    if not rices_data:
        print("error: no rice found; run `rice-switcher add <name>` first", file=sys.stderr)
        return 2
    manifests: list[Manifest] = []
    for name, path in rices_data:
        try:
            manifests.append(load_manifest(path))
        except Exception as e:
            print(f"warning: skipping {name} (parse error: {e})", file=sys.stderr)
    if args.name not in {m.meta.name for m in manifests}:
        print(f"error: unknown rice {args.name!r}", file=sys.stderr)
        return 2

    target_m = next(m for m in manifests if m.meta.name == args.name)

    dry_run = not args.no_dry_run

    # Audit fix S3: when the user opts into a real (non-dry-run) switch,
    # PLAN.md §2h requires an explicit "yes" confirmation unless the
    # rice is already trusted and the manifest hash matches.
    if not dry_run:
        require, reason = _should_require_confirmation(
            roots, target_m.meta.name, target_m.source_sha256
        )
        if require:
            print(
                f"warning: confirmation required: {reason}",
                file=sys.stderr,
            )
            # PLAN.md §2h: print the plan + all commands that will run
            # (with secret redaction; log module already redacts on
            # output). We show the same plan the dry-run prints so the
            # user can see what is about to happen.
            outcome_preview = do_switch(
                roots, args.name, manifests,
                dry_run=True, force_real=False, logger=log,
            )
            print(plan_str(outcome_preview.plan, roots), file=sys.stderr)
            if not _confirm_prompt(
                f"about to switch to {target_m.meta.name!r} (real execution)"
            ):
                print("aborted by user; no changes made", file=sys.stderr)
                return 1

    outcome = do_switch(
        roots, args.name, manifests,
        dry_run=dry_run,
        force_real=args.no_dry_run,
        logger=log,
    )
    print(textwrap.dedent(f"""
        {plan_str(outcome.plan, roots)}
        dry_run={outcome.dry_run}  snapshot={outcome.snapshot_ts}
        errors={outcome.errors}  warnings={outcome.warnings}
    """).strip())
    if outcome.errors:
        return 1
    return 0


def plan_str(plan, roots: Roots) -> str:
    from .switch import _print_plan
    return _print_plan(plan, roots)


# ---------------------------------------------------------------------------
# Subcommand: snapshot (manual)
# ---------------------------------------------------------------------------

def cmd_snapshot(args, roots: Roots, log) -> int:
    from .snapshot import create_pending, commit as commit_snapshot, discard_pending
    from .state import read_state
    state = read_state(roots)
    if not state.current_rice:
        print("error: no current rice; nothing to snapshot", file=sys.stderr)
        return 2
    rices = list_manifests(roots.rice_home)
    manifests = {n: load_manifest(p) for n, p in rices}
    target = manifests.get(state.current_rice)
    if target is None:
        print(f"error: state refers to {state.current_rice!r} but its manifest is gone",
              file=sys.stderr)
        return 2
    config_links = [c.config_link for c in target.components if c.config_link]
    try:
        with __import__("rice_switcher.lock", fromlist=["switch_lock"]).switch_lock(roots):
            ts, captured = create_pending(roots, config_links)
            commit_snapshot(roots, ts)
            from .snapshot import prune as prune_snapshots
            prune_snapshots(roots, keep=5)
    except LockError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"snapshot {ts} captured: {len(captured)} paths")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: rollback
# ---------------------------------------------------------------------------

def cmd_rollback(args, roots: Roots, log) -> int:
    ts, errors = do_rollback(roots, args.ts, log)
    if errors:
        for e in errors:
            print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"rolled back to snapshot {ts}")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: apply (Slice 3)
# ---------------------------------------------------------------------------

def cmd_apply(args, roots: Roots, log) -> int:
    """Restore the rice recorded in state.json. Invoked from
    `hl.on("hyprland.start", ...)` in the Hyprland config."""
    from .apply import do_apply
    res = do_apply(
        roots,
        require_session=bool(args.require_session),
        logger=log,
    )
    if res.status == "noop":
        print("no current rice; nothing to apply")
        return 0
    if res.status == "applied":
        print(f"applied: {res.rice}")
        if res.detail:
            print(res.detail)
        return 0
    if res.status == "missing_state":
        print(f"error: {res.detail}", file=sys.stderr)
        return 2
    if res.status == "rice_gone":
        print(f"error: {res.detail}", file=sys.stderr)
        return 2
    if res.status == "lock_busy":
        print(f"error: lock busy: {res.detail}", file=sys.stderr)
        return 1
    if res.status == "session_check_failed":
        print(f"error: {res.detail}", file=sys.stderr)
        return 2
    if res.status == "switch_failed":
        print(f"error: switch failed: {res.detail}", file=sys.stderr)
        return 1
    print(f"unknown status: {res.status}", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# Subcommand: import (Slice 4)
# ---------------------------------------------------------------------------

def cmd_import(args, roots: Roots, log) -> int:
    """Import an existing config directory as a new rice."""
    from .import_diff_edit import do_import as _do_import
    res = _do_import(
        roots,
        source=args.source,
        name=args.name,
        dry_run=bool(args.dry_run),
    )
    if res.status == "applied":
        print(res.detail)
        return 0
    if res.status == "dry_run":
        print(f"[dry-run] {res.detail}")
        return 0
    if res.status == "name_invalid":
        print(f"error: {res.detail}", file=sys.stderr)
        return 2
    if res.status == "name_taken":
        print(f"error: {res.detail}", file=sys.stderr)
        return 2
    if res.status == "source_missing":
        print(f"error: {res.detail}", file=sys.stderr)
        return 2
    if res.status == "source_no_manifest":
        print(f"error: {res.detail}", file=sys.stderr)
        return 2
    if res.status == "source_manifest_invalid":
        print(f"error: {res.detail}", file=sys.stderr)
        return 2
    if res.status in ("copy_failed", "io_error"):
        print(f"error: {res.detail}", file=sys.stderr)
        return 1
    print(f"unknown status: {res.status}", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# Subcommand: diff (Slice 4)
# ---------------------------------------------------------------------------

def cmd_diff(args, roots: Roots, log) -> int:
    """Show unified diff between two installed rice."""
    from .import_diff_edit import do_diff as _do_diff
    res = _do_diff(roots, args.name_a, args.name_b)
    if res.status == "same":
        return 0
    if res.status == "different":
        if res.diff_text:
            print(res.diff_text, end="")
        return 1
    if res.status == "invalid_name":
        print(f"error: invalid rice name: {res.name_a!r} or {res.name_b!r}", file=sys.stderr)
        return 2
    if res.status == "missing":
        if res.diff_text:
            print(res.diff_text, end="", file=sys.stderr)
        print("error: one or both rice are not installed", file=sys.stderr)
        return 2
    if res.status == "io_error":
        print(f"error: I/O error reading manifests: {res.diff_text}", file=sys.stderr)
        return 1
    print(f"unknown status: {res.status}", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# Subcommand: edit (Slice 4)
# ---------------------------------------------------------------------------

def cmd_edit(args, roots: Roots, log) -> int:
    """Open a rice's rice.toml in $EDITOR."""
    from .import_diff_edit import do_edit as _do_edit
    res = _do_edit(roots, args.name, editor=args.editor)
    if res.status == "ok":
        print(res.detail)
        return 0
    if res.status == "invalid_name":
        print(f"error: {res.detail}", file=sys.stderr)
        return 2
    if res.status == "missing":
        print(f"error: {res.detail}", file=sys.stderr)
        return 2
    if res.status == "not_a_rice":
        print(f"error: {res.detail}", file=sys.stderr)
        return 2
    if res.status == "no_editor":
        print(f"error: {res.detail}", file=sys.stderr)
        return 2
    if res.status == "editor_failed":
        print(f"error: {res.detail}", file=sys.stderr)
        return 1
    print(f"unknown status: {res.status}", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# Subcommand: menu (Slice 3)
# ---------------------------------------------------------------------------

def cmd_menu(args, roots: Roots, log) -> int:
    """TUI picker (fuzzel or rofi) to choose a rice and switch to it."""
    from .menu import do_menu
    res = do_menu(
        roots,
        picker_override=args.picker,
        dry_run=bool(args.dry_run),
        require_session=bool(args.require_session),
        logger=log,
    )
    if res.status == "ok":
        if res.selected:
            print(f"switched to {res.selected}")
        if res.detail:
            print(res.detail)
        return 0
    if res.status == "no_rice":
        print(f"error: {res.detail}", file=sys.stderr)
        return 0  # nothing to do
    if res.status == "no_picker":
        print(f"error: {res.detail}", file=sys.stderr)
        return 2
    if res.status == "cancelled":
        print("cancelled")
        return 0
    if res.status == "invalid_selection":
        print(f"error: {res.detail}", file=sys.stderr)
        return 2
    if res.status == "switch_failed":
        print(f"error: switch failed: {res.detail}", file=sys.stderr)
        return 1
    print(f"unknown status: {res.status}", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# Subcommand: keybind-install (Slice 3)
# ---------------------------------------------------------------------------

def cmd_keybind_install(args, roots: Roots, log) -> int:
    """Install the SUPER+SHIFT+R keybind into a Hyprland keybinds.lua."""
    from .keybind import install as keybind_install
    target = args.path or os.path.expanduser("~/.config/hypr/custom/keybinds.lua")
    try:
        res = keybind_install(
            roots,
            target_path=target,
            command=args.command,
            allow_any_path=bool(args.allow_any_path),
            dry_run=bool(args.dry_run),
        )
    except TimeoutError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"error: filesystem error: {e}", file=sys.stderr)
        return 1

    # Emit audit event for any non-dry-run outcome.
    if not res.result == "dry_run":
        from .log import append_audit
        try:
            append_audit(roots, {
                "action": "keybind_install",
                "path": res.path,
                "result": res.result,
                "detail": res.detail,
            })
        except Exception:
            # Audit failure must not block the operation.
            pass

    if res.result == "added":
        print(res.detail)
        return 0
    if res.result == "already_present":
        print(f"keybind already present in {res.path}")
        return 0
    if res.result == "conflict":
        print(f"conflict: {res.detail}", file=sys.stderr)
        return 2
    if res.result == "dry_run":
        print(f"[dry-run] would append to {res.path}:\n  {res.detail}")
        return 0
    if res.result == "error":
        print(f"error: {res.detail}", file=sys.stderr)
        return 2
    print(f"unknown result: {res.result}", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rice-switcher",
        description="Switch between Hyprland rice configurations.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="scaffold rice_home, state, and log directories")
    add_p = sub.add_parser("add", help="scaffold a new rice directory")
    add_p.add_argument("name")

    sub.add_parser("list", help="list all rice directories")
    sub.add_parser("current", help="print the current rice (from state.json)")
    val = sub.add_parser("validate", help="comprehensive validation of a rice manifest")
    val.add_argument("name")
    sub.add_parser("status", help="table of rice x component x last-switch")

    sw = sub.add_parser("switch", help="switch to a named rice")
    sw.add_argument("name")
    sw.add_argument("--dry-run", dest="dry_run", action="store_true", default=True,
                    help="print the plan, change nothing (default)")
    sw.add_argument("--no-dry-run", dest="no_dry_run", action="store_true",
                    help="actually perform the switch")

    sub.add_parser("snapshot", help="capture a manual snapshot of the current state")
    rb = sub.add_parser("rollback", help="rollback to a snapshot (default: latest)")
    rb.add_argument("ts", nargs="?", default=None)

    ap = sub.add_parser(
        "apply",
        help="restore the rice recorded in state.json (for hl.on hyprland.start)",
    )
    ap.add_argument("--require-session", dest="require_session",
                    action="store_true", default=False,
                    help="abort if no Hyprland session is reachable")

    im = sub.add_parser(
        "import",
        help="import an existing config directory as a new rice (read-only)",
    )
    im.add_argument("source",
                     help="path to the source config directory (must contain rice.toml)")
    im.add_argument("--name", default=None,
                    help="rice name (default: derive from [meta].name)")
    im.add_argument("--dry-run", dest="dry_run", action="store_true", default=True,
                    help="print the plan, copy nothing (default)")
    im.add_argument("--apply", dest="dry_run", action="store_false",
                    help="actually copy the files into rice_home")

    df = sub.add_parser(
        "diff",
        help="show unified diff between two installed rice (rice.toml)",
    )
    df.add_argument("name_a")
    df.add_argument("name_b")

    ed = sub.add_parser(
        "edit",
        help="open a rice's rice.toml in $EDITOR",
    )
    ed.add_argument("name")
    ed.add_argument("--editor", default=None,
                    help="override the editor command (default: $EDITOR or vi)")

    mn = sub.add_parser(
        "menu",
        help="TUI picker (fuzzel or rofi) to choose a rice and switch to it",
    )
    mn.add_argument("--picker", default=None,
                    help="picker binary to use (default: detect fuzzel, then rofi)")
    mn.add_argument("--dry-run", dest="dry_run", action="store_true", default=False,
                    help="print the picker menu without invoking it")
    mn.add_argument("--require-session", dest="require_session",
                    action="store_true", default=False,
                    help="abort if no Hyprland session is reachable")

    kb = sub.add_parser(
        "keybind-install",
        help="install SUPER+SHIFT+R keybind into ~/.config/hypr/custom/keybinds.lua",
    )
    kb.add_argument("--path", default=None,
                    help="target keybinds file (default: ~/.config/hypr/custom/keybinds.lua)")
    kb.add_argument("--command", default="rice-switcher apply",
                    help="command to invoke (default: rice-switcher apply)")
    kb.add_argument("--dry-run", dest="dry_run", action="store_true", default=False,
                    help="print the planned change without writing any file")
    kb.add_argument("--allow-any-path", dest="allow_any_path", action="store_true",
                    default=False,
                    help="bypass the ~/.config/hypr/ path-safety check")

    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    roots = resolve_roots()
    log = setup_logging(roots)

    table = {
        "init": cmd_init,
        "add": cmd_add,
        "list": cmd_list,
        "current": cmd_current,
        "status": cmd_status,
        "switch": cmd_switch,
        "snapshot": cmd_snapshot,
        "rollback": cmd_rollback,
        "validate": cmd_validate,
        "keybind-install": cmd_keybind_install,
        "apply": cmd_apply,
        "menu": cmd_menu,
        "import": cmd_import,
        "diff": cmd_diff,
        "edit": cmd_edit,
    }
    return table[args.cmd](args, roots, log)


if __name__ == "__main__":
    raise SystemExit(main())
