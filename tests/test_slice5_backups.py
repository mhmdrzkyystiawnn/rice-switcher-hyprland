"""Fase 5 Langkah 1: pure backup listing / planning / safe removal.

`.bak-<ts>` entries are created by `snapshot.swap_symlink` as
`<target>.bak-<ts>` inside `config_home`, where `<ts>` is `_now_ts()`
(`%Y%m%dT%H%M%S_<ms>`). They can be a **symlink** (typical, pointing at a
rice tree) or a **real directory** (the first swap when the live link was
a real dir).

Hermetic: everything is built under a tempdir. `remove_backup` must never
follow a symlink or delete anything outside `config_home`.
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

from rice_switcher.backups import (
    BackupError,
    list_backups,
    plan_cleanup,
    remove_backup,
)


class ListTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rs-5-backups-")
        self.cfg = os.path.join(self.tmp, ".config")
        os.makedirs(self.cfg, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _dir_backup(self, name):
        p = os.path.join(self.cfg, name)
        os.makedirs(p, exist_ok=True)
        return p

    def _link_backup(self, name, target):
        p = os.path.join(self.cfg, name)
        os.symlink(target, p)
        return p

    def test_matches_exact_timestamp_pattern(self):
        self._dir_backup("hypr.bak-20230101T000000_000")
        self._dir_backup("waybar.bak-20231231T235959_999")
        names = [os.path.basename(b.path) for b in list_backups(self.cfg)]
        self.assertEqual(set(names), {
            "hypr.bak-20230101T000000_000",
            "waybar.bak-20231231T235959_999",
        })

    def test_ignores_non_matching_names(self):
        for bad in ("hypr.bak-", "hypr.bak-1", "hypr.bak",
                    "hypr.bak-20230101T000000",          # no ms
                    "hyprbak-20230101T000000_000",       # no dot
                    "hypr.bak-2023010T000000_000",       # short date
                    "keep.txt", ".hidden", "notes.md"):
            self._dir_backup(bad)
        self.assertEqual(list_backups(self.cfg), [])

    def test_ignores_regular_file_backup(self):
        # A `.bak-<ts>` that is a regular file is not a symlink or dir; the
        # design only handles those two kinds (§7).
        p = os.path.join(self.cfg, "hypr.bak-20230101T000000_000")
        with open(p, "w") as f:
            f.write("x")
        self.assertEqual(list_backups(self.cfg), [])

    def test_kind_symlink_vs_dir(self):
        outside = os.path.join(self.tmp, "rice-target")
        os.makedirs(outside, exist_ok=True)
        self._link_backup("hypr.bak-20230102T000000_000", outside)
        self._dir_backup("waybar.bak-20230101T000000_000")
        kinds = {os.path.basename(b.path): b.kind for b in list_backups(self.cfg)}
        self.assertEqual(kinds["hypr.bak-20230102T000000_000"], "symlink")
        self.assertEqual(kinds["waybar.bak-20230101T000000_000"], "dir")

    def test_never_follows_symlink_for_kind(self):
        # A symlink whose target is a directory must still be "symlink".
        target = os.path.join(self.tmp, "d")
        os.makedirs(target, exist_ok=True)
        self._link_backup("hypr.bak-20230101T000000_000", target)
        self.assertEqual(list_backups(self.cfg)[0].kind, "symlink")

    def test_ordered_by_name_timestamp_not_mtime(self):
        older = self._dir_backup("hypr.bak-20230101T000000_000")
        newer = self._dir_backup("hypr.bak-20230102T000000_000")
        # Reverse the mtimes: the older-named entry gets the newest mtime.
        os.utime(newer, (1000, 1000))
        os.utime(older, (2000, 2000))
        order = [os.path.basename(b.path) for b in list_backups(self.cfg)]
        self.assertEqual(order, ["hypr.bak-20230102T000000_000",
                                 "hypr.bak-20230101T000000_000"])

    def test_missing_config_home_is_empty(self):
        self.assertEqual(list_backups(os.path.join(self.tmp, "nope")), [])


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rs-5-plan-")
        self.cfg = os.path.join(self.tmp, ".config")
        os.makedirs(self.cfg, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _dir_backup(self, ts):
        p = os.path.join(self.cfg, f"hypr.bak-{ts}")
        os.makedirs(p, exist_ok=True)
        return p

    def test_keep_one_keeps_newest(self):
        self._dir_backup("20230101T000000_000")
        self._dir_backup("20230102T000000_000")
        self._dir_backup("20230103T000000_000")
        entries = list_backups(self.cfg)
        to_remove = plan_cleanup(entries, 1)
        removed = {os.path.basename(b.path) for b in to_remove}
        self.assertEqual(removed, {"hypr.bak-20230102T000000_000",
                                   "hypr.bak-20230101T000000_000"})

    def test_keep_zero_removes_all(self):
        self._dir_backup("20230101T000000_000")
        self._dir_backup("20230102T000000_000")
        entries = list_backups(self.cfg)
        self.assertEqual(len(plan_cleanup(entries, 0)), 2)

    def test_keep_none_removes_all(self):
        self._dir_backup("20230101T000000_000")
        entries = list_backups(self.cfg)
        self.assertEqual(len(plan_cleanup(entries, None)), 1)

    def test_keep_larger_than_count_removes_nothing(self):
        self._dir_backup("20230101T000000_000")
        entries = list_backups(self.cfg)
        self.assertEqual(plan_cleanup(entries, 5), [])

    def test_plan_is_side_effect_free(self):
        self._dir_backup("20230101T000000_000")
        entries = list_backups(self.cfg)
        plan_cleanup(entries, 0)
        self.assertTrue(os.path.isdir(os.path.join(
            self.cfg, "hypr.bak-20230101T000000_000")))


class RemoveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rs-5-remove-")
        self.cfg = os.path.join(self.tmp, ".config")
        os.makedirs(self.cfg, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_symlink_removed_target_intact(self):
        target = os.path.join(self.cfg, "hypr")
        os.makedirs(target, exist_ok=True)
        with open(os.path.join(target, "f.conf"), "w") as f:
            f.write("x")
        link = os.path.join(self.cfg, "hypr.bak-20230101T000000_000")
        os.symlink(target, link)
        self.assertEqual(remove_backup(link, self.cfg), "symlink")
        self.assertFalse(os.path.lexists(link))
        self.assertTrue(os.path.isfile(os.path.join(target, "f.conf")))

    def test_symlink_escaping_config_home_removed_but_target_intact(self):
        outside = os.path.join(self.tmp, "outside-rice")
        os.makedirs(outside, exist_ok=True)
        with open(os.path.join(outside, "keep.conf"), "w") as f:
            f.write("keep")
        link = os.path.join(self.cfg, "hypr.bak-20230101T000000_000")
        os.symlink(outside, link)
        self.assertEqual(remove_backup(link, self.cfg), "symlink")
        self.assertFalse(os.path.lexists(link))
        self.assertTrue(os.path.isfile(os.path.join(outside, "keep.conf")))

    def test_real_dir_with_escaping_symlink_inside(self):
        outside = os.path.join(self.tmp, "outside-rice")
        os.makedirs(outside, exist_ok=True)
        with open(os.path.join(outside, "keep.conf"), "w") as f:
            f.write("keep")
        backup = os.path.join(self.cfg, "hypr.bak-20230101T000000_000")
        os.makedirs(backup, exist_ok=True)
        with open(os.path.join(backup, "own.conf"), "w") as f:
            f.write("own")
        os.symlink(outside, os.path.join(backup, "escape"))
        self.assertEqual(remove_backup(backup, self.cfg), "dir")
        self.assertFalse(os.path.lexists(backup))
        self.assertTrue(os.path.isfile(os.path.join(outside, "keep.conf")))

    def test_path_outside_config_home_rejected(self):
        outside = os.path.join(self.tmp, "elsewhere")
        os.makedirs(outside, exist_ok=True)
        victim = os.path.join(outside, "hypr.bak-20230101T000000_000")
        os.makedirs(victim, exist_ok=True)
        with self.assertRaises(BackupError):
            remove_backup(victim, self.cfg)
        self.assertTrue(os.path.isdir(victim))

    def test_non_backup_name_rejected(self):
        d = os.path.join(self.cfg, "keep.txt")
        os.makedirs(d, exist_ok=True)
        with self.assertRaises(BackupError):
            remove_backup(d, self.cfg)
        self.assertTrue(os.path.isdir(d))

    def test_regular_file_backup_rejected(self):
        p = os.path.join(self.cfg, "hypr.bak-20230101T000000_000")
        with open(p, "w") as f:
            f.write("x")
        with self.assertRaises(BackupError):
            remove_backup(p, self.cfg)
        self.assertTrue(os.path.isfile(p))


if __name__ == "__main__":
    unittest.main()
