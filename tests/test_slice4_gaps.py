"""Fase 4 Langkah 1: gap finding model + pure classifiers (DESIGN §6.1).

Hermetic: fixtures are tempdir trees; nothing is executed. The classifiers
must only *report* — a rice script with an `apply` subcommand is never run.
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

from rice_switcher.detect import AutostartRef
from rice_switcher.gaps import (
    AUTOSTART_CONFLICT,
    NEEDS_MATERIALIZATION,
    OPTION_CONFIG,
    OPTION_SWITCHER,
    autostart_conflict,
    autostart_conflicts,
    collect_findings,
    materialisation_signals,
    needs_materialization,
)


def _ref(kind="exec", file="hypr/hyprland.conf", line=1, command="waybar",
         reachable=True):
    return AutostartRef(file, line, kind, f"{kind} = {command}", command,
                        "auto", reachable)


def _write(base, files):
    for rel, content in files.items():
        path = os.path.join(base, rel)
        if rel.endswith("/"):
            os.makedirs(path, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)


class ConflictFindingTest(unittest.TestCase):
    def test_exec_defaults_to_option_config(self):
        f = autostart_conflict("waybar", [_ref("exec")])
        self.assertIsNotNone(f)
        self.assertEqual(f.kind, AUTOSTART_CONFLICT)
        self.assertEqual(f.component, "waybar")
        self.assertEqual(f.default_option, OPTION_CONFIG)
        self.assertEqual(f.options, [OPTION_CONFIG, OPTION_SWITCHER])
        self.assertEqual(f.refs, [("hypr/hyprland.conf", 1, "exec")])
        self.assertFalse(f.is_resolved)
        self.assertIn("neutralise", f.detail)

    def test_exec_once_has_no_default(self):
        f = autostart_conflict("waybar", [_ref("exec-once")])
        self.assertEqual(f.default_option, "")

    def test_hl_exec_cmd_has_no_default(self):
        f = autostart_conflict("quickshell", [_ref("hl.exec_cmd")])
        self.assertEqual(f.default_option, "")

    def test_mixed_exec_and_exec_once_has_no_default(self):
        f = autostart_conflict("waybar", [_ref("exec", line=1),
                                          _ref("exec-once", line=2)])
        self.assertEqual(f.default_option, "")
        self.assertEqual(len(f.refs), 2)

    def test_unreachable_ref_yields_no_finding(self):
        self.assertIsNone(autostart_conflict("waybar", [_ref(reachable=False)]))

    def test_no_refs_yields_no_finding(self):
        self.assertIsNone(autostart_conflict("waybar", []))

    def test_batch(self):
        out = autostart_conflicts({
            "waybar": [_ref("exec")],
            "archived": [_ref(reachable=False)],
        })
        self.assertEqual([f.component for f in out], ["waybar"])


class MaterialisationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rs-4-gaps-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _rice(self, files):
        rice = os.path.join(self.tmp, "r")
        _write(rice, files)
        return rice

    def test_profiles_dir_triggers(self):
        rice = self._rice({"profiles/a.conf": "exec = waybar\n"})
        f = needs_materialization(rice, has_entrypoint=False)
        self.assertIsNotNone(f)
        self.assertEqual(f.kind, NEEDS_MATERIALIZATION)
        self.assertEqual(f.path, rice)
        self.assertIn("profiles/", f.detail)

    def test_settings_lua_triggers(self):
        rice = self._rice({"settings.lua": "return {}\n"})
        self.assertIsNotNone(needs_materialization(rice, has_entrypoint=False))

    def test_variables_lua_triggers(self):
        rice = self._rice({"variables.lua": "return {}\n"})
        self.assertIsNotNone(needs_materialization(rice, has_entrypoint=False))

    def test_apply_script_triggers(self):
        rice = self._rice({"hypr-profile.sh": "case \"$1\" in\napply) echo hi;;\nesac\n"})
        f = needs_materialization(rice, has_entrypoint=False)
        self.assertIsNotNone(f)
        self.assertIn("hypr-profile.sh", f.detail)

    def test_script_without_apply_does_not_trigger(self):
        rice = self._rice({"build.sh": "echo hello\n"})
        self.assertIsNone(needs_materialization(rice, has_entrypoint=False))

    def test_entrypoint_present_does_not_trigger(self):
        rice = self._rice({"profiles/a.conf": "x",
                           "hypr/hyprland.conf": "exec = waybar\n"})
        self.assertIsNone(needs_materialization(rice, has_entrypoint=True))

    def test_clean_rice_does_not_trigger(self):
        rice = self._rice({"hypr/hyprland.conf": "exec = waybar\n"})
        self.assertIsNone(needs_materialization(rice, has_entrypoint=False))

    def test_archive_only_signal_does_not_trigger(self):
        rice = self._rice({"archive/old/hypr-profile.sh":
                           "case \"$1\" in\napply) echo hi;;\nesac\n"})
        self.assertIsNone(needs_materialization(rice, has_entrypoint=False))

    def test_signals_listing(self):
        rice = self._rice({"profiles/a.conf": "x", "settings.lua": "y"})
        sig = materialisation_signals(rice)
        self.assertIn("profiles/", sig)
        self.assertIn("settings.lua", sig)


class CollectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rs-4-collect-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_collect_both_kinds(self):
        rice = os.path.join(self.tmp, "r")
        _write(rice, {"profiles/a.conf": "x"})
        findings = collect_findings(
            rice, has_entrypoint=False,
            autostart_refs_by_name={"waybar": [_ref("exec")]},
        )
        kinds = sorted(f.kind for f in findings)
        self.assertEqual(kinds, [AUTOSTART_CONFLICT, NEEDS_MATERIALIZATION])

    def test_collect_clean_is_empty(self):
        rice = os.path.join(self.tmp, "r")
        _write(rice, {"hypr/hyprland.conf": "exec = waybar\n"})
        findings = collect_findings(
            rice, has_entrypoint=True, autostart_refs_by_name={})
        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
