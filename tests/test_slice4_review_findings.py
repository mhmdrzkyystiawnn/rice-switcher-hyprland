"""Fase 4 Langkah 3: findings in the review + `--yes` conflict handling.

Three layers, all hermetic:
  * model (`ReviewSession.handle_finding` / `undo` / `apply_default_findings`);
  * driver (`run_review` `c` command, scripted answers);
  * CLI (`generate --yes` applies defaults and blocks on unresolved findings).
No real terminal, HOME or root is touched.
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

THIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from rice_switcher.cli import build_parser, cmd_generate, main
from rice_switcher.detect import (
    CONF_AUTO,
    AutostartRef,
    DraftComponent,
    Field,
    ManifestDraft,
)
from rice_switcher.gaps import (
    OPTION_CONFIG,
    OPTION_SWITCHER,
    autostart_conflict,
    needs_materialization,
)
from rice_switcher.paths import Roots
from rice_switcher.review import ReviewError, ReviewSession
from rice_switcher.review_cli import run_review


def _ref(kind="exec", line=1, command="waybar", reachable=True):
    return AutostartRef("hypr/hyprland.conf", line, kind,
                        f"{kind} = {command}", command, CONF_AUTO, reachable)


def _comp(name="waybar", ctype="bar", *, argv=(), exact=True) -> DraftComponent:
    return DraftComponent(
        name=name,
        type=Field(ctype, CONF_AUTO),
        executable=Field(name, CONF_AUTO),
        argv_match=Field(list(argv), CONF_AUTO),
        exact_argv=Field(exact, CONF_AUTO),
        start=Field(name, CONF_AUTO),
        stop=Field("", CONF_AUTO),
        start_check=Field("", CONF_AUTO),
    )


def _session(components=None, findings=None) -> ReviewSession:
    draft = ManifestDraft(name="r", components=list(components or [_comp()]),
                          findings=list(findings or []))
    return ReviewSession(draft)


class ScriptedAsk:
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


class ModelTest(unittest.TestCase):
    def test_default_option_sets_started_by_config(self):
        f = autostart_conflict("waybar", [_ref("exec")])
        s = _session(findings=[f])
        s.handle_finding(0)  # default
        self.assertEqual(f.decision, OPTION_CONFIG)
        self.assertTrue(s.component("waybar").started_by_config.value)

    def test_switcher_option_clears_started_by_config(self):
        f = autostart_conflict("waybar", [_ref("exec-once")])
        s = _session(findings=[f])
        s.handle_finding(0, OPTION_SWITCHER)
        self.assertEqual(f.decision, OPTION_SWITCHER)
        self.assertFalse(s.component("waybar").started_by_config.value)

    def test_skip_resolves_without_action(self):
        f = autostart_conflict("waybar", [_ref("exec-once")])
        s = _session(findings=[f])
        s.handle_finding(0, "skip")
        self.assertEqual(f.decision, "skip")
        self.assertTrue(f.is_resolved)
        self.assertFalse(s.component("waybar").started_by_config.value)

    def test_no_default_requires_explicit_option(self):
        f = autostart_conflict("waybar", [_ref("exec-once")])
        s = _session(findings=[f])
        self.assertEqual(f.default_option, "")
        with self.assertRaises(ReviewError):
            s.handle_finding(0)

    def test_unknown_option_rejected(self):
        f = autostart_conflict("waybar", [_ref("exec")])
        s = _session(findings=[f])
        with self.assertRaises(ReviewError):
            s.handle_finding(0, "neutralise")   # Opsi A: not implemented
        self.assertFalse(f.is_resolved)

    def test_bad_index_rejected(self):
        s = _session(findings=[autostart_conflict("waybar", [_ref("exec")])])
        with self.assertRaises(ReviewError):
            s.handle_finding(5, "skip")

    def test_undo_reverts_handling_and_field(self):
        f = autostart_conflict("waybar", [_ref("exec")])
        s = _session(findings=[f])
        s.handle_finding(0)
        self.assertTrue(s.component("waybar").started_by_config.value)
        self.assertTrue(s.undo())
        self.assertFalse(s.component("waybar").started_by_config.value)
        self.assertFalse(s.draft.findings[0].is_resolved)

    def test_apply_default_findings_handles_exec_only(self):
        exec_f = autostart_conflict("waybar", [_ref("exec")])
        once_f = autostart_conflict("hypridle", [_ref("exec-once", command="hypridle")])
        s = _session(components=[_comp("waybar"), _comp("hypridle")],
                     findings=[exec_f, once_f])
        pending = s.apply_default_findings()
        self.assertEqual(pending, [once_f])
        self.assertEqual(exec_f.decision, OPTION_CONFIG)

    def test_pending_findings(self):
        f = autostart_conflict("waybar", [_ref("exec-once")])
        s = _session(findings=[f])
        self.assertEqual(s.pending_findings(), [f])
        s.handle_finding(0, "skip")
        self.assertEqual(s.pending_findings(), [])

    def test_findings_shown_above_components(self):
        f = autostart_conflict("waybar", [_ref("exec")])
        s = _session(findings=[f])
        out = Recorder()
        from rice_switcher.review_cli import _print_summary
        _print_summary(s, out)
        text = out.text
        self.assertIn("autostart bentrok: waybar", text)
        self.assertLess(text.index("Temuan"), text.index("Komponen"))


class DriverTest(unittest.TestCase):
    def _drive(self, session, answers):
        ask = ScriptedAsk(answers)
        out = Recorder()
        outcome = run_review(session, ask=ask, out=out)
        return outcome, ask, out

    def test_c_handles_default_then_save(self):
        f = autostart_conflict("waybar", [_ref("exec")])
        s = _session(findings=[f])
        outcome, _, out = self._drive(s, ["c", "0", "", "s"])
        self.assertEqual(outcome.action, "save")
        self.assertEqual(f.decision, OPTION_CONFIG)
        self.assertTrue(s.component("waybar").started_by_config.value)

    def test_c_handles_explicit_switcher(self):
        f = autostart_conflict("waybar", [_ref("exec-once")])
        s = _session(findings=[f])
        outcome, _, _ = self._drive(s, ["c", "0", "switcher", "s"])
        self.assertEqual(outcome.action, "save")
        self.assertEqual(f.decision, OPTION_SWITCHER)

    def test_c_skip(self):
        f = autostart_conflict("waybar", [_ref("exec-once")])
        s = _session(findings=[f])
        outcome, _, _ = self._drive(s, ["c", "0", "skip", "s"])
        self.assertEqual(outcome.action, "save")
        self.assertEqual(f.decision, "skip")

    def test_c_without_default_and_enter_is_reported(self):
        f = autostart_conflict("waybar", [_ref("exec-once")])
        s = _session(findings=[f])
        outcome, _, out = self._drive(s, ["c", "0", "", "s"])
        self.assertEqual(outcome.action, "save")
        self.assertFalse(f.is_resolved)
        self.assertIn("tidak punya opsi default", out.text)

    def test_undo_after_handling_via_driver(self):
        f = autostart_conflict("waybar", [_ref("exec")])
        s = _session(findings=[f])
        outcome, _, _ = self._drive(s, ["c", "0", "", "u", "s"])
        self.assertEqual(outcome.action, "save")
        self.assertFalse(s.draft.findings[0].is_resolved)
        self.assertFalse(s.component("waybar").started_by_config.value)


class CliBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rs-4-review-")
        self.home = os.path.join(self.tmp, "home")
        self.rice_home = os.path.join(self.home, "rice")
        self.config_home = os.path.join(self.home, ".config")
        self.state_home = os.path.join(self.home, ".local", "state")
        for d in (self.rice_home, self.config_home, self.state_home):
            os.makedirs(d, exist_ok=True)
        self.env = {
            "HOME": self.home,
            "RICE_HOME": self.rice_home,
            "RICE_CONFIG_HOME": self.config_home,
            "RICE_STATE_HOME": self.state_home,
        }
        self.roots = Roots(self.rice_home, self.config_home, self.state_home)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def mkrice(self, name, files=None):
        rice = os.path.join(self.rice_home, name)
        os.makedirs(rice, exist_ok=True)
        for rel, content in (files or {}).items():
            path = os.path.join(rice, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(content)
        return rice

    def manifest_path(self, name):
        return os.path.join(self.rice_home, name, "rice.toml")

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=False):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def run_direct(self, argv, answers):
        args = build_parser().parse_args(argv)
        out: list[str] = []
        so, se = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=False):
            with contextlib.redirect_stdout(so), contextlib.redirect_stderr(se):
                rc = cmd_generate(args, self.roots, None,
                                  ask=ScriptedAsk(answers), out=out.append)
        return rc, "\n".join(out) + so.getvalue() + se.getvalue()


class YesConflictTest(CliBase):
    def test_yes_exec_conflict_applies_default_and_saves(self):
        self.mkrice("r", {"hypr/hyprland.conf": "exec = waybar\n"})
        rc, out, err = self.run_cli("generate", "r", "--yes")
        self.assertEqual(rc, 0, out + err)
        data = tomllib.loads(open(self.manifest_path("r")).read())
        waybar = next(c for c in data["components"] if c["name"] == "waybar")
        self.assertTrue(waybar["started_by_config"])

    def test_yes_exec_once_conflict_stops_without_writing(self):
        self.mkrice("r", {"hypr/hyprland.conf": "exec-once = waybar\n"})
        rc, out, err = self.run_cli("generate", "r", "--yes")
        self.assertNotEqual(rc, 0, out + err)
        self.assertFalse(os.path.exists(self.manifest_path("r")))
        self.assertIn("autostart_conflict", err)
        self.assertIn("waybar", err)

    def test_yes_needs_materialization_stops_without_writing(self):
        self.mkrice("r", {"profiles/ii.conf": "exec = waybar\n"})
        rc, out, err = self.run_cli("generate", "r", "--yes")
        self.assertNotEqual(rc, 0, out + err)
        self.assertFalse(os.path.exists(self.manifest_path("r")))
        self.assertIn("needs_materialization", err)

    def test_yes_clean_rice_saves(self):
        self.mkrice("r", {"hypr/hyprland.conf": "general { gaps_in = 2 }\n"})
        rc, out, err = self.run_cli("generate", "r", "--yes")
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(os.path.isfile(self.manifest_path("r")))

    def test_interactive_handle_conflict_then_save(self):
        self.mkrice("r", {"hypr/hyprland.conf": "exec-once = waybar\n"})
        rc, text = self.run_direct(
            ["generate", "r", "--no-tui"],
            ["c", "0", "switcher", "s"],
        )
        self.assertEqual(rc, 0, text)
        data = tomllib.loads(open(self.manifest_path("r")).read())
        waybar = next(c for c in data["components"] if c["name"] == "waybar")
        self.assertFalse(waybar.get("started_by_config", False))


if __name__ == "__main__":
    unittest.main()
