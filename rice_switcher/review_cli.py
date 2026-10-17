"""Line-per-line review driver for a detected draft (DESIGN §5.2/§5.3).

This is the non-`curses` path (Fase 3a): a small REPL over a
`ReviewSession`. It is deliberately *headless-testable* — `ask` and `out`
are injected, so no real terminal is touched in tests, and the future
`curses` UI (Fase 3b) can sit on top of the same model.

Contract:
  * every loop prints the components, with `guess`/`ask` fields
    highlighted at the top and listed before anything else;
  * `s` saves, `q` cancels *without* saving, and an input EOF also
    cancels — it never silently saves;
  * a bad command or a rejected edit prints a message and re-prompts;
  * no destructive action (delete) happens without an explicit `y`.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Callable

from .review import ReviewError, ReviewSession

AskFn = Callable[[str], str]
OutFn = Callable[[str], None]

_HELP = ("perintah: [Enter]=detail  e=edit  a=tambah  d=hapus  t=type  "
         "c=temuan  u=undo  s=simpan  q=batal")

_FIELD_HELP = ("field (name/start/executable/argv_match/exact_argv/stop/"
               "start_check): ")


@dataclass
class ReviewOutcome:
    """Result of a review session: `save` or `cancel` (+ reason)."""

    action: str
    reason: str = ""


def _parse_bool(raw: str) -> bool:
    v = raw.strip().lower()
    if v in ("true", "t", "yes", "y", "1", "on"):
        return True
    if v in ("false", "f", "no", "n", "0", "off", ""):
        return False
    raise ReviewError(f"nilai boolean tidak valid: {raw!r} (pakai true/false)")


def _parse_value(field_name: str, raw: str):
    """Turn a typed line into the Python value a field expects."""
    if field_name == "argv_match":
        text = raw.strip()
        if not text:
            return []
        try:
            return shlex.split(text)
        except ValueError:
            return text.split()
    if field_name == "exact_argv":
        return _parse_bool(raw)
    return raw.strip()


def _print_summary(session: ReviewSession, out: OutFn) -> None:
    findings = session.findings()
    if findings:
        out("Temuan (perintah `c` untuk menangani):")
        for i, f in enumerate(findings):
            title = getattr(f, "title", None) or str(f)
            state = "selesai" if getattr(f, "is_resolved", False) else "belum ditangani"
            out(f"  [{i}] {title} -- {state}")
            opts = getattr(f, "options", None)
            if opts is not None:
                default = getattr(f, "default_option", "") or "tidak ada"
                listed = ", ".join(opts) if opts else "tidak ada"
                out(f"       opsi: {listed} | skip | default: {default}")
    review = session.needs_review()
    if review:
        out("Perlu ditinjau (guess/ask) -- wajib dilihat sebelum simpan:")
        for name, field_name in review:
            out(f"  - {name}.{field_name}")
    else:
        out("Perlu ditinjau: tidak ada (semua auto).")
    out("Komponen:")
    for v in session.summary():
        out(f"  [{v.index}] {v.name:16} {v.type:12} ({v.confidence})")


def _print_detail(session: ReviewSession, out: OutFn) -> None:
    for c in session.components():
        out(f"== {c.name} ({c.type.value}) ==")
        for field_name, f in c.field_map().items():
            if field_name == "type":
                continue
            marker = "" if f.confidence == "auto" else f"  <{f.confidence}>"
            note = f"  # {f.note}" if f.note else ""
            out(f"  {field_name:12} = {f.value!r}{marker}{note}")


def _cmd_edit(session: ReviewSession, ask: AskFn, out: OutFn) -> None:
    name = ask("komponen: ").strip()
    field_name = ask(_FIELD_HELP).strip()
    raw = ask("nilai baru: ")
    value = _parse_value(field_name, raw)
    session.set_field(name, field_name, value)
    out(f"diset {name}.{field_name}")


def _cmd_add(session: ReviewSession, ask: AskFn, out: OutFn) -> None:
    ctype = ask("type komponen: ").strip()
    name = ask("nama (kosong = otomatis): ").strip()
    start = ask("start: ").strip()
    executable = ask("executable (kosong = nama): ").strip()
    argv = _parse_value("argv_match", ask("argv_match (pisahkan spasi, kosong = []): "))
    comp = session.add(ctype, name or None, start=start,
                       executable=executable, argv_match=argv)
    out(f"ditambahkan {comp.name}")


def _cmd_delete(session: ReviewSession, ask: AskFn, out: OutFn) -> None:
    name = ask("komponen yang dihapus: ").strip()
    confirm = ask(f"hapus {name}? [y/N]: ").strip().lower()
    if confirm in ("y", "yes"):
        session.remove(name)
        out(f"dihapus {name}")
    else:
        out("batal menghapus.")


def _cmd_type(session: ReviewSession, ask: AskFn, out: OutFn) -> None:
    name = ask("komponen: ").strip()
    new_type = ask("type baru: ").strip()
    session.set_type(name, new_type)
    out(f"type {name} -> {new_type}")


def _cmd_finding(session: ReviewSession, ask: AskFn, out: OutFn) -> None:
    """Handle one Fase 4 finding: choose an option, its default, or skip."""
    findings = session.findings()
    if not findings:
        out("tidak ada temuan.")
        return
    raw = ask("temuan nomor: ").strip()
    try:
        idx = int(raw)
    except ValueError:
        raise ReviewError(f"nomor temuan tidak valid: {raw!r}")
    f = session.finding(idx)          # raises ReviewError if out of range
    default = getattr(f, "default_option", "") or ""
    opts = getattr(f, "options", []) or []
    listed = ", ".join(opts) if opts else "tidak ada"
    hint = f"opsi ({listed}; skip"
    hint += f"; Enter=default {default}): " if default else "; Enter=tidak ada default): "
    choice = ask(hint).strip()
    if choice == "":
        if not default:
            raise ReviewError(
                "temuan ini tidak punya opsi default; pilih opsi eksplisit "
                "atau `skip`"
            )
        choice = default
    if choice == "skip":
        session.handle_finding(idx, "skip")
        out(f"temuan {idx} di-skip.")
    else:
        session.handle_finding(idx, choice)
        out(f"temuan {idx} -> {choice}")


def run_review(session: ReviewSession, *, ask: AskFn, out: OutFn) -> ReviewOutcome:
    """Drive an interactive review of `session`.

    Returns `ReviewOutcome("save")` when the user asks to save, or
    `ReviewOutcome("cancel", reason)` on `q`/EOF. The caller decides what
    saving means (Langkah 3 validates and writes the TOML).
    """
    while True:
        _print_summary(session, out)
        out(_HELP)
        try:
            cmd = ask("review> ").strip()
        except EOFError:
            out("input berakhir (EOF); dibatalkan tanpa menyimpan.")
            return ReviewOutcome("cancel", "eof")

        try:
            if cmd == "":
                _print_detail(session, out)
            elif cmd in ("s", "save"):
                return ReviewOutcome("save")
            elif cmd in ("q", "quit"):
                out("dibatalkan tanpa menyimpan.")
                return ReviewOutcome("cancel", "user")
            elif cmd == "u":
                if session.undo():
                    out("undo: satu langkah dikembalikan.")
                else:
                    out("tidak ada yang bisa di-undo.")
            elif cmd == "e":
                _cmd_edit(session, ask, out)
            elif cmd == "a":
                _cmd_add(session, ask, out)
            elif cmd == "d":
                _cmd_delete(session, ask, out)
            elif cmd == "t":
                _cmd_type(session, ask, out)
            elif cmd == "c":
                _cmd_finding(session, ask, out)
            else:
                out(f"perintah tidak dikenal: {cmd!r}")
        except EOFError:
            out("input berakhir (EOF); dibatalkan tanpa menyimpan.")
            return ReviewOutcome("cancel", "eof")
        except ReviewError as e:
            out(f"ditolak: {e}")
