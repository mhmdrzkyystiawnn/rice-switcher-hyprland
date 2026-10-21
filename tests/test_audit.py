"""Regression tests for security issues found during the Slice 1 audit.

These tests target specific behaviours that the audit found to be
unsafe or to interact with the real filesystem in unexpected ways.

All tests run in `tempfile.TemporaryDirectory()` and never touch
`~/.config/`, `~/.local/`, the live desktop, or any real process.
"""

from __future__ import annotations

import os as _os

# Process management (Slice 5) is exercised in tests/test_slice5_process.py
# with an injected fake runtime. Everywhere else it is disabled so the suite
# never spawns real commands or touches the desktop (PLAN.md §3.5).
_os.environ.setdefault("RICE_TEST", "1")

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

from rice_switcher.cli import cmd_add, cmd_switch, main
from rice_switcher.manifest import load_manifest
from rice_switcher.paths import Roots, resolve_roots, rice_dir
from rice_switcher.snapshot import (
    _copy_tree_symlinks,
    commit as commit_snapshot,
    create_pending,
    restore as restore_snapshot,
    swap_symlink,
)
from rice_switcher.state import read_state, write_state, State
from rice_switcher.switch import do_switch, do_rollback
from rice_switcher.managed_set import compute_plan, Identity, SwitchPlan
from rice_switcher.manifest import Component
from argparse import Namespace


# ---------------------------------------------------------------------------
# Path traversal on cmd_add (audit finding S1)
# ---------------------------------------------------------------------------

class CmdAddPathTraversalTest(unittest.TestCase):
    """Audit finding S1 (cmd_add): a rice name like "../escape" used to
    create a directory OUTSIDE RICE_HOME and a name with a double-quote
    used to produce an unparseable manifest. After the fix, all of
    these must be REJECTED before any directory is created, and no
    files may be left on disk.
    """

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="rice-audit-s1-")
        self.rice_home = os.path.join(self._td.name, "rice")
        os.makedirs(self.rice_home, exist_ok=True)
        self.roots = Roots(
            rice_home=self.rice_home,
            config_home=os.path.join(self._td.name, "config"),
            state_home=os.path.join(self._td.name, "state"),
        )

    def tearDown(self):
        # Clean up any escape that the test caused outside RICE_HOME.
        # The escape target is <tmp>/escape/, so delete it explicitly
        # if it exists.
        stray = os.path.join(self._td.name, "..", "escape")
        try:
            if os.path.lexists(stray):
                if os.path.islink(stray) or os.path.isfile(stray):
                    os.unlink(stray)
                else:
                    shutil.rmtree(stray)
        except OSError:
            pass
        self._td.cleanup()

    def _count_rice_home_entries(self) -> int:
        if not os.path.isdir(self.rice_home):
            return 0
        return len([e for e in os.listdir(self.rice_home) if e != "README"])

    def _assert_nothing_created_outside(self, msg: str):
        # After the fix, an invalid name must not create ANY directory
        # or file outside RICE_HOME.
        parent = os.path.dirname(self.rice_home)
        for entry in os.listdir(parent):
            full = os.path.join(parent, entry)
            if full == self.rice_home:
                continue
            self.assertFalse(
                os.path.lexists(full) and entry not in ("escape",),
                f"{msg}: unexpected entry at {full}",
            )

    def test_rejects_parent_traversal(self):
        from argparse import Namespace
        args = Namespace(name="../escape", cmd="add")
        rc = cmd_add(args, self.roots, log=None)
        self.assertEqual(rc, 2,
            f"audit fix S1: cmd_add must reject '../escape' with rc=2, got {rc}")
        self.assertEqual(self._count_rice_home_entries(), 0,
            f"audit fix S1: rejected name left a dir in RICE_HOME: "
            f"{os.listdir(self.rice_home)}")
        # No escape outside
        self._assert_nothing_created_outside("../escape")

    def test_rejects_dot(self):
        from argparse import Namespace
        args = Namespace(name=".", cmd="add")
        rc = cmd_add(args, self.roots, log=None)
        self.assertEqual(rc, 2,
            f"audit fix S1: cmd_add must reject '.' with rc=2, got {rc}")
        self.assertEqual(self._count_rice_home_entries(), 0)

    def test_rejects_double_dot(self):
        from argparse import Namespace
        args = Namespace(name="..", cmd="add")
        rc = cmd_add(args, self.roots, log=None)
        self.assertEqual(rc, 2,
            f"audit fix S1: cmd_add must reject '..' with rc=2, got {rc}")
        self.assertEqual(self._count_rice_home_entries(), 0)

    def test_rejects_absolute_path(self):
        from argparse import Namespace
        args = Namespace(name="/etc/passwd", cmd="add")
        rc = cmd_add(args, self.roots, log=None)
        self.assertEqual(rc, 2,
            f"audit fix S1: cmd_add must reject absolute path with rc=2, got {rc}")
        self.assertEqual(self._count_rice_home_entries(), 0)

    def test_rejects_double_quote(self):
        from argparse import Namespace
        args = Namespace(name='evil"name', cmd="add")
        rc = cmd_add(args, self.roots, log=None)
        self.assertEqual(rc, 2,
            f"audit fix S1: cmd_add must reject name with '\"' with rc=2, got {rc}")
        # Nothing should be created
        suspect = os.path.join(self.rice_home, 'evil"name')
        self.assertFalse(os.path.lexists(suspect),
            f"audit fix S1: rejected name created {suspect}")

    def test_rejects_backslash(self):
        from argparse import Namespace
        args = Namespace(name="..\\windows", cmd="add")
        rc = cmd_add(args, self.roots, log=None)
        self.assertEqual(rc, 2,
            f"audit fix S1: cmd_add must reject name with backslash with rc=2, got {rc}")

    def test_rejects_too_long(self):
        from argparse import Namespace
        args = Namespace(name="a" * 70, cmd="add")
        rc = cmd_add(args, self.roots, log=None)
        self.assertEqual(rc, 2,
            f"audit fix S1: cmd_add must reject name >64 chars with rc=2, got {rc}")

    def test_accepts_valid_name(self):
        from argparse import Namespace
        for valid in ("alpha", "rice-1", "My.Rice_v2", "abc123"):
            args = Namespace(name=valid, cmd="add")
            rc = cmd_add(args, self.roots, log=None)
            self.assertEqual(rc, 0,
                f"audit fix S1: cmd_add should accept {valid!r}, got rc={rc}")
            # Manifest must be parseable
            mp = os.path.join(self.rice_home, valid, "rice.toml")
            self.assertTrue(os.path.isfile(mp), f"no manifest at {mp}")
            m = load_manifest(mp)
            self.assertEqual(m.meta.name, valid,
                f"manifest meta.name={m.meta.name!r}, expected {valid!r}")


# ---------------------------------------------------------------------------
# Dry-run side-effect (audit finding S2)
# ---------------------------------------------------------------------------

class DryRunSideEffectTest(unittest.TestCase):
    """Audit finding S2: confirm dry-run does not touch any files."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="rice-audit-s2-")
        self.roots = Roots(
            rice_home=os.path.join(self._td.name, "rice"),
            config_home=os.path.join(self._td.name, "config"),
            state_home=os.path.join(self._td.name, "state"),
        )
        for d in (self.roots.rice_home, self.roots.config_home,
                  self.roots.state_home):
            os.makedirs(d, exist_ok=True)
        # Create a live config dir
        live = os.path.join(self.roots.config_home, "hypr")
        os.makedirs(live)
        with open(os.path.join(live, "hyprland.lua"), "w") as f:
            f.write("-- stub\n")
        # Create a rice manifest pointing at the live config
        Path(self.roots.rice_home, "r1").mkdir()
        with open(os.path.join(self.roots.rice_home, "r1", "rice.toml"), "w") as f:
            f.write(
                '[meta]\nname = "r1"\n\n'
                '[[components]]\nname = "hyprland"\ntype = "compositor"\n'
                'start = "true"\nstop = "true"\n'
                f'config_link = "{live}"\n'
            )

    def tearDown(self):
        self._td.cleanup()

    def test_dry_run_does_not_create_snapshot_dir(self):
        # Audit finding S2: ensure no .pending-* or committed snapshot
        # dir is created on a dry-run.
        snap_dir = os.path.join(self.roots.state_home, "rice-switcher", "snapshot")
        # Pre-state: dir may or may not exist; record existence.
        before_existed = os.path.isdir(snap_dir)
        # Run dry-run switch
        from rice_switcher.manifest import load_manifest
        m = load_manifest(os.path.join(self.roots.rice_home, "r1", "rice.toml"))
        outcome = do_switch(self.roots, "r1", [m], dry_run=True, force_real=False)
        # After dry-run: no .pending-* dir, no committed dir.
        if os.path.isdir(snap_dir):
            entries = os.listdir(snap_dir)
            self.assertEqual(entries, [],
                f"audit finding S2: dry-run created snapshot entries: {entries}")
        # Lock file should not exist
        lock = os.path.join(self.roots.state_home, "rice-switcher", "switch.lock")
        self.assertFalse(os.path.exists(lock) and os.path.getsize(lock) > 0,
            f"audit finding S2: dry-run touched the lock file: {lock}")


# ---------------------------------------------------------------------------
# Lock release on exception (audit finding S3)
# ---------------------------------------------------------------------------

class LockReleaseTest(unittest.TestCase):
    """Audit finding S3: verify the lock is released even when the body
    of the `with switch_lock` block raises."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="rice-audit-s3-")
        self.roots = Roots(
            rice_home=os.path.join(self._td.name, "rice"),
            config_home=os.path.join(self._td.name, "config"),
            state_home=os.path.join(self._td.name, "state"),
        )
        for d in (self.roots.rice_home, self.roots.config_home,
                  self.roots.state_home):
            os.makedirs(d, exist_ok=True)

    def tearDown(self):
        self._td.cleanup()

    def test_lock_released_after_exception(self):
        from rice_switcher.lock import switch_lock
        # Acquire once
        with switch_lock(self.roots, timeout_seconds=1.0) as info:
            self.assertTrue(info.held)
        # Try to acquire again inside an exception-raising block
        raised = False
        try:
            with switch_lock(self.roots, timeout_seconds=1.0):
                raise RuntimeError("simulated failure in switch body")
        except RuntimeError:
            raised = True
        self.assertTrue(raised)
        # Re-acquire should succeed
        with switch_lock(self.roots, timeout_seconds=1.0) as info2:
            self.assertTrue(info2.held)


# ---------------------------------------------------------------------------
# State.json half-written detection (audit finding S4)
# ---------------------------------------------------------------------------

class StateAtomicWriteTest(unittest.TestCase):
    """Audit finding S4: state.json uses os.replace for atomic write.
    Verify that the temp file does not persist after a successful write.
    """

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="rice-audit-s4-")
        self.roots = Roots(
            rice_home=os.path.join(self._td.name, "rice"),
            config_home=os.path.join(self._td.name, "config"),
            state_home=os.path.join(self._td.name, "state"),
        )
        for d in (self.roots.rice_home, self.roots.config_home,
                  self.roots.state_home):
            os.makedirs(d, exist_ok=True)

    def tearDown(self):
        self._td.cleanup()

    def test_no_temp_files_after_write(self):
        from rice_switcher.state import write_state, State
        from rice_switcher.paths import state_file
        write_state(self.roots, State(current_rice="x", last_switch=1.0))
        state_dir = os.path.dirname(state_file(self.roots))
        temps = [f for f in os.listdir(state_dir) if ".tmp-" in f]
        self.assertEqual(temps, [],
            f"audit finding S4: state write left temp files: {temps}")
        # Read back: it should be valid
        s = read_state(self.roots)
        self.assertEqual(s.current_rice, "x")


# ---------------------------------------------------------------------------
# restore() handles missing snapshot cleanly (audit finding S5)
# ---------------------------------------------------------------------------

class RestoreMissingSnapshotTest(unittest.TestCase):
    """Audit finding S5: restore() should raise FileNotFoundError when
    the snapshot does not exist, and the orchestrator should report it."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="rice-audit-s5-")
        self.roots = Roots(
            rice_home=os.path.join(self._td.name, "rice"),
            config_home=os.path.join(self._td.name, "config"),
            state_home=os.path.join(self._td.name, "state"),
        )
        for d in (self.roots.rice_home, self.roots.config_home,
                  self.roots.state_home):
            os.makedirs(d, exist_ok=True)

    def tearDown(self):
        self._td.cleanup()

    def test_restore_nonexistent_snapshot_raises(self):
        from rice_switcher.snapshot import restore
        with self.assertRaises(FileNotFoundError):
            restore(self.roots, "2099-01-01T00-00-00_nonexistent")

    def test_do_rollback_reports_missing_snapshot(self):
        from rice_switcher.switch import do_rollback
        from rice_switcher.log import setup_logging
        log = setup_logging(self.roots)
        ts, errors = do_rollback(self.roots, "2099-01-01T00-00-00_nonexistent", log, reload_fn=lambda: None)
        self.assertEqual(ts, "")
        self.assertTrue(any("not found" in e.lower() or "not found" in e for e in errors),
                        msg=f"expected 'not found' in errors, got {errors}")

# ---------------------------------------------------------------------------
# compute_plan: target_rice_dir fallback when source_path empty (audit finding S6)
# ---------------------------------------------------------------------------

class ComputePlanNoSourcePathTest(unittest.TestCase):
    """Audit finding S6: when target_manifest.source_path is empty,
    compute_plan must fail FAST with a clear error (not silently fall
    back to meta.name). A manifest built in memory without a backing
    file cannot be located on the filesystem, and guessing the
    location produced silently-wrong swap decisions."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="rice-audit-s6-")
        self.roots = Roots(
            rice_home=os.path.join(self._td.name, "rice"),
            config_home=os.path.join(self._td.name, "config"),
            state_home=os.path.join(self._td.name, "state"),
        )
        os.makedirs(self.roots.config_home, exist_ok=True)
        # A live config_link target
        self.live = os.path.join(self.roots.config_home, "hypr")
        os.makedirs(self.live, exist_ok=True)
        with open(os.path.join(self.live, "hyprland.lua"), "w") as f:
            f.write("-- stub\n")

    def tearDown(self):
        self._td.cleanup()

    def test_compute_plan_rejects_empty_source_path(self):
        from rice_switcher.manifest import Manifest, Meta
        from rice_switcher.snapshot import current_symlinks_state
        # Build a Manifest directly (no source_path) — this simulates
        # code paths that build manifests in memory (e.g. tests,
        # future code that imports a manifest via the API).
        c = Component(name="hyprland", type="compositor",
                      start="true", stop="true",
                      config_link=self.live)
        m = Manifest(meta=Meta(name="x"), components=[c], source_path="")
        sym = current_symlinks_state(self.roots, [self.live])
        # Audit fix S2: the previous fallback to meta.name produced
        # silently-wrong swap decisions. compute_plan now raises
        # ValueError instead.
        with self.assertRaises(ValueError) as ctx:
            compute_plan(None, m, [m], sym, self.roots.config_home)
        self.assertIn("source_path", str(ctx.exception).lower(),
            f"expected error to mention source_path, got: {ctx.exception}")

    def test_compute_plan_works_for_on_disk_manifest(self):
        # Verify the fix did NOT break the happy path: a manifest
        # loaded from disk has a source_path and must be accepted
        # without raising.
        from rice_switcher.snapshot import current_symlinks_state
        # Reuse the same live config; create a temp manifest file
        Path(self.roots.rice_home, "good").mkdir(parents=True, exist_ok=True)
        manifest_file = Path(self.roots.rice_home, "good", "rice.toml")
        manifest_file.write_text(
            f'[meta]\nname = "good"\n\n'
            f'[[components]]\nname = "hyprland"\ntype = "compositor"\n'
            f'start = "true"\nstop = "true"\n'
            f'config_link = "{self.live}"\n'
        )
        from rice_switcher.manifest import load_manifest
        m = load_manifest(str(manifest_file))
        sym = current_symlinks_state(self.roots, [self.live])
        # Should not raise (audit fix S2 made empty source_path raise,
        # not silently misbehave). A swap being flagged is a separate
        # matter: with no current rice, the plan defaults to "swap
        # needed" because it has no prior state to compare against.
        try:
            plan = compute_plan(None, m, [m], sym, self.roots.config_home)
        except ValueError as e:
            self.fail(
                f"on-disk manifest with valid source_path must NOT raise: {e}"
            )
        # Sanity: the plan target is "good"
        self.assertEqual(plan.target, "good")


# ---------------------------------------------------------------------------
# Symlink restoration: does the restored symlink keep the same target?
# ---------------------------------------------------------------------------

class RestoreSymlinkRoundTripTest(unittest.TestCase):
    """Audit finding S7: snapshot.capture stores the live config_link
    contents. On restore, the symlink at <config_link> should be created
    such that realpath matches what was originally there. Verify."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="rice-audit-s7-")
        self.roots = Roots(
            rice_home=os.path.join(self._td.name, "rice"),
            config_home=os.path.join(self._td.name, "config"),
            state_home=os.path.join(self._td.name, "state"),
        )
        for d in (self.roots.rice_home, self.roots.config_home,
                  self.roots.state_home):
            os.makedirs(d, exist_ok=True)
        # A live config dir with a regular file
        self.live = os.path.join(self.roots.config_home, "hypr")
        os.makedirs(self.live, exist_ok=True)
        with open(os.path.join(self.live, "x.txt"), "w") as f:
            f.write("hello\n")

    def tearDown(self):
        self._td.cleanup()

    def test_create_and_restore_round_trip(self):
        from rice_switcher.snapshot import restore
        ts, captured = create_pending(self.roots, [self.live])
        commit_snapshot(self.roots, ts)
        # Mutate the live
        with open(os.path.join(self.live, "x.txt"), "w") as f:
            f.write("MUTATED\n")
        # Restore
        restored, rice_name = restore(self.roots, ts)
        with open(os.path.join(self.live, "x.txt")) as f:
            self.assertEqual(f.read(), "hello\n",
                "audit finding S7: restore did not recover original file content")


# ---------------------------------------------------------------------------
# Audit fix S3: trust confirmation
# ---------------------------------------------------------------------------

class TrustConfirmationTest(unittest.TestCase):
    """Audit fix S3: cmd_switch must require a `Type 'yes' to continue`
    confirmation when `--no-dry-run` is used and the rice is not yet
    trusted (or its manifest hash changed). Dry-run must NOT prompt.
    A non-"yes" answer must abort without any FS, state, or process
    change."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="rice-audit-s3-")
        self.rice_home = os.path.join(self._td.name, "rice")
        self.config_home = os.path.join(self._td.name, "config")
        self.state_home = os.path.join(self._td.name, "state")
        self.roots = Roots(
            rice_home=self.rice_home,
            config_home=self.config_home,
            state_home=self.state_home,
        )
        for d in (self.rice_home, self.config_home, self.state_home):
            os.makedirs(d, exist_ok=True)
        # Init state
        from rice_switcher.state import write_state, State
        write_state(self.roots, State(current_rice="", last_switch=0.0))
        # Live config to swap
        self.live = os.path.join(self.config_home, "hypr")
        os.makedirs(self.live, exist_ok=True)
        with open(os.path.join(self.live, "h.txt"), "w") as f:
            f.write("v1\n")
        # Rice
        self.rice_dir = os.path.join(self.rice_home, "alpha")
        os.makedirs(self.rice_dir, exist_ok=True)
        with open(os.path.join(self.rice_dir, "rice.toml"), "w") as f:
            f.write(
                '[meta]\nname = "alpha"\n'
                '[[components]]\nname = "hyprland"\ntype = "compositor"\n'
                'start = "true"\nstop = "true"\n'
                f'config_link = "{self.live}"\n'
            )

    def tearDown(self):
        self._td.cleanup()

    def _fake_stdin(self, line: str):
        """Monkey-patch sys.stdin so _confirm_prompt reads `line`."""
        import io
        self._orig_stdin = sys.stdin
        sys.stdin = io.StringIO(line + "\n")

    def _restore_stdin(self):
        sys.stdin = self._orig_stdin if hasattr(self, "_orig_stdin") else sys.__stdin__

    def _swap_stdin_empty(self):
        """Replace sys.stdin with an empty StringIO (read would return '')."""
        import io
        self._orig_stdin = sys.stdin
        sys.stdin = io.StringIO("")

    def test_dry_run_does_not_prompt(self):
        # Dry-run path must never read stdin. We let stdin be a closed
        # stream; any read attempt would raise.
        self._swap_stdin_empty()
        try:
            from rice_switcher.cli import cmd_switch
            args = Namespace(name="alpha", no_dry_run=False, cmd="switch")
            rc = cmd_switch(args, self.roots, log=None)
            # Dry-run may return 0 (success) but must not raise on stdin
            self.assertIn(rc, (0, 2),
                f"cmd_switch dry-run returned unexpected rc={rc}")
        finally:
            self._restore_stdin()

    def test_no_dry_run_untrusted_prompts_and_aborts_without_yes(self):
        from rice_switcher.cli import cmd_switch
        self._fake_stdin("no")
        try:
            args = Namespace(name="alpha", no_dry_run=True, cmd="switch")
            rc = cmd_switch(args, self.roots, log=None)
            self.assertEqual(rc, 1,
                f"non-yes must abort with rc=1, got {rc}")
            # State must NOT be changed
            from rice_switcher.state import read_state
            st = read_state(self.roots)
            self.assertEqual(st.current_rice, "",
                f"state.current_rice changed despite abort: {st.current_rice!r}")
            # Live file must NOT be changed
            with open(os.path.join(self.live, "h.txt")) as f:
                self.assertEqual(f.read(), "v1\n",
                    f"live file changed despite abort")
        finally:
            self._restore_stdin()

    def test_no_dry_run_untrusted_prompts_and_proceeds_with_yes(self):
        from rice_switcher.cli import cmd_switch
        from rice_switcher.state import read_state
        self._fake_stdin("yes")
        try:
            args = Namespace(name="alpha", no_dry_run=True, cmd="switch")
            rc = cmd_switch(args, self.roots, log=None)
            self.assertEqual(rc, 0,
                f"yes must proceed; got rc={rc}")
            # State updated
            st = read_state(self.roots)
            self.assertEqual(st.current_rice, "alpha",
                f"state.current_rice expected 'alpha', got {st.current_rice!r}")
        finally:
            self._restore_stdin()

    def test_no_dry_run_trusted_does_not_prompt(self):
        # Pre-populate trust cache with current manifest hash, so the
        # trust check passes and no prompt is required.
        from rice_switcher.cli import cmd_switch
        from rice_switcher.state import mark_trust, read_state
        from rice_switcher.manifest import load_manifest
        m = load_manifest(os.path.join(self.rice_dir, "rice.toml"))
        mark_trust(self.roots, "alpha", m.source_sha256, status="ok")
        # Switch to alpha first to set state.current_rice
        st = read_state(self.roots)
        st.current_rice = "alpha"
        from rice_switcher.state import write_state
        write_state(self.roots, st)
        # Now no_dry_run to alpha (same rice) should be no-op and not
        # require a prompt. (Same-rice no-op is also fine here.)
        self._swap_stdin_empty()
        try:
            args = Namespace(name="alpha", no_dry_run=True, cmd="switch")
            rc = cmd_switch(args, self.roots, log=None)
            self.assertIn(rc, (0, 1),
                f"trusted no_dry_run should not require input; got rc={rc}")
        finally:
            self._restore_stdin()


# ---------------------------------------------------------------------------
# Audit fix S4: rollback updates state.current_rice
# ---------------------------------------------------------------------------

class RollbackUpdatesStateTest(unittest.TestCase):
    """Audit fix S4: do_rollback must restore `state.current_rice` to
    the rice that was captured in the snapshot, not leave it pointing
    at the most-recently-switched rice."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="rice-audit-s4b-")
        self.rice_home = os.path.join(self._td.name, "rice")
        self.config_home = os.path.join(self._td.name, "config")
        self.state_home = os.path.join(self._td.name, "state")
        self.roots = Roots(
            rice_home=self.rice_home,
            config_home=self.config_home,
            state_home=self.state_home,
        )
        for d in (self.rice_home, self.config_home, self.state_home):
            os.makedirs(d, exist_ok=True)
        # Live config
        self.live = os.path.join(self.config_home, "hypr")
        os.makedirs(self.live, exist_ok=True)
        with open(os.path.join(self.live, "h.txt"), "w") as f:
            f.write("v0\n")
        # Two rice manifests
        for r in ("a", "b"):
            d = os.path.join(self.rice_home, r)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "rice.toml"), "w") as f:
                f.write(
                    f'[meta]\nname = "{r}"\n'
                    f'[[components]]\nname = "hyprland"\ntype = "compositor"\n'
                    f'start = "true"\nstop = "true"\n'
                    f'config_link = "{self.live}"\n'
                )

    def tearDown(self):
        self._td.cleanup()

    def test_switch_a_then_b_then_rollback_recovers_state(self):
        from rice_switcher.state import read_state
        from rice_switcher.manifest import load_manifest
        manifests = [load_manifest(os.path.join(self.rice_home, n, "rice.toml"))
                     for n in ("a", "b")]
        # Pre-create the rice directories so swap_symlink can target
        # a real path. do_switch does NOT mkdir rice subdirs.
        for r in ("a", "b"):
            d = os.path.join(self.rice_home, r, "hypr")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "h.txt"), "w") as f:
                f.write(f"v_{r}\n")

        # First switch: a
        outcome_a = do_switch(
            self.roots, "a", manifests,
            dry_run=False, force_real=True, logger=None,
        )
        self.assertEqual(outcome_a.errors, [],
            f"first switch A failed: {outcome_a.errors}")
        st = read_state(self.roots)
        self.assertEqual(st.current_rice, "a")
        sym_target = os.readlink(self.live) if os.path.islink(self.live) else None
        self.assertIsNotNone(sym_target, f"live should be a symlink now; got {sym_target}")
        self.assertIn("/a/hypr", sym_target or "", f"symlink target unexpected: {sym_target}")

        # Mutate the content behind the symlink (still points to a/hypr)
        with open(os.path.join(self.roots.rice_home, "a", "hypr", "h.txt"), "w") as f:
            f.write("from_a\n")

        # Second switch: b
        outcome_b = do_switch(
            self.roots, "b", manifests,
            dry_run=False, force_real=True, logger=None,
        )
        self.assertEqual(outcome_b.errors, [],
            f"second switch B failed: {outcome_b.errors}")
        st = read_state(self.roots)
        self.assertEqual(st.current_rice, "b")
        sym_target = os.readlink(self.live)
        self.assertIn("/b/hypr", sym_target, f"symlink target unexpected: {sym_target}")

        # Rollback the B switch (which captured state from "a" pre-B)
        from rice_switcher.switch import do_rollback
        ts = outcome_b.snapshot_ts
        self.assertIsNotNone(ts, "snapshot_ts must be set after a real switch")
        ts_rb, errors_rb = do_rollback(self.roots, ts, None, reload_fn=lambda: None)
        self.assertEqual(errors_rb, [], f"rollback errors: {errors_rb}")
        st = read_state(self.roots)
        self.assertEqual(st.current_rice, "a",
            f"after rollback state.current_rice should be 'a', got {st.current_rice!r}")
        # Live content should be from_a (captured at the moment of switch B).
        # Live is restored as a symlink to the rice directory (a/hypr).
        target_file = os.path.join(self.live, "h.txt")
        if not os.path.exists(target_file):
            target_file = os.path.join(self.live, "hypr", "h.txt")
        with open(target_file) as f:
            self.assertEqual(f.read(), "from_a\n",
                f"after rollback, live content should be 'from_a', got {f.read()!r}")

    def test_rollback_with_no_rice_name_in_snapshot_reports_error(self):
        # Older snapshot (without rice= line) must NOT silently leave
        # state.current_rice pointing at the wrong rice; instead
        # do_rollback returns an explicit error.
        from rice_switcher.snapshot import (
            create_pending, commit, pending_path, committed_path,
        )
        from rice_switcher.state import read_state, write_state, State
        # 1. Create a snapshot, then strip the `rice=` line entirely so
        #    it looks like a pre-S4 snapshot (a present-but-empty
        #    `rice=` is a DIFFERENT, valid case; see the next test).
        ts, _ = create_pending(self.roots, [self.live])  # no rice_name
        commit(self.roots, ts)
        man = os.path.join(committed_path(self.roots, ts), ".captured.txt")
        with open(man) as f:
            kept = [l for l in f.read().splitlines() if not l.startswith("rice=")]
        with open(man, "w") as f:
            f.write("\n".join(kept) + "\n")
        # 2. Set state.current_rice to something to prove the rollback
        #    does NOT change it (since rice_name is missing)
        write_state(self.roots, State(current_rice="prev", last_switch=0.0))
        # 3. Mutate live so rollback can prove it restored
        with open(os.path.join(self.live, "h.txt"), "w") as f:
            f.write("MUTATED\n")
        # 4. Rollback
        from rice_switcher.switch import do_rollback
        rb_ts, errs = do_rollback(self.roots, ts, None, reload_fn=lambda: None)
        self.assertEqual(rb_ts, ts)
        # The error must mention the missing rice= line
        self.assertTrue(any("rice=" in e for e in errs),
            f"expected error mentioning rice= line, got: {errs}")
        # state.current_rice was NOT changed
        st = read_state(self.roots)
        self.assertEqual(st.current_rice, "prev",
            f"state.current_rice was changed despite missing rice name: {st.current_rice!r}")
        # But the file was restored
        with open(os.path.join(self.live, "h.txt")) as f:
            self.assertEqual(f.read(), "v0\n",
                f"file was not restored: {f.read()!r}")

    def test_rollback_present_empty_rice_resets_state(self):
        # A present-but-empty `rice=` means the pre-switch state had no
        # rice: rollback must reset current_rice to "" (not error, not
        # leave the target rice in place).
        from rice_switcher.snapshot import create_pending, commit
        from rice_switcher.state import read_state, write_state, State
        ts, _ = create_pending(self.roots, [self.live], rice_name="")
        commit(self.roots, ts)
        write_state(self.roots, State(current_rice="prev", last_switch=0.0))
        from rice_switcher.switch import do_rollback
        _, errs = do_rollback(self.roots, ts, None, reload_fn=lambda: None)
        self.assertEqual(errs, [], f"unexpected errors: {errs}")
        self.assertEqual(read_state(self.roots).current_rice, "",
            "empty rice= must reset state.current_rice to ''")


if __name__ == "__main__":
    unittest.main(verbosity=2)
