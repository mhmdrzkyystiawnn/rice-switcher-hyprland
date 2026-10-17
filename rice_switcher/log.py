"""Logging setup + JSON Lines audit log.

Slice 2 refactor:
- `append_audit(roots, event: dict)` writes a JSON object per line.
- Sensitive keys/values are redacted recursively.
- Active log file is `audit.log`. Old logs are archived as
  `audit-YYYYMMDD-HHMMSS-<pid>-<counter>.log` then optionally gzipped
  when their age exceeds `RICE_AUDIT_ROTATE_DAYS`.
- A sidecar `audit.lock` (flock) protects counter scan, append,
  rotation, and compression in a single critical section. There is
  NO auto-delete.

Backward compatibility: if a plain-text `audit.log` from Slice 1 is
detected on first call to `append_audit`, it is renamed to
`audit-pre-slice2-<ts>.log` (byte-identical, no redaction) so the
legacy content is preserved in a separate file. New writes go to a
fresh JSON Lines `audit.log`.
"""

from __future__ import annotations

import errno
import fcntl
import gzip
import io
import json
import logging
import os
import re
import time
from typing import IO, Iterable

# ---------------------------------------------------------------------------
# Secret redaction (Slice 1 patterns, extended in Slice 2)
# ---------------------------------------------------------------------------

# Key-name substrings (case-insensitive) that trigger value redaction.
SENSITIVE_KEYS = (
    "api_key", "access_key", "authorization", "password",
    "token", "secret", "private_key", "client_secret",
)

# Value regexes: match a secret-shaped literal anywhere in a string.
_SECRET_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9]{16,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{16,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"(?i)(api[_-]?key|access[_-]?key|authorization|password|passwd|private[_-]?key|client[_-]?secret)\s*[=:]\s*\S+"),
]


def _redact_value(value):
    """Redact secret-shaped substrings in a single string value."""
    if not isinstance(value, str):
        return value
    for pat in _SECRET_PATTERNS:
        value = pat.sub("[REDACTED]", value)
    return value


def _is_sensitive_key(key) -> bool:
    if not isinstance(key, str):
        return False
    kl = key.lower()
    return any(s in kl for s in SENSITIVE_KEYS)


def redact_event(event):
    """Return a new structure with sensitive keys/values redacted.

    Handles nested dicts and lists recursively. Non-collection values
    are scanned for secret patterns via `_redact_value`.
    """
    if isinstance(event, dict):
        out = {}
        for k, v in event.items():
            if _is_sensitive_key(k):
                out[k] = "[REDACTED]"
            elif isinstance(v, dict):
                out[k] = redact_event(v)
            elif isinstance(v, list):
                out[k] = [redact_event(x) if isinstance(x, (dict, list)) else _redact_value(x) for x in v]
            else:
                out[k] = _redact_value(v)
        return out
    if isinstance(event, list):
        return [redact_event(x) if isinstance(x, (dict, list)) else _redact_value(x) for x in event]
    return _redact_value(event)


# ---------------------------------------------------------------------------
# Plain text log (unchanged behaviour from Slice 1)
# ---------------------------------------------------------------------------

class _RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:  # type: ignore[override]
        if isinstance(record.msg, str):
            record.msg = _redact_value(record.msg)
        if record.args:
            record.args = tuple(_redact_value(str(a)) if isinstance(a, str) else a for a in record.args)
        return True


_CONFIGURED = False


def setup_logging(roots, level: int = logging.INFO) -> logging.Logger:
    """Configure the `rice_switcher` logger to write to log_dir/switcher.log.

    Idempotent: re-calling will not duplicate handlers.
    """
    global _CONFIGURED

    from .paths import log_dir, default_log_file
    log = logging.getLogger("rice_switcher")

    if _CONFIGURED:
        return log

    log.setLevel(level)
    log.propagate = False

    # Honour RICE_LOG_FILE override
    path = os.environ.get("RICE_LOG_FILE") or default_log_file(roots)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    redactor = _RedactingFilter()

    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(formatter)
    fh.addFilter(redactor)
    log.addHandler(fh)

    # Also a stderr handler in TTY mode
    if os.isatty(2):
        sh = logging.StreamHandler()
        sh.setFormatter(formatter)
        sh.addFilter(redactor)
        log.addHandler(sh)

    _CONFIGURED = True
    return log


def log_to_file(roots, name: str, level: int = logging.INFO) -> logging.Logger:
    """Return a sub-logger that writes to logs/<name>.log under roots."""
    from .paths import log_dir
    sub = logging.getLogger(f"rice_switcher.{name}")
    sub.setLevel(level)
    sub.propagate = False

    path = os.path.join(log_dir(roots), f"{name}.log")
    os.makedirs(os.path.dirname(path), exist_ok=True)

    if not sub.handlers:
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)-7s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
        fh = logging.FileHandler(path, encoding="utf-8")
        fh.setFormatter(formatter)
        fh.addFilter(_RedactingFilter())
        sub.addHandler(fh)

    return sub


# ---------------------------------------------------------------------------
# JSON Lines audit log (Slice 2)
# ---------------------------------------------------------------------------

_ARCHIVE_NAME_RE = re.compile(
    # PID is `{pid:05d}` (5+ digits; pid_max can be larger than 99999) and
    # the counter is `{counter:03d}` (3+ digits). Use `+` quantifiers or a
    # 6-digit PID makes the scan miss existing archives and collide.
    r"^audit-(\d{8})-(\d{6})-(\d{5,})-(\d{3,})\.log$"
)


def audit_archive_dir(roots) -> str:
    """Directory where rotated audit archives live. Same dir as the
    active `audit.log`."""
    from .paths import audit_log_file
    return os.path.dirname(audit_log_file(roots))


def audit_lock_path(roots) -> str:
    """Path of the audit sidecar lock file."""
    return os.path.join(audit_archive_dir(roots), "audit.lock")


def _audit_lock(roots, timeout: float = 5.0) -> int:
    """Acquire the audit lock. Returns the file descriptor; caller must
    close it. Raises OSError on timeout."""
    path = audit_lock_path(roots)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError as e:
            if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                os.close(fd)
                raise
            if time.monotonic() >= deadline:
                os.close(fd)
                raise TimeoutError(f"audit lock not acquired within {timeout}s")
            time.sleep(0.05)


def _now_ts() -> str:
    """Local-time YYYYMMDD-HHMMSS for archive names."""
    return time.strftime("%Y%m%d-%H%M%S", time.localtime())


def _next_counter(archive_dir: str, pid: int) -> int:
    """Scan existing archives, return the highest counter + 1.

    The counter is a 3-digit number parsed from filenames matching
    `audit-YYYYMMDD-HHMMSS-<pid:05d>-<counter:03d>.log`. We scan
    ALL PIDs, not just the current one — simpler, and `O_EXCL` on the
    rename is the ultimate collision guard.
    """
    highest = -1
    if not os.path.isdir(archive_dir):
        return 0
    for name in os.listdir(archive_dir):
        m = _ARCHIVE_NAME_RE.match(name)
        if not m:
            continue
        try:
            counter = int(m.group(4))
        except ValueError:
            continue
        if counter > highest:
            highest = counter
    return highest + 1


def _next_archive_target(archive_dir: str, pid: int, max_retries: int = 5) -> str:
    """Compute a unique target archive name. Uses scan + O_EXCL retry."""
    last_err: Exception | None = None
    for _ in range(max_retries):
        counter = _next_counter(archive_dir, pid)
        target_name = f"audit-{_now_ts()}-{pid:05d}-{counter:03d}.log"
        target = os.path.join(archive_dir, target_name)
        # O_EXCL is the actual collision guard; the scan is best-effort.
        try:
            fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.close(fd)
            return target
        except FileExistsError as e:
            last_err = e
            time.sleep(0.01)
    raise RuntimeError(
        f"could not find a unique archive name after {max_retries} retries: {last_err}"
    )


def _migrate_legacy_audit(roots, archive_dir: str) -> None:
    """If a plain-text `audit.log` from Slice 1 exists, rename it to
    `audit-pre-slice2-<ts>.log` (byte-identical, no redaction). The
    new active file is left empty for JSON Lines writes."""
    from .paths import audit_log_file
    active = audit_log_file(roots)
    if not os.path.isfile(active):
        return
    # Quick heuristic: if the file's first non-empty line parses as
    # JSON, assume it's already JSON Lines and skip.
    try:
        with open(active, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    json.loads(stripped)
                    return  # already JSON Lines
                except json.JSONDecodeError:
                    break
    except OSError:
        return
    ts = _now_ts()
    legacy_name = f"audit-pre-slice2-{ts}.log"
    legacy = os.path.join(archive_dir, legacy_name)
    # Use O_EXCL so we never overwrite a previous legacy migration
    try:
        fd = os.open(legacy, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.close(fd)
    except FileExistsError:
        # Append a millisecond suffix
        ms = int(time.time() * 1000) % 1000
        legacy = os.path.join(archive_dir, f"audit-pre-slice2-{ts}-{ms:03d}.log")
    os.rename(active, legacy)


def _should_rotate(active: str, max_bytes: int) -> tuple[bool, str]:
    """Decide whether the active audit.log should be rotated now.

    Returns (should_rotate, reason). reason is "size" or "day" or ""."""
    if not os.path.isfile(active):
        return False, ""
    try:
        size = os.path.getsize(active)
    except OSError:
        return False, ""
    if size >= max_bytes:
        return True, "size"
    # Day boundary: compare mtime's YYYYMMDD to today's YYYYMMDD
    try:
        mtime_day = time.strftime("%Y%m%d", time.localtime(os.path.getmtime(active)))
    except OSError:
        return False, ""
    today = time.strftime("%Y%m%d")
    if mtime_day != today:
        return True, "day"
    return False, ""


def audit_rotate(roots) -> list[str]:
    """Rotate the active audit.log if size or day threshold exceeded.

    Returns list of newly-archived file paths. Caller must hold the
    audit lock (this function does NOT lock by itself; the lock is
    shared with append/compress for the whole critical section).
    """
    from .paths import audit_log_file
    active = audit_log_file(roots)
    max_bytes = int(os.environ.get("RICE_AUDIT_ROTATE_MAX_BYTES", "50000000"))
    should, _ = _should_rotate(active, max_bytes)
    if not should:
        return []
    archive_dir = audit_archive_dir(roots)
    os.makedirs(archive_dir, exist_ok=True)
    target = _next_archive_target(archive_dir, os.getpid())
    os.rename(active, target)
    return [target]


def audit_compress_old(roots) -> list[str]:
    """Gzip any `.log` archive older than `RICE_AUDIT_ROTATE_DAYS`.

    Returns list of newly-gzipped paths. Does NOT delete the original
    until the gzip is complete and the new file is on disk. Caller
    must hold the audit lock."""
    archive_dir = audit_archive_dir(roots)
    if not os.path.isdir(archive_dir):
        return []
    days = int(os.environ.get("RICE_AUDIT_ROTATE_DAYS", "30"))
    cutoff = time.time() - days * 86400
    out: list[str] = []
    for name in os.listdir(archive_dir):
        if not name.startswith("audit-") or not name.endswith(".log"):
            continue
        path = os.path.join(archive_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if mtime >= cutoff:
            continue
        gz_path = path + ".gz"
        if os.path.exists(gz_path):
            continue
        # Read source, write gz atomically: write to .tmp, rename.
        tmp_gz = gz_path + ".tmp"
        try:
            with open(path, "rb") as src, gzip.open(tmp_gz, "wb") as dst:
                dst.write(src.read())
            os.rename(tmp_gz, gz_path)
            os.unlink(path)
            out.append(gz_path)
        except OSError:
            # Cleanup best-effort
            try:
                if os.path.exists(tmp_gz):
                    os.unlink(tmp_gz)
            except OSError:
                pass
    return out


def read_audit_log(roots) -> tuple[list[dict], int]:
    """Read all JSON entries from the active + archived audit logs.

    Returns (entries, skipped_count). Skipped entries are non-JSON
    lines from legacy (pre-Slice 2) plain-text logs. Order is
    chronological (active first, then archives in mtime order).
    """
    from .paths import audit_log_file
    archive_dir = audit_archive_dir(roots)
    files: list[str] = []
    active = audit_log_file(roots)
    if os.path.isfile(active):
        files.append(active)
    if os.path.isdir(archive_dir):
        for name in sorted(os.listdir(archive_dir)):
            if name.startswith("audit-") and (name.endswith(".log") or name.endswith(".log.gz")):
                if name.endswith(".log") and os.path.join(archive_dir, name) == active:
                    continue
                files.append(os.path.join(archive_dir, name))
    entries: list[dict] = []
    skipped = 0
    for f in files:
        opener = gzip.open if f.endswith(".gz") else open
        mode = "rt" if f.endswith(".gz") else "r"
        try:
            with opener(f, mode, encoding="utf-8") as fh:
                for line in fh:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        entries.append(json.loads(stripped))
                    except json.JSONDecodeError:
                        skipped += 1
        except OSError:
            continue
    return entries, skipped


# Cache: have we already migrated the legacy audit.log in this process?
_MIGRATED = False


def append_audit(roots, event) -> None:
    """Append a single JSON-Lines audit entry.

    `event` must be a dict (or a list of dicts). Non-dict input raises
    TypeError. Sensitive keys/values are redacted via `redact_event`.

    The function manages rotation (size/day) and pre-write compression
    under a single flock(LOCK_EX) on `audit.lock`. Migration of any
    Slice 1 plain-text `audit.log` happens on first call.
    """
    if not isinstance(event, (dict, list)):
        raise TypeError(
            f"append_audit requires dict or list of dicts, got {type(event).__name__}"
        )
    from .paths import audit_log_file
    active = audit_log_file(roots)
    archive_dir = audit_archive_dir(roots)
    os.makedirs(archive_dir, exist_ok=True)

    fd = _audit_lock(roots)
    try:
        global _MIGRATED
        if not _MIGRATED:
            _migrate_legacy_audit(roots, archive_dir)
            _MIGRATED = True
        # Rotate first, then compress; both inside the lock.
        try:
            audit_rotate(roots)
        except OSError:
            pass
        try:
            audit_compress_old(roots)
        except OSError:
            pass
        # Build JSON line(s)
        if isinstance(event, dict):
            payload = event
            payload.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()))
            payload.setdefault("pid", os.getpid())
            lines = [json.dumps(redact_event(payload), ensure_ascii=False, sort_keys=True)]
        else:
            lines = []
            for item in event:
                if not isinstance(item, dict):
                    raise TypeError("append_audit list must contain only dicts")
                item.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()))
                item.setdefault("pid", os.getpid())
                lines.append(json.dumps(redact_event(item), ensure_ascii=False, sort_keys=True))
        with open(active, "a", encoding="utf-8") as f:
            for line in lines:
                f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)
