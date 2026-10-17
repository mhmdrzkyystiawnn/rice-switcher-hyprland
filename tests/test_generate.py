"""Tests for the `generate` command (Fase 2, DESIGN §4.2 / §2.4).

Run through `main()` with RICE_HOME / RICE_CONFIG_HOME / RICE_STATE_HOME
redirected to a tempdir, so nothing outside it is touched.
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

THIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from rice_switcher.cli import main
from rice_switcher.manifest import load_manifest


def _write(base: str, files: dict) -> None:
    for rel, content in files.items():
        path = os.path.join(base, rel)
        if rel.endswith("/"):
            os.makedirs(path, exist_ok=True)
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)


class GenerateTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rs-generate-")
        self.rice_home = os.path.join(self.tmp, "rice")
        self.config_home = os.path.join(self.tmp, "config")
        self.state_home = os.path.join(self.tmp, "state")
        os.makedirs(self.rice_home, exist_ok=True)
        os.makedirs(self.config_home, exist_ok=True)
        os.makedirs(self.state_home, exist_ok=True)
        self.env = {
            "RICE_HOME": self.rice_home,
            "RICE_CONFIG_HOME": self.config_home,
            "RICE_STATE_HOME": self.state_home,
        }

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def mkrice(self, name="r", files=None):
        rice = os.path.join(self.rice_home, name)
        os.makedirs(rice, exist_ok=True)
        _write(rice, files or {})
        return rice

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=False):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def audit_text(self):
        path = os.path.join(self.state_home, "rice-switcher", "logs", "audit.log")
        if not os.path.isfile(path):
            return ""
        with open(path) as f:
            return f.read()


class GeneratePrintTest(GenerateTestBase):
    def test_default_prints_parseable_toml_and_writes_nothing(self):
        self.mkrice("r", {
            "hypr/hyprland.conf": "exec-once = waybar\n",
            "waybar/config.jsonc": "{}\n",
        })
        rc, out, err = self.run_cli("generate", "r")
        self.assertEqual(rc, 0, err)
        data = tomllib.loads(out)
        self.assertEqual(data["meta"]["name"], "r")
        self.assertNotIn("description", data["meta"])
        names = [c["name"] for c in data["components"]]
        self.assertIn("hyprland", names)
        self.assertIn("waybar", names)
        self.assertFalse(os.path.exists(os.path.join(self.rice_home, "r", "rice.toml")))

    def test_stop_is_emitted_empty_for_identity_stop(self):
        self.mkrice("r", {"hypr/hyprland.conf": "exec-once = waybar\n"})
        rc, out, err = self.run_cli("generate", "r")
        self.assertEqual(rc, 0, err)
        self.assertIn('stop = ""', out)
        data = tomllib.loads(out)
        waybar = next(c for c in data["components"] if c["name"] == "waybar")
        self.assertEqual(waybar["stop"], "")

    def test_ask_fields_surface_as_todo_and_review(self):
        # An unresolved variable is genuinely ambiguous -> ask.
        self.mkrice("r", {"hypr/hyprland.conf": "exec-once = qs -c $unknownCfg\n"})
        rc, out, err = self.run_cli("generate", "r")
        self.assertEqual(rc, 0, err)
        self.assertIn("TODO(ask)", out)
        self.assertIn("review", err.lower())

    def test_quickshell_identity_is_auto(self):
        self.mkrice("r", {
            "hypr/variables.lua": 'hl.env("qsConfig", "ii")\n',
            "hypr/execs.lua": 'hl.exec_cmd("qs -c $qsConfig")\n',
            "quickshell/ii/shell.qml": "// x\n",
        })
        rc, out, err = self.run_cli("generate", "r")
        self.assertEqual(rc, 0, err)
        data = tomllib.loads(out)
        qs = next(c for c in data["components"] if c["name"] == "quickshell")
        self.assertEqual(qs["executable"], "qs")
        self.assertEqual(qs["argv_match"], ["-c", "ii"])

    def test_missing_rice_is_error(self):
        rc, out, err = self.run_cli("generate", "nope")
        self.assertEqual(rc, 2)
        self.assertIn("no rice", err.lower())

    def test_invalid_name_is_error(self):
        rc, out, err = self.run_cli("generate", "..", "--write")
        self.assertEqual(rc, 2)


class GenerateWriteTest(GenerateTestBase):
    def test_write_creates_manifest(self):
        self.mkrice("r", {"hypr/hyprland.conf": "exec-once = waybar\n"})
        rc, out, err = self.run_cli("generate", "r", "--write")
        self.assertEqual(rc, 0, err)
        mp = os.path.join(self.rice_home, "r", "rice.toml")
        self.assertTrue(os.path.isfile(mp))
        m = load_manifest(mp)
        self.assertEqual(m.meta.name, "r")
        self.assertIn("waybar", [c.name for c in m.components])

    def test_write_refuses_when_manifest_exists(self):
        self.mkrice("r", {"hypr/hyprland.conf": ""})
        mp = os.path.join(self.rice_home, "r", "rice.toml")
        with open(mp, "w") as f:
            f.write("# original\n")
        rc, out, err = self.run_cli("generate", "r", "--write")
        self.assertEqual(rc, 2)
        self.assertIn("exist", err.lower())
        with open(mp) as f:
            self.assertEqual(f.read(), "# original\n")

    def test_write_applies_layout_normalization(self):
        self.mkrice("r", {
            ".config/hypr/hyprland.conf": "exec-once = waybar\n",
            ".config/waybar/config.jsonc": "{}\n",
        })
        rc, out, err = self.run_cli("generate", "r", "--write")
        self.assertEqual(rc, 0, err)
        rice = os.path.join(self.rice_home, "r")
        self.assertTrue(os.path.islink(os.path.join(rice, "hypr")))
        self.assertEqual(os.readlink(os.path.join(rice, "hypr")), ".config/hypr")
        self.assertTrue(os.path.islink(os.path.join(rice, "waybar")))

    def test_write_audits(self):
        self.mkrice("r", {"hypr/hyprland.conf": ""})
        rc, out, err = self.run_cli("generate", "r", "--write")
        self.assertEqual(rc, 0, err)
        self.assertIn("generate", self.audit_text())

    def test_write_reports_validation(self):
        self.mkrice("r", {"hypr/hyprland.conf": "exec-once = waybar\n"})
        rc, out, err = self.run_cli("generate", "r", "--write")
        self.assertEqual(rc, 0, err)
        combined = (out + err).lower()
        self.assertIn("valid", combined)

    def test_second_write_is_refused(self):
        self.mkrice("r", {"hypr/hyprland.conf": ""})
        rc1, _, _ = self.run_cli("generate", "r", "--write")
        self.assertEqual(rc1, 0)
        mp = os.path.join(self.rice_home, "r", "rice.toml")
        with open(mp) as f:
            first = f.read()
        rc2, _, err2 = self.run_cli("generate", "r", "--write")
        self.assertEqual(rc2, 2)
        with open(mp) as f:
            self.assertEqual(f.read(), first)


if __name__ == "__main__":
    unittest.main()
