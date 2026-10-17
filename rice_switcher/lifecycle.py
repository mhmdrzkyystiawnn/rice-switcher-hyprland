"""Component lifecycle execution for `switch` (PLAN.md §3.2).

`switch.do_switch` delegates every external action here: hooks, stop,
reload, start, theme, matugen. Everything goes through a `Runtime`, which
bundles the command runner, the process probe, the sleeper and the clock
so tests can drive the whole flow deterministically without touching the
desktop (PLAN.md §3.5 intends the same via PATH stubs; injectable seams
are the approach the rest of this codebase already uses).

Ordering and failure handling follow PLAN.md §3.2 / §3.3:
  hooks.pre -> stop -> theme -> (swap happens in switch) -> reload ->
  matugen -> start -> hooks.post
A failed non-optional step raises `LifecycleError`; the caller rolls back.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from .executor import (
    DEFAULT_TIMEOUT,
    HOOK_TIMEOUT,
    MATUGEN_TIMEOUT,
    ExecResult,
    default_runner,
    default_spawner,
    run_argv,
    run_command,
    run_hook,
    spawn_command,
)
from .managed_set import Identity
from .manifest import Component, Manifest, Theme
from .process import probe_pids_by_identity, terminate_by_identity


class LifecycleError(RuntimeError):
    """A lifecycle step failed and the caller must roll back."""


AuditFn = Callable[[str, str, str, "ExecResult | None"], None]


@dataclass
class Runtime:
    """Injectable execution environment.

    runner : `(argv, timeout) -> ExecResult`. Defaults to subprocess.
    probe  : `(Component) -> bool` (is it running?). If None, uses the
             component's `start_check` or a /proc identity scan.
    sleeper / clock : injected so tests do not really sleep.
    """

    runner: Callable[..., ExecResult] = default_runner
    spawner: Callable[..., ExecResult] = default_spawner
    probe: Callable[[Component], bool] | None = None
    sleeper: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    poll_interval: float = 0.2

    def run(self, command: str, *, timeout: float = DEFAULT_TIMEOUT) -> ExecResult:
        return run_command(command, timeout=timeout, runner=self.runner)

    def spawn(self, command: str, *, timeout: float = DEFAULT_TIMEOUT) -> ExecResult:
        """Start `command` detached (does not wait for it to exit)."""
        return spawn_command(command, timeout=timeout, spawner=self.spawner)

    def argv(self, argv: Sequence[str], *, timeout: float = DEFAULT_TIMEOUT) -> ExecResult:
        return run_argv(list(argv), timeout=timeout, runner=self.runner)

    def hook(self, command: str, *, timeout: float = HOOK_TIMEOUT) -> ExecResult:
        return run_hook(command, timeout=timeout, runner=self.runner)

    def is_running(self, c: Component) -> bool:
        if self.probe is not None:
            return bool(self.probe(c))
        if c.start_check:
            return self.run(c.start_check).ok
        res = probe_pids_by_identity(c.executable or c.name, c.argv_match)
        return res.status == "running"

    def kill_processes(self, c: Component) -> list[int]:
        """Stop `c` by process identity (Prasyarat 0).

        Used when a component declares no `stop` command: find its
        processes by `(executable, argv_match)`, re-check ownership and
        identity, then SIGTERM -> wait -> SIGKILL. See
        `process.terminate_by_identity` for the guards.
        """
        return terminate_by_identity(
            c.executable or c.name,
            tuple(c.argv_match),
            timeout=float(c.timeout),
        )

    def wait_for(self, predicate: Callable[[], bool], timeout: float) -> bool:
        start = self.clock()
        while True:
            if predicate():
                return True
            if self.clock() - start >= timeout:
                return False
            self.sleeper(self.poll_interval)


# ---------------------------------------------------------------------------
# Component lookup
# ---------------------------------------------------------------------------

def find_component(ident: Identity, manifests: Iterable[Manifest]) -> Component | None:
    """Return the Component matching `ident` across all manifests."""
    for m in manifests:
        for c in m.components:
            if Identity.from_component(c) == ident:
                return c
    return None


def _binary_present(start: str) -> bool:
    """Same coarse check as validate._binary_present (first token on PATH)."""
    if not start or start.strip() == "true":
        return True
    return shutil.which(start.split()[0]) is not None


# ---------------------------------------------------------------------------
# Per-component actions
# ---------------------------------------------------------------------------

def stop_component(rt: Runtime, c: Component, *, audit: AuditFn | None = None) -> None:
    if c.type == "compositor" and not c.stop:
        # Defence in depth (PLAN.md §2c Aturan 1): the compositor is never
        # part of the managed set. Even with an empty `stop`, refuse to
        # kill it by identity rather than signalling Hyprland.
        raise LifecycleError(
            f"refusing to stop compositor {c.name!r} by identity: the "
            f"compositor is never stopped or started by the switcher; give "
            f"it an explicit `stop` if this is truly intended"
        )
    if c.stop:
        cmd = c.stop
        res = rt.run(cmd, timeout=float(c.timeout))
        if audit:
            audit("stop", c.name, cmd, res)
        if not res.ok and cmd.strip() not in ("", "true"):
            if c.optional:
                return
            raise LifecycleError(
                f"stop failed for {c.name!r}: rc={res.rc} {res.stderr.strip()} (cmd={cmd!r})"
            )
    else:
        # Prasyarat 0 (DESIGN-onboarding-tui.md §4.2): no `stop` command
        # declared -> stop by process identity. `argv_match` is
        # mandatory here: a basename-only kill would also hit an
        # unrelated instance (e.g. `qs -c minimal`).
        if not c.argv_match:
            if c.optional:
                return
            raise LifecycleError(
                f"component {c.name!r}: cannot stop by identity without "
                f"argv_match (refusing a basename-only kill)"
            )
        rt.kill_processes(c)
        if audit:
            label = f"{c.executable or c.name} {' '.join(c.argv_match)}".strip()
            audit("stop.identity", c.name, label, None)
    if not rt.wait_for(lambda: not rt.is_running(c), float(c.timeout)):
        if c.optional:
            return
        raise LifecycleError(
            f"component {c.name!r} still running after stop (timeout {c.timeout}s)"
        )


def start_component(
    rt: Runtime,
    c: Component,
    *,
    audit: AuditFn | None = None,
    require_running: bool = True,
) -> None:
    if c.optional and not _binary_present(c.start):
        if audit:
            audit("skip-optional", c.name, c.start, None)
        return
    # `start` launches a long-running daemon, so it must be spawned
    # detached: waiting on it (as `run` would) blocks until timeout and
    # kills the component. Liveness is confirmed by the poll below.
    res = rt.spawn(c.start, timeout=float(c.timeout))
    if audit:
        audit("start", c.name, c.start, res)
    if not res.ok and c.start.strip() not in ("", "true"):
        raise LifecycleError(
            f"start failed for {c.name!r}: rc={res.rc} {res.stderr.strip()} (cmd={c.start!r})"
        )
    if require_running and not rt.wait_for(lambda: rt.is_running(c), float(c.timeout)):
        raise LifecycleError(
            f"component {c.name!r} not running after start (timeout {c.timeout}s)"
        )
    if c.health_check:
        h = rt.run(c.health_check, timeout=5.0)
        if audit:
            audit("health", c.name, c.health_check, h)
        if not h.ok:
            raise LifecycleError(
                f"health check failed for {c.name!r}: rc={h.rc} (cmd={c.health_check!r})"
            )


def reload_component(rt: Runtime, c: Component, *, audit: AuditFn | None = None) -> None:
    if not c.reload:
        return
    res = rt.run(c.reload, timeout=float(c.timeout))
    if audit:
        audit("reload", c.name, c.reload, res)
    if not res.ok:
        raise LifecycleError(
            f"reload failed for {c.name!r}: rc={res.rc} (cmd={c.reload!r})"
        )


# ---------------------------------------------------------------------------
# Hooks / theme / wallpaper
# ---------------------------------------------------------------------------

def run_hooks(
    rt: Runtime,
    hooks: Sequence[str],
    *,
    phase: str,
    audit: AuditFn | None = None,
) -> None:
    for h in hooks:
        if not h:
            continue
        res = rt.hook(h)
        if audit:
            audit(f"hook.{phase}", "", h, res)
        if not res.ok:
            raise LifecycleError(
                f"{phase}-hook failed: rc={res.rc} {res.stderr.strip()} (cmd={h!r})"
            )


def apply_theme(rt: Runtime, theme: Theme, *, audit: AuditFn | None = None) -> None:
    """Apply `[theme]` (PLAN.md §1.4/§1.5, §2b).

    Cursor via `hyprctl setcursor`; GTK/icon/fonts via `gsettings`; the
    optional `theme.apply` command runs last through the shell (hook).
    All are best-effort except `theme.apply`, whose failure is fatal.
    """
    if theme.cursor_name:
        size = theme.cursor_size or 24
        res = rt.argv(["hyprctl", "setcursor", theme.cursor_name, str(size)])
        if audit:
            audit("theme.cursor", "", theme.cursor_name, res)
    if theme.gtk_theme:
        res = rt.argv(["gsettings", "set", "org.gnome.desktop.interface", "gtk-theme", theme.gtk_theme])
        if audit:
            audit("theme.gtk", "", theme.gtk_theme, res)
    if theme.icon_theme:
        res = rt.argv(["gsettings", "set", "org.gnome.desktop.interface", "icon-theme", theme.icon_theme])
        if audit:
            audit("theme.icon", "", theme.icon_theme, res)
    if theme.font_main:
        res = rt.argv(["gsettings", "set", "org.gnome.desktop.interface", "font-name", theme.font_main])
        if audit:
            audit("theme.font_main", "", theme.font_main, res)
    if theme.font_mono:
        res = rt.argv(["gsettings", "set", "org.gnome.desktop.interface", "monospace-font-name", theme.font_mono])
        if audit:
            audit("theme.font_mono", "", theme.font_mono, res)
    if theme.apply:
        res = rt.hook(theme.apply)
        if audit:
            audit("theme.apply", "", theme.apply, res)
        if not res.ok:
            raise LifecycleError(
                f"theme.apply failed: rc={res.rc} {res.stderr.strip()} (cmd={theme.apply!r})"
            )


def run_wallpaper(rt: Runtime, wallpaper: str, *, audit: AuditFn | None = None) -> None:
    """Regenerate colors with `matugen image <wallpaper>` (PLAN.md §2b)."""
    if not wallpaper:
        return
    res = rt.argv(["matugen", "image", wallpaper], timeout=MATUGEN_TIMEOUT)
    if audit:
        audit("matugen", "", wallpaper, res)
    if not res.ok:
        # PLAN.md §5.3 #6: matugen failure does not roll back the symlink
        # swap; the config is correct, only the colors may be stale. We
        # surface it as a warning via the audit entry, not a fatal error.
        return


def hyprland_verify(rt: Runtime, *, audit: AuditFn | None = None) -> bool:
    """Best-effort `hyprland --verify-config` (PLAN.md §3.2 #9).

    Returns True when the check passed or could not be run (binary
    missing); False only when the binary ran and reported an error.
    """
    res = rt.argv(["hyprland", "--verify-config"], timeout=5.0)
    if audit:
        audit("hyprland.verify", "", "hyprland --verify-config", res)
    if res.rc in (-1, 127):
        return True
    return res.ok
