"""Exclusive file locks across processes, on POSIX and on Windows.

Each lock of fw-context (the index lock, the write lock, the watcher lock,
the lock of the ChatGPT credentials) is a lock of one open file descriptor.
The operating system releases the lock when the process ends, however it
ends.  Thus a process that stopped never leaves a lock that blocks the next
process, and no PID check is necessary for the exclusion.

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
from pathlib import Path

__all__ = ["LOCK_OFFSET", "held_by_another", "lock", "try_lock", "unlock"]

#: The offset of the byte that the lock on Windows locks.  POSIX ignores it.
LOCK_OFFSET = 2**30

if sys.platform == "win32":
    import ctypes
    import msvcrt
    import time
    from ctypes import wintypes

    _LOCKFILE_FAIL_IMMEDIATELY = 0x1
    _LOCKFILE_EXCLUSIVE_LOCK = 0x2
    # A different handle holds a lock on the byte.
    _ERROR_LOCK_VIOLATION = 33
    # The pause between two tries of :func:`lock`.
    _LOCK_POLL_S = 0.05

    class _Overlapped(ctypes.Structure):
        """The ``OVERLAPPED`` structure of the Win32 API.

        Only ``Offset`` and ``OffsetHigh`` have a function here: they give
        the offset of the locked byte.  The union of the two offsets with
        ``Pointer`` has the same size, thus two DWORD fields are sufficient.
        """

        _fields_ = [
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _LockFileEx = _kernel32.LockFileEx
    _LockFileEx.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_Overlapped),
    ]
    _LockFileEx.restype = wintypes.BOOL
    _UnlockFileEx = _kernel32.UnlockFileEx
    _UnlockFileEx.argtypes = [
        wintypes.HANDLE, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(_Overlapped),
    ]
    _UnlockFileEx.restype = wintypes.BOOL

    def _overlapped() -> _Overlapped:
        """Return an ``OVERLAPPED`` structure that points to :data:`LOCK_OFFSET`."""
        overlapped = _Overlapped()
        overlapped.Offset = LOCK_OFFSET & 0xFFFFFFFF
        overlapped.OffsetHigh = LOCK_OFFSET >> 32
        return overlapped

    def try_lock(fd: int) -> bool:
        """Get the exclusive lock of *fd* if it is free, and return True.

        Return False when a different file descriptor holds the lock.  Raise
        OSError for each other failure.
        """
        handle = msvcrt.get_osfhandle(fd)
        flags = _LOCKFILE_EXCLUSIVE_LOCK | _LOCKFILE_FAIL_IMMEDIATELY
        if _LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(_overlapped())):
            return True
        error = ctypes.get_last_error()
        if error == _ERROR_LOCK_VIOLATION:
            return False
        raise ctypes.WinError(error)

    def lock(fd: int) -> None:
        """Wait until the exclusive lock of *fd* is free, and get it.

        The wait has no time limit.  WHY a loop of :func:`try_lock` and not
        ``LockFileEx`` without ``LOCKFILE_FAIL_IMMEDIATELY``: that call stays
        in the kernel until it gets the lock.  The main thread then runs no
        Python code, thus Ctrl+C and the stop request of a different process
        (a signal handler that ``_thread.interrupt_main`` starts) wait for
        the lock too.  The loop runs Python code between two tries.
        """
        while not try_lock(fd):
            time.sleep(_LOCK_POLL_S)

    def unlock(fd: int) -> None:
        """Release the lock that *fd* holds.  Raise OSError when *fd* holds no lock."""
        handle = msvcrt.get_osfhandle(fd)
        if not _UnlockFileEx(handle, 0, 1, 0, ctypes.byref(_overlapped())):
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


def held_by_another(path: Path) -> bool:
    """Return True while a different file descriptor holds the lock of *path*.

    A try of the lock that releases it at once.  The function does not make
    the file.  A file that it cannot open (it does not exist, or no
    permission) gives False.  WHY: the probe only tells if a different
    process works now.  The process that then takes the lock opens the same
    file, and it reports the error.  The daemon calls the probe in its loop,
    and an exception there stops the daemon for the error of one file.
    """
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return False
    try:
        if not try_lock(fd):
            return True
        unlock(fd)
        return False
    finally:
        os.close(fd)
