"""Background services — daemon lifecycle, auto-reindex, pause coordination.

The background module solves a coordination problem: multiple MCP server
processes (one per AI assistant instance) share a single per-project index.
They must agree on who spawns the watcher daemon, who detects staleness,
and how to pause the background reindex during write operations.

Key design decisions:

- **Daemon spawning uses ``fcntl.flock`` (not PID files) for mutual exclusion**:
  PID files have an inherent TOCTOU race — the PID may be reused between
  ``read_pid()`` and ``os.kill()``.  ``flock`` is an atomic kernel lock that
  survives process death (the kernel releases it automatically).  The spawning
  protocol in ``_ensure_daemon_running`` holds the lock, spawns the daemon,
  then releases — the daemon blocks on its own ``flock`` acquisition and
  picks up the lock as soon as the spawner releases.  No race window.

- **Staleness check is split into two tiers**: ``_fast_staleness_check``
  (called via ``get_active_build``) does only structural checks — schema
  version, compile_commands.json mtime, missing refs.  The daemon's
  ``_staleness_check`` adds file-level mtime comparison because the daemon
  needs to detect files that changed BEFORE it started (watchfiles only
  detects NEW changes).

- **Pause/resume protocol**: before an MCP tool writes to the index
  (reindex_file_impl), it calls ``_request_bg_reindex_pause`` to prevent
  the daemon's background reindex from holding the write lock.  The pause
  marker is a PID file — if the requesting process dies, the daemon detects
  the stale marker and resumes automatically.  This is safe because the
  background reindex checks the marker BETWEEN translation units, not
  mid-operation.
"""

from __future__ import annotations

import fcntl
import logging
import os
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

from .shared.context import _db_path
from .shared.pid_file import PidFile

log = logging.getLogger(__name__)


def _pid_exists(pid: int) -> bool:
    """Check whether a process with the given PID is running.

    Delegates to :meth:`PidFile._pid_exists`.
    """
    return PidFile._pid_exists(pid)


def _spawn_daemon(root: Path) -> None:
    """Spawn the watcher daemon, logging output to a file.

    Must be called without holding ``watcher.lock`` so the daemon can
    acquire it on startup.
    """
    db_path = _db_path(root)
    log_file = db_path.parent / "daemon.log"
    try:
        fd = os.open(log_file, os.O_NOFOLLOW | os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        log_fh = os.fdopen(fd, "a", encoding="utf-8", closefd=True)
        subprocess.Popen(
            [sys.executable, "-u", "-m", "fw_context_mcp.mcp.daemon", str(root)],
            start_new_session=True,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
        )
        log_fh.close()
        # Daemon process runs independently via start_new_session=True — no detach needed
    except (OSError, ValueError, TypeError, RuntimeError, AttributeError):
        log.exception("Failed to spawn watcher daemon for %s", root)

# ── _is_bg_reindex_running (was at server.py:367) ──
def _is_bg_reindex_running(root: Path) -> bool:
    """Check whether any index process is running for *root*.

    Checks two signals:

    1. ``<db_dir>/<project_id>/reindex.pid`` — written by the daemon's
       index subprocess or a standalone ``fw-context index`` at startup,
       removed on exit.  A stale file (PID not alive) is cleaned up.
    2. ``<db_dir>/write.lock`` — held during write operations
       (``reindex_file``, ``reset_index``, index write phases).

    If either signal is active, another index process is active — skip
    launching a duplicate.

    Note: ``watcher.lock`` is NOT checked here — the persistent daemon
    holds it for its entire lifetime, even when idle (just watching for
    file changes).  Checking it was a false positive that made every
    ``get_active_build`` call report "reindexing" when the daemon was
    merely running.
    """
    db_path = _db_path(root)
    if not db_path.exists():
        return False

    def _lock_held(lock_file: Path) -> bool:
        try:
            lock_fd = os.open(lock_file, os.O_CREAT | os.O_RDWR)
        except OSError:
            return False
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(lock_fd)
            return True
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        return False

    # 1. Index PID file — daemon subprocess or standalone fw-context index
    reindex_pid_file = db_path.parent / "reindex.pid"
    if PidFile.is_active(reindex_pid_file):
        return True

    # 2. General write lock — held during reindex_file, reset_index, etc.
    if _lock_held(db_path.parent / "write.lock"):
        return True

    return False


# ── _ensure_daemon_running ───────────────────────────────────────────────────
def _ensure_daemon_running(root: Path) -> None:
    """Make sure the persistent watcher daemon is running for *root*.

    Uses *watcher.lock* (fcntl) as both daemon-alive indicator and spawn
    guard.  The daemon acquires the lock at startup; this function checks
    the ping first (fast path), then falls back to the lock for spawn
    coordination.
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
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # Lock held — daemon is running or another MCP server is spawning it
        return
    except OSError:
        return

    # We hold the lock — daemon is definitely dead (or never started).
    # Double-check ping in case of a race (daemon started between ping and lock).
    if ping_daemon(root):
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        return

    # ── Spawn-before-release protocol ──
    # The daemon's main() blocks on LOCK_EX acquisition of watcher.lock.
    # We spawn the daemon WHILE holding our lock.  The daemon starts,
    # reaches its fcntl.flock(LOCK_EX) call, and blocks (because WE
    # hold the lock).  Then we release — the daemon immediately acquires
    # the lock and enters its main loop.
    #
    # Without this protocol, a second MCP server arriving between our
    # release and the daemon's acquisition would see the lock free,
    # conclude "daemon dead," and spawn a SECOND daemon — leading to
    # two daemons racing to run background reindexes.
    log.info("Spawning watcher daemon for %s", root)
    _spawn_daemon(root)

    fcntl.flock(lock_fd, fcntl.LOCK_UN)
    os.close(lock_fd)

# ── _request_bg_reindex_pause (was at server.py:414) ──
def _request_bg_reindex_pause(root: Path) -> None:
    """Signal the background reindex to release the write lock.

    Writes ``<pid>`` to ``<db_dir>/<project_id>/reindex.pause`` so the
    bg reindex can check whether the requesting process is still alive.
    The bg process checks this marker between translation units — if the
    requesting PID is dead, it ignores the stale marker and continues.
    """
    db_path = _db_path(root)
    PidFile(db_path.parent / "reindex.pause").write()

# ── _resume_bg_reindex (was at server.py:430) ──
def _resume_bg_reindex(root: Path) -> None:
    """Remove the pause marker — the bg reindex may now resume.

    Only removes the marker when it was written by this process.
    A concurrent caller (e.g. ``fw-context index --force``) may have
    overwritten the marker with its own PID — in that case the marker
    must stay so the bg reindex remains paused for that caller.
    """
    db_path = _db_path(root)
    PidFile(db_path.parent / "reindex.pause").unlink_if_ours()


# ── bg_reindex_pause — context manager for safe pause/resume ──
@contextmanager
def bg_reindex_pause(root: Path):
    """Context manager: pause background reindex, auto-resume on exit.

    Safe for early returns, exceptions — resume always runs in finally.
    Usage::

        with bg_reindex_pause(root):
            ...  # critical section — bg reindex is paused
    """
    _request_bg_reindex_pause(root)
    try:
        yield
    finally:
        _resume_bg_reindex(root)

# ── _check_bg_pause (was at server.py:440) ──
def _check_bg_pause(root: Path) -> bool:
    """Check whether a pause was requested and the requester is still alive.

    Returns True if the bg reindex should pause (pause marker exists AND
    the requesting PID is still running).  Returns False if there is no
    pause marker or the requesting process has died (stale marker —
    cleaned up automatically).
    """
    db_path = _db_path(root)
    return PidFile.is_active(db_path.parent / "reindex.pause")

# ── _fast_staleness_check ────────────────────────────────────────────────────
def _fast_staleness_check(root: Path) -> tuple[bool, list[str]]:
    """Lightweight staleness check — COUNT queries and at most one ``stat()``.

    Returns ``(needs_reindex, reasons)`` where *reasons* is a list of
    human-readable strings explaining why a reindex is needed.

    Does **not** call ``_count_modified_files`` — the background subprocess
    does its own per-file mtime comparison during ``run()``.  This function
    only detects structural issues (missing data, schema changes,
    compile_commands.json modification).
    """
    from ..config import derive_project_id
    from ..config import load as load_config
    from ..indexer.db import get_active_config
    from .shared.context import _db_path, _quick_open_readonly
    from .shared.stale import check_structural_staleness

    db_path = _db_path(root)
    if not db_path.exists():
        return False, []

    # Read-only quick open: a full open_db would run ensure_schema's
    # unconditional executescript — a write transaction on every check
    # interval — causing lock contention with the executor.  This path
    # only reads.
    try:
        conn = _quick_open_readonly(db_path)
    except sqlite3.Error:
        return False, []
    try:
        reasons: list[str] = []
        project_id = derive_project_id(root)
        cfg = get_active_config(conn, project_id)
        if not cfg:
            return False, []
        config_hash = cfg["config_hash"]

        # 1-3. Structural checks (shared with daemon._staleness_check)
        reasons.extend(check_structural_staleness(conn, config_hash, dict(cfg), root))

        # 4. Unanalyzed symbols?
        # Uses CONFIG analyze_vendor (not stored) because this check
        # predicts what the background reindex will do — and the
        # background reindex uses config, not stored flags.  Using
        # stored would cause an infinite reindex loop when a manual
        # --analyze-vendor run stored True but config is False.
        proj_cfg = load_config(root)
        if proj_cfg.llm.enabled and proj_cfg.llm.analyze_symbols:
            if proj_cfg.llm.analyze_vendor:
                unanalyzed = conn.execute(
                    """SELECT COUNT(*)
                       FROM symbols s
                       WHERE s.config_hash = ?
                         AND s.is_definition = 1
                         AND s.kind IN ('function', 'method',
                                        'constructor', 'destructor',
                                        'class', 'struct')
                         AND s.name NOT LIKE '%(anonymous%'
                         AND s.name NOT LIKE '%(unnamed%'
                         AND NOT EXISTS (SELECT 1 FROM llm_analysis a WHERE a.symbol_id = s.id)""",
                    (config_hash,),
                ).fetchone()[0]
            else:
                # Use is_project column directly for unanalyzed symbol count
                unanalyzed = conn.execute(
                    """SELECT COUNT(*)
                       FROM symbols s
                       WHERE s.config_hash = ?
                         AND s.is_definition = 1
                         AND s.is_project = 1
                         AND s.kind IN ('function', 'method',
                                        'constructor', 'destructor',
                                        'class', 'struct')
                         AND s.name NOT LIKE '%(anonymous%'
                         AND s.name NOT LIKE '%(unnamed%'
                         AND NOT EXISTS (SELECT 1 FROM llm_analysis a WHERE a.symbol_id = s.id)""",
                    (config_hash,),
                ).fetchone()[0]
            if unanalyzed > 0:
                reasons.append(f"{unanalyzed} unanalyzed symbols")
        return len(reasons) > 0, reasons
    finally:
        conn.close()

