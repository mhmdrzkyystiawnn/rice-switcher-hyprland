"""Fase 4 Langkah 2: the detector fills `ManifestDraft.findings`.

Read-only, hermetic: tempdir rice trees. Findings must come only from
*reachable* autostart lines; archive/backup files and dir-signal-only
components never create an `autostart_conflict`.
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

from rice_switcher.detect import detect
from rice_switcher.gaps import (
    AUTOSTART_CONFLICT,
    NEEDS_MATERIALIZATION,
    OPTION_CONFIG,
)
from rice_switcher.paths import Roots


def _roots(base):
    return Roots(rice_home=f"{base}/rice", config_home=f"{base}/config",
                 state_home=f"{base}/state")


def _mkrice(tmp, name, files):
    rice = os.path.join(tmp, name)
    for rel, content in files.items():
        path = os.path.join(rice, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
    return rice


def _findings(draft, kind=None):
    out = [f for f in draft.findings if kind is None or f.kind == kind]
    return out


class AutostartConflictFindingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rs-4-detect-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_exec_conflict_has_default_option_config(self):
        rice = _mkrice(self.tmp, "r", {"hypr/hyprland.conf": "exec = waybar\n"})
        d = detect(rice, roots=_roots(self.tmp))
        fs = _findings(d, AUTOSTART_CONFLICT)
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0].component, "waybar")
        self.assertEqual(fs[0].default_option, OPTION_CONFIG)
        self.assertEqual(fs[0].refs[0][2], "exec")

    def test_exec_once_conflict_has_no_default(self):
        rice = _mkrice(self.tmp, "r", {"hypr/hyprland.conf": "exec-once = waybar\n"})
        d = detect(rice, roots=_roots(self.tmp))
        fs = _findings(d, AUTOSTART_CONFLICT)
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0].default_option, "")

    def test_hl_exec_cmd_conflict(self):
        rice = _mkrice(self.tmp, "r", {
            "hypr/hyprland.lua": 'hl.exec_cmd("swww-daemon")\n',
        })
        d = detect(rice, roots=_roots(self.tmp))
        fs = _findings(d, AUTOSTART_CONFLICT)
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0].component, "swww")
        self.assertEqual(fs[0].refs[0][2], "hl.exec_cmd")
        self.assertEqual(fs[0].default_option, "")

    def test_dir_signal_only_makes_no_conflict(self):
        rice = _mkrice(self.tmp, "r", {
            "hypr/hyprland.conf": "general { gaps_in = 2 }\n",
            "waybar/config.jsonc": "{}\n",
        })
        d = detect(rice, roots=_roots(self.tmp))
        self.assertEqual(_findings(d, AUTOSTART_CONFLICT), [])

    def test_archive_line_makes_no_conflict(self):
        rice = _mkrice(self.tmp, "r", {
            "hypr/hyprland.conf": "general { gaps_in = 2 }\n",
            "archive/old/hyprland.conf": "exec = waybar\n",
        })
        d = detect(rice, roots=_roots(self.tmp))
        self.assertEqual(_findings(d, AUTOSTART_CONFLICT), [])

    def test_clean_rice_has_no_findings(self):
        rice = _mkrice(self.tmp, "r", {
            "hypr/hyprland.conf": "general { gaps_in = 2 }\n",
        })
        d = detect(rice, roots=_roots(self.tmp))
        self.assertEqual(d.findings, [])


class NeedsMaterializationFindingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rs-4-mat-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_profile_without_entrypoint(self):
        rice = _mkrice(self.tmp, "r", {
            "profiles/ii.conf": "exec = waybar\n",
        })
        d = detect(rice, roots=_roots(self.tmp))
        fs = _findings(d, NEEDS_MATERIALIZATION)
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0].path, rice)

    def test_apply_script_without_entrypoint(self):
        rice = _mkrice(self.tmp, "r", {
            "hypr-profile.sh": 'case "$1" in\napply) echo hi;;\nesac\n',
        })
        d = detect(rice, roots=_roots(self.tmp))
        self.assertEqual(len(_findings(d, NEEDS_MATERIALIZATION)), 1)

    def test_entrypoint_present_no_materialization(self):
        rice = _mkrice(self.tmp, "r", {
            "hypr/hyprland.conf": "general { gaps_in = 2 }\n",
            "profiles/ii.conf": "exec = waybar\n",
        })
        d = detect(rice, roots=_roots(self.tmp))
        self.assertEqual(_findings(d, NEEDS_MATERIALIZATION), [])

    def test_apply_script_is_never_executed(self):
        # The materialisation finding is *reported*, never acted on: the
        # detector must not run the rice's `apply` script (or any process).
        rice = _mkrice(self.tmp, "r", {
            "profiles/ii.conf": "exec = waybar\n",
            "hypr-profile.sh": (
                "case \"$1\" in\n"
                "apply) touch " + os.path.join(self.tmp, "PWNED") + ";;\n"
                "esac\n"
            ),
        })
        marker = os.path.join(self.tmp, "PWNED")
        with mock.patch("subprocess.run", side_effect=AssertionError("run")), \
             mock.patch("subprocess.Popen", side_effect=AssertionError("Popen")), \
             mock.patch("subprocess.check_output",
                        side_effect=AssertionError("check_output")), \
             mock.patch("os.system", side_effect=AssertionError("system")), \
             mock.patch("os.popen", side_effect=AssertionError("popen")), \
             mock.patch("os.execv", side_effect=AssertionError("execv")), \
             mock.patch("os.spawnv", side_effect=AssertionError("spawnv")):
            d = detect(rice, roots=_roots(self.tmp))
        # the finding fired (so the scan really saw the script) ...
        self.assertEqual(len(_findings(d, NEEDS_MATERIALIZATION)), 1)
        # ... yet nothing was executed.
        self.assertFalse(os.path.exists(marker))


if __name__ == "__main__":
    unittest.main()
