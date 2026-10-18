"""Top-level switch orchestrator.

PLAN.md §3.2 (pseudocode for `rice-switcher switch`). This module glues
together validate, managed_set, snapshot, lock, state, and the component
lifecycle (`lifecycle.py`). It performs the full PLAN §3.2 sequence:
``hooks.pre -> stop -> theme -> swap -> verify -> reload -> matugen ->
start -> hooks.post``.

The orchestrator respects:
  * `--dry-run` / `RICE_DRY_RUN=1`: print the plan, change nothing.
  * `RICE_TEST=1`: disable lifecycle execution so tests never spawn real
    commands. Tests that want it inject a fake `Runtime` and pass
    `manage_processes=True`.
  * Compositor invariant: never stops or starts the compositor.
  * Managed-set invariant: never touches components outside it.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass

from .executor import DEFAULT_TIMEOUT
from .lifecycle import (
    LifecycleError,
    Runtime,
    apply_theme,
    find_component,
    hyprland_verify,
    reload_component,
    run_hooks,
    run_wallpaper,
    start_component,
    stop_component,
)
from .lock import LockError, switch_lock
from .log import append_audit, setup_logging
from .managed_set import Identity, SwitchPlan, compute_plan
from .manifest import Component, Manifest
from .paths import (
    Roots,
    audit_log_file,
    manifest_path,
    state_file,
)
from .snapshot import (
    SnapshotInfo,
    commit as commit_snapshot,
    create_pending,
    current_symlinks_state,
    discard_pending,
    prune as prune_snapshots,
    restore as restore_snapshot,
    swap_symlink,
)
from .state import State, mark_trust, read_state, write_state
from .validate import ValidationError, ValidationResult, validate_manifest


@dataclass
class SwitchOutcome:
    plan: SwitchPlan
    snapshot_ts: str | None
    dry_run: bool
    errors: list[str]
    warnings: list[str]


# ---------------------------------------------------------------------------
# Sub-step helpers
# ---------------------------------------------------------------------------

def _gather_config_links(manifest: Manifest) -> list[str]:
    # Normalise to absolute paths: manifests may write `~/.config/hypr`,
    # and both the snapshot and the rollback must operate on the real
    # location. `os.path.lexists("~/.config/hypr")` is False, so leaving
    # the tilde would make the snapshot silently empty (data-loss on
    # rollback).
    return [
        os.path.abspath(os.path.expanduser(c.config_link))
        for c in manifest.components
        if c.config_link
    ]


def stop_targets(plan: SwitchPlan) -> list[str]:
    """Human-readable list of the processes the plan will stop.

    PLAN.md §2h requires the confirmation view to show what will be
    stopped. Kill-by-identity has no command string, so we render the
    identity (`executable argv_match`) instead of a shell command.
    """
    return [str(ident) for ident in plan.to_stop]


def _print_plan(plan: SwitchPlan, roots: Roots) -> str:
    lines: list[str] = []
    lines.append(f"Switch plan: {plan.current or '<none>'} -> {plan.target}")
    lines.append(f"  to_stop ({len(plan.to_stop)}):")
    for i in plan.to_stop:
        lines.append(f"    - {i}")
    lines.append(f"  to_start ({len(plan.to_start)}):")
    for i in plan.to_start:
        lines.append(f"    + {i}")
    lines.append(f"  to_reload ({len(plan.to_reload)}):")
    for i in plan.to_reload:
        lines.append(f"    ~ {i}")
    lines.append(f"  to_keep ({len(plan.to_keep)}):")
    for i in plan.to_keep:
        lines.append(f"    = {i}")
    lines.append(f"  to_swap ({len(plan.to_swap)}):")
    for p in plan.to_swap:
        lines.append(f"    @ {p}")
    lines.append(f"  to_config ({len(plan.to_config)}):  # dijalankan oleh config rice")
    for i in plan.to_config:
        lines.append(f"    c {i}")
    for n in plan.notes:
        lines.append(f"  note: {n}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

def resolve_manage_processes(value: bool | None) -> bool:
    """Decide whether lifecycle steps run.

    Explicit True/False wins. When None (the default), process management
    is on unless ``RICE_TEST=1`` is set, so the test suite stays hermetic
    without every test having to pass the flag (PLAN.md §3.5).
    """
    if value is not None:
        return value
    return os.environ.get("RICE_TEST") != "1"


def do_switch(
    roots: Roots,
    target_name: str,
    all_manifests: list[Manifest],
    *,
    dry_run: bool = True,
    force_real: bool = False,
    logger=None,
    manage_processes: bool | None = None,
    runtime: Runtime | None = None,
) -> SwitchOutcome:
    """Plan and (optionally) execute a switch.

    Parameters
    ----------
    roots : resolved filesystem roots (see `paths.resolve_roots`).
    target_name : name of the rice to switch into.
    all_manifests : every manifest the switcher knows about. The
        managed set is computed from this list.
    dry_run : if True (default), print the plan and return without
        touching the filesystem. Caller may pass --no-dry-run to
        override; we also honour the RICE_DRY_RUN env var.
    force_real : when True, real execution is allowed. The flag's job
        is to require an explicit opt-in.
    logger : optional logger; if absent, a default one is set up.
    manage_processes : run the lifecycle steps (hooks, stop, theme,
        reload, matugen, start). When None (default), it is enabled
        unless ``RICE_TEST=1`` is set, so the existing test suite stays
        hermetic. Pass True/False to force it.
    runtime : the execution environment (see ``lifecycle.Runtime``).
        Injected by tests; defaults to the real subprocess/proc one.
    """
    if logger is None:
        logger = setup_logging(roots)

    # Force dry-run if env var is set.
    if os.environ.get("RICE_DRY_RUN") == "1":
        dry_run = True

    # Lifecycle execution is on by default; RICE_TEST=1 disables it so
    # tests never spawn real commands unless they inject a runtime.
    manage_processes = resolve_manage_processes(manage_processes)
    if runtime is None:
        runtime = Runtime()

    errors: list[str] = []
    warnings: list[str] = []

    # 1. Find the target manifest
    target_m = next((m for m in all_manifests if m.meta.name == target_name), None)
    if target_m is None:
        return SwitchOutcome(
            plan=SwitchPlan(target=target_name, current=None),
            snapshot_ts=None, dry_run=dry_run,
            errors=[f"unknown rice: {target_name!r}"], warnings=[],
        )

    # 2. Load state and identify current rice
    state = read_state(roots)
    current_m = next((m for m in all_manifests if m.meta.name == state.current_rice), None) if state.current_rice else None

    # 3. Validate the target manifest
    vres = validate_manifest(target_m, roots)
    if not vres.ok:
        errs = [f"validate({target_name}): {e}" for e in vres.errors]
        return SwitchOutcome(
            plan=SwitchPlan(target=target_name, current=state.current_rice),
            snapshot_ts=None, dry_run=dry_run,
            errors=errs, warnings=vres.warnings,
        )
    warnings.extend(vres.warnings)

    # 4. If the same rice is requested, plan is a no-op (PLAN.md §3.2 #6)
    if state.current_rice == target_name:
        plan = compute_plan(current_m, target_m, all_manifests, {}, roots.config_home,
                            active_identities=set())
        return SwitchOutcome(plan=plan, snapshot_ts=None, dry_run=True,
                            errors=[], warnings=warnings + ["no-op: already on this rice"])

    # 5. Compute the plan
    sym_state = current_symlinks_state(roots, _gather_config_links(target_m))
    plan = compute_plan(
        current_m, target_m, all_manifests,
        sym_state, roots.config_home,
    )

    # 6. Dry-run: print and return
    if dry_run:
        logger.info("dry-run: %s", _print_plan(plan, roots))
        return SwitchOutcome(plan=plan, snapshot_ts=None, dry_run=True,
                            errors=[], warnings=warnings)

    # 7. Acquire lock (5s timeout)
    try:
        with switch_lock(roots):
            # 8. Capture pre-swap snapshot (pending -> commit on success)
            config_links = _gather_config_links(target_m)
            # Audit fix S4: record the rice name that was CURRENT
            # before this switch. On rollback, we restore to that rice
            # (the one whose state we just captured). The target name
            # would be the WRONG choice because the snapshot contains
            # the pre-swap state, which belongs to the current rice.
            rollback_rice_name = state.current_rice or ""
            ts, captured = create_pending(
                roots, config_links, rice_name=rollback_rice_name,
            )
            logger.info("snapshot pending ts=%s captured=%d", ts, len(captured))

            started: list[Component] = []
            stopped: list[Component] = []
            # Track whether we reloaded Hyprland, so a rollback can reload
            # the restored config back into the running compositor.
            hypr_reloaded = False
            hypr_reload_cmd = ""

            def _audit(kind: str, name: str, command: str, res) -> None:
                append_audit(roots, {
                    "action": "exec",
                    "kind": kind,
                    "component": name or None,
                    "command": command,
                    "rc": None if res is None else res.rc,
                    "timed_out": None if res is None else res.timed_out,
                })

            try:
                # Lifecycle (PLAN.md §3.2 #5-#7): pre-hooks, stop old
                # components, apply theme. All skipped when process
                # management is off (e.g. RICE_TEST=1).
                if manage_processes:
                    run_hooks(runtime, target_m.hooks_pre, phase="pre", audit=_audit)
                    for ident in plan.to_stop:
                        c = find_component(ident, all_manifests)
                        if c is None:
                            continue
                        stop_component(runtime, c, audit=_audit)
                        stopped.append(c)
                    apply_theme(runtime, target_m.theme, audit=_audit)

                # 9. Swap symlinks (PLAN.md §3.2 #8). This is the only
                # filesystem change: the live config_link is pointed at
                # <rice_home>/<name>/<basename(config_link)>.
                for c in target_m.components:
                    if c.config_link:
                        n = os.path.abspath(os.path.expanduser(c.config_link))
                        rice_dir = os.path.dirname(os.path.dirname(target_m.source_path))
                        new_target = os.path.join(rice_dir, target_name,
                                                  os.path.basename(n))
                        backup = swap_symlink(n, new_target, roots=roots)

                # Lifecycle (PLAN.md §3.2 #9-#12): verify, reload,
                # matugen, start, post-hooks.
                if manage_processes:
                    hypr_swapped = any(
                        c.name == "hyprland" and c.config_link
                        for c in target_m.components
                    )
                    if hypr_swapped and not hyprland_verify(runtime, audit=_audit):
                        raise LifecycleError("hyprland --verify-config failed after swap")
                    reloaded_hypr = False
                    for ident in plan.to_reload:
                        c = find_component(ident, all_manifests)
                        if c is not None:
                            reload_component(runtime, c, audit=_audit)
                            if c.name == "hyprland" and c.reload:
                                reloaded_hypr = True
                                hypr_reloaded = True
                                hypr_reload_cmd = c.reload
                    # Reload Hyprland when its config was swapped and the
                    # component did not already carry a reload command.
                    if hypr_swapped and not reloaded_hypr:
                        hypr = next(c for c in target_m.components if c.name == "hyprland")
                        reload_cmd = hypr.reload or "hyprctl reload"
                        res = runtime.run(reload_cmd, timeout=float(hypr.timeout))
                        _audit("reload", "hyprland", reload_cmd, res)
                        hypr_reloaded = True
                        hypr_reload_cmd = reload_cmd
                    run_wallpaper(runtime, target_m.wallpaper, audit=_audit)
                    for ident in plan.to_start:
                        c = find_component(ident, all_manifests)
                        if c is None:
                            continue
                        start_component(runtime, c, audit=_audit)
                        started.append(c)
                    run_hooks(runtime, target_m.hooks_post, phase="post", audit=_audit)

                # 10. Commit the pending snapshot
                commit_snapshot(roots, ts)
                # 11. Prune old snapshots (keep 5)
                prune_snapshots(roots, keep=5)
                # 12. Update state (current_rice + trust cache)
                st = read_state(roots)
                st.current_rice = target_name
                write_state(roots, st)
                mark_trust(roots, target_name, target_m.source_sha256, status="ok")
                # 13. Audit (Slice 2: JSON Lines dict event)
                append_audit(roots, {
                    "action": "switch",
                    "from": state.current_rice or None,
                    "to": target_name,
                    "snapshot": ts,
                    "plan": {
                        "stop": len(plan.to_stop),
                        "start": len(plan.to_start),
                        "swap": len(plan.to_swap),
                        "reload": len(plan.to_reload),
                    },
                })
                logger.info("switched to %s (snapshot=%s)", target_name, ts)
            except Exception as e:
                # Rollback: processes first (best-effort, PLAN.md §3.3),
                # then the filesystem, then reload Hyprland so the
                # running compositor drops the failed target's config.
                logger.error("switch failed: %s; rolling back", e)
                if manage_processes:
                    for c in reversed(started):
                        try:
                            stop_component(runtime, c)
                        except Exception:
                            pass
                    for c in reversed(stopped):
                        try:
                            start_component(runtime, c, require_running=False)
                        except Exception:
                            pass
                # Restore each config_link. Prefer undoing the exact swap
                # (the `.bak-<ts>` made by swap_symlink); fall back to the
                # pending snapshot when there is no backup (the link did
                # not exist before the swap).
                from .snapshot import _copy_tree_symlinks, undo_swap
                for cl in config_links:
                    n = os.path.abspath(os.path.expanduser(cl))
                    if undo_swap(n, roots=roots):
                        continue
                    name = os.path.basename(n.rstrip("/")) or "config"
                    src = os.path.join(roots.state_home, "rice-switcher",
                                       "snapshot", f".pending-{ts}", name)
                    if os.path.lexists(src):
                        if os.path.lexists(n):
                            if os.path.islink(n) or os.path.isfile(n):
                                os.unlink(n)
                            else:
                                shutil.rmtree(n)
                        _copy_tree_symlinks(src, n)
                # If the failed switch had reloaded Hyprland, reload the
                # restored config back in; otherwise the compositor keeps
                # running the target's (rolled-back) config in memory.
                if manage_processes and hypr_reloaded:
                    cmd = hypr_reload_cmd or "hyprctl reload"
                    try:
                        res = runtime.run(cmd, timeout=DEFAULT_TIMEOUT)
                        _audit("reload.rollback", "hyprland", cmd, res)
                    except Exception:
                        pass
                discard_pending(roots, ts)
                mark_trust(roots, target_name, target_m.source_sha256, status="rolled_back")
                errors.append(f"switch failed and rolled back: {e}")
                return SwitchOutcome(plan=plan, snapshot_ts=None, dry_run=False,
                                    errors=errors, warnings=warnings)
    except LockError as e:
        errors.append(f"lock error: {e}")
        return SwitchOutcome(plan=plan, snapshot_ts=None, dry_run=False,
                            errors=errors, warnings=warnings)

    return SwitchOutcome(plan=plan, snapshot_ts=ts, dry_run=False,
                        errors=[], warnings=warnings)


def _default_hypr_reload():
    """Best-effort `hyprctl reload` used after a rollback restores the
    compositor config. Imported lazily so tests can inject a no-op."""
    from .executor import run_argv
    return run_argv(["hyprctl", "reload"], timeout=DEFAULT_TIMEOUT)


def _reconcile_processes(runtime, current_m, prev_m, logger, audit) -> None:
    """Bring the running processes in line with a restored rice.

    Stops what the rolled-back (*current*) rice owned and starts what the
    restored (*prev*) rice needs. Mirrors ``compute_plan``'s rules but
    tolerates a missing manifest on either side (e.g. rolling back to
    "no rice"). Best-effort: individual failures are logged, not raised.
    """
    from .managed_set import Identity

    def _comps(m) -> dict:
        if m is None:
            return {}
        return {Identity.from_component(c): c for c in m.components}

    cur = _comps(current_m)
    prev = _comps(prev_m)

    def _is_compositor(c) -> bool:
        return c is not None and c.type == "compositor"

    stopped = started = 0
    for ident in sorted(set(cur) - set(prev), key=str):
        c = cur[ident]
        if _is_compositor(c):
            continue
        try:
            stop_component(runtime, c, audit=audit)
            stopped += 1
        except Exception as exc:
            logger.warning("rollback: could not stop %s: %s", c.name, exc)
    for ident in sorted(set(prev) - set(cur), key=str):
        c = prev[ident]
        if _is_compositor(c):
            continue
        try:
            start_component(runtime, c, require_running=False, audit=audit)
            started += 1
        except Exception as exc:
            logger.warning("rollback: could not start %s: %s", c.name, exc)
    logger.info("rollback process reconciliation: stopped=%d started=%d", stopped, started)


def do_rollback(roots: Roots, ts: str | None = None, logger=None, *,
                reload_fn=None, runtime=None,
                all_manifests=None) -> tuple[str, list[str]]:
    """Roll back to a specific (or the latest) committed snapshot.

    When ``runtime`` and ``all_manifests`` are supplied, the component
    processes are reconciled too: components the rolled-back (target)
    rice started are stopped and components the restored rice needs are
    started (best-effort), mirroring the failed-switch rollback.
    """
    if logger is None:
        logger = setup_logging(roots)
    from .snapshot import list_committed, latest_snapshot_link
    if ts is None:
        # Use the latest symlink
        if os.path.islink(latest_snapshot_link(roots)):
            ts = os.readlink(latest_snapshot_link(roots))
        else:
            committed = list_committed(roots)
            if not committed:
                return "", ["no snapshot to roll back to"]
            ts = committed[0]
    errors: list[str] = []
    try:
        with switch_lock(roots):
            target_rice = read_state(roots).current_rice
            restored, rice_name = restore_snapshot(roots, ts)
            logger.info("rolled back to snapshot %s; restored %d paths", ts, len(restored))

            def _audit(kind, name, command, res):
                append_audit(roots, {
                    "action": "exec",
                    "kind": kind,
                    "component": name or None,
                    "command": command,
                    "rc": None if res is None else res.rc,
                    "timed_out": None if res is None else res.timed_out,
                })

            # Reconcile processes with the restored rice (best-effort).
            if runtime is not None and all_manifests is not None:
                prev_m = next((m for m in all_manifests if m.meta.name == rice_name), None)
                cur_m = next((m for m in all_manifests if m.meta.name == target_rice), None)
                _reconcile_processes(runtime, cur_m, prev_m, logger, _audit)

            # Audit fix S4: keep `state.current_rice` in sync with the
            # rice that was actually restored. A snapshot whose
            # `.captured.txt` has no `rice=` line at all (an older
            # snapshot) is reported as an error rather than guessed; an
            # empty-but-present `rice=` means "no rice" and is valid.
            if rice_name is not None:
                st = read_state(roots)
                st.current_rice = rice_name
                write_state(roots, st)
                logger.info("state.current_rice restored to %r", rice_name)
            else:
                errors.append(
                    f"snapshot {ts} has no rice= line in .captured.txt; "
                    "state.current_rice was NOT updated. Inspect and "
                    "adjust manually if needed."
                )
            # If the compositor config was restored, reload it: restoring
            # the files alone leaves the running compositor on the old
            # (target) config (same class of bug as the failed-switch
            # rollback).
            reloaded = False
            if any(os.path.basename(p.rstrip("/")) in ("hypr", "hyprland")
                   for p in restored):
                fn = reload_fn or _default_hypr_reload
                try:
                    fn()
                    reloaded = True
                except Exception as exc:  # best-effort
                    logger.warning("compositor reload after rollback failed: %s", exc)
            append_audit(roots, {
                "action": "rollback",
                "snapshot": ts,
                "restored_count": len(restored),
                "rice": rice_name,
                "compositor_reload": reloaded,
                "errors": errors,
            })
            return ts, errors
    except LockError as e:
        return "", [f"lock error: {e}"]
    except FileNotFoundError as e:
        return "", [str(e)]
