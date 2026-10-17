"""Fase 2b Langkah 2: warn when a managed component can never be stopped.

A non-compositor component with a meaningful `start` but a no-op `stop`
(`true`, `:`, or an empty `stop` with no pin-down-able identity) will be
started on every switch and never stopped — so switching to the rice that
declares it leaves a duplicate instance running. `validate` must say so.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from rice_switcher.manifest import Component, Manifest, Meta
from rice_switcher.paths import Roots
from rice_switcher.validate import validate_manifest

_ROOTS = Roots("/r", "/c", "/s")


def _manifest(*comps) -> Manifest:
    return Manifest(meta=Meta(name="r", description="d"), components=list(comps))


def _comp(name="waybar", ctype="bar", **kw) -> Component:
    kw.setdefault("start", "waybar")
    return Component(name=name, type=ctype, **kw)


def _stop_warnings(res):
    return [w for w in res.warnings if "never be stopped" in w or "duplicate" in w]


class StopNoopWarningTest(unittest.TestCase):
    def _warns(self, c: Component) -> bool:
        res = validate_manifest(_manifest(c), _ROOTS, check_binaries=False)
        return bool(_stop_warnings(res))

    # --- warn -------------------------------------------------------------

    def test_real_start_with_stop_true_warns(self):
        res = validate_manifest(
            _manifest(_comp(stop="true")), _ROOTS, check_binaries=False
        )
        self.assertTrue(_stop_warnings(res), res.warnings)

    def test_real_start_with_stop_colon_warns(self):
        self.assertTrue(self._warns(_comp(stop=":")))

    def test_real_start_with_empty_stop_no_identity_warns(self):
        self.assertTrue(self._warns(_comp(stop="", argv_match=[])))

    # --- do not warn ------------------------------------------------------

    def test_empty_stop_with_argv_match_does_not_warn(self):
        self.assertFalse(self._warns(_comp(stop="", argv_match=["-c", "ii"])))

    def test_empty_stop_with_exact_argv_does_not_warn(self):
        self.assertFalse(
            self._warns(_comp(stop="", exact_argv=True))
        )

    def test_real_stop_command_does_not_warn(self):
        self.assertFalse(self._warns(_comp(stop="pkill -x waybar")))

    def test_start_true_is_not_meaningful(self):
        self.assertFalse(self._warns(_comp(ctype="shell", start="true", stop="true")))

    def test_compositor_is_exempt(self):
        c = _comp(name="hyprland", ctype="compositor", start="true", stop="true")
        self.assertFalse(self._warns(c))
        # Even a compositor with a real start and no-op stop is exempt: the
        # switcher never stops or starts the compositor (PLAN.md §2c).
        c2 = _comp(name="hyprland", ctype="compositor", start="Hyprland", stop="true")
        self.assertFalse(self._warns(c2))


if __name__ == "__main__":
    unittest.main()
