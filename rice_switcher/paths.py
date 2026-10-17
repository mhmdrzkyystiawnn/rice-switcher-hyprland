"""Path roots and override handling.

PLAN.md §2e + §2.0 [run] table. All path operations in the switcher flow
through this module so the test mode can redirect every root to a temp
directory without touching the real filesystem.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Rice name rules (audit fix S1)
# ---------------------------------------------------------------------------
#
# A rice name is a short, filesystem-safe label. The rules are:
#   * non-empty
#   * at most 64 chars
#   * only [A-Za-z0-9._-] (no path separators, no whitespace, no quotes)
#   * not "." or ".."
#   * must not start with "." (hidden dirs collide with clone temp dirs)
#   * not absolute (no leading / or ~)
# These rules are enforced BEFORE any directory or file is created, so
# a rejected name leaves the filesystem untouched. The same rules are
# used by both `cli.cmd_add` and `snapshot.create_pending` (for the
# rice= field in `.captured.txt`).
_RICE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def validate_rice_name(name: str) -> tuple[bool, str]:
    """Return (ok, error_message). On error, error_message is non-empty
    and suitable for printing to stderr."""
    if not isinstance(name, str):
        return False, "rice name must be a string"
    if not name:
        return False, "rice name must not be empty"
    if len(name) > 64:
        return False, f"rice name too long ({len(name)} > 64 chars)"
    if name in (".", ".."):
        return False, f"rice name {name!r} is reserved"
    if name.startswith("."):
        # A leading dot would hide the rice dir and can collide with the
        # clone temp dirs (".<name>.tmp-<pid>"). Reject it outright.
        return False, f"rice name must not start with a dot (got {name!r})"
    if os.path.isabs(name) or name.startswith("~"):
        return False, f"rice name must not be an absolute path (got {name!r})"
    if "/" in name or "\\" in name:
        return False, f"rice name must not contain path separators (got {name!r})"
    if not _RICE_NAME_RE.match(name):
        return False, (
            f"rice name {name!r} contains invalid characters; "
            f"allowed: A-Z a-z 0-9 . _ - (max 64 chars)"
        )
    return True, ""


@dataclass(frozen=True)
class Roots:
    """Resolved filesystem roots. Frozen so they cannot be mutated mid-switch."""

    rice_home: str       # root rice directory (contains <name>/rice.toml)
    config_home: str    # symlink target base (usually ~/.config)
    state_home: str     # state, snapshot, lock, log base (usually ~/.local/state)


def _expand(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def _from_manifest(manifest_run: dict | None) -> dict:
    """Extract path overrides from a parsed manifest's [run] table.

    Returns a dict with the same keys as env vars (lowercase). Each value
    is expanded but not normalised; the caller decides whether to absolutise.
    Missing keys are absent (caller falls back to env/default).
    """
    if not manifest_run:
        return {}
    out = {}
    for k, v in manifest_run.items():
        if isinstance(v, str):
            out[k.lower()] = v
    return out


def resolve_roots(manifest_run: dict | None = None) -> Roots:
    """Resolve roots from (in order of precedence): manifest [run], env, defaults.

    Precedence (highest first):
      1. manifest `[run]` table (per PLAN.md §2e, overrides env for this rice)
      2. env vars: RICE_HOME, RICE_CONFIG_HOME, RICE_STATE_HOME
      3. defaults from XDG vars: $XDG_CONFIG_HOME, $XDG_STATE_HOME
      4. hardcoded fallbacks: ~/rice, ~/.config, ~/.local/state
    """
    overrides = _from_manifest(manifest_run)

    rice_home = overrides.get("rice_home") or os.environ.get("RICE_HOME")
    if not rice_home:
        rice_home = _expand("~/rice")

    config_home = overrides.get("config_home") or os.environ.get("RICE_CONFIG_HOME")
    if not config_home:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        config_home = _expand(xdg) if xdg else _expand("~/.config")

    state_home = overrides.get("state_home") or os.environ.get("RICE_STATE_HOME")
    if not state_home:
        xdg_state = os.environ.get("XDG_STATE_HOME")
        state_home = _expand(xdg_state) if xdg_state else _expand("~/.local/state")

    return Roots(
        rice_home=_expand(rice_home),
        config_home=_expand(config_home),
        state_home=_expand(state_home),
    )


# Sub-paths derived from roots
def rice_dir(roots: Roots, name: str) -> str:
    """Path to a rice's directory: <rice_home>/<name>"""
    return os.path.join(roots.rice_home, name)


def manifest_path(roots: Roots, name: str) -> str:
    return os.path.join(rice_dir(roots, name), "rice.toml")


def snapshot_dir(roots: Roots) -> str:
    return os.path.join(roots.state_home, "rice-switcher", "snapshot")


def pending_snapshot_dir(roots: Roots, ts: str) -> str:
    return os.path.join(snapshot_dir(roots), f".pending-{ts}")


def committed_snapshot_dir(roots: Roots, ts: str) -> str:
    return os.path.join(snapshot_dir(roots), ts)


def latest_snapshot_link(roots: Roots) -> str:
    return os.path.join(snapshot_dir(roots), "latest")


def state_file(roots: Roots) -> str:
    return os.path.join(roots.state_home, "rice-switcher", "state.json")


def lock_file(roots: Roots) -> str:
    return os.path.join(roots.state_home, "rice-switcher", "switch.lock")


def log_dir(roots: Roots) -> str:
    return os.path.join(roots.state_home, "rice-switcher", "logs")


def default_log_file(roots: Roots) -> str:
    return os.path.join(log_dir(roots), "switcher.log")


def audit_log_file(roots: Roots) -> str:
    return os.path.join(log_dir(roots), "audit.log")
