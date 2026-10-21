"""Regression tests for rollback integrity and symlink preservation.

Verifies:
1. A failed switch at the 'stop' phase (before swap_symlink) NEVER consumes
   stale .bak-* files from earlier switches, leaving the current live symlink intact.
2. A live symlink captured in a snapshot is restored strictly as a symlink
   pointing to the original target, never converted to a real directory or nested symlink.
3. find_pids_by_identity matches process executable names case-insensitively (e.g. 'hyprland' vs 'Hyprland').
"""

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

from rice_switcher.paths import Roots
from rice_switcher.manifest import Manifest, Component, Meta
from rice_switcher.process import find_pids_by_identity, pid_matches_identity
from rice_switcher.snapshot import (
    create_pending, commit, restore, swap_symlink, undo_swap
)
from rice_switcher.switch import do_switch


class RollbackStaleBackupIsolationTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="rs-rollback-stale-")
        self.roots = Roots(
            rice_home=os.path.join(self._td.name, "rice"),
            config_home=os.path.join(self._td.name, "config"),
            state_home=os.path.join(self._td.name, "state"),
        )
        for d in (self.roots.rice_home, self.roots.config_home, self.roots.state_home):
            os.makedirs(d, exist_ok=True)

    def tearDown(self):
        self._td.cleanup()

    def test_failed_stop_does_not_consume_old_bak_file(self):
        """When a switch fails before swap_symlink runs (e.g. a failing stop command),
        the rollback handler must NOT call undo_swap and must NOT consume old .bak-* files.
        """
        # Create live config link
        live = os.path.join(self.roots.config_home, "hypr")
        rice_a = os.path.join(self.roots.rice_home, "a", "hypr")
        os.makedirs(rice_a, exist_ok=True)
        Path(rice_a, "hyprland.lua").write_text("-- rice a")
        os.symlink(rice_a, live)

        # Simulate an old stale .bak file from a previous switch days ago
        stale_bak = f"{live}.bak-20261001T000000_000"
        os.makedirs(stale_bak, exist_ok=True)
        Path(stale_bak, "stale.txt").write_text("STALE_DATA")

        # Create target rice B whose stop command fails
        rice_b = os.path.join(self.roots.rice_home, "b", "hypr")
        os.makedirs(rice_b, exist_ok=True)
        Path(rice_b, "hyprland.lua").write_text("-- rice b")

        manifests = [
            Manifest(
                meta=Meta(name="a"),
                components=[
                    Component(name="hyprland", type="compositor", config_link=live),
                    Component(name="failing_proc", type="bar", stop="false", executable="failing_proc"),
                ],
                source_path=os.path.join(self.roots.rice_home, "a", "rice.toml"),
            ),
            Manifest(
                meta=Meta(name="b"),
                components=[
                    Component(name="hyprland", type="compositor", config_link=live),
                    Component(name="failing_proc", type="bar", stop="false", executable="failing_proc"),
                ],
                source_path=os.path.join(self.roots.rice_home, "b", "rice.toml"),
            ),
        ]

        # Fake runtime where 'false' returns exit code 1
        class FakeRuntime:
            def run(self, cmd, timeout=10):
                class Res:
                    rc = 1 if "false" in cmd else 0
                    timed_out = False
                    stdout = ""
                    stderr = "command failed"
                return Res()

        # Run switch from a to b; it should fail on stop and rollback
        from rice_switcher.state import read_state, write_state
        st = read_state(self.roots)
        st.current_rice = "a"
        write_state(self.roots, st)

        outcome = do_switch(
            self.roots, "b", manifests,
            dry_run=False, force_real=True, logger=None,
            runtime=FakeRuntime(),
        )
        self.assertTrue(len(outcome.errors) > 0, "switch should fail due to stop failure")

        # Crucial assertions:
        # 1. Live link must STILL be a symlink to rice A!
        self.assertTrue(os.path.islink(live), "live path should remain a symlink")
        self.assertEqual(os.readlink(live), rice_a, "live symlink must still point to rice A")

        # 2. Stale .bak file must NOT have been moved or touched!
        self.assertTrue(os.path.exists(stale_bak), "stale .bak file must not be consumed")


class SymlinkSnapshotPreservationTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="rs-snap-symlink-")
        self.roots = Roots(
            rice_home=os.path.join(self._td.name, "rice"),
            config_home=os.path.join(self._td.name, "config"),
            state_home=os.path.join(self._td.name, "state"),
        )
        for d in (self.roots.rice_home, self.roots.config_home, self.roots.state_home):
            os.makedirs(d, exist_ok=True)

    def tearDown(self):
        self._td.cleanup()

    def test_live_symlink_preserved_as_symlink_on_restore(self):
        live = os.path.join(self.roots.config_home, "hypr")
        rice_target = os.path.join(self.roots.rice_home, "alpha", "hypr")
        os.makedirs(rice_target, exist_ok=True)
        Path(rice_target, "hyprland.lua").write_text("HYPR_CONFIG")
        os.symlink(rice_target, live)

        ts, captured = create_pending(self.roots, [live], rice_name="alpha")
        commit(self.roots, ts)

        # Mutate live into a new symlink to beta
        os.unlink(live)
        rice_beta = os.path.join(self.roots.rice_home, "beta", "hypr")
        os.makedirs(rice_beta, exist_ok=True)
        Path(rice_beta, "hyprland.lua").write_text("BETA_CONFIG")
        os.symlink(rice_beta, live)

        # Restore from snapshot
        restored, r_name = restore(self.roots, ts)
        self.assertEqual(r_name, "alpha")
        self.assertEqual(restored, [live])

        # Assert: live is a symlink directly to alpha, NOT a dir and NOT containing a nested symlink!
        self.assertTrue(os.path.islink(live), "restored config_link must be a symlink")
        self.assertEqual(os.readlink(live), rice_target)
        self.assertEqual(Path(live, "hyprland.lua").read_text(), "HYPR_CONFIG")


class ProcessCaseInsensitiveTest(unittest.TestCase):
    def test_matches_hyprland_case_insensitively(self):
        with mock.patch("rice_switcher.process.list_all_pids", return_value=[9999]), \
             mock.patch("rice_switcher.process.read_cmdline", return_value=["/usr/bin/Hyprland", "--watchdog-fd", "4"]):
            pids = find_pids_by_identity("hyprland", exclude_self=False)
            self.assertEqual(pids, [9999], "executable 'hyprland' should match running 'Hyprland'")
            self.assertTrue(pid_matches_identity(9999, "hyprland"))


if __name__ == "__main__":
    unittest.main()
