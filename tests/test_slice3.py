"""Slice 3 tests: `rice-switcher keybind-install`.

All tests use temporary directories; the live keybinds file and the
live Hyprland config are never touched.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from rice_switcher.cli import cmd_keybind_install
from rice_switcher.keybind import (
    ALLOWED_ROOT,
    DEFAULT_COMMAND,
    DEFAULT_TARGET,
    KEYSYM,
    InstallResult,
    _EXEC_CMD_RE,
    _classify_target,
    _parse_binds,
    _check_path_allowed,
    install,
    render_bind_line,
)
from rice_switcher.paths import Roots


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


# ---------------------------------------------------------------------------
# Parsing and classification
# ---------------------------------------------------------------------------

class ParseBindsTest(unittest.TestCase):
    def test_parses_simple_bind(self):
        content = 'hl.bind("SUPER+SHIFT+R", hl.dsp.exec_cmd("rice-switcher apply"), {description = "x"} )'
        binds = _parse_binds(content)
        self.assertEqual(len(binds), 1)
        _, keysym, exec_cmd, _, _ = binds[0]
        self.assertEqual(keysym, "SUPER+SHIFT+R")
        self.assertEqual(exec_cmd, "rice-switcher apply")

    def test_parses_bind_with_whitespace_variations(self):
        # Multi-line, extra whitespace
        content = (
            'hl.bind(\n'
            '  "SUPER+SHIFT+R" , '
            'hl.dsp.exec_cmd( "rice-switcher apply" ),\n'
            '{description = "x"} )'
        )
        binds = _parse_binds(content)
        self.assertEqual(len(binds), 1)
        _, keysym, exec_cmd, _, _ = binds[0]
        self.assertEqual(keysym, "SUPER+SHIFT+R")
        self.assertEqual(exec_cmd, "rice-switcher apply")

    def test_parses_multiple_binds(self):
        content = (
            'hl.bind("CTRL+SUPER+ALT+Slash", hl.dsp.exec_cmd("xdg-open x"), {description = "edit"} )\n'
            'hl.bind("SUPER+Q", hl.dsp.exec_cmd("kitty"), {description = "term"} )\n'
            'hl.bind("SUPER+SHIFT+R", hl.dsp.exec_cmd("rice-switcher apply"), {description = "apply"} )\n'
        )
        binds = _parse_binds(content)
        self.assertEqual(len(binds), 3)
        self.assertEqual([b[1] for b in binds],
                         ["CTRL+SUPER+ALT+Slash", "SUPER+Q", "SUPER+SHIFT+R"])

    def test_parse_skips_non_bind_lines(self):
        content = (
            '-- this is a comment\n'
            'local x = "hl.bind(\"FAKE\", ...)"\n'
            'hl.bind("REAL", hl.dsp.exec_cmd("cmd"), {})\n'
        )
        binds = _parse_binds(content)
        self.assertEqual(len(binds), 1)
        self.assertEqual(binds[0][1], "REAL")


class ClassifyTargetTest(unittest.TestCase):
    def test_absent(self):
        s, _ = _classify_target("", KEYSYM, "rice-switcher apply")
        self.assertEqual(s, "absent")

    def test_present_with_matching_exec(self):
        content = (
            'hl.bind("SUPER+SHIFT+R", hl.dsp.exec_cmd("rice-switcher apply"), '
            '{description = "x"} )\n'
        )
        s, _ = _classify_target(content, KEYSYM, "rice-switcher apply")
        self.assertEqual(s, "present")

    def test_conflict_with_different_exec(self):
        content = (
            'hl.bind("SUPER+SHIFT+R", hl.dsp.exec_cmd("firefox"), '
            '{description = "browser"} )\n'
        )
        s, detail = _classify_target(content, KEYSYM, "rice-switcher apply")
        self.assertEqual(s, "conflict")
        self.assertIn("firefox", detail)

    def test_present_ignores_other_binds(self):
        content = (
            'hl.bind("SUPER+Q", hl.dsp.exec_cmd("kitty"), {description = "term"} )\n'
            'hl.bind("SUPER+SHIFT+R", hl.dsp.exec_cmd("rice-switcher apply"), {description = "apply"} )\n'
        )
        s, _ = _classify_target(content, KEYSYM, "rice-switcher apply")
        self.assertEqual(s, "present")


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------

class PathSafetyTest(unittest.TestCase):
    def test_default_path_allowed(self):
        ok, err = _check_path_allowed(DEFAULT_TARGET, allow_any=False)
        self.assertTrue(ok, f"default path should be allowed: {err}")

    def test_path_outside_hypr_rejected(self):
        ok, err = _check_path_allowed("/tmp/keybinds.lua", allow_any=False)
        self.assertFalse(ok)
        self.assertIn("outside the allowed root", err)

    def test_path_outside_hypr_allowed_with_flag(self):
        ok, err = _check_path_allowed("/tmp/keybinds.lua", allow_any=True)
        self.assertTrue(ok, f"should be allowed with flag: {err}")


# ---------------------------------------------------------------------------
# render_bind_line
# ---------------------------------------------------------------------------

class RenderBindLineTest(unittest.TestCase):
    def test_render_matches_existing_format(self):
        line = render_bind_line("SUPER+SHIFT+R", "rice-switcher apply",
                                 "Apply current rice")
        # The format must contain the keysym, the command, and a
        # description, similar to the existing user keybinds.lua line.
        self.assertIn('hl.bind(', line)
        self.assertIn('"SUPER+SHIFT+R"', line)
        self.assertIn('exec_cmd("rice-switcher apply")', line)
        self.assertIn('description = "Apply current rice"', line)
        self.assertTrue(line.endswith(")"))


# ---------------------------------------------------------------------------
# install() — main entry
# ---------------------------------------------------------------------------

class InstallTest(unittest.TestCase):
    def setUp(self):
        self.roots, self._td = _make_roots("rice-slice3-install-")
        self.target_dir = os.path.join(self._td.name, "fake_hypr", "custom")
        os.makedirs(self.target_dir, exist_ok=True)
        self.target = os.path.join(self.target_dir, "keybinds.lua")
        # Override ALLOWED_ROOT via monkey-patching the function under test
        import rice_switcher.keybind as kbm
        self._orig_allowed = kbm.ALLOWED_ROOT
        kbm.ALLOWED_ROOT = os.path.realpath(os.path.join(self._td.name, "fake_hypr"))

    def tearDown(self):
        import rice_switcher.keybind as kbm
        kbm.ALLOWED_ROOT = self._orig_allowed
        self._td.cleanup()

    def test_install_creates_file_when_missing(self):
        res = install(self.roots, target_path=self.target)
        self.assertEqual(res.result, "added")
        self.assertTrue(os.path.isfile(self.target))
        content = open(self.target).read()
        self.assertIn('"SUPER+SHIFT+R"', content)
        self.assertIn('exec_cmd("rice-switcher apply")', content)

    def test_install_is_idempotent(self):
        install(self.roots, target_path=self.target)
        size_first = os.path.getsize(self.target)
        res = install(self.roots, target_path=self.target)
        self.assertEqual(res.result, "already_present")
        # File must not grow
        self.assertEqual(os.path.getsize(self.target), size_first)

    def test_install_preserves_existing_content(self):
        with open(self.target, "w") as f:
            f.write('hl.bind("CTRL+SUPER+ALT+Slash", hl.dsp.exec_cmd("xdg-open x"), {description = "edit"} )\n')
        res = install(self.roots, target_path=self.target)
        self.assertEqual(res.result, "added")
        content = open(self.target).read()
        self.assertIn("CTRL+SUPER+ALT+Slash", content)
        self.assertIn("SUPER+SHIFT+R", content)

    def test_install_detects_conflict(self):
        with open(self.target, "w") as f:
            f.write('hl.bind("SUPER+SHIFT+R", hl.dsp.exec_cmd("firefox"), {description = "browser"} )\n')
        res = install(self.roots, target_path=self.target)
        self.assertEqual(res.result, "conflict")
        # File must be unchanged
        content = open(self.target).read()
        self.assertIn("firefox", content)
        self.assertNotIn("rice-switcher apply", content)

    def test_install_creates_backup(self):
        with open(self.target, "w") as f:
            f.write("original content\n")
        res = install(self.roots, target_path=self.target)
        self.assertEqual(res.result, "added")
        self.assertIn("(backup:", res.detail)
        # Find the backup file
        backups = [f for f in os.listdir(self.target_dir) if f.startswith("keybinds.lua.bak-")]
        self.assertEqual(len(backups), 1)
        with open(os.path.join(self.target_dir, backups[0])) as f:
            self.assertEqual(f.read(), "original content\n")

    def test_install_dry_run_does_not_modify(self):
        res = install(self.roots, target_path=self.target, dry_run=True)
        self.assertEqual(res.result, "dry_run")
        # The detail is the new line, ready to append
        self.assertIn("SUPER+SHIFT+R", res.detail)
        # No file created
        self.assertFalse(os.path.exists(self.target))

    def test_install_dry_run_with_existing_does_not_modify(self):
        with open(self.target, "w") as f:
            f.write("original content\n")
        size_before = os.path.getsize(self.target)
        res = install(self.roots, target_path=self.target, dry_run=True)
        self.assertEqual(res.result, "dry_run")
        self.assertEqual(os.path.getsize(self.target), size_before)

    def test_install_rejects_path_outside_allowed_root(self):
        bad = os.path.join(self._td.name, "other_dir", "keybinds.lua")
        res = install(self.roots, target_path=bad)
        self.assertEqual(res.result, "error")
        self.assertIn("outside the allowed root", res.detail)

    def test_install_allow_any_path(self):
        bad = os.path.join(self._td.name, "other_dir", "keybinds.lua")
        os.makedirs(os.path.dirname(bad), exist_ok=True)
        res = install(self.roots, target_path=bad, allow_any_path=True)
        self.assertEqual(res.result, "added")
        self.assertTrue(os.path.isfile(bad))

    def test_install_custom_command(self):
        res = install(
            self.roots,
            target_path=self.target,
            command="fuzzel --list",
        )
        self.assertEqual(res.result, "added")
        content = open(self.target).read()
        self.assertIn('exec_cmd("fuzzel --list")', content)

    def test_install_no_backup_when_file_does_not_exist(self):
        res = install(self.roots, target_path=self.target)
        self.assertEqual(res.result, "added")
        backups = [f for f in os.listdir(self.target_dir) if f.startswith("keybinds.lua.bak-")]
        self.assertEqual(backups, [])

    def test_install_handles_multi_line_existing(self):
        content = (
            "-- comment line\n"
            'hl.bind("SUPER+Q", hl.dsp.exec_cmd("kitty"), {description = "term"} )\n'
            "\n"
        )
        with open(self.target, "w") as f:
            f.write(content)
        res = install(self.roots, target_path=self.target)
        self.assertEqual(res.result, "added")
        out = open(self.target).read()
        # Both binds present, and the original blank line / comment kept
        self.assertIn("SUPER+Q", out)
        self.assertIn("SUPER+SHIFT+R", out)
        self.assertIn("-- comment line", out)


# ---------------------------------------------------------------------------
# cmd_keybind_install integration
# ---------------------------------------------------------------------------

class CmdKeybindInstallTest(unittest.TestCase):
    def setUp(self):
        self.roots, self._td = _make_roots("rice-slice3-cmd-")
        self.target_dir = os.path.join(self._td.name, "fake_hypr", "custom")
        os.makedirs(self.target_dir, exist_ok=True)
        self.target = os.path.join(self.target_dir, "keybinds.lua")
        import rice_switcher.keybind as kbm
        self._orig_allowed = kbm.ALLOWED_ROOT
        kbm.ALLOWED_ROOT = os.path.realpath(os.path.join(self._td.name, "fake_hypr"))

    def tearDown(self):
        import rice_switcher.keybind as kbm
        kbm.ALLOWED_ROOT = self._orig_allowed
        self._td.cleanup()

    def test_cmd_added(self):
        args = Namespace(
            path=self.target, command="rice-switcher apply",
            dry_run=False, allow_any_path=False,
        )
        rc = cmd_keybind_install(args, self.roots, log=None)
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isfile(self.target))

    def test_cmd_already_present(self):
        # Pre-populate
        with open(self.target, "w") as f:
            f.write('hl.bind("SUPER+SHIFT+R", hl.dsp.exec_cmd("rice-switcher apply"), {description = "x"} )\n')
        args = Namespace(
            path=self.target, command="rice-switcher apply",
            dry_run=False, allow_any_path=False,
        )
        rc = cmd_keybind_install(args, self.roots, log=None)
        self.assertEqual(rc, 0)
        # No new content added
        content = open(self.target).read()
        self.assertEqual(content.count("SUPER+SHIFT+R"), 1)

    def test_cmd_conflict_returns_2(self):
        with open(self.target, "w") as f:
            f.write('hl.bind("SUPER+SHIFT+R", hl.dsp.exec_cmd("firefox"), {description = "browser"} )\n')
        args = Namespace(
            path=self.target, command="rice-switcher apply",
            dry_run=False, allow_any_path=False,
        )
        rc = cmd_keybind_install(args, self.roots, log=None)
        self.assertEqual(rc, 2)

    def test_cmd_dry_run_does_not_write(self):
        args = Namespace(
            path=self.target, command="rice-switcher apply",
            dry_run=True, allow_any_path=False,
        )
        rc = cmd_keybind_install(args, self.roots, log=None)
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(self.target))

    def test_cmd_writes_audit_event(self):
        args = Namespace(
            path=self.target, command="rice-switcher apply",
            dry_run=False, allow_any_path=False,
        )
        cmd_keybind_install(args, self.roots, log=None)
        # Audit log should have an entry
        from rice_switcher.paths import audit_log_file
        log_path = audit_log_file(self.roots)
        if os.path.isfile(log_path):
            entries = []
            with open(log_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
            actions = [e.get("action") for e in entries]
            self.assertIn("keybind_install", actions)


# ---------------------------------------------------------------------------
# Re-do test (idempotency + 2-run check)
# ---------------------------------------------------------------------------

class IdempotencyRepeatedTest(unittest.TestCase):
    def setUp(self):
        self.roots, self._td = _make_roots("rice-slice3-idem-")
        self.target_dir = os.path.join(self._td.name, "fake_hypr", "custom")
        os.makedirs(self.target_dir, exist_ok=True)
        self.target = os.path.join(self.target_dir, "keybinds.lua")
        import rice_switcher.keybind as kbm
        self._orig_allowed = kbm.ALLOWED_ROOT
        kbm.ALLOWED_ROOT = os.path.realpath(os.path.join(self._td.name, "fake_hypr"))

    def tearDown(self):
        import rice_switcher.keybind as kbm
        kbm.ALLOWED_ROOT = self._orig_allowed
        self._td.cleanup()

    def test_run_twice_equals_run_once(self):
        # First run: added
        r1 = install(self.roots, target_path=self.target)
        self.assertEqual(r1.result, "added")
        content_1 = open(self.target).read()
        # Second run: already_present
        r2 = install(self.roots, target_path=self.target)
        self.assertEqual(r2.result, "already_present")
        content_2 = open(self.target).read()
        # The file must be byte-identical (no extra binding lines)
        self.assertEqual(content_1, content_2)
        # Exactly one SUPER+SHIFT+R bind
        self.assertEqual(content_2.count('"SUPER+SHIFT+R"'), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
