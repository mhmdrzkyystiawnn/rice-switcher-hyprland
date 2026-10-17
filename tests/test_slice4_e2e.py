"""Fase 4 Langkah 4: hermetic end-to-end conflict handling.

The full Fase 4 flow in one tempdir, with every root redirected by env
override (`RICE_HOME`/`RICE_CONFIG_HOME`/`RICE_STATE_HOME` + `HOME`):

    clone (local `file://`) -> `generate --no-tui` with a scripted `c`
    answer that takes the conflict's default option -> `validate` ->
    `switch --dry-run`.

Proof obligations:

  * a component handled with the default option (Opsi B, an `exec` line)
    is written as `started_by_config = true`, so the dry-run plan lists it
    as "dijalankan oleh config" and **never** puts it in `to_start`
    (no double start on reload);
  * nothing is written outside the tempdir; the dry-run changes no link.
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
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
from rice_switcher.managed_set import Identity, compute_plan
from rice_switcher.manifest import load_manifest
from rice_switcher.paths import Roots
from rice_switcher.state import State, write_state
from rice_switcher.switch import do_switch


class _ScriptedAsk:
    def __init__(self, answers):
        self.answers = list(answers)

    def __call__(self, prompt: str) -> str:
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)


def _write(base: str, files: dict) -> None:
    for rel, content in files.items():
        path = os.path.join(base, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)


class Fase4E2ETest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rs-4-e2e-")
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
            "RICE_TEST": "1",
            "RICE_DRY_RUN": "0",
        }
        self.roots = Roots(self.rice_home, self.config_home, self.state_home)
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=False):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def _source_repo(self) -> str:
        src = os.path.join(self.tmp, "src")
        # `exec` (not `exec-once`): re-runs on reload, so the default Opsi B
        # is safe and must suppress the switcher's start.
        _write(src, {
            ".config/hypr/hyprland.conf": (
                "monitor = , preferred, auto, auto\n"
                "exec = waybar\n"
            ),
            ".config/waybar/config.jsonc": "{}\n",
        })
        genv = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                    GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        for argv in (["git", "init", "-q"], ["git", "add", "-A"],
                     ["git", "commit", "-q", "-m", "init"]):
            subprocess.run(argv, cwd=src, check=True, env=genv,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return src

    def test_clone_review_conflict_default_dryrun(self):
        src = self._source_repo()

        rc, out, err = self.run_cli(
            "clone", f"file://{src}", "--name", "rice4",
            "--allow-local", "--yes",
        )
        self.assertEqual(rc, 0, err)
        cloned = os.path.join(self.rice_home, "rice4")

        # generate --no-tui: handle finding 0 with its default (Opsi B), save.
        args = build_parser().parse_args(["generate", "rice4", "--no-tui"])
        out_lines: list[str] = []
        with mock.patch.dict(os.environ, self.env, clear=False):
            rc = cmd_generate(args, self.roots, None,
                              ask=_ScriptedAsk(["c", "0", "", "s"]),
                              out=out_lines.append)
        self.assertEqual(rc, 0, "\n".join(out_lines))
        mp = os.path.join(cloned, "rice.toml")
        self.assertTrue(os.path.isfile(mp))
        data = tomllib.loads(open(mp).read())
        comps = {c["name"]: c for c in data["components"]}
        self.assertIn("waybar", comps)
        # the default option was applied to the manifest.
        self.assertTrue(comps["waybar"]["started_by_config"])

        # validate.
        rc, out, err = self.run_cli("validate", "rice4")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("0 error(s)", out + err)

        # the dry-run plan must NOT start waybar; it is "dijalankan oleh config".
        live_before = {n: os.readlink(os.path.join(self.config_home, n))
                       for n in os.listdir(self.config_home)
                       if os.path.islink(os.path.join(self.config_home, n))}
        rc, out, err = self.run_cli("switch", "rice4", "--dry-run")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("dijalankan oleh config", out)
        self.assertIn("waybar", out)

        # precise plan assertion: to_config has waybar, to_start does not.
        cloned_m = load_manifest(mp)
        with mock.patch.dict(os.environ, self.env, clear=False):
            write_state(self.roots, State(current_rice=""))
            outcome = do_switch(self.roots, "rice4", [cloned_m], dry_run=True,
                                manage_processes=False)
        waybar_c = next(c for c in cloned_m.components if c.name == "waybar")
        ident = Identity.from_component(waybar_c)
        self.assertIn(ident, outcome.plan.to_config)
        self.assertNotIn(ident, outcome.plan.to_start)

        # dry-run changed no live link.
        live_after = {n: os.readlink(os.path.join(self.config_home, n))
                      for n in os.listdir(self.config_home)
                      if os.path.islink(os.path.join(self.config_home, n))}
        self.assertEqual(live_before, live_after)

        # everything landed inside the tempdir.
        tmp_real = os.path.realpath(self.tmp)
        for p in (mp, self.config_home, self.state_home,
                  os.path.join(self.rice_home, "rice4")):
            self.assertTrue(os.path.realpath(p).startswith(tmp_real),
                            f"{p} resolves outside the tempdir")

    def test_double_start_control_without_flag(self):
        """Control: the same component without the flag *would* be started."""
        # Build two manifests by hand: identical except `started_by_config`.
        tmp_manifest = os.path.join(self.tmp, "ctrl.toml")
        with open(tmp_manifest, "w") as f:
            f.write(
                '[meta]\nname = "ctrl"\ndescription = "d"\n\n'
                '[[components]]\nname = "hyprland"\ntype = "compositor"\n'
                'start = "true"\nstop = "true"\nconfig_link = "~/.config/hypr"\n\n'
                '[[components]]\nname = "waybar"\ntype = "bar"\n'
                'executable = "waybar"\nexact_argv = true\nstart = "waybar"\n'
                'stop = ""\nstarted_by_config = true\n'
            )
        flag_m = load_manifest(tmp_manifest)
        waybar_flag = next(c for c in flag_m.components if c.name == "waybar")
        ident = Identity.from_component(waybar_flag)

        plan = compute_plan(None, flag_m, [flag_m], {}, self.config_home,
                            active_identities=set())
        self.assertIn(ident, plan.to_config)
        self.assertNotIn(ident, plan.to_start)

        # flip the flag off: now it is a normal start.
        waybar_flag.started_by_config = False
        plan2 = compute_plan(None, flag_m, [flag_m], {}, self.config_home,
                             active_identities=set())
        self.assertIn(ident, plan2.to_start)
        self.assertNotIn(ident, plan2.to_config)


if __name__ == "__main__":
    unittest.main()
