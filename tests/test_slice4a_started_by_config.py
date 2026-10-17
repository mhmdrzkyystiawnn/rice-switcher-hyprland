"""Fase 4 Langkah 1a: `started_by_config` (Opsi B) core support.

The rice's own config launches some components (`exec`, executed on every
`hyprctl reload`). Marking such a component `started_by_config = true`
means: **the switcher only stops it, never starts it** — the config brings
it back. This module pins that contract:

  * the field is optional, defaults to `False`, and old manifests parse
    identically (positional/`Identity` behaviour unchanged);
  * `compute_plan` drops `started_by_config` identities from `to_start`
    (so a switch never double-starts them) but still allows `to_stop`;
  * the dry-run plan shows them as "dijalankan oleh config";
  * `validate` rejects the flag on a `compositor`.

Pure, hermetic: builds manifests in memory; no real filesystem.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from rice_switcher.managed_set import Identity, compute_plan
from rice_switcher.manifest import (
    Component,
    Manifest,
    ManifestError,
    Meta,
    parse_manifest,
)
from rice_switcher.paths import Roots
from rice_switcher.switch import _print_plan
from rice_switcher.validate import validate_manifest


def _comp_dict(name, ctype="bar", **extra):
    d = {"name": name, "type": ctype, "start": name}
    d.update(extra)
    return d


def _manifest(name, comps, source_path=None):
    data = {"meta": {"name": name}, "components": comps}
    sp = source_path if source_path is not None else f"/rice/{name}/rice.toml"
    return parse_manifest(data, source_path=sp)


def _roots(base="/tmp/rs-4a"):
    return Roots(rice_home=f"{base}/rice", config_home=f"{base}/config",
                 state_home=f"{base}/state")


class ParseTest(unittest.TestCase):
    def test_default_false_when_absent(self):
        m = _manifest("a", [_comp_dict("waybar")])
        self.assertFalse(m.components[0].started_by_config)

    def test_explicit_true(self):
        m = _manifest("a", [_comp_dict("waybar", started_by_config=True)])
        self.assertTrue(m.components[0].started_by_config)

    def test_explicit_false(self):
        m = _manifest("a", [_comp_dict("waybar", started_by_config=False)])
        self.assertFalse(m.components[0].started_by_config)

    def test_old_manifest_parses_identically(self):
        # A manifest dict with no `started_by_config` key must produce the
        # exact same Component as one that spells out the default.
        legacy = _manifest("a", [_comp_dict("waybar", executable="waybar",
                                            exact_argv=True, stop="")])
        explicit = _manifest("a", [_comp_dict("waybar", executable="waybar",
                                              exact_argv=True, stop="",
                                              started_by_config=False)])
        self.assertEqual(legacy.components[0], explicit.components[0])

    def test_non_bool_rejected(self):
        with self.assertRaises(ManifestError):
            _manifest("a", [_comp_dict("waybar", started_by_config="yes")])

    def test_positional_signature_unchanged(self):
        # Existing positional construction (name, type, executable,
        # argv_match, exact_argv, start, stop, ...) must still work.
        c = Component("qs", "shell", "qs", ["-c", "ii"], True, "qs -c ii",
                      "", "", "pgrep -x qs", "", "~/.config/quickshell")
        self.assertEqual(c.name, "qs")
        self.assertFalse(c.started_by_config)

    def test_identity_unaffected_by_flag(self):
        a = Component("waybar", "bar", "waybar", [], True,
                      started_by_config=False)
        b = Component("waybar", "bar", "waybar", [], True,
                      started_by_config=True)
        self.assertEqual(Identity.from_component(a), Identity.from_component(b))


class ComputePlanTest(unittest.TestCase):
    def _plan(self, current, target, all_manifests):
        return compute_plan(current, target, all_manifests, {}, "/cfg",
                            active_identities=set())

    def test_excluded_from_to_start_and_listed_as_config(self):
        target = _manifest("t", [
            _comp_dict("hyprland", "compositor", stop="true",
                       config_link="~/.config/hypr"),
            _comp_dict("waybar", started_by_config=True, exact_argv=True,
                       stop=""),
        ])
        plan = self._plan(None, target, [target])
        idents = [str(i) for i in plan.to_start]
        self.assertNotIn("waybar", " ".join(idents))
        self.assertEqual([str(i) for i in plan.to_config],
                         [str(Identity.from_component(target.components[1]))])

    def test_normal_component_still_starts(self):
        target = _manifest("t", [
            _comp_dict("hyprland", "compositor", stop="true",
                       config_link="~/.config/hypr"),
            _comp_dict("waybar", exact_argv=True, stop=""),
        ])
        plan = self._plan(None, target, [target])
        self.assertEqual(len(plan.to_start), 1)
        self.assertEqual(plan.to_config, [])

    def test_still_stopped_when_leaving(self):
        other = _manifest("other", [
            _comp_dict("waybar", started_by_config=True, exact_argv=True,
                       stop=""),
        ])
        target = _manifest("t", [
            _comp_dict("hyprland", "compositor", stop="true",
                       config_link="~/.config/hypr"),
        ])
        ident = Identity.from_component(other.components[0])
        plan = compute_plan(other, target, [other, target], {}, "/cfg",
                            active_identities={ident})
        self.assertIn(ident, plan.to_stop)

    def test_started_by_config_not_in_to_config_when_active(self):
        target = _manifest("t", [
            _comp_dict("waybar", started_by_config=True, exact_argv=True,
                       stop=""),
        ])
        ident = Identity.from_component(target.components[0])
        plan = compute_plan(target, target, [target], {}, "/cfg",
                            active_identities={ident})
        self.assertEqual(plan.to_config, [])
        self.assertIn(ident, plan.to_keep)


class DryRunDisplayTest(unittest.TestCase):
    def test_print_plan_shows_config_managed(self):
        target = _manifest("t", [
            _comp_dict("waybar", started_by_config=True, exact_argv=True,
                       stop=""),
        ])
        plan = compute_plan(None, target, [target], {}, "/cfg",
                            active_identities=set())
        text = _print_plan(plan, _roots())
        self.assertIn("dijalankan oleh config", text)
        self.assertIn("waybar", text)


class ValidateTest(unittest.TestCase):
    def test_rejects_started_by_config_on_compositor(self):
        m = _manifest("a", [
            _comp_dict("hyprland", "compositor", stop="true",
                       started_by_config=True),
        ])
        res = validate_manifest(m, _roots())
        self.assertTrue(any("started_by_config" in e for e in res.errors),
                        res.errors)

    def test_allows_started_by_config_on_normal_component(self):
        m = _manifest("a", [
            _comp_dict("hyprland", "compositor", stop="true"),
            _comp_dict("waybar", started_by_config=True, exact_argv=True,
                       stop=""),
        ])
        res = validate_manifest(m, _roots())
        self.assertFalse(any("started_by_config" in e for e in res.errors),
                         res.errors)


if __name__ == "__main__":
    unittest.main()
