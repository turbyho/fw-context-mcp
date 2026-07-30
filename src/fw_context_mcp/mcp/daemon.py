"""Per-project background daemon: watches source files, runs ``fw-context index`` on
changes, exits when all MCP clients disconnect.

One daemon per project, shared by all MCP server instances over that project.
Communication is via a Unix domain socket in ``<db_dir>/<project_id>/daemon.sock``.

Lifecycle
---------
1. First MCP server spawns the daemon (via ``_ensure_daemon_running``).
2. Each MCP server pings the daemon every 15 s over the socket.
3. If no ping arrives for 60 s, the daemon exits (all clients gone).
4. SIGTERM / SIGINT → clean shutdown, signal forwarded to index subprocess.

Isolation
---------
Each project has its own socket file at ``<db_dir>/<project_id>/daemon.sock``.
Pings from an MCP server on project A never affect project B's daemon.
"""

from __future__ import annotations

import fcntl
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from ..config import derive_project_id
from ..config import load as load_config

log = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────
DAEMON_SOCK_NAME = "daemon.sock"
PING_INTERVAL = 15   # How often each MCP server pings (seconds)
PING_TIMEOUT = 60    # Exit after this long without any ping (seconds)
_DEBOUNCE_S = 2.0    # Collect changes before spawning index
_WATCH_TIMEOUT = 5   # watchfiles yield_on_timeout interval (seconds)

# ── Watched file extensions ──────────────────────────────────────────────────
_SOURCE_EXTS_WATCH = frozenset({".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hh", ".hxx"})
_EXCLUDE_RX = re.compile(r"(/\.git/|/\.pio/|/build/|/__pycache__/|/node_modules/)")


# ── Public helpers ───────────────────────────────────────────────────────────


def _get_db_dir(project_root: Path) -> Path:
    """Return ``<index_dir>/<project_id>/`` for *project_root*."""
    cfg = load_config(project_root=project_root)
    project_id = derive_project_id(project_root)
    return cfg.index.db_dir / project_id


def _socket_path(db_dir: Path) -> Path:
    """Return the Unix domain socket path for a project."""
    return db_dir / DAEMON_SOCK_NAME


def ping_daemon(project_root: Path) -> bool:
    """Send a ping to the project daemon.

    Opens a connection to ``daemon.sock``, sends ``"ping\\n"``, and closes.
    Returns ``True`` when the daemon accepted the connection; ``False`` when
    the socket does not exist or no daemon is listening.
    """
    db_dir = _get_db_dir(project_root)
    sock_path = _socket_path(db_dir)
    if not sock_path.exists():
        return False
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        sock.connect(str(sock_path))
        sock.sendall(b"ping\n")
        sock.close()
        return True
    except OSError:
        return False


# ── Daemon main ──────────────────────────────────────────────────────────────


def daemon_main(project_root: Path) -> None:
    """Run the watcher daemon for *project_root*.

    Does not return until shutdown (SIGTERM / SIGINT / ping timeout).
    Intended to be called as a standalone process::

        python -m fw_context_mcp.mcp.daemon <project_root>
    """
    db_dir = _get_db_dir(project_root)
    db_dir.mkdir(parents=True, exist_ok=True)

    # ── Acquire watcher lock ─────────────────────────────────────────────
    lock_file = db_dir / "watcher.lock"
    try:
        lock_fd = os.open(lock_file, os.O_CREAT | os.O_RDWR)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        log.error("Daemon already running for %s", project_root)
        sys.exit(1)

    pid_file = db_dir / "watcher.pid"
    # O_EXCL | O_CREAT does not follow symlinks — defense against symlink attack
    try:
        wfd = os.open(str(pid_file), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        # Stale PID file from a previous crash — clean up and retry
        pid_file.unlink(missing_ok=True)
        wfd = os.open(str(pid_file), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(wfd, "w", encoding="utf-8") as pf:
        pf.write(str(os.getpid()))

    # ── Socket setup ─────────────────────────────────────────────────────
    sock_path = _socket_path(db_dir)

    server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server_sock.bind(str(sock_path))
    except OSError:
        # Socket exists but nobody listening — clean up and retry
        sock_path.unlink(missing_ok=True)
        try:
            server_sock.bind(str(sock_path))
        except OSError:
            log.error("Cannot bind socket %s — daemon already running?", sock_path)
            _cleanup_lock_and_exit(lock_fd, lock_file, pid_file)
    server_sock.listen(8)
    os.chmod(sock_path, 0o600)
    server_sock.settimeout(1.0)  # accept() timeout → check shutdown flag

    # ── Shared state ─────────────────────────────────────────────────────
    last_ping_time = time.monotonic()
    ping_lock = threading.Lock()
    shutdown = threading.Event()

    # ── Socket thread — accept pings ─────────────────────────────────────
    def _socket_server() -> None:
        """Accept connections on *server_sock*, read pings, update timestamp."""
        nonlocal last_ping_time
        while not shutdown.is_set():
            try:
                conn, _addr = server_sock.accept()
            except TimeoutError:
                continue  # Periodic check for shutdown
            except OSError:
                if not shutdown.is_set():
                    log.debug("Socket accept error", exc_info=True)
                break
            try:
                conn.settimeout(2.0)
                data = conn.recv(1024)
                if b"ping" in data:
                    with ping_lock:
                        last_ping_time = time.monotonic()
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    socket_thread = threading.Thread(
        target=_socket_server, daemon=True, name="fw-daemon-socket",
    )
    socket_thread.start()

    # ── Index subprocess handle (for signal forwarding) ──────────────────
    index_proc: subprocess.Popen | None = None
    _proc_lock = threading.Lock()  # guards index_proc reads/writes

    # ── Wakeup pipe for async-signal-safe shutdown notification ─────────
    _wakeup_r, _wakeup_w = os.pipe()
    signal.set_wakeup_fd(_wakeup_w)

    def _handle_shutdown(signum: int, _frame) -> None:
        """Async-signal-safe: only set shutdown and write to wakeup fd.

        Does NOT call log.info(), proc.terminate(), or any other
        non-async-signal-safe function.  All cleanup happens in the
        main loop when shutdown.is_set() is detected.
        """
        shutdown.set()
        try:
            os.write(_wakeup_w, b"\x00")  # os.write() is async-signal-safe
        except OSError:
            pass  # pipe full — shutdown flag already set

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    # ── Main loop ────────────────────────────────────────────────────────
    from watchfiles import watch

    log.info("Daemon started  root=%s  pid=%d  socket=%s", project_root, os.getpid(), sock_path)

    try:
        # Initial staleness check — run index at startup if needed.
        # Skip when a manual fw-context index holds the pause marker
        # (manual index always wins over background reindex).
        from .background import _check_bg_pause as _bg_paused
        needs, reasons = _staleness_check(project_root)
        if needs and not _bg_paused(project_root):
            log.info("Initial index needed (%s)", ", ".join(reasons))
            force_refs = "refs missing" in reasons
            index_proc = _run_index_async(project_root, db_dir, force_refs=force_refs)
            _wait_index(index_proc, shutdown, db_dir=db_dir)
            index_proc = None

        while not shutdown.is_set():
            # ── Check ping timeout ───────────────────────────────────────
            with ping_lock:
                ping_elapsed = time.monotonic() - last_ping_time
            if ping_elapsed > PING_TIMEOUT:
                log.info("No ping for %.0f s — all clients disconnected, exiting", ping_elapsed)
                break

            # ── Watch for file changes ───────────────────────────────────
            changed = False
            try:
                for changes in watch(
                    project_root,
                    debounce=int(_DEBOUNCE_S * 1000),
                    recursive=True,
                    rust_timeout=_WATCH_TIMEOUT * 1000,
                    yield_on_timeout=True,
                ):
                    if shutdown.is_set():
                        break
                    if _ping_timeout(last_ping_time, ping_lock):
                        break

                    for _, changed_path_str in changes:
                        if _is_source_file(changed_path_str):
                            changed = True
                            break
                    if changed:
                        break  # Exit watch loop → run index
            except (OSError, RuntimeError) as exc:
                log.warning("watchfiles error: %s", exc)
                time.sleep(5)
                continue

            if changed and not shutdown.is_set():
                # Skip when a manual fw-context index holds the pause marker
                from .background import _check_bg_pause as _bg_paused
                if _bg_paused(project_root):
                    log.info("Manual index in progress — skipping background reindex")
                else:
                    index_proc = _run_index_async(project_root, db_dir)
                    _wait_index(index_proc, shutdown, db_dir=db_dir)
                    index_proc = None

    finally:
        log.info("Daemon shutting down for %s", project_root)
        proc = index_proc
        if proc is not None and proc.poll() is None:
            log.info("Terminating index subprocess (pid %d)", proc.pid)
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                log.warning("Index subprocess did not exit, killing")
                proc.kill()
                proc.wait()
        shutdown.set()
        server_sock.close()
        _cleanup_files(sock_path, pid_file)
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


# ── Internal helpers ─────────────────────────────────────────────────────────


def _is_source_file(path_str: str) -> bool:
    """Return True when *path_str* is a C/C++ source file outside excluded dirs."""
    p = Path(path_str)
    return p.suffix.lower() in _SOURCE_EXTS_WATCH and not _EXCLUDE_RX.search(path_str)


def _ping_timeout(last_ping_time: float, lock: threading.Lock) -> bool:
    """Return True when ping timeout has elapsed."""
    with lock:
        return (time.monotonic() - last_ping_time) > PING_TIMEOUT



def _cleanup_lock_and_exit(
    lock_fd: int, lock_file: Path, pid_file: Path,
    server_sock=None,
) -> None:
    """Release lock, remove files, exit."""
    if server_sock is not None:
        try:
            server_sock.close()
        except OSError:
            pass
    fcntl.flock(lock_fd, fcntl.LOCK_UN)
    os.close(lock_fd)
    pid_file.unlink(missing_ok=True)
    os._exit(1)


def _cleanup_files(sock_path: Path, pid_file: Path) -> None:
    """Remove daemon runtime files."""
    sock_path.unlink(missing_ok=True)
    pid_file.unlink(missing_ok=True)


# ── Staleness check (minimal — same spirit as _fast_staleness_check) ─────────


def _staleness_check(project_root: Path) -> tuple[bool, list[str]]:
    """Return ``(needs_reindex, reasons)`` for the initial daemon startup.

    Checks structural staleness (compile_commands.json, schema, refs) AND
    file-level staleness (on-disk mtime newer than stored mtime).  Without
    the file-level check the daemon would never detect files that were
    modified BEFORE it started — watchfiles only detects NEW changes.
    """
    from .shared.context import _db_path, _is_stale
    from .shared.stale import _count_modified_files

    db_path = _db_path(project_root)
    if not db_path.exists():
        return False, []

    conn = _open_db(db_path)
    if conn is None:
        return False, []
    reasons: list[str] = []
    try:
        from ..indexer.db import (
            CURRENT_SCHEMA_VERSION,
            get_active_config,
            get_db_schema_version,
        )

        project_id = derive_project_id(project_root)
        cfg = get_active_config(conn, project_id)
        if not cfg:
            return False, []

        config_hash = cfg["config_hash"]

        # 1. compile_commands.json changed?
        cc_path = cfg["compile_commands_path"]
        if cc_path and _is_stale(cfg, cc_path):
            reasons.append("compile_commands.json changed")

        # 2. Schema version mismatch?
        schema_ver = get_db_schema_version(conn)
        if schema_ver < CURRENT_SCHEMA_VERSION:
            reasons.append(f"schema {schema_ver} < {CURRENT_SCHEMA_VERSION}")

        # 3. Missing refs?
        proj_cfg = load_config(project_root)
        if proj_cfg.index.index_refs:
            ref_count = conn.execute(
                "SELECT COUNT(*) FROM refs WHERE config_hash=?",
                (config_hash,),
            ).fetchone()[0]
            if ref_count == 0:
                reasons.append("refs missing")

        # 4. Modified source files — files changed before daemon started.
        #    watchfiles only detects NEW events, so without this check
        #    already-stale files would never be reindexed.
        modified = _count_modified_files(conn, config_hash, project_root)
        if modified > 0:
            reasons.append(f"{modified} modified files")
    finally:
        conn.close()

    return len(reasons) > 0, reasons


# ── Index subprocess ─────────────────────────────────────────────────────────


def _run_index_async(
    project_root: Path, db_dir: Path, *, force_refs: bool = False,
) -> subprocess.Popen:
    """Spawn ``fw-context index --background``, write stdout to ``reindex.log``.

    Writes the subprocess PID to ``reindex.pid`` so ``_is_bg_reindex_running``
    can reliably detect an active index run (without false positives from
    the daemon's ``watcher.lock``).

    Returns the Popen handle — caller must wait or terminate.
    """
    log_file = db_dir / "reindex.log"
    try:
        log_fh = open(log_file, "w")
    except OSError:
        log_fh = subprocess.DEVNULL  # type: ignore[assignment]

    env: dict[str, str] = {
        **os.environ,
        "PYTHONUNBUFFERED": "1",
    }
    if force_refs:
        env["FW_CONTEXT_FORCE_REFINDEX"] = "1"

    log.info("Starting background index for %s (force_refs=%s)", project_root, force_refs)
    try:
        proc = subprocess.Popen(
            [sys.executable, "-u", "-m", "fw_context_mcp.cli", "index", "--background"],
            cwd=str(project_root),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            env=env,
        )
        # Write PID for health checks — _is_bg_reindex_running reads this
        rp = db_dir / "reindex.pid"
        rfd = os.open(str(rp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(rfd, "w", encoding="utf-8") as pf:
            pf.write(str(proc.pid))
        if log_fh != subprocess.DEVNULL:
            log_fh.close()
    except Exception:
        log.exception("Failed to spawn index subprocess")
        if log_fh != subprocess.DEVNULL:
            log_fh.close()
        raise
    return proc


def _wait_index(proc: subprocess.Popen, shutdown: threading.Event, *, db_dir: Path) -> None:
    """Wait for *proc* to finish, polling *shutdown* every 500 ms.

    When *shutdown* is set, terminates the subprocess gracefully
    (SIGTERM → 10 s → SIGKILL).  Removes ``reindex.pid`` on exit
    so ``_is_bg_reindex_running`` doesn't report a stale PID.
    """
    try:
        while proc.poll() is None:
            if shutdown.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    log.warning("Index subprocess did not exit after SIGTERM, killing")
                    proc.kill()
                    proc.wait()
                return
            time.sleep(0.5)
    finally:
        (db_dir / "reindex.pid").unlink(missing_ok=True)
        # log file handle already closed in _run_index_async (stdout=PIPE never used)

    if proc.returncode == 0:
        log.info("Background index completed")
        _optimize_db(db_dir)
    else:
        log.warning("Background index exited with %d", proc.returncode)


# ── DB helper ────────────────────────────────────────────────────────────────


def _open_db(db_path: Path):
    """Open the database, returning ``None`` on corruption."""
    from ..indexer.db import DatabaseCorruptionError, open_db

    try:
        return open_db(db_path)
    except DatabaseCorruptionError:
        log.warning("Database corrupt — skipping staleness check")
        return None


def _optimize_db(db_dir: Path) -> None:
    """Run PRAGMA optimize to shrink the WAL file and defragment indexes."""
    db_path = db_dir / "index.db"
    if not db_path.exists():
        return
    conn = _open_db(db_path)
    if conn is None:
        return
    try:
        conn.execute("PRAGMA optimize")
    except Exception:
        log.debug("PRAGMA optimize failed", exc_info=True)
    finally:
        conn.close()


# ── Entry point ───────────────────────────────────────────────────────────────


if __name__ == "__main__":
    import sys as _sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    if len(_sys.argv) < 2:
        print("Usage: python -m fw_context_mcp.mcp.daemon <project_root>", file=_sys.stderr)
        _sys.exit(1)
    target = Path(_sys.argv[1]).resolve()
    if not target.is_dir():
        print(f"Error: {target} is not a valid directory", file=_sys.stderr)
        _sys.exit(1)
    daemon_main(target)
