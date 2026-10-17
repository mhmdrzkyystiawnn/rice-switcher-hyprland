"""Fase 3a Langkah 1: pure review/edit model (DESIGN §5).

`rice_switcher.review` wraps a `ManifestDraft` and exposes validated edit
operations plus a one-step (stackable) undo. It performs no I/O: tests build
drafts in memory and assert on the resulting state.

Rules reused (not reinvented):
  * valid component `type` comes from `manifest.VALID_TYPES`;
  * session-critical executables come from `validate._SESSION_EXECUTABLES`;
  * `pkill -f`/`--full` is refused (validate / process);
  * an empty `argv_match` without `exact_argv` is refused for a
    non-compositor (lifecycle identity-stop guard);
  * the compositor's `stop` is locked to `"true"`.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from rice_switcher.detect import (
    CONF_ASK,
    CONF_AUTO,
    CONF_GUESS,
    DraftComponent,
    Field,
    Finding,
    ManifestDraft,
)
from rice_switcher.paths import Roots
from rice_switcher.review import (
    EDITABLE_FIELDS,
    ReviewError,
    ReviewSession,
)


def _roots(base: str = "/tmp/rs-3a-review-roots") -> Roots:
    return Roots(
        rice_home=os.path.join(base, "rice"),
        config_home=os.path.join(base, "config"),
        state_home=os.path.join(base, "state"),
    )


def _comp(name="waybar", ctype="bar", *, exe="waybar", argv=(),
          exact=False, start="waybar", stop="", start_check="",
          argv_conf=CONF_AUTO) -> DraftComponent:
    return DraftComponent(
        name=name,
        type=Field(ctype, CONF_AUTO),
        executable=Field(exe, argv_conf),
        argv_match=Field(list(argv), argv_conf),
        exact_argv=Field(exact, CONF_AUTO),
        start=Field(start, CONF_AUTO),
        stop=Field(stop, CONF_AUTO),
        start_check=Field(start_check, CONF_AUTO),
    )


def _session(components) -> ReviewSession:
    return ReviewSession(ManifestDraft(name="r", components=list(components)))


def _waybar_and_compositor():
    return [
        _comp("hyprland", "compositor", exe="", argv=(), exact=False,
              start="true", stop="true"),
        _comp(),
    ]


class SummaryTest(unittest.TestCase):
    def test_summary_lists_name_type_confidence(self):
        s = _session(_waybar_and_compositor())
        rows = {r.name: r for r in s.summary()}
        self.assertEqual(rows["waybar"].type, "bar")
        self.assertEqual(rows["waybar"].confidence, CONF_AUTO)
        self.assertEqual(rows["hyprland"].type, "compositor")

    def test_needs_review_highlights_guess_and_ask(self):
        c = _comp()
        c.argv_match.confidence = CONF_ASK
        c.start.confidence = CONF_GUESS
        s = _session([c])
        fields = {f for _, f in s.needs_review()}
        self.assertEqual(fields, {"argv_match", "start"})

    def test_summary_confidence_is_worst_field(self):
        c = _comp()
        c.argv_match.confidence = CONF_ASK
        s = _session([c])
        self.assertEqual(s.summary()[0].confidence, CONF_ASK)


class FieldEditTest(unittest.TestCase):
    def test_edit_start_sets_value_and_auto_confidence(self):
        c = _comp(argv=["-c", "ii"])
        c.start.confidence = CONF_ASK
        s = _session([c])
        s.set_field("waybar", "start", "waybar -c other")
        self.assertEqual(s.component("waybar").start.value, "waybar -c other")
        self.assertEqual(s.component("waybar").start.confidence, CONF_AUTO)

    def test_edit_argv_and_exact(self):
        c = _comp(argv=["-c", "ii"])
        s = _session([c])
        s.set_field("waybar", "argv_match", ["-c", "minimal"])
        self.assertEqual(s.component("waybar").argv_match.value, ["-c", "minimal"])
        s.set_field("waybar", "exact_argv", True)
        self.assertTrue(s.component("waybar").exact_argv.value)
        self.assertEqual(s.component("waybar").exact_argv.confidence, CONF_AUTO)

    def test_edit_name_renames(self):
        s = _session([_comp()])
        s.set_field("waybar", "name", "bar-top")
        self.assertIsNone(s.component("waybar"))
        self.assertIsNotNone(s.component("bar-top"))

    def test_edit_executable(self):
        s = _session([_comp()])
        s.set_field("waybar", "executable", "waybar-next")
        self.assertEqual(s.component("waybar").executable.value, "waybar-next")

    def test_edit_start_check(self):
        s = _session([_comp()])
        s.set_field("waybar", "start_check", "pgrep -x waybar")
        self.assertEqual(s.component("waybar").start_check.value, "pgrep -x waybar")

    def test_editable_fields_are_the_documented_set(self):
        self.assertEqual(
            set(EDITABLE_FIELDS),
            {"name", "start", "executable", "argv_match", "exact_argv",
             "stop", "start_check"},
        )


class RejectionTest(unittest.TestCase):
    def test_empty_argv_without_exact_rejected(self):
        s = _session([_comp(argv=["-c", "ii"])])
        with self.assertRaises(ReviewError):
            s.set_field("waybar", "argv_match", [])
        # unchanged
        self.assertEqual(s.component("waybar").argv_match.value, ["-c", "ii"])

    def test_clearing_exact_rejected_when_argv_empty(self):
        s = _session([_comp(argv=[], exact=True)])
        with self.assertRaises(ReviewError):
            s.set_field("waybar", "exact_argv", False)
        self.assertTrue(s.component("waybar").exact_argv.value)

    def test_empty_argv_with_exact_is_allowed(self):
        s = _session([_comp(argv=["-c", "ii"], exact=True)])
        s.set_field("waybar", "argv_match", [])
        self.assertEqual(s.component("waybar").argv_match.value, [])

    def test_forbidden_executable_rejected(self):
        s = _session([_comp(exe="waybar")])
        for bad in ("systemd", "dbus-daemon", "gnome-keyring-daemon"):
            with self.assertRaises(ReviewError):
                s.set_field("waybar", "executable", bad)

    def test_compositor_executable_is_allowed(self):
        # The compositor legitimately names Hyprland.
        s = _session([_comp("hyprland", "compositor", exe="hyprland",
                            argv=["-c", "cfg"], stop="true")])
        s.set_field("hyprland", "executable", "Hyprland")
        self.assertEqual(s.component("hyprland").executable.value, "Hyprland")

    def test_compositor_stop_locked_true(self):
        s = _session(_waybar_and_compositor())
        with self.assertRaises(ReviewError):
            s.set_field("hyprland", "stop", "")
        self.assertEqual(s.component("hyprland").stop.value, "true")

    def test_compositor_stop_true_is_accepted(self):
        s = _session(_waybar_and_compositor())
        s.set_field("hyprland", "stop", "true")  # no-op, already true
        self.assertEqual(s.component("hyprland").stop.value, "true")

    def test_dangerous_pkill_rejected(self):
        s = _session([_comp()])
        for field in ("start", "stop", "start_check"):
            with self.assertRaises(ReviewError):
                s.set_field("waybar", field, "pkill -f waybar")

    def test_unknown_field_rejected(self):
        s = _session([_comp()])
        with self.assertRaises(ReviewError):
            s.set_field("waybar", "timeout", 5)

    def test_unknown_component_rejected(self):
        s = _session([_comp()])
        with self.assertRaises(ReviewError):
            s.set_field("nope", "start", "x")

    def test_duplicate_name_rejected(self):
        s = _session([_comp("waybar"), _comp("mako", "notifications", exe="mako")])
        with self.assertRaises(ReviewError):
            s.set_field("mako", "name", "waybar")

    def test_empty_name_rejected(self):
        s = _session([_comp()])
        with self.assertRaises(ReviewError):
            s.set_field("waybar", "name", "")

    def test_invalid_type_rejected(self):
        s = _session([_comp()])
        with self.assertRaises(ReviewError):
            s.set_type("waybar", "not-a-type")

    def test_rejected_edit_leaves_draft_unchanged(self):
        s = _session([_comp(argv=["-c", "ii"])])
        before = s.component("waybar").argv_match.value
        with self.assertRaises(ReviewError):
            s.set_field("waybar", "argv_match", [])
        self.assertEqual(s.component("waybar").argv_match.value, before)


class StructuralTest(unittest.TestCase):
    def test_add_component(self):
        s = _session([])
        c = s.add("notifications", name="dunst", start="dunst")
        self.assertEqual(c.name, "dunst")
        self.assertEqual(s.component("dunst").type.value, "notifications")

    def test_add_default_name_unique(self):
        s = _session([_comp("bar", "bar")])
        c = s.add("bar")
        self.assertNotEqual(c.name, "bar")
        self.assertNotIn(c.name, ("",))
        self.assertEqual(len(s.components()), 2)

    def test_add_invalid_type_rejected(self):
        s = _session([])
        with self.assertRaises(ReviewError):
            s.add("nope")

    def test_remove_component(self):
        s = _session(_waybar_and_compositor())
        s.remove("waybar")
        self.assertIsNone(s.component("waybar"))

    def test_remove_missing_rejected(self):
        s = _session([_comp()])
        with self.assertRaises(ReviewError):
            s.remove("nope")

    def test_move_reorders(self):
        a = _comp("a", "bar")
        b = _comp("b", "bar")
        s = _session([a, b])
        s.move("b", -1)
        self.assertEqual([c.name for c in s.components()], ["b", "a"])

    def test_move_clamps_at_edges(self):
        a = _comp("a", "bar")
        b = _comp("b", "bar")
        s = _session([a, b])
        s.move("a", -1)
        self.assertEqual([c.name for c in s.components()], ["a", "b"])

    def test_set_type_changes_and_marks_auto(self):
        c = _comp(argv=["-c", "ii"])
        c.type.confidence = CONF_GUESS
        s = _session([c])
        s.set_type("waybar", "shell")
        self.assertEqual(s.component("waybar").type.value, "shell")
        self.assertEqual(s.component("waybar").type.confidence, CONF_AUTO)

    def test_set_type_to_compositor_forces_stop_true(self):
        s = _session([_comp(stop="")])
        s.set_type("waybar", "compositor")
        self.assertEqual(s.component("waybar").stop.value, "true")


class UndoTest(unittest.TestCase):
    def test_undo_restores_one_step(self):
        s = _session([_comp()])
        s.set_field("waybar", "start", "changed")
        self.assertTrue(s.undo())
        self.assertEqual(s.component("waybar").start.value, "waybar")

    def test_undo_stacks(self):
        s = _session([_comp()])
        s.set_field("waybar", "start", "one")
        s.set_field("waybar", "start", "two")
        s.undo()
        self.assertEqual(s.component("waybar").start.value, "one")
        s.undo()
        self.assertEqual(s.component("waybar").start.value, "waybar")

    def test_undo_covers_structural_changes(self):
        s = _session([])
        s.add("bar", name="x")
        self.assertTrue(s.undo())
        self.assertEqual(s.components(), [])

    def test_undo_without_history_returns_false(self):
        s = _session([_comp()])
        self.assertFalse(s.undo())


class FindingsTest(unittest.TestCase):
    def test_findings_empty_by_default(self):
        s = _session([_comp()])
        self.assertEqual(s.findings(), [])

    def test_findings_passthrough(self):
        d = ManifestDraft(name="r", components=[_comp()])
        d.findings.append(Finding(kind="gap", title="needs materialisation"))
        s = ReviewSession(d)
        self.assertEqual([f.title for f in s.findings()],
                         ["needs materialisation"])


class ValidateTest(unittest.TestCase):
    def test_validate_reports_missing_start(self):
        c = _comp(start="")
        s = _session([c])
        res = s.validate(_roots())
        self.assertTrue(any("start" in e for e in res.errors), res.errors)

    def test_validate_ok_for_wellformed(self):
        s = _session(_waybar_and_compositor())
        res = s.validate(_roots())
        self.assertEqual(res.errors, [])

    def test_to_manifest_carries_name_and_components(self):
        s = _session(_waybar_and_compositor())
        m = s.to_manifest()
        self.assertEqual(m.meta.name, "r")
        self.assertEqual({c.name for c in m.components}, {"hyprland", "waybar"})


if __name__ == "__main__":
    unittest.main()
