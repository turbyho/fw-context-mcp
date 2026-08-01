"""PID file operations — write, read, liveness check, stale cleanup.

TOCTOU note: ``os.kill(pid, 0)`` has an inherent race — the PID may be
reused between the check and the action.  Risk is low on Linux (PID wrap
at 4M), and :func:`fcntl.flock` is used where correctness matters
(``watcher.lock``).  The helpers in this module are for **coordination
markers** (pause, reindex-in-progress), not mutual exclusion.
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
        (no signal is actually delivered).

        **TOCTOU:** the PID may be reused between the check and the
        action.  Risk is low on Linux (PID wrap at 4M).  Use
        :func:`fcntl.flock` where correctness matters.
        """
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

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
