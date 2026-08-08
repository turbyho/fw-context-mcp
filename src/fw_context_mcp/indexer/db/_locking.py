"""File-system write lock for index operations.

Serializes all write operations (symbol storage, LLM analysis, embeddings)
across processes.  Uses ``fcntl.flock`` — the kernel releases the lock
automatically on process exit, so a crash never leaves a stale lock.

WHY fcntl.flock (not a file-based lock or SQLite WAL): SQLite WAL mode
allows concurrent reads but serializes writes per connection.  Multiple
processes (indexing daemon, reindex_file, CLI watch) may all try to write
simultaneously.  An external lock serializes them at the application level,
preventing SQLITE_BUSY errors and ensuring atomic multi-table operations.

WHY advisory lock (not mandatory): fcntl.flock is cooperative — only
processes that check the lock are serialized.  This is sufficient because
all fw-context processes use ``write_lock``.  A mandatory lock (chmod +l)
would block even read-only operations and requires root.
"""

from __future__ import annotations

import fcntl
import os
import time as _time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

__all__ = [
    "WriteLockTimeout",
    "write_lock",
]


class WriteLockTimeout(RuntimeError):
    """Raised when the write lock cannot be acquired within the timeout."""


@contextmanager
def write_lock(db_dir: Path, timeout: float = 60.0) -> Generator[None, None, None]:
    """Acquire an exclusive write lock for the index directory.

    Serializes all write operations (symbol storage, LLM analysis, embeddings)
    across processes.  Uses ``fcntl.flock`` — the kernel releases the lock
    automatically on process exit, so a crash never leaves a stale lock.

    Blocks for up to *timeout* seconds; raises ``WriteLockTimeout`` when the
    lock cannot be acquired in time.  Callers should catch and propagate
    the error gracefully — never retry indefinitely.

    Args:
        db_dir: Directory containing the index database (lock file is
            ``<db_dir>/write.lock``).
        timeout: Maximum time to wait for the lock, in seconds (default 60).

    Raises:
        WriteLockTimeout: Lock could not be acquired within *timeout*.
    """
    lock_file = db_dir / "write.lock"
    fd = os.open(lock_file, os.O_CREAT | os.O_RDWR)
    deadline = _time.monotonic() + timeout
    acquired = False
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if _time.monotonic() > deadline:
                    raise WriteLockTimeout(
                        f"Could not acquire write lock for {db_dir} within {timeout:.0f}s"
                    ) from None
                _time.sleep(0.5)
        yield
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
