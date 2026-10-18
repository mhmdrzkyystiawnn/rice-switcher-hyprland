"""Fase debug Langkah 1: reproduksi hermetis Bug 1-3.

Semua tes di tempdir (RICE_HOME / RICE_CONFIG_HOME / RICE_STATE_HOME
dalam satu TemporaryDirectory). Tidak menyentuh ~/.config, ~/.local,
atau proses nyata.

Hipotesis yang diuji (RED/GREEN):
- 1A. no-op (current=end4, target=end4): filesystem tidak berubah,
      trust tidak berubah, plan to_swap kosong, SwitchOutcome.dry_run
      adalah True (internal semantic: "no filesystem change"). Pesan
      warning "no-op: already on this rice".
- 1B. cross rice (current=elifouts dgn config_link hypr, target=end4
      tanpa config_link): do_switch TIDAK menyentuh config_home/hypr
      walaupun plan valid; to_swap kosong.
- 1C. Capture footer output CLI: ada baris `dry_run=True` di SwitchOutcome
      no-op path; non-no-op path menulis `dry_run=False` setelah swap
      sukses.
- 1D. Swap pertama dengan live = direktori nyata → backup = direktori
      nyata. clean-backups --keep 0 --yes menghapusnya (konfirmasi sudah
      dilewati oleh --yes, tidak ada barrier tambahan).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from rice_switcher.backups import list_backups
from rice_switcher.cli import cmd_clean_backups, cmd_switch, main
from rice_switcher.managed_set import compute_plan
from rice_switcher.manifest import Manifest, load_manifest
from rice_switcher.paths import Roots
from rice_switcher.state import State, read_state, write_state
from rice_switcher.switch import (
    SwitchOutcome,
    _gather_config_links,
    do_switch,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_roots(tmp: str, prefix: str = "rs-6-") -> Roots:
    base = os.path.join(tmp, f"{prefix}root")
    os.makedirs(base, exist_ok=True)
    rh = os.path.join(base, "rice")
    ch = os.path.join(base, "config")
    sh = os.path.join(base, "state")
    for d in (rh, ch, sh):
        os.makedirs(d, exist_ok=True)
    return Roots(rice_home=rh, config_home=ch, state_home=sh)


def _write_manifest(path: str, text: str) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)
    return path


END4_TOML = """[meta]
name = "end4"
description = "Baseline: hand-installed end-4 desktop. No config_link."

[[components]]
name = "quickshell"
type = "shell"
executable = "qs"
argv_match = ["-c", "ii"]
start = "qs -c ii"
stop = "qs -c ii kill"
start_check = "pgrep -x qs"
stop_check = "pgrep -x qs"
"""

ELIFOUTS_TOML_TEMPLATE = """[meta]
name = "{rice_name}"
description = "test rice"

[[components]]
name = "hyprland"
type = "compositor"
start = "true"
stop = "true"
reload = "hyprctl reload"
config_link = "{config_link}"

[[components]]
name = "waybar"
type = "bar"
executable = "waybar"
start = "waybar"
stop = "pkill -x waybar"
start_check = "pgrep -x waybar"
config_link = "{waybar_link}"
"""


# ---------------------------------------------------------------------------
# 1A. no-op
# ---------------------------------------------------------------------------

class NoOpTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="rs-6-noop-")
        self.roots = _make_roots(self._td.name)
        write_manifest_path = os.path.join(self.roots.rice_home, "end4", "rice.toml")
        _write_manifest(write_manifest_path, END4_TOML)
        write_state(self.roots, State(current_rice="end4", last_switch=0.0))

    def tearDown(self):
        self._td.cleanup()

    def test_no_op_outcome_dry_run_is_true(self):
        m = load_manifest(os.path.join(self.roots.rice_home, "end4", "rice.toml"))
        out = do_switch(
            self.roots, "end4", [m],
            dry_run=False, force_real=True, manage_processes=False, logger=None,
        )
        # Internal semantic: "no filesystem change happened"
        self.assertIsInstance(out, SwitchOutcome)
        self.assertTrue(out.dry_run,
                        f"no-op path must report dry_run=True (no swap); got {out.dry_run!r}")
        self.assertIn("no-op: already on this rice", out.warnings,
                      f"missing no-op warning; got warnings={out.warnings!r}")

    def test_no_op_plan_has_no_swap(self):
        m = load_manifest(os.path.join(self.roots.rice_home, "end4", "rice.toml"))
        out = do_switch(
            self.roots, "end4", [m],
            dry_run=False, force_real=True, manage_processes=False, logger=None,
        )
        self.assertEqual(out.plan.to_swap, [],
                         f"end4 has no config_link; to_swap must be empty; got {out.plan.to_swap!r}")
        self.assertEqual(out.plan.target, "end4")
        self.assertEqual(out.plan.current, "end4")

    def test_no_op_filesystem_unchanged(self):
        # config_home starts empty. After no-op, must still be empty.
        before = sorted(os.listdir(self.roots.config_home))
        m = load_manifest(os.path.join(self.roots.rice_home, "end4", "rice.toml"))
        do_switch(
            self.roots, "end4", [m],
            dry_run=False, force_real=True, manage_processes=False, logger=None,
        )
        after = sorted(os.listdir(self.roots.config_home))
        self.assertEqual(before, after,
                         f"config_home changed during no-op: before={before} after={after}")
        # No .bak-* either
        self.assertEqual(list_backups(self.roots.config_home), [])

    def test_no_op_trust_unchanged(self):
        m = load_manifest(os.path.join(self.roots.rice_home, "end4", "rice.toml"))
        before = read_state(self.roots)
        # Trust may or may not exist; record whatever's there.
        trust_before = before.trust.get("end4")
        do_switch(
            self.roots, "end4", [m],
            dry_run=False, force_real=True, manage_processes=False, logger=None,
        )
        after = read_state(self.roots)
        trust_after = after.trust.get("end4")
        # In the no-op path, trust entry should NOT be created/touched.
        if trust_before is None:
            self.assertIsNone(trust_after,
                              f"no-op must not create a trust entry; got {trust_after!r}")
        else:
            self.assertEqual(trust_after.last_switched, trust_before.last_switched,
                             f"no-op must not bump last_switched; "
                             f"before={trust_before.last_switched!r} after={trust_after.last_switched!r}")


# ---------------------------------------------------------------------------
# 1B. cross rice
# ---------------------------------------------------------------------------

class CrossRiceTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="rs-6-cross-")
        self.roots = _make_roots(self._td.name)
        # Build rice end4 (no config_link) and elifouts (with config_link).
        _write_manifest(os.path.join(self.roots.rice_home, "end4", "rice.toml"),
                        END4_TOML)
        self.live_hypr = os.path.join(self.roots.config_home, "hypr")
        self.live_waybar = os.path.join(self.roots.config_home, "waybar")
        _write_manifest(
            os.path.join(self.roots.rice_home, "elifouts", "rice.toml"),
            ELIFOUTS_TOML_TEMPLATE.format(
                rice_name="elifouts",
                config_link=self.live_hypr, waybar_link=self.live_waybar,
            ),
        )
        # Pre-create rice dirs so swap_symlink has a real target.
        elifouts_hypr = os.path.join(self.roots.rice_home, "elifouts", "hypr")
        elifouts_waybar = os.path.join(self.roots.rice_home, "elifouts", "waybar")
        for d in (elifouts_hypr, elifouts_waybar):
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "h.txt"), "w") as f:
                f.write("v_elifouts\n")
        # Initial state: elifouts is current, with live symlinks to elifouts.
        write_state(self.roots, State(current_rice="elifouts", last_switch=0.0))
        for live, target in ((self.live_hypr, elifouts_hypr),
                             (self.live_waybar, elifouts_waybar)):
            if os.path.lexists(live) or os.path.islink(live):
                os.unlink(live)
            os.symlink(target, live)
        self._elifouts_hypr = elifouts_hypr
        self._elifouts_waybar = elifouts_waybar

    def tearDown(self):
        self._td.cleanup()

    def test_switch_to_end4_does_not_touch_config_home_hypr(self):
        m_end4 = load_manifest(os.path.join(self.roots.rice_home, "end4", "rice.toml"))
        m_elif = load_manifest(os.path.join(self.roots.rice_home, "elifouts", "rice.toml"))
        before_target = os.readlink(self.live_hypr)
        before_state = read_state(self.roots)
        out = do_switch(
            self.roots, "end4", [m_end4, m_elif],
            dry_run=False, force_real=True, manage_processes=False, logger=None,
        )
        # plan.to_swap must be empty (end4 has no config_link)
        self.assertEqual(out.plan.to_swap, [],
                         f"to_swap must be empty; got {out.plan.to_swap!r}")
        # The live symlink, if it still exists, must STILL point at elifouts.
        # (switch to end4 doesn't declare hypr/waybar, so it should not swap.)
        if os.path.islink(self.live_hypr):
            after_target = os.readlink(self.live_hypr)
            self.assertEqual(after_target, before_target,
                             f"hypr symlink changed! before={before_target} after={after_target}")
        # State should now be end4.
        after_state = read_state(self.roots)
        self.assertEqual(after_state.current_rice, "end4")
        # No .bak-* for hypr should be created (end4 didn't ask for swap).
        bak = [b for b in list_backups(self.roots.config_home)
               if b.path.startswith(self.roots.config_home) and
               os.path.basename(b.path).startswith("hypr.bak-")]
        self.assertEqual(bak, [],
                         f"no hypr.bak-* should exist after switch end4; got {[b.path for b in bak]}")


# ---------------------------------------------------------------------------
# 1C. SwitchOutcome.dry_run reflects actual execution, NOT CLI mode
# ---------------------------------------------------------------------------

class DryRunFieldTest(unittest.TestCase):
    """The `dry_run` field on SwitchOutcome is the INTERNAL semantic
    ('did a real filesystem change happen?'), not the CLI flag. We
    pin that down here so the CLI renderer doesn't conflate the two.
    """

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="rs-6-dryfield-")
        self.roots = _make_roots(self._td.name)
        # Make a minimal pair of rices.
        _write_manifest(os.path.join(self.roots.rice_home, "end4", "rice.toml"),
                        END4_TOML)
        self.live_hypr = os.path.join(self.roots.config_home, "hypr")
        _write_manifest(
            os.path.join(self.roots.rice_home, "elifouts", "rice.toml"),
            ELIFOUTS_TOML_TEMPLATE.format(
                rice_name="elifouts",
                config_link=self.live_hypr,
                waybar_link=os.path.join(self.roots.config_home, "waybar"),
            ),
        )
        elifouts_hypr = os.path.join(self.roots.rice_home, "elifouts", "hypr")
        elifouts_waybar = os.path.join(self.roots.rice_home, "elifouts", "waybar")
        for d in (elifouts_hypr, elifouts_waybar):
            os.makedirs(d, exist_ok=True)
        write_state(self.roots, State(current_rice="elifouts", last_switch=0.0))
        os.symlink(elifouts_hypr, self.live_hypr)
        os.symlink(elifouts_waybar, os.path.join(self.roots.config_home, "waybar"))
        self._elifouts_hypr = elifouts_hypr
        self._elifouts_waybar = elifouts_waybar

    def tearDown(self):
        self._td.cleanup()

    def test_real_switch_reports_dry_run_false(self):
        # current=elifouts, target=end4 (no config_link) -> real switch,
        # but no swap happens (end4 has no config_link). Either way the
        # `dry_run` field is False because state/current_rice changed.
        m_end4 = load_manifest(os.path.join(self.roots.rice_home, "end4", "rice.toml"))
        m_elif = load_manifest(os.path.join(self.roots.rice_home, "elifouts", "rice.toml"))
        out = do_switch(
            self.roots, "end4", [m_end4, m_elif],
            dry_run=False, force_real=True, manage_processes=False, logger=None,
        )
        # Non-no-op path: state is updated -> real execution path.
        self.assertFalse(out.dry_run,
                         f"real switch must report dry_run=False; got {out.dry_run!r}")

    def test_cli_dry_run_does_not_change_state(self):
        # current=elifouts, target=end4, but dry_run=True (CLI default).
        m_end4 = load_manifest(os.path.join(self.roots.rice_home, "end4", "rice.toml"))
        m_elif = load_manifest(os.path.join(self.roots.rice_home, "elifouts", "rice.toml"))
        before = read_state(self.roots)
        out = do_switch(
            self.roots, "end4", [m_end4, m_elif],
            dry_run=True, force_real=False, manage_processes=False, logger=None,
        )
        after = read_state(self.roots)
        self.assertEqual(after.current_rice, before.current_rice,
                         f"dry-run must NOT update state; before={before.current_rice!r} "
                         f"after={after.current_rice!r}")
        self.assertTrue(out.dry_run, "dry-run path must report dry_run=True")


# ---------------------------------------------------------------------------
# 1D. Real directory live -> backup = real directory
# ---------------------------------------------------------------------------

class RealDirBackupTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="rs-6-realdir-")
        self.roots = _make_roots(self._td.name)
        _write_manifest(os.path.join(self.roots.rice_home, "a", "rice.toml"),
                        ELIFOUTS_TOML_TEMPLATE.format(
                            rice_name="a",
                            config_link=os.path.join(self.roots.config_home, "hypr"),
                            waybar_link=os.path.join(self.roots.config_home, "waybar"),
                        ))
        _write_manifest(os.path.join(self.roots.rice_home, "b", "rice.toml"),
                        ELIFOUTS_TOML_TEMPLATE.format(
                            rice_name="b",
                            config_link=os.path.join(self.roots.config_home, "hypr"),
                            waybar_link=os.path.join(self.roots.config_home, "waybar"),
                        ))
        for r in ("a", "b"):
            for sub in ("hypr", "waybar"):
                d = os.path.join(self.roots.rice_home, r, sub)
                os.makedirs(d, exist_ok=True)
                with open(os.path.join(d, "f"), "w") as f:
                    f.write(f"v_{r}\n")
        # The live config_link is a REAL directory (not a symlink).
        live = os.path.join(self.roots.config_home, "hypr")
        os.makedirs(live, exist_ok=True)
        with open(os.path.join(live, "pre.conf"), "w") as f:
            f.write("original\n")
        write_state(self.roots, State(current_rice="a", last_switch=0.0))

    def tearDown(self):
        self._td.cleanup()

    def test_first_swap_backs_up_real_directory(self):
        m_a = load_manifest(os.path.join(self.roots.rice_home, "a", "rice.toml"))
        m_b = load_manifest(os.path.join(self.roots.rice_home, "b", "rice.toml"))
        out = do_switch(
            self.roots, "b", [m_a, m_b],
            dry_run=False, force_real=True, manage_processes=False, logger=None,
        )
        self.assertEqual(out.errors, [], f"switch failed: {out.errors}")
        backups = list_backups(self.roots.config_home)
        self.assertEqual(len(backups), 1,
                         f"expected 1 backup (real dir), got {[b.path for b in backups]}")
        self.assertEqual(backups[0].kind, "dir",
                         f"first-swap backup must be kind=dir; got {backups[0].kind!r}")
        # The backup preserves the original file.
        self.assertTrue(os.path.isfile(os.path.join(backups[0].path, "pre.conf")))

    def test_clean_backups_keep_zero_yes_removes_real_dir(self):
        m_a = load_manifest(os.path.join(self.roots.rice_home, "a", "rice.toml"))
        m_b = load_manifest(os.path.join(self.roots.rice_home, "b", "rice.toml"))
        do_switch(
            self.roots, "b", [m_a, m_b],
            dry_run=False, force_real=True, manage_processes=False, logger=None,
        )
        backups = list_backups(self.roots.config_home)
        self.assertEqual(len(backups), 1)
        backup_path = backups[0].path
        # Run clean-backups --keep 0 --yes directly (no interactive ask).
        from rice_switcher.cli import build_parser
        args = build_parser().parse_args(["clean-backups", "--keep", "0", "--yes"])
        # Stub ask/out for hermetic CLI.
        with mock.patch("rice_switcher.log.append_audit"):
            so, se = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(so), contextlib.redirect_stderr(se):
                rc = cmd_clean_backups(args, self.roots, None)
        self.assertEqual(rc, 0, f"clean-backups --keep 0 --yes must succeed; got rc={rc}, "
                                f"out={so.getvalue()!r} err={se.getvalue()!r}")
        self.assertFalse(os.path.lexists(backup_path),
                         f"backup must be gone; still exists at {backup_path}")
        self.assertEqual(list_backups(self.roots.config_home), [])


from unittest import mock


# ---------------------------------------------------------------------------
# 1E. Footer rendering (Bug 2 fix regression)
# ---------------------------------------------------------------------------

class FooterRenderingTest(unittest.TestCase):
    """`cmd_switch` no longer renders the misleading `dry_run=` literal.

    The `dry_run` field on `SwitchOutcome` is the INTERNAL semantic
    ("did a filesystem change happen?"). Showing it as `dry_run=True`
    in the footer misled users who passed `--no-dry-run`. The footer
    now uses `no_op=` to make the meaning explicit.
    """

    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="rs-6-footer-")
        self.roots = _make_roots(self._td.name)
        _write_manifest(os.path.join(self.roots.rice_home, "end4", "rice.toml"),
                        END4_TOML)
        write_state(self.roots, State(current_rice="end4", last_switch=0.0))
        # Pre-trust the rice so the confirmation prompt doesn't abort.
        from rice_switcher.state import mark_trust
        m = load_manifest(os.path.join(self.roots.rice_home, "end4", "rice.toml"))
        mark_trust(self.roots, "end4", m.source_sha256, status="ok")

    def tearDown(self):
        self._td.cleanup()

    def _run_cmd(self, no_dry_run: bool) -> str:
        from argparse import Namespace
        args = Namespace(name="end4", no_dry_run=no_dry_run, cmd="switch")
        so, se = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(so), contextlib.redirect_stderr(se):
            rc = cmd_switch(args, self.roots, log=None)
        return so.getvalue() + se.getvalue()

    def test_footer_does_not_show_dry_run_literal(self):
        out = self._run_cmd(no_dry_run=True)
        self.assertNotIn("dry_run=", out,
                         f"footer still renders the misleading `dry_run=` literal:\n{out}")

    def test_footer_shows_no_op_label_for_noop(self):
        out = self._run_cmd(no_dry_run=True)
        # For a no-op (current=end4, target=end4) with --no-dry-run, the
        # internal `dry_run` field is True (no filesystem change). The
        # new label should be `no_op=True`.
        self.assertIn("no_op=True", out,
                      f"footer should show no_op=True for the no-op case:\n{out}")


if __name__ == "__main__":
    unittest.main()
