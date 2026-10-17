"""Fase 1 tests: hardened clone + naming (DESIGN-onboarding-tui.md §3, §9, §10).

Hermetic: every clone uses an injected runner or, for the single
integration test, a real `git` against a LOCAL repo created in a tmp dir
(never the network, never ~/.config or ~/.local). All writes land under a
throwaway base directory (RICE_HOME / state / a watched empty config dir).
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
REPO_ROOT = THIS_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from rice_switcher.clone import (
    derive_name,
    do_clone,
    redact_url,
    scan_escaping_symlinks,
    validate_branch,
    validate_depth,
    validate_url,
)
from rice_switcher.executor import ExecResult
from rice_switcher.log import read_audit_log
from rice_switcher.paths import Roots, validate_rice_name


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _roots(base: str) -> Roots:
    r = Roots(
        rice_home=os.path.join(base, "rice"),
        config_home=os.path.join(base, "config"),
        state_home=os.path.join(base, "state"),
    )
    for d in (r.rice_home, r.config_home, r.state_home):
        os.makedirs(d, exist_ok=True)
    return r


class FakeGit:
    """Injected runner. Scripts clone success/failure and rev-parse."""

    def __init__(self, *, clone_rc=0, stderr="", rev="deadbeef", on_clone=None):
        self.calls: list[tuple[list[str], dict]] = []
        self.clone_rc = clone_rc
        self.stderr = stderr
        self.rev = rev
        self.on_clone = on_clone

    def __call__(self, argv, env, timeout):
        argv = list(argv)
        self.calls.append((argv, dict(env)))
        if len(argv) >= 2 and argv[1] == "clone":
            if self.clone_rc == 0:
                tgt = argv[-1]
                os.makedirs(tgt, exist_ok=True)
                os.makedirs(os.path.join(tgt, ".git", "info"), exist_ok=True)
                if self.on_clone:
                    self.on_clone(tgt)
                return ExecResult(tuple(argv), 0, "", "")
            return ExecResult(tuple(argv), self.clone_rc, "", self.stderr)
        if "rev-parse" in argv:
            return ExecResult(tuple(argv), 0, self.rev + "\n", "")
        return ExecResult(tuple(argv), 0, "", "")

    def clone_call(self):
        return next((c for c in self.calls
                     if len(c[0]) >= 2 and c[0][1] == "clone"), None)


# ---------------------------------------------------------------------------
# derive_name
# ---------------------------------------------------------------------------

class DeriveNameTest(unittest.TestCase):
    def test_basic_https(self):
        self.assertEqual(derive_name("https://github.com/foo/bar"), "bar")

    def test_dot_git_suffix(self):
        self.assertEqual(derive_name("https://github.com/foo/bar.git"), "bar")

    def test_trailing_slash(self):
        self.assertEqual(derive_name("https://github.com/foo/bar/"), "bar")

    def test_query_string(self):
        self.assertEqual(derive_name("https://github.com/foo/bar.git?ref=main"), "bar")

    def test_fragment(self):
        self.assertEqual(derive_name("https://github.com/foo/bar#readme"), "bar")

    def test_scp_like(self):
        self.assertEqual(derive_name("git@github.com:foo/bar.git"), "bar")

    def test_ssh_scheme(self):
        self.assertEqual(derive_name("ssh://git@host/foo/bar.git"), "bar")

    def test_leading_dot_stripped(self):
        self.assertEqual(derive_name("https://github.com/foo/.config"), "config")

    def test_weird_chars_sanitised(self):
        self.assertEqual(derive_name("https://host/foo/b@r!!!"), "br")

    def test_fallback_on_empty(self):
        self.assertEqual(derive_name("https://host/"), "rice")
        self.assertEqual(derive_name(""), "rice")
        self.assertEqual(derive_name("https://host/@@@"), "rice")

    def test_too_long_truncated_to_64(self):
        name = derive_name("https://host/" + "a" * 80)
        self.assertEqual(len(name), 64)
        self.assertTrue(name.startswith("aaaa"))

    def test_result_always_valid_rice_name(self):
        for url in ("https://host/.foo", "git@h:p/.bar.git", "https://host/ok"):
            name = derive_name(url)
            ok, err = validate_rice_name(name)
            self.assertTrue(ok, f"{url} -> {name!r}: {err}")


# ---------------------------------------------------------------------------
# validate_url
# ---------------------------------------------------------------------------

class ValidateUrlTest(unittest.TestCase):
    def test_https_ok(self):
        c = validate_url("https://github.com/foo/bar.git")
        self.assertTrue(c.ok)
        self.assertFalse(c.needs_confirm)
        self.assertEqual(c.protocol, "https")

    def test_ssh_ok(self):
        self.assertTrue(validate_url("ssh://git@host/foo/bar.git").ok)
        self.assertTrue(validate_url("git@host:foo/bar.git").ok)

    def test_leading_dash_rejected(self):
        self.assertFalse(validate_url("--upload-pack=evil").ok)

    def test_newline_rejected(self):
        self.assertFalse(validate_url("https://host/x\n").ok)

    def test_control_char_rejected(self):
        self.assertFalse(validate_url("https://host/x\x00").ok)

    def test_ext_rejected(self):
        self.assertFalse(validate_url("ext::sh -c whoami").ok)

    def test_unknown_scheme_rejected(self):
        self.assertFalse(validate_url("ftp://host/x").ok)

    def test_file_requires_allow_local(self):
        self.assertFalse(validate_url("file:///tmp/x").ok)
        self.assertTrue(validate_url("file:///tmp/x", allow_local=True).ok)

    def test_local_path_requires_allow_local(self):
        self.assertFalse(validate_url("/tmp/repo").ok)
        self.assertTrue(validate_url("/tmp/repo", allow_local=True).ok)

    def test_http_warns_and_needs_confirm(self):
        c = validate_url("http://host/x")
        self.assertTrue(c.ok)
        self.assertTrue(c.needs_confirm)
        self.assertTrue(c.warning)

    def test_git_scheme_warns_and_needs_confirm(self):
        c = validate_url("git://host/x")
        self.assertTrue(c.ok)
        self.assertTrue(c.needs_confirm)


class BranchDepthTest(unittest.TestCase):
    def test_branch_leading_dash_rejected(self):
        self.assertFalse(validate_branch("-x")[0])

    def test_branch_ok(self):
        self.assertTrue(validate_branch("main")[0])

    def test_depth_must_be_positive_int(self):
        for bad in ("0", "-1", "abc", "1.5", ""):
            self.assertFalse(validate_depth(bad)[0], bad)
        self.assertTrue(validate_depth("5")[0])


# ---------------------------------------------------------------------------
# rice name: leading dot
# ---------------------------------------------------------------------------

class RiceNameLeadingDotTest(unittest.TestCase):
    def test_leading_dot_rejected(self):
        ok, err = validate_rice_name(".foo")
        self.assertFalse(ok)
        self.assertTrue(err)

    def test_dot_and_dotdot_still_rejected(self):
        self.assertFalse(validate_rice_name(".")[0])
        self.assertFalse(validate_rice_name("..")[0])

    def test_inner_dot_allowed(self):
        self.assertTrue(validate_rice_name("my.rice")[0])


# ---------------------------------------------------------------------------
# redact_url
# ---------------------------------------------------------------------------

class RedactUrlTest(unittest.TestCase):
    def test_http_userinfo_stripped(self):
        self.assertEqual(redact_url("https://user:token@host/r.git"),
                         "https://host/r.git")

    def test_ssh_userinfo_stripped(self):
        self.assertEqual(redact_url("ssh://git@host/r.git"), "ssh://host/r.git")

    def test_scp_left_alone(self):
        self.assertEqual(redact_url("git@host:path/repo.git"), "git@host:path/repo.git")

    def test_query_and_fragment_dropped(self):
        self.assertEqual(redact_url("https://host/r.git?token=abc#x"),
                         "https://host/r.git")


# ---------------------------------------------------------------------------
# do_clone (fake runner)
# ---------------------------------------------------------------------------

class DoCloneTest(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="rs-clone-")
        self.roots = _roots(self.base)

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def test_argv_and_env(self):
        fake = FakeGit()
        res = do_clone("https://host/r.git", "bar", self.roots,
                       runner=fake, depth=3, branch="main")
        self.assertTrue(res.ok, res.error)
        argv, env = fake.clone_call()
        self.assertEqual(
            argv,
            ["git", "clone", "--depth", "3", "--branch", "main", "--",
             "https://host/r.git",
             os.path.join(self.roots.rice_home, f".bar.tmp-{os.getpid()}")],
        )
        i = argv.index("--")
        self.assertEqual(argv[i + 1], "https://host/r.git")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GIT_ASKPASS"], "echo")
        self.assertIn("https", env["GIT_ALLOW_PROTOCOL"])
        self.assertNotIn("--recurse-submodules", argv)

    def test_name_already_exists_does_not_overwrite(self):
        os.makedirs(os.path.join(self.roots.rice_home, "bar"))
        fake = FakeGit()
        res = do_clone("https://host/r.git", "bar", self.roots,
                       runner=fake, depth=None, branch=None)
        self.assertFalse(res.ok)
        self.assertEqual(fake.calls, [])

    def test_invalid_url_no_runner_call(self):
        fake = FakeGit()
        res = do_clone("--upload-pack=evil", "bar", self.roots,
                       runner=fake, depth=None, branch=None)
        self.assertFalse(res.ok)
        self.assertEqual(fake.calls, [])

    def test_leading_dot_name_rejected(self):
        fake = FakeGit()
        res = do_clone("https://host/r.git", ".bar", self.roots,
                       runner=fake, depth=None, branch=None)
        self.assertFalse(res.ok)
        self.assertEqual(fake.calls, [])

    def test_clone_failure_cleans_temp(self):
        tmp_path = os.path.join(self.roots.rice_home, f".bar.tmp-{os.getpid()}")
        os.makedirs(tmp_path, exist_ok=True)
        fake = FakeGit(clone_rc=1, stderr="boom")
        res = do_clone("https://host/r.git", "bar", self.roots,
                       runner=fake, depth=None, branch=None)
        self.assertFalse(res.ok)
        self.assertFalse(os.path.exists(tmp_path))
        self.assertFalse(os.path.exists(os.path.join(self.roots.rice_home, "bar")))

    def test_git_missing_reports_127(self):
        fake = FakeGit(clone_rc=127, stderr="git: not found")
        res = do_clone("https://host/r.git", "bar", self.roots,
                       runner=fake, depth=None, branch=None)
        self.assertFalse(res.ok)
        self.assertEqual(res.rc, 127)

    def test_cleanup_does_not_follow_symlink(self):
        outside = os.path.join(self.base, "outside")
        os.makedirs(outside)
        keep = os.path.join(outside, "keep")
        with open(keep, "w") as f:
            f.write("x")
        tmp_path = os.path.join(self.roots.rice_home, f".bar.tmp-{os.getpid()}")
        os.makedirs(tmp_path, exist_ok=True)
        link = os.path.join(tmp_path, "link")
        os.symlink(outside, link)
        fake = FakeGit(clone_rc=1)
        do_clone("https://host/r.git", "bar", self.roots,
                 runner=fake, depth=None, branch=None)
        self.assertFalse(os.path.exists(tmp_path))
        self.assertTrue(os.path.exists(keep), "cleanup followed a symlink")

    def test_token_never_leaks(self):
        fake = FakeGit(rev="abc123")
        url = "https://user:s3cr3t-token@host/r.git"
        res = do_clone(url, "bar", self.roots, runner=fake,
                       depth=None, branch=None, now="2026-01-01T00:00:00")
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.redacted_url, "https://host/r.git")
        prov_path = os.path.join(self.roots.rice_home, "bar", ".rice-source.toml")
        with open(prov_path, encoding="utf-8") as f:
            prov = f.read()
        self.assertIn('source_url = "https://host/r.git"', prov)
        self.assertIn('cloned_rev = "abc123"', prov)
        self.assertNotIn("s3cr3t-token", prov)
        self.assertNotIn("user:", prov)
        audit_path = os.path.join(self.roots.state_home, "rice-switcher",
                                  "logs", "audit.log")
        with open(audit_path, encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn("s3cr3t-token", raw)
        entries, _ = read_audit_log(self.roots)
        self.assertTrue(any(e.get("action") == "clone" for e in entries))

    def test_provenance_registered_in_git_exclude(self):
        fake = FakeGit()
        do_clone("https://host/r.git", "bar", self.roots, runner=fake,
                 depth=None, branch=None)
        exclude_path = os.path.join(self.roots.rice_home, "bar",
                                    ".git", "info", "exclude")
        with open(exclude_path, encoding="utf-8") as f:
            exclude = f.read()
        self.assertIn(".rice-source.toml", exclude)

    def test_escaping_symlink_warns_and_is_kept(self):
        outside = os.path.join(self.base, "outside")
        os.makedirs(outside)

        def on_clone(tgt):
            os.symlink(outside, os.path.join(tgt, "evil"))

        fake = FakeGit(on_clone=on_clone)
        res = do_clone("https://host/r.git", "bar", self.roots, runner=fake,
                       depth=None, branch=None)
        self.assertTrue(res.ok, res.error)
        self.assertTrue(any("evil" in w for w in res.warnings), res.warnings)
        self.assertTrue(os.path.islink(
            os.path.join(self.roots.rice_home, "bar", "evil")))


class ScanEscapingSymlinksTest(unittest.TestCase):
    def test_detects_escape_and_keeps_safe(self):
        base = tempfile.mkdtemp(prefix="rs-scan-")
        try:
            rice = os.path.join(base, "rice")
            outside = os.path.join(base, "outside")
            os.makedirs(rice)
            os.makedirs(outside)
            os.makedirs(os.path.join(rice, "sub"))
            with open(os.path.join(rice, "inside.txt"), "w") as f:
                f.write("x")
            os.symlink(outside, os.path.join(rice, "escape"))
            os.symlink(os.path.join(rice, "inside.txt"),
                       os.path.join(rice, "sub", "ok"))
            found = scan_escaping_symlinks(rice)
            self.assertEqual(found, ["escape"])
        finally:
            shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------------------
# Integration: real git against a local repo (no network)
# ---------------------------------------------------------------------------

@unittest.skipUnless(shutil.which("git"), "git required")
class DoCloneIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="rs-int-")
        self.roots = _roots(self.base)

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def _make_repo(self, path, *, escaping_symlink=False):
        os.makedirs(path)
        subprocess.run(["git", "-c", "init.defaultBranch=main", "init", "-q", path], check=True)
        with open(os.path.join(path, "rice.toml"), "w") as f:
            f.write('[meta]\nname = "x"\ndescription = "y"\n')
        if escaping_symlink:
            outside = os.path.join(self.base, "secret")
            os.makedirs(outside, exist_ok=True)
            with open(os.path.join(outside, "k"), "w") as f:
                f.write("secret")
            os.symlink(outside, os.path.join(path, "escape"))
        subprocess.run(["git", "-C", path, "-c", "user.email=t@e",
                        "-c", "user.name=t", "add", "-A"], check=True)
        subprocess.run(["git", "-C", path, "-c", "user.email=t@e",
                        "-c", "user.name=t", "commit", "-qm", "init"], check=True)

    def test_local_repo_clone_with_allow_local(self):
        src = os.path.join(self.base, "src")
        self._make_repo(src)
        res = do_clone(src, "localrice", self.roots, allow_local=True,
                       depth=None, branch=None)
        self.assertTrue(res.ok, res.error)
        rice = os.path.join(self.roots.rice_home, "localrice")
        self.assertTrue(os.path.isfile(os.path.join(rice, "rice.toml")))
        self.assertTrue(os.path.isfile(os.path.join(rice, ".rice-source.toml")))
        # nothing written to config_home
        self.assertEqual(os.listdir(self.roots.config_home), [])

    def test_local_path_blocked_without_allow_local(self):
        src = os.path.join(self.base, "src")
        self._make_repo(src)
        res = do_clone(src, "localrice", self.roots, allow_local=False,
                       depth=None, branch=None)
        self.assertFalse(res.ok)
        self.assertFalse(os.path.exists(
            os.path.join(self.roots.rice_home, "localrice")))

    def test_escaping_symlink_warned_after_real_clone(self):
        src = os.path.join(self.base, "src")
        self._make_repo(src, escaping_symlink=True)
        res = do_clone(src, "localrice", self.roots, allow_local=True,
                       depth=None, branch=None)
        self.assertTrue(res.ok, res.error)
        self.assertTrue(any("escape" in w for w in res.warnings), res.warnings)


if __name__ == "__main__":
    unittest.main()
