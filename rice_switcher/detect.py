"""Detect a rice's components from its config tree (DESIGN-onboarding-tui.md §4).

Produces a `ManifestDraft`, never a final manifest: every field carries a
confidence (`auto` / `guess` / `ask`) so the review step (Fase 3) can
highlight what a human must decide.

Hard rules (DESIGN §1, §10):

* **Text only.** No `eval`, no Lua interpreter, no shell, no subprocess.
  Autostart lines are matched with regexes; only a pure string literal is
  turned into a command. A concatenation or a dynamic expression is
  reported as `ask` and never guessed.
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

# binary -> (component name, type). Aligned with manifest.VALID_TYPES.
KNOWN_BINARIES = {
    "qs": ("quickshell", "shell"),
    "quickshell": ("quickshell", "shell"),
    "waybar": ("waybar", "bar"),
    "hypridle": ("hypridle", "system"),
    "dunst": ("dunst", "notifications"),
    "mako": ("mako", "notifications"),
    "swaync": ("swaync", "notifications"),
    "hyprpaper": ("hyprpaper", "wallpaper"),
    "swww": ("swww", "wallpaper"),
    "awww": ("awww", "wallpaper"),
    "mpvpaper": ("mpvpaper", "wallpaper"),
    "hyprlock": ("hyprlock", "locker"),
}

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

_FIELD_ORDER = (
    "type", "executable", "argv_match", "start", "stop", "start_check",
    "reload", "config_link", "depends_on", "optional", "timeout",
)

_SUSPICIOUS_CHARS = set(";&|`<>*?[]{}()\"'\\")

_LUA_ENV_RE = re.compile(
    r'hl\.env\(\s*"((?:[^"\\]|\\.)*)"\s*,\s*"((?:[^"\\]|\\.)*)"\s*\)')
_LUA_EXEC_RE = re.compile(r'hl\.exec_cmd\(\s*"((?:[^"\\]|\\.)*)"\s*\)')
_HYPR_EXEC_RE = re.compile(r'^\s*(exec-once|exec)\s*=\s*(.*)$')
_HYPR_VAR_RE = re.compile(r'^\s*\$([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$')
_HYPR_SOURCE_RE = re.compile(r'^\s*source\s*=\s*(.+)$')
_VAR_REF_RE = re.compile(r'\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?')

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
        for key in ("executable", "argv_match", "start", "start_check",
                    "reload", "config_link", "depends_on", "optional",
                    "timeout"):
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


def _parse_texts(texts: dict[str, str]) -> tuple[dict[str, str], list[AutostartRef]]:
    var_map: dict[str, str] = {}
    for rel, text in texts.items():
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
        for i, line in enumerate(text.splitlines(), 1):
            commented = line.lstrip().startswith("--")
            m = _LUA_EXEC_RE.search(line)
            if m and not commented:
                refs.append(AutostartRef(rel, i, "hl.exec_cmd", line.strip(),
                                         _unescape_lua(m.group(1)), CONF_AUTO))
                continue
            if "hl.exec_cmd(" in line and not commented:
                refs.append(AutostartRef(rel, i, "hl.exec_cmd", line.strip(),
                                         "", CONF_ASK))
                continue
            h = _HYPR_EXEC_RE.match(line)
            if h:
                rhs = _strip_hypr_comment(h.group(2)).strip()
                refs.append(AutostartRef(rel, i, h.group(1), line.strip(),
                                         rhs.strip('"'), CONF_AUTO))
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
          start_check: str, source: str, start_conf: str = CONF_AUTO) -> DraftComponent:
    return DraftComponent(
        name=name,
        type=Field(ctype, CONF_AUTO),
        executable=Field(exe, CONF_AUTO),
        argv_match=Field(list(argv), argv_conf),
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
        cur.start = dc.start
        cur.executable = dc.executable
    if not cur.config_link.value and dc.config_link.value:
        cur.config_link = dc.config_link
    if not cur.start_check.value and dc.start_check.value:
        cur.start_check = dc.start_check
    if not cur.source:
        cur.source = dc.source


def _component_from_autostart(ref: AutostartRef, var_map: dict[str, str]):
    if not ref.command:
        return None
    resolved, unresolved = _resolve_vars(ref.command, var_map)
    tokens = _tokenize(resolved)
    if not tokens:
        return None
    exe = os.path.basename(tokens[0])
    if exe not in KNOWN_BINARIES:
        return None
    name, ctype = KNOWN_BINARIES[exe]
    argv = tokens[1:]
    argv_conf = CONF_AUTO
    if not argv or unresolved or any(_suspicious(t) for t in argv):
        argv_conf = CONF_ASK
    start_conf = CONF_ASK if unresolved else CONF_AUTO
    start_check = f"pgrep -x {exe}" if name in ("waybar", "quickshell", "hypridle") else ""
    return _comp(name, ctype, exe, resolved, argv, argv_conf, "", CONF_AUTO,
                 start_check, f"autostart:{ref.file}:{ref.line}", start_conf)


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
                          "dir:waybar"))

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
                              "dir:quickshell"))

    # hypridle (config lives inside ~/.config/hypr, so no separate link).
    if (os.path.isfile(os.path.join(cfg, "hypridle.conf"))
            or os.path.isfile(os.path.join(cfg, "hypr", "hypridle.conf"))):
        _add(comps, _comp("hypridle", "system", "hypridle", "hypridle", [],
                          CONF_ASK, "", CONF_AUTO, "pgrep -x hypridle",
                          "dir:hypridle.conf"))

    # Notifications / wallpaper daemons by config dir.
    for dirname, (name, ctype) in DIR_SIGNALS.items():
        if os.path.isdir(os.path.join(cfg, dirname)):
            _add(comps, _comp(name, ctype, name, name, [], CONF_ASK,
                              f"~/.config/{dirname}", CONF_GUESS, "",
                              f"dir:{dirname}"))

    # hyprpaper config file (covered by ~/.config/hypr when it is managed).
    if (os.path.isfile(os.path.join(cfg, "hyprpaper.conf"))
            or os.path.isfile(os.path.join(cfg, "hypr", "hyprpaper.conf"))):
        _add(comps, _comp("hyprpaper", "wallpaper", "hyprpaper", "hyprpaper",
                          [], CONF_ASK, "", CONF_AUTO, "", "dir:hyprpaper.conf"))


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
    var_map, refs = _parse_texts(texts)
    draft.autostart_refs = refs

    comps: dict[str, DraftComponent] = {}
    autostart_names: set[str] = set()
    for ref in refs:
        dc = _component_from_autostart(ref, var_map)
        if dc is not None:
            autostart_names.add(dc.name)
            _add(comps, dc)

    _add_dir_signals(comps, cfg, draft)

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
