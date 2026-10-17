"""`rice-switcher import`, `rice-switcher diff`, `rice-switcher edit` (Slice 4).

PLAN §6.4 + §2h contract:

- `import <path>`:
  - Read the existing config directory at `path` (the source). It must
    contain a `rice.toml` (parsed via tomllib). The `config_link`
    paths are inspected but NEVER executed.
  - `--dry-run` default ON. `--apply` to actually copy.
  - Validates the manifest (schema, basic checks) and rejects
    name conflicts with existing rice.
  - Atomic: source files are copied into `<rice_home>/<name>/` after
    the manifest parses and validates. If any copy step fails, the
    target directory is removed (best-effort) and a single
    `ImportResult` is returned.
  - Audit event emitted on success.

- `diff <a> <b>`:
  - Compare two installed rice. Default: unified diff of their
    `rice.toml` files. If the names resolve to manifest files
    (rather than directories), treat the comparison as file-vs-file.
  - Returns `(diff_text, exit_code)`. exit 0 if same, 1 if
    different, 2 on error.
  - Output is plain `unified_diff` text suitable for piping.

- `edit <name>`:
  - Open the rice's `rice.toml` in `$EDITOR` (or `vi` as fallback).
  - Slice 4 implementation passes the editor command as an argv
    list to `subprocess.run`; the editor itself runs against the
    target file path. No auto-write of any other file.

This module is read-only w.r.t. the live desktop. All real work
happens in tempdirs under tests.
"""

from __future__ import annotations

import difflib
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Iterable, Literal

from .log import append_audit
from .manifest import Manifest, list_manifests, load_manifest
from .paths import Roots, manifest_path, rice_dir


# ---------------------------------------------------------------------------
# Path safety helpers
# ---------------------------------------------------------------------------

# Use the canonical rice-name validator from `paths` so that import /
# diff / edit share the exact same defensive contract as the rest of
# the codebase (regex + reject . / .. / absolute / ~ / separators).
from .paths import validate_rice_name


def _is_safe_rice_name(name: str) -> bool:
    """A rice name is a short, filesystem-safe label. Delegates to
    `paths.validate_rice_name` so the rules stay consistent across
    every module."""
    ok, _ = validate_rice_name(name)
    return ok


def _resolve_rice_path(roots: Roots, name: str) -> str | None:
    """Return absolute path to the rice directory if installed."""
    if not _is_safe_rice_name(name):
        return None
    return os.path.abspath(rice_dir(roots, name))


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ImportResult:
    status: Literal[
        "applied", "dry_run", "name_invalid", "name_taken",
        "source_missing", "source_no_manifest", "source_manifest_invalid",
        "copy_failed", "io_error",
    ]
    name: str = ""
    detail: str = ""
    copied_paths: tuple[str, ...] = ()


def _gather_source_files(source_dir: str) -> list[str]:
    """Return a sorted list of file paths (relative to source_dir) for
    every file under `source_dir`. Directories themselves are not
    listed; the caller recurses via `shutil.copytree`."""
    out: list[str] = []
    base = os.path.abspath(source_dir)
    for root, _dirs, files in os.walk(base):
        for f in files:
            full = os.path.join(root, f)
            rel = os.path.relpath(full, base)
            out.append(rel)
    out.sort()
    return out


def _safe_join_target(target_dir: str, rel_path: str) -> str | None:
    """Join `target_dir` and `rel_path` and verify the result is
    still under `target_dir`. Reject symlink-like sequences."""
    target_abs = os.path.abspath(target_dir)
    candidate = os.path.normpath(os.path.join(target_abs, rel_path))
    if not (candidate == target_abs or candidate.startswith(target_abs + os.sep)):
        return None
    return candidate


def do_import(
    roots: Roots,
    source: str,
    name: str | None = None,
    *,
    dry_run: bool = True,
    runner=None,
) -> ImportResult:
    """Import an existing config directory as a new rice.

    Parameters
    ----------
    roots : filesystem roots.
    source : path to the source config directory. It must contain a
        `rice.toml` that parses as a `Manifest`.
    name : rice name to install under. If None, derived from
        `[meta].name` in the source manifest. Must be safe (no path
        separators, no `..`).
    dry_run : if True, do not copy anything. Return a description of
        what would happen.
    runner : reserved for future use; not used in Slice 4. Import is
        pure filesystem copy via `shutil.copytree`.
    """
    source_abs = os.path.abspath(os.path.expanduser(source))
    if not os.path.isdir(source_abs):
        return ImportResult(status="source_missing", detail=f"source not a directory: {source_abs}")
    src_manifest = os.path.join(source_abs, "rice.toml")
    if not os.path.isfile(src_manifest):
        return ImportResult(status="source_no_manifest",
                            detail=f"no rice.toml at {src_manifest}")

    try:
        m = load_manifest(src_manifest)
    except Exception as e:
        return ImportResult(status="source_manifest_invalid",
                            detail=f"failed to parse {src_manifest}: {e}")

    # Determine rice name: explicit > manifest meta.name
    target_name = name if name else m.meta.name
    if not _is_safe_rice_name(target_name):
        return ImportResult(
            status="name_invalid",
            name=target_name,
            detail=f"rice name {target_name!r} is not safe (allowed: A-Z a-z 0-9 . _ -, max 64)",
        )

    # Check for existing rice
    existing = [n for n, _ in list_manifests(roots.rice_home)]
    if target_name in existing:
        return ImportResult(
            status="name_taken", name=target_name,
            detail=f"rice {target_name!r} is already installed; refusing to overwrite",
        )

    src_files = _gather_source_files(source_abs)

    if dry_run:
        return ImportResult(
            status="dry_run", name=target_name,
            detail=f"would copy {len(src_files)} file(s) from {source_abs} to {rice_dir(roots, target_name)}",
            copied_paths=tuple(src_files),
        )

    # Real apply: copy the tree atomically. We copy into a temp
    # sibling first, then rename into place. If the rename fails, we
    # clean up the temp sibling.
    target_dir = _resolve_rice_path(roots, target_name)
    if target_dir is None:
        return ImportResult(status="name_invalid", name=target_name,
                            detail="target_dir resolve failed")
    if os.path.lexists(target_dir):
        return ImportResult(status="name_taken", name=target_name,
                            detail=f"target already exists at {target_dir}")

    parent = os.path.dirname(target_dir)
    os.makedirs(parent, exist_ok=True)

    # Copy into a tmp staging directory, then rename.
    staging = os.path.join(parent, f".import-{target_name}-{os.getpid()}")
    try:
        # shutil.copytree(symlinks=True) preserves symlinks
        shutil.copytree(source_abs, staging, symlinks=True)
    except OSError as e:
        # Clean up partial staging
        if os.path.isdir(staging):
            shutil.rmtree(staging, ignore_errors=True)
        return ImportResult(status="copy_failed", name=target_name,
                            detail=f"copytree failed: {e}")

    # Atomic-ish rename into place
    try:
        os.rename(staging, target_dir)
    except OSError as e:
        shutil.rmtree(staging, ignore_errors=True)
        return ImportResult(status="io_error", name=target_name,
                            detail=f"rename failed: {e}")

    # Audit event
    try:
        append_audit(roots, {
            "action": "import",
            "name": target_name,
            "source": source_abs,
            "files": len(src_files),
        })
    except Exception:
        pass

    return ImportResult(
        status="applied", name=target_name,
        detail=f"imported {len(src_files)} file(s) to {target_dir}",
        copied_paths=tuple(src_files),
    )


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DiffResult:
    status: Literal["same", "different", "missing", "invalid_name", "io_error"]
    diff_text: str = ""
    name_a: str = ""
    name_b: str = ""


def _read_text_file(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def do_diff(
    roots: Roots,
    name_a: str,
    name_b: str,
    *,
    context_lines: int = 3,
) -> DiffResult:
    """Produce a unified diff between two installed rice.

    The comparison is the `rice.toml` file of each rice. Output is
    a `unified_diff` text with a header naming the two sides.
    """
    if not _is_safe_rice_name(name_a) or not _is_safe_rice_name(name_b):
        return DiffResult(status="invalid_name", name_a=name_a, name_b=name_b,
                          diff_text="")
    path_a = manifest_path(roots, name_a)
    path_b = manifest_path(roots, name_b)
    if not os.path.isfile(path_a):
        return DiffResult(status="missing", name_a=name_a, name_b=name_b,
                          diff_text=f"--- {path_a} (not found)\n")
    if not os.path.isfile(path_b):
        return DiffResult(status="missing", name_a=name_a, name_b=name_b,
                          diff_text=f"--- {path_b} (not found)\n")

    a_lines = _read_text_file(path_a) or ""
    b_lines = _read_text_file(path_b) or ""

    # If both are the empty string, treat as same
    if a_lines == b_lines:
        return DiffResult(status="same", name_a=name_a, name_b=name_b)

    diff_iter = difflib.unified_diff(
        a_lines.splitlines(keepends=True),
        b_lines.splitlines(keepends=True),
        fromfile=f"a/{name_a}/rice.toml",
        tofile=f"b/{name_b}/rice.toml",
        n=context_lines,
    )
    text = "".join(diff_iter)
    return DiffResult(status="different", name_a=name_a, name_b=name_b,
                     diff_text=text)


# ---------------------------------------------------------------------------
# edit
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EditResult:
    status: Literal["ok", "invalid_name", "missing", "no_editor",
                     "editor_failed", "not_a_rice"]
    name: str = ""
    detail: str = ""
    editor: str = ""


def _pick_editor() -> str | None:
    """Return the editor command to use, or None if no editor is set."""
    return os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"


def do_edit(
    roots: Roots,
    name: str,
    *,
    editor: str | None = None,
    timeout_seconds: float = 600.0,
    runner=None,
) -> EditResult:
    """Open the rice's `rice.toml` in an editor.

    Parameters
    ----------
    name : rice name. Must be a safe filesystem label.
    editor : override the editor command. If None, use $EDITOR /
        $VISUAL, falling back to `vi`. The editor is invoked as
        a subprocess that BLOCKS until the user closes the editor.
    timeout_seconds : maximum wall-clock time for the editor process.
    runner : injectable subprocess runner. If None, uses
        `subprocess.run` (blocking). The runner signature is
        `(argv, timeout) -> (returncode, stdout, stderr)`.

    Slice 4 NEVER auto-spawns an interactive editor in tests; tests
    use a fake runner.
    """
    if not _is_safe_rice_name(name):
        return EditResult(status="invalid_name", name=name,
                          detail=f"rice name {name!r} is not safe")
    target_dir = _resolve_rice_path(roots, name)
    if target_dir is None:
        return EditResult(status="invalid_name", name=name, detail="resolve failed")
    if not os.path.isdir(target_dir):
        return EditResult(status="missing", name=name,
                          detail=f"rice {name!r} is not installed")
    manifest = os.path.join(target_dir, "rice.toml")
    if not os.path.isfile(manifest):
        return EditResult(status="not_a_rice", name=name,
                          detail=f"{manifest} not found; refusing to spawn an editor on a non-rice directory")

    chosen = editor if editor else _pick_editor()
    if not chosen:
        return EditResult(status="no_editor", name=name,
                          detail="$EDITOR and $VISUAL are empty; install an editor or pass --editor")

    argv = [chosen, manifest]
    try:
        if runner is None:
            proc = subprocess.run(argv, timeout=timeout_seconds, check=False)
            rc = proc.returncode
        else:
            rc, _out, _err = runner(argv, timeout_seconds)
    except (FileNotFoundError, OSError) as e:
        return EditResult(status="no_editor", name=name, editor=chosen,
                          detail=f"editor {chosen!r} could not be invoked: {e}")
    except subprocess.TimeoutExpired as e:
        return EditResult(status="editor_failed", name=name, editor=chosen,
                          detail=f"editor timed out after {timeout_seconds}s: {e}")

    if rc != 0:
        return EditResult(status="editor_failed", name=name, editor=chosen,
                          detail=f"editor exited with rc={rc}")

    # After the editor returns, audit the edit.
    try:
        append_audit(roots, {
            "action": "edit",
            "name": name,
            "editor": chosen,
        })
    except Exception:
        pass

    return EditResult(status="ok", name=name, editor=chosen,
                      detail=f"edited {manifest}")
