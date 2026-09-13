"""Exclusive advisory file lock that works on POSIX and Windows.

`fcntl` does not exist on Windows; `msvcrt.locking` does not exist elsewhere.
Both trackers lock a sidecar `.lock` file around their read-modify-write, so
this is the one place that knows which primitive the platform offers.
"""

from __future__ import annotations

import contextlib
from typing import IO, Iterator

try:  # POSIX
    import fcntl

    def _acquire(fh: IO) -> None:
        fcntl.flock(fh, fcntl.LOCK_EX)

    def _release(fh: IO) -> None:
        fcntl.flock(fh, fcntl.LOCK_UN)

except ImportError:  # Windows
    import msvcrt

    def _acquire(fh: IO) -> None:
        # msvcrt locks a byte range; the sidecar file is empty, so lock one
        # byte at offset 0. LK_LOCK retries for ~10s before raising.
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)

    def _release(fh: IO) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)


@contextlib.contextmanager
def exclusive(lock_path) -> Iterator[None]:
    """Hold an exclusive lock on `lock_path` for the duration of the block."""
    with open(lock_path, "a+") as fh:
        _acquire(fh)
        try:
            yield
        finally:
            _release(fh)
