"""Fase 3a Langkah 0: a one-shot *client* command is not a daemon.

`swww restore` / `swww img …` are client invocations of an already-running
daemon (`swww-daemon`); using them to derive a managed component's process
identity is unreliable, so the detector must mark the identity `ask` (never
`auto`) and attach a note. The `swww-daemon` command itself stays `auto`.

Hermetic: tempdir trees only; `detect` is read-only.
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

from rice_switcher.detect import CONF_ASK, CONF_AUTO, detect
from rice_switcher.paths import Roots


def _roots(base: str) -> Roots:
    return Roots(
        rice_home=os.path.join(base, "rice"),
        config_home=os.path.join(base, "config"),
        state_home=os.path.join(base, "state"),
    )


def _mkrice(tmp: str, name: str, files: dict) -> str:
    rice = os.path.join(tmp, name)
    for rel, content in files.items():
        path = os.path.join(rice, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
    return rice


def _by_name(draft, name):
    for c in draft.components:
        if c.name == name:
            return c
    raise AssertionError(f"no component {name!r}; have {[c.name for c in draft.components]}")


class ClientSubcommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rs-3a-cli-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_swww_restore_is_client_ask(self):
        rice = _mkrice(self.tmp, "r", {"hypr/hyprland.conf": "exec-once = swww restore\n"})
        d = detect(rice, roots=_roots(self.tmp))
        c = _by_name(d, "swww")
        self.assertEqual(c.argv_match.value, ["restore"])
        self.assertEqual(c.argv_match.confidence, CONF_ASK)
        self.assertEqual(c.executable.value, "swww")
        self.assertIn("klien", c.argv_match.note.lower())

    def test_swww_img_is_client_ask(self):
        rice = _mkrice(self.tmp, "r", {
            "hypr/hyprland.conf": "exec-once = swww img /wallpapers/a.png\n",
        })
        d = detect(rice, roots=_roots(self.tmp))
        c = _by_name(d, "swww")
        self.assertEqual(c.argv_match.value, ["img", "/wallpapers/a.png"])
        self.assertEqual(c.argv_match.confidence, CONF_ASK)

    def test_awww_restore_is_client_ask(self):
        rice = _mkrice(self.tmp, "r", {"hypr/hyprland.conf": "exec-once = awww restore\n"})
        d = detect(rice, roots=_roots(self.tmp))
        c = _by_name(d, "awww")
        self.assertEqual(c.argv_match.confidence, CONF_ASK)

    def test_swww_daemon_identity_stays_auto(self):
        # The real daemon invocation keeps a confident (auto) identity.
        rice = _mkrice(self.tmp, "r", {"hypr/hyprland.conf": "exec-once = swww-daemon\n"})
        d = detect(rice, roots=_roots(self.tmp))
        c = _by_name(d, "swww")
        self.assertEqual(c.executable.value, "swww-daemon")
        self.assertTrue(c.exact_argv.value)
        self.assertEqual(c.exact_argv.confidence, CONF_AUTO)
        # a literal no-arg daemon command is strong evidence -> auto
        self.assertEqual(c.argv_match.confidence, CONF_AUTO)
        self.assertEqual(c.argv_match.note, "")

    def test_swww_img_no_args_exact_flag_not_set(self):
        # A client subcommand must never be turned into a no-arg exact identity.
        rice = _mkrice(self.tmp, "r", {"hypr/hyprland.conf": "exec-once = swww restore\n"})
        d = detect(rice, roots=_roots(self.tmp))
        c = _by_name(d, "swww")
        self.assertFalse(c.exact_argv.value)


if __name__ == "__main__":
    unittest.main()
