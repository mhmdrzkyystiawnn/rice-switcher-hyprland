"""flock(2)-based lock with a 5-second timeout.

PLAN.md §5.4 + §11.1. The lock file lives at <state_home>/rice-switcher/
switch.lock. We use `flock(fd, LOCK_EX | LOCK_NB)` so a second process
trying to acquire raises `BlockingIOError` immediately rather than
blocking. The caller is responsible for the timeout policy (5s).
"""

from __future__ import annotations

import errno
import fcntl
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from .paths import Roots, lock_file

DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_POLL_INTERVAL = 0.05


class LockError(Exception):
    """Raised when the lock cannot be acquired within the timeout."""


@dataclass
class LockInfo:
    path: str
    held: bool
    timeout_seconds: float
    elapsed_seconds: float


def _try_acquire(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as e:
        if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            return False
        raise


@contextmanager
def switch_lock(
    roots: Roots,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
) -> Iterator[LockInfo]:
    """Acquire the global switch lock with a timeout.

    Usage::

        with switch_lock(roots) as info:
            # ... do switch ...
            pass

    On success, the lock is released on context exit. If the timeout
    expires, `LockError` is raised before entering the body. The lock
    file is created lazily; the directory is `os.makedirs`-ed.
    """
    path = lock_file(roots)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    info = LockInfo(path=path, held=False, timeout_seconds=timeout_seconds,
                    elapsed_seconds=0.0)
    start = time.monotonic()
    try:
        while True:
            if _try_acquire(fd):
                info.held = True
                info.elapsed_seconds = time.monotonic() - start
                # Write the holder pid for debugging.
                try:
                    os.ftruncate(fd, 0)
                    os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
                except OSError:
                    pass
                yield info
                return
            elapsed = time.monotonic() - start
            if elapsed >= timeout_seconds:
                info.elapsed_seconds = elapsed
                raise LockError(
                    f"could not acquire switch lock at {path} within "
                    f"{timeout_seconds:.1f}s (held by another process)"
                )
            time.sleep(poll_interval)
    finally:
        if info.held:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)
