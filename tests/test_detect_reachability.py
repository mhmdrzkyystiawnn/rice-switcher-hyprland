"""Fase 2b Langkah 3: reachability-based detection.

Only autostart lines in files reachable from the Hyprland entrypoint may
create components. Reachability follows `source =` (hyprlang) and literal
`require(...)`/`dofile(...)` (Lua) while staying inside the rice dir, with
cycles and depth bounded. Archive/backup/example dirs are never live.

Also covers compound-command splitting and the broadened executable map.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from rice_switcher.detect import CONF_AUTO, detect
from rice_switcher.paths import Roots


def _roots(base: str) -> Roots:
    return Roots(
        rice_home=os.path.join(base, "rice"),
        config_home=os.path.join(base, "config"),
        state_home=os.path.join(base, "state"),
    )


def _mkrice(tmp: str, name: str, files: dict) -> str:
    rice = os.path.join(tmp, name)
    os.makedirs(rice, exist_ok=True)
    for rel, content in files.items():
        path = os.path.join(rice, rel)
        if rel.endswith("/"):
            os.makedirs(path, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
    return rice


def _by_name(d, name):
    for c in d.components:
        if c.name == name:
            return c
    raise AssertionError(f"no component {name!r}; have {[c.name for c in d.components]}")


def _names(d):
    return {c.name for c in d.components}


class ReachabilityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rs-reach-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_archive_dir_ignored(self):
        rice = _mkrice(self.tmp, "r", {
            "hypr/hyprland.conf": "exec-once = waybar\n",
            "hypr/archive/oldhypr.conf": "exec-once = swaync\n",
        })
        d = detect(rice, roots=_roots(self.tmp))
        self.assertIn("waybar", _names(d))
        self.assertNotIn("swaync", _names(d))
        ref = next(r for r in d.autostart_refs if "oldhypr.conf" in r.file)
        self.assertFalse(ref.reachable)
        self.assertTrue(any("terjangkau" in n for n in d.notes), d.notes)

    def test_example_dir_ignored(self):
        rice = _mkrice(self.tmp, "r", {
            "hypr/hyprland.conf": "exec-once = waybar\n",
            "hypr/example/sample.conf": "exec-once = swaync\n",
        })
        d = detect(rice, roots=_roots(self.tmp))
        self.assertNotIn("swaync", _names(d))

    def test_source_chain_is_reachable(self):
        rice = _mkrice(self.tmp, "r", {
            "hypr/hyprland.conf": "source = sub/extra.conf\n",
            "hypr/sub/extra.conf": "exec-once = swaync\n",
        })
        d = detect(rice, roots=_roots(self.tmp))
        self.assertIn("swaync", _names(d))
        self.assertEqual(_by_name(d, "swaync").argv_match.confidence, CONF_AUTO)

    def test_source_into_archive_not_followed(self):
        rice = _mkrice(self.tmp, "r", {
            "hypr/hyprland.conf": "source = archive/old.conf\n",
            "hypr/archive/old.conf": "exec-once = swaync\n",
        })
        d = detect(rice, roots=_roots(self.tmp))
        self.assertNotIn("swaync", _names(d))

    def test_source_cycle_terminates(self):
        rice = _mkrice(self.tmp, "r", {
            "hypr/hyprland.conf": "source = a.conf\nexec-once = waybar\n",
            "hypr/a.conf": "source = b.conf\n",
            "hypr/b.conf": "source = a.conf\nexec-once = dunst\n",
        })
        d = detect(rice, roots=_roots(self.tmp))  # must terminate
        self.assertIn("waybar", _names(d))
        self.assertIn("dunst", _names(d))

    def test_require_chain_is_reachable(self):
        rice = _mkrice(self.tmp, "r", {
            "hypr/hyprland.lua": 'require("execs")\n',
            "hypr/execs.lua": 'hl.exec_cmd("swaync")\n',
        })
        d = detect(rice, roots=_roots(self.tmp))
        self.assertIn("swaync", _names(d))

    def test_require_outside_rice_not_followed(self):
        outside = os.path.join(self.tmp, "outside.lua")
        with open(outside, "w") as f:
            f.write('hl.exec_cmd("swaync")\n')
        rice = _mkrice(self.tmp, "r", {
            "hypr/hyprland.lua": 'require("../../outside")\nrequire("/etc/passwd")\n',
        })
        d = detect(rice, roots=_roots(self.tmp))
        self.assertNotIn("swaync", _names(d))

    def test_missing_entrypoint_reports_and_does_not_scan_all(self):
        rice = _mkrice(self.tmp, "r", {
            "hypr/execs.lua": 'hl.exec_cmd("qs -c ii")\n',
        })
        d = detect(rice, roots=_roots(self.tmp))
        self.assertNotIn("quickshell", _names(d))
        self.assertTrue(any("entrypoint" in n for n in d.notes), d.notes)


class CompoundCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rs-cmd-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _detect_conf(self, line):
        rice = _mkrice(self.tmp, "r", {"hypr/hyprland.conf": line + "\n"})
        return detect(rice, roots=_roots(self.tmp))

    def test_sleep_then_swww(self):
        d = self._detect_conf("exec-once = sleep .5 && swww restore")
        s = _by_name(d, "swww")
        self.assertEqual(s.start.value, "swww restore")
        self.assertEqual(s.argv_match.value, ["restore"])
        self.assertNotIn("sleep", _names(d))

    def test_semicolon_and_pipe_split(self):
        d = self._detect_conf("exec-once = hypridle; waybar")
        self.assertIn("hypridle", _names(d))
        self.assertIn("waybar", _names(d))

    def test_awww_daemon_exact_noargs(self):
        d = self._detect_conf("exec-once = awww-daemon")
        a = _by_name(d, "awww")
        self.assertEqual(a.executable.value, "awww-daemon")
        self.assertTrue(a.exact_argv.value)
        self.assertEqual(a.argv_match.value, [])
        self.assertEqual(a.stop.value, "")

    def test_uwsm_app_wrapper(self):
        d = self._detect_conf("exec-once = uwsm app -- waybar")
        self.assertEqual(_by_name(d, "waybar").start.value, "waybar")

    def test_systemd_run_wrapper(self):
        d = self._detect_conf("exec-once = systemd-run --user -- hypridle")
        self.assertEqual(_by_name(d, "hypridle").start.value, "hypridle")

    def test_env_assignment_wrapper(self):
        d = self._detect_conf("exec-once = WAYLAND=1 hypridle")
        self.assertEqual(_by_name(d, "hypridle").start.value, "hypridle")

    def test_args_use_subset_identity(self):
        d = self._detect_conf("exec-once = waybar -c /etc/waybar/config")
        w = _by_name(d, "waybar")
        self.assertEqual(w.argv_match.value, ["-c", "/etc/waybar/config"])
        self.assertFalse(w.exact_argv.value)
        self.assertEqual(w.stop.value, "")


class BroadenedMapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rs-map-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_more_daemons_detected(self):
        rice = _mkrice(self.tmp, "r", {
            "hypr/hyprland.conf":
                "exec-once = swaync\n"
                "exec-once = hyprsunset\n"
                "exec-once = mpvpaper\n"
                "exec-once = swww-daemon\n",
        })
        d = detect(rice, roots=_roots(self.tmp))
        for name in ("swaync", "hyprsunset", "mpvpaper", "swww"):
            self.assertIn(name, _names(d), _names(d))


class GenerateExactIdentityTest(unittest.TestCase):
    """`generate` must emit the exact no-arg identity so the draft can stop."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rs-gen-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_generated_toml_has_exact_argv_and_identity_stop(self):
        from rice_switcher.cli import _draft_to_toml
        from rice_switcher.manifest import parse_manifest
        import tomllib

        rice = _mkrice(self.tmp, "r", {"hypr/hyprland.conf": "exec-once = waybar\n"})
        d = detect(rice, roots=_roots(self.tmp))
        text = _draft_to_toml(d)
        data = tomllib.loads(text)
        w = next(c for c in data["components"] if c["name"] == "waybar")
        self.assertTrue(w["exact_argv"])
        self.assertEqual(w["stop"], "")
        # ... and the result round-trips into a stoppable manifest.
        m = parse_manifest(data)
        c = next(c for c in m.components if c.name == "waybar")
        self.assertTrue(c.exact_argv)
        self.assertTrue(c.stop == "")


if __name__ == "__main__":
    unittest.main()
