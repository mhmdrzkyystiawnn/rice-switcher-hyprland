"""`rice-switcher menu` (Slice 3).

PLAN §6.3, line 1220: "TUI picker — `fuzzel` atau `rofi -dmenu` sesuai
rice". For Slice 3 we pick a simpler, testable approach:

- list installed rice (from `list_manifests(roots.rice_home)`)
- spawn a TUI picker subprocess (`fuzzel` or `rofi -dmenu`) with the
  rice names on stdin; the picker writes the selected rice name to
  stdout
- if the user picks a name, invoke the switcher orchestrator for
  that rice (same code path as `cmd_switch` with --no-dry-run)
- if the user cancels (empty stdout), exit 0 with no changes

We never invoke the picker on the live desktop in this module. The
implementation accepts a `subprocess_runner` callable so tests can
inject a fake that returns a deterministic selection. The default
runner is `subprocess.run` with a 30-second timeout.

Picker detection order (overridable via `--picker`):
1. explicit `--picker <name>` if provided
2. `fuzzel` if available
3. `rofi -dmenu` if available
4. exit 2 if neither is available

Failure modes that are NOT crashes:
- no rice installed: print a helpful message and exit 0
- picker binary missing: print diagnostic and exit 2
- user cancels (empty stdout): exit 0
- switch fails: surface the error from the orchestrator and exit 1
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, Literal

from .log import append_audit
from .manifest import list_manifests
from .paths import Roots
from .switch import SwitchOutcome, do_switch


# Type alias for the injectable subprocess runner.
# Signature: (argv, stdin_str, timeout_seconds) -> (rc, stdout_str)
SubprocessRunner = Callable[
    [list[str], str, float],
    tuple[int, str],
]


def _default_runner(argv: list[str], stdin: str, timeout: float) -> tuple[int, str]:
    """Default subprocess runner using `subprocess.run`. Returns
    `(returncode, stdout)`. stderr is folded into the result on
    failure for diagnostics."""
    try:
        proc = subprocess.run(
            argv, input=stdin, capture_output=True,
            text=True, timeout=timeout, check=False,
        )
    except FileNotFoundError as e:
        return 127, str(e)
    except subprocess.TimeoutExpired as e:
        return 124, f"timeout: {e}"
    except OSError as e:
        return 126, str(e)
    return proc.returncode, proc.stdout or ""


# ---------------------------------------------------------------------------
# Picker detection
# ---------------------------------------------------------------------------

def _detect_picker(override: str | None) -> list[str] | None:
    """Return the argv list to invoke the picker, or None if no picker
    is available. `override` is a literal binary name from the CLI
    flag."""
    if override:
        if not shutil.which(override):
            return None
        if override == "rofi":
            return ["rofi", "-dmenu", "-p", "rice"]
        if override == "fuzzel":
            return ["fuzzel", "--prompt=rice"]
        # Unknown picker name: still try to invoke it as-is, with a
        # generic prompt via --prompt flag (best-effort).
        return [override]
    if shutil.which("fuzzel"):
        return ["fuzzel", "--prompt=rice"]
    if shutil.which("rofi"):
        return ["rofi", "-dmenu", "-p", "rice"]
    return None


# ---------------------------------------------------------------------------
# Core: list, prompt, switch
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MenuResult:
    status: Literal["ok", "noop", "no_rice", "no_picker", "cancelled",
                     "switch_failed", "invalid_selection"]
    selected: str = ""
    detail: str = ""


def list_rice_names(roots: Roots) -> list[str]:
    """Return the sorted list of installed rice names."""
    return sorted(name for name, _ in list_manifests(roots.rice_home))


def run_picker(
    runner: SubprocessRunner,
    picker_argv: list[str],
    rice_names: list[str],
    timeout: float = 30.0,
) -> str:
    """Run the picker subprocess and return the selected rice name (or
    the empty string on cancel). `runner` is injectable for tests."""
    stdin = "\n".join(rice_names) + "\n"
    rc, stdout = runner(picker_argv, stdin, timeout)
    if rc != 0:
        # fuzzel/rofi may return non-zero on cancel; treat as cancel
        # unless it's a hard error (rc=124/126/127).
        if rc in (124, 126, 127):
            raise RuntimeError(
                f"picker failed: rc={rc} argv={picker_argv}"
            )
        return ""
    return stdout.strip()


def do_menu(
    roots: Roots,
    *,
    picker_override: str | None = None,
    dry_run: bool = False,
    require_session: bool = False,
    runner: SubprocessRunner | None = None,
    logger=None,
) -> MenuResult:
    """Run the menu picker and (unless cancelled or dry-run) apply
    the selected rice."""
    if runner is None:
        runner = _default_runner

    names = list_rice_names(roots)
    if not names:
        return MenuResult(status="no_rice", detail="no rice installed; run `rice-switcher add <name>` first")

    if dry_run:
        # Just show what would be presented
        return MenuResult(
            status="ok", selected="", detail=(
                f"[dry-run] would present {len(names)} rice to picker: "
                + ", ".join(names)
            ),
        )

    picker_argv = _detect_picker(picker_override)
    if picker_argv is None:
        return MenuResult(
            status="no_picker",
            detail="no picker available; install `fuzzel` or `rofi`, or pass --picker <name>",
        )

    try:
        selected = run_picker(runner, picker_argv, names)
    except RuntimeError as e:
        return MenuResult(status="no_picker", detail=str(e))

    if not selected:
        return MenuResult(status="cancelled", detail="no rice selected")

    if selected not in names:
        return MenuResult(
            status="invalid_selection",
            selected=selected,
            detail=f"picker returned {selected!r}, which is not an installed rice",
        )

    # Load manifests and invoke the orchestrator.
    from .manifest import load_manifest
    manifests = []
    for name, path in list_manifests(roots.rice_home):
        try:
            manifests.append(load_manifest(path))
        except Exception:
            continue

    outcome: SwitchOutcome = do_switch(
        roots, selected, manifests,
        dry_run=False, force_real=True, logger=logger,
    )

    try:
        append_audit(roots, {
            "action": "menu",
            "to": selected,
            "snapshot": outcome.snapshot_ts,
            "errors": outcome.errors,
            "warnings": outcome.warnings,
        })
    except Exception:
        pass

    if outcome.errors:
        return MenuResult(
            status="switch_failed", selected=selected,
            detail="; ".join(outcome.errors),
        )
    return MenuResult(
        status="ok", selected=selected,
        detail=f"switched; plan: stop={len(outcome.plan.to_stop)} "
               f"start={len(outcome.plan.to_start)} "
               f"swap={len(outcome.plan.to_swap)} "
               f"reload={len(outcome.plan.to_reload)}",
    )
