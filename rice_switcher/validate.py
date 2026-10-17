"""Validation for manifests.

PLAN.md §2.0 (schema), §2f (config_link rules), §2i (Hyprland config
verification), §11.1 (basic referenced-path extraction).

Validation is a pure function: it returns a `ValidationResult` describing
the errors and warnings it found. The CLI decides what to do with them
(Slice 1: errors abort, warnings are surfaced but do not block).
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from typing import Iterable

from .manifest import Component, Manifest
from .paths import Roots, manifest_path, rice_dir
from .process import looks_like_dangerous_pkill


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class ValidationResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def extend(self, other: "ValidationResult") -> None:
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)

    def raise_if_errors(self) -> None:
        if self.errors:
            raise ValidationError(self.errors)


class ValidationError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("manifest validation failed:\n  " + "\n  ".join(errors))


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _norm(path: str) -> str:
    """Normalise a path for comparison: expanduser + abspath, no realpath.

    Per PLAN.md §2f Aturan 6, we deliberately do NOT call realpath here.
    This preserves dangling symlinks as themselves so we can detect them
    later, and lets us compare two paths that may not yet exist.
    """
    if not path:
        return ""
    return os.path.abspath(os.path.expanduser(path))


def _is_strict_ancestor(child: str, parent: str) -> bool:
    """True if `child` equals `parent` or is strictly inside it.

    PLAN.md §2f Aturan 1: A ⊂ B if child path starts with `parent + os.sep`
    or equals parent.
    """
    if child == parent:
        return True
    return child.startswith(parent.rstrip(os.sep) + os.sep)


# ---------------------------------------------------------------------------
# config_link validation
# ---------------------------------------------------------------------------

ALLOWED_PATH_PREFIXES = (
    "~/.config/",
    "/.config/",
    "~/.local/state/",
    "/.local/state/",
)


def _config_link_under_allowed_roots(path: str, roots: Roots) -> bool:
    """PLAN.md §2f Aturan 7: config_link must live under allowed roots.

    The check is done on the *normalised* path, not the realpath. The
    intent is to refuse paths that would touch the system itself
    (`/etc/passwd`, `/home/other-user/.config`). We allow anything
    under the resolved config_home or state_home, plus rice_home
    (so rice can self-reference its own tree, used by snapshot logic).
    """
    if not path:
        return False
    n = _norm(path)
    candidates = {roots.config_home, roots.state_home, roots.rice_home}
    return any(_is_strict_ancestor(n, root) for root in candidates)


def _config_link_conflicts(components: Iterable[Component]) -> list[str]:
    """PLAN.md §2f Aturan 1: detect overlapping normalised paths."""
    out: list[str] = []
    pairs: list[tuple[str, Component]] = []
    for c in components:
        if c.config_link:
            pairs.append((_norm(c.config_link), c))
    for i in range(len(pairs)):
        for j in range(i + 1, len(pairs)):
            a_path, a = pairs[i]
            b_path, b = pairs[j]
            if a_path == b_path:
                out.append(
                    f"identical: {a.name} and {b.name} both -> {a_path}"
                )
            elif _is_strict_ancestor(a_path, b_path):
                out.append(f"nesting: {a.name}({a_path}) inside {b.name}({b_path})")
            elif _is_strict_ancestor(b_path, a_path):
                out.append(f"nesting: {b.name}({b_path}) inside {a.name}({a_path})")
    return out


def _config_link_redundant_sublings(components: Iterable[Component]) -> list[str]:
    """PLAN.md §2f Aturan 2: hypridle.conf / hyprlock.conf must not be
    declared when the parent `~/.config/hypr` is already managed.
    """
    warnings: list[str] = []
    parent_paths: set[str] = set()
    for c in components:
        if c.config_link:
            parent_paths.add(_norm(c.config_link))
    suspicious = ("hypridle.conf", "hyprlock.conf")
    for c in components:
        if not c.config_link:
            continue
        n = _norm(c.config_link)
        base = os.path.basename(n)
        if base in suspicious:
            for parent in parent_paths:
                if _is_strict_ancestor(n, parent) and n != parent:
                    warnings.append(
                        f"{c.name}: {n} is inside already-managed parent {parent}; "
                        f"remove this config_link to avoid double-swap"
                    )
    return warnings


# ---------------------------------------------------------------------------
# Binary presence
# ---------------------------------------------------------------------------

def _binary_present(start: str) -> bool:
    """Lightweight check: extract the first word of `start` and look it up
    via shutil.which. We do not actually execute the command.

    This is a coarse filter that catches typos in `start` (e.g.
    "qssp" instead of "qs"); it does not validate argv or behaviour.
    """
    if not start or start == "true":
        return True  # noop / explicit skip
    # Tokenise shell-quoted commands. The first word is the binary.
    first = start.split()[0]
    return shutil.which(first) is not None


# ---------------------------------------------------------------------------
# Hyprland config referenced-path extraction
# ---------------------------------------------------------------------------

_REQUIRE_RE = re.compile(r'require\(\s*"([^"]+)"\s*\)')
_SOURCE_RE = re.compile(r'^\s*source\s*=\s*(.+)$', re.MULTILINE)


def _extract_hyprland_refs(rice_root: str, config_link: str) -> tuple[list[str], list[str]]:
    """Extract `require("…")` and `source = …` references from a Hyprland
    config file under `rice_root`. Returns (refs, warnings).

    PLAN.md §11.1 (#20): basic referenced-path extraction for Slice 1.
    This is NOT a full Lua parser; it covers the common cases of
    `require("hyprland/foo")` and `source = ~/.config/hypr/hyprland/bar`
    inside the entry-point `rice_root/hyprland.lua`. Anything we can't
    parse is reported as a warning, not a fatal error.

    The function returns a list of *relative* paths within the rice
    root; the caller (validate_manifest) is responsible for checking
    that each resolves to a real file under the rice directory.
    """
    warnings: list[str] = []
    refs: list[str] = []

    # Find the entry-point file: <rice_root>/hyprland.lua
    entry = os.path.join(rice_root, "hyprland.lua")
    if not os.path.isfile(entry):
        return refs, warnings

    text: str
    try:
        with open(entry, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as e:
        warnings.append(f"could not read {entry}: {e}")
        return refs, warnings

    # Collect require()s
    for m in _REQUIRE_RE.finditer(text):
        rel = m.group(1)
        # Normalise Lua require: drop the leading "hyprland/" if present
        # (the dots-hyprland layout mirrors this convention).
        if rel.startswith("hyprland/"):
            rel = rel[len("hyprland/"):]
        refs.append(rel)

    # Collect source = … lines (Hyprlang syntax)
    for m in _SOURCE_RE.finditer(text):
        rhs = m.group(1).strip()
        # Strip trailing comments
        if "#" in rhs:
            rhs = rhs.split("#", 1)[0].strip()
        # Strip surrounding quotes
        if (rhs.startswith('"') and rhs.endswith('"')) or (
            rhs.startswith("'") and rhs.endswith("'")
        ):
            rhs = rhs[1:-1]
        if not rhs:
            continue
        # If it's a relative path (no leading slash, no ~), treat as
        # relative to the entry's parent.
        if not os.path.isabs(rhs) and not rhs.startswith("~"):
            # Resolve relative to entry's directory.
            refs.append(os.path.join(os.path.dirname(rel or ""), rhs))
        else:
            # Absolute / ~-prefixed paths: caller will resolve them
            # against allowed roots in validate_manifest.
            refs.append(rhs)

    return refs, warnings


# Prasyarat 0 (DESIGN-onboarding-tui.md §4.2): a manifest must not be
# able to make the switcher stop/kill a session-critical process. The
# compositor component is exempt: it legitimately names `hyprland` and is
# never stopped by the managed set (§2c Aturan 1).
_SESSION_EXECUTABLES = frozenset({
    "hyprland",
    "systemd", "systemd-logind", "systemd-user", "init",
    "dbus-daemon", "dbus-broker", "dbus-launch",
    "gnome-keyring-daemon", "gnome-keyring",
    "kwin_wayland", "kwin_x11", "gnome-shell", "plasmashell",
})


# ---------------------------------------------------------------------------
# Top-level entry
# ---------------------------------------------------------------------------

def validate_manifest(
    manifest: Manifest,
    roots: Roots,
    *,
    check_binaries: bool = True,
    check_hyprland: bool = True,
) -> ValidationResult:
    """Validate a parsed manifest against the rules in PLAN.md §2.0, §2f, §2i.

    Returns a `ValidationResult`; never raises for the issues it knows
    about (callers may raise via `raise_if_errors`).

    Parameters
    ----------
    check_binaries : if True (default), warn for components whose `start`
        command's first word is not on PATH. Skipped for `optional` ones.
    check_hyprland : if True (default), run a basic referenced-path
        check against `rice/<name>/hyprland.lua` if a `hyprland`
        component is present (PLAN.md §11.1 #20). This is a
        best-effort check; it does NOT replace `hyprland --verify-config`.
    """
    res = ValidationResult()

    # --- Section: meta ---
    if not manifest.meta.name:
        res.errors.append("[meta].name is required")
    if not manifest.meta.description:
        res.warnings.append("[meta].description is empty (recommended)")

    # --- Section: per-component ---
    for c in manifest.components:
        if c.type == "compositor":
            # PLAN.md §2c Aturan 1: explicit, just for documentation.
            pass
        if not c.start and not c.optional:
            res.errors.append(f"component {c.name!r}: `start` is required")
        if check_binaries and c.start and not c.optional and not _binary_present(c.start):
            res.warnings.append(
                f"component {c.name!r}: start command {c.start!r} "
                f"not found on PATH"
            )
        if c.health_check and not c.start_check:
            # health_check makes sense only if there's a way to confirm
            # the process is up; we don't enforce it, just warn.
            res.warnings.append(
                f"component {c.name!r}: health_check set but start_check empty"
            )
        if c.executable and "/" in c.executable:
            res.warnings.append(
                f"component {c.name!r}: executable should be a basename, "
                f"got {c.executable!r}"
            )
        # Prasyarat 0 (§4.2): refuse session-critical executables. The
        # compositor is exempt (it names `hyprland` on purpose and is
        # never killed); every other component must not point at a
        # session process.
        exe_base = os.path.basename(c.executable).lower() if c.executable else ""
        if c.type != "compositor" and exe_base in _SESSION_EXECUTABLES:
            res.errors.append(
                f"component {c.name!r}: executable {c.executable!r} names a "
                f"session-critical process; refusing to manage or kill it"
            )
        # PLAN.md §2g: `pkill -f`/`--full` matches the whole command line,
        # including the switcher's own argv. Refuse it in any lifecycle
        # command so a manifest cannot kill the thing running it.
        for field_name in ("start", "stop", "reload", "start_check",
                           "stop_check", "health_check"):
            value = getattr(c, field_name)
            if value and looks_like_dangerous_pkill(value):
                res.errors.append(
                    f"component {c.name!r}: {field_name}={value!r} uses "
                    f"`pkill -f`/`--full`, which can match the switcher itself; "
                    f"use `pkill -x`"
                )

    # PLAN.md §2g/§2h: hooks and theme.apply run arbitrary shell; refuse
    # the same dangerous pattern there.
    for phase, hooks in (("hooks.pre", manifest.hooks_pre),
                         ("hooks.post", manifest.hooks_post)):
        for h in hooks:
            if h and looks_like_dangerous_pkill(h):
                res.errors.append(
                    f"{phase}: {h!r} uses `pkill -f`/`--full`, which can match "
                    f"the switcher itself; use `pkill -x`"
                )
    if manifest.theme.apply and looks_like_dangerous_pkill(manifest.theme.apply):
        res.errors.append(
            "[theme].apply uses `pkill -f`/`--full`, which can match the "
            "switcher itself; use `pkill -x`"
        )

    # --- Section: config_link conflicts (§2f) ---
    res.warnings.extend(_config_link_redundant_sublings(manifest.components))
    for err in _config_link_conflicts(manifest.components):
        res.errors.append(err)

    # --- Section: config_link escape prevention (§2f Aturan 7) ---
    for c in manifest.components:
        if not c.config_link:
            continue
        if not _config_link_under_allowed_roots(c.config_link, roots):
            res.errors.append(
                f"component {c.name!r}: config_link {c.config_link!r} is outside "
                f"allowed roots (config_home={roots.config_home}, "
                f"state_home={roots.state_home}, rice_home={roots.rice_home})"
            )

    # --- Section: hyprlock prerequisites (§9.2) ---
    rice_root = manifest.rice_root(roots.rice_home)
    has_hyprlock = any(c.name == "hyprlock" or c.type == "locker" for c in manifest.components)
    if has_hyprlock:
        for script in ("check-capslock.sh", "status.sh"):
            path = os.path.join(rice_root, "hypr", "hyprlock", script)
            if not os.path.isfile(path):
                # Also check the live config (in case rice shares a parent)
                live = os.path.expanduser(f"~/.config/hypr/hyprlock/{script}")
                if not os.path.isfile(live):
                    res.warnings.append(
                        f"rice enables hyprlock but {script} not found at "
                        f"{path} (or ~/.config/hypr/hyprlock/{script}). "
                        f"hyprlock will fail to start until this script is provided."
                    )

    # --- Section: Hyprland basic referenced-path check (§11.1 #20) ---
    if check_hyprland:
        has_hyprland = any(c.name == "hyprland" for c in manifest.components)
        if has_hyprland:
            # Resolve the rice dir for this manifest
            refs, parse_warnings = _extract_hyprland_refs(rice_root, "hyprland")
            res.warnings.extend(parse_warnings)
            missing: list[str] = []
            for r in refs:
                # Resolve relative to rice root; for absolute or ~ paths
                # we resolve against the rice dir but treat outside as a
                # warning rather than an error (we don't know if the
                # user has external includes by design).
                if r.startswith("~") or os.path.isabs(r):
                    target = os.path.expanduser(r)
                    if not os.path.exists(target):
                        missing.append(r)
                else:
                    target = os.path.normpath(os.path.join(rice_root, r))
                    if not os.path.exists(target):
                        missing.append(r)
            for m in missing:
                res.warnings.append(
                    f"hyprland config references {m!r} but it does not exist "
                    f"under {rice_root}/. hyprland --verify-config may pass but "
                    f"Hyprland could fail to start. (basic check, not a full Lua parse)"
                )

    return res


# ---------------------------------------------------------------------------
# Slice 2: extended validation (hyprland --verify-config, matugen L1+L2)
# ---------------------------------------------------------------------------

import subprocess as _subprocess
import tomllib as _tomllib
from typing import Iterable


def hyprland_verify_config(
    config_path: str,
    *,
    hyprland_bin: str = "hyprland",
    timeout: float = 5.0,
) -> tuple[int, str]:
    """Run `hyprland --verify-config --config <config_path>`.

    Returns (returncode, combined_stdout_stderr). Uses argv list, never
    shell. Returns (-1, str(exc)) on binary not found or OSError. A
    timeout returns (-2, "timeout")."""
    try:
        proc = _subprocess.run(
            [hyprland_bin, "--verify-config", "--config", config_path],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        return proc.returncode, (proc.stdout + proc.stderr).strip()
    except FileNotFoundError as e:
        return -1, f"hyprland binary not found: {e}"
    except _subprocess.TimeoutExpired:
        return -2, "timeout"
    except OSError as e:
        return -1, str(e)


def resolve_matugen_path(cfg_dir: str, raw: str) -> str:
    """Mirror matugen 4.2.0 path resolution.

    - '~'-prefix: os.path.expanduser (honors HOME env).
    - absolute:    os.path.normpath.
    - relative:    os.path.normpath joined with cfg_dir.
    """
    if not isinstance(raw, str):
        return ""
    if raw.startswith("~"):
        return os.path.normpath(os.path.expanduser(raw))
    if os.path.isabs(raw):
        return os.path.normpath(raw)
    return os.path.normpath(os.path.join(cfg_dir, raw))


def check_matugen_template_l1l2(
    matugen_config_path: str,
) -> tuple[list[str], list[str], list[str]]:
    """L1 + L2 matugen template check (no runtime call).

    Returns (errors, warnings, info_messages)."""
    errors: list[str] = []
    warnings: list[str] = []
    info: list[str] = []

    if not os.path.isfile(matugen_config_path):
        info.append(f"matugen config not present at {matugen_config_path}; skipping L1+L2")
        return errors, warnings, info

    # L1: TOML well-formed
    try:
        with open(matugen_config_path, "rb") as f:
            data = _tomllib.load(f)
    except _tomllib.TOMLDecodeError as e:
        errors.append(f"matugen config: TOML parse error: {e}")
        return errors, warnings, info

    cfg_dir = os.path.dirname(matugen_config_path) or "."

    templates = data.get("templates")
    if not isinstance(templates, dict) or not templates:
        info.append("matugen config has no [templates.*] entries; L2 not applicable")
        return errors, warnings, info

    # L2: existence check for input_path; parent-existence for output_path
    for tname, tbody in templates.items():
        if not isinstance(tbody, dict):
            warnings.append(f"matugen templates.{tname}: not a table; skipping L2")
            continue
        in_raw = tbody.get("input_path", "")
        out_raw = tbody.get("output_path", "")
        if not in_raw:
            warnings.append(f"matugen templates.{tname}: input_path missing")
        else:
            in_resolved = resolve_matugen_path(cfg_dir, in_raw)
            if not os.path.exists(in_resolved):
                warnings.append(
                    f"matugen templates.{tname}: input_path {in_raw!r} "
                    f"not found at {in_resolved}"
                )
        if not out_raw:
            warnings.append(f"matugen templates.{tname}: output_path missing")
        else:
            out_resolved = resolve_matugen_path(cfg_dir, out_raw)
            parent = os.path.dirname(out_resolved)
            if parent and not os.path.isdir(parent):
                warnings.append(
                    f"matugen templates.{tname}: output_path parent "
                    f"{parent!r} not found"
                )
            # Also warn if relative path escapes the rice dir (../)
            if not os.path.isabs(out_raw) and not out_raw.startswith("~"):
                rel = os.path.relpath(out_resolved, cfg_dir)
                if rel.startswith(".."):
                    warnings.append(
                        f"matugen templates.{tname}: output_path {out_raw!r} "
                        f"escapes matugen config directory"
                    )

    return errors, warnings, info


def extended_validate(
    manifest,
    roots: Roots,
    *,
    check_hyprland_verify: bool = True,
    hyprland_verify_timeout: float = 5.0,
    matugen_config_search: Iterable[str] = (),
) -> "ValidationResult":
    """Slice 2 comprehensive validation. Wraps validate_manifest() and
    adds hyprland --verify-config and matugen L1+L2 checks.

    `matugen_config_search` is an iterable of candidate paths relative
    to the rice root to check for a matugen config.toml. We use the
    first that exists.
    """
    res = validate_manifest(manifest, roots, check_binaries=True, check_hyprland=True)

    # Hyprland --verify-config
    if check_hyprland_verify:
        has_hyprland = any(c.name == "hyprland" for c in manifest.components)
        if has_hyprland:
            # Determine the rice root
            rice_root = os.path.dirname(os.path.dirname(manifest.source_path)) if manifest.source_path else roots.rice_home
            candidate = os.path.join(rice_root, "hyprland.lua")
            if not os.path.isfile(candidate):
                # Fall back to the live config (in case the rice shares a parent)
                live = os.path.expanduser("~/.config/hypr/hyprland.lua")
                if os.path.isfile(live):
                    candidate = live
                else:
                    res.warnings.append(
                        f"hyprland --verify-config skipped: no config found at "
                        f"{candidate} or {live}"
                    )
                    candidate = ""
            if candidate:
                rc, out = hyprland_verify_config(
                    candidate, timeout=hyprland_verify_timeout,
                )
                if rc == -1 and "not found" in out:
                    res.warnings.append(
                        f"hyprland binary not found; --verify-config not run. "
                        f"Required if rice uses hyprland."
                    )
                elif rc == -2:
                    res.errors.append(
                        f"hyprland --verify-config timeout after "
                        f"{hyprland_verify_timeout}s on {candidate}"
                    )
                elif rc == 0:
                    res.warnings.append(
                        f"hyprland --verify-config ok ({candidate})"
                    )
                else:
                    res.errors.append(
                        f"hyprland --verify-config failed (rc={rc}) on "
                        f"{candidate}: {out[:500]}"
                    )

    # Matugen L1+L2
    if matugen_config_search:
        rice_root = os.path.dirname(os.path.dirname(manifest.source_path)) if manifest.source_path else roots.rice_home
        for rel in matugen_config_search:
            candidate = os.path.join(rice_root, rel)
            if os.path.isfile(candidate):
                merrs, mwarns, minfo = check_matugen_template_l1l2(candidate)
                res.errors.extend(f"matugen: {e}" for e in merrs)
                res.warnings.extend(f"matugen: {w}" for w in mwarns)
                # info messages are not added to res; we just used them to gate
                break

    return res


def manifest_exists(roots: Roots, name: str) -> bool:
    return os.path.isfile(manifest_path(roots, name))
