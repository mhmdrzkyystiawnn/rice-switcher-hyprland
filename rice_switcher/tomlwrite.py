"""Minimal TOML writer for manifest drafts (DESIGN-onboarding-tui.md §4.5).

`tomllib` is read-only, so Fase 2 needs a small serialiser. It covers
exactly the manifest value model: `str`, `int`, `bool`, `float`,
`list[str]`, nested `dict`, and `list[dict]` (array-of-tables). Anything
else — including `None`, because TOML has no null — raises
`TomlWriteError`; callers drop unsupported keys themselves.

Key order inside every table is: scalars and arrays-of-scalars first,
then sub-tables, then arrays-of-tables (PLAN.md §4.1). That order is not
just cosmetic: in TOML a value must be written under its own table before
any sub-table begins, so a stray scalar after a `[sub]` header would land
in the wrong table.

The writer is text-only and never evaluates anything. Comments are
optional and passed in by the caller (e.g. `# TODO(ask): …` for fields
the detector could not decide); their content is sanitised so untrusted
input cannot splice in a new line.
"""

from __future__ import annotations

import math
import re
from typing import Mapping, Sequence

# Bare keys: A-Za-z0-9_-. Anything else must be quoted.
_BARE_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# Control characters that must not appear raw in a comment.
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")

_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}


class TomlWriteError(ValueError):
    """Raised for a value the writer cannot represent (e.g. `None`)."""


def escape_string(s: str) -> str:
    """Return `s` as a quoted TOML basic string.

    Escapes the quote, the backslash, the named control escapes, and any
    other C0/DEL character as `\\uXXXX`. Unicode is left literal (the file
    is written as UTF-8).
    """
    if not isinstance(s, str):
        raise TomlWriteError(f"expected str, got {type(s).__name__}")
    out = ['"']
    for ch in s:
        esc = _ESCAPES.get(ch)
        if esc is not None:
            out.append(esc)
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def sanitize_comment(text) -> str:
    """Return comment-safe text: no newlines, no control characters.

    Newlines/tabs/controls become spaces and runs of whitespace collapse,
    so untrusted input cannot end the comment line and inject TOML.
    """
    scrubbed = _CTRL_RE.sub(" ", str(text))
    return " ".join(scrubbed.split())


# ---------------------------------------------------------------------------
# Value classification
# ---------------------------------------------------------------------------

def _is_aot(value) -> bool:
    """True for a non-empty list whose every element is a dict."""
    return (
        isinstance(value, list)
        and len(value) > 0
        and all(isinstance(x, dict) for x in value)
    )


def _is_plain(value) -> bool:
    """True for a value emitted inline (scalar or array-of-scalars)."""
    return not isinstance(value, dict) and not _is_aot(value)


def _key(k) -> str:
    if not isinstance(k, str):
        raise TomlWriteError(f"table keys must be strings, got {type(k).__name__}")
    return k if _BARE_RE.match(k) else escape_string(k)


def _dotted(path: Sequence[str]) -> str:
    return ".".join(_key(p) for p in path)


def _format_float(value: float) -> str:
    if math.isnan(value):
        return "nan"
    if math.isinf(value):
        return "inf" if value > 0 else "-inf"
    return repr(value)


def _format_scalar(value) -> str:
    """Format a value that is written inline: `k = <this>`."""
    if isinstance(value, bool):  # before int: bool is a subclass of int
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _format_float(value)
    if isinstance(value, str):
        return escape_string(value)
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, (dict, list)):
                raise TomlWriteError(
                    "nested arrays/tables cannot be written inline; use an "
                    "array-of-tables instead"
                )
            parts.append(_format_scalar(item))
        return "[" + ", ".join(parts) + "]"
    raise TomlWriteError(f"unsupported value type: {type(value).__name__}")


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------

def _comment_for(comments: Mapping[str, str], path: Sequence[str]):
    if not comments:
        return None
    return comments.get(".".join(str(p) for p in path))


def _emit_comment(lines: list[str], comments, path, fallback=None) -> None:
    text = _comment_for(comments, path)
    if text is None and fallback is not None:
        text = _comment_for(comments, fallback)
    if text:
        lines.append("# " + sanitize_comment(text))


def _has_direct(table: dict) -> bool:
    """True if the table has anything written right under its own header."""
    return any(_is_plain(v) or _is_aot(v) for v in table.values())


def _emit_table(lines: list[str], table: dict, path: tuple, comments,
                cpath: tuple | None = None) -> None:
    # `path` drives TOML headers; `cpath` drives comment lookup and differs
    # inside array-of-tables, where a component is addressed by index
    # ("components.0") even though its header is just `[[components]]`.
    cpath = path if cpath is None else cpath

    scalars = [(k, v) for k, v in table.items() if _is_plain(v)]
    tables = [(k, v) for k, v in table.items() if isinstance(v, dict)]
    aots = [(k, v) for k, v in table.items() if _is_aot(v)]

    for k, v in scalars:
        _emit_comment(lines, comments, cpath + (k,))
        lines.append(f"{_key(k)} = {_format_scalar(v)}")

    for k, sub in tables:
        subpath = path + (k,)
        sub_cpath = cpath + (k,)
        if not sub:
            _emit_comment(lines, comments, sub_cpath)
            lines.append(f"[{_dotted(subpath)}]")
        elif _has_direct(sub):
            _emit_comment(lines, comments, sub_cpath)
            lines.append(f"[{_dotted(subpath)}]")
            _emit_table(lines, sub, subpath, comments, sub_cpath)
        else:
            # Only nested tables inside: skip this header and let the
            # children emit their own dotted headers.
            _emit_table(lines, sub, subpath, comments, sub_cpath)

    for k, arr in aots:
        subpath = path + (k,)
        sub_cpath = cpath + (k,)
        for i, elem in enumerate(arr):
            elem_cpath = sub_cpath + (str(i),)
            _emit_comment(lines, comments, elem_cpath, fallback=sub_cpath)
            lines.append(f"[[{_dotted(subpath)}]]")
            _emit_table(lines, elem, subpath, comments, elem_cpath)


def dump(
    data: dict,
    *,
    comments: Mapping[str, str] | None = None,
    header: Sequence[str] = (),
) -> str:
    """Serialise `data` to TOML text.

    `comments` maps a dotted key path (e.g. `"meta.name"`, `"components.0"`,
    `"components.0.argv_match"`) to a comment emitted on the line above.
    `header` is a list of leading comment lines. Both are sanitised.

    Raises `TomlWriteError` for a non-dict top level, a non-string key, or
    an unrepresentable value (`None`, bytes, sets, nested arrays, …).
    """
    if not isinstance(data, dict):
        raise TomlWriteError("top-level value must be a table (dict)")
    lines: list[str] = []
    for h in header:
        lines.append("# " + sanitize_comment(h))
    _emit_table(lines, data, (), comments or {})
    if not lines:
        return ""
    return "\n".join(lines) + "\n"
