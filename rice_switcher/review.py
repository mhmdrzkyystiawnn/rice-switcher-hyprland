"""Pure review/edit model for a detected `ManifestDraft` (DESIGN §5).

Fase 3a: the detector produces a *draft*; this module lets a human review
and edit it before anything is written. It is deliberately free of I/O —
the prompt driver (`review_cli.py`) and the CLI own the reading, printing
and writing, so every operation here is trivially testable headless.

Design notes
------------
* **Rules are reused, not reinvented.** Edits are validated against the
  same vocabulary the rest of the codebase uses: `manifest.VALID_TYPES`
  for `type`, `validate._SESSION_EXECUTABLES` for a forbidden executable,
  `process.looks_like_dangerous_pkill` for `pkill -f`, and the
  lifecycle identity-stop guard (an empty `argv_match` without
  `exact_argv` cannot be stopped for a non-compositor). The full
  `validate_manifest` runs at save time via `ReviewSession.validate`.
* **User edits are trustworthy.** Any field the user edits becomes
  confidence `auto` (a human confirmed it), which also clears the review
  comment the generator would otherwise emit.
* **The compositor is special.** Its `stop` is locked to `"true"`: the
  switcher never stops or starts the compositor (PLAN.md §2c), so there
  is nothing safe to edit there.
* **Undo is one or more steps.** Every successful mutation records a deep
  copy; `undo()` restores the most recent. A rejected edit records
  nothing and leaves the draft untouched.
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field

from .detect import (
    CONF_ASK,
    CONF_AUTO,
    CONF_GUESS,
    DraftComponent,
    Field,
    ManifestDraft,
)
from .manifest import VALID_TYPES, Component, Manifest, Meta
from .gaps import AUTOSTART_CONFLICT, OPTION_CONFIG
from .process import looks_like_dangerous_pkill
from .validate import ValidationResult, _SESSION_EXECUTABLES, validate_manifest

# Fields a user may edit through the review session. `type` is handled by
# `set_type` (it changes the compositor invariant); the rest are free-form.
EDITABLE_FIELDS = ("name", "start", "executable", "argv_match", "exact_argv",
                   "stop", "start_check")

_VALUE_FIELDS = ("start", "executable", "argv_match", "exact_argv", "stop",
                 "start_check")


class ReviewError(ValueError):
    """Raised when a requested edit would violate a manifest rule."""


@dataclass
class ComponentView:
    """A read-only row for the review driver."""

    index: int
    name: str
    type: str
    confidence: str
    needs_review: list[str] = field(default_factory=list)


def _forbidden_executable(exe: str, ctype: str) -> bool:
    """True if `exe` names a session-critical process for a non-compositor."""
    if ctype == "compositor":
        return False
    base = os.path.basename(exe).lower() if exe else ""
    return base in _SESSION_EXECUTABLES


def _identity_missing(comp: DraftComponent) -> bool:
    """True if a non-compositor has no addressable process identity."""
    if comp.type.value == "compositor":
        return False
    return not comp.argv_match.value and not comp.exact_argv.value


def _check_value(comp: DraftComponent, field_name: str, value) -> None:
    """Raise `ReviewError` if `value` is not acceptable for `field_name`."""
    if field_name == "name":
        if not isinstance(value, str) or not value:
            raise ReviewError("component name must be a non-empty string")
        return
    if field_name == "executable":
        if not isinstance(value, str):
            raise ReviewError("executable must be a string")
        if _forbidden_executable(value, comp.type.value):
            raise ReviewError(
                f"executable {value!r} names a session-critical process; "
                f"refusing to manage or kill it"
            )
        return
    if field_name == "argv_match":
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise ReviewError("argv_match must be a list of strings")
        if not value and not comp.exact_argv.value and comp.type.value != "compositor":
            raise ReviewError(
                "an empty argv_match without exact_argv cannot be stopped by "
                "identity; set exact_argv=true or provide argv_match"
            )
        return
    if field_name == "exact_argv":
        if not isinstance(value, bool):
            raise ReviewError("exact_argv must be a bool")
        if not value and not comp.argv_match.value and comp.type.value != "compositor":
            raise ReviewError(
                "clearing exact_argv would leave no identity (empty "
                "argv_match); set argv_match or keep exact_argv=true"
            )
        return
    if field_name in ("start", "stop", "start_check"):
        if not isinstance(value, str):
            raise ReviewError(f"{field_name} must be a string")
        if field_name == "stop" and comp.type.value == "compositor" and value != "true":
            raise ReviewError(
                "the compositor's stop is locked to \"true\": the switcher "
                "never stops or starts the compositor"
            )
        if looks_like_dangerous_pkill(value):
            raise ReviewError(
                f"{field_name}={value!r} uses `pkill -f`/`--full`, which can "
                f"match the switcher itself; use `pkill -x`"
            )
        return
    raise ReviewError(f"field {field_name!r} is not editable")


def _to_component(dc: DraftComponent) -> Component:
    timeout = dc.timeout.value
    if not isinstance(timeout, int):
        timeout = 10
    return Component(
        name=dc.name,
        type=dc.type.value,
        executable=dc.executable.value,
        argv_match=list(dc.argv_match.value),
        exact_argv=bool(dc.exact_argv.value),
        start=dc.start.value,
        stop=dc.stop.value,
        start_check=dc.start_check.value,
        reload=dc.reload.value,
        config_link=dc.config_link.value,
        depends_on=list(dc.depends_on.value),
        optional=bool(dc.optional.value),
        timeout=timeout,
        started_by_config=bool(dc.started_by_config.value),
    )


class ReviewSession:
    """A mutable review session over one `ManifestDraft`."""

    def __init__(self, draft: ManifestDraft):
        self.draft = draft
        self._history: list[tuple[list[DraftComponent], list]] = []

    # -- inspection --------------------------------------------------------

    def components(self) -> list[DraftComponent]:
        return list(self.draft.components)

    def component(self, name: str) -> DraftComponent | None:
        for c in self.draft.components:
            if c.name == name:
                return c
        return None

    def _get(self, name: str) -> DraftComponent:
        c = self.component(name)
        if c is None:
            raise ReviewError(f"no component named {name!r}")
        return c

    def findings(self) -> list:
        return list(self.draft.findings)

    def finding(self, index: int):
        """The finding at `index` (raises `ReviewError` when out of range)."""
        if not isinstance(index, int) or index < 0 or index >= len(self.draft.findings):
            raise ReviewError(f"tidak ada temuan nomor {index!r}")
        return self.draft.findings[index]

    def pending_findings(self) -> list:
        """Findings the user/`--yes` has neither handled nor skipped."""
        return [f for f in self.draft.findings if not f.is_resolved]

    def summary(self) -> list[ComponentView]:
        out: list[ComponentView] = []
        for i, c in enumerate(self.draft.components):
            out.append(ComponentView(
                index=i,
                name=c.name,
                type=c.type.value,
                confidence=c.worst_confidence(),
                needs_review=[k for k, f in c.field_map().items() if f.needs_review],
            ))
        return out

    def needs_review(self) -> list[tuple[str, str]]:
        """[(component_name, field_name)] for every `guess`/`ask` field."""
        out: list[tuple[str, str]] = []
        for c in self.draft.components:
            for field_name, f in c.field_map().items():
                if f.confidence in (CONF_ASK, CONF_GUESS):
                    out.append((c.name, field_name))
        return out

    # -- history -----------------------------------------------------------

    def _record(self) -> None:
        self._history.append((copy.deepcopy(self.draft.components),
                              copy.deepcopy(self.draft.findings)))

    def undo(self) -> bool:
        if not self._history:
            return False
        comps, findings = self._history.pop()
        self.draft.components = comps
        self.draft.findings = findings
        return True

    # -- mutations ---------------------------------------------------------

    def set_type(self, name: str, new_type: str) -> None:
        if new_type not in VALID_TYPES:
            raise ReviewError(
                f"invalid component type {new_type!r}; "
                f"must be one of {sorted(VALID_TYPES)}"
            )
        comp = self._get(name)
        self._record()
        comp.type = Field(new_type, CONF_AUTO)
        if new_type == "compositor":
            # The compositor's stop is locked to "true".
            comp.stop = Field("true", CONF_AUTO)

    def set_field(self, name: str, field_name: str, value) -> None:
        if field_name not in EDITABLE_FIELDS:
            raise ReviewError(f"field {field_name!r} is not editable")
        comp = self._get(name)
        _check_value(comp, field_name, value)
        if field_name == "name":
            if any(c is not comp and c.name == value for c in self.draft.components):
                raise ReviewError(f"a component named {value!r} already exists")
            self._record()
            comp.name = value
            return
        self._record()
        setattr(comp, field_name, Field(value, CONF_AUTO))

    def add(self, ctype: str, name: str | None = None, *,
            start: str = "", executable: str = "", argv_match=None,
            exact_argv=None, stop: str = "") -> DraftComponent:
        if ctype not in VALID_TYPES:
            raise ReviewError(
                f"invalid component type {ctype!r}; "
                f"must be one of {sorted(VALID_TYPES)}"
            )
        existing = {c.name for c in self.draft.components}
        if not name:
            base = ctype
            name = base
            i = 2
            while name in existing:
                name = f"{base}-{i}"
                i += 1
        elif name in existing:
            raise ReviewError(f"a component named {name!r} already exists")
        argv = list(argv_match) if argv_match else []
        exact = bool(exact_argv) if exact_argv is not None else False
        comp = DraftComponent(
            name=name,
            type=Field(ctype, CONF_AUTO),
            executable=Field(executable, CONF_AUTO),
            argv_match=Field(argv, CONF_AUTO),
            exact_argv=Field(exact, CONF_AUTO),
            start=Field(start, CONF_AUTO),
            stop=Field("true" if ctype == "compositor" else stop, CONF_AUTO),
            start_check=Field("", CONF_AUTO),
        )
        self._record()
        self.draft.components.append(comp)
        return comp

    def remove(self, name: str) -> None:
        self._get(name)
        self._record()
        self.draft.components = [c for c in self.draft.components if c.name != name]

    def move(self, name: str, delta: int) -> None:
        comp = self._get(name)
        idx = self.draft.components.index(comp)
        new = max(0, min(len(self.draft.components) - 1, idx + delta))
        if new == idx:
            return
        self._record()
        comps = list(self.draft.components)
        comps.pop(idx)
        comps.insert(new, comp)
        self.draft.components = comps

    # -- findings (Fase 4) -------------------------------------------------

    def handle_finding(self, index: int, option: str | None = None) -> None:
        """Resolve finding `index` with `option` (or its default).

        `option="skip"` acknowledges without acting. For an
        `autostart_conflict` the chosen option is also written back to the
        component: `config` sets `started_by_config=true` (Opsi B) and
        `switcher` clears it (Opsi C). Opsi A (`neutralise`) is reported as
        unavailable and is intentionally not accepted here.
        """
        f = self.finding(index)
        if option is None:
            option = f.default_option
            if not option:
                raise ReviewError(
                    "temuan ini tidak punya opsi default; pilih opsi "
                    "eksplisit atau `skip`"
                )
        if option == "skip":
            self._record()
            f.decision = "skip"
            return
        if option not in f.options:
            choices = ", ".join(f.options) if f.options else "tidak ada"
            raise ReviewError(
                f"opsi {option!r} tidak tersedia untuk temuan ini "
                f"(pilihan: {choices}, atau skip)"
            )
        self._record()
        f.decision = option
        if f.kind == AUTOSTART_CONFLICT and f.component:
            comp = self.component(f.component)
            if comp is not None:
                comp.started_by_config = Field(option == OPTION_CONFIG, CONF_AUTO)

    def apply_default_findings(self) -> list:
        """Resolve every finding that has a default option; return the rest.

        This is what `generate --yes` means by "accept the defaults": an
        `exec` conflict becomes Opsi B automatically, while an
        `exec-once`/`hl.exec_cmd` conflict and a `needs_materialization`
        warning (no default) stay pending and block the save.
        """
        for i, f in enumerate(self.draft.findings):
            if f.is_resolved:
                continue
            if f.default_option:
                self.handle_finding(i, f.default_option)
        return self.pending_findings()

    # -- output ------------------------------------------------------------

    def to_manifest(self) -> Manifest:
        return Manifest(
            meta=Meta(name=self.draft.name),
            components=[_to_component(c) for c in self.draft.components],
        )

    def validate(self, roots) -> ValidationResult:
        return validate_manifest(self.to_manifest(), roots)
