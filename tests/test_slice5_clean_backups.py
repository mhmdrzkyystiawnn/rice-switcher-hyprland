"""Fase 5 Langkah 2: `clean-backups` subcommand.

Hermetic: roots + HOME are redirected into a tempdir; `ask`/`out` are
injected, so no real stdin/stdout is used and nothing outside is touched.
"""

from __future__ import annotations

import contextlib
import io
import json
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
from rice_switcher.cli import build_parser, cmd_clean_backups, main
from rice_switcher.lock import switch_lock
from rice_switcher.paths import Roots, audit_log_file


class ScriptedAsk:
    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)


class Recorder:
    def __init__(self):
        self.lines: list[str] = []

    def __call__(self, text: str) -> None:
        self.lines.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


class CleanBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rs-5-cli-")
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

    def backup_dir(self, ts, name="hypr"):
        p = os.path.join(self.config_home, f"{name}.bak-{ts}")
        os.makedirs(p, exist_ok=True)
        with open(os.path.join(p, "conf"), "w") as f:
            f.write(ts)
        return p

    def backup_link(self, ts, target, name="hypr"):
        p = os.path.join(self.config_home, f"{name}.bak-{ts}")
        os.symlink(target, p)
        return p

    def names(self):
        return [os.path.basename(b.path) for b in list_backups(self.config_home)]

    def run_direct(self, argv, answers=None):
        args = build_parser().parse_args(argv)
        out = Recorder()
        ask = ScriptedAsk(answers if answers is not None else [])
        so, se = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=False):
            with contextlib.redirect_stdout(so), contextlib.redirect_stderr(se):
                rc = cmd_clean_backups(args, self.roots, None, ask=ask, out=out)
        return rc, out, so.getvalue() + se.getvalue()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=False):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def audit_events(self):
        path = audit_log_file(self.roots)
        if not os.path.isfile(path):
            return []
        events = []
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        return events


class RemoveTest(CleanBase):
    def test_yes_removes_all_candidates(self):
        self.backup_dir("20230101T000000_000")
        self.backup_dir("20230102T000000_000")
        rc, out, text = self.run_direct(["clean-backups", "--yes", "--include-real-dirs"])
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.names(), [])

    def test_keep_one_leaves_newest(self):
        self.backup_dir("20230101T000000_000")
        self.backup_dir("20230102T000000_000")
        rc, out, text = self.run_direct(["clean-backups", "--keep", "1", "--yes", "--include-real-dirs"])
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.names(), ["hypr.bak-20230102T000000_000"])

    def test_non_backup_entries_untouched(self):
        self.backup_dir("20230101T000000_000")
        keep = os.path.join(self.config_home, "keep.txt")
        os.makedirs(keep, exist_ok=True)
        near = os.path.join(self.config_home, "hypr.bak-1")
        os.makedirs(near, exist_ok=True)
        rc, out, text = self.run_direct(["clean-backups", "--yes", "--include-real-dirs"])
        self.assertEqual(rc, 0, text)
        self.assertTrue(os.path.isdir(keep))
        self.assertTrue(os.path.isdir(near))
        self.assertEqual(self.names(), [])

    def test_symlink_removed_target_intact(self):
        target = os.path.join(self.rice_home, "rice-x", "hypr")
        os.makedirs(target, exist_ok=True)
        with open(os.path.join(target, "keep.conf"), "w") as f:
            f.write("keep")
        self.backup_link("20230101T000000_000", target)
        rc, out, text = self.run_direct(["clean-backups", "--yes"])
        self.assertEqual(rc, 0, text)
        self.assertFalse(os.path.lexists(os.path.join(
            self.config_home, "hypr.bak-20230101T000000_000")))
        self.assertTrue(os.path.isfile(os.path.join(target, "keep.conf")))

    def test_no_candidates_is_zero(self):
        rc, out, text = self.run_direct(["clean-backups", "--yes"])
        self.assertEqual(rc, 0, text)
        self.assertIn("tidak ada", out.text.lower())

    def test_prints_consequence(self):
        self.backup_dir("20230101T000000_000")
        rc, out, text = self.run_direct(["clean-backups", "--yes"])
        self.assertIn("snapshot", out.text.lower())

    def test_audit_written(self):
        self.backup_dir("20230101T000000_000")
        self.run_direct(["clean-backups", "--yes", "--include-real-dirs"])
        events = [e for e in self.audit_events() if e.get("action") == "cleanup"]
        self.assertEqual(len(events), 1)
        removed = events[0]["removed"]
        self.assertEqual(len(removed), 1)
        self.assertIn("hypr.bak-20230101T000000_000", removed[0])


class ConfirmTest(CleanBase):
    def test_default_no_does_not_delete(self):
        self.backup_dir("20230101T000000_000")
        rc, out, text = self.run_direct(["clean-backups"])
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.names(), ["hypr.bak-20230101T000000_000"])

    def test_explicit_no_does_not_delete(self):
        self.backup_dir("20230101T000000_000")
        rc, out, text = self.run_direct(["clean-backups"], answers=["n"])
        self.assertEqual(rc, 0, text)
        self.assertEqual(len(self.names()), 1)

    def test_eof_cancels(self):
        self.backup_dir("20230101T000000_000")
        rc, out, text = self.run_direct(["clean-backups"], answers=[])
        self.assertEqual(rc, 0, text)
        self.assertEqual(len(self.names()), 1)

    def test_confirm_yes_deletes(self):
        self.backup_dir("20230101T000000_000")
        rc, out, text = self.run_direct(
            ["clean-backups", "--include-real-dirs"], answers=["y"])
        self.assertEqual(rc, 0, text)
        self.assertEqual(self.names(), [])


class ValidationTest(CleanBase):
    def test_invalid_keep_text_exits_nonzero(self):
        self.backup_dir("20230101T000000_000")
        with self.assertRaises(SystemExit) as cm:
            self.run_cli("clean-backups", "--keep", "abc")
        self.assertNotEqual(cm.exception.code, 0)
        self.assertEqual(len(self.names()), 1)

    def test_negative_keep_rejected_no_delete(self):
        self.backup_dir("20230101T000000_000")
        rc, out, text = self.run_direct(["clean-backups", "--keep", "-1", "--yes"])
        self.assertNotEqual(rc, 0, text)
        self.assertEqual(len(self.names()), 1)

    def test_switch_running_refused(self):
        self.backup_dir("20230101T000000_000")
        with mock.patch.dict(os.environ, self.env, clear=False):
            with switch_lock(self.roots):
                rc, out, text = self.run_direct(["clean-backups", "--yes"])
        self.assertNotEqual(rc, 0, text)
        self.assertEqual(len(self.names()), 1)

    def test_partial_failure_reports_and_exits_nonzero(self):
        a = self.backup_dir("20230101T000000_000")
        b = self.backup_dir("20230102T000000_000")
        real = __import__("rice_switcher.backups", fromlist=["remove_backup"]).remove_backup

        def flaky(path, cfg):
            if path == a:
                from rice_switcher.backups import BackupError
                raise BackupError("simulated failure")
            return real(path, cfg)

        with mock.patch("rice_switcher.backups.remove_backup", side_effect=flaky):
            rc, out, text = self.run_direct(["clean-backups", "--yes", "--include-real-dirs"])
        self.assertNotEqual(rc, 0, text)
        self.assertTrue(os.path.isdir(a))          # failed -> remains
        self.assertFalse(os.path.lexists(b))       # succeeded -> gone
        events = [e for e in self.audit_events() if e.get("action") == "cleanup"]
        self.assertEqual(len(events), 1)
        self.assertEqual(len(events[0]["removed"]), 1)
        self.assertIn("hypr.bak-20230102T000000_000", events[0]["removed"][0])
        self.assertTrue(events[0]["failed"])

    def test_all_inside_tempdir(self):
        self.backup_dir("20230101T000000_000")
        self.run_direct(["clean-backups", "--yes"])
        tmp_real = os.path.realpath(self.tmp)
        for p in (self.config_home, self.state_home):
            self.assertTrue(os.path.realpath(p).startswith(tmp_real))


if __name__ == "__main__":
    unittest.main()
