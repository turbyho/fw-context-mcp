"""Background services — daemon lifecycle, auto-reindex, pause coordination."""

from __future__ import annotations

import fcntl
import logging
import os
import subprocess
import sys
from pathlib import Path

from .shared.context import _db_path

log = logging.getLogger(__name__)

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
    if reindex_pid_file.exists():
        try:
            pid = int(reindex_pid_file.read_text(encoding="utf-8").strip())
            os.kill(pid, 0)  # Signal 0 = existence check only
            return True
        except (OSError, ValueError):
            # PID not alive or garbage — clean up stale file
            reindex_pid_file.unlink(missing_ok=True)

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

    # Spawn the daemon.  It acquires its own watcher.lock on startup.
    # Release our lock before spawning so the daemon can take it.
    fcntl.flock(lock_fd, fcntl.LOCK_UN)
    os.close(lock_fd)

    log.info("Spawning watcher daemon for %s", root)
    try:
        subprocess.Popen(
            [sys.executable, "-u", "-m", "fw_context_mcp.mcp.daemon", str(root)],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        log.exception("Failed to spawn watcher daemon for %s", root)

# ── _request_bg_reindex_pause (was at server.py:414) ──
def _request_bg_reindex_pause(root: Path) -> None:
    """Signal the background reindex to release the write lock.

    Writes ``<pid>`` to ``<db_dir>/<project_id>/reindex.pause`` so the
    bg reindex can check whether the requesting process is still alive.
    The bg process checks this marker between translation units — if the
    requesting PID is dead, it ignores the stale marker and continues.
    """
    db_path = _db_path(root)
    pause_file = db_path.parent / "reindex.pause"
    try:
        pause_file.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass

# ── _resume_bg_reindex (was at server.py:430) ──
def _resume_bg_reindex(root: Path) -> None:
    """Remove the pause marker — the bg reindex may now resume.

    Only removes the marker when it was written by this process.
    A concurrent caller (e.g. ``fw-context index --force``) may have
    overwritten the marker with its own PID — in that case the marker
    must stay so the bg reindex remains paused for that caller.
    """
    db_path = _db_path(root)
    pause_file = db_path.parent / "reindex.pause"
    try:
        if pause_file.exists():
            content = pause_file.read_text(encoding="utf-8").strip()
            if content == str(os.getpid()):
                pause_file.unlink(missing_ok=True)
    except OSError:
        pass

# ── _check_bg_pause (was at server.py:440) ──
def _check_bg_pause(root: Path) -> bool:
    """Check whether a pause was requested and the requester is still alive.

    Returns True if the bg reindex should pause (pause marker exists AND
    the requesting PID is still running).  Returns False if there is no
    pause marker or the requesting process has died (stale marker —
    cleaned up automatically).
    """
    db_path = _db_path(root)
    pause_file = db_path.parent / "reindex.pause"
    if not pause_file.exists():
        return False
    try:
        content = pause_file.read_text(encoding="utf-8").strip()
        requester_pid = int(content)
    except (OSError, ValueError):
        try:
            pause_file.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    # Check if the requesting process is still alive
    try:
        os.kill(requester_pid, 0)
    except OSError:
        # Process dead — clean up stale marker
        try:
            pause_file.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True

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
    from ..indexer.db import (
        CURRENT_SCHEMA_VERSION,
        get_active_config,
        get_db_schema_version,
    )
    from .shared.context import _db_path, _is_stale, _open_db_safe

    db_path = _db_path(root)
    if not db_path.exists():
        return False, []

    conn, err = _open_db_safe(db_path)
    if err:
        return False, []
    assert conn is not None
    reasons: list[str] = []
    try:
        with conn:
            project_id = derive_project_id(root)
            cfg = get_active_config(conn, project_id)
            if not cfg:
                return False, []
            config_hash = cfg["config_hash"]

            # 1. compile_commands.json changed? (one stat call)
            cc_path = cfg["compile_commands_path"]
            if _is_stale(cfg, cc_path):
                reasons.append("compile_commands.json changed")

            # 2. Schema version mismatch?
            schema_ver = get_db_schema_version(conn)
            if schema_ver < CURRENT_SCHEMA_VERSION:
                reasons.append(f"schema {schema_ver} < {CURRENT_SCHEMA_VERSION}")

            # 3. Missing refs or indirect call sites?
            proj_cfg = load_config(root)
            if proj_cfg.index.index_refs:
                ref_count = conn.execute(
                    "SELECT COUNT(*) FROM refs WHERE config_hash=?",
                    (config_hash,),
                ).fetchone()[0]
                if ref_count == 0:
                    reasons.append("refs missing")
                    # Also check indirect call sites — but only flag them
                    # when refs are missing (if refs were populated, the
                    # indirect extraction ran too; empty table is legitimate).
                    ics_count = conn.execute(
                        "SELECT COUNT(*) FROM indirect_call_sites WHERE config_hash=?",
                        (config_hash,),
                    ).fetchone()[0]
                    if ics_count == 0:
                        reasons.append("indirect call sites missing")

            # 4. Unanalyzed symbols?
            # Uses CONFIG analyze_vendor (not stored) because this check
            # predicts what the background reindex will do — and the
            # background reindex uses config, not stored flags.  Using
            # stored would cause an infinite reindex loop when a manual
            # --analyze-vendor run stored True but config is False.
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
                             AND s.id NOT IN (SELECT symbol_id FROM llm_analysis)""",
                        (config_hash,),
                    ).fetchone()[0]
                else:
                    from .shared.filtering import compute_exclude_like

                    exclude_like = compute_exclude_like(
                        root,
                        analyze_vendor=False,
                        exclude_paths=proj_cfg.index.exclude_paths,
                    )
                    exclude_clauses = " AND ".join(
                        ["s.file_path NOT LIKE ?"] * len(exclude_like)
                    )
                    exclude_clause = (" AND " + exclude_clauses) if exclude_clauses else ""
                    query = f"""SELECT COUNT(*)
                           FROM symbols s
                           WHERE s.config_hash = ?
                             AND s.is_definition = 1
                             AND s.kind IN ('function', 'method',
                                            'constructor', 'destructor',
                                            'class', 'struct')
                             {exclude_clause}
                             AND s.name NOT LIKE '%(anonymous%'
                             AND s.name NOT LIKE '%(unnamed%'
                             AND s.id NOT IN (SELECT symbol_id FROM llm_analysis)"""
                    unanalyzed = conn.execute(
                        query, (config_hash, *exclude_like),
                    ).fetchone()[0]
                if unanalyzed > 0:
                    reasons.append(f"{unanalyzed} unanalyzed symbols")
    finally:
        conn.close()

    return len(reasons) > 0, reasons
