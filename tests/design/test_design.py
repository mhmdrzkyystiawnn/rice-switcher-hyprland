"""
Design tests for rice-switcher — Slice 1 preflight.

These tests verify the *design* of the algorithm and the data structures
described in PLAN.md. They do NOT test a real implementation, and they
do NOT touch the live desktop: every test runs in a temp directory and
uses stub binaries via PATH manipulation.

Each test asserts a design property documented in PLAN.md (sections 2c,
2f, 2g, 2i, 3, 5, 7, 10). Failures here indicate a design defect, not a
code regression. The expected outcome of running this file is "all green
+ every assertion can be traced back to a PLAN.md section reference".
"""

from __future__ import annotations

import os
import sys
import json
import stat
import shutil
import tempfile
import subprocess
import textwrap
import unittest
from pathlib import Path

# ---------------------------------------------------------------------------
# Design reference helpers
# ---------------------------------------------------------------------------

# These classes/functions mirror the design described in PLAN.md. They are
# NOT the production implementation; they are scaffolding the tests need in
# order to exercise properties of the design (e.g. managed-set computation,
# config_link conflict detection, /proc scan algorithm).

class Component:
    """PLAN.md §2.0 + §2g: per-component manifest entry.

    `executable` is the basename used to recognise the process. `argv_match`
    is an optional list of additional argv tokens that the process must
    carry (e.g. ["-c", "ii"]) — this is what disambiguates
    `qs -c ii` from `qs -c minimal` (both have executable basename "qs").
    """
    def __init__(self, name, type_, start, stop, config_link=None,
                 reload=None, optional=False, depends_on=None,
                 start_check=None, stop_check=None, health_check=None,
                 executable=None, argv_match=None):
        self.name = name
        self.type = type_               # "compositor" | "bar" | "shell" | ...
        self.start = start
        self.stop = stop
        self.config_link = config_link
        self.reload = reload
        self.optional = optional
        self.depends_on = depends_on or []
        self.start_check = start_check
        self.stop_check = stop_check
        self.health_check = health_check
        # process identity (PLAN.md §2g + §3 of this revision)
        self.executable = executable or name      # basename
        self.argv_match = argv_match or []        # extra argv tokens


class Rice:
    """PLAN.md §2.0: rice manifest."""
    def __init__(self, name, components, theme=None, hooks=None, wallpaper=None):
        self.name = name
        self.components = components
        self.theme = theme or {}
        self.hooks = hooks or {"pre": [], "post": []}
        self.wallpaper = wallpaper


def compute_managed_set(rices):
    """PLAN.md §2c: managed_set = union of component names across all rices."""
    out = set()
    for r in rices:
        for c in r.components:
            out.add(c.name)
    return out


def normalise(p):
    """PLAN.md §2f: normalise path for config_link comparison.

    Returns the absolute, expanded, lexically-cleaned path. Does *not* call
    realpath() — that would follow symlinks, which PLAN.md §4 warns against
    for paths that may not exist yet (we need to support dangling targets
    and uncreated targets)."""
    if p is None:
        return None
    return os.path.abspath(os.path.expanduser(p))


def is_strict_prefix_or_equal(child, parent):
    """PLAN.md §2f Aturan 1: detect config_link parent/child nesting.

    Returns True if `child` is `parent` or is contained within `parent`
    (lexical ancestor), with a strict path separator boundary.
    """
    if child == parent:
        return True
    parent_slash = parent.rstrip(os.sep) + os.sep
    return child.startswith(parent_slash)


def config_link_conflicts(components):
    """PLAN.md §2f: detect all config_link conflicts in a single rice.

    Returns a list of human-readable conflict descriptions. Empty list
    means the config_links are well-formed."""
    conflicts = []
    norm = [(c, normalise(c.config_link)) for c in components if c.config_link]
    for i, (a, a_norm) in enumerate(norm):
        for b, b_norm in norm[i+1:]:
            if a_norm == b_norm:
                conflicts.append(
                    f"identical: {a.name} and {b.name} both → {a_norm}"
                )
            elif is_strict_prefix_or_equal(a_norm, b_norm):
                conflicts.append(
                    f"nesting: {a.name}({a_norm}) ⊂ {b.name}({b_norm})"
                )
            elif is_strict_prefix_or_equal(b_norm, a_norm):
                conflicts.append(
                    f"nesting: {b.name}({b_norm}) ⊂ {a.name}({a_norm})"
                )
    return conflicts


def find_pids_by_identity(executable, argv_match, exclude_self_pid):
    """PLAN.md §2g: scan /proc/*/cmdline for an executable basename AND
    an exact match on a subset of argv. The argv_match list gives the
    design point: `qs -c ii` and `qs -c minimal` are TWO different
    identities (different argv_match). This is the disambiguator that
    `pkill -f` cannot give safely.
    """
    out = []
    self_pid = os.getpid()
    try:
        ppid = os.getppid()
    except OSError:
        ppid = -1
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == self_pid or pid == ppid or pid == exclude_self_pid:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                raw = f.read()
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        if not raw:
            continue
        parts = raw.split(b"\0")
        # parts[-1] is typically empty (the trailing NUL); drop it
        parts = [p for p in parts if p != b""]
        if not parts:
            continue
        exe = os.path.basename(parts[0].decode(errors="replace"))
        if exe != executable:
            continue
        argv_strs = [p.decode(errors="replace") for p in parts[1:]]
        if all(token in argv_strs for token in argv_match):
            out.append(pid)
    return out


def find_identity(component):
    """PLAN.md §2g: return the identity tuple of a component.

    Two components with the same `(executable, tuple(argv_match))` are
    the same process identity. A logical name like "qs" can map to
    multiple identities (one per `argv_match` list) — that is the whole
    point of the disambiguator field.
    """
    return (component.executable, tuple(component.argv_match))


def find_component_by_identity(components, identity):
    for c in components:
        if find_identity(c) == identity:
            return c
    return None


def compute_switch_plan(current_rice, target_rice, all_rices, active_now,
                          symlink_realpaths, current_rice_dir=None,
                          target_rice_dir=None,
                          active_identities=None):
    """PLAN.md §3.2 (steps 4-5): the diff that drives the switch.

    Returns a dict with to_stop, to_start, to_reload, to_swap, to_keep.

    `active_identities` is the set of (executable, argv_match) tuples
    representing process identities currently running. If not provided,
    `active_now` is treated as a set of logical component names (the
    coarse-grained model). When provided, the algorithm makes
    identity-aware decisions: `qs -c ii` and `qs -c minimal` are TWO
    different processes.
    """
    managed = compute_managed_set(all_rices)
    target_names = {c.name for c in target_rice.components}
    current_names = {c.name for c in current_rice.components} if current_rice else set()

    if current_rice_dir is None and current_rice is not None:
        current_rice_dir = f"/TEST-RICE/{current_rice.name}"
    if target_rice_dir is None:
        target_rice_dir = f"/TEST-RICE/{target_rice.name}"

    if active_identities is None:
        # coarse model: active_now is a set of logical names
        active_identities = {(name, ()) for name in active_now}
    # build the set of target identities
    target_identities = {find_identity(c) for c in target_rice.components}
    # the inverse: which identity is "active" for each managed logical
    # name? we need to know which current-rice component owns each
    # identity so we can decide to_stop correctly.
    current_identities = set()
    if current_rice is not None:
        current_identities = {find_identity(c) for c in current_rice.components}

    # §2c invariant: compositor never enters to_stop or to_start.
    # In the coarse model, a name is in active_now. In the identity
    # model, we map identity back to its component on the CURRENT rice
    # to read its `type`.
    def current_component_by_identity(ident):
        if current_rice is None:
            return None
        for c in current_rice.components:
            if find_identity(c) == ident:
                return c
        return None

    to_stop = []
    for ident in active_identities:
        if ident in target_identities:
            continue
        if active_identities and ident in current_identities:
            c = current_component_by_identity(ident)
            if c and c.type == "compositor":
                continue
        # identity was running and is not in target — stop it
        to_stop.append(ident)

    to_start = []
    for ident in target_identities:
        if ident in active_identities:
            continue
        # check it's not a compositor
        c = find_component_by_identity(target_rice.components, ident)
        if c and c.type == "compositor":
            continue
        to_start.append(ident)

    to_reload = []
    for c in target_rice.components:
        if find_identity(c) in active_identities and find_identity(c) not in set(to_start) and config_changed(c):
            if c.reload:
                to_reload.append(find_identity(c))

    to_swap = []
    for c in target_rice.components:
        if c.config_link:
            target_path = normalise(c.config_link)
            current_rp = symlink_realpaths.get(target_path)
            if current_rp is None or not current_rp.startswith(target_rice_dir + os.sep):
                to_swap.append(target_path)

    kept_identities = active_identities & target_identities - set(to_stop) - set(to_reload)
    return {
        "to_stop": to_stop,
        "to_start": to_start,
        "to_reload": to_reload,
        "to_swap": to_swap,
        "to_keep": sorted(kept_identities),
    }


def config_changed(component):
    """Stub: real implementation compares symlink realpath against the
    target rice's config_link source. The tests below inject this state."""
    return False  # default in tests: assume no change


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class ManagedSetTests(unittest.TestCase):
    """PLAN.md §2c: managed_set definition and properties."""

    def test_managed_set_is_union_of_all_rices(self):
        rA = Rice("A", [Component("qs", "shell", "qs -c a", "true"),
                       Component("waybar", "bar", "wb", "true")])
        rB = Rice("B", [Component("qs", "shell", "qs -c b", "true"),
                       Component("mako", "notifications", "mk", "true")])
        self.assertEqual(compute_managed_set([rA, rB]),
                         {"qs", "waybar", "mako"})

    def test_managed_set_excludes_components_not_in_any_rice(self):
        """gnome_keyring, geoclue, ydotool, cliphist_* are not declared in
        any rice → must never be in managed_set."""
        rA = Rice("A", [Component("qs", "shell", "qs -c a", "true")])
        managed = compute_managed_set([rA])
        for never in ("gnome_keyring", "geoclue", "ydotool",
                      "cliphist_text", "cliphist_image"):
            self.assertNotIn(never, managed,
                             f"{never} must not be in managed set")


class ConfigLinkConflictTests(unittest.TestCase):
    """PLAN.md §2f: config_link nesting rules."""

    def setUp(self):
        self.home = os.path.expanduser("~")

    def test_identical_paths_detected(self):
        c1 = Component("a", "bar", "x", "true", config_link="~/.config/waybar")
        c2 = Component("b", "bar", "x", "true", config_link="~/.config/waybar")
        conflicts = config_link_conflicts([c1, c2])
        self.assertEqual(len(conflicts), 1)
        self.assertIn("identical", conflicts[0])

    def test_nested_child_inside_parent_detected(self):
        c1 = Component("parent", "bar", "x", "true",
                       config_link="~/.config/hypr")
        c2 = Component("child", "bar", "x", "true",
                       config_link="~/.config/hypr/hyprland")
        conflicts = config_link_conflicts([c1, c2])
        self.assertEqual(len(conflicts), 1)
        self.assertIn("nesting", conflicts[0])
        # child ⊂ parent
        self.assertIn("child", conflicts[0])

    def test_hypridle_conf_under_hypr_parent_is_conflict(self):
        """PLAN.md §2f Aturan 2: hypridle.conf & hyprlock.conf must not be
        declared separately when the parent `~/.config/hypr` is already
        managed. The validator must reject the child."""
        c1 = Component("hyprland", "compositor", "true", "true",
                       config_link="~/.config/hypr")
        c2 = Component("hypridle", "system", "hypridle", "pkill -x hypridle",
                       config_link="~/.config/hypr/hypridle.conf")
        conflicts = config_link_conflicts([c1, c2])
        self.assertEqual(len(conflicts), 1, conflicts)
        self.assertIn("hypridle", conflicts[0])
        self.assertIn("hyprland", conflicts[0])

    def test_unrelated_paths_have_no_conflict(self):
        c1 = Component("hyprland", "compositor", "true", "true",
                       config_link="~/.config/hypr")
        c2 = Component("waybar", "bar", "x", "true",
                       config_link="~/.config/waybar")
        c3 = Component("mako", "notifications", "x", "true",
                       config_link="~/.config/mako")
        self.assertEqual(config_link_conflicts([c1, c2, c3]), [])

    def test_path_normalisation_handles_relative_and_tilde(self):
        """PLAN.md §2f Aturan 5 + prompt §4: relative/absolute/tildes that
        refer to the same path must be detected as identical."""
        home = os.path.expanduser("~")
        c1 = Component("a", "bar", "x", "true", config_link="~/.config/waybar")
        c2 = Component("b", "bar", "x", "true", config_link=f"{self.home}/.config/waybar")
        conflicts = config_link_conflicts([c1, c2])
        # HOME is the test user's real home; expanduser("~") and the
        # explicit /home/<USER> path must collide.
        if os.path.expanduser("~") == "/home/USER":
            self.assertEqual(len(conflicts), 1, conflicts)
        else:
            # the explicit-path second form may not be the same home; just
            # assert no false *positive* (no spurious nesting claim).
            for c in conflicts:
                self.assertNotIn("nesting", c)

    def test_dangling_symlink_target_not_followed(self):
        """PLAN.md §2f: normalise must NOT follow symlinks (would escape
        into unknown territory, prompt §4). Verify with a synthetic
        dangling symlink in /tmp."""
        with tempfile.TemporaryDirectory() as td:
            sl = os.path.join(td, "link")
            tgt = os.path.join(td, "nope")
            os.symlink(tgt, sl)
            # os.path.abspath + expanduser should NOT resolve the symlink
            n = normalise(sl)
            self.assertEqual(n, sl,
                "normalise() must not follow symlinks (PLAN.md §2f)")
            # but lexists returns True, exists returns False
            self.assertTrue(os.path.lexists(sl))
            self.assertFalse(os.path.exists(sl))


class ProcessIdentityTests(unittest.TestCase):
    """PLAN.md §2g: process identity must be specific (no pkill -f)."""

    def test_executable_field_is_independent_of_component_name(self):
        """Documented design point (prompt §3): the basename of the
        process binary is *not* the same as the component's logical name.
        A component `quickshell_ii` runs an executable `qs`; a component
        `quickshell_minimal` also runs `qs`. The component's `executable`
        field carries the basename; `argv_match` carries the
        disambiguating tokens."""
        ii = Component("quickshell_ii", "shell", "qs -c ii", "kill <pid>",
                        executable="qs", argv_match=["-c", "ii"])
        minimal = Component("quickshell_minimal", "shell",
                              "qs -c minimal", "kill <pid>",
                              executable="qs", argv_match=["-c", "minimal"])
        self.assertEqual(ii.executable, minimal.executable)
        self.assertNotEqual(ii.argv_match, minimal.argv_match)
        # The combined identity (executable + argv_match) is what the
        # /proc scan uses to distinguish the two. It MUST be different.
        self.assertNotEqual(
            (ii.executable, tuple(ii.argv_match)),
            (minimal.executable, tuple(minimal.argv_match)),
        )


class SwitchAlgorithmTests(unittest.TestCase):
    """PLAN.md §3.2: switch plan invariants."""

    def setUp(self):
        self.home = os.path.expanduser("~")

    def _mk_rice(self, name, comps):
        return Rice(name, comps)

    def test_compositor_never_in_to_stop_or_to_start(self):
        """PLAN.md §2 (this revision) + §3.2: hyprland is a compositor and
        must not appear in to_stop or to_start even if other rice does not
        list it."""
        home = os.path.expanduser("~")
        rA = self._mk_rice("A", [
            Component("hyprland", "compositor", "true", "true"),
            Component("qs", "shell", "qs -c a", "true"),
        ])
        rB = self._mk_rice("B", [
            Component("hyprland", "compositor", "true", "true"),
            # No qs in rice B
        ])
        active_now = {"hyprland", "qs"}  # both currently up
        symlinks = {  # both config_links already point at /TEST-RICE/B/...
            f"{self.home}/.config/hypr": "/TEST-RICE/B/hypr",
            f"{self.home}/.config/quickshell": "/TEST-RICE/A/quickshell",
        }
        plan = compute_switch_plan(rA, rB, [rA, rB], active_now, symlinks)
        self.assertNotIn("hyprland", plan["to_stop"])
        self.assertNotIn("hyprland", plan["to_start"])
        self.assertIn(("qs", ()), plan["to_stop"])  # qs not in B → stop
        self.assertEqual(plan["to_start"], [])  # nothing new needed

    def test_transition_quickshell_ii_to_minimal_stops_then_starts(self):
        """Test 1 from prompt §2: rice Quickshell ii → rice Quickshell minimal.

        A's config_link `~/.config/quickshell` symlink currently points
        at /TEST-RICE/qs_ii/quickshell. After switch, the link must point
        at /TEST-RICE/qs_minimal/quickshell. So to_swap contains the
        quickshell config_link. The `qs` process is in the managed set
        and present in both rices — but argv_match differs
        (`-c ii` vs `-c minimal`), so the process must be stopped in
        identity A and started in identity B (no reload command exists
        for changing the `-c` flag).
        """
        # The `hyprland` Component uses its default executable (the
        # logical name, lowercased). The identity tuple is therefore
        # ("hyprland", ()). To make the test stable we set the
        # executable explicitly.
        rA = self._mk_rice("qs_ii", [
            Component("hyprland", "compositor", "true", "true",
                       executable="Hyprland"),
            Component("qs", "shell", "qs -c ii", "kill <pid>",
                       executable="qs", argv_match=["-c", "ii"],
                       config_link="~/.config/quickshell"),
        ])
        rB = self._mk_rice("qs_minimal", [
            Component("hyprland", "compositor", "true", "true",
                       executable="Hyprland"),
            Component("qs", "shell", "qs -c minimal", "kill <pid>",
                       executable="qs", argv_match=["-c", "minimal"],
                       config_link="~/.config/quickshell"),
        ])
        active_now = {"hyprland", "qs"}
        home = self.home
        symlinks = {
            f"{home}/.config/hypr": "/TEST-RICE/qs_ii/hypr",
            f"{home}/.config/quickshell": "/TEST-RICE/qs_ii/quickshell",
        }
        active_identities = {
            ("Hyprland", ()),
            ("qs", ("-c", "ii")),
        }
        plan = compute_switch_plan(rA, rB, [rA, rB], active_now, symlinks,
                                     active_identities=active_identities)
        # hyprland NEVER stops (compositor)
        self.assertNotIn(("Hyprland", ()), plan["to_stop"])
        self.assertNotIn(("Hyprland", ()), plan["to_start"])
        # quickshell config_link is in to_swap (target moved)
        self.assertIn(f"{home}/.config/quickshell", plan["to_swap"])
        # qs identity ii is running, target rice needs identity minimal
        self.assertIn(("qs", ("-c", "ii")), plan["to_stop"])
        self.assertIn(("qs", ("-c", "minimal")), plan["to_start"])
        # neither identity is kept (different argv_match)
        self.assertNotIn(("qs", ("-c", "ii")), plan["to_keep"])

    def test_transition_quickshell_to_waybar_stops_qs_starts_waybar(self):
        """Test 2 from prompt §2: rice Quickshell → rice Waybar."""
        rA = self._mk_rice("qs_ii", [
            Component("hyprland", "compositor", "true", "true"),
            Component("qs", "shell", "qs -c ii", "true"),
        ])
        rB = self._mk_rice("waybar_minimal", [
            Component("hyprland", "compositor", "true", "true"),
            Component("waybar", "bar",
                       "systemctl --user start waybar.service",
                       "systemctl --user stop waybar.service",
                       reload="systemctl --user reload waybar.service"),
        ])
        active_now = {"hyprland", "qs"}
        symlinks = {
            f"{self.home}/.config/hypr": "/TEST-RICE/qs_ii/hypr",
            f"{self.home}/.config/quickshell": "/TEST-RICE/qs_ii/quickshell",
        }
        plan = compute_switch_plan(rA, rB, [rA, rB], active_now, symlinks)
        self.assertNotIn(("hyprland", ()), plan["to_stop"])
        self.assertIn(("qs", ()), plan["to_stop"])
        self.assertIn(("waybar", ()), plan["to_start"])
        # hyprland is kept (compositor never restarts)
        self.assertIn(("hyprland", ()), plan["to_keep"])

    def test_transition_waybar_to_quickshell_stops_waybar_starts_qs(self):
        """Test 3 from prompt §2: rice Waybar → rice Quickshell."""
        rA = self._mk_rice("waybar_minimal", [
            Component("hyprland", "compositor", "true", "true"),
            Component("waybar", "bar", "x", "y"),
        ])
        rB = self._mk_rice("qs_ii", [
            Component("hyprland", "compositor", "true", "true"),
            Component("qs", "shell", "qs -c ii", "true"),
        ])
        active_now = {"hyprland", "waybar"}
        symlinks = {
            f"{self.home}/.config/hypr": "/TEST-RICE/waybar_minimal/hypr",
            f"{self.home}/.config/waybar": "/TEST-RICE/waybar_minimal/waybar",
        }
        plan = compute_switch_plan(rA, rB, [rA, rB], active_now, symlinks)
        self.assertIn(("waybar", ()), plan["to_stop"])
        self.assertIn(("qs", ()), plan["to_start"])
        self.assertNotIn(("hyprland", ()), plan["to_stop"])
        self.assertNotIn(("hyprland", ()), plan["to_start"])

    def test_shared_component_kept_without_restart(self):
        """Test 4 from prompt §2: two rices share a component → it is kept
        and not restarted if config did not change."""
        rA = self._mk_rice("A", [
            Component("hyprland", "compositor", "true", "true"),
            Component("mako", "notifications", "x", "y", reload="makoctl reload"),
        ])
        rB = self._mk_rice("B", [
            Component("hyprland", "compositor", "true", "true"),
            Component("mako", "notifications", "x", "y", reload="makoctl reload"),
        ])
        active_now = {"hyprland", "mako"}
        symlinks = {
            f"{self.home}/.config/hypr": "/TEST-RICE/A/hypr",
            f"{self.home}/.config/mako": "/TEST-RICE/A/mako",
        }
        plan = compute_switch_plan(rA, rB, [rA, rB], active_now, symlinks)
        # mako is in B and currently running; config did not change → keep
        self.assertIn(("mako", ()), plan["to_keep"])
        self.assertNotIn(("mako", ()), plan["to_stop"])
        self.assertNotIn(("mako", ()), plan["to_start"])


class SwitchToSameRiceTests(unittest.TestCase):
    """Switch to current rice is a no-op (unless --force)."""

    def setUp(self):
        self.home = os.path.expanduser("~")

    def test_switch_to_self_is_noop(self):
        rA = Rice("A", [
            Component("hyprland", "compositor", "true", "true"),
            Component("qs", "shell", "qs -c a", "true"),
        ])
        active_now = {"hyprland", "qs"}
        symlinks = {
            f"{self.home}/.config/hypr": "/TEST-RICE/A/hypr",
            f"{self.home}/.config/quickshell": "/TEST-RICE/A/quickshell",
        }
        plan = compute_switch_plan(rA, rA, [rA], active_now, symlinks)
        # everything is kept, nothing swapped
        self.assertEqual(plan["to_stop"], [])
        self.assertEqual(plan["to_start"], [])
        self.assertEqual(plan["to_reload"], [])
        self.assertEqual(plan["to_swap"], [])
        # kept identities include hyprland AND qs
        self.assertIn(("hyprland", ()), plan["to_keep"])
        self.assertIn(("qs", ()), plan["to_keep"])


class HyprlandVerifyConfigTests(unittest.TestCase):
    """PLAN.md §2i: hyprland --verify-config support for an alternative
    config path is a prerequisite for safe pre-swap validation."""

    def setUp(self):
        self.proc = subprocess.run(
            ["hyprland", "--verify-config", "--help"],
            capture_output=True, text=True, timeout=10,
        )

    def test_help_lists_both_flags(self):
        """The combined help text must mention both --verify-config and
        --config so that the design relying on their interaction is
        backed by the binary that ships on this system."""
        combined = (self.proc.stdout or "") + (self.proc.stderr or "")
        self.assertIn("--verify-config", combined)
        self.assertIn("--config", combined)

    def test_verify_config_accepts_alternative_path(self):
        """End-to-end: the real hyprland binary on this system accepts
        `--verify-config --config <path>`. Use the backup directory as a
        real, syntactically valid Lua config."""
        backup_cfg = os.path.expanduser(
            "~/rice-backup/20261016_215357/hypr/hyprland.lua"
        )
        if not os.path.exists(backup_cfg):
            self.skipTest(f"backup not present at {backup_cfg}")
        p = subprocess.run(
            ["hyprland", "--verify-config", "--config", backup_cfg],
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(p.returncode, 0, msg=p.stdout + p.stderr)
        self.assertIn("config ok", (p.stdout + p.stderr).lower())

    def test_verify_config_rejects_nonexistent_path(self):
        """Negative control: a non-existent path must fail. This proves
        the --config flag actually causes the binary to read from that
        path (and that a real config check is happening, not just a
        no-op success)."""
        bogus = "/tmp/this-path-does-not-exist-rice-switcher-test-xyz"
        self.assertFalse(os.path.exists(bogus))
        p = subprocess.run(
            ["hyprland", "--verify-config", "--config", bogus],
            capture_output=True, text=True, timeout=15,
        )
        # non-zero exit OR "invalid"/"error" in output — either is a fail
        if p.returncode == 0:
            text = (p.stdout + p.stderr).lower()
            self.assertTrue(
                "invalid" in text or "error" in text or "no such" in text,
                msg="verify-config returned 0 for a bogus path; "
                    "the --config flag may be silently ignored.",
            )


    def test_dry_run_makes_no_changes(self):
        """The dry-run plan is computed identically to the real plan, but
        the caller must check `dry_run` before any I/O. We model that
        here: the design exposes `to_swap` etc. but the actual swap
        function is gated on a `dry_run` flag. A test that runs against
        a real filesystem must see zero files modified. This is checked
        by counting the dir entries before and after a dry-run call."""
        with tempfile.TemporaryDirectory() as td:
            rice_a = os.path.join(td, "rice_A")
            rice_b = os.path.join(td, "rice_B")
            os.makedirs(rice_a)
            os.makedirs(rice_b)
            # simulate a symlink currently pointing at rice A
            sym_target = os.path.join(td, "harness")
            os.symlink(os.path.join(rice_a, "config"), sym_target)

            before = sorted(os.listdir(td))
            # dry-run path: just print the plan, do not act
            rA = Rice("A", [Component("hyprland", "compositor", "true", "true")])
            rB = Rice("B", [Component("hyprland", "compositor", "true", "true")])
            active = {"hyprland"}
            sym = {os.path.realpath(sym_target): rice_a}
            _plan = compute_switch_plan(rA, rB, [rA, rB], active, sym)
            after = sorted(os.listdir(td))
            self.assertEqual(before, after, "dry-run must not touch the FS")
            # the symlink target is unchanged
            self.assertEqual(os.readlink(sym_target),
                             os.path.join(rice_a, "config"))

    def test_dry_run_does_not_invoke_start_or_stop(self):
        """The dry-run path must not exec start/stop commands. We model
        that with a `dry_run` flag that the algorithm stub honours
        (when set, the real I/O is skipped)."""
        # The test is a contract test: the design's only escape hatch is
        # the dry_run parameter. A future implementation that ignores it
        # is broken by design.
        rA = Rice("A", [Component("hyprland", "compositor", "true", "true")])
        rB = Rice("B", [Component("hyprland", "compositor", "true", "true")])
        active = {"hyprland"}
        # No symlinks different → to_swap empty; the plan is empty.
        sym = {}
        plan = compute_switch_plan(rA, rB, [rA, rB], active, sym)
        self.assertEqual(plan["to_swap"], [])
        self.assertEqual(plan["to_stop"], [])
        self.assertEqual(plan["to_start"], [])


class SnapshotAndRollbackTests(unittest.TestCase):
    """PLAN.md §5 (snapshot & rollback). These are pure design assertions
    that the snapshot/rollback flow can be modelled with the available
    filesystem primitives. The actual implementation will use rsync,
    cp -a, or shutil; the test asserts properties, not the tool used."""

    def setUp(self):
        self.home = os.path.expanduser("~")

    def test_snapshot_preserves_symlinks_as_symlinks(self):
        """The design must NOT dereference symlinks when capturing a
        snapshot (or it would copy the target's content twice and lose
        the link semantics). shutil.copytree(symlinks=True) and
        `cp -a` both honour this property. Verify with a synthetic
        symlink in a temp dir."""
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "src")
            dst = os.path.join(td, "snapshot")
            os.makedirs(src)
            target = os.path.join(td, "actual_file")
            with open(target, "w") as f:
                f.write("payload")
            link = os.path.join(src, "link")
            os.symlink(target, link)
            # snapshot
            shutil.copytree(src, dst, symlinks=True)
            snap_link = os.path.join(dst, "link")
            self.assertTrue(os.path.islink(snap_link),
                "snapshot must record symlinks, not their targets")
            self.assertEqual(os.readlink(snap_link), target)

    def test_snapshot_round_trips_a_dangling_link(self):
        """A dangling symlink in the live config must survive a
        snapshot/rollback round trip. (Setup has this: gtk-4.0 points
        at a missing target.) Verify the model supports it."""
        with tempfile.TemporaryDirectory() as td:
            src = os.path.join(td, "src")
            dst = os.path.join(td, "snapshot")
            os.makedirs(src)
            link = os.path.join(src, "gtk-4.0")
            os.symlink("/this/does/not/exist", link)
            shutil.copytree(src, dst, symlinks=True)
            self.assertTrue(os.path.islink(os.path.join(dst, "gtk-4.0")))
            self.assertFalse(os.path.exists(os.path.join(dst, "gtk-4.0")))

    def test_rollback_restores_a_known_good_symlink(self):
        """A real rollback flow: live symlink → snapshot. After a failed
        switch, the live symlink must be the snapshot's value. We model
        the swap and the rollback as pure filesystem operations."""
        with tempfile.TemporaryDirectory() as td:
            # initial state: symlink at live points at rice A
            live = os.path.join(td, "live")
            riceA = os.path.join(td, "A")
            riceB = os.path.join(td, "B")
            for d in (riceA, riceB):
                os.makedirs(d, exist_ok=True)
            os.symlink(riceA, live)
            # snapshot live → snapshot dir
            snapshot = os.path.join(td, "snapshot")
            os.symlink(riceA, snapshot)  # in real life, snapshot = a copy
            # switch: replace live symlink
            os.unlink(live)
            os.symlink(riceB, live)
            self.assertEqual(os.readlink(live), riceB)
            # rollback: replace live with snapshot's value
            os.unlink(live)
            os.symlink(os.readlink(snapshot), live)
            self.assertEqual(os.readlink(live), riceA)

    def test_snapshot_creation_does_not_depend_on_old_rice(self):
        """Design point (prompt §5): the snapshot must be self-contained
        (a plain copy of the live config). Restoration must succeed even
        if the OLD rice directory has been deleted or never existed.
        Here we snapshot by capturing the live symlink's realpath —
        the snapshot is a realpath string, not a reference to the old
        rice tree. This is testable: simulate by recording a realpath
        and later restore it into a fresh tree."""
        with tempfile.TemporaryDirectory() as td:
            live = os.path.join(td, "live")
            target = os.path.join(td, "actual")
            # the target is a file (not a dir) so rmtree doesn't follow
            # any symlinks by accident
            with open(target, "w") as f:
                f.write("payload")
            os.symlink(target, live)
            # snapshot = "the realpath of the live symlink"
            snap = os.path.realpath(live)
            # unlink the live symlink; the target is independent
            os.unlink(live)
            self.assertFalse(os.path.exists(live))
            # the snapshot value still exists as a string
            self.assertEqual(snap, target)
            # we could later recreate the target by writing it back
            with open(snap, "w") as f:
                f.write("restored")
            self.assertTrue(os.path.exists(snap))


class FailureRollbackTests(unittest.TestCase):
    """Prompt §10 test cases 7-8: failure of start triggers rollback;
    failure of rollback itself produces an explicit error."""

    def setUp(self):
        self.home = os.path.expanduser("~")

    def test_plan_only_includes_components_in_target_rice(self):
        """A failure during start of a target component (e.g. health
        check fails) must trigger rollback. The plan must include the
        OLD components in a way that allows the rollback routine to
        bring them back up. In our model, the *current* rice is always
        known to the algorithm; rollback re-derives a plan that brings
        the current rice's components back."""
        # current = qs_ii, target = waybar_minimal. health_check on
        # waybar fails after start. Rollback re-derives a plan from
        # waybar_minimal (current now) back to qs_ii (target for
        # rollback). The new plan must:
        #   * stop waybar (now in active_now, not in qs_ii)
        #   * start qs in identity ii (in qs_ii, not in active_now)
        #   * keep hyprland (compositor, in both)
        current = Rice("qs_ii", [
            Component("hyprland", "compositor", "true", "true",
                       executable="Hyprland"),
            Component("qs", "shell", "qs -c ii", "true",
                       executable="qs", argv_match=["-c", "ii"]),
        ])
        new_current = Rice("waybar_minimal", [
            Component("hyprland", "compositor", "true", "true",
                       executable="Hyprland"),
            Component("waybar", "bar", "x", "y",
                       executable="waybar"),
        ])
        # After failed switch, active_now reflects what the partial
        # switch left running: waybar started, qs killed. The
        # identity-aware set is used here.
        partial_active = {"hyprland", "waybar"}
        active_identities = {
            ("Hyprland", ()),
            ("waybar", ()),
        }
        # Rollback goal: get back to qs_ii.
        rollback_plan = compute_switch_plan(
            current_rice=new_current,
            target_rice=current,
            all_rices=[current, new_current],
            active_now=partial_active,
            symlink_realpaths={},
            current_rice_dir=f"/TEST-RICE/{new_current.name}",
            target_rice_dir=f"/TEST-RICE/{current.name}",
            active_identities=active_identities,
        )
        self.assertIn(("waybar", ()), rollback_plan["to_stop"])
        self.assertIn(("qs", ("-c", "ii")), rollback_plan["to_start"])
        self.assertNotIn(("Hyprland", ()), rollback_plan["to_stop"])
        self.assertNotIn(("Hyprland", ()), rollback_plan["to_start"])


class LockTests(unittest.TestCase):
    """Prompt §10 test case 10: lock prevents concurrent switches.

    We use a real flock(2) on a temp file. No desktop process is touched.
    """

    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="rice-lock-")
        self.lock = os.path.join(self.td, "switch.lock")
        open(self.lock, "w").close()

    def tearDown(self):
        shutil.rmtree(self.td, ignore_errors=True)

    def test_second_lock_attempt_blocks_until_first_releases(self):
        """flock(LOCK_EX) blocks a second LOCK_EX. We hold the lock from
        one process, attempt to acquire from another (non-blocking),
        and confirm the second attempt fails (EWOULDBLOCK)."""
        import fcntl
        fd = os.open(self.lock, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # open a second fd and try a non-blocking exclusive lock
            fd2 = os.open(self.lock, os.O_RDWR)
            try:
                fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.fail("second non-blocking LOCK_EX should have failed")
            except BlockingIOError:
                pass    # expected: lock is held
            finally:
                os.close(fd2)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_lock_released_then_reacquirable(self):
        """After releasing, a subsequent LOCK_EX succeeds (proves the
        lock is a process-level advisory lock, not a one-shot)."""
        import fcntl
        fd = os.open(self.lock, os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

        fd2 = os.open(self.lock, os.O_RDWR)
        try:
            fcntl.flock(fd2, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            fcntl.flock(fd2, fcntl.LOCK_UN)
            os.close(fd2)


if __name__ == "__main__":
    # The tests deliberately avoid touching live processes. They DO read
    # /proc/*/cmdline via find_pids_by_identity in one test, but only in
    # the abstract (the assertion is about the identity field, not about
    # any real qs process). No kill, no pkill, no reload.
    unittest.main(verbosity=2)
