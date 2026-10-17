"""Fase 2b Langkah 4: hermetic end-to-end onboarding test.

Exercises the whole "clone a third-party rice -> make it switchable" flow in
one tempdir, with every root redirected by env override
(`RICE_HOME`/`RICE_CONFIG_HOME`/`RICE_STATE_HOME`, and `HOME` so `~`
expansion agrees with those roots). Nothing outside the tempdir is touched:
no network (the "remote" is a local `file://` git repo), no real desktop.

Steps:

  1. `clone` a local `.config/<x>` fixture repo;
  2. `generate --write` the draft (auto layout normalization);
  3. `validate` the generated manifest;
  4. `switch --dry-run` through the CLI;
  5. a real `switch` via `do_switch` with an injected stub Runtime.

Proof obligations:

  * **mismatch #6** — the normalization symlink `<rice>/<x> -> .config/<x>`
    makes the swap target `<rice_home>/<name>/<x>` resolve to the rice's
    `.config/<x>`, so the live `~/.config/<x>` link points at the right tree;
  * **Langkah 1** — an argument-less daemon component (empty `stop`,
    `exact_argv = true`) is stopped by process identity: the no-arg process
    dies, an argument-carrying process with the *same* executable basename
    survives.
"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from unittest import mock

THIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from rice_switcher.cli import main
from rice_switcher.executor import DEFAULT_TIMEOUT, HOOK_TIMEOUT, ExecResult
from rice_switcher.lifecycle import Runtime
from rice_switcher.managed_set import Identity
from rice_switcher.manifest import load_manifest
from rice_switcher.paths import Roots
from rice_switcher.state import State, write_state
from rice_switcher.switch import do_switch


def _write(base: str, files: dict) -> None:
    for rel, content in files.items():
        path = os.path.join(base, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)


_BASE_TOML = """\
[meta]
name = "base"
description = "baseline for the e2e test"

[[components]]
name = "hyprland"
type = "compositor"
start = "true"
stop = "true"
config_link = "~/.config/hypr"
reload = "hyprctl reload"

[[components]]
name = "waybar"
type = "bar"
executable = "waybar"
exact_argv = true
start = "waybar"
stop = ""
config_link = "~/.config/waybar"

[[components]]
name = "mako"
type = "notifications"
executable = "STUB_BASENAME"
exact_argv = true
start = "true"
stop = ""
timeout = 5
"""


class _StubRuntime(Runtime):
    """A Runtime whose command seams are no-ops.

    `kill_processes`/`is_running` are NOT overridden, so the stop path is
    the real `terminate_by_identity` against the real stub processes.
    """

    def __init__(self):
        super().__init__()
        self.calls: list[tuple[str, object]] = []

    def run(self, command, *, timeout=DEFAULT_TIMEOUT):
        self.calls.append(("run", command))
        return ExecResult((command,), 0, "", "")

    def argv(self, argv, *, timeout=DEFAULT_TIMEOUT):
        self.calls.append(("argv", tuple(argv)))
        return ExecResult(tuple(argv), 0, "", "")

    def hook(self, command, *, timeout=HOOK_TIMEOUT):
        self.calls.append(("hook", command))
        return ExecResult((command,), 0, "", "")

    def spawn(self, command, *, timeout=DEFAULT_TIMEOUT):
        self.calls.append(("spawn", command))
        return ExecResult((command,), 0, "", "")


class OnboardingE2ETest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rs-e2e-")
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
        }
        self.roots = Roots(self.rice_home, self.config_home, self.state_home)
        self._procs: list[subprocess.Popen] = []
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for p in self._procs:
            try:
                p.kill()
                p.wait(timeout=5)
            except Exception:
                pass
            try:
                if p.stdin:
                    p.stdin.close()
            except Exception:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- helpers ----------------------------------------------------------

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=False):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def _make_source_repo(self) -> str:
        src = os.path.join(self.tmp, "src")
        _write(src, {
            ".config/hypr/hyprland.conf": "exec-once = waybar\n",
            ".config/waybar/config.jsonc": "{}\n",
        })
        genv = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                    GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        for argv in (["git", "init", "-q"],
                     ["git", "add", "-A"],
                     ["git", "commit", "-q", "-m", "init"]):
            subprocess.run(argv, cwd=src, check=True, env=genv,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return src

    def _stub_binary(self) -> str:
        cat = shutil.which("cat")
        if not cat:
            self.skipTest("no `cat` binary to build a no-arg stub process")
        dst = os.path.join(self.tmp, f"rsstub{os.getpid()}")
        shutil.copy2(cat, dst)
        os.chmod(dst, 0o755)
        return dst

    def _spawn(self, argv) -> subprocess.Popen:
        p = subprocess.Popen(argv, stdin=subprocess.PIPE)
        self._procs.append(p)
        return p

    def _wait_listed(self, pid: int) -> None:
        from rice_switcher.process import read_cmdline
        for _ in range(400):
            if read_cmdline(pid):
                return
            time.sleep(0.01)
        self.fail(f"pid {pid} never appeared in /proc")

    # --- the test ---------------------------------------------------------

    def test_clone_generate_validate_switch_end_to_end(self):
        src = self._make_source_repo()

        # 1. clone (local file:// "remote").
        rc, out, err = self.run_cli(
            "clone", f"file://{src}", "--name", "cloned",
            "--allow-local", "--yes",
        )
        self.assertEqual(rc, 0, err)
        cloned = os.path.join(self.rice_home, "cloned")
        self.assertTrue(
            os.path.isfile(os.path.join(cloned, ".config", "hypr", "hyprland.conf")))

        # 2. generate --write (draft + layout normalization).
        rc, out, err = self.run_cli("generate", "cloned", "--write")
        self.assertEqual(rc, 0, err)
        manifest_path = os.path.join(cloned, "rice.toml")
        self.assertTrue(os.path.isfile(manifest_path))
        with open(manifest_path) as f:
            data = tomllib.loads(f.read())
        comps = {c["name"]: c for c in data["components"]}
        self.assertEqual(comps["hyprland"]["type"], "compositor")
        self.assertEqual(comps["waybar"]["type"], "bar")
        self.assertTrue(comps["waybar"]["exact_argv"])
        self.assertEqual(comps["waybar"]["stop"], "")

        # mismatch #6: layout normalization created <rice>/<x> -> .config/<x>.
        for name in ("hypr", "waybar"):
            link = os.path.join(cloned, name)
            self.assertTrue(os.path.islink(link), f"{link} is not a symlink")
            self.assertEqual(os.readlink(link), f".config/{name}")

        # 3. validate.
        rc, out, err = self.run_cli("validate", "cloned")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("0 error(s)", out + err)

        # 4. switch --dry-run through the CLI.
        rc, out, err = self.run_cli("switch", "cloned", "--dry-run")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("hypr", out)

        # 5. real switch with a stub Runtime.
        stub = self._stub_binary()
        stub_base = os.path.basename(stub)
        base_dir = os.path.join(self.rice_home, "base")
        os.makedirs(base_dir, exist_ok=True)
        with open(os.path.join(base_dir, "rice.toml"), "w") as f:
            f.write(_BASE_TOML.replace("STUB_BASENAME", stub_base))

        victim = self._spawn([stub])          # no arguments -> the identity
        bystander = self._spawn([stub, "-"])  # same basename, has an argument
        self._wait_listed(victim.pid)
        self._wait_listed(bystander.pid)

        with mock.patch.dict(os.environ, self.env, clear=False):
            base_m = load_manifest(os.path.join(base_dir, "rice.toml"))
            cloned_m = load_manifest(manifest_path)
            write_state(self.roots, State(current_rice="base"))
            rt = _StubRuntime()
            outcome = do_switch(
                self.roots, "cloned", [base_m, cloned_m],
                dry_run=False, manage_processes=True, runtime=rt,
            )

        self.assertEqual(outcome.errors, [], outcome.errors)
        self.assertFalse(outcome.dry_run)

        # Langkah 1: the no-arg daemon was stopped by exact identity ...
        self.assertIn(Identity(stub_base, (), True), outcome.plan.to_stop)
        victim.wait(timeout=5)
        self.assertIsNotNone(victim.poll(), "the no-arg process was not stopped")
        time.sleep(0.2)
        self.assertIsNone(bystander.poll(),
                          "an argument-carrying process was wrongly killed")

        # mismatch #6: the live links resolve to the rice's .config trees.
        for name in ("hypr", "waybar"):
            live = os.path.join(self.config_home, name)
            rice_target = os.path.join(cloned, ".config", name)
            self.assertTrue(os.path.islink(live), f"{live} is not a symlink")
            self.assertEqual(os.path.realpath(live), os.path.realpath(rice_target))
            self.assertTrue(os.path.isdir(live))

        # state advanced to the cloned rice.
        from rice_switcher.state import read_state
        self.assertEqual(read_state(self.roots).current_rice, "cloned")


if __name__ == "__main__":
    unittest.main()
