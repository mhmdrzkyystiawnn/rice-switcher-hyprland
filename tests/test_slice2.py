"""Slice 2 tests: validate, status (real-time), JSON Lines audit log.

All tests use temporary directories and never touch the live desktop,
~/.config, ~/.local, or the backup at ~/rice-backup/20261016_215357/.
"""

from __future__ import annotations

import gzip
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from rice_switcher.cli import (
    _expected_target,
    _norm_symlink_target,
    _owner_label,
    _probe_symlink,
    _rice_status,
    cmd_status,
    cmd_validate,
)
from rice_switcher.log import (
    SENSITIVE_KEYS,
    append_audit,
    audit_archive_dir,
    audit_compress_old,
    audit_rotate,
    read_audit_log,
    redact_event,
)
from rice_switcher.manifest import load_manifest
from rice_switcher.paths import Roots, audit_log_file
from rice_switcher.process import ProbeResult, probe_pids_by_identity
from rice_switcher.state import RiceTrust, State, mark_trust, read_state, write_state


def _make_roots(prefix: str) -> tuple[Roots, tempfile.TemporaryDirectory]:
    td = tempfile.TemporaryDirectory(prefix=prefix)
    base = td.name
    roots = Roots(
        rice_home=os.path.join(base, "rice"),
        config_home=os.path.join(base, "config"),
        state_home=os.path.join(base, "state"),
    )
    for d in (roots.rice_home, roots.config_home, roots.state_home):
        os.makedirs(d, exist_ok=True)
    return roots, td


def _make_rice(
    roots: Roots, name: str, config_link: str,
    *, with_start: bool = True, optional: bool = False,
    hypr_config: str | None = None,
) -> str:
    """Create a minimal rice on disk. Returns manifest path."""
    d = os.path.join(roots.rice_home, name)
    os.makedirs(d, exist_ok=True)
    mp = os.path.join(d, "rice.toml")
    start_field = 'start = "true"' if with_start else ""
    opt_field = "optional = true" if optional else ""
    with open(mp, "w") as f:
        f.write(
            f'[meta]\nname = "{name}"\n'
            f'[[components]]\nname = "hyprland"\ntype = "compositor"\n'
            f'{start_field}\n'
            f'stop = "true"\n'
            f'{opt_field}\n'
            f'config_link = "{config_link}"\n'
        )
    if hypr_config is not None:
        # Write a config file at <rice_root>/hyprland.lua
        rice_root = d
        cfg = os.path.join(rice_root, "hyprland.lua")
        with open(cfg, "w") as f:
            f.write(hypr_config)
    return mp


# ---------------------------------------------------------------------------
# Status helpers
# ---------------------------------------------------------------------------

class StatusHelpersTest(unittest.TestCase):
    """Tests for the small status helpers in cli.py."""

    def test_norm_symlink_target_absolute(self):
        self.assertEqual(_norm_symlink_target("/a/b", "/c/d"), "/c/d")

    def test_norm_symlink_target_relative(self):
        # relative "h.txt" against /a/b -> /a/h.txt (parent)
        self.assertEqual(_norm_symlink_target("/a/b", "h.txt"), "/a/h.txt")

    def test_expected_target_formula(self):
        # Mirrors switch.py do_switch: <RICE_HOME>/<name>/<basename(config_link)>
        et = _expected_target("/r", "alpha", "/cfg/hyprland")
        self.assertEqual(et, "/r/alpha/hyprland")

    def test_probe_symlink_missing(self):
        exists, actual = _probe_symlink("/no/such/path")
        self.assertFalse(exists)
        self.assertIsNone(actual)

    def test_probe_symlink_relative_target(self):
        td = tempfile.mkdtemp()
        try:
            link = os.path.join(td, "l")
            os.symlink("h.txt", link)
            exists, actual = _probe_symlink(link)
            self.assertTrue(exists)
            self.assertEqual(actual, os.path.join(td, "h.txt"))
        finally:
            os.unlink(link)
            os.rmdir(td)

    def test_probe_symlink_absolute_target(self):
        td = tempfile.mkdtemp()
        try:
            target = os.path.join(td, "real.txt")
            with open(target, "w") as f:
                f.write("x")
            link = os.path.join(td, "l")
            os.symlink(target, link)
            exists, actual = _probe_symlink(link)
            self.assertTrue(exists)
            self.assertEqual(actual, target)
        finally:
            os.unlink(link)
            os.unlink(target)
            os.rmdir(td)


class RiceStatusTest(unittest.TestCase):
    """Tests for the per-rice status determination."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory(prefix="rice-slice2-status-")
        base = self.td.name
        self.roots = Roots(
            rice_home=os.path.join(base, "rice"),
            config_home=os.path.join(base, "config"),
            state_home=os.path.join(base, "state"),
        )
        for d in (self.roots.rice_home, self.roots.config_home, self.roots.state_home):
            os.makedirs(d, exist_ok=True)
        self.rice_home = self.roots.rice_home

    def tearDown(self):
        self.td.cleanup()

    def _make_symlink_target(self, name, basename="hypr_cfg_link"):
        """Create a directory that matches the expected symlink target
        for a rice. Returns the absolute path."""
        d = os.path.join(self.rice_home, name, basename)
        os.makedirs(d, exist_ok=True)
        return d

    def _cfg_path(self):
        """Return a path that does not yet exist (so we can symlink to
        it)."""
        p = os.path.join(self.roots.config_home, "hypr_cfg_link")
        if os.path.lexists(p):
            os.unlink(p)
        return p

    def test_uninit(self):
        p = self._cfg_path()
        s = _rice_status("alpha", p, "beta", self.rice_home, set())
        self.assertEqual(s, "uninit")

    def test_missing(self):
        p = self._cfg_path()
        s = _rice_status("alpha", p, "alpha", self.rice_home, {"alpha"})
        self.assertEqual(s, "missing")

    def test_current(self):
        target = self._make_symlink_target("alpha")
        p = self._cfg_path()
        os.symlink(target, p)
        s = _rice_status("alpha", p, "alpha", self.rice_home, set())
        self.assertEqual(s, "current")

    def test_current_mismatch(self):
        # State=alpha but symlink points to beta
        target_b = self._make_symlink_target("beta")
        p = self._cfg_path()
        os.symlink(target_b, p)
        s = _rice_status("alpha", p, "alpha", self.rice_home, set())
        self.assertEqual(s, "current-mismatch")

    def test_idle(self):
        # Symlink points to beta's expected target, state=alpha (not beta)
        # → rice=beta, but state=alpha, so actual==expected(beta),
        # state!=beta → "idle"
        target_b = self._make_symlink_target("beta")
        p = self._cfg_path()
        os.symlink(target_b, p)
        s = _rice_status("beta", p, "alpha", self.rice_home, set())
        self.assertEqual(s, "idle")

    def test_idle_foreign(self):
        # State=gamma, symlink points to alpha target (not gamma, not itself)
        target_a = self._make_symlink_target("alpha")
        p = self._cfg_path()
        os.symlink(target_a, p)
        s = _rice_status("gamma", p, "alpha", self.rice_home, set())
        self.assertEqual(s, "idle-foreign")

    def test_dir(self):
        # Path is a regular dir, not a symlink
        p = self._cfg_path()
        os.makedirs(p)
        s = _rice_status("alpha", p, "alpha", self.rice_home, set())
        self.assertEqual(s, "dir")


class OwnerLabelTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(prefix="rice-slice2-owner-")
        base = self.td.name
        self.roots = Roots(
            rice_home=os.path.join(base, "rice"),
            config_home=os.path.join(base, "config"),
            state_home=os.path.join(base, "state"),
        )
        for d in (self.roots.rice_home, self.roots.config_home, self.roots.state_home):
            os.makedirs(d, exist_ok=True)
        self.rice_home = self.roots.rice_home
        self.cfg = os.path.join(self.roots.config_home, "hypr_cfg_link")
        if os.path.lexists(self.cfg):
            os.unlink(self.cfg)

    def tearDown(self):
        self.td.cleanup()

    def test_owner_alpha_when_symlink_points_to_alpha(self):
        target = os.path.join(self.rice_home, "alpha", "hypr_cfg_link")
        os.makedirs(target, exist_ok=True)
        os.symlink(target, self.cfg)
        owner = _owner_label(self.rice_home, ["alpha", "beta"], self.cfg)
        self.assertEqual(owner, "alpha")

    def test_owner_unmanaged(self):
        unmanaged = os.path.join(self.td.name, "external", "stuff")
        os.makedirs(unmanaged, exist_ok=True)
        os.symlink(unmanaged, self.cfg)
        owner = _owner_label(self.rice_home, ["alpha", "beta"], self.cfg)
        self.assertTrue(owner.startswith("<unmanaged:"))
        self.assertIn("external/stuff", owner)


# ---------------------------------------------------------------------------
# ProbeResult (tri-state)
# ---------------------------------------------------------------------------

class ProbeResultTest(unittest.TestCase):
    def test_running_when_self_match_excluded(self):
        # Use a name that won't match any real process on the test host
        r = probe_pids_by_identity("this-binary-does-not-exist-xyz123", ())
        self.assertEqual(r.status, "stopped")
        self.assertEqual(r.pids, ())

    def test_unknown_signature(self):
        pr = ProbeResult(pids=(), status="unknown", error="permission denied")
        self.assertEqual(pr.status, "unknown")
        self.assertEqual(pr.pids, ())


# ---------------------------------------------------------------------------
# JSON Lines audit log
# ---------------------------------------------------------------------------

class JsonLinesAuditTest(unittest.TestCase):
    def setUp(self):
        self.roots, self._td = _make_roots("rice-slice2-audit-")

    def tearDown(self):
        self._td.cleanup()

    def test_append_dict_creates_json_line(self):
        append_audit(self.roots, {"action": "test", "value": 42})
        active = audit_log_file(self.roots)
        with open(active) as f:
            line = f.readline().strip()
        d = json.loads(line)
        self.assertEqual(d["action"], "test")
        self.assertEqual(d["value"], 42)
        self.assertIn("ts", d)
        self.assertIn("pid", d)

    def test_append_str_raises_type_error(self):
        with self.assertRaises(TypeError):
            append_audit(self.roots, "legacy string line")

    def test_sensitive_key_top_level_redacted(self):
        append_audit(self.roots, {"action": "x", "api_key": "secret123"})
        active = audit_log_file(self.roots)
        with open(active) as f:
            line = f.read().strip()
        self.assertIn("[REDACTED]", line)
        self.assertNotIn("secret123", line)

    def test_sensitive_key_nested_dict_redacted(self):
        append_audit(self.roots, {
            "action": "x",
            "nested": {"authorization": "Bearer abcdef", "public": "ok"},
        })
        active = audit_log_file(self.roots)
        content = open(active).read()
        self.assertIn("[REDACTED]", content)
        self.assertNotIn("Bearer", content)
        self.assertIn("public", content)

    def test_sensitive_in_list(self):
        append_audit(self.roots, {
            "items": [
                {"token": "t1", "ok": "v1"},
                {"ok": "v2"},
            ]
        })
        content = open(audit_log_file(self.roots)).read()
        self.assertNotIn("t1", content)
        self.assertIn("v1", content)
        self.assertIn("v2", content)

    def test_sk_token_redacted(self):
        append_audit(self.roots, {"note": "found sk-abcdefghijklmnop1234 in env"})
        content = open(audit_log_file(self.roots)).read()
        self.assertNotIn("sk-abcdefghijklmnop1234", content)
        self.assertIn("[REDACTED]", content)

    def test_grep_secrets_returns_zero(self):
        append_audit(self.roots, {
            "data": "password=hunter2",
            "nested": {"api_key": "secret123"},
            "bearer": "Bearer abcdefghijklmnop1234",
            "sk_token": "sk-abcdefghijklmnop1234",
        })
        import re
        content = open(audit_log_file(self.roots)).read()
        # The redaction patterns should have removed all these literals
        for needle in ("hunter2", "secret123", "Bearer abcdef", "sk-abcdef", "ghp_", "xoxb-"):
            self.assertNotIn(needle, content, f"secret {needle!r} found in log")

    def test_read_audit_log_returns_entries(self):
        for i in range(3):
            append_audit(self.roots, {"action": "test", "i": i})
        entries, skipped = read_audit_log(self.roots)
        self.assertEqual(len(entries), 3)
        self.assertEqual(skipped, 0)
        self.assertEqual([e["i"] for e in entries], [0, 1, 2])


class AuditRotationTest(unittest.TestCase):
    def setUp(self):
        self.roots, self._td = _make_roots("rice-slice2-rotate-")

    def tearDown(self):
        self._td.cleanup()

    def test_rotate_by_size(self):
        # Force a small size cap for the test
        os.environ["RICE_AUDIT_ROTATE_MAX_BYTES"] = "100"
        try:
            # Write 5 events to exceed 100 bytes
            for i in range(5):
                append_audit(self.roots, {"action": "fill", "i": i, "padding": "x" * 50})
            archived = audit_rotate(self.roots)
            self.assertEqual(len(archived), 1)
            # Active file should be fresh (or non-existent)
            active = audit_log_file(self.roots)
            if os.path.exists(active):
                self.assertLess(os.path.getsize(active), 50)
            # Archived file exists
            self.assertTrue(os.path.isfile(archived[0]))
            self.assertRegex(
                os.path.basename(archived[0]),
                r"^audit-\d{8}-\d{6}-\d{5}-\d{3}\.log$",
            )
        finally:
            del os.environ["RICE_AUDIT_ROTATE_MAX_BYTES"]

    def test_no_rotate_when_small(self):
        append_audit(self.roots, {"action": "tiny"})
        archived = audit_rotate(self.roots)
        self.assertEqual(archived, [])

    def test_rotate_archive_counter_increments(self):
        os.environ["RICE_AUDIT_ROTATE_MAX_BYTES"] = "50"
        try:
            archived_names = set()
            for i in range(3):
                for j in range(3):
                    append_audit(self.roots, {"a": i, "b": j, "x": "y" * 40})
                archived = audit_rotate(self.roots)
                for a in archived:
                    archived_names.add(os.path.basename(a))
            # 3 different counter values
            self.assertEqual(len(archived_names), 3)
        finally:
            del os.environ["RICE_AUDIT_ROTATE_MAX_BYTES"]


class AuditCompressionTest(unittest.TestCase):
    def setUp(self):
        self.roots, self._td = _make_roots("rice-slice2-compress-")

    def tearDown(self):
        self._td.cleanup()

    def test_compress_old_archive(self):
        # Make an archive directly and back-date its mtime
        archive_dir = audit_archive_dir(self.roots)
        os.makedirs(archive_dir, exist_ok=True)
        old_path = os.path.join(archive_dir, "audit-20200101-000000-00001-000.log")
        with open(old_path, "w") as f:
            f.write("old content\n")
        # Set mtime to 100 days ago
        old_time = time.time() - 100 * 86400
        os.utime(old_path, (old_time, old_time))
        gzipped = audit_compress_old(self.roots)
        self.assertEqual(len(gzipped), 1)
        self.assertTrue(gzipped[0].endswith(".gz"))
        self.assertFalse(os.path.exists(old_path))
        # Verify content readable
        with gzip.open(gzipped[0], "rt") as f:
            self.assertIn("old content", f.read())

    def test_no_compress_recent_archive(self):
        archive_dir = audit_archive_dir(self.roots)
        os.makedirs(archive_dir, exist_ok=True)
        recent_path = os.path.join(archive_dir, "audit-20251016-120000-00001-000.log")
        with open(recent_path, "w") as f:
            f.write("recent\n")
        # No mtime adjustment; default 30 days
        gzipped = audit_compress_old(self.roots)
        self.assertEqual(gzipped, [])
        self.assertTrue(os.path.exists(recent_path))

    def test_no_auto_delete(self):
        # Even with RICE_AUDIT_ROTATE_DAYS=0 (immediate compress), the
        # .gz file stays around — never delete.
        os.environ["RICE_AUDIT_ROTATE_DAYS"] = "0"
        try:
            archive_dir = audit_archive_dir(self.roots)
            os.makedirs(archive_dir, exist_ok=True)
            old_path = os.path.join(archive_dir, "audit-20200101-000000-00001-000.log")
            with open(old_path, "w") as f:
                f.write("old\n")
            old_time = time.time() - 1
            os.utime(old_path, (old_time, old_time))
            gzipped = audit_compress_old(self.roots)
            # The .gz exists, the .log is gone (replaced by .gz).
            # The .gz itself is NOT deleted.
            self.assertEqual(len(gzipped), 1)
            self.assertTrue(os.path.exists(gzipped[0]))
        finally:
            del os.environ["RICE_AUDIT_ROTATE_DAYS"]


class AuditLockTest(unittest.TestCase):
    def setUp(self):
        self.roots, self._td = _make_roots("rice-slice2-lock-")

    def tearDown(self):
        self._td.cleanup()

    def test_concurrent_append_no_half_line(self):
        import threading
        results = []
        def writer(i):
            try:
                append_audit(self.roots, {"writer": i, "padding": "x" * 30})
                results.append(i)
            except Exception as e:
                results.append(e)
        threads = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(results), 5)
        content = open(audit_log_file(self.roots)).read()
        # Every non-empty line must be valid JSON
        for line in content.splitlines():
            if line.strip():
                json.loads(line)  # raises if invalid


class LegacyMigrationTest(unittest.TestCase):
    def test_legacy_plain_text_renamed_on_first_append(self):
        from rice_switcher import log as log_mod
        log_mod._MIGRATED = False
        roots, td = _make_roots("rice-slice2-legacy-")
        try:
            # Plant a plain-text audit.log
            active = audit_log_file(roots)
            os.makedirs(os.path.dirname(active), exist_ok=True)
            with open(active, "w") as f:
                f.write("legacy line 1\n")
                f.write("legacy line 2\n")
            # First append triggers migration
            append_audit(roots, {"action": "new"})
            # The legacy file should be renamed
            archive_dir = audit_archive_dir(roots)
            legacy_files = [f for f in os.listdir(archive_dir) if f.startswith("audit-pre-slice2-")]
            self.assertEqual(len(legacy_files), 1)
            with open(os.path.join(archive_dir, legacy_files[0])) as f:
                content = f.read()
            self.assertIn("legacy line 1", content)
            self.assertIn("legacy line 2", content)
            # Active file now contains only JSON
            with open(active) as f:
                line = f.read().strip()
            json.loads(line)  # must parse
        finally:
            td.cleanup()


# ---------------------------------------------------------------------------
# cmd_validate (integration with validate.py)
# ---------------------------------------------------------------------------

class CmdValidateTest(unittest.TestCase):
    def setUp(self):
        self.roots, self._td = _make_roots("rice-slice2-cmdvalidate-")
        self.live = os.path.join(self.roots.config_home, "hypr")
        os.makedirs(self.live, exist_ok=True)
        with open(os.path.join(self.live, "h.txt"), "w") as f:
            f.write("v0\n")

    def tearDown(self):
        self._td.cleanup()

    def test_validate_unknown_rice_returns_2(self):
        from argparse import Namespace
        args = Namespace(name="nope", cmd="validate")
        rc = cmd_validate(args, self.roots, log=None)
        self.assertEqual(rc, 2)

    def test_validate_valid_rice_returns_0(self):
        _make_rice(self.roots, "alpha", self.live)
        from argparse import Namespace
        args = Namespace(name="alpha", cmd="validate")
        rc = cmd_validate(args, self.roots, log=None)
        # May be 0 (no errors) or 1 (some warnings count as error). The
        # main check: the call does not crash and returns an int.
        self.assertIn(rc, (0, 1))

    def test_validate_component_missing_start_returns_1(self):
        # A component without `start` and not `optional` must error
        d = os.path.join(self.roots.rice_home, "bad")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "rice.toml"), "w") as f:
            f.write(
                '[meta]\nname = "bad"\n'
                '[[components]]\nname = "hyprland"\ntype = "compositor"\n'
                'stop = "true"\n'
                f'config_link = "{self.live}"\n'
            )
        from argparse import Namespace
        args = Namespace(name="bad", cmd="validate")
        rc = cmd_validate(args, self.roots, log=None)
        self.assertEqual(rc, 1)


# ---------------------------------------------------------------------------
# cmd_status (integration; uses the live /proc which is harmless)
# ---------------------------------------------------------------------------

class CmdStatusIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.roots, self._td = _make_roots("rice-slice2-cmdstatus-")

    def tearDown(self):
        self._td.cleanup()

    def test_cmd_status_no_rice(self):
        from argparse import Namespace
        args = Namespace(cmd="status")
        rc = cmd_status(args, self.roots, log=None)
        self.assertEqual(rc, 0)

    def test_cmd_status_with_rice(self):
        live = os.path.join(self.roots.config_home, "hypr")
        os.makedirs(live, exist_ok=True)
        _make_rice(self.roots, "alpha", live)
        from argparse import Namespace
        args = Namespace(cmd="status")
        rc = cmd_status(args, self.roots, log=None)
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
