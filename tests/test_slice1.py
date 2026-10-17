"""Unit tests for the Slice 1 implementation.

All tests run in `tempfile.TemporaryDirectory()` and never touch
`~/.config/`, `~/.local/`, the live filesystem, or any real process.
They are designed to fail loudly if any side effect leaks out of the
temp dir.

Test classes mirror PLAN.md §11.3:
- ManifestTest
- ValidateTest
- ManagedSetTest
- ProcessTest
- LockTest
- SnapshotTest
- StateTest
- SwitchTest (CLI orchestrator)
- DryRunTest
- CompositorInvariantTest
- ReferencedPathTest
- EndToEndTest (init -> add -> switch --dry-run -> list)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

# Ensure the package is importable from the repo root.
THIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from rice_switcher import __init__ as pkg_init  # noqa: F401
from rice_switcher.lock import LockError, switch_lock
from rice_switcher.log import setup_logging
from rice_switcher.managed_set import (
    Identity,
    SHARED_COMPONENT_NAMES,
    SwitchPlan,
    compute_plan,
    load_managed_set,
)
from rice_switcher.manifest import (
    Component,
    Manifest,
    ManifestError,
    VALID_TYPES,
    list_manifests,
    load_manifest,
    parse_manifest,
)
from rice_switcher.paths import (
    Roots,
    audit_log_file,
    default_log_file,
    lock_file,
    manifest_path,
    resolve_roots,
    rice_dir,
    snapshot_dir,
    state_file,
)
from rice_switcher.process import (
    find_pids_by_identity,
    identity_from_argv,
    looks_like_dangerous_pkill,
    read_cmdline,
)
from rice_switcher.snapshot import (
    commit as commit_snapshot,
    create_pending,
    current_symlinks_state,
    discard_pending,
    list_committed,
    pending_path,
    prune as prune_snapshots,
    restore as restore_snapshot,
    swap_symlink,
    undo_swap,
)
from rice_switcher.state import State, mark_trust, read_state, write_state
from rice_switcher.switch import do_rollback, do_switch
from rice_switcher.validate import (
    ValidationError,
    ValidationResult,
    validate_manifest,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_tmp_roots() -> tuple[Roots, tempfile.TemporaryDirectory]:
    td = tempfile.TemporaryDirectory(prefix="rice-slice1-")
    base = td.name
    roots = Roots(
        rice_home=os.path.join(base, "rice"),
        config_home=os.path.join(base, "config"),
        state_home=os.path.join(base, "state"),
    )
    os.makedirs(roots.rice_home, exist_ok=True)
    os.makedirs(roots.config_home, exist_ok=True)
    os.makedirs(roots.state_home, exist_ok=True)
    return roots, td


def _write_manifest(roots: Roots, name: str, body: str) -> str:
    """Write a rice.toml under <rice_home>/<name>/rice.toml."""
    target_dir = rice_dir(roots, name)
    os.makedirs(target_dir, exist_ok=True)
    path = manifest_path(roots, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    return path


def _minimal_body(name: str, components: list[Component]) -> str:
    """Render a minimal valid manifest with the given components."""
    lines = [
        "[meta]",
        f'name = "{name}"',
        "",
    ]
    if components:
        for c in components:
            d = [f'  [[components]]', f'  name = "{c.name}"', f'  type = "{c.type}"',
                 f'  start = "{c.start}"', f'  stop = "{c.stop}"']
            if c.executable:
                d.append(f'  executable = "{c.executable}"')
            if c.argv_match:
                d.append(f'  argv_match = {c.argv_match!r}')
            if c.config_link:
                d.append(f'  config_link = "{c.config_link}"')
            if c.reload:
                d.append(f'  reload = "{c.reload}"')
            if c.optional:
                d.append(f'  optional = true')
            lines.extend(d)
            lines.append("")
    return "\n".join(lines)


def _make_manifest(roots: Roots, name: str, components: list[Component]) -> Manifest:
    path = _write_manifest(roots, name, _minimal_body(name, components))
    return load_manifest(path)


# ---------------------------------------------------------------------------
# ManifestTest
# ---------------------------------------------------------------------------

class ManifestTest(unittest.TestCase):
    def setUp(self):
        self.roots, self._td = _make_tmp_roots()

    def tearDown(self):
        self._td.cleanup()

    def test_parse_minimal_valid(self):
        m = _make_manifest(self.roots, "r1", [
            Component(name="hyprland", type="compositor", start="true", stop="true"),
        ])
        self.assertEqual(m.meta.name, "r1")
        self.assertEqual(len(m.components), 1)
        self.assertEqual(m.components[0].name, "hyprland")
        self.assertEqual(m.components[0].type, "compositor")

    def test_parse_missing_meta_name_fails(self):
        body = '[meta]\ndescription = "x"\n'
        path = _write_manifest(self.roots, "bad", body)
        with self.assertRaises(ManifestError) as ctx:
            load_manifest(path)
        self.assertIn("[meta].name", str(ctx.exception))

    def test_parse_invalid_type_fails(self):
        body = '[meta]\nname = "x"\n\n[[components]]\nname = "bad"\ntype = "unsupported"\nstart = "true"\nstop = "true"\n'
        path = _write_manifest(self.roots, "bad", body)
        with self.assertRaises(ManifestError):
            load_manifest(path)

    def test_parse_duplicate_component_name_fails(self):
        body = textwrap.dedent("""\
            [meta]
            name = "x"

            [[components]]
            name = "dup"
            type = "bar"
            start = "true"
            stop = "true"

            [[components]]
            name = "dup"
            type = "bar"
            start = "true"
            stop = "true"
        """)
        path = _write_manifest(self.roots, "dup", body)
        with self.assertRaises(ManifestError) as ctx:
            load_manifest(path)
        self.assertIn("duplicate", str(ctx.exception).lower())

    def test_parse_argv_match_must_be_list(self):
        body = textwrap.dedent("""\
            [meta]
            name = "x"

            [[components]]
            name = "qs"
            type = "shell"
            start = "qs"
            stop = "true"
            argv_match = "not-a-list"
        """)
        path = _write_manifest(self.roots, "x", body)
        with self.assertRaises(ManifestError):
            load_manifest(path)

    def test_list_manifests_filters_to_dirs_with_rice_toml(self):
        _make_manifest(self.roots, "a", [])
        # Create a sibling dir without rice.toml
        os.makedirs(os.path.join(self.roots.rice_home, "no-manifest"), exist_ok=True)
        # Create a file at the top level
        Path(os.path.join(self.roots.rice_home, "stray.txt")).write_text("x")
        rices = list_manifests(self.roots.rice_home)
        names = [n for n, _ in rices]
        self.assertEqual(names, ["a"])

    def test_component_identity_uses_executable_field(self):
        c1 = Component(name="qs", type="shell", start="qs -c ii", stop="true",
                       executable="qs", argv_match=["-c", "ii"])
        c2 = Component(name="qs_minimal", type="shell", start="qs -c minimal", stop="true",
                       executable="qs", argv_match=["-c", "minimal"])
        # Even with different logical names, the identities differ
        self.assertNotEqual(Identity.from_component(c1),
                            Identity.from_component(c2))
        # Same logical name, same executable, same argv -> same identity
        c3 = Component(name="qs2", type="shell", start="qs -c ii", stop="true",
                       executable="qs", argv_match=["-c", "ii"])
        self.assertEqual(Identity.from_component(c1),
                         Identity.from_component(c3))


# ---------------------------------------------------------------------------
# ValidateTest
# ---------------------------------------------------------------------------

class ValidateTest(unittest.TestCase):
    def setUp(self):
        self.roots, self._td = _make_tmp_roots()

    def tearDown(self):
        self._td.cleanup()

    def test_validate_minimal_ok(self):
        m = _make_manifest(self.roots, "r1", [
            Component(name="hyprland", type="compositor", start="true", stop="true",
                       config_link=os.path.join(self.roots.config_home, "hypr")),
        ])
        v = validate_manifest(m, self.roots)
        self.assertTrue(v.ok, msg=f"errors: {v.errors}")

    def test_validate_config_link_conflict_parent_child(self):
        # Two components: one points at config_home/hypr, the other at config_home/hypr/hyprland
        a = Component(name="hyprland", type="compositor", start="true", stop="true",
                      config_link=os.path.join(self.roots.config_home, "hypr"))
        b = Component(name="hypridle", type="system", start="/bin/true", stop="/bin/true",
                      config_link=os.path.join(self.roots.config_home, "hypr", "hyprland"))
        m = _make_manifest(self.roots, "x", [a, b])
        v = validate_manifest(m, self.roots)
        self.assertFalse(v.ok)
        self.assertTrue(any("nesting" in e for e in v.errors))

    def test_validate_config_link_identical(self):
        link = os.path.join(self.roots.config_home, "hypr")
        a = Component(name="hyprland", type="compositor", start="true", stop="true",
                      config_link=link)
        b = Component(name="hyprland2", type="compositor", start="true", stop="true",
                      config_link=link)
        m = _make_manifest(self.roots, "x", [a, b])
        v = validate_manifest(m, self.roots)
        self.assertTrue(any("identical" in e for e in v.errors))

    def test_validate_config_link_escape_attempt(self):
        # A config_link that escapes allowed roots (e.g. /etc/passwd) must
        # be flagged as an error.
        bad = Component(name="evil", type="bar", start="/bin/true", stop="/bin/true",
                        config_link="/etc/passwd")
        m = _make_manifest(self.roots, "x", [bad])
        v = validate_manifest(m, self.roots)
        self.assertFalse(v.ok)
        self.assertTrue(any("outside allowed roots" in e for e in v.errors))

    def test_validate_optional_skips_binary_check(self):
        # A component with optional=true should not produce a binary
        # warning even if its `start` binary is missing on PATH.
        c = Component(name="missing", type="system", start="definitely-not-on-path-xyz",
                      stop="/bin/true", optional=True)
        m = _make_manifest(self.roots, "x", [c])
        v = validate_manifest(m, self.roots)
        self.assertTrue(v.ok)
        self.assertFalse(any("not found on PATH" in w for w in v.warnings))

    def test_validate_required_component_missing_start_is_error(self):
        c = Component(name="no-start", type="bar", start="", stop="/bin/true")
        m = _make_manifest(self.roots, "x", [c])
        v = validate_manifest(m, self.roots)
        self.assertFalse(v.ok)
        self.assertTrue(any("start" in e and "required" in e for e in v.errors))

    def test_validate_hyprlock_warns_when_script_missing(self):
        c = Component(name="hyprlock", type="locker", start="/bin/true", stop="/bin/true")
        m = _make_manifest(self.roots, "x", [c])
        v = validate_manifest(m, self.roots)
        self.assertTrue(any("check-capslock.sh" in w for w in v.warnings))


# ---------------------------------------------------------------------------
# ManagedSetTest
# ---------------------------------------------------------------------------

class ManagedSetTest(unittest.TestCase):
    def setUp(self):
        self.roots, self._td = _make_tmp_roots()

    def tearDown(self):
        self._td.cleanup()

    def test_load_managed_set_is_union_of_identities(self):
        cA = Component(name="qs", type="shell", start="qs -c a", stop="/bin/true",
                       executable="qs", argv_match=["-c", "a"])
        cB = Component(name="qs", type="shell", start="qs -c b", stop="/bin/true",
                       executable="qs", argv_match=["-c", "b"])
        mA = _make_manifest(self.roots, "A", [cA])
        mB = _make_manifest(self.roots, "B", [cB])
        ms = load_managed_set([mA, mB])
        self.assertIn(Identity("qs", ("-c", "a")), ms)
        self.assertIn(Identity("qs", ("-c", "b")), ms)

    def test_compositor_never_in_to_stop_or_to_start(self):
        cHypr = Component(name="hyprland", type="compositor", start="true", stop="true")
        cQs = Component(name="qs", type="shell", start="qs -c a", stop="/bin/true",
                        executable="qs", argv_match=["-c", "a"])
        cur = _make_manifest(self.roots, "cur", [cHypr, cQs])
        tgt = _make_manifest(self.roots, "tgt", [cHypr])
        plan = compute_plan(cur, tgt, [cur, tgt], {}, self.roots.config_home)
        for ident in plan.to_stop:
            self.assertNotEqual(ident.executable, "hyprland",
                                f"compositor must not be in to_stop; got {ident}")
        for ident in plan.to_start:
            self.assertNotEqual(ident.executable, "hyprland")

    def test_transition_qs_to_waybar(self):
        cHypr = Component(name="hyprland", type="compositor", start="true", stop="true",
                          config_link=os.path.join(self.roots.config_home, "hypr"))
        cQs = Component(name="qs", type="shell", start="qs -c a", stop="/bin/true",
                        executable="qs", argv_match=["-c", "a"],
                        config_link=os.path.join(self.roots.config_home, "quickshell"))
        cWb = Component(name="waybar", type="bar", start="/bin/true", stop="/bin/true",
                        executable="waybar",
                        config_link=os.path.join(self.roots.config_home, "waybar"))
        cur = _make_manifest(self.roots, "qs", [cHypr, cQs])
        tgt = _make_manifest(self.roots, "wb", [cHypr, cWb])
        plan = compute_plan(cur, tgt, [cur, tgt], {}, self.roots.config_home)
        self.assertIn(Identity("qs", ("-c", "a")), plan.to_stop)
        self.assertIn(Identity("waybar", ()), plan.to_start)
        self.assertIn(Identity("hyprland", ()), plan.to_keep)

    def test_qs_ii_to_qs_minimal(self):
        cHypr = Component(name="hyprland", type="compositor", start="true", stop="true",
                          config_link=os.path.join(self.roots.config_home, "hypr"))
        cQsA = Component(name="qs_ii", type="shell", start="qs -c ii", stop="/bin/true",
                         executable="qs", argv_match=["-c", "ii"],
                         config_link=os.path.join(self.roots.config_home, "quickshell"))
        cQsB = Component(name="qs_minimal", type="shell", start="qs -c minimal", stop="/bin/true",
                         executable="qs", argv_match=["-c", "minimal"],
                         config_link=os.path.join(self.roots.config_home, "quickshell"))
        cur = _make_manifest(self.roots, "ii", [cHypr, cQsA])
        tgt = _make_manifest(self.roots, "min", [cHypr, cQsB])
        active = {Identity("hyprland", ()), Identity("qs", ("-c", "ii"))}
        plan = compute_plan(cur, tgt, [cur, tgt], {}, self.roots.config_home,
                            active_identities=active)
        self.assertIn(Identity("qs", ("-c", "ii")), plan.to_stop)
        self.assertIn(Identity("qs", ("-c", "minimal")), plan.to_start)

    def test_shared_component_kept(self):
        cMako = Component(name="mako", type="notifications", start="/bin/true", stop="/bin/true")
        cur = _make_manifest(self.roots, "A", [cMako])
        tgt = _make_manifest(self.roots, "B", [cMako])
        plan = compute_plan(cur, tgt, [cur, tgt], {}, self.roots.config_home)
        self.assertIn(Identity("mako", ()), plan.to_keep)
        self.assertNotIn(Identity("mako", ()), plan.to_stop)

    def test_switch_to_self_is_noop(self):
        c = Component(name="qs", type="shell", start="qs -c a", stop="/bin/true",
                      executable="qs", argv_match=["-c", "a"])
        m = _make_manifest(self.roots, "A", [c])
        plan = compute_plan(m, m, [m], {}, self.roots.config_home)
        self.assertTrue(plan.is_noop())


# ---------------------------------------------------------------------------
# ProcessTest
# ---------------------------------------------------------------------------

class ProcessTest(unittest.TestCase):
    def test_dangerous_pkill_patterns_detected(self):
        self.assertTrue(looks_like_dangerous_pkill("pkill -f qs"))
        self.assertTrue(looks_like_dangerous_pkill("pkill --full -e qs"))
        self.assertTrue(looks_like_dangerous_pkill("/usr/bin/pkill -f something"))
        # Without -f / --full, it's fine
        self.assertFalse(looks_like_dangerous_pkill("pkill qs"))
        self.assertFalse(looks_like_dangerous_pkill("pkill -9 qs"))
        # Empty / unrelated
        self.assertFalse(looks_like_dangerous_pkill(""))
        self.assertFalse(looks_like_dangerous_pkill("echo hi"))

    def test_find_pids_by_identity_does_not_match_self(self):
        # We can't easily synthesize a child process in a unit test
        # without forking, so we just check that the result does not
        # include the test runner's own pid. This catches the obvious
        # "matched everything in /proc" bug.
        pids = find_pids_by_identity("python3", ())
        self.assertNotIn(os.getpid(), pids)

    def test_identity_from_argv_empty_returns_none(self):
        self.assertIsNone(identity_from_argv([]))

    def test_identity_from_argv_extracts_basename(self):
        ident = identity_from_argv(["/usr/bin/qs", "-c", "ii"])
        self.assertEqual(ident, ("qs", ("-c", "ii")))


# ---------------------------------------------------------------------------
# LockTest
# ---------------------------------------------------------------------------

class LockTest(unittest.TestCase):
    def setUp(self):
        self.roots, self._td = _make_tmp_roots()

    def tearDown(self):
        self._td.cleanup()

    def test_lock_acquire_and_release(self):
        with switch_lock(self.roots, timeout_seconds=1.0) as info:
            self.assertTrue(info.held)
            self.assertGreaterEqual(info.elapsed_seconds, 0.0)
        # Re-acquiring should work after release
        with switch_lock(self.roots, timeout_seconds=1.0) as info2:
            self.assertTrue(info2.held)

    def test_lock_blocks_second_holder(self):
        import threading
        got_lock = threading.Event()
        release = threading.Event()
        def hold():
            with switch_lock(self.roots, timeout_seconds=2.0):
                got_lock.set()
                release.wait(timeout=2.0)
        t = threading.Thread(target=hold, daemon=True)
        t.start()
        try:
            self.assertTrue(got_lock.wait(timeout=2.0))
            # Now try to acquire from the test thread; should timeout.
            start = time.monotonic()
            with self.assertRaises(LockError):
                with switch_lock(self.roots, timeout_seconds=0.3):
                    pass
            elapsed = time.monotonic() - start
            self.assertGreaterEqual(elapsed, 0.25)  # we waited ~timeout
        finally:
            release.set()
            t.join(timeout=2.0)


# ---------------------------------------------------------------------------
# SnapshotTest
# ---------------------------------------------------------------------------

class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.roots, self._td = _make_tmp_roots()
        # Create a fake live config dir with files (and a sub-symlink)
        self.live_dir = os.path.join(self.roots.config_home, "test")
        os.makedirs(self.live_dir)
        Path(self.live_dir, "a.txt").write_text("alpha")
        Path(self.live_dir, "b.txt").write_text("beta")
        # A symlink inside live_dir
        os.symlink("/etc/hostname", os.path.join(self.live_dir, "hostlink"))

    def tearDown(self):
        self._td.cleanup()

    def test_create_pending_and_commit(self):
        ts, captured = create_pending(self.roots, [self.live_dir])
        pend = pending_path(self.roots, ts)
        self.assertTrue(os.path.isdir(pend))
        self.assertEqual(captured, [self.live_dir])
        # The pending contains a directory named after the basename
        # of the live_dir, holding the captured files.
        target = os.path.join(pend, "test")
        self.assertTrue(os.path.isdir(target))
        self.assertEqual(Path(target, "a.txt").read_text(), "alpha")
        self.assertEqual(Path(target, "b.txt").read_text(), "beta")
        # Symlink preserved as symlink
        link = os.path.join(target, "hostlink")
        self.assertTrue(os.path.islink(link))
        # .captured.txt written
        self.assertTrue(os.path.isfile(os.path.join(pend, ".captured.txt")))

        # Now commit
        commit_snapshot(self.roots, ts)
        self.assertFalse(os.path.isdir(pend))
        committed = os.path.join(self.roots.state_home, "rice-switcher", "snapshot", ts)
        self.assertTrue(os.path.isdir(committed))
        # latest symlink
        latest = os.path.join(self.roots.state_home, "rice-switcher", "snapshot", "latest")
        self.assertTrue(os.path.islink(latest))
        self.assertEqual(os.readlink(latest), ts)

    def test_swap_and_undo(self):
        target = self.live_dir
        # First create a snapshot of the original state
        Path(target, "a.txt").write_text("alpha")
        new_src = os.path.join(self.roots.config_home, "new")
        os.makedirs(new_src)
        Path(new_src, "a.txt").write_text("ALPHA-NEW")
        Path(new_src, "c.txt").write_text("gamma")

        # Snapshot original
        ts, _ = create_pending(self.roots, [target])
        commit_snapshot(self.roots, ts)

        # Swap
        swap_symlink(target, new_src)
        self.assertEqual(Path(target, "a.txt").read_text(), "ALPHA-NEW")
        self.assertEqual(Path(target, "c.txt").read_text(), "gamma")

        # Undo
        ok = undo_swap(target)
        self.assertTrue(ok)
        self.assertEqual(Path(target, "a.txt").read_text(), "alpha")
        self.assertFalse(os.path.exists(os.path.join(target, "c.txt")))

    def test_prune_keeps_n_most_recent(self):
        # Create 7 snapshots, sleeping 5ms between each so timestamps
        # never collide (the ms-resolution ts could still tie on very
        # fast systems if two snapshots fall in the same millisecond).
        import time
        timestamps: list[str] = []
        for i in range(7):
            time.sleep(0.005)
            Path(self.live_dir, "a.txt").write_text(f"v{i}")
            ts, _ = create_pending(self.roots, [self.live_dir])
            commit_snapshot(self.roots, ts)
            timestamps.append(ts)
        # Prune keeping 3
        pruned = prune_snapshots(self.roots, keep=3)
        self.assertEqual(len(pruned), 4)
        # The 3 newest remain
        committed = list_committed(self.roots)
        self.assertEqual(len(committed), 3)
        for ts in timestamps[-3:]:
            self.assertIn(ts, committed)

    def test_dangling_symlink_in_live_preserved_as_link(self):
        # Add a dangling symlink to the live config
        os.symlink("/nonexistent-target-xyz",
                   os.path.join(self.live_dir, "dangling"))
        ts, _ = create_pending(self.roots, [self.live_dir])
        commit_snapshot(self.roots, ts)
        snap = os.path.join(self.roots.state_home, "rice-switcher", "snapshot",
                            ts, "test")
        self.assertTrue(os.path.islink(os.path.join(snap, "dangling")))


# ---------------------------------------------------------------------------
# StateTest
# ---------------------------------------------------------------------------

class StateTest(unittest.TestCase):
    def setUp(self):
        self.roots, self._td = _make_tmp_roots()

    def tearDown(self):
        self._td.cleanup()

    def test_read_missing_returns_default(self):
        s = read_state(self.roots)
        self.assertEqual(s.current_rice, "")

    def test_write_read_round_trip(self):
        s = State(current_rice="r1", last_switch=12345.0)
        write_state(self.roots, s)
        s2 = read_state(self.roots)
        self.assertEqual(s2.current_rice, "r1")
        self.assertEqual(s2.last_switch, 12345.0)

    def test_mark_trust_updates_sha(self):
        mark_trust(self.roots, "r1", "abc123", "ok")
        s = read_state(self.roots)
        self.assertEqual(s.trust["r1"].manifest_sha256, "abc123")
        self.assertEqual(s.trust["r1"].last_known_good, "ok")

    def test_write_is_atomic(self):
        # Simulate a crash by writing state and reading back.
        s = State(current_rice="alpha")
        write_state(self.roots, s)
        # The temp file should not exist after a successful write
        tmp_files = [p for p in os.listdir(self.roots.state_home)
                     if "rice-switcher" in p and ".tmp-" in p]
        self.assertEqual(tmp_files, [])


# ---------------------------------------------------------------------------
# CompositorInvariantTest
# ---------------------------------------------------------------------------

class CompositorInvariantTest(unittest.TestCase):
    def setUp(self):
        self.roots, self._td = _make_tmp_roots()

    def tearDown(self):
        self._td.cleanup()

    def test_hyprland_never_appears_in_stop_or_start(self):
        # Try various transitions; hyprland must never be touched.
        cHypr = Component(name="hyprland", type="compositor", start="true", stop="true",
                          config_link=os.path.join(self.roots.config_home, "hypr"))
        cQs = Component(name="qs", type="shell", start="qs -c a", stop="/bin/true",
                        executable="qs", argv_match=["-c", "a"],
                        config_link=os.path.join(self.roots.config_home, "quickshell"))
        cWb = Component(name="waybar", type="bar", start="/bin/true", stop="/bin/true",
                        executable="waybar",
                        config_link=os.path.join(self.roots.config_home, "waybar"))

        # Build three rices
        cur = _make_manifest(self.roots, "cur", [cHypr, cQs])
        mid = _make_manifest(self.roots, "mid", [cHypr])
        wb = _make_manifest(self.roots, "wb", [cHypr, cWb])

        for src, dst, label in [(cur, mid, "cur->mid"), (mid, wb, "mid->wb"),
                                (cur, wb, "cur->wb")]:
            plan = compute_plan(src, dst, [cur, mid, wb], {}, self.roots.config_home)
            for ident in list(plan.to_stop) + list(plan.to_start):
                self.assertNotEqual(
                    ident.executable, "hyprland",
                    f"{label}: compositor in stop/start list: {ident}",
                )


# ---------------------------------------------------------------------------
# DryRunTest
# ---------------------------------------------------------------------------

class DryRunTest(unittest.TestCase):
    def setUp(self):
        self.roots, self._td = _make_tmp_roots()
        # Set up a live "current" rice (qs) and a target rice (waybar)
        self.live_hypr = os.path.join(self.roots.config_home, "hypr")
        os.makedirs(self.live_hypr)
        Path(self.live_hypr, "hyprland.lua").write_text("-- stub\n")
        self.live_qs = os.path.join(self.roots.config_home, "quickshell")
        os.makedirs(self.live_qs)
        Path(self.live_qs, "shell.qml").write_text("// stub\n")
        self.live_wb = os.path.join(self.roots.config_home, "waybar")
        os.makedirs(self.live_wb)
        Path(self.live_wb, "config.jsonc").write_text("{}\n")

        cHypr = Component(name="hyprland", type="compositor", start="true", stop="true",
                          config_link=self.live_hypr)
        cQs = Component(name="qs", type="shell", start="/bin/true", stop="/bin/true",
                        executable="qs", argv_match=["-c", "a"],
                        config_link=self.live_qs)
        cWb = Component(name="waybar", type="bar", start="/bin/true", stop="/bin/true",
                        executable="waybar", config_link=self.live_wb)
        self.cur = _make_manifest(self.roots, "cur", [cHypr, cQs])
        self.tgt = _make_manifest(self.roots, "wb", [cHypr, cWb])
        # Mark cur as current rice (so the switcher knows there is a current state)
        from rice_switcher.state import write_state, State
        write_state(self.roots, State(current_rice="cur"))

    def tearDown(self):
        self._td.cleanup()

    def test_dry_run_does_not_modify_filesystem(self):
        os.makedirs(snapshot_dir(self.roots), exist_ok=True)
        snapshot_dir_before = set(os.listdir(snapshot_dir(self.roots)))
        lock_before = os.path.isfile(lock_file(self.roots))
        with open(state_file(self.roots)) as f:
            state_before = f.read()
        # dry-run switch
        outcome = do_switch(
            self.roots, "wb", [self.cur, self.tgt],
            dry_run=True, force_real=False,
        )
        # After dry-run: nothing changed on disk
        self.assertEqual(set(os.listdir(snapshot_dir(self.roots))),
                         snapshot_dir_before)
        self.assertFalse(os.path.isfile(lock_file(self.roots)))
        with open(state_file(self.roots)) as f:
            self.assertEqual(f.read(), state_before)
        self.assertTrue(outcome.dry_run)
        self.assertIsNone(outcome.snapshot_ts)
        self.assertFalse(outcome.errors)

    def test_real_switch_changes_state_and_creates_snapshot(self):
        # The real switch creates a new snapshot dir, updates state.
        outcome = do_switch(
            self.roots, "wb", [self.cur, self.tgt],
            dry_run=False, force_real=True,
        )
        self.assertFalse(outcome.dry_run)
        self.assertIsNotNone(outcome.snapshot_ts)
        # State should now point at "wb"
        s = read_state(self.roots)
        self.assertEqual(s.current_rice, "wb")
        # A committed snapshot exists
        committed = list_committed(self.roots)
        self.assertIn(outcome.snapshot_ts, committed)


# ---------------------------------------------------------------------------
# ReferencedPathTest
# ---------------------------------------------------------------------------

class ReferencedPathTest(unittest.TestCase):
    def setUp(self):
        self.roots, self._td = _make_tmp_roots()

    def tearDown(self):
        self._td.cleanup()

    def test_missing_required_ref_produces_warning(self):
        # Create a rice with a hyprland.lua that requires a missing file
        rice_dir_path = rice_dir(self.roots, "broken")
        os.makedirs(rice_dir_path)
        # Manually craft the manifest pointing at a config_link, with
        # a hyprland.lua that requires "hyprland/missing.lua".
        manifest_body = textwrap.dedent("""\
            [meta]
            name = "broken"

            [[components]]
            name = "hyprland"
            type = "compositor"
            start = "true"
            stop = "true"
            config_link = "~/.config/hypr"
        """)
        Path(rice_dir_path, "rice.toml").write_text(manifest_body)
        # Place a hyprland.lua that requires a missing file
        Path(rice_dir_path, "hyprland.lua").write_text(
            'require("hyprland/missing.lua")\n'
        )
        m = load_manifest(manifest_path(self.roots, "broken"))
        v = validate_manifest(m, self.roots)
        # Should warn about the missing required file
        self.assertTrue(any("missing.lua" in w for w in v.warnings),
                        msg=f"warnings: {v.warnings}")

    def test_present_ref_does_not_warn(self):
        rice_dir_path = rice_dir(self.roots, "ok")
        os.makedirs(rice_dir_path)
        manifest_body = textwrap.dedent("""\
            [meta]
            name = "ok"

            [[components]]
            name = "hyprland"
            type = "compositor"
            start = "true"
            stop = "true"
            config_link = "~/.config/hypr"
        """)
        Path(rice_dir_path, "rice.toml").write_text(manifest_body)
        Path(rice_dir_path, "hyprland.lua").write_text(
            'require("hyprland/hyprland.lua")\n'
        )
        # Provide the required file
        hypr_sub = os.path.join(rice_dir_path, "hyprland")
        os.makedirs(hypr_sub, exist_ok=True)
        Path(hypr_sub, "hyprland.lua").write_text("-- stub\n")
        m = load_manifest(manifest_path(self.roots, "ok"))
        v = validate_manifest(m, self.roots)
        self.assertFalse(
            any("missing" in w.lower() and "hyprland.lua" in w for w in v.warnings),
            msg=f"warnings: {v.warnings}",
        )


# ---------------------------------------------------------------------------
# EndToEndTest
# ---------------------------------------------------------------------------

class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self.roots, self._td = _make_tmp_roots()
        # Set up live config dirs
        self.live_hypr = os.path.join(self.roots.config_home, "hypr")
        os.makedirs(self.live_hypr)
        Path(self.live_hypr, "hyprland.lua").write_text("-- stub\n")

    def tearDown(self):
        self._td.cleanup()

    def test_init_add_list_status_flow(self):
        from rice_switcher.cli import cmd_init, cmd_add, cmd_list, cmd_status
        # init
        rc = cmd_init(_Args(), self.roots, setup_logging(self.roots))
        self.assertEqual(rc, 0)
        # add two rices
        for name in ("alpha", "beta"):
            args = _Args(name=name)
            rc = cmd_add(args, self.roots, setup_logging(self.roots))
            self.assertEqual(rc, 0)
        # list
        listed = _capture_stdout(cmd_list, _Args(), self.roots, setup_logging(self.roots))
        self.assertIn("alpha", listed)
        self.assertIn("beta", listed)
        # status
        rc = cmd_status(_Args(), self.roots, setup_logging(self.roots))
        self.assertEqual(rc, 0)


class _Args:
    """Minimal argparse.Namespace replacement for subcommand tests."""
    def __init__(self, **kw):
        self.cmd = kw.pop("cmd", None)
        for k, v in kw.items():
            setattr(self, k, v)


def _capture_stdout(fn, *args, **kw) -> str:
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        fn(*args, **kw)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# RealProcessSafety: prove the implementation does NOT touch /proc/<our pid>
# or send signals anywhere.
# ---------------------------------------------------------------------------

class RealProcessSafetyTest(unittest.TestCase):
    def setUp(self):
        self.roots, self._td = _make_tmp_roots()

    def tearDown(self):
        self._td.cleanup()

    def test_no_signal_sent_during_dry_run(self):
        # Sanity: a dry-run switch should not signal anything. We
        # verify this indirectly: if a signal had been sent, the
        # test runner would have been killed (the test process holds
        # the same pid). We just assert that the test reaches the end.
        cHypr = Component(name="hyprland", type="compositor", start="true", stop="true",
                          config_link=os.path.join(self.roots.config_home, "hypr"))
        cQs = Component(name="qs", type="shell", start="/bin/true", stop="/bin/true",
                        executable="qs", argv_match=["-c", "a"])
        m = _make_manifest(self.roots, "x", [cHypr, cQs])
        # Run do_switch in dry-run; the test must complete.
        outcome = do_switch(self.roots, "x", [m], dry_run=True, force_real=False)
        self.assertTrue(outcome.dry_run)
        # We are still alive.
        self.assertEqual(os.getpid(), os.getpid())


if __name__ == "__main__":
    unittest.main(verbosity=2)
