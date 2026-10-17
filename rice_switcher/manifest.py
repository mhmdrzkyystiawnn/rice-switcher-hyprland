"""Manifest loader and dataclasses.

PLAN.md §2.0 (field schema) + §2g (process identity). Pure data: no
side-effects, no I/O beyond reading the file. Validation is in
`validate.py`; computation is in `managed_set.py`.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# Component type vocabulary. PLAN.md §2.0.
VALID_TYPES = frozenset({
    "bar", "shell", "notifications", "locker", "wallpaper", "compositor",
    "terminal", "launcher", "theme_generator", "color_daemon", "system",
    "clipboard",
})


@dataclass
class Component:
    name: str
    type: str
    executable: str = ""
    argv_match: list[str] = field(default_factory=list)
    exact_argv: bool = False
    start: str = ""
    stop: str = "true"
    stop_check: str = ""
    start_check: str = ""
    reload: str = ""
    config_link: str = ""
    depends_on: list[str] = field(default_factory=list)
    health_check: str = ""
    optional: bool = False
    timeout: int = 10
    match: str = ""

    def identity(self) -> tuple[str, tuple[str, ...]]:
        """Return the (executable, argv_match) tuple used for /proc matching.

        See PLAN.md §2g. If `executable` is empty (default), the
        component's logical name is used. This makes the identity
        derivation stable without requiring every manifest to set the
        field, but it does mean two components with the same logical
        name and the same default executable are conflated; manifests
        that need disambiguation must set `executable` and `argv_match`.
        """
        exe = self.executable or self.name
        return (exe, tuple(self.argv_match))


@dataclass
class Theme:
    cursor_name: str = ""
    cursor_size: int = 0
    gtk_theme: str = ""
    icon_theme: str = ""
    font_main: str = ""
    font_mono: str = ""
    font_size: str = ""
    apply: str = ""


@dataclass
class Meta:
    name: str = ""
    description: str = ""
    author: str = ""
    created: str = ""
    parent: str = ""   # MVP: ignored at parse time


@dataclass
class Manifest:
    """A parsed rice.toml manifest."""
    meta: Meta
    components: list[Component]
    hooks_pre: list[str] = field(default_factory=list)
    hooks_post: list[str] = field(default_factory=list)
    theme: Theme = field(default_factory=Theme)
    wallpaper: str = ""
    config_dir: str = "."
    run: dict[str, Any] = field(default_factory=dict)
    source_path: str = ""   # path the manifest was loaded from
    source_sha256: str = ""  # hex digest, for trust cache (PLAN.md §2h)

    @property
    def name(self) -> str:
        return self.meta.name

    def rice_root(self, rice_home: str) -> str:
        """Absolute path of this rice's directory on disk."""
        return str(Path(rice_home) / self.meta.name)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class ManifestError(ValueError):
    """Raised when a manifest file is malformed or violates the schema.

    PLAN.md §2.0: the parser is strict. The CLI is responsible for
    formatting the error for the user.
    """


def _coerce_component(raw: dict, idx: int) -> Component:
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        raise ManifestError(f"components[{idx}].name is required and must be a non-empty string")
    ctype = raw.get("type")
    if not isinstance(ctype, str) or ctype not in VALID_TYPES:
        raise ManifestError(
            f"components[{idx}] (name={name!r}): type={ctype!r} is invalid; "
            f"must be one of {sorted(VALID_TYPES)}"
        )
    argv = raw.get("argv_match", [])
    if not isinstance(argv, list) or not all(isinstance(x, str) for x in argv):
        raise ManifestError(f"components[{idx}] (name={name!r}): argv_match must be list[str]")
    exact_argv = raw.get("exact_argv", False)
    if not isinstance(exact_argv, bool):
        raise ManifestError(f"components[{idx}] (name={name!r}): exact_argv must be bool")
    deps = raw.get("depends_on", [])
    if not isinstance(deps, list) or not all(isinstance(x, str) for x in deps):
        raise ManifestError(f"components[{idx}] (name={name!r}): depends_on must be list[str]")
    timeout = raw.get("timeout", 10)
    if not isinstance(timeout, int) or timeout < 0:
        raise ManifestError(f"components[{idx}] (name={name!r}): timeout must be non-negative int")
    return Component(
        name=name,
        type=ctype,
        executable=raw.get("executable", ""),
        argv_match=argv,
        exact_argv=exact_argv,
        start=raw.get("start", ""),
        stop=raw.get("stop", "true"),
        stop_check=raw.get("stop_check", ""),
        start_check=raw.get("start_check", ""),
        reload=raw.get("reload", ""),
        config_link=raw.get("config_link", ""),
        depends_on=deps,
        health_check=raw.get("health_check", ""),
        optional=bool(raw.get("optional", False)),
        timeout=timeout,
        match=raw.get("match", ""),
    )


def _coerce_theme(raw: dict) -> Theme:
    cursor_size = raw.get("cursor_size", 0)
    if not isinstance(cursor_size, int):
        raise ManifestError(f"theme.cursor_size must be int, got {type(cursor_size).__name__}")
    return Theme(
        cursor_name=raw.get("cursor_name", ""),
        cursor_size=cursor_size,
        gtk_theme=raw.get("gtk_theme", ""),
        icon_theme=raw.get("icon_theme", ""),
        font_main=raw.get("font_main", ""),
        font_mono=raw.get("font_mono", ""),
        font_size=str(raw.get("font_size", "")),
        apply=raw.get("apply", ""),
    )


def _coerce_hooks(raw) -> tuple[list[str], list[str]]:
    if raw is None:
        return [], []
    if not isinstance(raw, dict):
        raise ManifestError("hooks must be a table")
    pre = raw.get("pre", [])
    post = raw.get("post", [])
    for label, value in (("pre", pre), ("post", post)):
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise ManifestError(f"hooks.{label} must be list[str]")
    return list(pre), list(post)


def parse_manifest(data: dict, source_path: str = "", source_sha256: str = "") -> Manifest:
    """Parse a dict (already deserialised from TOML) into a Manifest.

    Use `load_manifest(path)` for the full file-read + hash path.
    """
    if not isinstance(data, dict):
        raise ManifestError("manifest root must be a table")

    meta_raw = data.get("meta")
    if not isinstance(meta_raw, dict):
        raise ManifestError("[meta] table is required")
    name = meta_raw.get("name")
    if not isinstance(name, str) or not name:
        raise ManifestError("[meta].name is required and must be a non-empty string")
    meta = Meta(
        name=name,
        description=str(meta_raw.get("description", "")),
        author=str(meta_raw.get("author", "")),
        created=str(meta_raw.get("created", "")),
        parent=str(meta_raw.get("parent", "")),
    )

    components_raw = data.get("components", [])
    if not isinstance(components_raw, list):
        raise ManifestError("components must be an array")
    components = [_coerce_component(c, i) for i, c in enumerate(components_raw)]

    # Duplicate-name check
    seen: set[str] = set()
    for c in components:
        if c.name in seen:
            raise ManifestError(f"duplicate component name: {c.name!r}")
        seen.add(c.name)

    pre, post = _coerce_hooks(data.get("hooks"))
    theme = _coerce_theme(data.get("theme", {}) or {})

    wallpaper = str(data.get("wallpaper", ""))
    config_dir = str(data.get("config_dir", "."))
    run_raw = data.get("run", {}) or {}
    if not isinstance(run_raw, dict):
        raise ManifestError("[run] must be a table")
    # Keep only string values for run
    run = {k: v for k, v in run_raw.items() if isinstance(v, str)}

    return Manifest(
        meta=meta,
        components=components,
        hooks_pre=pre,
        hooks_post=post,
        theme=theme,
        wallpaper=wallpaper,
        config_dir=config_dir,
        run=run,
        source_path=source_path,
        source_sha256=source_sha256,
    )


def load_manifest(path: str) -> Manifest:
    """Read, hash, and parse a rice.toml file."""
    import hashlib
    p = Path(path)
    raw = p.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    data = tomllib.loads(raw.decode("utf-8"))
    return parse_manifest(data, source_path=str(p), source_sha256=sha)


def list_manifests(rice_home: str) -> list[tuple[str, str]]:
    """Return [(name, manifest_path)] for every rice directory.

    A directory is a rice if it contains a `rice.toml` file. Read-only.
    """
    out: list[tuple[str, str]] = []
    root = Path(rice_home)
    if not root.is_dir():
        return out
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        manifest = child / "rice.toml"
        if manifest.is_file():
            out.append((child.name, str(manifest)))
    return out
