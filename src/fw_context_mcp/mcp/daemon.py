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

WHY the file watcher is a separate task: an index run takes minutes on
a large project.  A loop that watches and indexes in turn must leave its
``async for`` to start the run, and that closes the inotify subscription.
``watchfiles`` keeps no events while the subscription is closed, thus each
edit made during the run is lost.  A watcher task that lives as long as
the daemon keeps those edits as a flag for the reindex loop.

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
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

from ..config import derive_project_id
from ..config import load as load_config
from ..exit_codes import EXIT_ALREADY_RUNNING, EXIT_SUPERSEDED
from ..utils import HEADER_EXTENSIONS, TU_EXTENSIONS, remove_stale_tmp
from .background import PENDING_MARKER
from .shared.pid_file import PidFile

log = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────
DAEMON_SOCK_NAME = "daemon.sock"
PING_INTERVAL = 15   # How often each MCP server pings (seconds)
PING_TIMEOUT = 60    # Exit after this long without any ping (seconds)
_DEBOUNCE_S = 2.0    # Collect changes before spawning index
_WATCH_TIMEOUT = 5   # watchfiles yield_on_timeout interval (seconds)
# A run that a manual operation stopped (EXIT_SUPERSEDED) is work that is
# still to do, thus the loop runs it again when the operation releases the
# index.  The limit is for an operation that stops each new run, for example
# an LLM that calls reindex_file after each edit: each retry starts an index
# subprocess.  The limit counts the stopped runs in a row, the first run
# included, thus 3 runs give 2 retries.  After the limit the next change
# brings the work back.
_SUPERSEDED_RUNS_MAX = 3
_HELD_POLL_S = 2.0  # How often the loop looks if the other process released the index

# ── Watched file extensions ──────────────────────────────────────────────────
# What the watcher reacts to: a translation unit or a header, by the
# compiler rules.  The hand-written set this replaced was missing `.c++`,
# `.tcc` and every uppercase form — measured against the real manifests,
# `.tcc` alone appears 3 to 18 times per project.
#
# Extension-less headers stay out: the compiler has no rule for them, so
# they belong to the derived set of the project rather than here.
_SOURCE_EXTS_WATCH: frozenset[str] = TU_EXTENSIONS | HEADER_EXTENSIONS
# `.fw-context/` holds the output of the automatic build (generated
# headers).  A file that fw-context writes itself must never start an index
# run: a build that fails would then start the next build without end.  The
# manifest patterns hold `.fw-context/` too, but an index older than v0.29.0
# has no such pattern, and a daemon without a manifest has none at all.
_EXCLUDE_RX = re.compile(r"(/\.git/|/\.pio/|/build/|/\.fw-context/|/__pycache__/|/node_modules/)")


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


# ── Pending work ─────────────────────────────────────────────────────────────


class _PendingWork(asyncio.Event):
    """The flag of the work that the daemon has, and the marker that shows it to the other processes.

    The marker ``reindex.pending`` holds the PID of the daemon
    (``background.read_reindex_state`` reads it).  It means "the daemon has
    work that is not done".  :meth:`set` writes it.  Three places remove
    it:

    - The reindex loop, at the end of a turn that leaves no work
      (:meth:`hide_if_done`), also a turn that gives the work up.
    - ``_cleanup_daemon``, when the daemon stops.
    - ``daemon_main``, when the daemon starts: a daemon that a SIGKILL
      stopped leaves its marker, and its PID can come back.

    WHY the marker stays while a run works: the run owns ``reindex.pid``
    only from the moment that it holds the index lock until it releases
    it.  The start of the subprocess comes before, and the outcome of the
    run, which can be "do it again", comes after.  A marker that went away
    at the start of the run left these times without a state, and the
    answers of that time said "run fw-context index" for work that came.
    While the run owns ``reindex.pid``, the reader gives "reindexing"
    first.

    WHY :meth:`set` writes the marker, and not the loop: the loop takes the
    flag only on its next turn.  A change during a run was invisible until
    that turn, measured at 1.4 s.
    """

    def __init__(self, db_dir: Path) -> None:
        super().__init__()
        self.marker = PidFile(db_dir / PENDING_MARKER)

    def set(self) -> None:
        super().set()
        self.show()

    def show(self) -> None:
        """Write the marker.  A failed write costs the state only, thus the work still runs."""
        try:
            self.marker.write()
        except OSError as exc:
            log.warning("Cannot write %s: %s", self.marker.path, exc)

    def hide_if_done(self) -> None:
        """Remove the marker when no change came after the last clear of the flag."""
        if not self.is_set():
            self.marker.unlink_if_ours()


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

    # Only the daemon that holds the lock writes the marker of pending work.
    # A marker that is there now is from a daemon that a SIGKILL stopped,
    # and a new process with its old PID makes it read as work that comes.
    # The startup check of the reindex loop finds that work again.
    (db_dir / PENDING_MARKER).unlink(missing_ok=True)
    # A kill between the write and the rename of a marker leaves its
    # temporary file (`PidFile.write`).  Only the file of a dead writer goes.
    remove_stale_tmp(db_dir)

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
    # ── Structured logger — attach project context for log aggregation ──
    dlog = logging.LoggerAdapter(
        log,
        {"project_root": str(project_root), "pid": os.getpid()},
    )

    dlog.info("Daemon started  socket=%s", sock_path)

    watch_task: asyncio.Task | None = None
    head_task: asyncio.Task | None = None
    try:
        # Load the build-dir exclusion patterns from the manifest (derived
        # from SDK detection and the ``[index] exclude_paths`` config).  They
        # supplement the hardcoded _EXCLUDE_RX during change filtering.  This
        # runs before the initial index, because the watcher starts first and
        # reads this list.
        build_patterns = await asyncio.get_running_loop().run_in_executor(
            None, _load_build_patterns, db_dir
        )

        # Start the watcher BEFORE the initial index.  The initial index runs
        # as long as any other index run, thus a watcher that starts after it
        # loses each edit that a user makes while it runs.
        pending = _PendingWork(db_dir)
        watch_task = asyncio.create_task(
            _watch_changes(project_root, build_patterns, pending, shutdown, dlog)
        )
        # A checkout changes HEAD, and HEAD is outside of the files that the
        # watcher above reacts to (and outside of the tree for a worktree).
        head_task = asyncio.create_task(_watch_head(project_root, pending, shutdown, dlog))

        def _set_index_proc(proc: asyncio.subprocess.Process | None) -> None:
            """Publish the running subprocess, thus the cleanup can stop it."""
            nonlocal index_proc
            index_proc = proc

        def _ping_expired() -> bool:
            """Return True when no MCP client sent a ping for PING_TIMEOUT."""
            elapsed = time.monotonic() - last_ping_time
            if elapsed <= PING_TIMEOUT:
                return False
            dlog.info(
                "No ping for %.0f s — all clients disconnected, exiting", elapsed
            )
            return True

        await _reindex_loop(
            project_root,
            db_dir,
            pending=pending,
            shutdown=shutdown,
            dlog=dlog,
            ping_expired=_ping_expired,
            on_index_proc=_set_index_proc,
            build_patterns=build_patterns,
            startup_check=_staleness_check,
        )

    finally:
        shutdown.set()
        if watch_task is not None:
            await _stop_watcher(watch_task)
        if head_task is not None:
            await _stop_watcher(head_task)
        await _cleanup_daemon(index_proc, server, sock_path, pid_file, lock_fd, shutdown)


# ── Internal helpers ─────────────────────────────────────────────────────────


# ── Change watcher ───────────────────────────────────────────────────────────


class _WatchErrors:
    """Log the first failure of a watch as a warning, and the same failure again at debug level.

    WHY: a watch that cannot start fails again on each retry, every 5 s.
    The limit of inotify instances of the user (128 on many systems) is
    such a cause, and the daemon has two watches.  A warning for each
    retry wrote 720 lines an hour, and the first line says all of it.
    A watch that works again (:meth:`clear`) makes the next failure a
    warning again.
    """

    def __init__(self, dlog: logging.LoggerAdapter, where: str) -> None:
        self._dlog = dlog
        self._where = where
        self._last: str | None = None

    def report(self, exc: BaseException) -> None:
        text = f"{type(exc).__name__}: {exc}"
        level = logging.DEBUG if text == self._last else logging.WARNING
        self._dlog.log(level, "watchfiles error on %s: %s", self._where, text)
        self._last = text

    def clear(self) -> None:
        self._last = None


async def _watch_changes(
    project_root: Path,
    build_patterns: list[str],
    pending: asyncio.Event,
    shutdown: asyncio.Event,
    dlog: logging.LoggerAdapter,
) -> None:
    """Set *pending* for each C/C++ change under *project_root*, until shutdown.

    WHY this is a task, and not an ``async for`` inside the reindex loop:
    an index run takes minutes on a large project.  A loop that watches and
    indexes in turn must leave its ``async for`` to start the run, and that
    closes the inotify subscription.  ``watchfiles`` keeps no events while
    the subscription is closed, thus each edit made during the run is lost,
    and the index stays stale until an unrelated edit starts the next run.
    This task lives as long as the daemon, thus it keeps those edits as a
    flag that the reindex loop reads when the current run stops.

    ``awatch`` gets *shutdown* as its ``stop_event``, thus it returns as soon
    as the daemon stops, and does not wait for the Rust timeout.

    *build_patterns* is read for each change, thus the caller can refresh the
    list in place after an index run writes a new manifest.
    """
    from watchfiles import awatch

    errors = _WatchErrors(dlog, str(project_root))
    while not shutdown.is_set():
        try:
            async for changes in awatch(
                project_root,
                debounce=int(_DEBOUNCE_S * 1000),
                recursive=True,
                rust_timeout=_WATCH_TIMEOUT * 1000,
                yield_on_timeout=True,
                stop_event=shutdown,
            ):
                if shutdown.is_set():
                    return
                errors.clear()
                for _, changed_path_str in changes:
                    if _is_source_file(changed_path_str, build_patterns):
                        pending.set()
                        break
        except (OSError, RuntimeError) as exc:
            errors.report(exc)
            await asyncio.sleep(5)


async def _watch_head(
    project_root: Path,
    pending: asyncio.Event,
    shutdown: asyncio.Event,
    dlog: logging.LoggerAdapter,
) -> None:
    """Set *pending* for each change of the file HEAD of *project_root*, until shutdown.

    A checkout changes HEAD, and the run that follows builds when the
    checkout of a build differs (``cli._index._plan_auto_build``).  Without
    this task only the source files that a checkout rewrites started a
    run, and a checkout that changed only a configuration file started
    nothing.

    WHY the directory of HEAD and not the file: git writes ``HEAD.lock``
    and renames it to ``HEAD``, thus a watch of the inode of the old file
    sees nothing after the first checkout.  The watch is not recursive: the
    directory of HEAD is the git directory, with all of its objects.

    WHY ``watch_filter=None``: the default filter of watchfiles ignores each
    path in a ``.git`` directory, and HEAD is always in one.  With that
    filter this task saw no checkout.

    WHY the text of HEAD decides, and not the event: git also rewrites
    HEAD with the same text, for a checkout of the branch that is out
    already.  Such an event started a run for nothing.  The text is what
    the index compares too (``git_context.read_head``), thus a run comes
    exactly when the checkout of the index can differ.

    WHY the task reads the text at each output of the watch, also at the
    empty output after each timeout, and not only for an event of HEAD:
    the watch starts in a thread, and a checkout before the watch exists
    has no event.  A read at each output finds it within one timeout.  The
    read is of one small file.  For the same reason no event is compared
    with the path of HEAD: ``.git/logs/HEAD`` has the same name, and
    macOS gives ``/private/var`` for ``/var``.

    A tree without git gets a watch when ``git init`` comes later:
    ``head_path`` looks for the git directory again after its recheck time.
    A git directory that goes away, or that ``rm -rf .git && git init``
    replaces, stops the watch: inotify watches the old directory, which
    gets no more events.  The task then finds HEAD again.

    On a file system that gives a directory another inode at each stat
    (some FUSE file systems), the watch starts again at each output.  The
    checkout still reaches the daemon.
    """
    from watchfiles import Change, awatch

    from ..indexer.git_context import _NO_GIT_RECHECK_S, head_label, head_path, read_head

    loop = asyncio.get_running_loop()
    errors = _WatchErrors(dlog, f"the git directory of {project_root}")
    # The checkout that this task saw last.  The startup check compares the
    # index with the checkout of the start, thus the task starts from it.
    seen = await loop.run_in_executor(None, read_head, project_root)

    async def _follow() -> None:
        nonlocal seen
        now = await loop.run_in_executor(None, read_head, project_root)
        if now != seen:
            dlog.info(
                "Checkout changed from %s to %s — the index follows it",
                head_label(seen) or "no git", head_label(now) or "no git",
            )
            seen = now
            pending.set()

    while not shutdown.is_set():
        head = await loop.run_in_executor(None, head_path, project_root)
        git_dir = head.parent.resolve() if head is not None else None
        identity = _dir_identity(git_dir) if git_dir is not None else None
        if head is None or git_dir is None or identity is None:
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=_NO_GIT_RECHECK_S)
            except TimeoutError:
                pass
            continue
        try:
            async for changes in awatch(
                git_dir,
                watch_filter=None,
                debounce=int(_DEBOUNCE_S * 1000),
                recursive=False,
                rust_timeout=_WATCH_TIMEOUT * 1000,
                yield_on_timeout=True,
                stop_event=shutdown,
            ):
                if shutdown.is_set():
                    return
                errors.clear()
                await _follow()
                # The deletion of the directory itself is necessary too: a
                # new directory can get the inode of the old one.
                if any(
                    change == Change.deleted and Path(changed) == git_dir for change, changed in changes
                ) or _dir_identity(git_dir) != identity:
                    dlog.info("The git directory %s went away — looking for HEAD again", git_dir)
                    break
        except (OSError, RuntimeError) as exc:
            errors.report(exc)
            await asyncio.sleep(5)


def _dir_identity(path: Path) -> tuple[int, int] | None:
    """Return the device and the inode of the directory *path*, or None when it is not there.

    A directory of the same name that replaces it has another inode.
    """
    try:
        info = os.stat(path)
    except OSError:
        return None
    return (info.st_dev, info.st_ino)


async def _stop_watcher(task: asyncio.Task) -> None:
    """Stop the watcher *task*, and do not let its error stop the shutdown.

    The task stops on its own when the daemon sets the shutdown event.  The
    cancel is the fallback for a task that is between two ``awatch`` calls at
    that moment.
    """
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except (OSError, RuntimeError):
        log.exception("Watcher task stopped with an error")


# ── Reindex loop ─────────────────────────────────────────────────────────────


async def _reindex_loop(
    project_root: Path,
    db_dir: Path,
    *,
    pending: _PendingWork,
    shutdown: asyncio.Event,
    dlog: logging.LoggerAdapter,
    ping_expired: Callable[[], bool],
    on_index_proc: Callable[[asyncio.subprocess.Process | None], None],
    build_patterns: list[str] | None = None,
    startup_check: Callable[[Path], tuple[bool, list[str]]] | None = None,
) -> None:
    """Run one index for each pending change, until shutdown or a ping timeout.

    WHY the loop clears *pending* BEFORE it starts the index subprocess: a
    change that arrives while the subprocess runs sets the flag again, thus
    the next turn of the loop indexes that change.  A clear after the run
    drops such a change, and the index then stays stale until an unrelated
    edit starts the next run.  The cost of this order is one more run when
    the subprocess already saw the change.  A run that is not necessary is
    cheaper than an index that is not correct.

    *startup_check* finds the work of the time when no daemon watched:
    watchfiles reports new events only.  It runs here, and not before the
    loop, because it reads the index: a manual run that holds the index
    writes it, and a check of a half-written index finds work that the
    manual run does.  ``refs missing`` from such a check started a full
    parse of the project after the manual run.
    """
    loop = asyncio.get_running_loop()
    force_refs = False
    if startup_check is not None:
        if not await _index_is_free(db_dir, shutdown, ping_expired, dlog):
            return
        # Synchronous, thus in an executor: the socket server answers the
        # pings in this time.
        needs, reasons = await loop.run_in_executor(None, startup_check, project_root)
        if needs:
            dlog.info("Initial index needed (%s)", ", ".join(reasons))
            # A reason of a build carries its variant/image in front.
            force_refs = any(reason.endswith("refs missing") for reason in reasons)
            pending.set()

    # Runs in a row that a manual operation stopped.
    superseded_runs = 0

    while not shutdown.is_set():
        if not pending.is_set():
            try:
                await asyncio.wait_for(pending.wait(), timeout=_WATCH_TIMEOUT)
            except TimeoutError:
                pass
        if ping_expired():
            return
        if shutdown.is_set() or not pending.is_set():
            continue

        # Collect the rest of the burst.  A git checkout and a build both
        # touch many files, and one index run covers all of them.
        await asyncio.sleep(_DEBOUNCE_S)

        # The flag stays while another process holds the index: a
        # single-file reindex does not cover the other changes.
        if not await _index_is_free(db_dir, shutdown, ping_expired, dlog):
            return

        pending.clear()
        try:
            proc = await _run_index_async(project_root, db_dir, force_refs=force_refs)
        except OSError as exc:
            # The daemon stays: it watches on, and the next change tries
            # again.  Without a marker the answers tell the LLM to run
            # `fw-context index`, and that run shows the cause.
            dlog.error("Cannot start the background reindex: %s — the work waits for the next change", exc)
            superseded_runs = 0
            pending.hide_if_done()
            continue
        on_index_proc(proc)
        returncode = await _wait_index(proc, shutdown, db_dir=db_dir)
        on_index_proc(None)
        if returncode is None:
            return  # shutdown

        if returncode == EXIT_ALREADY_RUNNING:
            # Another run took the index lock after the check above.  Not a
            # retry that counts: the next turn waits for that run.
            dlog.info("Background index skipped — another index run holds the index; it runs after that run")
            pending.set()
            continue
        if returncode == EXIT_SUPERSEDED:
            # A manual operation took the index over.  The changes of this
            # run are still not in the index, and the next turn waits for
            # the operation.
            superseded_runs += 1
            if superseded_runs < _SUPERSEDED_RUNS_MAX:
                dlog.info("Background index superseded by a manual operation — it runs again after it")
                pending.set()
                continue
            dlog.warning(
                "Background index superseded %d times in a row — giving up until the next change",
                _SUPERSEDED_RUNS_MAX,
            )
        elif returncode == 0:
            dlog.info("Background index completed")
        else:
            dlog.warning("Background index exited with %d", returncode)
        superseded_runs = 0
        force_refs = False
        # The work of this turn is done or given up.  A change that came
        # during the run keeps the marker that its set() wrote.
        pending.hide_if_done()

        if returncode == 0:
            # After the marker: the run is done, and this is maintenance.
            await loop.run_in_executor(None, _optimize_db, db_dir)
        # A completed run can write a new manifest.  Refresh the exclusions in
        # place, because the watcher task holds the same list object.
        if build_patterns is not None and not shutdown.is_set():
            build_patterns[:] = await loop.run_in_executor(
                None, _load_build_patterns, db_dir
            )


def _index_held(db_dir: Path) -> bool:
    """Return True while another process holds the index.

    ``reindex.pause``: ``reindex_file`` and ``reset_index`` of an MCP
    server write it, and each ``fw-context index`` run.  ``reindex.pid``:
    each ``fw-context index`` run writes it when it holds the index lock
    (``cli._index._claim_index``), also a run that holds no pause marker
    any more because a ``reindex_file`` replaced it and then removed it.
    Between two turns no run of this daemon owns either file: the run
    removes them when it ends, and ``_wait_index`` removes the files of a
    run that a kill stopped.
    """
    return PidFile.is_active(db_dir / "reindex.pause") or PidFile.is_active(db_dir / "reindex.pid")


async def _index_is_free(
    db_dir: Path,
    shutdown: asyncio.Event,
    ping_expired: Callable[[], bool],
    dlog: logging.LoggerAdapter,
) -> bool:
    """Wait while another process holds the index.

    Returns True when the index is free, and False on shutdown or a ping
    timeout.  WHY no time limit: a manual index of a large project runs
    for hours, and work that the loop gives up after a limit was not done
    by anybody.  The loop must still stop with the daemon, thus this
    function looks at the shutdown and the pings.
    """
    if not _index_held(db_dir):
        return True
    dlog.info("Another process holds the index — the background reindex waits")
    while _index_held(db_dir):
        if shutdown.is_set() or ping_expired():
            return False
        try:
            await asyncio.wait_for(shutdown.wait(), timeout=_HELD_POLL_S)
        except TimeoutError:
            pass
    return not shutdown.is_set()


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
    # Work that this daemon did not start goes with it.  The startup check
    # of the next daemon finds it again.
    PidFile(sock_path.parent / PENDING_MARKER).unlink_if_ours()
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
    if p.suffix not in _SOURCE_EXTS_WATCH:
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
    this at startup and after each index run that can write a new manifest,
    thus the cost is per run rather than per query.
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

    Checks structural staleness (compile_commands.json, schema, row format,
    refs) AND file-level staleness (the stored hash against the file on
    disk, or the stored time for a row without a hash,
    ``_count_modified_files``).
    Without the file-level check the daemon would never detect files that
    were modified BEFORE it started — watchfiles only detects NEW changes.

    The row-format check is the reason a daemon can ask for a reindex over
    an index whose files did not change: the columns are the same, but the
    text in them carries an older meaning.  See ``CURRENT_ROW_FORMAT``.

    Every build of the project is checked, each ``(variant, image)``, and
    not only the newest one: an edit to a file that only another image
    compiles made that build stale, and the check of the newest build did
    not see it.  A reason of a build carries its ``variant/image``.

    A checkout that happened while no daemon ran is a reason too: the
    watcher sees a change of HEAD only while it runs
    (``git_context.head_moved``).  A new source file that the run builds on
    its own (``_unindexed_sources``) is also a reason: the watcher sees a
    new file only while it runs, and without this check no run came for a
    file that the user added before the daemon started.
    """
    from ..indexer.git_context import read_head
    from .shared.context import _db_path, _quick_open_readonly
    from .shared.stale import _count_modified_files, check_structural_staleness, checkout_reason

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
        from .shared.stale import build_label, latest_builds

        project_id = derive_project_id(project_root)
        live_head = read_head(project_root)
        for cfg in latest_builds(conn, project_id):
            config_hash = cfg["config_hash"]
            label = build_label(cfg)
            prefix = f"{label}: " if label else ""

            # 1-4. Structural checks (shared with background._fast_staleness_check):
            #      compile_commands.json, schema version, row format, refs.
            found = check_structural_staleness(conn, config_hash, dict(cfg), project_root)

            # 5. Modified source files — files changed before daemon started.
            #    watchfiles only detects NEW events, so without this check
            #    already-stale files would never be reindexed.
            modified = _count_modified_files(conn, config_hash, project_root, use_cache=True)
            if modified > 0:
                found.append(f"{modified} modified files")

            # 6. Another checkout than the build: the run then builds
            #    (`cli._index._plan_auto_build`).
            moved = checkout_reason(cfg, live_head, project_root)
            if moved:
                found.append(moved)
            reasons.extend(prefix + reason for reason in found)

        # 7. Source files that compile_commands.json does not cover, which
        #    came while no daemon ran: the watcher sees a new file only while
        #    it runs.  Only where the run builds them on its own
        #    (`cli._index._plan_auto_build`): for another backend a run
        #    changes nothing, and the status tools name the command.
        unindexed = _unindexed_sources(conn, project_id, project_root)
        if unindexed:
            reasons.append(f"{len(unindexed)} source file(s) not in compile_commands.json")
    finally:
        conn.close()

    return len(reasons) > 0, reasons


def _unindexed_sources(conn, project_id: str, project_root: Path) -> list[str]:
    """Return the new source files of the project that an index run builds on its own.

    The function reads the build that the status tools check
    (`variants.checked_build`), thus the daemon and `get_project_status` see
    the same files.  The list is empty when the backend cannot build in the
    background (`autobuild.project_state`): a run then changes nothing.
    """
    from ..config import load as load_config
    from ..indexer.autobuild import AutobuildState
    from ..indexer.autobuild import project_state as autobuild_state
    from .shared.stale import find_unindexed_sources
    from .shared.variants import checked_build

    cfg = load_config(project_root)
    checked = checked_build(conn, project_id, cfg, project_root)
    if checked is None or not checked["compile_commands_path"]:
        return []
    found = find_unindexed_sources(
        conn, checked["config_hash"], project_root, Path(checked["compile_commands_path"]),
    )
    if not found or autobuild_state(project_root, cfg) is not AutobuildState.WILL_BUILD:
        return []
    return found


# ── Index subprocess ─────────────────────────────────────────────────────────


async def _run_index_async(
    project_root: Path, db_dir: Path, *, force_refs: bool = False,
) -> asyncio.subprocess.Process:
    """Spawn ``fw-context index --background``, write stdout to ``reindex.log``.

    No ``--build`` here, and the daemon decides nothing about building.  The
    index run turns the build on itself where it is needed and allowed —
    ``cli._index._plan_auto_build`` — because only that path also sets the
    isolated output directory that makes a background build legal.  A
    ``--build`` from here failed with "--build and --background are mutually
    exclusive" in under a second.

    WHY the daemon writes no ``reindex.pid``: the run writes it when it
    holds the index lock (``cli._index._claim_index``), and removes it when
    it releases the lock.  A file that the daemon wrote before the spawn
    named a run that could lose the lock to a manual run, and the daemon
    then overwrote the file of that manual run.  Until the run holds the
    lock, the marker of the pending work shows it.

    Returns the asyncio Process handle — caller must wait or terminate.
    Raises :class:`OSError` when the subprocess cannot start.
    """
    log_file = db_dir / "reindex.log"
    log_target: int | TextIO
    try:
        fd = os.open(log_file, os.O_NOFOLLOW | os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        log_target = os.fdopen(fd, "w", closefd=True)
    except OSError as exc:
        # The run does its work also without a log.
        log.warning("Cannot open %s: %s — the background index runs without a log", log_file, exc)
        log_target = asyncio.subprocess.DEVNULL

    env: dict[str, str] = {
        **os.environ,
        "PYTHONUNBUFFERED": "1",
    }
    if force_refs:
        env["FW_CONTEXT_FORCE_REFINDEX"] = "1"

    log.info("Starting background index for %s (force_refs=%s)", project_root, force_refs)
    try:
        return await asyncio.create_subprocess_exec(
            sys.executable, "-u", "-m", "fw_context_mcp.cli", "index", "--background",
            cwd=str(project_root),
            stdout=log_target,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
    finally:
        # The subprocess has its own copy of the file descriptor.
        if not isinstance(log_target, int):
            log_target.close()


async def _wait_index(
    proc: asyncio.subprocess.Process,
    shutdown: asyncio.Event,
    *,
    db_dir: Path,
) -> int | None:
    """Wait for *proc* to finish, polling *shutdown* every 500 ms.

    Returns the exit code of the run, or None when *shutdown* stopped it.
    On shutdown the run gets SIGTERM, and SIGKILL after 10 s.

    Removes ``reindex.pid`` and ``reindex.pause`` when they still name
    *proc*: a run that a SIGKILL stopped cannot remove them, and the PID
    can come back as another process.  A ``reindex.pause`` of that PID
    then makes ``_index_is_free`` wait, with no time limit, for a process
    that has no relation to the index.  The files of another run stay.
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
                return None
            await asyncio.sleep(0.5)
    finally:
        PidFile(db_dir / "reindex.pid", pid=proc.pid).unlink_if_ours()
        PidFile(db_dir / "reindex.pause", pid=proc.pid).unlink_if_ours()
    return proc.returncode


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
