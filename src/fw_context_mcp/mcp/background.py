"""Background services — daemon lifecycle, auto-reindex, pause coordination.

The background module solves a coordination problem: multiple MCP server
processes (one per AI assistant instance) share a single per-project index.
They must agree on who spawns the watcher daemon, who detects staleness,
and how to pause the background reindex during write operations.

Key design decisions:

- **Daemon spawning uses a kernel file lock (not PID files) for mutual exclusion**:
  PID files have an inherent TOCTOU race — the PID may be reused between
  ``read_pid()`` and ``os.kill()``.  :mod:`fw_context_mcp.file_lock` is an
  atomic kernel lock that the kernel releases when its process ends.  The
  daemon holds ``watcher.lock`` for its whole life, and a second daemon
  exits at its try of the lock.  ``_ensure_daemon_running`` releases its
  try of the lock before it spawns the daemon.

- **The check of the work that waits is in the daemon**: the daemon's
  ``_staleness_check`` runs one time at its start.  It finds the work of the
  time when no daemon watched: the structural reasons, files that changed,
  a checkout and new source files.  watchfiles finds only NEW changes.

- **The index lock of a project**: each writer of the index holds it
  (``indexer._index_owner``): an index run, ``fw-context analyze``, the MCP
  tools ``reindex_file`` and ``reset_index``, and ``fw-context db``.  A
  manual operation takes the index over from a background run; the daemon
  waits while another process holds the index (``daemon._index_held``).
  Before, an MCP tool only wrote a pause marker, which a running index run
  checked between two translation units.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from pathlib import Path

from ..file_lock import held_by_another, try_lock, unlock
from ..utils import O_NOFOLLOW
from .shared.context import _db_path
from .shared.pid_file import PidFile

log = logging.getLogger(__name__)


def _pid_exists(pid: int) -> bool:
    """Check whether a process with the given PID is running.

    Delegates to :meth:`PidFile._pid_exists`.
    """
    return PidFile._pid_exists(pid)


# The daemon serves each MCP server of the project, thus it must not end with
# the terminal or the console of the process that started it, and a Ctrl-C
# there must not reach it.
#
# - POSIX: a new session (start_new_session), thus no SIGHUP and no SIGINT of
#   the terminal.  creationflags must be 0 there.
# - Windows: start_new_session does nothing there, and the daemon shared the
#   console of its parent: the close of that console or a Ctrl-C in it ended
#   the daemon.  CREATE_NO_WINDOW gives the daemon a console of its own with
#   no window, and its index runs and their builds get that console too, thus
#   no window opens for them.  CREATE_NEW_PROCESS_GROUP keeps a Ctrl-C of a
#   console from the daemon.
if sys.platform == "win32":
    _DAEMON_CREATIONFLAGS = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    _DAEMON_NEW_SESSION = False
else:
    _DAEMON_CREATIONFLAGS = 0
    _DAEMON_NEW_SESSION = True


def _spawn_daemon(root: Path) -> None:
    """Spawn the watcher daemon, logging output to a file.

    Must be called without holding ``watcher.lock`` so the daemon can
    acquire it on startup.
    """
    db_path = _db_path(root)
    log_file = db_path.parent / "daemon.log"
    try:
        fd = os.open(log_file, O_NOFOLLOW | os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        log_fh = os.fdopen(fd, "a", encoding="utf-8", closefd=True)
        subprocess.Popen(  # noqa: S603 — argv list, no shell
            [sys.executable, "-u", "-m", "fw_context_mcp.mcp.daemon", str(root)],
            # Not the stdin of this process: in an MCP server that is the
            # JSON-RPC pipe of the client, and a daemon that held it kept the
            # pipe open after the end of the server.
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=_DAEMON_NEW_SESSION,
            creationflags=_DAEMON_CREATIONFLAGS,
        )
        log_fh.close()
    except (OSError, ValueError, TypeError, RuntimeError, AttributeError):
        log.exception("Failed to spawn watcher daemon for %s", root)

# ── _is_bg_reindex_running (was at server.py:367) ──
def _is_bg_reindex_running(root: Path) -> bool:
    """Check whether any index process is running for *root*.

    Checks two signals:

    1. ``<db_dir>/<project_id>/reindex.pid`` — each ``fw-context index``
       run writes it when it holds the index lock
       (``indexer._index_owner.claim_index``), the run that the daemon started
       included, and removes it when it releases the lock.  A stale file
       (PID not alive) is cleaned up.
    2. ``<db_dir>/write.lock`` — held during write operations
       (``reindex_file``, ``reset_index``, index write phases).

    If either signal is active, another index process is active — skip
    launching a duplicate.

    Note: ``watcher.lock`` is NOT checked here — the persistent daemon
    holds it for its entire lifetime, even when idle (just watching for
    file changes).  Checking it was a false positive that made every
    ``get_project_status`` call report "reindexing" when the daemon was
    merely running.
    """
    db_path = _db_path(root)
    if not db_path.exists():
        return False

    # 1. Index PID file — daemon subprocess or standalone fw-context index
    reindex_pid_file = db_path.parent / "reindex.pid"
    if PidFile.is_active(reindex_pid_file):
        return True

    # 2. General write lock — held during reindex_file, reset_index, etc.
    if held_by_another(db_path.parent / "write.lock"):
        return True

    return False


# The marker of the work that the daemon has and did not complete: a change
# that waits for the debounce, for a manual operation that holds the index,
# or for the run that the daemon started.  It holds the PID of the daemon,
# thus a daemon that died leaves a marker that nobody reads as work.
PENDING_MARKER = "reindex.pending"


def read_reindex_state(root: Path) -> str | None:
    """Return ``"reindexing"`` while an index run is active, ``"pending"`` while the daemon has work that is not complete, else None.

    WHY the two: an LLM that waits for a reindex must know that one comes.
    The daemon keeps its work as a flag in its memory, thus before this
    marker no other process saw a change that waited for the debounce or
    for a manual operation, and the answers said "run fw-context index"
    while the daemon was about to run it.  A run that holds the index
    makes "reindexing", also when the marker is there.
    """
    if _is_bg_reindex_running(root):
        return "reindexing"
    db_path = _db_path(root)
    if PidFile.is_active(db_path.parent / PENDING_MARKER):
        return "pending"
    return None


# ── read_reindex_progress ────────────────────────────────────────────────────
# How a fw-context line starts.  Three shapes, and nothing else writes them:
#
# * ``HH:MM:SS LEVEL `` — the logging format of a background index run,
#   ``cli/_index.py`` (``datefmt="%H:%M:%S"``).  ``--verbose`` picks the
#   framed VerboseFormatter instead, but the daemon never passes ``-v``.
# * ``fw-context: error: `` — a CLI diagnostic.
# * ``Project: `` — the header, printed before the first log record.
_REINDEX_RECORD_START = re.compile(
    r"^(?:\d{2}:\d{2}:\d{2} \w+ |fw-context: \w+: |Project: )"
)

# How much of the tail to read.  A record start must be inside it, and a
# failed build can quote up to 2000 characters of each stream of another
# tool ahead of one (``utils._BUILD_OUTPUT_TAIL``) — see the docstring below.  64 KiB holds hundreds of lines
# and keeps a multi-megabyte log off the query path.
_REINDEX_TAIL_BYTES = 65536


def read_reindex_progress(db_dir: Path) -> str | None:
    """Return the newest line the index run itself wrote to ``reindex.log``.

    Not the last line of the file.  ``daemon._run_index_async`` sends the
    stdout AND the stderr of ``fw-context index --background`` to that
    file, and a build runs inside that process, so the file also holds the
    output of west, cmake, ninja, and the shell that started them.  The
    last line is often one of theirs: a run in the Zephyr project ended on a
    bare ``lean-ctx:``, and both readers of this log — ``get_project_status``
    and ``fw-context watch status`` — reported that text as the progress.

    The error text of fw-context is not safe to take either.  When a build
    command fails, ``utils.run_build_command`` puts up to 2000 characters
    of the stdout and of the stderr of the tool into the message
    (``utils._BUILD_OUTPUT_TAIL``), so the record ends on a line that
    fw-context printed but did not write.

    So this reads backwards for the newest line that STARTS a fw-context
    record and gives that line.  After a failed build it is the
    ``fw-context: error:`` line, which names the failure, and not the
    quoted tool output below it.

    *db_dir* is the directory that holds ``index.db``.  Returns None when
    the file is absent, is empty, or holds no record start in its tail.
    """
    log_file = db_dir / "reindex.log"
    start = 0
    try:
        # Binary, and NOT text with a seek.  A text-mode seek to
        # ``size - N`` can land inside a UTF-8 sequence, and the read then
        # raises UnicodeDecodeError — which this function did not catch.
        # The log carries "→" and "—" in ordinary progress lines, so the
        # boundary is reachable.
        with open(log_file, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            if size == 0:
                return None
            start = max(0, size - _REINDEX_TAIL_BYTES)
            fh.seek(start)
            tail = fh.read()
    except OSError:
        return None

    lines = tail.decode("utf-8", errors="replace").splitlines()
    # A tail that begins mid-file begins mid-line.  Drop that fragment: a
    # cut line cannot be reported as a whole one, and its start is gone.
    if start > 0 and lines:
        del lines[0]
    for line in reversed(lines):
        if _REINDEX_RECORD_START.match(line):
            return line.strip()
    return None


# ── _ensure_daemon_running ───────────────────────────────────────────────────
def _ensure_daemon_running(root: Path) -> None:
    """Make sure the persistent watcher daemon is running for *root*.

    Uses *watcher.lock* (a kernel file lock) as the daemon-alive indicator.
    The daemon holds the lock for its whole life.  This function checks
    the ping first (fast path), then tries the lock.

    WHY the lock is free when the daemon starts: the daemon takes the lock
    with a try that does not wait, and it exits when a different file
    descriptor holds it.  A wait in the daemon is not a solution, because
    a second daemon would then wait for the end of the first one.  Before,
    this function held the lock until after the spawn: a daemon that came
    to its lock call first exited, and no daemon ran until the next ping
    cycle.

    Two MCP servers can spawn a daemon at the same time.  The daemon that
    gets the lock first runs, and the other one exits at its lock call
    before it writes a marker or its PID file (``daemon_main``).  Thus a
    second spawn costs one process start and one line in ``daemon.log``,
    and no second daemon runs.
    """
    from .daemon import ping_daemon

    db_path = _db_path(root)
    if not db_path.exists():
        return
    db_dir = db_path.parent

    # Fast path — daemon is already running
    if ping_daemon(root):
        return

    # Slow path — daemon may be dead or starting up
    lock_file = db_dir / "watcher.lock"
    try:
        lock_fd = os.open(lock_file, os.O_CREAT | os.O_RDWR)
    except OSError:
        return
    try:
        if not try_lock(lock_fd):
            # Lock held — a daemon runs, or a daemon starts.
            return
        try:
            # We hold the lock — no daemon holds it.  A daemon that started
            # between the ping and the try of the lock answers the ping now.
            daemon_answers = ping_daemon(root)
        finally:
            unlock(lock_fd)
    except OSError:
        return
    finally:
        os.close(lock_fd)
    if daemon_answers:
        return

    log.info("Spawning watcher daemon for %s", root)
    _spawn_daemon(root)
