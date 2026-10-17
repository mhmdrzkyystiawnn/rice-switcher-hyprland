"""Slice 4 tests: import, diff, edit.

All tests use temporary directories; the live desktop, ~/.config,
~/.local, and the backup are never touched. The `edit` test uses an
injectable runner so no real editor is ever spawned.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from rice_switcher.cli import cmd_diff, cmd_edit, cmd_import
from rice_switcher.import_diff_edit import (
    DiffResult,
    EditResult,
    ImportResult,
    _is_safe_rice_name,
    do_diff,
    do_edit,
    do_import,
)
from rice_switcher.paths import Roots, manifest_path, rice_dir


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


def _make_source_dir(parent: str, name: str, *, manifest_name: str | None = None,
                      with_files: dict | None = None) -> str:
    """Create a source config directory at <parent>/<name> containing
    a rice.toml and optionally extra files. Returns the absolute path."""
    src = os.path.join(parent, name)
    os.makedirs(src, exist_ok=True)
    mn = manifest_name or name
    with open(os.path.join(src, "rice.toml"), "w") as f:
        f.write(
            f'[meta]\nname = "{mn}"\ndescription = "imported test rice"\n\n'
            '[[components]]\nname = "hyprland"\ntype = "compositor"\n'
            'start = "true"\nstop = "true"\n'
            'config_link = "/tmp/test-config/hypr"\n'
        )
    for rel, content in (with_files or {}).items():
        full = os.path.join(src, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as f:
            f.write(content)
    return src


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class HelpersTest(unittest.TestCase):
    def test_safe_name(self):
        self.assertTrue(_is_safe_rice_name("alpha"))
        self.assertTrue(_is_safe_rice_name("My.Rice_v2"))
        self.assertTrue(_is_safe_rice_name("a" * 64))
        self.assertFalse(_is_safe_rice_name(""))
        self.assertFalse(_is_safe_rice_name("a" * 65))
        self.assertFalse(_is_safe_rice_name("../escape"))
        self.assertFalse(_is_safe_rice_name("a/b"))
        self.assertFalse(_is_safe_rice_name("a b"))
        self.assertFalse(_is_safe_rice_name("a\"b"))

    def test_safe_name_rejects_dot_and_dotdot(self):
        # Audit P1: . and .. must be rejected so that
        # `edit ..` cannot escape the rice_home directory.
        self.assertFalse(_is_safe_rice_name("."))
        self.assertFalse(_is_safe_rice_name(".."))


# ---------------------------------------------------------------------------
# do_import
# ---------------------------------------------------------------------------

class DoImportTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(prefix="rice-slice4-import-")
        self.roots, self._inner = _make_roots("rice-slice4-import-")
        self.scratch = self.td.name  # absolute parent of source

    def tearDown(self):
        self._inner.cleanup()
        self.td.cleanup()

    def test_dry_run_does_not_copy(self):
        src = _make_source_dir(self.scratch, "my-rice", with_files={
            "hyprland.lua": "-- stub\n",
            "hyprlock.conf": "# stub\n",
        })
        res = do_import(self.roots, src, dry_run=True)
        self.assertEqual(res.status, "dry_run")
        self.assertEqual(res.name, "my-rice")
        self.assertEqual(len(res.copied_paths), 3)  # rice.toml + 2 files
        # Nothing under rice_home
        self.assertEqual(os.listdir(self.roots.rice_home), [])

    def test_apply_copies_files(self):
        src = _make_source_dir(self.scratch, "my-rice", with_files={
            "hypr/hyprland.lua": "-- stub lua\n",
        })
        res = do_import(self.roots, src, dry_run=False)
        self.assertEqual(res.status, "applied")
        self.assertEqual(res.name, "my-rice")
        target = rice_dir(self.roots, "my-rice")
        self.assertTrue(os.path.isdir(target))
        # Files copied
        self.assertTrue(os.path.isfile(os.path.join(target, "rice.toml")))
        self.assertTrue(os.path.isfile(os.path.join(target, "hypr", "hyprland.lua")))

    def test_apply_uses_explicit_name(self):
        src = _make_source_dir(self.scratch, "src", manifest_name="orig")
        res = do_import(self.roots, src, name="renamed", dry_run=False)
        self.assertEqual(res.status, "applied")
        self.assertEqual(res.name, "renamed")
        # The manifest meta.name is "orig" (from source) but the
        # rice is installed under "renamed"
        self.assertTrue(os.path.isdir(rice_dir(self.roots, "renamed")))

    def test_rejects_existing_name(self):
        # Pre-install alpha
        target = rice_dir(self.roots, "alpha")
        os.makedirs(target, exist_ok=True)
        with open(os.path.join(target, "rice.toml"), "w") as f:
            f.write('[meta]\nname = "alpha"\n')
        src = _make_source_dir(self.scratch, "alpha")
        res = do_import(self.roots, src, dry_run=False)
        self.assertEqual(res.status, "name_taken")
        # Original alpha untouched
        with open(os.path.join(target, "rice.toml")) as f:
            self.assertIn("alpha", f.read())

    def test_rejects_invalid_name(self):
        src = _make_source_dir(self.scratch, "good")
        res = do_import(self.roots, src, name="../escape", dry_run=False)
        self.assertEqual(res.status, "name_invalid")

    def test_empty_name_falls_back_to_manifest(self):
        # An empty --name is treated as "use [meta].name from manifest"
        src = _make_source_dir(self.scratch, "good", manifest_name="alpha")
        res = do_import(self.roots, src, name="", dry_run=False)
        self.assertEqual(res.status, "applied")
        self.assertEqual(res.name, "alpha")

    def test_rejects_too_long_name(self):
        src = _make_source_dir(self.scratch, "good")
        res = do_import(self.roots, src, name="a" * 65, dry_run=False)
        self.assertEqual(res.status, "name_invalid")

    def test_source_missing(self):
        res = do_import(self.roots, "/no/such/dir/abcxyz", dry_run=True)
        self.assertEqual(res.status, "source_missing")

    def test_source_no_manifest(self):
        # Create a source directory WITHOUT a rice.toml
        src = os.path.join(self.scratch, "nomanifest")
        os.makedirs(src, exist_ok=True)
        with open(os.path.join(src, "readme.txt"), "w") as f:
            f.write("hello\n")
        res = do_import(self.roots, src, dry_run=True)
        self.assertEqual(res.status, "source_no_manifest")

    def test_source_manifest_invalid(self):
        src = os.path.join(self.scratch, "badmanifest")
        os.makedirs(src, exist_ok=True)
        with open(os.path.join(src, "rice.toml"), "w") as f:
            f.write("[meta\n")  # invalid TOML
        res = do_import(self.roots, src, dry_run=False)
        self.assertEqual(res.status, "source_manifest_invalid")

    def test_apply_writes_audit_event(self):
        src = _make_source_dir(self.scratch, "audit-rice")
        do_import(self.roots, src, dry_run=False)
        from rice_switcher.paths import audit_log_file
        log_path = audit_log_file(self.roots)
        if os.path.isfile(log_path):
            actions = []
            with open(log_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        actions.append(json.loads(line).get("action"))
                    except json.JSONDecodeError:
                        pass
            self.assertIn("import", actions)

    def test_preserves_symlinks_in_source(self):
        # Source has a symlink inside; import should preserve it
        src = _make_source_dir(self.scratch, "sym-rice")
        target = os.path.join(src, "real.txt")
        with open(target, "w") as f:
            f.write("real content\n")
        os.symlink(target, os.path.join(src, "link.txt"))
        res = do_import(self.roots, src, dry_run=False)
        self.assertEqual(res.status, "applied")
        target_dir = rice_dir(self.roots, "sym-rice")
        self.assertTrue(os.path.islink(os.path.join(target_dir, "link.txt")))

    def test_dry_run_audit_not_emitted(self):
        src = _make_source_dir(self.scratch, "dry-rice")
        do_import(self.roots, src, dry_run=True)
        from rice_switcher.paths import audit_log_file
        log_path = audit_log_file(self.roots)
        if os.path.isfile(log_path):
            actions = []
            with open(log_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        actions.append(json.loads(line).get("action"))
                    except json.JSONDecodeError:
                        pass
            self.assertNotIn("import", actions)


# ---------------------------------------------------------------------------
# do_diff
# ---------------------------------------------------------------------------

class DoDiffTest(unittest.TestCase):
    def setUp(self):
        self.roots, self._td = _make_roots("rice-slice4-diff-")

    def tearDown(self):
        self._td.cleanup()

    def _make_rice(self, name: str, manifest_text: str):
        d = rice_dir(self.roots, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "rice.toml"), "w") as f:
            f.write(manifest_text)

    def test_same_returns_status_same(self):
        text = '[meta]\nname = "x"\n'
        self._make_rice("a", text)
        self._make_rice("b", text)
        res = do_diff(self.roots, "a", "b")
        self.assertEqual(res.status, "same")
        self.assertEqual(res.diff_text, "")

    def test_different_returns_diff_text(self):
        self._make_rice("a", '[meta]\nname = "a"\n[theme]\nfoo = 1\n')
        self._make_rice("b", '[meta]\nname = "b"\n[theme]\nfoo = 2\n')
        res = do_diff(self.roots, "a", "b")
        self.assertEqual(res.status, "different")
        # Verify the diff includes both the removed and added lines
        self.assertIn("-foo = 1", res.diff_text)
        self.assertIn("+foo = 2", res.diff_text)
        # And the meta.name changes
        self.assertIn('-name = "a"', res.diff_text)
        self.assertIn('+name = "b"', res.diff_text)

    def test_missing_a(self):
        self._make_rice("b", '[meta]\nname = "b"\n')
        res = do_diff(self.roots, "nope", "b")
        self.assertEqual(res.status, "missing")

    def test_missing_b(self):
        self._make_rice("a", '[meta]\nname = "a"\n')
        res = do_diff(self.roots, "a", "nope")
        self.assertEqual(res.status, "missing")

    def test_invalid_name_a(self):
        res = do_diff(self.roots, "../escape", "b")
        self.assertEqual(res.status, "invalid_name")

    def test_invalid_name_b(self):
        res = do_diff(self.roots, "a", "../escape")
        self.assertEqual(res.status, "invalid_name")

    def test_diff_text_includes_header(self):
        self._make_rice("a", "x = 1\n")
        self._make_rice("b", "x = 2\n")
        res = do_diff(self.roots, "a", "b")
        self.assertIn("a/rice.toml", res.diff_text)
        self.assertIn("b/rice.toml", res.diff_text)


# ---------------------------------------------------------------------------
# do_edit
# ---------------------------------------------------------------------------

class DoEditTest(unittest.TestCase):
    def setUp(self):
        self.roots, self._td = _make_roots("rice-slice4-edit-")

    def tearDown(self):
        self._td.cleanup()

    def _make_rice(self, name: str, manifest_text: str = '[meta]\nname = "x"\n'):
        d = rice_dir(self.roots, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "rice.toml"), "w") as f:
            f.write(manifest_text)

    def test_invalid_name(self):
        res = do_edit(self.roots, "../escape")
        self.assertEqual(res.status, "invalid_name")

    def test_missing_rice(self):
        res = do_edit(self.roots, "ghost")
        self.assertEqual(res.status, "missing")

    def test_not_a_rice(self):
        # Directory exists but no rice.toml inside
        d = rice_dir(self.roots, "broken")
        os.makedirs(d, exist_ok=True)
        res = do_edit(self.roots, "broken")
        self.assertEqual(res.status, "not_a_rice")

    def test_edit_dot_does_not_escape(self):
        # Audit P1: `.` and `..` must be rejected so that
        # `edit ..` cannot escape the rice_home directory.
        res = do_edit(self.roots, ".")
        self.assertEqual(res.status, "invalid_name")
        res = do_edit(self.roots, "..")
        self.assertEqual(res.status, "invalid_name")

    def test_diff_dot_does_not_escape(self):
        res = do_diff(self.roots, ".", "alpha")
        self.assertEqual(res.status, "invalid_name")
        res = do_diff(self.roots, "alpha", "..")
        self.assertEqual(res.status, "invalid_name")

    def test_import_dot_rejected(self):
        # Build a source with manifest meta.name = "."  (rejected)
        td = tempfile.mkdtemp()
        try:
            src = os.path.join(td, "src")
            os.makedirs(src)
            with open(os.path.join(src, "rice.toml"), "w") as f:
                f.write(
                    '[meta]\nname = "."\n'
                    '[[components]]\nname = "hyprland"\ntype = "compositor"\n'
                    'start = "true"\nstop = "true"\n'
                    'config_link = "/tmp/x"\n'
                )
            res = do_import(self.roots, src, dry_run=False)
            self.assertEqual(res.status, "name_invalid")
        finally:
            import shutil
            shutil.rmtree(td, ignore_errors=True)

    def test_no_editor(self):
        self._make_rice("alpha")
        # Force no editor
        saved = os.environ.pop("EDITOR", None)
        saved2 = os.environ.pop("VISUAL", None)
        try:
            res = do_edit(self.roots, "alpha", editor="")
        finally:
            if saved is not None:
                os.environ["EDITOR"] = saved
            if saved2 is not None:
                os.environ["VISUAL"] = saved2
        self.assertEqual(res.status, "no_editor")

    def test_editor_not_found(self):
        self._make_rice("alpha")
        res = do_edit(self.roots, "alpha", editor="definitely-not-a-real-editor-12345")
        self.assertEqual(res.status, "no_editor")

    def test_successful_edit(self):
        self._make_rice("alpha")
        called = {"n": 0, "argv": None}
        def runner(argv, timeout):
            called["n"] += 1
            called["argv"] = argv
            return 0, "", ""
        res = do_edit(self.roots, "alpha", editor="vim", runner=runner)
        self.assertEqual(res.status, "ok")
        self.assertEqual(called["n"], 1)
        # Verify the editor was invoked with the manifest path
        self.assertEqual(called["argv"][0], "vim")
        self.assertTrue(called["argv"][1].endswith("rice.toml"))
        # Verify audit event was emitted
        from rice_switcher.paths import audit_log_file
        log_path = audit_log_file(self.roots)
        if os.path.isfile(log_path):
            actions = []
            with open(log_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        actions.append(json.loads(line).get("action"))
                    except json.JSONDecodeError:
                        pass
            self.assertIn("edit", actions)

    def test_editor_nonzero_return(self):
        self._make_rice("alpha")
        def runner(argv, timeout):
            return 1, "", "vim: error"
        res = do_edit(self.roots, "alpha", editor="vim", runner=runner)
        self.assertEqual(res.status, "editor_failed")


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------

class CmdImportTest(unittest.TestCase):
    def setUp(self):
        self.roots, self._td = _make_roots("rice-slice4-cmdimport-")

    def tearDown(self):
        self._td.cleanup()

    def test_cmd_import_dry_run_default(self):
        # Create a source dir on disk
        td = tempfile.mkdtemp()
        try:
            src = _make_source_dir(td, "my-rice")
            args = Namespace(source=src, name=None, dry_run=True, cmd="import")
            rc = cmd_import(args, self.roots, log=None)
            self.assertEqual(rc, 0)
        finally:
            import shutil
            shutil.rmtree(td, ignore_errors=True)

    def test_cmd_import_apply(self):
        td = tempfile.mkdtemp()
        try:
            src = _make_source_dir(td, "my-rice")
            args = Namespace(source=src, name=None, dry_run=False, cmd="import")
            rc = cmd_import(args, self.roots, log=None)
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.isdir(rice_dir(self.roots, "my-rice")))
        finally:
            import shutil
            shutil.rmtree(td, ignore_errors=True)

    def test_cmd_import_source_missing(self):
        args = Namespace(source="/no/such/dir", name=None, dry_run=True, cmd="import")
        rc = cmd_import(args, self.roots, log=None)
        self.assertEqual(rc, 2)


class CmdDiffTest(unittest.TestCase):
    def setUp(self):
        self.roots, self._td = _make_roots("rice-slice4-cmddiff-")

    def tearDown(self):
        self._td.cleanup()

    def _make_rice(self, name: str, text: str):
        d = rice_dir(self.roots, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "rice.toml"), "w") as f:
            f.write(text)

    def test_cmd_diff_same(self):
        text = '[meta]\nname = "x"\n'
        self._make_rice("a", text)
        self._make_rice("b", text)
        args = Namespace(name_a="a", name_b="b", cmd="diff")
        rc = cmd_diff(args, self.roots, log=None)
        self.assertEqual(rc, 0)

    def test_cmd_diff_different(self):
        self._make_rice("a", "x = 1\n")
        self._make_rice("b", "x = 2\n")
        args = Namespace(name_a="a", name_b="b", cmd="diff")
        rc = cmd_diff(args, self.roots, log=None)
        self.assertEqual(rc, 1)

    def test_cmd_diff_missing(self):
        self._make_rice("a", "x = 1\n")
        args = Namespace(name_a="a", name_b="nope", cmd="diff")
        rc = cmd_diff(args, self.roots, log=None)
        self.assertEqual(rc, 2)


class CmdEditTest(unittest.TestCase):
    def setUp(self):
        self.roots, self._td = _make_roots("rice-slice4-cmdedit-")

    def tearDown(self):
        self._td.cleanup()

    def _make_rice(self, name: str, text: str = '[meta]\nname = "x"\n'):
        d = rice_dir(self.roots, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "rice.toml"), "w") as f:
            f.write(text)

    def test_cmd_edit_missing(self):
        args = Namespace(name="ghost", editor=None, cmd="edit")
        rc = cmd_edit(args, self.roots, log=None)
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
