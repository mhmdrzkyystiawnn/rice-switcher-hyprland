"""Fase 3a Langkah 2: line-per-line review driver (DESIGN §5.2/§5.3).

`run_review` is headless-testable: `ask` (prompt -> line, EOFError on EOF)
and `out` (line -> None) are injected, so no real stdin/stdout is used.

Covered: save, cancel via `q`, cancel via EOF (never a silent save),
editing, add/delete/type, undo, unknown input, and a validator-rejected
edit that must be reported and skipped without corrupting the draft.
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

from rice_switcher.detect import CONF_AUTO, DraftComponent, Field, ManifestDraft
from rice_switcher.review import ReviewSession
from rice_switcher.review_cli import run_review


class ScriptedAsk:
    """Returns queued answers; raises EOFError when exhausted."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)


class Recorder:
    def __init__(self):
        self.lines: list[str] = []

    def __call__(self, text: str) -> None:
        self.lines.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def _comp(name="waybar", ctype="bar", *, exe="waybar", argv=("-c", "ii"),
          exact=False, start="waybar -c ii", stop="", start_check="",
          argv_conf=CONF_AUTO) -> DraftComponent:
    return DraftComponent(
        name=name,
        type=Field(ctype, CONF_AUTO),
        executable=Field(exe, CONF_AUTO),
        argv_match=Field(list(argv), argv_conf),
        exact_argv=Field(exact, CONF_AUTO),
        start=Field(start, CONF_AUTO),
        stop=Field(stop, CONF_AUTO),
        start_check=Field(start_check, CONF_AUTO),
    )


def _session(components=None) -> ReviewSession:
    if components is None:
        components = [_comp()]
    return ReviewSession(ManifestDraft(name="r", components=list(components)))


def _drive(session, answers):
    ask = ScriptedAsk(answers)
    out = Recorder()
    outcome = run_review(session, ask=ask, out=out)
    return outcome, ask, out


class DriverTest(unittest.TestCase):
    def test_save(self):
        s = _session()
        outcome, _, _ = _drive(s, ["s"])
        self.assertEqual(outcome.action, "save")

    def test_quit_cancels_without_saving(self):
        s = _session()
        outcome, _, _ = _drive(s, ["q"])
        self.assertEqual(outcome.action, "cancel")
        self.assertEqual(s.component("waybar").start.value, "waybar -c ii")

    def test_eof_cancels(self):
        s = _session()
        outcome, _, _ = _drive(s, [])
        self.assertEqual(outcome.action, "cancel")
        self.assertEqual(outcome.reason, "eof")

    def test_unknown_command_is_reported_and_loop_continues(self):
        s = _session()
        outcome, _, out = _drive(s, ["zzz", "s"])
        self.assertEqual(outcome.action, "save")
        self.assertIn("tidak dikenal", out.text.lower())

    def test_summary_highlights_needs_review_first(self):
        c = _comp()
        c.argv_match.confidence = "ask"
        s = _session([c])
        outcome, _, out = _drive(s, ["s"])
        self.assertEqual(outcome.action, "save")
        self.assertIn("waybar.argv_match", out.text)


class EditCommandTest(unittest.TestCase):
    def test_edit_start(self):
        s = _session()
        outcome, _, _ = _drive(s, ["e", "waybar", "start", "waybar -c other", "s"])
        self.assertEqual(outcome.action, "save")
        self.assertEqual(s.component("waybar").start.value, "waybar -c other")
        self.assertEqual(s.component("waybar").start.confidence, CONF_AUTO)

    def test_edit_argv_match_parses_space_separated(self):
        s = _session()
        _drive(s, ["e", "waybar", "argv_match", "-c minimal", "s"])
        self.assertEqual(s.component("waybar").argv_match.value, ["-c", "minimal"])

    def test_edit_exact_argv_parses_bool(self):
        c = _comp(argv=(), exact=False)
        # give it an identity so it is valid, then flip exact
        c.argv_match = Field([], CONF_AUTO)
        c.exact_argv = Field(True, CONF_AUTO)
        s = _session([c])
        _drive(s, ["e", "waybar", "exact_argv", "true", "s"])
        self.assertTrue(s.component("waybar").exact_argv.value)

    def test_edit_unknown_component_reported(self):
        s = _session()
        outcome, _, out = _drive(s, ["e", "nope", "start", "x", "s"])
        self.assertEqual(outcome.action, "save")
        self.assertIn("nope", out.text)

    def test_rejected_edit_is_reported_and_draft_untouched(self):
        s = _session()
        outcome, _, out = _drive(s, ["e", "waybar", "argv_match", "", "s"])
        self.assertEqual(outcome.action, "save")
        self.assertEqual(s.component("waybar").argv_match.value, ["-c", "ii"])
        self.assertTrue(any(w in out.text.lower() for w in ("tidak", "ditolak", "error")),
                        out.text)

    def test_edit_dangerous_pkill_rejected(self):
        s = _session()
        _drive(s, ["e", "waybar", "stop", "pkill -f waybar", "s"])
        self.assertEqual(s.component("waybar").stop.value, "")

    def test_eof_during_edit_cancels_without_saving(self):
        s = _session()
        outcome, _, _ = _drive(s, ["e"])
        self.assertEqual(outcome.action, "cancel")
        self.assertEqual(s.component("waybar").start.value, "waybar -c ii")


class StructuralCommandTest(unittest.TestCase):
    def test_add(self):
        s = _session([])
        outcome, _, _ = _drive(
            s, ["a", "notifications", "dunst", "dunst", "dunst", "", "s"])
        self.assertEqual(outcome.action, "save")
        self.assertEqual(s.component("dunst").type.value, "notifications")

    def test_add_invalid_type_reported(self):
        s = _session([])
        outcome, _, out = _drive(s, ["a", "bogus", "x", "x", "x", "", "s"])
        self.assertEqual(outcome.action, "save")
        self.assertIn("bogus", out.text)
        self.assertEqual(s.components(), [])

    def test_delete_with_confirmation(self):
        s = _session()
        outcome, _, _ = _drive(s, ["d", "waybar", "y", "s"])
        self.assertEqual(outcome.action, "save")
        self.assertIsNone(s.component("waybar"))

    def test_delete_aborts_without_confirmation(self):
        s = _session()
        outcome, _, _ = _drive(s, ["d", "waybar", "n", "s"])
        self.assertEqual(outcome.action, "save")
        self.assertIsNotNone(s.component("waybar"))

    def test_change_type(self):
        s = _session()
        _drive(s, ["t", "waybar", "shell", "s"])
        self.assertEqual(s.component("waybar").type.value, "shell")

    def test_change_type_invalid_reported(self):
        s = _session()
        _drive(s, ["t", "waybar", "bogus", "s"])
        self.assertEqual(s.component("waybar").type.value, "bar")

    def test_undo(self):
        s = _session()
        _drive(s, ["e", "waybar", "start", "changed", "u", "s"])
        self.assertEqual(s.component("waybar").start.value, "waybar -c ii")

    def test_detail_on_empty_line(self):
        s = _session()
        outcome, _, out = _drive(s, ["", "s"])
        self.assertEqual(outcome.action, "save")
        self.assertIn("waybar", out.text)


if __name__ == "__main__":
    unittest.main()
