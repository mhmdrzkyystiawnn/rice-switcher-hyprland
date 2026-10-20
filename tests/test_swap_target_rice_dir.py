"""Regression: config_link swap detection between two rices that
declare the SAME config_link.

Bug (found 2026-10-20): `compute_plan` computed

    target_rice_dir = os.path.dirname(os.path.dirname(source_path))

which is `<rice_home>` (one level ABOVE the rice dir), not
`<rice_home>/<name>`. Since every rice lives under `<rice_home>`,
`realpath(live_link).startswith(<rice_home> + "/")` was True for
*every* rice -- so switching between two rices that both declare the
same config_link reported `to_swap == []` and silently skipped the
swap. The live symlink kept pointing at the previous rice, which is
why a `elifouts -> end4` switch left elifouts' keybinds in place.

The swap target written by `switch.do_switch` is
`<rice_home>/<name>/<basename(config_link)>`, so the directory a live
link must point under is the rice dir itself.

Hermetic: everything under a TemporaryDirectory; no real switch, no
process management, no writes outside the temp tree.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from rice_switcher.managed_set import (
    _config_link_swap_needed,
    compute_plan,
)
from rice_switcher.manifest import Component, Manifest, Meta
from rice_switcher.paths import Roots


def _make_roots(tmp: str) -> Roots:
    base = os.path.join(tmp, "root")
    rh = os.path.join(base, "rice")
    ch = os.path.join(base, "config")
    sh = os.path.join(base, "state")
    for d in (rh, ch, sh):
        os.makedirs(d, exist_ok=True)
    return Roots(rice_home=rh, config_home=ch, state_home=sh)


def _manifest(roots: Roots, name: str, link: str) -> Manifest:
    """A one-component rice (compositor with a config_link), written to
    disk so `source_path` is set exactly as the real loader sets it."""
    rice_dir = os.path.join(roots.rice_home, name)
    os.makedirs(os.path.join(rice_dir, os.path.basename(link)), exist_ok=True)
    src = os.path.join(rice_dir, "rice.toml")
    with open(src, "w") as f:
        f.write(
            f'[meta]\nname = "{name}"\n\n'
            f'[[components]]\nname = "hyprland"\ntype = "compositor"\n'
            f'start = "true"\nstop = "true"\n'
            f'config_link = "{link}"\n'
        )
    return Manifest(
        meta=Meta(name=name),
        components=[Component(
            name="hyprland", type="compositor",
            start="true", stop="true", config_link=link,
        )],
        source_path=src,
    )


class SwapTargetRiceDirTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory(prefix="rs-swap-target-")
        self.roots = _make_roots(self._td.name)
        self.live = os.path.join(self.roots.config_home, "hypr")

    def tearDown(self):
        self._td.cleanup()

    # -- the core regression ------------------------------------------

    def test_swap_needed_when_link_owned_by_other_rice(self):
        """live -> elifouts, target = end4: the swap MUST be planned."""
        a = _manifest(self.roots, "elifouts", self.live)
        b = _manifest(self.roots, "end4", self.live)
        # Live link currently points at elifouts' rice dir.
        sym_state: dict[str, str | None] = {
            self.live: os.path.join(self.roots.rice_home, "elifouts", "hypr")
        }

        plan = compute_plan(a, b, [a, b], sym_state, self.roots.config_home)

        self.assertEqual(plan.target, "end4")
        self.assertEqual(
            plan.to_swap, [self.live],
            "switching to a rice that declares the same config_link as the "
            "current rice must still plan the swap; got "
            f"to_swap={plan.to_swap!r}",
        )

    def test_no_swap_when_link_already_owned_by_target(self):
        """live -> end4, target = end4: no swap needed."""
        a = _manifest(self.roots, "elifouts", self.live)
        b = _manifest(self.roots, "end4", self.live)
        sym_state: dict[str, str | None] = {
            self.live: os.path.join(self.roots.rice_home, "end4", "hypr")
        }

        plan = compute_plan(a, b, [a, b], sym_state, self.roots.config_home)

        self.assertEqual(
            plan.to_swap, [],
            f"link already points at the target rice; got to_swap={plan.to_swap!r}",
        )

    def test_swap_needed_when_link_dangling_or_absent(self):
        a = _manifest(self.roots, "elifouts", self.live)
        b = _manifest(self.roots, "end4", self.live)
        plan = compute_plan(a, b, [a, b], {self.live: None}, self.roots.config_home)
        self.assertEqual(plan.to_swap, [self.live])

    # -- unit-level check of the predicate ----------------------------

    def test_predicate_uses_rice_dir_not_rice_home(self):
        """`_config_link_swap_needed` must be False only when the link
        points under `<rice_home>/<name>`, never for a sibling rice."""
        b = _manifest(self.roots, "end4", self.live)
        rice_dir = os.path.dirname(b.source_path)  # <rice_home>/end4

        # Points at the target rice -> no swap.
        self.assertFalse(_config_link_swap_needed(
            self.live,
            {self.live: os.path.join(rice_dir, "hypr")},
            rice_dir,
        ))
        # Points at a SIBLING rice (still under rice_home) -> swap.
        sibling = os.path.join(self.roots.rice_home, "elifouts", "hypr")
        self.assertTrue(
            _config_link_swap_needed(self.live, {self.live: sibling}, rice_dir),
            "a sibling rice's path must NOT count as 'already swapped'; "
            f"sibling={sibling!r} rice_dir={rice_dir!r}",
        )

    def test_target_rice_dir_is_the_rice_dir(self):
        """Guard the exact expression: one dirname (the rice dir), not
        two (rice_home). This is what made the bug silent."""
        b = _manifest(self.roots, "end4", self.live)
        expected = os.path.join(self.roots.rice_home, "end4")
        self.assertEqual(os.path.dirname(b.source_path), expected)
        self.assertNotEqual(
            os.path.dirname(os.path.dirname(b.source_path)), expected,
            "two dirnames give rice_home, which is the buggy value",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
