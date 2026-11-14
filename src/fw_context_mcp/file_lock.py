"""Exclusive file locks across processes, on POSIX and on Windows.

Each lock of fw-context (the index lock, the write lock, the watcher lock,
the lock of the ChatGPT credentials) is a lock of one open file descriptor.
The operating system releases the lock when the process ends, however it
ends.  Thus a process that stopped never leaves a lock that blocks the next
process, and no PID check is necessary for the exclusion.

A lock that a different process must see without taking it (the index lock,
the write lock, the watcher lock) is a :class:`ProbedLock`, and
:func:`held_by_another` is its probe.

POSIX: ``fcntl.flock`` on the whole file.  The lock is advisory: only a
process that asks for the lock waits for it.

Windows: ``LockFileEx`` on one byte at :data:`LOCK_OFFSET`.  WHY not
``msvcrt.locking``: it locks from the current file position, thus each call
must move the position.  ``LockFileEx`` takes the offset in an
``OVERLAPPED`` structure.

WHY a byte after the data: a lock on Windows is mandatory for the bytes
that it locks.  A different process cannot read or write them.  The index
lock file holds the PID of its holder, and the next caller reads it for its
message.  A lock on byte 0 makes that read fail.  A lock after the end of
the file is permitted, and it locks no data.

WHY 1 GiB and not a larger offset: SQLite locks its ``PENDING_BYTE`` at the
same offset on each Windows file system, also on FAT, exFAT and network
shares.  A file system can refuse an offset after its largest file size.
The lock files of fw-context hold a few bytes, thus they never reach it.

The two systems agree on these cases:

- A second file descriptor of the same file does not get the lock, also in
  the same process.
- The lock goes away when its file descriptor closes or its process ends.
  On Windows the release after a close without :func:`unlock`, or after
  the end of the process, can come some time later: Microsoft documents
  that the time "depends upon available system resources".  Thus each
  caller unlocks before it closes, and a caller that waits for a stopped
  process waits with a time limit (``indexer._index_owner``).
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

__all__ = [
    "LOCK_OFFSET",
    "STATUS_WAIT_S",
    "ProbedLock",
    "held_by_another",
    "is_free",
    "lock",
    "status_path",
    "try_lock",
    "unlock",
    "wait_until_free",
]

#: The offset of the byte that the lock on Windows locks.  POSIX ignores it.
LOCK_OFFSET = 2**30

if sys.platform == "win32":
    import ctypes
    import msvcrt

    from . import _win32

    def _overlapped() -> _win32.Overlapped:
        """Return an ``OVERLAPPED`` structure that points to :data:`LOCK_OFFSET`."""
        overlapped = _win32.Overlapped()
        overlapped.Offset = LOCK_OFFSET & 0xFFFFFFFF
        overlapped.OffsetHigh = LOCK_OFFSET >> 32
        return overlapped

    def try_lock(fd: int) -> bool:
        """Get the exclusive lock of *fd* if it is free, and return True.

        Return False when a different file descriptor holds the lock.  Raise
        OSError for each other failure.
        """
        handle = msvcrt.get_osfhandle(fd)
        flags = _win32.LOCKFILE_EXCLUSIVE_LOCK | _win32.LOCKFILE_FAIL_IMMEDIATELY
        if _win32.kernel32.LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(_overlapped())):
            return True
        error = ctypes.get_last_error()
        # A different handle holds a lock on the byte.
        if error == _win32.ERROR_LOCK_VIOLATION:
            return False
        raise ctypes.WinError(error)

    def lock(fd: int) -> None:
        """Wait until the exclusive lock of *fd* is free, and get it.

        The wait has no time limit.  WHY a loop of :func:`try_lock` and not
        ``LockFileEx`` without ``LOCKFILE_FAIL_IMMEDIATELY``: that call stays
        in the kernel until it gets the lock.  The main thread then runs no
        Python code, thus Ctrl+C and the stop request of a different process
        (a signal handler that ``_thread.interrupt_main`` starts) wait for
        the lock too.  The loop (:func:`_poll_lock`) runs Python code
        between two tries.
        """
        _poll_lock(fd, None, _LOCK_POLL_S)

    def unlock(fd: int) -> None:
        """Release the lock that *fd* holds.  Raise OSError when *fd* holds no lock."""
        handle = msvcrt.get_osfhandle(fd)
        if not _win32.kernel32.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(_overlapped())):
            raise ctypes.WinError(ctypes.get_last_error())

else:
    import fcntl

    def try_lock(fd: int) -> bool:
        """Get the exclusive lock of *fd* if it is free, and return True.

        Return False when a different file descriptor holds the lock.  Raise
        OSError for each other failure.
        """
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def lock(fd: int) -> None:
        """Wait until the exclusive lock of *fd* is free, and get it.

        The wait has no time limit.  A signal handler that raises an
        exception stops the wait.
        """
        fcntl.flock(fd, fcntl.LOCK_EX)

    def unlock(fd: int) -> None:
        """Release the lock that *fd* holds."""
        fcntl.flock(fd, fcntl.LOCK_UN)


#: How long :meth:`ProbedLock.try_acquire` waits for the status lock after
#: it got the exclusion lock.  A probe holds the status lock for the time of
#: one system call.  The rest of the limit is for Windows, which releases the
#: lock of a process that ended "some time later" (module docstring); the
#: tests wait 10 s for that release too.
STATUS_WAIT_S = 10.0

#: The pause between two tries of the status lock.  A probe holds it for
#: the time of one system call, thus a short pause lets the holder go on soon.
_STATUS_POLL_S = 0.01

#: The pause between two tries of Windows :func:`lock`.  Its holder can keep
#: the lock for seconds (a refresh of the ChatGPT credentials), thus a longer
#: pause than for the status lock: fewer system calls while it waits.
_LOCK_POLL_S = 0.05

#: The pause between two tries of :func:`wait_until_free`.  Its caller waits
#: for the end of a different process, which takes longer than a probe.
_FREE_POLL_S = 0.1


def _poll_lock(fd: int, limit: float | None, pause: float) -> bool:
    """Try the lock of *fd* each *pause* seconds until it is free, and return True; False when *limit* ends first.

    *limit* None: no limit.  WHY tries and not a wait in the kernel: the
    blocking ``flock`` has no time limit, and the blocking ``LockFileEx``
    runs no Python code (thus no signal handler) until it gets the lock.
    Windows :func:`lock` and :meth:`ProbedLock.try_acquire` use this loop.
    """
    deadline = None if limit is None else time.monotonic() + limit
    while not try_lock(fd):
        if deadline is not None and time.monotonic() >= deadline:
            return False
        time.sleep(pause)
    return True


def status_path(path: Path) -> Path:
    """Return the status file of the :class:`ProbedLock` of *path*: ``<name>.held`` beside it."""
    return path.with_name(f"{path.name}.held")


class ProbedLock:
    """An exclusive lock of *path* that :func:`held_by_another` can see without taking it.

    Two files and two locks:

    - *path*, the exclusion lock.  Only a process that wants the lock tries
      it (:meth:`try_acquire`), with no wait.
    - :func:`status_path`, the status lock.  The holder holds it as long as
      the exclusion lock.  :func:`held_by_another` tries only this lock.

    WHY two locks: ``flock`` and ``LockFileEx`` cannot tell if a lock is
    held without taking it.  With one lock, the probe held the lock for a
    short time, and a :meth:`try_acquire` of a different process in that
    time failed as if a holder existed.  ``fw-context index`` then stopped
    with "the index is in use", and a new daemon exited with "Daemon
    already running".  Now the probe never touches the exclusion lock.
    The holder waits for the status lock, thus a probe in that moment only
    delays it for the time of the probe.  The wait has a limit
    (:data:`STATUS_WAIT_S`, :meth:`try_acquire`).

    The status lock goes after the exclusion lock (:meth:`release`), thus
    a probe sees the lock as held until its holder released the two.  A
    new holder that comes in that time waits for the status lock as for a
    probe.  This order is not sure for a process that the system stops:
    the system can release its two locks in either order.  A process that
    must know that the exclusion lock is free (``fw-context watch
    restart``) thus tries that lock itself (:func:`is_free`).

    The object opens *path* at once (it makes the file), and
    :meth:`close` closes it.  :attr:`fd` is the descriptor of *path*: the
    index lock writes the PID of its holder into it.
    """

    def __init__(self, path: Path, mode: int = 0o644) -> None:
        self.path = path
        self._mode = mode
        self._fd = os.open(path, os.O_CREAT | os.O_RDWR, mode)
        self._status_fd: int | None = None

    @property
    def fd(self) -> int:
        """The file descriptor of the exclusion lock file *path*."""
        return self._fd

    @property
    def held(self) -> bool:
        """True while this object holds the lock."""
        return self._status_fd is not None

    def try_acquire(self, status_wait: float | None = None) -> bool:
        """Get the two locks and return True, or return False when a different holder has the lock.

        False also when the status lock stays held for *status_wait*
        seconds (default :data:`STATUS_WAIT_S`) after this object got the
        exclusion lock: a process holds it, thus the lock is not free.  WHY
        a limit: a probe that is stopped while it holds the status lock
        (Ctrl-Z, a debugger), or a process that ended and whose status lock
        the system has not released yet, kept the wait going with no end.
        The exclusion lock was held all that time, thus each other holder
        failed too.  A caller with a time limit of its own (``write_lock``)
        gives the rest of its time as *status_wait*.  The wait is never
        longer than :data:`STATUS_WAIT_S`: the exclusion lock is held all
        that time, and a longer wait blocked each other holder for it.  The
        exclusion lock is free again after a False.

        Raise OSError for each other failure; the exclusion lock is then
        free again.  A second call while this object holds the lock is an
        error of the caller (RuntimeError).
        """
        if self.held:
            raise RuntimeError(f"{self.path} is already held by this object")
        if not try_lock(self._fd):
            return False
        try:
            status = os.open(status_path(self.path), os.O_CREAT | os.O_RDWR, self._mode)
            try:
                limit = STATUS_WAIT_S if status_wait is None else min(status_wait, STATUS_WAIT_S)
                got_status = _poll_lock(status, limit, _STATUS_POLL_S)
            except BaseException:
                os.close(status)
                raise
            if not got_status:
                os.close(status)
        except BaseException:
            # A stop (Ctrl-C, IndexStopped) during the wait for a probe
            # too: an exclusion lock without its status lock blocks the
            # next holder, and a probe does not see it.
            unlock(self._fd)
            raise
        if not got_status:
            unlock(self._fd)
            return False
        self._status_fd = status
        return True

    def release(self) -> None:
        """Release the exclusion lock, then the status lock.  Raise RuntimeError when this object holds no lock."""
        status = self._status_fd
        if status is None:
            raise RuntimeError(f"{self.path} is not held by this object")
        self._status_fd = None
        try:
            unlock(self._fd)
        finally:
            try:
                unlock(status)
            finally:
                os.close(status)

    def close(self) -> None:
        """Release the lock when this object holds it, and close *path*."""
        try:
            if self.held:
                self.release()
        finally:
            os.close(self._fd)


def held_by_another(path: Path) -> bool:
    """Return True while a different :class:`ProbedLock` holds the lock of *path*.

    A try of the status lock (:func:`status_path`) that releases it at
    once.  The function never touches *path* itself, thus a holder that
    tries the lock in the same time does not fail because of the probe.
    The function does not make the file.  A file that it cannot open (it
    does not exist, or no permission) gives False.  WHY: the probe only
    tells if a different process works now.  The process that then takes
    the lock opens the same file, and it reports the error.  The daemon
    calls the probe in its loop, and an exception there stops the daemon
    for the error of one file.
    """
    try:
        fd = os.open(status_path(path), os.O_RDWR)
    except OSError:
        return False
    try:
        if not try_lock(fd):
            return True
        unlock(fd)
        return False
    finally:
        os.close(fd)


def is_free(path: Path) -> bool:
    """Return True when no process holds the exclusion lock or the status lock of the :class:`ProbedLock` of *path*.

    A try of each lock that releases it at once, the exclusion lock first.
    WHY not only :func:`held_by_another`, which tries the status lock only:

    - A holder of an older version (a daemon of fw-context 0.36.0 or
      earlier) holds the exclusion lock and no status lock.
    - The system can release the two locks of a process that it ended in
      either order (:class:`ProbedLock`).  A probe that saw the status lock
      free let a caller start a new holder, which then found the exclusion
      lock held and stopped.

    WHY the status lock too: :func:`held_by_another` sees the lock as held
    until the status lock is free.

    The try of the exclusion lock can make the :meth:`ProbedLock.try_acquire`
    of a different process fail in that moment.  Thus only a caller that
    must know that the whole lock is free uses this function, and not a
    probe in a loop of a long process.  A file that does not exist is a
    free lock.  Raise OSError when the file cannot be opened for another
    reason.
    """
    try:
        fd = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return True
    try:
        if not try_lock(fd):
            return False
        unlock(fd)
    finally:
        os.close(fd)
    return not held_by_another(path)


def wait_until_free(path: Path, timeout: float) -> bool:
    """Return True when the lock of *path* is free (:func:`is_free`) within *timeout* seconds, else False.

    The tries come each 0.1 s.  A *timeout* of 0 tries one time.  Raise
    OSError as :func:`is_free` does.
    """
    deadline = time.monotonic() + timeout
    while not is_free(path):
        if time.monotonic() >= deadline:
            return False
        time.sleep(_FREE_POLL_S)
    return True
