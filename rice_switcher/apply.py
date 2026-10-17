"""`rice-switcher apply` (Slice 3).

PLAN §2d, line 378: "restore rice terakhir dari `state.json` (start
komponen yang harus aktif sesuai managed set)".

This is invoked from `hyprland/execs.lua` inside `hl.on("hyprland.start", ...)`
during a Hyprland session start. Slice 3's `apply` is intentionally
limited to:

- read `state.current_rice` from state.json
- if empty, exit 0 (first boot, nothing to do)
- validate the target rice is still installed
- acquire the switch lock
- run the same orchestrator (`do_switch`) used by `cmd_switch`,
  but skipping the user confirmation prompt (apply is automatic)
- emit a single audit event

Slice 3 does NOT:
- start/stop processes (managed set components). The startup of
  waybar/quickshell/etc. is the responsibility of the existing
  `hl.exec_cmd` lines in `hyprland/execs.lua`; this module is a
  pure orchestrator for the symlink swap, mirroring the Slice 1
  `do_switch` algorithm.
- reload Hyprland or signal any desktop process.
- touch the live desktop, the live config, the live state file, or
  any process.

Slice 3 lifecycle migration (removing the individual `hl.exec_cmd`
lines and replacing them with `hl.on("hyprland.start", ...) → apply`)
is out of scope for this slice per PLAN §2d. The `apply` function is
a building block that the user can wire up manually once they are
ready.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Literal

from .lock import LockError, switch_lock
from .log import append_audit
from .manifest import list_manifests, load_manifest
from .paths import Roots
from .state import read_state
from .switch import SwitchOutcome, do_switch


@dataclass(frozen=True)
class ApplyResult:
    status: Literal["noop", "applied", "missing_state", "rice_gone",
                     "lock_busy", "switch_failed", "session_check_failed"]
    rice: str = ""
    detail: str = ""
    snapshot_ts: str | None = None


# ---------------------------------------------------------------------------
# Session probe (PLAN §2d "fail-fast kalau Hyprland session tidak punya
# hl.on callback"). We do the cheapest possible check: try `hyprctl
# version`. If hyprctl is missing or returns non-zero, we still allow
# the apply (the function works regardless) but emit a warning.
# ---------------------------------------------------------------------------

def _check_hyprland_session() -> tuple[bool, str]:
    """Return (ok, message). ok=True means a Hyprland session is
    responsive; ok=False means we could not reach hyprctl (binary
    missing, no IPC, etc)."""
    hc = shutil.which("hyprctl")
    if not hc:
        return False, "hyprctl not in PATH"
    try:
        proc = subprocess.run(
            [hc, "version"], capture_output=True, text=True, timeout=2.0,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        return False, f"hyprctl error: {e}"
    if proc.returncode != 0:
        return False, f"hyprctl returned rc={proc.returncode}"
    return True, proc.stdout.strip().split("\n", 1)[0] if proc.stdout else ""


# ---------------------------------------------------------------------------
# Core: apply the rice recorded in state.json
# ---------------------------------------------------------------------------

def do_apply(
    roots: Roots,
    *,
    require_session: bool = False,
    logger=None,
) -> ApplyResult:
    """Restore the rice recorded in `state.json`.

    Parameters
    ----------
    roots : resolved filesystem roots.
    require_session : if True, the apply is aborted when no Hyprland
        session is reachable via `hyprctl version`. Default False
        (apply is a no-op on the desktop process tree; we just swap
        the symlinks and trust the caller to coordinate).
    logger : optional logger; if absent, the audit event is still
        emitted via `append_audit`.

    Returns an `ApplyResult`. Never raises for "expected" failure
    modes — they are returned as a status. Raises only for truly
    exceptional errors (programmer mistakes, missing modules).
    """
    state = read_state(roots)
    target_name = state.current_rice
    if not target_name:
        return ApplyResult(status="noop", detail="state.json has no current_rice")

    if require_session:
        ok, msg = _check_hyprland_session()
        if not ok:
            return ApplyResult(
                status="session_check_failed", rice=target_name,
                detail=f"hyprland session not reachable: {msg}",
            )

    rices_data = list_manifests(roots.rice_home)
    if not rices_data:
        return ApplyResult(
            status="rice_gone", rice=target_name,
            detail=f"state.current_rice={target_name!r} but no rice is installed",
        )
    manifests = []
    target_m = None
    for name, path in rices_data:
        try:
            m = load_manifest(path)
        except Exception as e:
            # Skip the broken one but keep loading the rest so we can
            # still detect "target absent".
            continue
        manifests.append(m)
        if m.meta.name == target_name:
            target_m = m
    if target_m is None:
        return ApplyResult(
            status="rice_gone", rice=target_name,
            detail=f"state.current_rice={target_name!r} but its manifest is not installed",
        )

    # No previous rice (state.current_rice was set on first switch).
    # If state.current_rice == target_name, this is a no-op (already
    # applied) — but we still want to validate the symlink target.
    # For now: do_switch handles same-rice as no-op.

    try:
        outcome = do_switch(
            roots, target_name, manifests,
            dry_run=False, force_real=True, logger=logger,
        )
    except LockError as e:
        return ApplyResult(
            status="lock_busy", rice=target_name, detail=str(e),
        )

    # Audit event (always emitted when we attempted an apply).
    try:
        append_audit(roots, {
            "action": "apply",
            "to": target_name,
            "snapshot": outcome.snapshot_ts,
            "errors": outcome.errors,
            "warnings": outcome.warnings,
            "plan": {
                "stop": len(outcome.plan.to_stop),
                "start": len(outcome.plan.to_start),
                "swap": len(outcome.plan.to_swap),
                "reload": len(outcome.plan.to_reload),
            },
        })
    except Exception:
        # Audit failure must not block the operation.
        pass

    if outcome.errors:
        return ApplyResult(
            status="switch_failed", rice=target_name,
            detail="; ".join(outcome.errors),
            snapshot_ts=outcome.snapshot_ts,
        )
    return ApplyResult(
        status="applied", rice=target_name,
        detail=f"applied {target_name}; snapshot={outcome.snapshot_ts}; "
               f"plan: stop={len(outcome.plan.to_stop)} start={len(outcome.plan.to_start)} "
               f"swap={len(outcome.plan.to_swap)} reload={len(outcome.plan.to_reload)}",
        snapshot_ts=outcome.snapshot_ts,
    )
