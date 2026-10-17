"""Prasyarat 0 (Fase 2b Langkah 1): exact no-argument process identity.

`terminate_by_identity` used to refuse an empty `argv_match` outright, so a
daemon that takes no arguments (waybar, hypridle, mako, dunst) could not be
stopped through an empty `stop`. We add an *explicit* declaration,
`exact_argv = true`, that turns the argv match into an exact, order-sensitive
comparison: the process's argv must be the executable plus exactly the
declared tokens — no more and no fewer. An empty `argv_match` with
`exact_argv = true` therefore means "executable with **no** arguments".

Guards that must NOT loosen (PLAN.md §2g, DESIGN §4.2):

  * never basename-only: the full argv must match exactly;
  * `waybar -c x` must not match the `waybar` (no-args) identity, and
    `qs -c ii` must not match `qs -c minimal`;
  * an empty `argv_match` **without** `exact_argv` stays an error;
  * own uid only, never PID 1/self/parent, re-read cmdline before signalling,
    SIGTERM -> wait -> SIGKILL.

The only real processes we touch are our own stubs under /tmp (a copy of a
real binary with a unique name so no unrelated process can match); bystanders
with arguments must survive.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from rice_switcher.lifecycle import Runtime, stop_component
from rice_switcher.managed_set import Identity
from rice_switcher.manifest import Component, parse_manifest
from rice_switcher.process import (
    find_pids_by_identity,
    probe_pids_by_identity,
    read_cmdline,
    terminate_by_identity,
)


# ---------------------------------------------------------------------------
# A real no-argument stub: a copy of a real binary under a unique name.
# ---------------------------------------------------------------------------

class _StubProcs:
    """Spawn real child processes with a unique argv[0] basename."""

    def __init__(self, case: unittest.TestCase):
        self._case = case
        self._dir = tempfile.mkdtemp(prefix="rs-exact-")
        self._procs: list[subprocess.Popen] = []
        case.addCleanup(self._cleanup)

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
        shutil.rmtree(self._dir, ignore_errors=True)

    def stub(self, name: str) -> str:
        src = shutil.which("cat")
        if not src:
            self._case.skipTest("no `cat` binary available to build a no-arg stub")
        dst = os.path.join(self._dir, name)
        shutil.copy2(src, dst)
        os.chmod(dst, 0o755)
        return dst

    def spawn(self, argv: list[str]) -> subprocess.Popen:
        # stdin=PIPE keeps `cat` (argv == [exe]) blocked and alive with no
        # extra argv tokens.
        p = subprocess.Popen(argv, stdin=subprocess.PIPE)
        self._procs.append(p)
        return p

    def wait_tokens(self, pid: int, tokens: tuple[str, ...]) -> None:
        for _ in range(400):
            argv = read_cmdline(pid)
            if argv and tuple(argv[1:]) == tokens:
                return
            time.sleep(0.01)
        self._case.fail(f"pid {pid} never reached argv tokens {tokens!r}")


# ---------------------------------------------------------------------------
# process.py : exact scan semantics
# ---------------------------------------------------------------------------

class ExactScanTest(unittest.TestCase):
    def test_exact_no_args_matches_only_the_noarg_process(self):
        stubs = _StubProcs(self)
        exe = stubs.stub(f"rsxn{os.getpid()}")
        base = os.path.basename(exe)
        noarg = stubs.spawn([exe])
        witharg = stubs.spawn([exe, "-"])
        stubs.wait_tokens(noarg.pid, ())
        stubs.wait_tokens(witharg.pid, ("-",))

        # Exact no-args identity: argv must be exactly [exe].
        self.assertEqual(find_pids_by_identity(base, (), exact=True), [noarg.pid])
        # Exact with-args identity: argv must be exactly [exe, "-"].
        self.assertEqual(
            find_pids_by_identity(base, ("-",), exact=True), [witharg.pid]
        )
        # The no-arg identity must NOT sweep in the argument-carrying one.
        self.assertNotIn(witharg.pid, find_pids_by_identity(base, (), exact=True))

    def test_legacy_subset_match_is_unchanged(self):
        stubs = _StubProcs(self)
        exe = stubs.stub(f"rsxs{os.getpid()}")
        base = os.path.basename(exe)
        noarg = stubs.spawn([exe])
        witharg = stubs.spawn([exe, "-"])
        stubs.wait_tokens(noarg.pid, ())
        stubs.wait_tokens(witharg.pid, ("-",))
        # Without `exact`, the old subset semantics still see both.
        found = find_pids_by_identity(base, ())
        self.assertIn(noarg.pid, found)
        self.assertIn(witharg.pid, found)

    def test_probe_honours_exact(self):
        stubs = _StubProcs(self)
        exe = stubs.stub(f"rsxp{os.getpid()}")
        base = os.path.basename(exe)
        noarg = stubs.spawn([exe])
        witharg = stubs.spawn([exe, "-"])
        stubs.wait_tokens(noarg.pid, ())
        stubs.wait_tokens(witharg.pid, ("-",))
        res = probe_pids_by_identity(base, (), exact=True)
        self.assertEqual(res.status, "running")
        self.assertEqual(res.pids, (noarg.pid,))


# ---------------------------------------------------------------------------
# process.py : terminate_by_identity exact mode
# ---------------------------------------------------------------------------

class _FakeOps:
    def __init__(self, pids):
        self.pids = list(pids)
        self.alive = set(pids)
        self.signals: list[int] = []
        self._t = 0.0
        self.find_args = None
        self.match_args = None

    def find(self, executable, argv_match):
        self.find_args = (executable, tuple(argv_match))
        return list(self.pids)

    def owned(self, pid):
        return True

    def matches(self, pid, executable, argv_match):
        self.match_args = (executable, tuple(argv_match))
        return pid in self.alive

    def kill(self, pid, sig):
        self.signals.append(pid)
        self.alive.discard(pid)

    def sleep(self, seconds):
        self._t += seconds

    def clock(self):
        return self._t


def _terminate_exact(fake, executable, argv_match, **kw):
    return terminate_by_identity(
        executable,
        argv_match,
        exact=True,
        find=fake.find,
        matches=fake.matches,
        owned=fake.owned,
        kill=fake.kill,
        sleeper=fake.sleep,
        clock=fake.clock,
        timeout=kw.pop("timeout", 5.0),
        **kw,
    )


class TerminateExactTest(unittest.TestCase):
    def test_exact_empty_argv_match_is_allowed_and_signals(self):
        fake = _FakeOps([101, 102])
        killed = _terminate_exact(fake, "waybar", ())
        self.assertEqual(sorted(killed), [101, 102])
        self.assertEqual(fake.signals, [101, 102])

    def test_empty_argv_match_without_exact_still_refuses(self):
        # Regression guard: the explicit declaration is the ONLY way to
        # unlock an empty argv_match. A silent `()` must keep erroring.
        with self.assertRaises(ValueError):
            terminate_by_identity("waybar", ())

    def test_exact_with_args_supported(self):
        fake = _FakeOps([7])
        killed = _terminate_exact(fake, "qs", ("-c", "ii"))
        self.assertEqual(killed, [7])
        self.assertEqual(fake.match_args, ("qs", ("-c", "ii")))

    def test_real_exact_noarg_kills_only_the_noarg_process(self):
        stubs = _StubProcs(self)
        exe = stubs.stub(f"rsxk{os.getpid()}")
        base = os.path.basename(exe)
        victim = stubs.spawn([exe])
        bystander = stubs.spawn([exe, "-"])
        stubs.wait_tokens(victim.pid, ())
        stubs.wait_tokens(bystander.pid, ("-",))

        killed = terminate_by_identity(base, (), exact=True, timeout=5.0)
        self.assertIn(victim.pid, killed)
        victim.wait(timeout=5)
        # The bystander, which has an argument, must still be alive.
        time.sleep(0.2)
        self.assertIsNone(bystander.poll(), "argument-carrying bystander was killed")


# ---------------------------------------------------------------------------
# manifest.py : the explicit field
# ---------------------------------------------------------------------------

class ManifestFieldTest(unittest.TestCase):
    def _manifest(self, comp):
        return parse_manifest({"meta": {"name": "r"}, "components": [comp]})

    def test_exact_argv_defaults_false(self):
        m = self._manifest({"name": "waybar", "type": "bar"})
        self.assertFalse(m.components[0].exact_argv)

    def test_exact_argv_true_is_parsed(self):
        m = self._manifest(
            {"name": "waybar", "type": "bar", "exact_argv": True}
        )
        self.assertTrue(m.components[0].exact_argv)


# ---------------------------------------------------------------------------
# lifecycle.py : exact identity reaches kill-by-identity
# ---------------------------------------------------------------------------

class _ExactFakeRt(Runtime):
    def __init__(self):
        self.up: set[str] = set()
        self.terminated: list[tuple[str, tuple[str, ...], bool]] = []
        self._t = 0.0
        from rice_switcher.executor import ExecResult

        super().__init__(
            runner=lambda argv, timeout: ExecResult(tuple(argv), 0, "", ""),
            probe=lambda c: c.name in self.up,
            sleeper=lambda s: setattr(self, "_t", self._t + s),
            clock=lambda: self._t,
            poll_interval=0.1,
        )

    def kill_processes(self, c: Component) -> list[int]:
        self.terminated.append((c.executable, tuple(c.argv_match), bool(c.exact_argv)))
        self.up.discard(c.name)
        return [4242]


class StopExactIdentityTest(unittest.TestCase):
    def test_exact_noarg_component_stops_by_identity(self):
        rt = _ExactFakeRt()
        rt.up.add("waybar")
        stop_component(
            rt,
            Component(
                name="waybar", type="bar", executable="waybar",
                argv_match=[], exact_argv=True, start="waybar", stop="",
                timeout=1,
            ),
        )
        self.assertNotIn("waybar", rt.up)
        self.assertEqual(rt.terminated, [("waybar", (), True)])

    def test_empty_argv_match_without_exact_still_refuses(self):
        from rice_switcher.lifecycle import LifecycleError

        rt = _ExactFakeRt()
        rt.up.add("waybar")
        with self.assertRaises(LifecycleError):
            stop_component(
                rt,
                Component(
                    name="waybar", type="bar", executable="waybar",
                    argv_match=[], start="waybar", stop="", timeout=1,
                ),
            )


# ---------------------------------------------------------------------------
# managed_set.py / switch.py : identity + confirmation display
# ---------------------------------------------------------------------------

class IdentityDisplayTest(unittest.TestCase):
    def test_identity_carries_exact(self):
        c = Component(
            name="waybar", type="bar", executable="waybar",
            argv_match=[], exact_argv=True,
        )
        ident = Identity.from_component(c)
        self.assertTrue(ident.exact)
        self.assertEqual(ident.argv_match, ())

    def test_legacy_identity_str_is_unchanged(self):
        self.assertEqual(str(Identity("qs", ("-c", "ii"))), "qs -c ii")
        self.assertEqual(str(Identity("waybar", ())), "waybar")

    def test_exact_noarg_identity_is_displayed_explicitly(self):
        s = str(Identity("waybar", (), True))
        self.assertIn("waybar", s)
        self.assertIn("no args", s)

    def test_exact_with_args_identity_is_displayed_explicitly(self):
        s = str(Identity("qs", ("-c", "ii"), True))
        self.assertIn("qs -c ii", s)
        self.assertIn("exact", s)

    def test_stop_targets_show_noargs_mode(self):
        from rice_switcher.switch import SwitchPlan, stop_targets

        plan = SwitchPlan(target="b", current="a", to_stop=[Identity("waybar", (), True)])
        self.assertEqual(len(stop_targets(plan)), 1)
        self.assertIn("no args", stop_targets(plan)[0])


if __name__ == "__main__":
    unittest.main()
