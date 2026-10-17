"""Hardened clone of a third-party rice repo (DESIGN-onboarding-tui.md §3, §9, §10).

Fase 1 scope: **clone + naming only**. No `rice.toml` is generated and
no state is touched. Every write lands under `roots.rice_home`; the
clone target is a hidden temp dir `.<name>.tmp-<pid>` that is
`os.rename`d into place only after the clone and the provenance write
succeed.

Security invariants
-------------------
* **Argument injection** — a URL, branch, or name starting with `-` is
  refused, `git clone` is invoked with `--` before the URL, and the URL
  is passed as a single argv element (never through a shell).
* **Protocol confining** — `GIT_ALLOW_PROTOCOL` is set to the one
  protocol the validated URL scheme maps to, so a redirect or a smuggled
  scheme cannot reach an unlisted transport. (Verified against
  `git help git` and `git help config`: anything not listed is denied,
  and a local path or `file://` URL uses the `file` protocol.)
* **Credentials** — `GIT_TERMINAL_PROMPT=0` and `GIT_ASKPASS=echo` stop
  git from ever prompting. URLs are redacted before being printed,
  audited, or written to provenance; git stderr is scrubbed of
  `scheme://user:pass@` before it is surfaced.
* **Symlink containment** — the post-clone scan reports symlinks whose
  `realpath` escapes the rice dir without following or deleting them.
  Temp cleanup uses `lstat` + `unlink` for symlinks and never follows a
  link target.
"""

from __future__ import annotations

import os
import re
import shutil
import stat as stat_mod
import subprocess
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable, Mapping, Sequence

from .executor import ExecResult
from .log import append_audit
from .paths import Roots, validate_rice_name

# A clone can be slow (a full rice repo over the network); unlike a
# component command it is not bounded by the 10s default.
DEFAULT_CLONE_TIMEOUT = 300.0

# Secure defaults. `http`/`git` are permitted only with a warning and an
# explicit confirmation; `file` only with `--allow-local`.
_SECURE_PROTOCOLS = ("https", "ssh")
_INSECURE_PROTOCOLS = ("http", "git")

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
# scp-like syntax: user@host:path (no scheme, no slash before the colon).
_SCP_RE = re.compile(r"^[A-Za-z0-9._-]+@[A-Za-z0-9._-]+:.+$")
_SANITISE_RE = re.compile(r"[^A-Za-z0-9._-]")
# `scheme://user:pass@host` -> `scheme://host` (used to scrub git stderr).
_CRED_RE = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)[^/@\s]*@")


# A clone runner receives (argv, env, timeout) and returns an ExecResult.
# Unlike `executor.Runner` it carries an explicit environment, because
# git needs `GIT_TERMINAL_PROMPT` / `GIT_ALLOW_PROTOCOL` to be confined.
GitRunner = Callable[[Sequence[str], Mapping[str, str], float], ExecResult]


def _decode(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def default_git_runner(
    argv: Sequence[str],
    env: Mapping[str, str],
    timeout: float = DEFAULT_CLONE_TIMEOUT,
) -> ExecResult:
    """Run `argv` with `subprocess.run` and an explicit environment."""
    argv = list(argv)
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=dict(env),
        )
        return ExecResult(tuple(argv), proc.returncode, proc.stdout or "", proc.stderr or "")
    except subprocess.TimeoutExpired as e:
        return ExecResult(tuple(argv), -1, _decode(e.stdout), _decode(e.stderr), timed_out=True)
    except FileNotFoundError as e:
        return ExecResult(tuple(argv), 127, "", str(e))
    except OSError as e:
        return ExecResult(tuple(argv), 126, "", str(e))


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------

def derive_name(url: str) -> str:
    """Derive a safe rice name from a clone URL.

    Drops the query/fragment and any trailing `/`, takes the last path
    segment, strips a `.git` suffix and leading dots, then sanitises to
    `[A-Za-z0-9._-]` (max 64 chars). Falls back to `"rice"` when nothing
    usable remains.
    """
    if not isinstance(url, str):
        return "rice"
    s = url.strip()
    for sep in ("#", "?"):
        if sep in s:
            s = s.split(sep, 1)[0]
    s = s.rstrip("/")
    if not s:
        return "rice"

    if "://" in s:
        rest = s.split("://", 1)[1]
        # Only the path after the host carries the repo name; a bare
        # host (or an empty path) has nothing to derive from.
        if "/" not in rest:
            return "rice"
        path = rest.split("/", 1)[1]
    elif _SCP_RE.match(s):
        path = s.split(":", 1)[1]
    else:
        path = s

    path = path.rstrip("/")
    if not path:
        return "rice"
    tail = path.rsplit("/", 1)[-1]
    if tail.endswith(".git"):
        tail = tail[:-4]
    tail = tail.lstrip(".")
    name = _SANITISE_RE.sub("", tail)[:64].lstrip(".")
    if not name or name in (".", ".."):
        return "rice"
    return name


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UrlCheck:
    ok: bool
    error: str = ""
    warning: str = ""
    needs_confirm: bool = False
    protocol: str = ""  # git protocol name for GIT_ALLOW_PROTOCOL


def validate_url(url: str, *, allow_local: bool = False) -> UrlCheck:
    """Validate a clone URL and classify its transport.

    Secure schemes (`https`, `ssh`, scp-like `user@host:path`) are
    accepted silently. `http`/`git` are accepted but flagged
    `needs_confirm`. `file` and bare local paths require `allow_local`.
    Anything else (including `ext::`) is refused.
    """
    if not isinstance(url, str) or not url:
        return UrlCheck(False, "empty URL")
    if url.startswith("-"):
        return UrlCheck(False, "URL must not start with '-'")
    if _CONTROL_RE.search(url):
        return UrlCheck(False, "URL contains control characters")

    low = url.lower()
    if low.startswith("ext:") or low.startswith("ext::"):
        return UrlCheck(False, "ext:: transport is not allowed")

    if "://" in url:
        scheme = low.split("://", 1)[0]
        if scheme == "https":
            return UrlCheck(True, protocol="https")
        if scheme == "ssh":
            return UrlCheck(True, protocol="ssh")
        if scheme == "http":
            return UrlCheck(
                True,
                warning="insecure protocol http:// (credentials and data travel in clear)",
                needs_confirm=True,
                protocol="http",
            )
        if scheme == "git":
            return UrlCheck(
                True,
                warning="unauthenticated protocol git:// (no encryption, no integrity)",
                needs_confirm=True,
                protocol="git",
            )
        if scheme == "file":
            if allow_local:
                return UrlCheck(True, protocol="file")
            return UrlCheck(False, "file:// URLs require --allow-local")
        return UrlCheck(False, f"unsupported URL scheme {scheme!r}")

    if _SCP_RE.match(url):
        return UrlCheck(True, protocol="ssh")

    # No scheme and not scp-like: treat as a local path.
    if allow_local:
        return UrlCheck(True, protocol="file")
    return UrlCheck(False, "local paths and file:// URLs require --allow-local")


def validate_branch(branch) -> tuple[bool, str]:
    """Reject a branch that could be read as a git option."""
    if branch is None:
        return True, ""
    if not isinstance(branch, str) or not branch:
        return False, "branch must be a non-empty string"
    if branch.startswith("-"):
        return False, "branch must not start with '-'"
    if _CONTROL_RE.search(branch):
        return False, "branch contains control characters"
    return True, ""


def validate_depth(depth) -> tuple[bool, str]:
    """Reject a depth that is not a positive integer."""
    if depth is None:
        return True, ""
    s = str(depth).strip()
    if not re.fullmatch(r"[0-9]+", s) or int(s) < 1:
        return False, f"depth must be a positive integer (got {depth!r})"
    return True, ""


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

def _redact_fallback(url: str) -> str:
    return _CRED_RE.sub(r"\g<scheme>", url)


def redact_url(url: str) -> str:
    """Return a URL safe to print, audit, or store.

    Strips the userinfo (`user:pass@`) from `scheme://` URLs and drops
    the query/fragment (they may carry tokens). scp-like `user@host:path`
    and bare local paths have no userinfo concept and are returned
    unchanged.
    """
    if not isinstance(url, str):
        return ""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return _redact_fallback(url)
    if not parts.netloc:
        return url
    netloc = parts.netloc.rsplit("@", 1)[1] if "@" in parts.netloc else parts.netloc
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _scrub_stderr(text: str) -> str:
    """Collapse whitespace and strip `scheme://user:pass@` from git output."""
    if not text:
        return ""
    text = " ".join(text.split())
    text = _CRED_RE.sub(r"\g<scheme>", text)
    if len(text) > 500:
        text = text[:500] + "…"
    return text


def _allow_protocol_env(protocol: str) -> str:
    """The tightest `GIT_ALLOW_PROTOCOL` for a validated URL.

    `http` may legitimately redirect up to `https`; every other scheme is
    confined to exactly itself.
    """
    if protocol == "http":
        return "http:https"
    return protocol or "https"


# ---------------------------------------------------------------------------
# Symlink scan
# ---------------------------------------------------------------------------

def scan_escaping_symlinks(root: str) -> list[str]:
    """Return rice-relative paths of symlinks whose realpath leaves `root`.

    Symlinks are never followed (neither by the walk nor by this scan);
    the `.git` metadata directory is skipped. The result is sorted for
    determinism.
    """
    root_real = os.path.realpath(root)
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        if os.path.basename(dirpath) == ".git":
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in dirnames + filenames:
            p = os.path.join(dirpath, name)
            if not os.path.islink(p):
                continue
            target = os.path.realpath(p)
            try:
                inside = os.path.commonpath([root_real, target]) == root_real
            except ValueError:
                inside = False
            if not inside:
                found.append(os.path.relpath(p, root))
    return sorted(found)


# ---------------------------------------------------------------------------
# Temp cleanup (no-follow)
# ---------------------------------------------------------------------------

def _safe_remove(path: str) -> None:
    """Remove `path` without ever following a symlink.

    A symlink (or a plain file) is `unlink`ed via `lstat`; only a real
    directory is handed to `shutil.rmtree` (which itself does not follow
    symlinked subdirectories).
    """
    try:
        st = os.lstat(path)
    except OSError:
        return
    if stat_mod.S_ISDIR(st.st_mode) and not stat_mod.S_ISLNK(st.st_mode):
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------

def _toml_string(s: str) -> str:
    """Minimal TOML basic-string serialiser (Fase 2 gets a full writer)."""
    out = (
        str(s)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{out}"'


def _write_provenance(rice_dir: str, redacted_url: str, rev: str, now: str | None) -> None:
    ts = now if now is not None else time.strftime("%Y-%m-%dT%H:%M:%S%z")
    body = (
        "# rice-switcher clone provenance — generated, do not edit.\n"
        "# The URL is redacted at write time; no credentials are stored here.\n"
        f"source_url = {_toml_string(redacted_url)}\n"
        f"cloned_at = {_toml_string(ts)}\n"
        f"cloned_rev = {_toml_string(rev)}\n"
    )
    with open(os.path.join(rice_dir, ".rice-source.toml"), "w", encoding="utf-8") as f:
        f.write(body)


def _register_git_exclude(rice_dir: str) -> None:
    """Add `.rice-source.toml` to the clone's `.git/info/exclude`.

    `info/exclude` is local to the clone and never committed, so this
    needs no change to the repo's own `.gitignore`.
    """
    info = os.path.join(rice_dir, ".git", "info")
    if not os.path.isdir(info):
        return
    path = os.path.join(info, "exclude")
    existing = ""
    if os.path.isfile(path):
        with open(path, encoding="utf-8", errors="replace") as f:
            existing = f.read()
    if ".rice-source.toml" in existing.split():
        return
    with open(path, "a", encoding="utf-8") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write("# added by rice-switcher clone\n.rice-source.toml\n")


def _rev_parse(run: GitRunner, tmp: str, env: Mapping[str, str], timeout: float) -> str:
    try:
        res = run(["git", "-C", tmp, "rev-parse", "HEAD"], env, timeout)
    except Exception:
        return ""
    if not res.ok:
        return ""
    return (res.stdout or "").strip()


def _audit(roots: Roots, redacted_url: str, name: str, rc: int) -> None:
    try:
        append_audit(roots, {"action": "clone", "url": redacted_url, "name": name, "rc": rc})
    except Exception:
        # Auditing must never turn a successful clone into a failure.
        pass


# ---------------------------------------------------------------------------
# Clone
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CloneResult:
    ok: bool
    rc: int
    name: str
    path: str = ""
    redacted_url: str = ""
    warnings: tuple[str, ...] = field(default_factory=tuple)
    error: str = ""


def do_clone(
    url: str,
    name: str,
    roots: Roots,
    *,
    runner: GitRunner | None = None,
    depth=None,
    branch=None,
    allow_local: bool = False,
    timeout: float = DEFAULT_CLONE_TIMEOUT,
    now: str | None = None,
) -> CloneResult:
    """Clone `url` into `<rice_home>/<name>`.

    Returns a `CloneResult`. On any failure the temp dir is removed
    (no-follow) and nothing is left under rice_home. A pre-existing
    `<name>` is never overwritten.
    """
    red = redact_url(url) if isinstance(url, str) else ""

    check = validate_url(url, allow_local=allow_local)
    if not check.ok:
        return CloneResult(False, 2, name, redacted_url=red, error=check.error)

    ok, err = validate_branch(branch)
    if not ok:
        return CloneResult(False, 2, name, redacted_url=red, error=err)

    ok, err = validate_depth(depth)
    if not ok:
        return CloneResult(False, 2, name, redacted_url=red, error=err)

    ok, err = validate_rice_name(name)
    if not ok:
        return CloneResult(False, 2, name, redacted_url=red, error=err)

    os.makedirs(roots.rice_home, exist_ok=True)
    final = os.path.join(roots.rice_home, name)
    if os.path.lexists(final):
        return CloneResult(
            False, 2, name, path=final, redacted_url=red,
            error=f"a rice named {name!r} already exists at {final}",
        )

    tmp = os.path.join(roots.rice_home, f".{name}.tmp-{os.getpid()}")
    _safe_remove(tmp)  # drop any stale temp left by a previous crash

    argv = ["git", "clone"]
    if depth is not None:
        argv += ["--depth", str(depth)]
    if branch is not None:
        argv += ["--branch", str(branch)]
    argv += ["--", url, tmp]

    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "echo"
    env["GIT_ALLOW_PROTOCOL"] = _allow_protocol_env(check.protocol)

    run = runner or default_git_runner
    try:
        res = run(argv, env, timeout)
    except Exception as e:  # the injected runner itself blew up
        _safe_remove(tmp)
        _audit(roots, red, name, 1)
        return CloneResult(False, 1, name, redacted_url=red,
                           error=f"git invocation failed: {e}")

    if not res.ok:
        _safe_remove(tmp)
        rc = res.rc
        if rc == 127:
            msg = "git is not installed (command not found)"
        elif res.timed_out:
            msg = "git clone timed out"
        else:
            msg = _scrub_stderr(res.stderr) or f"git clone failed (rc={rc})"
        _audit(roots, red, name, rc)
        return CloneResult(False, rc, name, redacted_url=red, error=msg)

    warnings: list[str] = []
    try:
        rev = _rev_parse(run, tmp, env, timeout)
        for rel in scan_escaping_symlinks(tmp):
            warnings.append(f"symlink escapes the rice directory (not followed): {rel}")
        _write_provenance(tmp, red, rev, now)
        _register_git_exclude(tmp)
        os.rename(tmp, final)
    except OSError as e:
        _safe_remove(tmp)
        _audit(roots, red, name, 1)
        return CloneResult(False, 1, name, redacted_url=red,
                           error=f"post-clone step failed: {_scrub_stderr(str(e))}")

    _audit(roots, red, name, 0)
    return CloneResult(True, 0, name, path=final, redacted_url=red,
                       warnings=tuple(warnings))
