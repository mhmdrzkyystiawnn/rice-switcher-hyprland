"""Fase 4 Langkah 1: gap finding model + pure classifiers (DESIGN §6.1).

Two kinds, matched to the Langkah 0 decision:

  * `autostart_conflict` — a managed component is *also* launched by a
    reachable autostart line. Two handling options: `config` (Opsi B,
    `started_by_config=true`, the default for a plain `exec` line) and
    `switcher` (Opsi C, the switcher starts and stops it). `exec-once`
    and `hl.exec_cmd` (inside `hl.on("hyprland.start")`) get **no**
    default: they must be decided by the user (`ask`).
  * `needs_materialization` — no readable Hyprland entrypoint, but the
    repo looks like it materialises its config from a profile
    (`profiles/`, `settings.lua`, `variables.lua`, or a `*.sh` with an
    `apply` subcommand).

Hard rule: **read-only**. Nothing here executes a rice script, evaluates
Lua, or spawns a process; it only reports paths and `file:line` refs.
`archive`/`backup`/`example` directories and symlinks that escape the rice
never trigger a finding.
"""

from __future__ import annotations

import os
import re

from .detect import (
    AutostartRef,
    Finding,
    _inside,
    _read_text,
    _under_ignored_dir,
    _walk_files,
)

# Finding kinds.
AUTOSTART_CONFLICT = "autostart_conflict"
NEEDS_MATERIALIZATION = "needs_materialization"

# autostart_conflict handling options (DESIGN §6.1).
OPTION_CONFIG = "config"        # Opsi B: started_by_config=true (stop-only)
OPTION_SWITCHER = "switcher"    # Opsi C: switcher manages start+stop
OPTION_NEUTRALISE = "neutralise"  # Opsi A: NOT implemented (reported only)

# Default option per autostart kind (Langkah 0 decision): only a plain
# `exec` line re-runs on `hyprctl reload`, so only it defaults to Opsi B.
# `exec-once` and `hl.exec_cmd` fire once at session start -> ask.
_DEFAULT_BY_KIND = {
    "exec": OPTION_CONFIG,
    "exec-once": "",
    "hl.exec_cmd": "",
}

# Materialisation signals (DESIGN §6.1).
_MATERIALISATION_DIRS = ("profiles",)
_MATERIALISATION_FILES = ("settings.lua", "variables.lua")

# A shell case-arm exposing an `apply` subcommand, e.g. `apply)`,
# `"apply")`, `apply|`. Textual only -- the script is never run.
_SH_APPLY_RE = re.compile(r"(?m)^\s*(?:\"|')?apply(?:\"|')?\s*[)|]")


def _default_for_kinds(kinds) -> str:
    """Opsi B only when *every* conflicting line is a plain `exec`."""
    defaults = {_DEFAULT_BY_KIND.get(k, "") for k in kinds}
    if defaults == {OPTION_CONFIG}:
        return OPTION_CONFIG
    return ""


def autostart_conflict(name: str, refs: list[AutostartRef]) -> Finding | None:
    """Build the conflict Finding for managed component `name`.

    `refs` are the autostart refs that launch this component (reachable
    ones only matter). Returns None when nothing reachable launches it.
    """
    live = [r for r in refs if r.reachable]
    if not live:
        return None
    kinds = sorted({r.kind for r in live})
    default = _default_for_kinds(kinds)
    ref_labels = ", ".join(f"{r.file}:{r.line} ({r.kind})" for r in live)
    if default:
        tail = (f"default Opsi B ({OPTION_CONFIG}): biarkan config yang "
                f"menjalankan, switcher hanya stop")
    else:
        tail = ("tidak ada default (exec-once/hl.exec_cmd hanya jalan sekali "
                "saat login); pilih " + OPTION_CONFIG + " atau " + OPTION_SWITCHER)
    return Finding(
        kind=AUTOSTART_CONFLICT,
        title=f"autostart bentrok: {name}",
        detail=f"{name} juga dijalankan oleh config rice ({ref_labels}); {tail}. "
               f"Opsi A ({OPTION_NEUTRALISE}, netralkan baris) belum tersedia.",
        decision="pending",
        component=name,
        refs=[(r.file, r.line, r.kind) for r in live],
        options=[OPTION_CONFIG, OPTION_SWITCHER],
        default_option=default,
    )


def autostart_conflicts(
    autostart_refs_by_name: dict[str, list[AutostartRef]],
) -> list[Finding]:
    """One Finding per managed component its own config also launches.

    `autostart_refs_by_name` maps a managed component name to the refs
    that produced it (only reachable refs create components).
    """
    out: list[Finding] = []
    for name in sorted(autostart_refs_by_name):
        f = autostart_conflict(name, autostart_refs_by_name[name])
        if f is not None:
            out.append(f)
    return out


def _apply_subcommand_sh(rel: str, abspath: str) -> bool:
    if not rel.lower().endswith(".sh"):
        return False
    text, _reason = _read_text(abspath)
    if not text:
        return False
    return bool(_SH_APPLY_RE.search(text))


def materialisation_signals(rice_dir: str) -> list[str]:
    """Rice-relative paths that suggest the config is materialised.

    Read-only and containment-checked: symlinks escaping the rice and
    `archive`/`backup`/`example` directories are ignored.
    """
    rice_dir = os.path.normpath(rice_dir)
    rice_real = os.path.realpath(rice_dir)
    signals: list[str] = []

    for d in _MATERIALISATION_DIRS:
        p = os.path.join(rice_dir, d)
        if os.path.isdir(p) and _inside(rice_real, os.path.realpath(p)):
            signals.append(f"{d}/")
    for f in _MATERIALISATION_FILES:
        p = os.path.join(rice_dir, f)
        if os.path.isfile(p) and _inside(rice_real, os.path.realpath(p)):
            signals.append(f)

    files, _notes = _walk_files(rice_dir, rice_real)
    for rel, abspath in files:
        if _under_ignored_dir(rel):
            continue
        if _apply_subcommand_sh(rel, abspath):
            signals.append(rel)
    return signals


def needs_materialization(rice_dir: str, *, has_entrypoint: bool) -> Finding | None:
    """Report a materialisation gap when there is no readable entrypoint.

    Returns None when an entrypoint exists (nothing to materialise) or
    when no signal is present.
    """
    if has_entrypoint:
        return None
    signals = materialisation_signals(rice_dir)
    if not signals:
        return None
    return Finding(
        kind=NEEDS_MATERIALIZATION,
        title="rice butuh materialisasi",
        detail="tidak ada entrypoint Hyprland yang terbaca; sinyal materialisasi: "
               + ", ".join(signals)
               + ". Skrip tidak dijalankan (butuh persetujuan eksplisit, fase lain).",
        decision="pending",
        path=rice_dir,
        options=[],
        default_option="",
    )


def collect_findings(
    rice_dir: str,
    *,
    has_entrypoint: bool,
    autostart_refs_by_name: dict[str, list[AutostartRef]],
) -> list[Finding]:
    """All Fase 4 findings for a rice (read-only)."""
    out = autostart_conflicts(autostart_refs_by_name)
    mat = needs_materialization(rice_dir, has_entrypoint=has_entrypoint)
    if mat is not None:
        out.append(mat)
    return out
