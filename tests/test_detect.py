"""Tests for rice detection and layout normalization (DESIGN §4).

Hermetic: every tree is built under a tempdir. `detect` is read-only and
must never spawn a process or evaluate the rice's Lua -- one test patches
the process-spawning seams to raise if touched.

Confidence vocabulary: `auto` (strong signal), `guess` (name-based),
`ask` (must be decided by the user).
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

THIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from rice_switcher.detect import (
    CONF_ASK,
    CONF_AUTO,
    CONF_GUESS,
    DraftComponent,
    Field,
    LayoutLink,
    ManifestDraft,
    apply_layout_normalization,
    detect,
    plan_layout_normalization,
)
from rice_switcher.gaps import AUTOSTART_CONFLICT
from rice_switcher.paths import Roots


def _roots(base: str) -> Roots:
    return Roots(
        rice_home=os.path.join(base, "rice"),
        config_home=os.path.join(base, "config"),
        state_home=os.path.join(base, "state"),
    )


def _write(base: str, files: dict) -> str:
    """Materialise a tree. Keys are relative paths; a key ending in '/'
    is a directory. Values are str or bytes."""
    for rel, content in files.items():
        path = os.path.join(base, rel)
        if rel.endswith("/"):
            os.makedirs(path, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        mode = "wb" if isinstance(content, bytes) else "w"
        with open(path, mode) as f:
            f.write(content)
    return base


def _mkrice(tmp: str, name: str, files: dict) -> str:
    rice = os.path.join(tmp, name)
    os.makedirs(rice, exist_ok=True)
    _write(rice, files)
    return rice


def _by_name(draft: ManifestDraft, name: str) -> DraftComponent:
    for c in draft.components:
        if c.name == name:
            return c
    raise AssertionError(f"no component {name!r}; have {[c.name for c in draft.components]}")


def _names(draft: ManifestDraft) -> set:
    return {c.name for c in draft.components}


class CompositorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rs-detect-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_hypr_dir_compositor(self):
        rice = _mkrice(self.tmp, "r", {"hypr/hyprland.conf": "exec-once = waybar\n"})
        d = detect(rice, roots=_roots(self.tmp))
        self.assertEqual(d.name, "r")
        h = _by_name(d, "hyprland")
        self.assertEqual(h.type.value, "compositor")
        self.assertEqual(h.reload.value, "hyprctl reload")
        self.assertEqual(h.config_link.value, "~/.config/hypr")
        self.assertEqual(h.start.value, "true")
        # compositor keeps an explicit stop so stop_component never
        # identity-kills it
        self.assertEqual(h.stop.value, "true")
        self.assertIn("waybar", _names(d))

    def test_hyprland_lua_only(self):
        rice = _mkrice(self.tmp, "r", {"hyprland.lua": 'require("hyprland.execs")\n'})
        d = detect(rice, roots=_roots(self.tmp))
        self.assertIn("hyprland", _names(d))


class AutostartTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rs-detect-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_hyprlang_exec_and_exec_once(self):
        rice = _mkrice(self.tmp, "r", {
            "hypr/hyprland.conf":
                "exec-once = waybar\n"
                "exec = dunst\n",
        })
        d = detect(rice, roots=_roots(self.tmp))
        self.assertIn("waybar", _names(d))
        self.assertIn("dunst", _names(d))
        kinds = {r.kind for r in d.autostart_refs}
        self.assertIn("exec-once", kinds)
        self.assertIn("exec", kinds)
        for r in d.autostart_refs:
            self.assertTrue(r.file.endswith("hyprland.conf"))
            self.assertGreaterEqual(r.line, 1)
            self.assertTrue(r.raw)

    def test_lua_exec_cmd_with_env_indirection(self):
        # Mirrors the real elifouts layout: entrypoint -> variables + execs.
        rice = _mkrice(self.tmp, "r", {
            "hypr/hyprland.lua":
                'require("variables")\n'
                'require("execs")\n',
            "hypr/variables.lua": 'hl.env("qsConfig", "ii")\n',
            "hypr/execs.lua":
                'hl.on("hyprland.start", function ()\n'
                '    hl.exec_cmd("qs -c $qsConfig")\n'
                'end)\n',
            "quickshell/ii/shell.qml": "// shell\n",
        })
        d = detect(rice, roots=_roots(self.tmp))
        qs = _by_name(d, "quickshell")
        self.assertEqual(qs.type.value, "shell")
        self.assertEqual(qs.executable.value, "qs")
        self.assertEqual(qs.argv_match.value, ["-c", "ii"])
        self.assertEqual(qs.start.value, "qs -c ii")
        self.assertEqual(qs.stop.value, "")
        self.assertEqual(qs.config_link.value, "~/.config/quickshell")
        self.assertEqual(qs.argv_match.confidence, CONF_AUTO)
        self.assertEqual(qs.executable.confidence, CONF_AUTO)
        self.assertIn("hl.exec_cmd", {r.kind for r in d.autostart_refs})

    def test_hyprlang_variable_indirection(self):
        rice = _mkrice(self.tmp, "r", {
            "hypr/hyprland.conf":
                "$bar = waybar\n"
                "exec-once = $bar -c /etc/waybar/config\n",
        })
        d = detect(rice, roots=_roots(self.tmp))
        w = _by_name(d, "waybar")
        self.assertEqual(w.start.value, "waybar -c /etc/waybar/config")
        self.assertEqual(w.argv_match.value, ["-c", "/etc/waybar/config"])
        self.assertEqual(w.argv_match.confidence, CONF_AUTO)

    def test_unresolved_variable_is_ask(self):
        rice = _mkrice(self.tmp, "r", {
            "hypr/hyprland.lua": 'require("execs")\n',
            "hypr/execs.lua": 'hl.exec_cmd("qs -c $unknownCfg")\n',
        })
        d = detect(rice, roots=_roots(self.tmp))
        qs = _by_name(d, "quickshell")
        self.assertEqual(qs.argv_match.confidence, CONF_ASK)
        self.assertIn("$unknownCfg", qs.start.value)

    def test_dynamic_lua_expression_is_ask(self):
        rice = _mkrice(self.tmp, "r", {
            "hypr/hyprland.lua": 'require("execs")\n',
            "hypr/execs.lua": 'hl.exec_cmd("qs -c " .. cfg)\n',
        })
        d = detect(rice, roots=_roots(self.tmp))
        self.assertTrue(any(r.confidence == CONF_ASK for r in d.autostart_refs),
                        [r for r in d.autostart_refs])
        self.assertNotIn("quickshell", _names(d))

    def test_commented_out_exec_ignored(self):
        rice = _mkrice(self.tmp, "r", {
            "hypr/hyprland.lua": 'require("execs")\n',
            "hypr/execs.lua": '--hl.exec_cmd("qs -c ii")\n',
        })
        d = detect(rice, roots=_roots(self.tmp))
        self.assertNotIn("quickshell", _names(d))

    def test_argless_daemon_gets_exact_noarg_identity(self):
        # Fase 2b Langkah 1 + 3: an argument-less daemon is given an
        # explicit exact identity so the draft can actually be stopped.
        rice = _mkrice(self.tmp, "r", {"hypr/hyprland.conf": "exec-once = hypridle\n"})
        d = detect(rice, roots=_roots(self.tmp))
        h = _by_name(d, "hypridle")
        self.assertEqual(h.start.value, "hypridle")
        self.assertEqual(h.argv_match.value, [])
        self.assertTrue(h.exact_argv.value)
        self.assertEqual(h.stop.value, "")

    def test_autostart_conflict_note_replaced_by_structured_finding(self):
        # Fase 4: the old free-text note is gone; a structured finding now
        # carries the component, refs and handling options (DESIGN §6.1).
        rice = _mkrice(self.tmp, "r", {"hypr/hyprland.conf": "exec-once = waybar\n"})
        d = detect(rice, roots=_roots(self.tmp))
        self.assertFalse(any("autostart bentrok" in n for n in d.notes))
        fs = [f for f in d.findings if f.kind == AUTOSTART_CONFLICT]
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0].component, "waybar")

    def test_autostart_refs_record_file_line_kind_raw(self):
        rice = _mkrice(self.tmp, "r", {
            "hypr/hyprland.lua": 'require("execs")\n',
            "hypr/execs.lua": '\nhl.exec_cmd("waybar")\n',
        })
        d = detect(rice, roots=_roots(self.tmp))
        ref = next(r for r in d.autostart_refs if r.kind == "hl.exec_cmd")
        self.assertEqual(ref.line, 2)
        self.assertIn("waybar", ref.raw)
        self.assertTrue(ref.reachable)


class DirSignalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rs-detect-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_waybar_dir(self):
        rice = _mkrice(self.tmp, "r", {
            "hypr/hyprland.conf": "",
            "waybar/config.jsonc": "{}\n",
            "waybar/style.css": "",
        })
        d = detect(rice, roots=_roots(self.tmp))
        w = _by_name(d, "waybar")
        self.assertEqual(w.type.value, "bar")
        self.assertEqual(w.executable.value, "waybar")
        self.assertEqual(w.start.value, "waybar")
        self.assertEqual(w.start_check.value, "pgrep -x waybar")
        self.assertEqual(w.config_link.value, "~/.config/waybar")

    def test_quickshell_cfg_from_dir(self):
        rice = _mkrice(self.tmp, "r", {
            "hypr/hyprland.conf": "",
            "quickshell/minimal/shell.qml": "// x\n",
        })
        d = detect(rice, roots=_roots(self.tmp))
        qs = _by_name(d, "quickshell")
        self.assertEqual(qs.start.value, "qs -c minimal")
        self.assertEqual(qs.argv_match.value, ["-c", "minimal"])

    def test_hypridle_conf(self):
        rice = _mkrice(self.tmp, "r", {
            "hypr/hyprland.conf": "",
            "hypridle.conf": "general {}\n",
        })
        d = detect(rice, roots=_roots(self.tmp))
        h = _by_name(d, "hypridle")
        self.assertEqual(h.executable.value, "hypridle")
        self.assertEqual(h.start.value, "hypridle")
        # config covered by ~/.config/hypr
        self.assertEqual(h.config_link.value, "")

    def test_notification_daemons(self):
        rice = _mkrice(self.tmp, "r", {
            "hypr/hyprland.conf": "",
            "mako/config": "x\n",
            "dunst/dunstrc": "y\n",
        })
        d = detect(rice, roots=_roots(self.tmp))
        self.assertIn("mako", _names(d))
        self.assertIn("dunst", _names(d))
        self.assertEqual(_by_name(d, "mako").type.value, "notifications")

    def test_wallpaper_daemon(self):
        rice = _mkrice(self.tmp, "r", {
            "hypr/hyprland.conf": "exec-once = hyprpaper\n",
        })
        d = detect(rice, roots=_roots(self.tmp))
        self.assertEqual(_by_name(d, "hyprpaper").type.value, "wallpaper")

    def test_hyprpaper_conf_detected(self):
        rice = _mkrice(self.tmp, "r", {
            "hypr/hyprland.conf": "",
            "hypr/hyprpaper.conf": "preload = x\n",
        })
        d = detect(rice, roots=_roots(self.tmp))
        self.assertIn("hyprpaper", _names(d))


class LayoutTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rs-detect-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_form_config_and_plan(self):
        rice = _mkrice(self.tmp, "r", {
            ".config/hypr/hyprland.conf": "exec-once = waybar\n",
            ".config/waybar/config.jsonc": "{}\n",
        })
        d = detect(rice, roots=_roots(self.tmp))
        self.assertEqual(d.layout.form, "config")
        links = plan_layout_normalization(d, rice)
        self.assertIn(LayoutLink(name="hypr", target=".config/hypr"), links)
        self.assertIn(LayoutLink(name="waybar", target=".config/waybar"), links)

    def test_form_plain_no_plan(self):
        rice = _mkrice(self.tmp, "r", {
            "hypr/hyprland.conf": "",
            "waybar/config.jsonc": "{}\n",
        })
        d = detect(rice, roots=_roots(self.tmp))
        self.assertEqual(d.layout.form, "plain")
        self.assertEqual(plan_layout_normalization(d, rice), [])

    def test_apply_creates_relative_symlink(self):
        rice = _mkrice(self.tmp, "r", {
            ".config/hypr/hyprland.conf": "exec-once = waybar\n",
            ".config/waybar/config.jsonc": "{}\n",
        })
        os.makedirs(os.path.join(rice, ".git", "info"), exist_ok=True)
        d = detect(rice, roots=_roots(self.tmp))
        results = apply_layout_normalization(d, rice)
        self.assertTrue(os.path.islink(os.path.join(rice, "hypr")))
        self.assertEqual(os.readlink(os.path.join(rice, "hypr")), ".config/hypr")
        with open(os.path.join(rice, ".git", "info", "exclude")) as f:
            exclude = f.read()
        self.assertIn("hypr", exclude)
        self.assertTrue(any(r.created for r in results))

    def test_apply_idempotent(self):
        rice = _mkrice(self.tmp, "r", {
            ".config/hypr/hyprland.conf": "exec-once = waybar\n",
        })
        d = detect(rice, roots=_roots(self.tmp))
        apply_layout_normalization(d, rice)
        second = apply_layout_normalization(d, rice)
        self.assertTrue(all(not r.created for r in second), second)

    def test_apply_never_overwrites_existing(self):
        rice = _mkrice(self.tmp, "r", {
            ".config/hypr/hyprland.conf": "exec-once = waybar\n",
            "hypr": "i am a file\n",
        })
        d = detect(rice, roots=_roots(self.tmp))
        results = apply_layout_normalization(d, rice)
        self.assertFalse(os.path.islink(os.path.join(rice, "hypr")))
        self.assertTrue(any("exist" in r.detail for r in results), results)

    def test_apply_refuses_escaping_target(self):
        rice = _mkrice(self.tmp, "r", {".config/keep": "x\n"})
        # .config/evil -> /etc  (escapes the rice dir)
        os.symlink("/etc", os.path.join(rice, ".config", "evil"))
        comp = DraftComponent(
            name="evil", type=Field("wallpaper"),
            config_link=Field("~/.config/evil"))
        draft = ManifestDraft(name="r", components=[comp])
        draft.layout.form = "config"
        results = apply_layout_normalization(draft, rice)
        self.assertFalse(os.path.lexists(os.path.join(rice, "evil")))
        self.assertTrue(any("escape" in r.detail for r in results), results)

    def test_apply_skips_when_not_git_repo(self):
        # No .git: creating the symlink still works, no exclude written.
        rice = _mkrice(self.tmp, "r", {
            ".config/hypr/hyprland.conf": "exec-once = waybar\n",
        })
        d = detect(rice, roots=_roots(self.tmp))
        apply_layout_normalization(d, rice)
        self.assertFalse(os.path.exists(os.path.join(rice, ".git")))


class SafetyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rs-detect-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_subprocess_or_exec(self):
        rice = _mkrice(self.tmp, "r", {
            "hypr/hyprland.conf": "exec-once = waybar\n",
            "hypr/execs.lua": 'hl.exec_cmd("qs -c ii")\n',
        })
        with mock.patch("subprocess.run", side_effect=AssertionError("run")), \
             mock.patch("subprocess.Popen", side_effect=AssertionError("Popen")), \
             mock.patch("os.system", side_effect=AssertionError("system")), \
             mock.patch("os.popen", side_effect=AssertionError("popen")), \
             mock.patch("os.execv", side_effect=AssertionError("execv")):
            detect(rice, roots=_roots(self.tmp))

    def test_escaping_symlink_warned_and_not_followed(self):
        rice = _mkrice(self.tmp, "r", {
            "hypr/hyprland.conf": "exec-once = waybar\n",
        })
        outside = os.path.join(self.tmp, "secret.txt")
        with open(outside, "w") as f:
            f.write("PRIVATE")
        os.symlink("/etc/passwd", os.path.join(rice, "hypr", "leak.conf"))
        d = detect(rice, roots=_roots(self.tmp))
        joined = " ".join(d.notes)
        self.assertIn("leak.conf", joined)

    def test_binary_file_skipped(self):
        rice = _mkrice(self.tmp, "r", {
            "hypr/hyprland.conf": "exec-once = waybar\n",
            "hypr/execs.lua": b"\x00\x01\x02\x03",
        })
        d = detect(rice, roots=_roots(self.tmp))
        self.assertIn("waybar", _names(d))

    def test_large_file_skipped(self):
        rice = _mkrice(self.tmp, "r", {
            "hypr/hyprland.conf": "exec-once = waybar\n",
            "hypr/execs.lua": 'hl.exec_cmd("qs -c ii")\n' + "#" * (600 * 1024),
        })
        d = detect(rice, roots=_roots(self.tmp))
        # the huge execs.lua is not parsed -> no quickshell, no crash
        self.assertNotIn("quickshell", _names(d))
        self.assertIn("waybar", _names(d))

    def test_decode_error_skipped(self):
        rice = _mkrice(self.tmp, "r", {
            "hypr/hyprland.conf": "exec-once = waybar\n",
            "hypr/execs.lua": b"\xff\xfe\xfa invalid utf8\n",
        })
        d = detect(rice, roots=_roots(self.tmp))
        self.assertIn("waybar", _names(d))

    def test_malformed_lua_does_not_crash(self):
        rice = _mkrice(self.tmp, "r", {
            "hypr/hyprland.conf": "exec-once = \nbroken = = =\n",
            "hypr/execs.lua": 'hl.exec_cmd("unterminated\nhl.env(\n',
        })
        detect(rice, roots=_roots(self.tmp))  # must not raise

    def test_no_deep_recursion_hang(self):
        rice = _mkrice(self.tmp, "r", {"hypr/hyprland.conf": "exec-once = waybar\n"})
        deep = os.path.join(rice, *(["d"] * 20))
        os.makedirs(deep, exist_ok=True)
        with open(os.path.join(deep, "x.conf"), "w") as f:
            f.write("exec-once = dunst\n")
        d = detect(rice, roots=_roots(self.tmp))
        self.assertIn("waybar", _names(d))


class ProfileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rs-detect-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_unrecognised_tree_warns(self):
        rice = _mkrice(self.tmp, "r", {"readme.txt": "not a rice\n"})
        d = detect(rice, roots=_roots(self.tmp))
        self.assertEqual(d.components, [])
        self.assertTrue(any("hyprland" in n.lower() or "bukan" in n.lower()
                            or "not a" in n.lower() for n in d.notes), d.notes)

    def test_name_is_folder_name(self):
        rice = _mkrice(self.tmp, "myrice", {"hypr/hyprland.conf": ""})
        d = detect(rice, roots=_roots(self.tmp))
        self.assertEqual(d.name, "myrice")


class FixtureIntegrationTest(unittest.TestCase):
    """Assemble a rice out of the shipped fixtures and detect it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rs-detect-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fixture_dirs_detected(self):
        rice = os.path.join(self.tmp, "r")
        os.makedirs(rice)
        for name in ("hyprland", "waybar", "quickshell"):
            src = REPO_ROOT / "fixtures" / name
            dst = os.path.join(rice, name)
            shutil.copytree(src, dst, symlinks=True)
        shutil.copy(REPO_ROOT / "fixtures" / "hyprland.lua",
                    os.path.join(rice, "hyprland.lua"))
        d = detect(rice, roots=_roots(self.tmp))
        names = _names(d)
        self.assertIn("hyprland", names)
        self.assertIn("waybar", names)
        self.assertIn("quickshell", names)
        qs = _by_name(d, "quickshell")
        self.assertEqual(qs.argv_match.value, ["-c", "ii"])
        self.assertEqual(qs.argv_match.confidence, CONF_AUTO)


if __name__ == "__main__":
    unittest.main()
