"""Slice 5 tests: process management (hooks, stop, theme, reload, start).

PLAN.md §3.2 (switch order), §3.3 (rollback), §2h (security), §5.2
(direct subprocess). Every test injects a fake `Runtime`, so no real
command runs and no desktop process is touched. The rest of the suite
sets ``RICE_TEST=1``; here we pass ``manage_processes=True`` explicitly.

The fake runtime is scripted through three tables:
  * ``rc``      : canonical command -> return code (default 0)
  * ``effects`` : canonical command -> (component name, should_be_up)
  * ``up``      : set of component names currently "running"
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from rice_switcher.executor import (
    ExecResult,
    needs_shell,
    run_command,
    run_hook,
    split_command,
)
from rice_switcher.lifecycle import (
    LifecycleError,
    Runtime,
    apply_theme,
    reload_component,
    run_hooks,
    run_wallpaper,
    start_component,
    stop_component,
)
from rice_switcher.manifest import (
    Component,
    Manifest,
    Meta,
    Theme,
    load_manifest,
)
from rice_switcher.paths import Roots, manifest_path, rice_dir
from rice_switcher.state import State, read_state, write_state
from rice_switcher.switch import do_switch, resolve_manage_processes
from rice_switcher.validate import validate_manifest


# ---------------------------------------------------------------------------
# Fake runtime
# ---------------------------------------------------------------------------

class FakeRuntime(Runtime):
    """A deterministic Runtime. Never spawns anything."""

    def __init__(self) -> None:
        self.records: list[tuple[str, int]] = []   # (canonical, rc)
        self.up: set[str] = set()
        self.effects: dict[str, tuple[str, bool]] = {}
        self.rc: dict[str, int] = {}
        self._t = 0.0
        super().__init__(
            runner=self._run,
            probe=self._probe,
            sleeper=self._sleep,
            clock=self._clock,
            poll_interval=0.1,
        )

    @staticmethod
    def _canonical(argv) -> str:
        argv = list(argv)
        if len(argv) >= 3 and argv[0] == "bash" and argv[1] in ("-c", "-lc"):
            return argv[2]
        return " ".join(argv)

    def _run(self, argv, timeout) -> ExecResult:
        argv = list(argv)
        canon = self._canonical(argv)
        rc = self.rc.get(canon, 0)
        self.records.append((canon, rc))
        eff = self.effects.get(canon)
        if eff is not None:
            name, up = eff
            if up:
                self.up.add(name)
            else:
                self.up.discard(name)
        return ExecResult(tuple(argv), rc, "", "")

    def _probe(self, c: Component) -> bool:
        return c.name in self.up

    def _sleep(self, seconds: float) -> None:
        self._t += seconds

    def _clock(self) -> float:
        return self._t

    def commands(self) -> list[str]:
        return [c for c, _ in self.records]

    def ran(self, canonical: str) -> bool:
        return canonical in self.commands()


# ---------------------------------------------------------------------------
# executors
# ---------------------------------------------------------------------------

class ExecutorTest(unittest.TestCase):
    def test_split_simple(self):
        self.assertEqual(split_command("pkill -x qs"), (["pkill", "-x", "qs"], ""))

    def test_noop_true(self):
        self.assertEqual(split_command("true"), ([], ""))

    def test_empty_is_noop(self):
        self.assertEqual(split_command(""), ([], ""))
        self.assertEqual(split_command("   "), ([], ""))

    def test_dangerous_pkill_rejected(self):
        argv, err = split_command("pkill -f qs")
        self.assertEqual(argv, [])
        self.assertIn("pkill", err)

    def test_dangerous_pkill_full_rejected(self):
        _, err = split_command("/usr/bin/pkill --full something")
        self.assertTrue(err)

    def test_needs_shell(self):
        self.assertTrue(needs_shell("a | b"))
        self.assertTrue(needs_shell("FOO=1 bar"))
        self.assertFalse(needs_shell("qs -c ii"))
        self.assertFalse(needs_shell("systemctl --user start x.service"))

    def test_run_command_noop_does_not_spawn(self):
        def boom(argv, timeout):
            raise AssertionError("runner must not be called for a no-op")
        res = run_command("true", runner=boom)
        self.assertTrue(res.ok)

    def test_run_hook_uses_bash(self):
        captured = []

        def cap(argv, timeout):
            captured.append(list(argv))
            return ExecResult(tuple(argv), 0)

        run_hook("echo hi", runner=cap)
        self.assertEqual(captured, [["bash", "-lc", "echo hi"]])

    def test_run_command_shell_fallback(self):
        captured = []

        def cap(argv, timeout):
            captured.append(list(argv))
            return ExecResult(tuple(argv), 0)

        run_command("a | b", runner=cap)
        self.assertEqual(captured, [["bash", "-lc", "a | b"]])

    def test_run_command_plain_argv(self):
        captured = []

        def cap(argv, timeout):
            captured.append(list(argv))
            return ExecResult(tuple(argv), 0)

        run_command("qs -c ii", runner=cap)
        self.assertEqual(captured, [["qs", "-c", "ii"]])


# ---------------------------------------------------------------------------
# lifecycle units
# ---------------------------------------------------------------------------

def _comp(name: str, **kw) -> Component:
    kw.setdefault("type", "shell")
    kw.setdefault("start", "true")
    kw.setdefault("stop", "true")
    return Component(name=name, **kw)


class StopComponentTest(unittest.TestCase):
    def test_stop_success(self):
        rt = FakeRuntime()
        rt.up.add("quickshell")
        rt.effects["qs-stop"] = ("quickshell", False)
        stop_component(rt, _comp("quickshell", stop="qs-stop", timeout=1))
        self.assertNotIn("quickshell", rt.up)

    def test_stop_command_failure_non_optional_raises(self):
        rt = FakeRuntime()
        rt.rc["qs-stop"] = 1
        with self.assertRaises(LifecycleError):
            stop_component(rt, _comp("quickshell", stop="qs-stop", timeout=1))

    def test_stop_command_failure_optional_is_ignored(self):
        rt = FakeRuntime()
        rt.rc["qs-stop"] = 1
        stop_component(rt, _comp("quickshell", stop="qs-stop", timeout=1, optional=True))

    def test_stop_still_running_times_out(self):
        rt = FakeRuntime()
        rt.up.add("quickshell")  # never goes down
        with self.assertRaises(LifecycleError):
            stop_component(rt, _comp("quickshell", stop="qs-stop", timeout=1))

    def test_stop_still_running_optional_is_ignored(self):
        rt = FakeRuntime()
        rt.up.add("quickshell")
        stop_component(rt, _comp("quickshell", stop="qs-stop", timeout=1, optional=True))


class StartComponentTest(unittest.TestCase):
    def test_start_success(self):
        rt = FakeRuntime()
        rt.effects["wb-start"] = ("waybar", True)
        start_component(rt, _comp("waybar", start="wb-start", timeout=1))
        self.assertIn("waybar", rt.up)

    def test_start_command_failure_raises(self):
        rt = FakeRuntime()
        rt.rc["wb-start"] = 1
        with self.assertRaises(LifecycleError):
            start_component(rt, _comp("waybar", start="wb-start", timeout=1))

    def test_start_never_comes_up_raises(self):
        rt = FakeRuntime()
        with self.assertRaises(LifecycleError):
            start_component(rt, _comp("waybar", start="wb-start", timeout=1))

    def test_health_check_failure_raises(self):
        rt = FakeRuntime()
        rt.effects["wb-start"] = ("waybar", True)
        rt.rc["wb-health"] = 1
        with self.assertRaises(LifecycleError):
            start_component(
                rt,
                _comp("waybar", start="wb-start", health_check="wb-health", timeout=1),
            )

    def test_require_running_false_skips_wait(self):
        rt = FakeRuntime()
        start_component(
            rt, _comp("waybar", start="wb-start", timeout=1), require_running=False
        )
        self.assertNotIn("waybar", rt.up)  # never came up, but we did not wait


class ReloadComponentTest(unittest.TestCase):
    def test_reload_success(self):
        rt = FakeRuntime()
        reload_component(rt, _comp("hyprland", reload="hyprctl reload", timeout=1))
        self.assertTrue(rt.ran("hyprctl reload"))

    def test_reload_failure_raises(self):
        rt = FakeRuntime()
        rt.rc["hyprctl reload"] = 1
        with self.assertRaises(LifecycleError):
            reload_component(rt, _comp("hyprland", reload="hyprctl reload", timeout=1))

    def test_empty_reload_is_noop(self):
        rt = FakeRuntime()
        reload_component(rt, _comp("hyprland", reload="", timeout=1))
        self.assertEqual(rt.records, [])


class HooksTest(unittest.TestCase):
    def test_hooks_run_in_order(self):
        rt = FakeRuntime()
        run_hooks(rt, ["first", "second"], phase="pre")
        self.assertEqual(rt.commands(), ["first", "second"])

    def test_hook_failure_raises(self):
        rt = FakeRuntime()
        rt.rc["bad"] = 1
        with self.assertRaises(LifecycleError):
            run_hooks(rt, ["bad"], phase="post")


class ThemeTest(unittest.TestCase):
    def test_theme_commands(self):
        rt = FakeRuntime()
        apply_theme(
            rt,
            Theme(cursor_name="Bibata", cursor_size=24, gtk_theme="Adwaita",
                  icon_theme="Papirus", font_main="Inter 11",
                  font_mono="JetBrains Mono 10", apply="apply-theme"),
        )
        cmds = rt.commands()
        self.assertIn("hyprctl setcursor Bibata 24", cmds)
        self.assertIn("gsettings set org.gnome.desktop.interface gtk-theme Adwaita", cmds)
        self.assertIn("gsettings set org.gnome.desktop.interface icon-theme Papirus", cmds)
        self.assertIn("gsettings set org.gnome.desktop.interface font-name Inter 11", cmds)
        self.assertIn(
            "gsettings set org.gnome.desktop.interface monospace-font-name JetBrains Mono 10",
            cmds,
        )
        self.assertIn("apply-theme", cmds)

    def test_theme_apply_failure_is_fatal(self):
        rt = FakeRuntime()
        rt.rc["apply-theme"] = 1
        with self.assertRaises(LifecycleError):
            apply_theme(rt, Theme(apply="apply-theme"))

    def test_empty_theme_is_noop(self):
        rt = FakeRuntime()
        apply_theme(rt, Theme())
        self.assertEqual(rt.records, [])


class WallpaperTest(unittest.TestCase):
    def test_matugen_called(self):
        rt = FakeRuntime()
        run_wallpaper(rt, "/w.png")
        self.assertTrue(rt.ran("matugen image /w.png"))

    def test_matugen_failure_is_not_fatal(self):
        rt = FakeRuntime()
        rt.rc["matugen image /w.png"] = 1
        run_wallpaper(rt, "/w.png")  # must not raise

    def test_empty_wallpaper_is_noop(self):
        rt = FakeRuntime()
        run_wallpaper(rt, "")
        self.assertEqual(rt.records, [])


# ---------------------------------------------------------------------------
# resolve_manage_processes
# ---------------------------------------------------------------------------

class ResolveManageProcessesTest(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.pop("RICE_TEST", None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("RICE_TEST", None)
        else:
            os.environ["RICE_TEST"] = self._saved

    def test_explicit_true_wins(self):
        os.environ["RICE_TEST"] = "1"
        self.assertTrue(resolve_manage_processes(True))

    def test_explicit_false_wins(self):
        self.assertFalse(resolve_manage_processes(False))

    def test_default_on_without_rice_test(self):
        self.assertTrue(resolve_manage_processes(None))

    def test_default_off_with_rice_test(self):
        os.environ["RICE_TEST"] = "1"
        self.assertFalse(resolve_manage_processes(None))


# ---------------------------------------------------------------------------
# validate: dangerous command guard
# ---------------------------------------------------------------------------

class ValidateDangerousCommandTest(unittest.TestCase):
    def _manifest(self, **kw) -> Manifest:
        return Manifest(meta=Meta(name="r", description="d"), **kw)

    def test_pkill_f_in_component_is_error(self):
        m = self._manifest(components=[
            _comp("qs", start="true", stop="pkill -f qs"),
        ])
        res = validate_manifest(m, Roots("/r", "/c", "/s"))
        self.assertFalse(res.ok)
        self.assertTrue(any("pkill -f" in e for e in res.errors))

    def test_pkill_f_in_hook_is_error(self):
        m = self._manifest(components=[_comp("qs")], hooks_pre=["pkill -f x"])
        res = validate_manifest(m, Roots("/r", "/c", "/s"))
        self.assertFalse(res.ok)

    def test_pkill_f_in_theme_apply_is_error(self):
        m = self._manifest(components=[_comp("qs")], theme=Theme(apply="pkill --full x"))
        res = validate_manifest(m, Roots("/r", "/c", "/s"))
        self.assertFalse(res.ok)

    def test_pkill_x_is_allowed(self):
        m = self._manifest(components=[_comp("qs", stop="pkill -x qs")])
        res = validate_manifest(m, Roots("/r", "/c", "/s"))
        self.assertTrue(res.ok, res.errors)


# ---------------------------------------------------------------------------
# end-to-end switch with the fake runtime
# ---------------------------------------------------------------------------

def _tmp_roots():
    td = tempfile.TemporaryDirectory(prefix="rice-slice5-")
    base = td.name
    roots = Roots(
        rice_home=os.path.join(base, "rice"),
        config_home=os.path.join(base, "config"),
        state_home=os.path.join(base, "state"),
    )
    for d in (roots.rice_home, roots.config_home, roots.state_home):
        os.makedirs(d, exist_ok=True)
    return roots, td


def _write_manifest(roots: Roots, name: str, body: str) -> str:
    os.makedirs(rice_dir(roots, name), exist_ok=True)
    path = manifest_path(roots, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    return path


_HYPR = """\
[meta]
name = "{name}"
description = "test rice {name}"

[[components]]
name = "hyprland"
type = "compositor"
start = "true"
stop = "true"
executable = "hyprland"
reload = "hyprctl reload"
config_link = "{hypr}"

[[components]]
name = "{shell}"
type = "shell"
start = "{shell}-start"
stop = "{shell}-stop"
executable = "{shell}"
config_link = "{shell_link}"
"""


class SwitchProcessTest(unittest.TestCase):
    def setUp(self):
        self.roots, self._td = _tmp_roots()
        # Live config dirs (the pre-switch state).
        for d in ("hypr", "quickshell"):
            os.makedirs(os.path.join(self.roots.config_home, d))
        # Rice A uses quickshell; rice B uses waybar.
        self.hypr_live = os.path.join(self.roots.config_home, "hypr")
        self.qs_live = os.path.join(self.roots.config_home, "quickshell")
        self.wb_live = os.path.join(self.roots.config_home, "waybar")
        self._write_rice("A", "quickshell", self.qs_live)
        self._write_rice("B", "waybar", self.wb_live)
        self.a = load_manifest(manifest_path(self.roots, "A"))
        self.b = load_manifest(manifest_path(self.roots, "B"))
        write_state(self.roots, State(current_rice="A"))

    def tearDown(self):
        self._td.cleanup()

    def _write_rice(self, name, shell, shell_link):
        _write_manifest(
            self.roots, name,
            _HYPR.format(
                name=name, shell=shell, hypr=self.hypr_live, shell_link=shell_link,
            ),
        )
        # The rice's own config dirs (swap targets).
        os.makedirs(os.path.join(rice_dir(self.roots, name), "hypr"), exist_ok=True)
        os.makedirs(os.path.join(rice_dir(self.roots, name), shell), exist_ok=True)

    def _runtime(self):
        rt = FakeRuntime()
        rt.up.add("quickshell")
        rt.effects["quickshell-stop"] = ("quickshell", False)
        rt.effects["waybar-start"] = ("waybar", True)
        return rt

    def test_success_runs_stop_start_reload_and_swaps(self):
        rt = self._runtime()
        outcome = do_switch(
            self.roots, "B", [self.a, self.b],
            dry_run=False, manage_processes=True, runtime=rt,
        )
        self.assertEqual(outcome.errors, [])
        self.assertIsNotNone(outcome.snapshot_ts)
        # State moved to B.
        self.assertEqual(read_state(self.roots).current_rice, "B")
        # Old shell stopped, new shell started.
        self.assertTrue(rt.ran("quickshell-stop"))
        self.assertTrue(rt.ran("waybar-start"))
        self.assertNotIn("quickshell", rt.up)
        self.assertIn("waybar", rt.up)
        # Hyprland config was reloaded, never stopped/started.
        self.assertTrue(rt.ran("hyprctl reload"))
        self.assertFalse(any("hyprland" in c and "stop" in c for c in rt.commands()))
        # Live symlink for the new rice points at the rice dir.
        link = os.path.join(self.roots.config_home, "waybar")
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.path.realpath(link),
                         os.path.realpath(os.path.join(rice_dir(self.roots, "B"), "waybar")))

    def test_failure_rolls_back_files_state_and_processes(self):
        rt = self._runtime()
        rt.effects.pop("waybar-start")   # waybar never comes up
        rt.rc["waybar-start"] = 1
        outcome = do_switch(
            self.roots, "B", [self.a, self.b],
            dry_run=False, manage_processes=True, runtime=rt,
        )
        self.assertTrue(outcome.errors)
        self.assertIsNone(outcome.snapshot_ts)
        # State untouched: still on A.
        self.assertEqual(read_state(self.roots).current_rice, "A")
        # The stopped component was restarted during rollback.
        self.assertTrue(rt.ran("quickshell-start"))

    def test_processes_skipped_when_disabled(self):
        rt = self._runtime()
        outcome = do_switch(
            self.roots, "B", [self.a, self.b],
            dry_run=False, manage_processes=False, runtime=rt,
        )
        self.assertEqual(outcome.errors, [])
        self.assertEqual(rt.records, [])   # no command at all


if __name__ == "__main__":
    unittest.main()
