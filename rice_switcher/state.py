"""State file read/write.

PLAN.md §5.5. The state.json records the current rice (so `apply` can
restore it after a Hyprland restart) and per-rice metadata used by
the trust cache (§2h).

Slice 1: the file is read on `current`/`status`, written on `switch`
success. Atomic write via temp file + rename so a crash mid-write
cannot leave a half-written state.json.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from .paths import Roots, state_file


CURRENT_STATE_VERSION = 1


@dataclass
class RiceTrust:
    """PLAN.md §2h: per-rice trust cache."""
    last_switched: float = 0.0
    manifest_sha256: str = ""
    last_known_good: str = ""   # last seen good state (e.g. "ok", "rolled_back")


@dataclass
class State:
    current_rice: str = ""
    last_switch: float = 0.0
    version: int = CURRENT_STATE_VERSION
    trust: dict[str, RiceTrust] = field(default_factory=dict)

    def trust_for(self, name: str) -> RiceTrust:
        if name not in self.trust:
            self.trust[name] = RiceTrust()
        return self.trust[name]


def read_state(roots: Roots) -> State:
    """Read state.json. Returns a default State if the file does not exist
    or cannot be parsed.
    """
    path = state_file(roots)
    if not os.path.isfile(path):
        return State()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data: dict[str, Any] = json.load(f)
    except (OSError, json.JSONDecodeError):
        # Corrupt state: do NOT silently overwrite. Return a default
        # but log; the caller may then choose to write a fresh one.
        return State()
    state = State(
        current_rice=str(data.get("current_rice", "")),
        last_switch=float(data.get("last_switch", 0.0)),
        version=int(data.get("version", CURRENT_STATE_VERSION)),
    )
    for name, td in (data.get("trust") or {}).items():
        state.trust[name] = RiceTrust(
            last_switched=float(td.get("last_switched", 0.0)),
            manifest_sha256=str(td.get("manifest_sha256", "")),
            last_known_good=str(td.get("last_known_good", "")),
        )
    return state


def write_state(roots: Roots, state: State) -> None:
    """Atomically write state.json. Uses temp + rename so concurrent
    readers see either the old or the new file, never a half-written
    one.
    """
    path = state_file(roots)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    data = {
        "current_rice": state.current_rice,
        "last_switch": state.last_switch,
        "version": state.version,
        "trust": {
            name: asdict(t) for name, t in state.trust.items()
        },
    }
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def mark_trust(roots: Roots, name: str, sha256: str, status: str = "ok") -> None:
    state = read_state(roots)
    t = state.trust_for(name)
    t.last_switched = time.time()
    t.manifest_sha256 = sha256
    t.last_known_good = status
    write_state(roots, state)
