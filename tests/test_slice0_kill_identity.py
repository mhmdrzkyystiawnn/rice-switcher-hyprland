"""Prasyarat 0 tests: kill-by-identity (PLAN.md §2g, DESIGN-onboarding-tui.md §4.2).

This is the one piece of code that can actually signal a process, and
the identity (`executable`/`argv_match`) can come from someone else's
`rice.toml`. The guards under test:

  * own uid only; never PID 1, self, or parent;
  * full argv match (never basename alone) -> require non-empty argv_match;
  * re-read /proc/<pid>/cmdline right before kill (PID-reuse guard);
  * SIGTERM -> wait -> SIGKILL;
  * the runtime seam `kill_processes` is used when `stop` is empty;
  * session processes (Hyprland, systemd, dbus-daemon,
    gnome-keyring-daemon, ...) are refused by the validator;
  * the confirmation view lists the processes to be stopped.

Nothing here touches a real desktop process: the signal path is driven
through injected fakes, and the only real process we spawn is our own
`sleep` child (terminated in the test).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from rice_switcher.executor import ExecResult
from rice_switcher.lifecycle import LifecycleError, Runtime, stop_component
from rice_switcher.managed_set import Identity
from rice_switcher.manifest import Component, Manifest, Meta
from rice_switcher.paths import Roots
from rice_switcher.process import (
    is_own_uid,
    pid_matches_identity,
    terminate_by_identity,
)
from rice_switcher.switch import SwitchPlan, stop_targets
from rice_switcher.validate import validate_manifest


def _comp(name: str, **kw) -> Component:
    kw.setdefault("type", "shell")
    kw.setdefault("start", "true")
    kw.setdefault("stop", "true")
    return Component(name=name, **kw)


# ---------------------------------------------------------------------------
# process.py : ownership + identity re-check
# ---------------------------------------------------------------------------

class ProcessOwnershipTest(unittest.TestCase):
    def test_own_pid_is_own_uid(self):
        self.assertTrue(is_own_uid(os.getpid()))

    def test_nonexistent_pid_is_not_own_uid(self):
        self.assertFalse(is_own_uid(99_999_999))


class PidMatchesIdentityTest(unittest.TestCase):
    def test_full_argv_is_compared_not_just_basename(self):
        p = subprocess.Popen(["sleep", "30"])
        try:
            # /proc/<pid>/cmdline is briefly empty between fork and exec;
            # wait until the child has exec'd.
            for _ in range(200):
                if pid_matches_identity(p.pid, "sleep", ("30",)):
                    break
                time.sleep(0.01)
            # basename matches ...
            self.assertTrue(pid_matches_identity(p.pid, "sleep", ()))
            # ... and the argv tokens are honoured: `sleep 30` != `sleep 99`
            self.assertTrue(pid_matches_identity(p.pid, "sleep", ("30",)))
            self.assertFalse(pid_matches_identity(p.pid, "sleep", ("99",)))
            # wrong basename never matches
            self.assertFalse(pid_matches_identity(p.pid, "not-sleep", ()))
        finally:
            p.terminate()
            p.wait()


# ---------------------------------------------------------------------------
# process.py : terminate_by_identity (pure w.r.t. injected ops)
# ---------------------------------------------------------------------------

class _FakeProcOps:
    """Fakes for /proc + os.kill so terminate_by_identity is hermetic."""

    def __init__(self, pids, *, stubborn=(), not_owned=(), mismatch=()):
        self.pids = list(pids)
        self.alive = set(pids)
        self.stubborn = set(stubborn)
        self.not_owned = set(not_owned)
        self.mismatch = set(mismatch)
        self.signals: list[tuple[int, int]] = []
        self._t = 0.0

    def find(self, executable, argv_match):
        return list(self.pids)

    def owned(self, pid):
        return pid not in self.not_owned

    def matches(self, pid, executable, argv_match):
        if pid in self.mismatch:
            return False
        return pid in self.alive

    def kill(self, pid, sig):
        self.signals.append((pid, sig))
        if sig == signal.SIGKILL or pid not in self.stubborn:
            self.alive.discard(pid)

    def sleep(self, seconds):
        self._t += seconds

    def clock(self):
        return self._t


def _terminate(fake, executable="qs", argv_match=("-c", "ii"), **kw):
    return terminate_by_identity(
        executable,
        argv_match,
        find=fake.find,
        matches=fake.matches,
        owned=fake.owned,
        kill=fake.kill,
        sleeper=fake.sleep,
        clock=fake.clock,
        timeout=kw.pop("timeout", 5.0),
        **kw,
    )


class TerminateByIdentityTest(unittest.TestCase):
    def test_requires_argv_match(self):
        # basename-only kill is too broad (`qs -c ii` vs `qs -c minimal`)
        with self.assertRaises(ValueError):
            terminate_by_identity("qs", ())

    def test_sigterm_stops_processes(self):
        fake = _FakeProcOps(pids=[101, 102])
        killed = _terminate(fake)
        self.assertEqual(sorted(killed), [101, 102])
        self.assertEqual(
            fake.signals,
            [(101, signal.SIGTERM), (102, signal.SIGTERM)],
        )

    def test_escalates_to_sigkill_when_stubborn(self):
        fake = _FakeProcOps(pids=[101], stubborn=[101])
        _terminate(fake, timeout=0.5)
        self.assertIn((101, signal.SIGKILL), fake.signals)
        self.assertIn((101, signal.SIGTERM), fake.signals)

    def test_skips_pid_1_self_and_parent(self):
        fake = _FakeProcOps(pids=[1, os.getpid(), os.getppid(), 555])
        killed = _terminate(fake)
        self.assertEqual(killed, [555])

    def test_skips_pids_not_owned_by_us(self):
        fake = _FakeProcOps(pids=[101, 102], not_owned=[101])
        killed = _terminate(fake)
        self.assertEqual(killed, [102])
        self.assertNotIn(101, [p for p, _ in fake.signals])

    def test_pid_reuse_guard_skips_mismatch(self):
        # /proc/<pid>/cmdline no longer matches -> do NOT signal it
        fake = _FakeProcOps(pids=[101], mismatch=[101])
        killed = _terminate(fake)
        self.assertEqual(killed, [])
        self.assertEqual(fake.signals, [])


# ---------------------------------------------------------------------------
# lifecycle.py : stop with empty `stop` uses kill-by-identity
# ---------------------------------------------------------------------------

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


class StopByIdentityTest(unittest.TestCase):
    def test_empty_stop_kills_by_identity(self):
        rt = _FakeRt()
        rt.up.add("quickshell")
        stop_component(
            rt,
            _comp(
                "quickshell",
                stop="",
                executable="qs",
                argv_match=["-c", "ii"],
                timeout=1,
            ),
        )
        self.assertNotIn("quickshell", rt.up)
        self.assertEqual(rt.terminated, [("qs", ("-c", "ii"))])

    def test_empty_stop_without_argv_match_refuses(self):
        rt = _FakeRt()
        rt.up.add("waybar")
        with self.assertRaises(LifecycleError):
            stop_component(
                rt,
                _comp("waybar", stop="", executable="waybar", argv_match=[], timeout=1),
            )

    def test_empty_stop_optional_without_argv_match_is_skipped(self):
        rt = _FakeRt()
        rt.up.add("waybar")
        stop_component(
            rt,
            _comp(
                "waybar",
                stop="",
                executable="waybar",
                argv_match=[],
                timeout=1,
                optional=True,
            ),
        )
        self.assertIn("waybar", rt.up)
        self.assertEqual(rt.terminated, [])

    def test_empty_stop_still_running_times_out(self):
        rt = _FakeRt()
        rt.up.add("quickshell")

        def stays_up(c):
            return True

        rt.probe = stays_up
        with self.assertRaises(LifecycleError):
            stop_component(
                rt,
                _comp(
                    "quickshell",
                    stop="",
                    executable="qs",
                    argv_match=["-c", "ii"],
                    timeout=1,
                ),
            )


# ---------------------------------------------------------------------------
# validate.py : session processes must be refused
# ---------------------------------------------------------------------------

class ProtectedExecutableTest(unittest.TestCase):
    SESSION_PROCESSES = (
        "Hyprland",
        "hyprland",
        "systemd",
        "dbus-daemon",
        "gnome-keyring-daemon",
    )

    def _manifest(self, comps):
        return Manifest(meta=Meta(name="r", description="d"), components=comps)

    def test_session_executable_in_shell_component_is_error(self):
        for exe in self.SESSION_PROCESSES:
            m = self._manifest([_comp("c", executable=exe, start="true", stop="")])
            res = validate_manifest(m, Roots("/r", "/c", "/s"))
            self.assertFalse(res.ok, f"{exe!r} should be rejected")
            self.assertTrue(
                any("protected" in e or "session" in e for e in res.errors),
                f"{exe!r} error should mention session/protected: {res.errors}",
            )

    def test_compositor_component_may_name_hyprland(self):
        c = Component(
            name="hyprland",
            type="compositor",
            executable="hyprland",
            start="true",
            stop="true",
        )
        res = validate_manifest(self._manifest([c]), Roots("/r", "/c", "/s"))
        self.assertTrue(res.ok, res.errors)


# ---------------------------------------------------------------------------
# switch.py : confirmation view lists stop targets
# ---------------------------------------------------------------------------

class StopTargetsTest(unittest.TestCase):
    def test_lists_identity_strings(self):
        plan = SwitchPlan(
            target="b",
            current="a",
            to_stop=[
                Identity.from_component(
                    _comp("quickshell", executable="qs", argv_match=["-c", "ii"])
                )
            ],
        )
        self.assertEqual(stop_targets(plan), ["qs -c ii"])

    def test_empty_when_nothing_to_stop(self):
        self.assertEqual(stop_targets(SwitchPlan(target="b", current="a")), [])


if __name__ == "__main__":
    unittest.main()
