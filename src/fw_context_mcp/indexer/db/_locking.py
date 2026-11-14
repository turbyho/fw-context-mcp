"""File-system write lock for index operations.

Serializes all write operations (symbol storage, LLM analysis, embeddings)
across processes.  Uses :mod:`fw_context_mcp.file_lock` (``flock`` on POSIX,
``LockFileEx`` on Windows) — the kernel releases the lock automatically on
process exit, so a crash never leaves a stale lock.

WHY a kernel file lock (not a file-based lock or SQLite WAL): SQLite WAL mode
allows concurrent reads but serializes writes per connection.  Multiple
processes (indexing daemon, reindex_file, CLI watch) may all try to write
simultaneously.  An external lock serializes them at the application level,
preventing SQLITE_BUSY errors and ensuring atomic multi-table operations.

WHY advisory lock (not mandatory): the lock is cooperative — only
processes that check the lock are serialized.  On Windows the lock is on one
byte after the end of the file, thus it blocks no read of the file.  This is sufficient because
all fw-context processes use ``write_lock``.  A mandatory lock (chmod +l)
would block even read-only operations and requires root.
"""

from __future__ import annotations

import logging
import os
import time as _time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from ...file_lock import ProbedLock, held_by_another
from ...processes import parse_pid, pid_exists

_log = logging.getLogger(__name__)

__all__ = [
    "IndexRunLocked",
    "WriteLockTimeout",
    "index_run_lock",
    "index_run_lock_held",
    "write_lock",
]


class WriteLockTimeout(RuntimeError):
    """Raised when the write lock cannot be acquired within the timeout."""


class IndexRunLocked(RuntimeError):
    """Another process holds the index lock of this index directory.

    An index run, ``fw-context analyze``, an MCP tool that writes the index,
    or ``fw-context db``: within one project each writer holds the lock.

    Carries the PID recorded by the holder, for the message only — the
    exclusion itself comes from the kernel, not from the PID.
    """

    def __init__(self, db_dir: Path, holder_pid: int | None, hint: str = "") -> None:
        self.holder_pid = holder_pid
        who = f"pid {holder_pid}" if holder_pid else "another process"
        message = f"the index {db_dir} is in use ({who})"
        super().__init__(f"{message} — {hint}" if hint else message)


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

    WHY a kernel file lock rather than a PID file: the kernel drops the lock when the
    process exits, however it exits.  A PID file survives a crash and has to
    be validated against ``/proc``, which is how the previous guard here came
    to be dead code — it compared the interpreter name against a list that
    did not include ``python3.14``, so it never matched and never excluded
    anything.

    Raises :class:`IndexRunLocked` immediately when another run holds it.
    Waiting would be wrong: an index run takes minutes to hours, and a caller
    that wants the work done can act on the refusal instead.  The only wait
    is for the status lock of the ``ProbedLock`` after the exclusion lock
    is ours: a probe holds it for the time of one system call, and
    ``file_lock.STATUS_WAIT_S`` limits the wait.
    """
    db_dir.mkdir(parents=True, exist_ok=True)
    run_lock = ProbedLock(db_dir / "index.lock")
    try:
        if not run_lock.try_acquire():
            raise IndexRunLocked(db_dir, _read_holder_pid(run_lock.path))
        # Record who holds it, for the refusal message the next caller prints.
        os.ftruncate(run_lock.fd, 0)
        os.write(run_lock.fd, str(os.getpid()).encode())
        os.fsync(run_lock.fd)
        yield
    finally:
        # The close also when the truncate fails (EIO on a network share):
        # a long-lived MCP server kept the lock, and each later run failed
        # with IndexRunLocked.  The failure is only a warning: the PID is
        # information for the message of the next caller, and an exception
        # here replaced the exception of the run (IndexSuperseded: the
        # daemon then did not retry the work) or failed a run that did its
        # work.  The next holder writes its own PID over it.
        try:
            if run_lock.held:
                try:
                    os.ftruncate(run_lock.fd, 0)
                except OSError as exc:
                    _log.warning("Cannot clear the holder PID in %s: %s", run_lock.path, exc)
        finally:
            run_lock.close()


def index_run_lock_held(db_dir: Path) -> bool:
    """Return True while another process holds :func:`index_run_lock` of *db_dir*.

    A probe of the status lock (``file_lock.held_by_another``), thus a run
    that starts in the same time does not fail because of it.  WHY next to the markers
    ``reindex.pause`` and ``reindex.pid``: ``fw-context analyze`` writes no
    ``reindex.pid``, and a marker can name a process that stopped while the
    lock went to another one.  The lock itself stays with its holder until
    the holder ends.
    """
    return held_by_another(db_dir / "index.lock")


def _read_holder_pid(lock_file: Path) -> int | None:
    """Return the PID the lock holder recorded, or None when unreadable.

    Advisory only — the lock is already known to be held.  A holder that has
    not written its PID yet, or a truncated read, simply means the message
    says "another process".  A PID outside 1 .. ``MAX_PID`` (a damaged
    file) gives None too: the takeover sends the PID a stop request, and
    such a PID names a process group or, cut to 32 bits on Windows, a
    different process.
    """
    try:
        content = lock_file.read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        # ValueError: UnicodeDecodeError of a damaged file.
        return None
    pid = parse_pid(content)
    if pid is None:
        return None
    # A PID of a process that ended is no holder: a run that a kill ended
    # cannot clear its PID, and the message named a process that was gone.
    # An OSError of the check (EINVAL from a sandbox) is no answer: the
    # refusal must stay IndexRunLocked, thus the PID goes out of it.
    try:
        alive = pid_exists(pid)
    except OSError:
        return None
    return pid if alive else None


@contextmanager
def write_lock(db_dir: Path, timeout: float = 60.0) -> Generator[None, None, None]:
    """Acquire an exclusive write lock for the index directory.

    Serializes all write operations (symbol storage, LLM analysis, embeddings)
    across processes.  Uses :class:`fw_context_mcp.file_lock.ProbedLock` — the
    kernel releases the lock automatically on process exit, so a crash never
    leaves a stale lock.

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
    # A ProbedLock: the MCP server probes it (background._is_bg_reindex_running).
    held = ProbedLock(db_dir / "write.lock")
    deadline = _time.monotonic() + timeout
    try:
        # The rest of the time as the wait for the status lock: the try
        # alone can wait for it (ProbedLock.try_acquire), and the wait went
        # past *timeout* by up to STATUS_WAIT_S.
        while not held.try_acquire(status_wait=max(0.0, deadline - _time.monotonic())):
            if _time.monotonic() > deadline:
                raise WriteLockTimeout(
                    f"Could not acquire write lock for {db_dir} within {timeout:.0f}s"
                )
            _time.sleep(0.5)
        yield
    finally:
        held.close()
