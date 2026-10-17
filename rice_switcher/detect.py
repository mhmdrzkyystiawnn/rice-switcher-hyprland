"""Detect a rice's components from its config tree (DESIGN-onboarding-tui.md §4).

Produces a `ManifestDraft`, never a final manifest: every field carries a
confidence (`auto` / `guess` / `ask`) so the review step (Fase 3) can
highlight what a human must decide.

Hard rules (DESIGN §1, §10):

* **Text only.** No `eval`, no Lua interpreter, no shell, no subprocess.
  Autostart lines are matched with regexes; only a pure string literal is
  turned into a command. A concatenation or a dynamic expression is
  reported as `ask` and never guessed.
* **Reachability.** Components come from autostart lines in files that are
  actually reachable from the Hyprland entrypoint (`hyprland.lua`/`.conf`),
  following `source =` (hyprlang) and literal `require(...)`/`dofile(...)`
  (Lua) only while they stay inside the rice dir. Archive/backup/profile
  files that are not reachable never create components; their refs are
  recorded with `reachable=False` (for Fase 4). When no entrypoint is
  readable we do **not** fall back to scanning everything; we report it.
* **Containment.** Every path is checked with `realpath` + `commonpath`
  against the rice directory. A symlink that escapes the rice dir is
  reported and never followed or read.
* **Bounded.** The scan stops after `MAX_FILES` files, skips files larger
  than `MAX_FILE_BYTES`, and does not descend past `MAX_DEPTH`. `.git`,
  binary files (a NUL byte), and undecodable files are skipped. Malformed
  input never raises.
* **`stop` is never guessed.** For every non-compositor component `stop`
  is left empty (identity-based stop, Prasyarat 0). Where the process
  identity cannot be pinned down (`argv_match`), the field is `ask`
  instead of inventing a `pkill`. The compositor is the one exception: it
  keeps an explicit `stop` because `stop_component` refuses to
  identity-kill a compositor.
"""

from __future__ import annotations

import os
import re
import shlex
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:  # pragma: no cover
    from .paths import Roots

# Confidence vocabulary.
CONF_AUTO = "auto"
CONF_GUESS = "guess"
CONF_ASK = "ask"
_RANK = {CONF_AUTO: 0, CONF_GUESS: 1, CONF_ASK: 2}

# Resource limits (DESIGN §10).
MAX_FILES = 5000
MAX_FILE_BYTES = 512 * 1024
MAX_DEPTH = 12
MAX_SOURCE_DEPTH = 24          # `source=`/`require`/`dofile` follow depth

# Executable basename -> (component name, type). Aligned with
# manifest.VALID_TYPES. Data table, not if/else (Langkah 3).
KNOWN_BINARIES = {
    "qs": ("quickshell", "shell"),
    "quickshell": ("quickshell", "shell"),
    "waybar": ("waybar", "bar"),
    "hypridle": ("hypridle", "system"),
    "dunst": ("dunst", "notifications"),
    "mako": ("mako", "notifications"),
    "swaync": ("swaync", "notifications"),
    "swaync-client": ("swaync", "notifications"),
    "hyprpaper": ("hyprpaper", "wallpaper"),
    "swww": ("swww", "wallpaper"),
    "swww-daemon": ("swww", "wallpaper"),
    "awww": ("awww", "wallpaper"),
    "awww-daemon": ("awww", "wallpaper"),
    "mpvpaper": ("mpvpaper", "wallpaper"),
    "hyprlock": ("hyprlock", "locker"),
    "hyprsunset": ("hyprsunset", "system"),
    "hyprpolkitagent": ("hyprpolkitagent", "system"),
    "pypr": ("pypr", "system"),
    "fcitx5": ("fcitx5", "system"),
}

# Executable basename -> the subcommands that are one-shot *clients*, not
# the daemon. `swww restore` / `swww img …` talk to an already-running
# `swww-daemon`; using them to derive a managed component's process
# identity is unreliable, so Langkah 0 marks those identities `ask`.
KNOWN_CLIENT_SUBCOMMANDS = {
    "swww": frozenset({"img", "restore", "clear", "kill", "query"}),
    "awww": frozenset({"img", "restore", "clear", "kill", "query"}),
}

_CLIENT_NOTE = "perintah klien, mungkin bukan daemon"

# Directory name -> (component name, type), for config-dir signals.
DIR_SIGNALS = {
    "waybar": ("waybar", "bar"),
    "dunst": ("dunst", "notifications"),
    "mako": ("mako", "notifications"),
    "swaync": ("swaync", "notifications"),
    "hyprpaper": ("hyprpaper", "wallpaper"),
    "swww": ("swww", "wallpaper"),
    "awww": ("awww", "wallpaper"),
    "mpvpaper": ("mpvpaper", "wallpaper"),
}

# Directories that are never treated as live configuration (Langkah 3):
# archived profiles, backups and examples must not create components.
IGNORED_DIRS = frozenset({
    "archive", "old", "backup", "bak", "examples", "example",
})

# Entrypoint candidates, relative to the config root, in preference order.
ENTRYPOINT_CANDIDATES = (
    "hypr/hyprland.lua", "hypr/hyprland.conf",
    "hyprland.lua", "hyprland.conf",
)

_FIELD_ORDER = (
    "type", "executable", "argv_match", "exact_argv", "start", "stop",
    "start_check", "reload", "config_link", "depends_on", "optional", "timeout",
)

_SUSPICIOUS_CHARS = set(";&|`<>*?[]{}()\"'\\")

_LUA_ENV_RE = re.compile(
    r'hl\.env\(\s*"((?:[^"\\]|\\.)*)"\s*,\s*"((?:[^"\\]|\\.)*)"\s*\)')
_LUA_EXEC_RE = re.compile(r'hl\.exec_cmd\(\s*"((?:[^"\\]|\\.)*)"\s*\)')
_HYPR_EXEC_RE = re.compile(r'^\s*(exec-once|exec)\s*=\s*(.*)$')
_HYPR_VAR_RE = re.compile(r'^\s*\$([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$')
_HYPR_SOURCE_RE = re.compile(r'^\s*source\s*=\s*(.+)$')
_VAR_REF_RE = re.compile(r'\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?')

# Lua reachability (Langkah 3). Only *literal* module/path strings are
# followed, and only when they resolve inside the rice directory.
_LUA_REQUIRE_RE = re.compile(
    r'\brequire\s*\(\s*(["\'])((?:[^"\\]|\\.)*)\1\s*\)'
    r'|\brequire\s*,\s*(["\'])((?:[^"\\]|\\.)*)\3')
_LUA_DOFILE_RE = re.compile(
    r'\b(?:dofile|loadfile)\s*\(\s*(["\'])((?:[^"\\]|\\.)*)\1\s*\)')

_LUA_ESCAPES = {
    "n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"',
    "a": "\a", "b": "\b", "f": "\f", "v": "\v", "0": "\0",
}


# ---------------------------------------------------------------------------
# Draft data model
# ---------------------------------------------------------------------------

@dataclass
class Field:
    """One draft value plus how sure the detector is of it."""

    value: Any = ""
    confidence: str = CONF_AUTO
    note: str = ""

    @property
    def needs_review(self) -> bool:
        return self.confidence in (CONF_GUESS, CONF_ASK)


@dataclass
class DraftComponent:
    name: str
    type: Field
    executable: Field = field(default_factory=Field)
    argv_match: Field = field(default_factory=lambda: Field([]))
    exact_argv: Field = field(default_factory=lambda: Field(False))
    start: Field = field(default_factory=Field)
    stop: Field = field(default_factory=Field)
    start_check: Field = field(default_factory=Field)
    reload: Field = field(default_factory=Field)
    config_link: Field = field(default_factory=Field)
    depends_on: Field = field(default_factory=lambda: Field([]))
    timeout: Field = field(default_factory=lambda: Field(10))
    optional: Field = field(default_factory=lambda: Field(False))
    source: str = ""

    def field_map(self) -> dict[str, Field]:
        return {k: getattr(self, k) for k in _FIELD_ORDER}

    def worst_confidence(self) -> str:
        return max((f.confidence for f in self.field_map().values()),
                   key=lambda c: _RANK.get(c, 0), default=CONF_AUTO)

    def to_manifest_dict(self) -> dict:
        """A plain dict for `tomlwrite.dump`.

        Empty/default values are dropped (TOML has no null), but `stop` is
        always emitted: an empty `stop` is what selects identity-based
        stopping in `lifecycle.stop_component`.
        """
        d: dict = {"name": self.name, "type": self.type.value}
        for key in ("executable", "argv_match", "exact_argv", "start",
                    "start_check", "reload", "config_link", "depends_on",
                    "optional", "timeout"):
            value = getattr(self, key).value
            if key == "timeout" and value == 10:
                continue
            if value in ("", [], False):
                continue
            d[key] = value
        d["stop"] = self.stop.value
        return d


@dataclass
class AutostartRef:
    file: str
    line: int
    kind: str          # "exec" | "exec-once" | "hl.exec_cmd"
    raw: str           # raw text as found (for Fase 4)
    command: str = ""  # extracted literal (unresolved), "" if dynamic
    confidence: str = CONF_AUTO
    reachable: bool = True   # False when only an archive/profile/backup file


@dataclass(frozen=True)
class LayoutLink:
    name: str
    target: str


@dataclass
class LayoutResult:
    name: str
    created: bool
    detail: str = ""


@dataclass
class LayoutInfo:
    form: str = "plain"        # "config" (a) | "plain" (b)
    config_root: str = "."     # relative to the rice dir


@dataclass
class ManifestDraft:
    name: str
    components: list[DraftComponent] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    autostart_refs: list[AutostartRef] = field(default_factory=list)
    layout: LayoutInfo = field(default_factory=LayoutInfo)


# ---------------------------------------------------------------------------
# Containment + bounded reading
# ---------------------------------------------------------------------------

def _inside(base_real: str, target_real: str) -> bool:
    try:
        return os.path.commonpath([base_real, target_real]) == base_real
    except ValueError:
        return False


def _read_text(path: str) -> tuple[str | None, str]:
    """Read a small text file. Returns (text, reason_if_skipped)."""
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            return None, "too_large"
        with open(path, "rb") as f:
            data = f.read(MAX_FILE_BYTES + 1)
    except OSError:
        return None, "unreadable"
    if len(data) > MAX_FILE_BYTES:
        return None, "too_large"
    if b"\x00" in data:
        return None, "binary"
    try:
        return data.decode("utf-8"), ""
    except UnicodeDecodeError:
        return None, "decode"


def _walk_files(rice_dir: str, rice_real: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Bounded, non-following walk. Returns ([(rel, abs)], notes)."""
    notes: list[str] = []
    out: list[tuple[str, str]] = []
    root_depth = os.path.normpath(rice_dir).count(os.sep)
    for dirpath, dirnames, filenames in os.walk(rice_dir, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        if os.path.normpath(dirpath).count(os.sep) - root_depth >= MAX_DEPTH:
            dirnames[:] = []
        keep = []
        for d in dirnames:
            p = os.path.join(dirpath, d)
            if os.path.islink(p) and not _inside(rice_real, os.path.realpath(p)):
                notes.append(
                    "symlink keluar folder rice (tidak diikuti): "
                    + os.path.relpath(p, rice_dir))
                continue
            keep.append(d)
        dirnames[:] = keep
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            if os.path.islink(p) and not _inside(rice_real, os.path.realpath(p)):
                notes.append(
                    "symlink keluar folder rice (tidak diikuti): "
                    + os.path.relpath(p, rice_dir))
                continue
            out.append((os.path.relpath(p, rice_dir), p))
            if len(out) >= MAX_FILES:
                notes.append(
                    f"batas pemindaian tercapai ({MAX_FILES} berkas); sisa dilewati")
                return out, notes
    return out, notes


# ---------------------------------------------------------------------------
# Text parsing (no evaluation)
# ---------------------------------------------------------------------------

def _unescape_lua(s: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s):
            nxt = s[i + 1]
            if nxt in _LUA_ESCAPES:
                out.append(_LUA_ESCAPES[nxt])
                i += 2
                continue
            if nxt == "x":
                try:
                    out.append(chr(int(s[i + 2:i + 4], 16)))
                    i += 4
                    continue
                except ValueError:
                    pass
            out.append(nxt)
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _strip_hypr_comment(rhs: str) -> str:
    in_q: str | None = None
    for i, ch in enumerate(rhs):
        if ch in "\"'":
            if in_q is None:
                in_q = ch
            elif in_q == ch:
                in_q = None
        elif ch == "#" and in_q is None:
            return rhs[:i]
    return rhs


def _resolve_vars(cmd: str, var_map: dict[str, str]) -> tuple[str, list[str]]:
    unresolved: list[str] = []

    def repl(m: re.Match) -> str:
        name = m.group(1)
        if name in var_map:
            return var_map[name]
        unresolved.append(name)
        return m.group(0)

    return _VAR_REF_RE.sub(repl, cmd), unresolved


def _tokenize(cmd: str) -> list[str]:
    try:
        return shlex.split(cmd)
    except ValueError:
        return cmd.split()


def _suspicious(token: str) -> bool:
    if "$" in token:
        return True
    return any(ch in _SUSPICIOUS_CHARS for ch in token)


def _split_commands(cmd: str) -> list[str]:
    """Split a shell-ish command line into independent sub-commands.

    Splits on `&&`, `||`, `;`, `|`, `&`, honouring single/double quotes so
    an operator inside a quoted argument does not split. Never evaluates.
    """
    parts: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(cmd):
        ch = cmd[i]
        if quote is not None:
            buf.append(ch)
            if ch == "\\" and i + 1 < len(cmd):
                buf.append(cmd[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        two = cmd[i:i + 2]
        if two in ("&&", "||"):
            parts.append("".join(buf))
            buf = []
            i += 2
            continue
        if ch in ";|&":
            parts.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


_WRAPPER_EXEC = frozenset({"exec", "setsid", "nohup"})
_DURATION_RE = re.compile(r"^\d+(\.\d+)?$")
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _strip_wrappers(tokens: list[str]) -> list[str]:
    """Drop leading wrappers so the real executable is first.

    Handles `sleep N`, `env VAR=...` (and a bare leading `VAR=...`),
    `exec`, `setsid`, `nohup`, `uwsm app [opts] -- CMD`, and
    `systemd-run [opts] -- CMD`. Returns [] for a command that is only a
    wrapper (e.g. `sleep .5`).
    """
    toks = list(tokens)
    while toks:
        head = toks[0]
        if _ENV_ASSIGN_RE.match(head):
            toks = toks[1:]
            continue
        if head in _WRAPPER_EXEC:
            toks = toks[1:]
            continue
        if head == "sleep":
            if len(toks) >= 2 and _DURATION_RE.match(toks[1]):
                toks = toks[2:]
            else:
                toks = toks[1:]
            continue
        if head == "env":
            toks = toks[1:]
            while toks and _ENV_ASSIGN_RE.match(toks[0]):
                toks = toks[1:]
            continue
        if head == "uwsm" and len(toks) >= 2 and toks[1] == "app":
            toks = toks[2:]
            while toks and toks[0] not in ("--",) and toks[0].startswith("-"):
                toks = toks[1:]
            if toks and toks[0] == "--":
                toks = toks[1:]
            continue
        if head == "systemd-run":
            rest = toks[1:]
            if "--" in rest:
                rest = rest[rest.index("--") + 1:]
            else:
                while rest and rest[0].startswith("-"):
                    rest = rest[1:]
            toks = rest
            continue
        break
    return toks


def _under_ignored_dir(rel: str) -> bool:
    """True if any parent directory of `rel` is an archive/backup/example."""
    parts = rel.split(os.sep)
    return any(p in IGNORED_DIRS for p in parts[:-1])


def _real_rel(path: str, rice_real: str, real_to_rel: dict[str, str]) -> str | None:
    """Resolve `path` to a rice-relative text file, or None.

    Containment: the realpath must be inside the rice directory, and it
    must be a file we actually loaded (i.e. a readable .lua/.conf).
    """
    try:
        rp = os.path.realpath(path)
    except OSError:
        return None
    if not _inside(rice_real, rp):
        return None
    return real_to_rel.get(rp)


def _module_candidates(mod: str, base_dir: str, rice_dir: str) -> list[str]:
    """Filesystem candidates for a Lua `require("<mod>")`."""
    rel = mod.replace(".", os.sep)
    out = []
    for base in (base_dir, rice_dir):
        out.append(os.path.join(base, rel + ".lua"))
        out.append(os.path.join(base, rel, "init.lua"))
    return out


def _refs_from_text(text: str, rel: str, rice_dir: str,
                    rice_real: str, real_to_rel: dict[str, str]) -> list[str]:
    """Rice-relative files this text pulls in (`source=`/`require`/`dofile`)."""
    refs: list[str] = []
    base_dir = os.path.join(rice_dir, os.path.dirname(rel))
    for line in text.splitlines():
        s = _HYPR_SOURCE_RE.match(line)
        if s:
            rhs = _strip_hypr_comment(s.group(1)).strip().strip('"').strip("'")
            if rhs and "$" not in rhs:
                cand = (rhs if os.path.isabs(rhs) or rhs.startswith("~")
                        else os.path.join(base_dir, rhs))
                r = _real_rel(os.path.expanduser(cand), rice_real, real_to_rel)
                if r:
                    refs.append(r)
    for m in _LUA_REQUIRE_RE.finditer(text):
        mod = _unescape_lua(m.group(2) or m.group(4) or "")
        if not mod or "$" in mod or " " in mod:
            continue
        for cand in _module_candidates(mod, base_dir, rice_dir):
            r = _real_rel(cand, rice_real, real_to_rel)
            if r:
                refs.append(r)
    for m in _LUA_DOFILE_RE.finditer(text):
        p = _unescape_lua(m.group(2))
        if not p or "$" in p:
            continue
        cand = (p if os.path.isabs(p) or p.startswith("~")
                else os.path.join(base_dir, p))
        r = _real_rel(os.path.expanduser(cand), rice_real, real_to_rel)
        if r:
            refs.append(r)
    return refs


def _find_entrypoints(cfg: str, rice_dir: str, rice_real: str,
                      real_to_rel: dict[str, str]) -> list[str]:
    """Existing, readable Hyprland entrypoints (rice-relative), ordered."""
    cfg_rel = os.path.relpath(cfg, rice_dir)
    out: list[str] = []
    seen: set[str] = set()
    for cand in ENTRYPOINT_CANDIDATES:
        rel = os.path.normpath(os.path.join(cfg_rel, cand))
        if rel in seen or _under_ignored_dir(rel):
            continue
        r = _real_rel(os.path.join(rice_dir, rel), rice_real, real_to_rel)
        if r is not None:
            seen.add(r)
            out.append(r)
    return out


def _reachable(entrypoints: list[str], texts: dict[str, str], rice_dir: str,
               rice_real: str, real_to_rel: dict[str, str]) -> set[str]:
    """BFS the reachability graph from the entrypoints (cycle-safe, bounded)."""
    seen: set[str] = set()
    queue: deque[tuple[str, int]] = deque((e, 0) for e in entrypoints)
    while queue:
        rel, depth = queue.popleft()
        if rel in seen:
            continue
        seen.add(rel)
        if depth >= MAX_SOURCE_DEPTH:
            continue
        text = texts.get(rel)
        if text is None:
            continue
        for nxt in _refs_from_text(text, rel, rice_dir, rice_real, real_to_rel):
            if nxt not in seen and not _under_ignored_dir(nxt):
                queue.append((nxt, depth + 1))
    return seen


def _parse_texts(texts: dict[str, str], reachable: set[str]) -> tuple[dict[str, str], list[AutostartRef]]:
    """Resolve `$vars` (from reachable files) and collect autostart refs.

    Refs from unreachable files are still recorded (with
    `reachable=False`, for Fase 4) but never create components.
    """
    var_map: dict[str, str] = {}
    for rel, text in texts.items():
        if rel not in reachable:
            continue
        for line in text.splitlines():
            m = _LUA_ENV_RE.search(line)
            if m:
                var_map[m.group(1)] = _unescape_lua(m.group(2))
            if rel.endswith(".conf"):
                v = _HYPR_VAR_RE.match(line)
                if v:
                    var_map[v.group(1)] = v.group(2).strip().strip('"')

    refs: list[AutostartRef] = []
    for rel, text in texts.items():
        reach = rel in reachable
        for i, line in enumerate(text.splitlines(), 1):
            commented = line.lstrip().startswith("--")
            m = _LUA_EXEC_RE.search(line)
            if m and not commented:
                refs.append(AutostartRef(rel, i, "hl.exec_cmd", line.strip(),
                                         _unescape_lua(m.group(1)), CONF_AUTO,
                                         reach))
                continue
            if "hl.exec_cmd(" in line and not commented:
                refs.append(AutostartRef(rel, i, "hl.exec_cmd", line.strip(),
                                         "", CONF_ASK, reach))
                continue
            h = _HYPR_EXEC_RE.match(line)
            if h:
                rhs = _strip_hypr_comment(h.group(2)).strip()
                refs.append(AutostartRef(rel, i, h.group(1), line.strip(),
                                         rhs.strip('"'), CONF_AUTO, reach))
    return var_map, refs


def _load_texts(files: Iterable[tuple[str, str]], rice_real: str,
                notes: list[str]) -> dict[str, str]:
    texts: dict[str, str] = {}
    for rel, abspath in files:
        if os.path.splitext(rel)[1].lower() not in (".lua", ".conf"):
            continue
        if not _inside(rice_real, os.path.realpath(abspath)):
            notes.append(f"symlink keluar folder rice (tidak dibaca): {rel}")
            continue
        text, _reason = _read_text(abspath)
        if text is not None:
            texts[rel] = text
    return texts


# ---------------------------------------------------------------------------
# Component assembly
# ---------------------------------------------------------------------------

def _comp(name: str, ctype: str, exe: str, start: str, argv: list[str],
          argv_conf: str, config_link: str, link_conf: str,
          start_check: str, source: str, start_conf: str = CONF_AUTO,
          *, exact: bool = False,
          exact_conf: str = CONF_AUTO, argv_note: str = "") -> DraftComponent:
    return DraftComponent(
        name=name,
        type=Field(ctype, CONF_AUTO),
        executable=Field(exe, CONF_AUTO),
        argv_match=Field(list(argv), argv_conf, argv_note),
        exact_argv=Field(bool(exact), exact_conf),
        start=Field(start, start_conf),
        stop=Field("", CONF_AUTO),
        start_check=Field(start_check, CONF_AUTO),
        config_link=Field(config_link, link_conf),
        source=source,
    )


def _add(comps: dict[str, DraftComponent], dc: DraftComponent) -> None:
    cur = comps.get(dc.name)
    if cur is None:
        comps[dc.name] = dc
        return
    if _RANK[dc.argv_match.confidence] < _RANK[cur.argv_match.confidence]:
        cur.argv_match = dc.argv_match
        cur.exact_argv = dc.exact_argv
        cur.start = dc.start
        cur.executable = dc.executable
    if not cur.config_link.value and dc.config_link.value:
        cur.config_link = dc.config_link
    if not cur.start_check.value and dc.start_check.value:
        cur.start_check = dc.start_check
    if not cur.source:
        cur.source = dc.source


def _start_check_for(name: str) -> str:
    return f"pgrep -x {name}" if name in ("waybar", "quickshell", "hypridle") else ""


def _component_from_command(raw: str, ref: AutostartRef,
                            var_map: dict[str, str]) -> DraftComponent | None:
    """Build a component from one literal sub-command, or None.

    Identity is derived from the literal argv of the sub-command:
    a command with no arguments becomes an exact no-argument identity
    (`exact_argv=true`), a command with literal arguments uses an
    `argv_match` subset. Unresolved variables or dynamic quoting make the
    fields `ask` — we never invent an identity.
    """
    resolved, unresolved = _resolve_vars(raw, var_map)
    tokens = _strip_wrappers(_tokenize(resolved))
    if not tokens:
        return None
    exe = os.path.basename(tokens[0])
    if exe not in KNOWN_BINARIES:
        return None
    name, ctype = KNOWN_BINARIES[exe]
    argv = tokens[1:]
    literal = not unresolved and not any(_suspicious(t) for t in tokens)
    argv_conf = CONF_AUTO if literal else CONF_ASK
    start_conf = CONF_ASK if unresolved else CONF_AUTO
    exact = literal and not argv
    # Langkah 0: a known *client* subcommand (`swww restore`, `swww img …`)
    # is a one-shot command against a daemon, not the daemon itself. Keep
    # the argv for visibility but never let it pass as a confident identity.
    argv_note = ""
    client_subs = KNOWN_CLIENT_SUBCOMMANDS.get(exe)
    if client_subs is not None and argv and argv[0] in client_subs:
        argv_conf = CONF_ASK
        argv_note = _CLIENT_NOTE
    start = " ".join(tokens)
    return _comp(name, ctype, exe, start, argv, argv_conf, "", CONF_AUTO,
                 _start_check_for(name), f"autostart:{ref.file}:{ref.line}",
                 start_conf, exact=exact, exact_conf=CONF_AUTO,
                 argv_note=argv_note)


def _components_from_autostart(ref: AutostartRef,
                               var_map: dict[str, str]) -> list[DraftComponent]:
    """Components from one autostart line (a line may run several commands)."""
    if not ref.command or not ref.reachable:
        return []
    out: list[DraftComponent] = []
    for piece in _split_commands(ref.command):
        dc = _component_from_command(piece, ref, var_map)
        if dc is not None:
            out.append(dc)
    return out


def _add_dir_signals(comps: dict[str, DraftComponent], cfg: str, draft: ManifestDraft) -> None:
    # Compositor.
    has_hypr = any(os.path.isfile(os.path.join(cfg, p)) for p in (
        "hypr/hyprland.conf", "hypr/hyprland.lua", "hyprland.conf", "hyprland.lua"))
    has_hypr_dir = os.path.isdir(os.path.join(cfg, "hypr"))
    if has_hypr or has_hypr_dir:
        link_conf = CONF_AUTO if has_hypr_dir else CONF_GUESS
        _add(comps, DraftComponent(
            name="hyprland",
            type=Field("compositor", CONF_AUTO),
            executable=Field("", CONF_AUTO),
            argv_match=Field([], CONF_AUTO),
            start=Field("true", CONF_AUTO),
            stop=Field("true", CONF_AUTO),
            reload=Field("hyprctl reload", CONF_AUTO),
            config_link=Field("~/.config/hypr", link_conf),
            source="dir:hypr",
        ))

    # waybar.
    if (os.path.isdir(os.path.join(cfg, "waybar"))
            or os.path.isfile(os.path.join(cfg, "waybar", "config.jsonc"))
            or os.path.isfile(os.path.join(cfg, "waybar", "style.css"))):
        _add(comps, _comp("waybar", "bar", "waybar", "waybar", [], CONF_ASK,
                          "~/.config/waybar", CONF_AUTO, "pgrep -x waybar",
                          "dir:waybar", exact=True))

    # quickshell.
    qs_root = os.path.join(cfg, "quickshell")
    if os.path.isdir(qs_root):
        try:
            entries = sorted(os.listdir(qs_root))
        except OSError:
            entries = []
        cands = [d for d in entries
                 if os.path.isfile(os.path.join(qs_root, d, "shell.qml"))]
        if len(cands) == 1:
            cfg_name = cands[0]
            _add(comps, _comp("quickshell", "shell", "qs", f"qs -c {cfg_name}",
                              ["-c", cfg_name], CONF_AUTO, "~/.config/quickshell",
                              CONF_AUTO, "pgrep -x qs",
                              "dir:quickshell/shell.qml"))
        else:
            if len(cands) > 1:
                draft.notes.append(
                    f"beberapa config quickshell ditemukan ({', '.join(cands)}); "
                    f"pilih satu")
            _add(comps, _comp("quickshell", "shell", "qs", "qs", [], CONF_ASK,
                              "~/.config/quickshell", CONF_AUTO, "pgrep -x qs",
                              "dir:quickshell", exact=True))

    # hypridle (config lives inside ~/.config/hypr, so no separate link).
    if (os.path.isfile(os.path.join(cfg, "hypridle.conf"))
            or os.path.isfile(os.path.join(cfg, "hypr", "hypridle.conf"))):
        _add(comps, _comp("hypridle", "system", "hypridle", "hypridle", [],
                          CONF_ASK, "", CONF_AUTO, "pgrep -x hypridle",
                          "dir:hypridle.conf", exact=True))

    # Notifications / wallpaper daemons by config dir.
    for dirname, (name, ctype) in DIR_SIGNALS.items():
        if os.path.isdir(os.path.join(cfg, dirname)):
            _add(comps, _comp(name, ctype, name, name, [], CONF_ASK,
                              f"~/.config/{dirname}", CONF_GUESS, "",
                              f"dir:{dirname}", exact=True))

    # hyprpaper config file (covered by ~/.config/hypr when it is managed).
    if (os.path.isfile(os.path.join(cfg, "hyprpaper.conf"))
            or os.path.isfile(os.path.join(cfg, "hypr", "hyprpaper.conf"))):
        _add(comps, _comp("hyprpaper", "wallpaper", "hyprpaper", "hyprpaper",
                          [], CONF_ASK, "", CONF_AUTO, "", "dir:hyprpaper.conf",
                          exact=True))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def detect(rice_dir: str, *, roots: "Roots | None" = None) -> ManifestDraft:
    """Analyse `rice_dir` read-only and return a `ManifestDraft`."""
    rice_dir = os.path.normpath(rice_dir)
    rice_real = os.path.realpath(rice_dir)
    draft = ManifestDraft(name=os.path.basename(rice_dir))

    dotconfig = os.path.join(rice_dir, ".config")
    if os.path.isdir(dotconfig):
        cfg = dotconfig
        draft.layout.form = "config"
        draft.layout.config_root = ".config"
    else:
        cfg = rice_dir
        draft.layout.form = "plain"
        draft.layout.config_root = "."

    files, walk_notes = _walk_files(rice_dir, rice_real)
    draft.notes.extend(walk_notes)

    texts = _load_texts(files, rice_real, draft.notes)

    # Map realpath -> rice-relative name so source/require resolution can
    # only ever point at files we actually loaded (containment).
    real_to_rel: dict[str, str] = {}
    for rel, abspath in files:
        if rel in texts:
            real_to_rel[os.path.realpath(abspath)] = rel

    entrypoints = _find_entrypoints(cfg, rice_dir, rice_real, real_to_rel)
    reachable = _reachable(entrypoints, texts, rice_dir, rice_real, real_to_rel)

    var_map, refs = _parse_texts(texts, reachable)
    draft.autostart_refs = refs

    comps: dict[str, DraftComponent] = {}
    autostart_names: set[str] = set()
    for ref in refs:
        for dc in _components_from_autostart(ref, var_map):
            autostart_names.add(dc.name)
            _add(comps, dc)

    _add_dir_signals(comps, cfg, draft)

    if not entrypoints:
        draft.notes.append(
            "tidak ada entrypoint Hyprland (hyprland.lua/.conf) yang bisa dibaca; "
            "autostart tidak dipindai (rice mungkin butuh materialisasi — Fase 4)")
    else:
        unreachable_refs = [r for r in refs if not r.reachable and r.file]
        if unreachable_refs:
            files_seen = []
            for r in unreachable_refs:
                if r.file not in files_seen:
                    files_seen.append(r.file)
            draft.notes.append(
                "autostart di berkas tak terjangkau diabaikan (arsip/profil "
                "tidak aktif): " + ", ".join(files_seen[:8]))

    for name in sorted(autostart_names):
        dc = comps[name]
        draft.notes.append(
            f"autostart bentrok: {name} juga dijalankan oleh config rice "
            f"({dc.source}); default Opsi B — biarkan config yang menjalankan, "
            f"switcher hanya stop")

    if not comps:
        draft.notes.append(
            "tidak ada berkas hyprland/config yang dikenal — rice ini mungkin "
            "bukan rice Hyprland")

    draft.components = sorted(
        comps.values(),
        key=lambda c: (0 if c.type.value == "compositor" else 1, c.name))
    return draft


# ---------------------------------------------------------------------------
# Layout normalization (DESIGN §4.3)
# ---------------------------------------------------------------------------

def plan_layout_normalization(draft: ManifestDraft, rice_dir: str) -> list[LayoutLink]:
    """Relative symlinks to add so a form-(a) rice matches the swap target.

    The swap target is `<rice>/<basename(config_link)>` (switch.py), so a
    repo laid out as `.config/hypr` needs `<rice>/hypr -> .config/hypr`.
    Only real directories under `.config` are planned.
    """
    if draft.layout.form != "config":
        return []
    links: list[LayoutLink] = []
    seen: set[str] = set()
    for c in draft.components:
        cl = c.config_link.value
        if not cl.startswith("~/.config/"):
            continue
        rel = cl[len("~/.config/"):].strip("/")
        if not rel or "/" in rel or rel in seen:
            continue
        src = os.path.join(rice_dir, ".config", rel)
        if not os.path.isdir(src):
            continue
        # Even if the target escapes, plan it; apply refuses and reports.
        seen.add(rel)
        links.append(LayoutLink(name=rel, target=f".config/{rel}"))
    return links


def _register_exclude(rice_dir: str, names: list[str]) -> None:
    info = os.path.join(rice_dir, ".git", "info")
    if not os.path.isdir(info):
        return
    path = os.path.join(info, "exclude")
    existing = ""
    if os.path.isfile(path):
        with open(path, encoding="utf-8", errors="replace") as f:
            existing = f.read()
    have = set(existing.split())
    add = [n for n in names if n not in have]
    if not add:
        return
    with open(path, "a", encoding="utf-8") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write("# added by rice-switcher generate (layout normalization)\n")
        for n in add:
            f.write(n + "\n")


def apply_layout_normalization(
    draft: ManifestDraft, rice_dir: str, *, dry_run: bool = False,
) -> list[LayoutResult]:
    """Create the planned relative symlinks, safely and idempotently.

    * never overwrites an existing path;
    * refuses a target whose realpath escapes the rice dir;
    * registers created links in `<rice>/.git/info/exclude` (skipped when
      the rice is not a git repo).
    """
    rice_real = os.path.realpath(rice_dir)
    results: list[LayoutResult] = []
    created_names: list[str] = []
    for link in plan_layout_normalization(draft, rice_dir):
        link_path = os.path.join(rice_dir, link.name)
        target_abs = os.path.join(rice_dir, link.target)
        if not _inside(rice_real, os.path.realpath(target_abs)):
            results.append(LayoutResult(link.name, False,
                                        "target escapes the rice dir; refused"))
            continue
        if os.path.lexists(link_path):
            results.append(LayoutResult(link.name, False,
                                        "already exists; left untouched"))
            continue
        if dry_run:
            results.append(LayoutResult(link.name, True, f"would create -> {link.target}"))
            continue
        try:
            os.symlink(link.target, link_path)
        except OSError as e:
            results.append(LayoutResult(link.name, False, f"symlink failed: {e}"))
            continue
        results.append(LayoutResult(link.name, True, f"-> {link.target}"))
        created_names.append(link.name)

    if created_names and not dry_run:
        _register_exclude(rice_dir, created_names)
    return results
