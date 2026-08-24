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

Design rationale
----------------
WHY a per-project daemon instead of a single system-wide watcher:
- Each project has a different root directory, index database, and
  build configuration.  A single watcher would need to multiplex
  watchfiles across unrelated directory trees, complicating error
  handling (one project's NFS mount hangs → all projects stall).
- Socket-based ping-per-project gives clean client counting:
  when no MCP server pings project A's daemon for 60 s, that daemon
  exits — independent of project B's activity.

WHY Unix domain sockets instead of a message queue or shared memory:
- Sockets work on all Unix-like systems without additional dependencies.
- No message broker process needed (ZeroMQ, Redis, etc.).
- Socket existence doubles as a daemon-alive indicator for ``ping_daemon``.
- 0o600 permissions prevent other users on the same machine from sending
  spurious pings.

WHY ``watchfiles`` instead of ``inotify`` directly:
- ``watchfiles`` provides a cross-platform polling + inotify backend with
  debouncing built in.  Raw inotify requires manual event coalescing
  (a ``git checkout`` generates hundreds of events).

WHY ``debounce=2000 ms``: ``git checkout``, ``git pull``, and IDE
auto-save generate bursts of file-change events.  Without debouncing,
the daemon would spawn a ``fw-context index`` for each event — then
kill and restart it for the next event arriving 20 ms later.  The 2 s
window collects all rapid changes into a single reindex run.

WHY 60 s ping timeout (not shorter): the MCP server pings every 15 s.
Four missed pings = 60 s.  This gives headroom for:
- MCP server process being paged out under memory pressure.
- Temporary socket buffer saturation.
- Brief NFS hangs on the project directory.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import re
import signal
import socket
import sqlite3
import sys
import time
from pathlib import Path

from ..config import derive_project_id
from ..config import load as load_config
from ..indexer.runner import EXIT_SUPERSEDED
from .shared.pid_file import PidFile

log = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────
DAEMON_SOCK_NAME = "daemon.sock"
PING_INTERVAL = 15   # How often each MCP server pings (seconds)
PING_TIMEOUT = 60    # Exit after this long without any ping (seconds)
_DEBOUNCE_S = 2.0    # Collect changes before spawning index
_WATCH_TIMEOUT = 5   # watchfiles yield_on_timeout interval (seconds)
# A reindex abandoned because a manual operation took the index over is
# retried, not dropped — its changes are still unindexed.  Bounded because a
# manual operation that keeps re-taking the index would otherwise spin here;
# after this the next file change picks the work up.
_SUPERSEDED_RETRIES = 3
_PAUSE_WAIT_S = 120.0  # How long to wait for a manual operation to release

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
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(2.0)
        sock.connect(str(sock_path))
        sock.sendall(b"ping\n")
        return True
    except OSError:
        return False
    finally:
        sock.close()


# ── Daemon main (asyncio) ────────────────────────────────────────────────────


async def daemon_main(project_root: Path) -> None:
    """Run the watcher daemon for *project_root*.

    Does not return until shutdown (SIGTERM / SIGINT / ping timeout).
    Intended to be run as a standalone process::

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

    # ── Shared state ─────────────────────────────────────────────────────
    # No lock needed — asyncio runs in a single thread; all accesses to
    # last_ping_time are cooperative and never preempted mid-operation.
    last_ping_time: float = time.monotonic()
    shutdown: asyncio.Event = asyncio.Event()
    index_proc: asyncio.subprocess.Process | None = None

    # ── Unix socket server ───────────────────────────────────────────────
    def _on_ping() -> None:
        nonlocal last_ping_time
        last_ping_time = time.monotonic()

    try:
        server, sock_path = await _setup_unix_socket(db_dir, _on_ping)
    except OSError:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
        pid_file.unlink(missing_ok=True)
        sys.exit(1)

    # ── Signal handlers ──────────────────────────────────────────────────
    _setup_signal_handlers(shutdown)

    # ── Main loop ────────────────────────────────────────────────────────
    from watchfiles import awatch

    # ── Structured logger — attach project context for log aggregation ──
    dlog = logging.LoggerAdapter(
        log,
        {"project_root": str(project_root), "pid": os.getpid()},
    )

    dlog.info("Daemon started  socket=%s", sock_path)

    try:
        # ── Initial staleness check ──────────────────────────────────────
        # Runs before the socket server is accepting pings, so blocking the
        # event loop briefly with a synchronous DB call is harmless.
        from .background import _check_bg_pause as _bg_paused
        needs, reasons = await asyncio.get_running_loop().run_in_executor(
            None, _staleness_check, project_root
        )
        if needs and not _bg_paused(project_root):
            dlog.info("Initial index needed (%s)", ", ".join(reasons))
            force_refs = "refs missing" in reasons
            index_proc = await _run_index_async(project_root, db_dir, force_refs=force_refs)
            await _wait_index(index_proc, shutdown, db_dir=db_dir)
            index_proc = None

        # Load build-dir exclusion patterns from manifest (derived from
        # SDK detection + [index] exclude_paths config).  Supplements the
        # hardcoded _EXCLUDE_RX during file-change filtering.
        build_patterns = await asyncio.get_running_loop().run_in_executor(
            None, _load_build_patterns, db_dir
        )

        while not shutdown.is_set():
            # ── Check ping timeout ───────────────────────────────────────
            ping_elapsed = time.monotonic() - last_ping_time
            if ping_elapsed > PING_TIMEOUT:
                dlog.info(
                    "No ping for %.0f s — all clients disconnected, exiting",
                    ping_elapsed,
                )
                break

            # ── Watch for file changes ───────────────────────────────────
            changed = False
            try:
                async for changes in awatch(
                    project_root,
                    debounce=int(_DEBOUNCE_S * 1000),
                    recursive=True,
                    rust_timeout=_WATCH_TIMEOUT * 1000,
                    yield_on_timeout=True,
                ):
                    if shutdown.is_set():
                        break
                    ping_elapsed = time.monotonic() - last_ping_time
                    if ping_elapsed > PING_TIMEOUT:
                        break

                    for _, changed_path_str in changes:
                        if _is_source_file(changed_path_str, build_patterns):
                            changed = True
                            break
                    if changed:
                        break  # Exit watch loop → run index
            except (OSError, RuntimeError) as exc:
                dlog.warning("watchfiles error: %s", exc)
                await asyncio.sleep(5)
                continue

            if changed and not shutdown.is_set():
                # Skip when a manual fw-context index holds the pause marker
                from .background import _check_bg_pause as _bg_paused
                if _bg_paused(project_root):
                    dlog.info("Manual index in progress — skipping background reindex")
                else:
                    # A manual operation can take the index over mid-run, and
                    # the run then abandons itself rather than finishing from
                    # a snapshot that operation invalidated.  Its work is
                    # still outstanding, so retry once the marker clears
                    # instead of falling back to waiting for another change —
                    # the edits that triggered this would otherwise stay
                    # unindexed indefinitely.
                    for _attempt in range(_SUPERSEDED_RETRIES):
                        index_proc = await _run_index_async(project_root, db_dir)
                        superseded = await _wait_index(
                            index_proc, shutdown, db_dir=db_dir
                        )
                        index_proc = None
                        if not superseded or shutdown.is_set():
                            break
                        if not await _wait_for_pause_to_clear(project_root, shutdown):
                            dlog.info(
                                "Pause marker still held — leaving the reindex "
                                "to the next change"
                            )
                            break
                    else:
                        dlog.warning(
                            "Background reindex superseded %d times — giving up "
                            "until the next change",
                            _SUPERSEDED_RETRIES,
                        )

    finally:
        await _cleanup_daemon(index_proc, server, sock_path, pid_file, lock_fd, shutdown)


# ── Internal helpers ─────────────────────────────────────────────────────────


async def _setup_unix_socket(db_dir: Path, on_ping) -> tuple[asyncio.Server, Path]:
    """Create and bind the Unix domain socket for daemon pings.

    *on_ping* is called (no arguments) each time a valid ping arrives.
    Returns the server and socket path.
    """
    sock_path = _socket_path(db_dir)
    sock_path.unlink(missing_ok=True)

    async def _handle_ping(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            data = await asyncio.wait_for(reader.read(1024), timeout=2.0)
            if b"ping" in data:
                on_ping()
        except (TimeoutError, OSError):
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except OSError:
                pass

    try:
        server = await asyncio.start_unix_server(_handle_ping, path=str(sock_path))
    except OSError:
        log.error("Cannot bind socket %s — daemon already running?", sock_path)
        raise

    os.chmod(sock_path, 0o600)
    return server, sock_path


def _setup_signal_handlers(shutdown: asyncio.Event) -> None:
    """Register SIGTERM / SIGINT handlers that set *shutdown*."""
    loop = asyncio.get_running_loop()

    def _handle_signal() -> None:
        shutdown.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            pass


async def _cleanup_daemon(
    index_proc: asyncio.subprocess.Process | None,
    server: asyncio.Server,
    sock_path: Path,
    pid_file: Path,
    lock_fd: int,
    shutdown: asyncio.Event,
) -> None:
    """Gracefully shut down the daemon: terminate subprocess, close server, clean up files."""
    log.info("Daemon shutting down")
    if index_proc is not None and index_proc.returncode is None:
        log.info("Terminating index subprocess (pid %d)", index_proc.pid)
        index_proc.terminate()
        try:
            await asyncio.wait_for(index_proc.wait(), timeout=10)
        except TimeoutError:
            log.warning("Index subprocess did not exit, killing")
            index_proc.kill()
            await index_proc.wait()
    shutdown.set()
    server.close()
    await server.wait_closed()
    _cleanup_files(sock_path, pid_file)
    fcntl.flock(lock_fd, fcntl.LOCK_UN)
    os.close(lock_fd)


def _is_source_file(path_str: str, build_patterns: list[str] | None = None) -> bool:
    """Return True when *path_str* is a C/C++ source file outside excluded dirs.

    Uses hardcoded ``_EXCLUDE_RX`` as baseline and supplements with
    *build_patterns* from the project manifest when available (derived
    from SDK detection and ``[index] exclude_paths`` config).
    """
    p = Path(path_str)
    if p.suffix.lower() not in _SOURCE_EXTS_WATCH:
        return False
    if _EXCLUDE_RX.search(path_str):
        return False
    if build_patterns and any(pat in path_str for pat in build_patterns):
        return False
    return True


def _load_build_patterns(db_dir: Path) -> list[str]:
    """Load ``build_dir_patterns`` from manifest.json, returning [] on any error.

    Reads the patterns without parsing the whole manifest — the file runs to
    tens of megabytes and this needs one short list from it.  The daemon calls
    this once at startup, so the cost was one-off rather than per query, but
    there is no reason to pay it.
    """
    try:
        from ..indexer.manifest import load_build_dir_patterns_any
        return load_build_dir_patterns_any(db_dir)
    except (OSError, ImportError, ValueError, KeyError):
        return []


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
    from .shared.context import _db_path, _quick_open_readonly
    from .shared.stale import _count_modified_files, check_structural_staleness

    db_path = _db_path(project_root)
    if not db_path.exists():
        return False, []

    # Read-only quick open: a full open_db would run ensure_schema's
    # unconditional executescript — a write transaction on every check
    # interval — causing lock contention with the executor.  The daemon
    # only reads here.
    try:
        conn = _quick_open_readonly(db_path)
    except sqlite3.Error:
        log.warning("Database unreadable — skipping staleness check")
        return False, []
    reasons: list[str] = []
    try:
        from ..indexer.db import get_active_config

        project_id = derive_project_id(project_root)
        cfg = get_active_config(conn, project_id)
        if not cfg:
            return False, []

        config_hash = cfg["config_hash"]

        # 1-3. Structural checks (shared with background._fast_staleness_check)
        reasons.extend(check_structural_staleness(conn, config_hash, dict(cfg), project_root))

        # 4. Modified source files — files changed before daemon started.
        #    watchfiles only detects NEW events, so without this check
        #    already-stale files would never be reindexed.
        modified = _count_modified_files(conn, config_hash, project_root, use_cache=True)
        if modified > 0:
            reasons.append(f"{modified} modified files")
    finally:
        conn.close()

    return len(reasons) > 0, reasons


# ── Index subprocess ─────────────────────────────────────────────────────────


async def _run_index_async(
    project_root: Path, db_dir: Path, *, force_refs: bool = False,
) -> asyncio.subprocess.Process:
    """Spawn ``fw-context index --background``, write stdout to ``reindex.log``.

    Writes the subprocess PID to ``reindex.pid`` so ``_is_bg_reindex_running``
    can reliably detect an active index run (without false positives from
    the daemon's ``watcher.lock``).

    Returns the asyncio Process handle — caller must wait or terminate.
    """
    log_file = db_dir / "reindex.log"
    try:
        fd = os.open(log_file, os.O_NOFOLLOW | os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        log_fh = os.fdopen(fd, "w", closefd=True)
    except OSError:
        log_fh = _DEVNULL  # type: ignore[assignment]

    env: dict[str, str] = {
        **os.environ,
        "PYTHONUNBUFFERED": "1",
    }
    if force_refs:
        env["FW_CONTEXT_FORCE_REFINDEX"] = "1"

    log.info("Starting background index for %s (force_refs=%s)", project_root, force_refs)

    # Write reindex.pid BEFORE spawning so a concurrent daemon
    # restart sees the marker and doesn't spawn a duplicate.
    rp = db_dir / "reindex.pid"
    old_pid = PidFile.read_pid(rp)
    if old_pid is not None and PidFile._pid_exists(old_pid):
        raise RuntimeError(
            f"Another index process (pid {old_pid}) is already running for {project_root}"
        )
    # Clean up any stale file before writing ours.
    rp.unlink(missing_ok=True)
    pf = open(rp, "w", encoding="utf-8")
    pf.write(str(os.getpid()))
    pf.flush()

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", "-m", "fw_context_mcp.cli", "index", "--background",
            cwd=str(project_root),
            stdout=log_fh,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        # Update PID file with the real subprocess PID.
        pf.seek(0)
        pf.truncate()
        pf.write(str(proc.pid))
        pf.close()
        if log_fh is not _DEVNULL:
            log_fh.close()
    except (ValueError, TypeError, RuntimeError, AttributeError, FileExistsError, OSError):
        log.exception("Failed to spawn index subprocess")
        pf.close()
        rp.unlink(missing_ok=True)
        if log_fh is not _DEVNULL:
            log_fh.close()
        raise
    return proc


async def _wait_for_pause_to_clear(
    project_root: Path, shutdown: asyncio.Event
) -> bool:
    """Wait for the manual operation to release the index.

    Returns True when the marker cleared and a retry makes sense, False on
    shutdown or when the operation outlasts the wait — a manual reindex of a
    large project can run for an hour, and holding the daemon in a poll loop
    that whole time buys nothing over letting the next file change trigger it.
    """
    from .background import _check_bg_pause as _bg_paused

    deadline = time.monotonic() + _PAUSE_WAIT_S
    while time.monotonic() < deadline:
        if shutdown.is_set():
            return False
        if not _bg_paused(project_root):
            return True
        await asyncio.sleep(2.0)
    return False


async def _wait_index(
    proc: asyncio.subprocess.Process,
    shutdown: asyncio.Event,
    *,
    db_dir: Path,
) -> bool:
    """Wait for *proc* to finish, polling *shutdown* every 500 ms.

    When *shutdown* is set, terminates the subprocess gracefully
    (SIGTERM → 10 s → SIGKILL).  Removes ``reindex.pid`` on exit
    so ``_is_bg_reindex_running`` doesn't report a stale PID.

    Returns True when the run was superseded by a manual operation and its
    work still has to be done.
    """
    try:
        while proc.returncode is None:
            if shutdown.is_set():
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=10)
                except TimeoutError:
                    log.warning("Index subprocess did not exit after SIGTERM, killing")
                    proc.kill()
                    await proc.wait()
                return False
            await asyncio.sleep(0.5)
    finally:
        PidFile(db_dir / "reindex.pid", pid=proc.pid).unlink_if_ours()

    if proc.returncode == EXIT_SUPERSEDED:
        # The run stopped because a manual operation took the index over.
        # The changes that triggered it are still unindexed, so this must not
        # be treated as done — going back to waiting for the NEXT change
        # would leave them out until something else happens to be edited.
        log.info("Background index superseded by a manual operation — will retry")
        return True
    if proc.returncode == 0:
        log.info("Background index completed")
        # WAL files grow unboundedly during long reindex runs because
        # the index subprocess writes continuously without checkpointing.
        # PRAGMA optimize triggers a checkpoint + ANALYZE to shrink the
        # WAL and refresh query-planner statistics.  This runs AFTER the
        # index completes — no lock contention with the subprocess.
        _optimize_db(db_dir)
    else:
        log.warning("Background index exited with %d", proc.returncode)
    return False


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
    """Run PRAGMA optimize to shrink the WAL file and defragment indexes.

    Deliberately uses a full ``open_db`` (whitelisted write path): this
    is a rare maintenance operation outside the request path, and WAL
    allows concurrent readers while it runs.  Not a bug — do not
    "convert" this to the read-only quick open; writes need a writable
    connection.
    """
    db_path = db_dir / "index.db"
    if not db_path.exists():
        return
    conn = _open_db(db_path)
    if conn is None:
        return
    try:
        conn.execute("PRAGMA optimize")
    except (sqlite3.Error, ValueError, TypeError, RuntimeError, AttributeError):
        log.debug("PRAGMA optimize failed", exc_info=True)
    finally:
        conn.close()


# ── Sentinel for log_fh default ──────────────────────────────────────────────
# ``object()`` suffices — all call sites use ``is`` comparison.
_DEVNULL = object()


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
    asyncio.run(daemon_main(target))
