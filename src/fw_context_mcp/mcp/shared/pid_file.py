"""PID file operations — write, read, liveness check, stale cleanup.

TOCTOU note: ``os.kill(pid, 0)`` has an inherent race — the PID may be
reused between the check and the action.  Risk is low on Linux (PID wrap
at 4M), and :func:`fcntl.flock` is used where correctness matters
(``watcher.lock``).  The helpers in this module are for **coordination
markers** (pause, reindex-in-progress), not mutual exclusion.

WHY PID files instead of a database flag: the background reindex runs in
a separate OS process (``subprocess.Popen``).  It cannot share an in-memory
mutex or a Python ``threading.Lock`` with the MCP server process.  PID files
are the simplest cross-process coordination primitive — the filesystem is
the only IPC channel guaranteed to exist without additional infrastructure.

WHY ``unlink_if_ours`` checks the PID before unlinking: during a long
``fw-context index --force`` run, a second MCP server may write its own
pause marker, then finish and try to clean up.  Without PID ownership
checking, it would delete the first server's marker, prematurely resuming
the background reindex.  PID-based ownership prevents this.
"""

from __future__ import annotations

import os
from pathlib import Path


class PidFile:
    """Write a PID to a file and optionally clean it up.

    Use as a context manager for auto-cleanup::

        with PidFile(path) as pf:
            ...  # PID file exists while the block is active

    Or use the instance methods for manual lifecycle::

        pf = PidFile(path)
        pf.write()
        ...
        pf.unlink_if_ours()

    Static helpers (:meth:`is_active`, :meth:`read_pid`, :meth:`_pid_exists`)
    are available for callers that only need to inspect a PID file.
    """

    def __init__(self, path: Path, pid: int | None = None) -> None:
        self._path = path
        self._pid: int = pid if pid is not None else os.getpid()

    # ── Properties ──────────────────────────────────────────────────

    @property
    def path(self) -> Path:
        """The filesystem path of this PID file."""
        return self._path

    @property
    def pid(self) -> int:
        """The PID written (or to be written) to the file."""
        return self._pid

    # ── Context manager ─────────────────────────────────────────────

    def __enter__(self) -> PidFile:
        self.write()
        return self

    def __exit__(self, *args: object) -> None:
        self.unlink_if_ours()

    # ── Instance methods ────────────────────────────────────────────

    def write(self) -> None:
        """Write our PID to the file, overwriting any existing content."""
        self._path.write_text(str(self._pid), encoding="utf-8")

    def unlink_if_ours(self) -> None:
        """Remove the PID file, but **only** if it contains our PID.

        A concurrent writer may have overwritten the file with its own
        PID (e.g. another ``fw-context index --force`` invocation) —
        in that case the marker must stay so the background reindex
        remains paused for that caller.
        """
        try:
            if self._path.exists():
                content = self._path.read_text(encoding="utf-8").strip()
                if content == str(self._pid):
                    self._path.unlink(missing_ok=True)
        except OSError:
            pass

    # ── Static helpers ──────────────────────────────────────────────

    @staticmethod
    def _pid_exists(pid: int) -> bool:
        """Check whether a process with *pid* is running.

        Uses ``os.kill(pid, 0)`` — signal 0 is an existence check only
        (no signal is actually delivered).  The three outcomes mean different
        things and must not be collapsed:

        - success — the process exists and we may signal it.
        - ``ProcessLookupError`` (ESRCH) — no such process.
        - ``PermissionError`` (EPERM) — the process EXISTS, we simply may not
          signal it.  It belongs to another user.

        Catching bare ``OSError`` reported that last case as dead, and
        :meth:`is_active` deletes the PID file when the process reads as dead.
        A pause marker written by another user's process would therefore be
        removed by a mere liveness check, resuming a background reindex that
        was deliberately held, or letting retention delete the build that
        process is still writing.

        **TOCTOU:** the PID may be reused between the check and the
        action.  Risk is low on Linux (PID wrap at 4M).  Use
        :func:`fcntl.flock` where correctness matters.
        """
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            # Anything else (EINVAL on a bad signal number, for instance) is
            # not evidence of life; treat it as gone rather than guess.
            return False
        return True

    @staticmethod
    def is_active(path: Path) -> bool:
        """Return ``True`` when *path* exists and the process is alive.

        Cleans up stale files (PID not alive or corrupt content)
        automatically — the caller never sees a stale PID file as active.
        """
        if not path.exists():
            return False
        try:
            pid = int(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            path.unlink(missing_ok=True)
            return False
        if PidFile._pid_exists(pid):
            return True
        path.unlink(missing_ok=True)
        return False

    @staticmethod
    def is_active_other(path: Path) -> bool:
        """Return ``True`` when *path* is held by a live OTHER process.

        WHY this exists next to :meth:`is_active`: a pause marker written by
        THIS process must not block this process's own work.  ``fw-context
        index`` writes ``reindex.pause`` with its own PID for the whole run,
        so a guard built on :meth:`is_active` is always true inside that run
        and silently disables whatever it protects.

        Use :meth:`is_active` to answer "is anyone paused?" and this to
        answer "is someone ELSE paused?".  A guard that must not fire on our
        own marker needs the latter.
        """
        if not PidFile.is_active(path):
            return False
        pid = PidFile.read_pid(path)
        return pid is not None and pid != os.getpid()

    @staticmethod
    def read_pid(path: Path) -> int | None:
        """Read a PID from *path*.

        Returns ``None`` when the file does not exist or contains
        garbage.  Corrupt files are cleaned up automatically.
        """
        if not path.exists():
            return None
        try:
            return int(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            path.unlink(missing_ok=True)
            return None
