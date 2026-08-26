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
    "IndexRunLocked",
    "WriteLockTimeout",
    "index_run_lock",
    "write_lock",
]


class WriteLockTimeout(RuntimeError):
    """Raised when the write lock cannot be acquired within the timeout."""


class IndexRunLocked(RuntimeError):
    """Another indexing run already owns this index directory.

    Carries the PID recorded by the holder, for the message only — the
    exclusion itself comes from the kernel, not from the PID.
    """

    def __init__(self, db_dir: Path, holder_pid: int | None) -> None:
        self.holder_pid = holder_pid
        who = f"pid {holder_pid}" if holder_pid else "another process"
        super().__init__(f"an index run is already in progress for {db_dir} ({who})")


@contextmanager
def index_run_lock(db_dir: Path) -> Generator[None, None, None]:
    """Hold an exclusive lock for a whole indexing run.

    WHY this exists next to :func:`write_lock`: that lock is taken and
    released per translation unit, deliberately, so a manual operation can
    interleave.  It therefore does nothing to stop a SECOND indexing run from
    starting and writing the same tables between two of the first run's
    units.  Two indexers sharing a database corrupt each other's bookkeeping:
    each captured its own file snapshot and header ownership at start, and
    each deletes rows the other just wrote.

    WHY ``flock`` rather than a PID file: the kernel drops the lock when the
    process exits, however it exits.  A PID file survives a crash and has to
    be validated against ``/proc``, which is how the previous guard here came
    to be dead code — it compared the interpreter name against a list that
    did not include ``python3.14``, so it never matched and never excluded
    anything.

    Raises :class:`IndexRunLocked` immediately when another run holds it.
    Waiting would be wrong: an index run takes minutes to hours, and a caller
    that wants the work done can act on the refusal instead.
    """
    db_dir.mkdir(parents=True, exist_ok=True)
    lock_file = db_dir / "index.lock"
    fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o644)
    acquired = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            raise IndexRunLocked(db_dir, _read_holder_pid(lock_file)) from None
        # Record who holds it, for the refusal message the next caller prints.
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        os.fsync(fd)
        yield
    finally:
        if acquired:
            os.ftruncate(fd, 0)
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read_holder_pid(lock_file: Path) -> int | None:
    """Return the PID the lock holder recorded, or None when unreadable.

    Advisory only — the lock is already known to be held.  A holder that has
    not written its PID yet, or a truncated read, simply means the message
    says "another process".
    """
    try:
        content = lock_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(content) if content.isdigit() else None


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
