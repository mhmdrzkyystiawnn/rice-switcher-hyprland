"""Fase 3a Langkah 3: non-interactive review + save for `generate`.

`--yes` accepts the draft defaults but refuses to save while any field is
still `ask` (never silently stores a guess). `--no-tui` runs the line
prompts and saves on `s`. The manifest is serialised with `tomlwrite`,
validated, and only then written — a validation error aborts the write.

Hermetic: HOME plus every root are redirected into a tempdir, so `~`
expansion agrees with the sandbox and nothing outside is touched.
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

from rice_switcher.cli import build_parser, cmd_generate, main
from rice_switcher.detect import DraftComponent, Field, ManifestDraft
from rice_switcher.paths import Roots


class ScriptedAsk:
    def __init__(self, answers):
        self.answers = list(answers)

    def __call__(self, prompt: str) -> str:
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)


class GenerateReviewBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rs-3a-gen-")
        self.home = os.path.join(self.tmp, "home")
        self.rice_home = os.path.join(self.home, "rice")
        self.config_home = os.path.join(self.home, ".config")
        self.state_home = os.path.join(self.home, ".local", "state")
        for d in (self.rice_home, self.config_home, self.state_home):
            os.makedirs(d, exist_ok=True)
        self.env = {
            "HOME": self.home,
            "RICE_HOME": self.rice_home,
            "RICE_CONFIG_HOME": self.config_home,
            "RICE_STATE_HOME": self.state_home,
        }
        self.roots = Roots(self.rice_home, self.config_home, self.state_home)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def mkrice(self, name, files=None):
        rice = os.path.join(self.rice_home, name)
        os.makedirs(rice, exist_ok=True)
        for rel, content in (files or {}).items():
            path = os.path.join(rice, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(content)
        return rice

    def manifest_path(self, name):
        return os.path.join(self.rice_home, name, "rice.toml")

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=False):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = main(list(argv))
        return rc, out.getvalue(), err.getvalue()


class YesPathTest(GenerateReviewBase):
    def test_yes_without_ask_saves(self):
        # `exec` conflicts have a default (Opsi B), so `--yes` applies it and
        # still saves. (`exec-once`/`hl.exec_cmd` would block; see Fase 4.)
        self.mkrice("r", {"hypr/hyprland.conf": "exec = waybar\n"})
        rc, out, err = self.run_cli("generate", "r", "--yes")
        self.assertEqual(rc, 0, out + err)
        mp = self.manifest_path("r")
        self.assertTrue(os.path.isfile(mp))
        data = tomllib.loads(open(mp).read())
        self.assertEqual(data["meta"]["name"], "r")
        names = [c["name"] for c in data["components"]]
        self.assertIn("hyprland", names)
        self.assertIn("waybar", names)
        waybar = next(c for c in data["components"] if c["name"] == "waybar")
        self.assertTrue(waybar["started_by_config"])

    def test_saved_manifest_round_trips_through_tomllib(self):
        self.mkrice("r", {"hypr/hyprland.conf": "exec = waybar\n"})
        rc, _, err = self.run_cli("generate", "r", "--yes")
        self.assertEqual(rc, 0, err)
        text = open(self.manifest_path("r")).read()
        data = tomllib.loads(text)
        self.assertEqual(data["meta"]["name"], "r")
        waybar = next(c for c in data["components"] if c["name"] == "waybar")
        self.assertTrue(waybar["exact_argv"])
        self.assertEqual(waybar["stop"], "")
        self.assertTrue(waybar["started_by_config"])

    def test_yes_with_ask_stops_and_does_not_write(self):
        self.mkrice("r", {"hypr/hyprland.conf": "exec-once = qs -c $unknownCfg\n"})
        rc, out, err = self.run_cli("generate", "r", "--yes")
        self.assertNotEqual(rc, 0)
        self.assertFalse(os.path.exists(self.manifest_path("r")))
        self.assertIn("quickshell", err)
        self.assertIn("argv_match", err)

    def test_guess_field_does_not_block_yes(self):
        # A `hypr/` dir with no readable entrypoint yields a compositor
        # whose config_link is `guess` (advisory) and nothing is `ask`, so
        # --yes may save.
        self.mkrice("r", {"hypr/keep.conf": "# no entrypoint\n"})
        rc, out, err = self.run_cli("generate", "r", "--yes")
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(os.path.isfile(self.manifest_path("r")))
        data = tomllib.loads(open(self.manifest_path("r")).read())
        self.assertIn("hyprland", [c["name"] for c in data["components"]])

    def test_validate_error_does_not_write(self):
        self.mkrice("r", {"hypr/hyprland.conf": ""})
        bad = ManifestDraft(name="r", components=[
            DraftComponent(name="broken", type=Field("bar"), start=Field("")),
        ])
        with mock.patch("rice_switcher.detect.detect", return_value=bad):
            rc, out, err = self.run_cli("generate", "r", "--yes")
        self.assertNotEqual(rc, 0, out + err)
        self.assertFalse(os.path.exists(self.manifest_path("r")))
        self.assertIn("tidak ditulis", (out + err).lower())

    def test_meta_name_equals_folder_name(self):
        self.mkrice("folder-name", {"hypr/hyprland.conf": "exec = waybar\n"})
        rc, _, err = self.run_cli("generate", "folder-name", "--yes")
        self.assertEqual(rc, 0, err)
        data = tomllib.loads(open(self.manifest_path("folder-name")).read())
        self.assertEqual(data["meta"]["name"], "folder-name")

    def test_refuses_to_overwrite_existing(self):
        rice = self.mkrice("r", {"hypr/hyprland.conf": "exec = waybar\n"})
        mp = os.path.join(rice, "rice.toml")
        with open(mp, "w") as f:
            f.write("# original\n")
        rc, out, err = self.run_cli("generate", "r", "--yes")
        self.assertNotEqual(rc, 0)
        with open(mp) as f:
            self.assertEqual(f.read(), "# original\n")


class NoTuiPathTest(GenerateReviewBase):
    def _run_direct(self, argv, answers):
        args = build_parser().parse_args(argv)
        out: list[str] = []
        so, se = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=False):
            with contextlib.redirect_stdout(so), contextlib.redirect_stderr(se):
                rc = cmd_generate(args, self.roots, None,
                                  ask=ScriptedAsk(answers), out=out.append)
        return rc, "\n".join(out) + so.getvalue() + se.getvalue()

    def test_interactive_save(self):
        self.mkrice("r", {"hypr/hyprland.conf": "exec-once = waybar\n"})
        rc, text = self._run_direct(["generate", "r", "--no-tui"], ["s"])
        self.assertEqual(rc, 0, text)
        self.assertTrue(os.path.isfile(self.manifest_path("r")))

    def test_interactive_cancel_does_not_write(self):
        self.mkrice("r", {"hypr/hyprland.conf": "exec-once = waybar\n"})
        rc, text = self._run_direct(["generate", "r", "--no-tui"], ["q"])
        self.assertEqual(rc, 1, text)
        self.assertFalse(os.path.exists(self.manifest_path("r")))

    def test_interactive_eof_does_not_write(self):
        self.mkrice("r", {"hypr/hyprland.conf": "exec-once = waybar\n"})
        rc, text = self._run_direct(["generate", "r", "--no-tui"], [])
        self.assertEqual(rc, 1, text)
        self.assertFalse(os.path.exists(self.manifest_path("r")))

    def test_interactive_edit_then_save(self):
        self.mkrice("r", {"hypr/hyprland.conf": "exec-once = waybar\n"})
        rc, text = self._run_direct(
            ["generate", "r", "--no-tui"],
            ["e", "waybar", "start", "waybar -c custom", "s"],
        )
        self.assertEqual(rc, 0, text)
        data = tomllib.loads(open(self.manifest_path("r")).read())
        waybar = next(c for c in data["components"] if c["name"] == "waybar")
        self.assertEqual(waybar["start"], "waybar -c custom")

    def test_interactive_ask_edit_then_save(self):
        # Resolving the `ask` field by hand clears it and allows the save.
        self.mkrice("r", {"hypr/hyprland.conf": "exec-once = qs -c $unknownCfg\n"})
        rc, text = self._run_direct(
            ["generate", "r", "--no-tui"],
            ["e", "quickshell", "argv_match", "-c ii", "s"],
        )
        self.assertEqual(rc, 0, text)
        data = tomllib.loads(open(self.manifest_path("r")).read())
        qs = next(c for c in data["components"] if c["name"] == "quickshell")
        self.assertEqual(qs["argv_match"], ["-c", "ii"])


if __name__ == "__main__":
    unittest.main()
