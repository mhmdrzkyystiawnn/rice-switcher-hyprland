"""Fase debug Bagian 3: clean-backups --include-real-dirs.

Keputusan produk: backup berupa DIREKTORI NYATA dilindungi by default.
- --yes tanpa --include-real-dirs MELEWATI direktori nyata dan
  melaporkannya (exit non-nol bila ada yang dilewati).
- --include-real-dirs boleh menghapus direktori nyata.
- Direktori nyata TERTUA tidak ikut terhapus walau --keep 0,
  kecuali --include-real-dirs diberikan.
- Symlink backup tetap dihapus oleh --yes tanpa flag tambahan.

Hermetic: semua di tempdir.
"""

from __future__ import annotations

import contextlib
import io
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

from rice_switcher.backups import list_backups
from rice_switcher.cli import build_parser, cmd_clean_backups


class CleanRealDirsTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="rs-7-real-")
        self.home = os.path.join(self._td.name, "home")
        self.config_home = os.path.join(self.home, ".config")
        os.makedirs(self.config_home, exist_ok=True)
        from rice_switcher.paths import Roots
        self.roots = Roots(
            rice_home=os.path.join(self.home, "rice"),
            config_home=self.config_home,
            state_home=os.path.join(self.home, ".local", "state"),
        )
        os.makedirs(self.roots.rice_home, exist_ok=True)
        os.makedirs(self.roots.state_home, exist_ok=True)

    def tearDown(self):
        self._td.cleanup()

    def _mk_symlink_backup(self, ts, name="hypr"):
        p = os.path.join(self.config_home, f"{name}.bak-{ts}")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        target = os.path.join(self._td.name, "outside")
        os.makedirs(target, exist_ok=True)
        os.symlink(target, p)
        return p

    def _mk_dir_backup(self, ts, name="waybar"):
        p = os.path.join(self.config_home, f"{name}.bak-{ts}")
        os.makedirs(p, exist_ok=True)
        with open(os.path.join(p, "conf"), "w") as f:
            f.write("x")
        return p

    def _run(self, argv):
        args = build_parser().parse_args(argv)
        so, se = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(so), contextlib.redirect_stderr(se):
            with mock.patch("rice_switcher.log.append_audit"):
                rc = cmd_clean_backups(args, self.roots, None)
        return rc, so.getvalue() + se.getvalue()

    # --- default: dir nyata dilindungi -----------------------------------

    def test_yes_without_flag_skips_real_dir(self):
        d = self._mk_dir_backup("20230101T000000_000")
        rc, out = self._run(["clean-backups", "--yes"])
        self.assertNotEqual(rc, 0,
                            f"exit non-nol expected when real dirs are skipped; got rc={rc}")
        self.assertTrue(os.path.isdir(d),
                        "real dir backup must NOT be deleted without --include-real-dirs")

    def test_yes_without_flag_reports_skipped(self):
        d = self._mk_dir_backup("20230101T000000_000")
        rc, out = self._run(["clean-backups", "--yes"])
        self.assertIn("dilewati", out.lower(),
                      f"skipped real dirs must be reported:\n{out}")

    def test_yes_without_flag_removes_symlink(self):
        s = self._mk_symlink_backup("20230101T000000_000")
        rc, out = self._run(["clean-backups", "--yes"])
        self.assertFalse(os.path.lexists(s),
                         "symlink backup must be removed by --yes without flag")

    def test_real_dir_oldest_still_kept_with_keep_zero(self):
        d = self._mk_dir_backup("20230101T000000_000")
        s = self._mk_symlink_backup("20230102T000000_000")
        rc, out = self._run(["clean-backups", "--keep", "0", "--yes"])
        self.assertTrue(os.path.isdir(d),
                        "oldest real dir must NOT be deleted by --keep 0 without flag")
        self.assertFalse(os.path.lexists(s),
                         "symlink backup must be removed")

    # --- dengan flag: dir nyata boleh dihapus -----------------------------

    def test_include_real_dirs_removes_real_dir(self):
        d = self._mk_dir_backup("20230101T000000_000")
        rc, out = self._run(["clean-backups", "--yes", "--include-real-dirs"])
        self.assertEqual(rc, 0,
                         f"exit 0 expected; got rc={rc}, out={out!r}")
        self.assertFalse(os.path.lexists(d),
                         "real dir must be deleted with --include-real-dirs")

    def test_include_real_dirs_removes_oldest_too(self):
        d1 = self._mk_dir_backup("20230101T000000_000")
        d2 = self._mk_dir_backup("20230102T000000_000")
        rc, out = self._run(["clean-backups", "--keep", "0", "--yes",
                             "--include-real-dirs"])
        self.assertEqual(rc, 0, f"exit 0 expected; got rc={rc}")
        self.assertFalse(os.path.lexists(d1))
        self.assertFalse(os.path.lexists(d2))

    def test_include_real_dirs_keep_one(self):
        d1 = self._mk_dir_backup("20230101T000000_000")
        d2 = self._mk_dir_backup("20230102T000000_000")
        rc, out = self._run(["clean-backups", "--keep", "1", "--yes",
                             "--include-real-dirs"])
        self.assertEqual(rc, 0, f"exit 0 expected; got rc={rc}")
        self.assertFalse(os.path.lexists(d1), "oldest must be removed")
        self.assertTrue(os.path.isdir(d2), "newest must be kept")

    # --- kasus campuran: symlink + dir + --keep 1 (regression Isu #1) ---

    def test_mixed_keep_one_with_flag_keeps_newest(self):
        """--keep 1 --include-real-dirs: symlink tertua + dir terbaru.
        Yang tersisa = 1 terbaru (dir), symlink tertua dihapus."""
        s_old = self._mk_symlink_backup("20230101T000000_000")
        d_new = self._mk_dir_backup("20230102T000000_000")  # newer
        rc, out = self._run(["clean-backups", "--keep", "1", "--yes",
                             "--include-real-dirs"])
        self.assertEqual(rc, 0, f"out={out!r}")
        self.assertFalse(os.path.lexists(s_old),
                         "oldest symlink must be removed")
        self.assertTrue(os.path.isdir(d_new),
                        "newest dir must be kept (global retention)")

    def test_mixed_keep_one_without_flag_protects_dir(self):
        """--keep 1 --yes tanpa flag: 1 symlink eligible, 1 dir dilindungi.
        Symlink disisakan (--keep 1), dir disisakan (dilindungi). Exit
        non-nol karena ada dir yang dilewati."""
        s_old = self._mk_symlink_backup("20230101T000000_000")
        d_new = self._mk_dir_backup("20230102T000000_000")
        rc, out = self._run(["clean-backups", "--keep", "1", "--yes"])
        self.assertNotEqual(rc, 0, f"expected non-zero; out={out!r}")
        # Both survive: symlink retained by --keep 1, dir retained by --include-real-dirs
        self.assertTrue(os.path.lexists(s_old),
                        "symlink retained by --keep 1")
        self.assertTrue(os.path.isdir(d_new),
                        "dir protected without --include-real-dirs")
        # Both reported in output
        self.assertIn("[simpan]", out, f"symlink must be marked simpan: {out!r}")
        self.assertIn("[dilewati]", out, f"dir must be marked dilewati: {out!r}")

    # --- interaktif: prompt terpisah untuk dir nyata ----------------------

    def test_interactive_asks_for_real_dir(self):
        d = self._mk_dir_backup("20230101T000000_000")
        args = build_parser().parse_args(["clean-backups"])
        so, se = io.StringIO(), io.StringIO()

        def fake_ask(prompt):
            if "direktori nyata" in prompt.lower():
                return "n"
            return "y"

        with contextlib.redirect_stdout(so), contextlib.redirect_stderr(se):
            with mock.patch("rice_switcher.log.append_audit"):
                rc = cmd_clean_backups(args, self.roots, None, ask=fake_ask)
        self.assertTrue(os.path.isdir(d),
                        "real dir must survive 'n' answer on separate prompt")


if __name__ == "__main__":
    unittest.main()
