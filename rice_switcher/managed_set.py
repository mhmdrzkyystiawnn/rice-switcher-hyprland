"""Managed set and diff computation.

PLAN.md §2c (managed set + compositor invariant) and §2g (identity-aware
diff). This module is pure: no I/O, no subprocess. The CLI combines it
with snapshot/lock/state modules to drive a real switch.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Iterable

from .manifest import Component, Manifest


@dataclass(frozen=True)
class Identity:
    executable: str
    argv_match: tuple[str, ...] = ()
    exact: bool = False

    @classmethod
    def from_component(cls, c: Component) -> "Identity":
        exe = c.executable or c.name
        return cls(exe, tuple(c.argv_match), bool(c.exact_argv))

    def __str__(self) -> str:
        argv = " " + " ".join(self.argv_match) if self.argv_match else ""
        if self.exact:
            # Make the match mode explicit in the confirmation view
            # (PLAN.md §2h): a no-argument identity must be readable as
            # such, not as a bare basename.
            if not self.argv_match:
                return f"{self.executable} (exact: no args)"
            return f"{self.executable}{argv} (exact)"
        return f"{self.executable}{argv}"


@dataclass
class SwitchPlan:
    """The result of computing a switch. Identity-aware (PLAN.md §2c)."""

    target: str
    current: str | None
    to_stop: list[Identity] = field(default_factory=list)
    to_start: list[Identity] = field(default_factory=list)
    to_reload: list[Identity] = field(default_factory=list)
    to_swap: list[str] = field(default_factory=list)   # config_link paths
    to_keep: list[Identity] = field(default_factory=list)
    to_touch: list[str] = field(default_factory=list)   # components in neither set
                                                # but listed; we still report them
                                                # for visibility.
    notes: list[str] = field(default_factory=list)     # human-readable warnings
    # Fase 4 (Opsi B): identities the target rice launches from its own
    # config (`started_by_config`). The switcher never starts them, so
    # they are reported here instead of `to_start` (DESIGN §6.1).
    to_config: list[Identity] = field(default_factory=list)

    def is_noop(self) -> bool:
        return not (self.to_stop or self.to_start or self.to_reload or self.to_swap)


# Components that are managed by the switcher. Any component listed
# under these names in any rice is, by convention, never touched by the
# switcher and is started by hyprland/execs.lua instead.
#
# PLAN.md §2c: these are auto-excluded from the managed set. Listed here
# as documentation; the load_managed_set function reads from
# manifest.components directly and does not special-case names.
SHARED_COMPONENT_NAMES = frozenset({
    "gnome_keyring", "geoclue", "ydotool",
    "cliphist_text", "cliphist_image",
})


def load_managed_set(rices: Iterable[Manifest]) -> set[Identity]:
    """Return the union of identities across all rices.

    PLAN.md §2c: every identity that appears in any rice is part of
    the managed set; anything else is left alone.
    """
    out: set[Identity] = set()
    for r in rices:
        for c in r.components:
            ident = Identity.from_component(c)
            if ident.executable in SHARED_COMPONENT_NAMES:
                # We still add it to the set so it can be skipped in
                # stop/start decisions; the invariant "never touched"
                # is enforced by the *executable* name lookup in
                # compute_plan below.
                pass
            out.add(ident)
    return out


def _config_link_swap_needed(
    config_link: str,
    symlink_realpaths: dict[str, str | None],
    target_rice_dir: str,
) -> bool:
    """Return True if a swap is required for this config_link.

    Per PLAN.md §2c + §2f Aturan 6, we use realpath only for *detection*;
    normalising config_link itself uses abspath+expanduser so dangling
    symlinks and not-yet-existing paths are handled.

    A swap is needed if the live symlink's realpath does NOT point
    under the target rice directory.
    """
    n = os.path.abspath(os.path.expanduser(config_link))
    rp = symlink_realpaths.get(n)
    if rp is None:
        # Not currently a symlink (or path doesn't exist): switcher
        # will create the symlink.
        return True
    # Otherwise, the link already points somewhere. If it points under
    # the target rice, no swap is needed.
    return not rp.startswith(target_rice_dir.rstrip(os.sep) + os.sep)


def compute_plan(
    current_manifest: Manifest | None,
    target_manifest: Manifest,
    all_manifests: list[Manifest],
    symlink_realpaths: dict[str, str | None],
    config_home: str,
    *,
    active_identities: set[Identity] | None = None,
) -> SwitchPlan:
    """Compute the SwitchPlan for moving from `current_manifest` to
    `target_manifest`. The caller supplies the live symlink realpaths
    (gathered via /proc-style filesystem reads elsewhere).

    Parameters
    ----------
    active_identities : if provided, the set of identities that are
        currently running. If absent, the plan assumes only the
        identities in `current_manifest` are running.
    symlink_realpaths : `{config_link: realpath_or_None}` for the LIVE
        filesystem. None means the path does not exist as a symlink.
    config_home : resolved config_home (used to compute absolute
        config_link paths and target_rice_dir).

    Raises
    ------
    ValueError : if `target_manifest.source_path` is empty. A manifest
        built in memory without a backing file cannot be reliably
        located on disk, and the previous fallback to `meta.name`
        produced silently-wrong swap decisions (audit fix S2).
    """
    if not target_manifest.source_path:
        raise ValueError(
            "compute_plan: target_manifest.source_path is empty; "
            "load the manifest from disk via load_manifest(path) before "
            "calling compute_plan. A manifest without source_path cannot "
            "be located reliably on the filesystem."
        )

    target_name = target_manifest.meta.name
    current_name = current_manifest.meta.name if current_manifest else None

    # The swap target for a config_link is
    # `<rice_home>/<name>/<basename(config_link)>` (see switch.do_switch).
    # So the "target rice directory" that a live symlink must point under
    # is `<rice_home>/<name>` -- i.e. the rice dir, one level above
    # rice.toml. Using `<rice_home>` here made every rice look like it
    # already owned the link (elifouts' realpath is under rice_home), so
    # switching between two rices that both declare the same config_link
    # silently skipped the swap.
    target_rice_dir = os.path.dirname(target_manifest.source_path)

    managed = load_managed_set(all_manifests)

    # Compositor identities across ALL manifests (PLAN.md §2c Aturan 1):
    # an identity is protected as the compositor if ANY rice declares it so,
    # regardless of which rice is current or target. Resolving the type from
    # `current_manifest` alone left a hole: a compositor that is active but
    # not declared as compositor in the current manifest (or declared with a
    # different `type`) could land in `to_stop`, where `find_component` then
    # resolves it to the real compositor component and stops it.
    compositor_identities = {
        Identity.from_component(c)
        for m in all_manifests
        for c in m.components
        if c.type == "compositor"
    }

    target_identities = {Identity.from_component(c) for c in target_manifest.components}

    # Derive "currently active identities" from the current manifest if
    # the caller did not supply active_identities.
    if active_identities is None:
        if current_manifest is None:
            active_identities = set()
        else:
            active_identities = {Identity.from_component(c) for c in current_manifest.components}

    plan = SwitchPlan(target=target_name, current=current_name)

    # 1. to_stop: identities currently active and NOT in target, and
    #    not compositor. Compositor is exempt (§2c Aturan 1).
    for ident in sorted(active_identities, key=str):
        if ident in target_identities:
            continue
        if ident.executable in SHARED_COMPONENT_NAMES:
            continue
        if ident in compositor_identities:
            continue
        plan.to_stop.append(ident)

    # 2. to_start: identities in target and NOT currently active.
    #    Compositor exempt. Fase 4 (Opsi B): a component the target rice
    #    already launches from its own config (`started_by_config`) is
    #    never started by the switcher -- it is reported in `to_config`
    #    so a dry-run shows it honestly and cannot double-start it.
    target_by_ident = {Identity.from_component(c): c for c in target_manifest.components}
    for ident in sorted(target_identities, key=str):
        if ident in active_identities:
            continue
        if ident.executable in SHARED_COMPONENT_NAMES:
            continue
        if ident in compositor_identities:
            continue
        tc = target_by_ident.get(ident)
        if tc is not None and tc.started_by_config:
            plan.to_config.append(ident)
            continue
        plan.to_start.append(ident)

    # 3. to_reload: active and in target, AND has a reload field. We
    #    assume the config_link swap already happened; reload follows.
    for c in target_manifest.components:
        ident = Identity.from_component(c)
        if ident in active_identities and ident not in set(plan.to_start) and c.reload:
            plan.to_reload.append(ident)

    # 4. to_swap: config_links whose live symlink is not pointing at the
    #    target rice dir.
    for c in target_manifest.components:
        if c.config_link:
            if _config_link_swap_needed(c.config_link, symlink_realpaths, target_rice_dir):
                plan.to_swap.append(os.path.abspath(os.path.expanduser(c.config_link)))

    # 5. to_keep: active identities that survive (not stopped, not
    #    reloaded). Note: excludes SHARED_COMPONENT_NAMES automatically.
    keep = (active_identities & target_identities
            - set(plan.to_stop) - set(plan.to_reload)
            - {i for i in active_identities if i.executable in SHARED_COMPONENT_NAMES})
    plan.to_keep = sorted(keep, key=str)

    # 6. Notes: surface interesting properties for the user.
    for c in target_manifest.components:
        if Identity.from_component(c).executable in SHARED_COMPONENT_NAMES:
            plan.notes.append(
                f"component {c.name!r} is in SHARED_COMPONENT_NAMES; "
                f"the switcher will not stop or start it"
            )

    return plan
