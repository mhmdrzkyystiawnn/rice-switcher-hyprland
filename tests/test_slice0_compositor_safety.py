"""Prasyarat 0 safety: the compositor must never be reachable by stop.

`validate.py` exempts `type = "compositor"` from the session-executable
ban, and an empty `stop` now means kill-by-identity. Together that could
let Hyprland be signalled. These tests pin the invariant from both ends:

  * `managed_set.compute_plan` never places a compositor identity in
    `to_stop` or `to_start`, even when that identity is reported active
    but the *current* manifest does not declare it as a compositor
    (the `_type_of(current_manifest)` hole);
  * `lifecycle.stop_component` refuses a compositor with no explicit
    `stop` instead of killing it by identity.

The plan-side test drives the real `compute_plan`; the stop-side test
injects a fake runtime that records `kill_processes`, so no real process
is ever touched.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from rice_switcher.executor import ExecResult
from rice_switcher.lifecycle import LifecycleError, Runtime, stop_component
from rice_switcher.managed_set import Identity, compute_plan
from rice_switcher.manifest import Component, Manifest, Meta


def _comp(name: str, **kw) -> Component:
    kw.setdefault("type", "shell")
    kw.setdefault("start", "true")
    kw.setdefault("stop", "true")
    return Component(name=name, **kw)


def _manifest(name: str, comps, **kw) -> Manifest:
    kw.setdefault("source_path", f"/rices/{name}/rice.toml")
    return Manifest(meta=Meta(name=name, description="d"), components=comps, **kw)


class _FakeRt(Runtime):
    def __init__(self):
        self.up: set[str] = set()
        self.terminated: list[tuple[str, tuple[str, ...]]] = []
        self._t = 0.0
        super().__init__(
            runner=lambda argv, timeout: ExecResult(tuple(argv), 0, "", ""),
            probe=lambda c: c.name in self.up,
            sleeper=lambda s: setattr(self, "_t", self._t + s),
            clock=lambda: self._t,
            poll_interval=0.1,
        )

    def kill_processes(self, c: Component) -> list[int]:
        self.terminated.append((c.executable, tuple(c.argv_match)))
        self.up.discard(c.name)
        return [4242]


class CompositorPlanSafetyTest(unittest.TestCase):
    def test_active_compositor_absent_from_current_is_never_stopped(self):
        # The compositor is in the managed set (rice "a"); the current
        # manifest (rice "b") does not declare it, yet it is reported
        # active. It must not reach to_stop.
        compositor = Component(
            name="hyprland", type="compositor", executable="hyprland",
            argv_match=[], start="Hyprland", stop="",
        )
        ric_a = _manifest("a", [compositor])
        ric_b = _manifest("b", [
            _comp("quickshell", executable="qs", argv_match=["-c", "ii"]),
        ])
        plan = compute_plan(
            current_manifest=ric_b,
            target_manifest=ric_b,
            all_manifests=[ric_a, ric_b],
            symlink_realpaths={},
            config_home="/c",
            active_identities={
                Identity.from_component(compositor),
                Identity.from_component(ric_b.components[0]),
            },
        )
        strings_stop = [str(i) for i in plan.to_stop]
        strings_start = [str(i) for i in plan.to_start]
        self.assertNotIn("hyprland", strings_stop)
        self.assertNotIn("hyprland", strings_start)

    def test_compositor_identity_mistyped_in_current_is_never_stopped(self):
        # An impostor component reuses the compositor identity but claims
        # type "shell". The type must be resolved across ALL manifests, so
        # the identity is still protected.
        compositor = Component(
            name="hyprland", type="compositor", executable="hyprland",
            argv_match=[], start="Hyprland", stop="",
        )
        impostor = Component(
            name="fake", type="shell", executable="hyprland",
            argv_match=[], start="true", stop="",
        )
        ric_a = _manifest("a", [compositor])
        ric_b = _manifest("b", [impostor])
        ric_c = _manifest("c", [_comp("waybar", executable="waybar")])
        plan = compute_plan(
            current_manifest=ric_b,
            target_manifest=ric_c,
            all_manifests=[ric_a, ric_b, ric_c],
            symlink_realpaths={},
            config_home="/c",
            active_identities={Identity.from_component(impostor)},
        )
        self.assertNotIn("hyprland", [str(i) for i in plan.to_stop])
        self.assertNotIn("hyprland", [str(i) for i in plan.to_start])

    def test_compositor_in_target_is_never_started(self):
        compositor = Component(
            name="hyprland", type="compositor", executable="hyprland",
            argv_match=[], start="Hyprland", stop="",
        )
        other = _manifest("other", [_comp("waybar", executable="waybar")])
        target = _manifest("t", [compositor])
        plan = compute_plan(
            current_manifest=other,
            target_manifest=target,
            all_manifests=[other, target],
            symlink_realpaths={},
            config_home="/c",
            active_identities=set(),
        )
        self.assertNotIn("hyprland", [str(i) for i in plan.to_start])
        self.assertNotIn("hyprland", [str(i) for i in plan.to_stop])


class CompositorStopRefusalTest(unittest.TestCase):
    def test_compositor_without_stop_refuses_and_never_kills(self):
        rt = _FakeRt()
        c = Component(
            name="hyprland", type="compositor", executable="hyprland",
            argv_match=["-c", "hypr"], start="Hyprland", stop="", timeout=1,
        )
        with self.assertRaises(LifecycleError):
            stop_component(rt, c)
        self.assertEqual(rt.terminated, [])

    def test_compositor_with_explicit_stop_runs_command_without_kill(self):
        rt = _FakeRt()
        c = Component(
            name="hyprland", type="compositor", executable="hyprland",
            start="Hyprland", stop="true", timeout=1,
        )
        stop_component(rt, c)
        self.assertEqual(rt.terminated, [])


if __name__ == "__main__":
    unittest.main()
