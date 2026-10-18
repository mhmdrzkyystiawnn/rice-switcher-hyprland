"""Fase 5 Langkah 3: hermetic end-to-end for `clean-backups`.

Two real switches (``dry_run=False``, ``manage_processes=False``) inside a
tempdir produce two tool backups:

* the first swap preserves the pre-existing *real directory* that was the
  live ``config_link`` (``hypr.bak-<ts>`` and kind ``dir``);
* the second swap preserves the *symlink* the first swap left behind
  (kind ``symlink``).

`clean-backups --keep 1 --yes` must then remove the older backup, keep the
newest, leave the active rice / state untouched, and the committed snapshot
fallback must still be present.

Nothing touches the real home: HOME and all RICE_* roots point into the
tempdir, RICE_TEST=1 keeps lifecycle steps off, and `manage_processes=False`
is passed explicitly.
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

THIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from rice_switcher.backups import list_backups
from rice_switcher.cli import main
from rice_switcher.manifest import load_manifest
from rice_switcher.paths import Roots
from rice_switcher.snapshot import list_committed, snapshot_dir
from rice_switcher.state import State, read_state, write_state
from rice_switcher.switch import do_switch


class CleanBackupsE2E(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="rs-5-e2e-")
        self.base = self._td.name
        self.home = os.path.join(self.base, "home")
        self.rice_home = os.path.join(self.home, "rices")
        self.config_home = os.path.join(self.home, ".config")
        self.state_home = os.path.join(self.home, ".local", "state")
        for d in (self.rice_home, self.config_home, self.state_home):
            os.makedirs(d, exist_ok=True)
        self.roots = Roots(self.rice_home, self.config_home, self.state_home)
        self.env = {
            "HOME": self.home,
            "RICE_HOME": self.rice_home,
            "RICE_CONFIG_HOME": self.config_home,
            "RICE_STATE_HOME": self.state_home,
            "RICE_TEST": "1",
        }
        self.live = os.path.join(self.config_home, "hypr")

    def tearDown(self):
        self._td.cleanup()

    def _make_rice(self, name):
        d = os.path.join(self.rice_home, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "rice.toml"), "w") as f:
            f.write(
                f'[meta]\nname = "{name}"\n'
                f'[[components]]\nname = "hyprland"\ntype = "compositor"\n'
                f'start = "true"\nstop = "true"\n'
                f'config_link = "{self.live}"\n'
            )
        rh = os.path.join(d, "hypr")
        os.makedirs(rh, exist_ok=True)
        with open(os.path.join(rh, "h.txt"), "w") as f:
            f.write(f"v_{name}\n")
        return os.path.join(d, "rice.toml")

    def _switch(self, name, manifests):
        out = do_switch(
            self.roots, name, manifests,
            dry_run=False, force_real=True, manage_processes=False, logger=None,
        )
        self.assertEqual(out.errors, [], f"switch to {name} failed: {out.errors}")

    def _run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=False):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_keep_one_preserves_state_and_snapshot_fallback(self):
        mp_a = self._make_rice("a")
        mp_b = self._make_rice("b")
        manifests = [load_manifest(mp_a), load_manifest(mp_b)]

        # The live config_link starts as a REAL directory, so the first
        # swap backs up a real dir.
        os.makedirs(self.live, exist_ok=True)
        with open(os.path.join(self.live, "pre.conf"), "w") as f:
            f.write("original\n")
        write_state(self.roots, State(current_rice="a", last_switch=0.0))

        self._switch("b", manifests)
        time.sleep(0.02)  # distinct millisecond timestamps
        self._switch("a", manifests)

        backups = list_backups(self.config_home)
        self.assertEqual(len(backups), 2, [b.path for b in backups])
        kinds = {b.kind for b in backups}
        self.assertEqual(kinds, {"dir", "symlink"},
                         "expected one real-dir and one symlink backup")
        newest = backups[0].path
        older = backups[1].path
        self.assertEqual(backups[0].kind, "symlink")

        # Active rice before cleanup.
        self.assertEqual(read_state(self.roots).current_rice, "a")
        active_target = os.readlink(self.live)
        self.assertIn("/a/hypr", active_target)

        rc, out, err = self._run_cli("clean-backups", "--keep", "1", "--yes",
                                    "--include-real-dirs")
        self.assertEqual(rc, 0, err or out)

        remaining = list_backups(self.config_home)
        self.assertEqual([b.path for b in remaining], [newest],
                         "only the newest backup should remain")
        self.assertEqual(remaining[0].kind, "symlink")
        self.assertFalse(os.path.lexists(older), "the older backup is gone")

        # State and the active rice's live link are untouched.
        self.assertEqual(read_state(self.roots).current_rice, "a")
        self.assertEqual(os.readlink(self.live), active_target)

        # Rollback fallback survives: a committed snapshot exists and
        # still captures the config_link.
        committed = list_committed(self.roots)
        self.assertTrue(committed, "a committed snapshot must exist as fallback")
        snap = os.path.join(snapshot_dir(self.roots), committed[0])
        self.assertTrue(os.path.isdir(os.path.join(snap, "hypr")),
                        f"snapshot should contain the captured hypr link: {snap}")
        self.assertTrue(os.path.lexists(os.path.join(snap, ".captured.txt")))


if __name__ == "__main__":
    unittest.main()
