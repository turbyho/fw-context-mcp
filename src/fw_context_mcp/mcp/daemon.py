"""Per-project background daemon: watches source files, runs ``fw-context index`` on
changes, exits when all MCP clients disconnect.

One daemon per project, shared by all MCP server instances over that project.
Communication is via a TCP socket on the loopback address 127.0.0.1.  The
daemon writes its port and a token into ``<db_dir>/<project_id>/daemon.port``.

Lifecycle
---------
1. First MCP server spawns the daemon (via ``_ensure_daemon_running``).
2. Each MCP server pings the daemon every 15 s over the socket, and the
   daemon answers with its token.
3. If no ping arrives for 60 s, the daemon exits (all clients gone).
4. SIGTERM / SIGINT → clean shutdown, signal forwarded to index subprocess.

Isolation
---------
Each project has its own port file at ``<db_dir>/<project_id>/daemon.port``.
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

WHY a TCP socket on 127.0.0.1 instead of a Unix domain socket (decision of
the user, 2026-11-08):
- One path of code on Linux, macOS and Windows.  Python has no AF_UNIX on
  Windows, and asyncio has no ``start_unix_server`` there.
- No limit on the length of a path: ``sun_path`` holds 108 bytes on Linux
  and 104 on macOS, and an index directory in a long path stopped the
  daemon ("AF_UNIX path too long").
- No message broker process needed (ZeroMQ, Redis, etc.).
- The ping carries no data.  Another local process that connects can only
  keep the daemon alive longer: the cost is one idle process.

WHY a proof in the answer: a daemon that a SIGKILL stopped leaves its
port file, and the system can give its port to another program.  A
connect alone then read as a live daemon, and no new daemon started.  The
client sends ``ping <nonce>`` (a new random value), and the daemon answers
``pong <proof> <pid>``: the proof is the HMAC-SHA256 of ``<nonce> <pid>``
with the token of ``daemon.port`` as its key.  Only the daemon that wrote
the file knows the token, thus the proof shows that the daemon of this
project listens on the port, and the PID is its own: ``fw-context watch
restart`` stops that PID (``daemon_pid``).  WHY not the token itself in
the answer: each local process that sent ``ping`` got it.  A different
user then answered on the port after the end of the daemon, and gave a
PID of their choice to the restart.  A ``ping`` with no nonce keeps the
daemon alive and gets ``pong`` with no proof.

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
import errno
import hashlib
import hmac
import logging
import os
import re
import secrets
import signal
import socket
import sqlite3
import struct
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from .. import loopback
from ..config import derive_project_id
from ..config import load as load_config
from ..exit_codes import EXIT_ALREADY_RUNNING, EXIT_SUPERSEDED
from ..file_lock import ProbedLock
from ..indexer.db._locking import index_run_lock_held
from ..processes import STOP_TOKEN_ENV, install_stop_listener, is_valid_pid, new_stop_token, parse_pid, request_stop
from ..utils import HEADER_EXTENSIONS, O_NOFOLLOW, TU_EXTENSIONS, remove_file, remove_stale_tmp, write_text_atomic
from .background import PENDING_MARKER
from .shared.pid_file import PidFile

log = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────
DAEMON_PORT_NAME = "daemon.port"
# The Unix domain socket of a daemon before the change to TCP: the start of
# a daemon removes it.
_LEGACY_SOCK_NAME = "daemon.sock"
_LOOPBACK = "127.0.0.1"
# The nonce of a ping: 32 hex digits (secrets.token_hex(16)).
_NONCE_PATTERN = re.compile(r"[0-9a-f]{32}")

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
#: How long the daemon waits for its index run after the stop request,
#: before it kills the run.  The run stops at a unit boundary and releases
#: its locks.  ``fw-context watch restart`` gives the daemon more time than
#: this (``cli._watch._stop_wait_s``): a kill of the daemon in that wait
#: left the run, its markers and its PID file.
RUN_STOP_WAIT_S = 10.0

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


def _port_file(db_dir: Path) -> Path:
    """Return the file with the port and the token of the daemon of a project."""
    return db_dir / DAEMON_PORT_NAME


_TOKEN_PATTERN = re.compile(r"[0-9a-f]{32}")


def _read_port_file(port_file: Path) -> tuple[int, str] | None:
    """Return ``(port, token)`` from *port_file*, or None when it is missing or not valid.

    The token must be the 32 hex digits that the daemon writes: a damaged
    file with another text gives None, and not an error in the ping thread
    of the MCP server.
    """
    try:
        port_text, token = port_file.read_text(encoding="utf-8").split()
        port = int(port_text)
    except (OSError, ValueError):
        return None
    if not 0 < port < 65536 or not _TOKEN_PATTERN.fullmatch(token):
        return None
    return port, token


def ping_daemon(project_root: Path) -> bool:
    """Send a ping to the project daemon.

    Returns ``True`` when the daemon of the project answers with the
    proof of the token of ``daemon.port`` (:func:`daemon_pid`), and
    ``False`` when the file is missing, no process listens, or a
    different program has the port.
    """
    return daemon_pid(_get_db_dir(project_root)) is not None


@dataclass(frozen=True)
class DaemonAnswer:
    """The answer of the daemon of an index directory to a ping (:func:`daemon_pid`).

    *pid* is the PID that the daemon gives of itself, under the proof of
    its token (see the module docstring).
    """

    pid: int


def daemon_pid(db_dir: Path) -> DaemonAnswer | None:
    """Ask the daemon of *db_dir* for its PID; None when no daemon of this index directory answers.

    WHY the answer of the daemon and not ``watcher.pid`` or the argv of a
    process: ``watch restart`` sends a stop and a kill to that PID.
    ``watcher.pid`` can name a different process (a daemon that a kill
    ended left it, and the system gave its PID to a new process), and an
    argv names a project root, while clones with one project id share one
    index directory and one daemon.  The proof of the token of
    ``daemon.port`` (a file of this user, see :func:`_setup_ping_server`)
    covers the PID: only the daemon that holds the lock of *db_dir* wrote
    the token, and the PID is its ``os.getpid()``.  An answer with no
    valid proof or no valid PID is no answer of the daemon.
    """
    known = _read_port_file(_port_file(db_dir))
    if known is None:
        return None
    port, token = known
    nonce = secrets.token_hex(16)
    words = _exchange(port, f"ping {nonce}\n".encode("ascii"))
    if words is None or len(words) != 3 or words[0] != "pong":
        return None
    _, proof, pid_text = words
    if not hmac.compare_digest(proof, _proof(token, nonce, pid_text)):
        return None
    pid = parse_pid(pid_text)
    return None if pid is None else DaemonAnswer(pid=pid)


def _proof(token: str, nonce: str, pid_text: str) -> str:
    """The proof of a ping: the HMAC-SHA256 of ``<nonce> <pid>`` with *token* as the key (module docstring)."""
    message = f"{nonce} {pid_text}".encode("ascii")
    return hmac.new(token.encode("ascii"), message, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class LegacyListener:
    """The process that listens on ``daemon.sock`` of an index directory (:func:`legacy_listener`).

    *pid* and *uid* are what the kernel gives of it.  *own* is True when it
    runs as the effective user of this process: only then is it a daemon of
    this user.
    """

    pid: int
    uid: int
    own: bool


def legacy_listener(db_dir: Path) -> LegacyListener | None:
    """Return the process that listens on ``daemon.sock`` of *db_dir* (a daemon of v0.36.0 or earlier), or None.

    Such a daemon listens on a Unix socket and answers no ping, thus
    :func:`daemon_pid` does not find it.  The kernel gives the PID and the
    user of the process that listens on the socket (:func:`_peer_pid_and_uid`).
    The socket is in *db_dir*, and a socket that no process listens on
    refuses the connect.  The connect sends nothing: that daemon counts only
    a ``ping`` as a client.

    WHY the user (*own*): ``watch restart`` sends a stop and a kill to the
    PID.  The ping over TCP has the token of a file of this user; this path
    has no such secret.  When *db_dir* is in a directory that a different
    user can write, that user can listen on ``daemon.sock`` after the
    daemon ended.  WHY the effective user: the kernel gives the effective
    user of the listener, thus the compare is with ``os.geteuid()``.

    None on Windows (a daemon of v0.36.0 did not run there: Python has no
    AF_UNIX on Windows), on a system with no query of the peer (only Linux
    and macOS have one), when no process listens, and when the kernel gives
    no PID in 1 .. MAX_PID or an answer that is not complete.
    """
    if not (sys.platform.startswith("linux") or sys.platform == "darwin"):
        return None
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(2.0)
        sock.connect(str(db_dir / _LEGACY_SOCK_NAME))
        peer = _peer_pid_and_uid(sock)
    except (OSError, struct.error):
        # struct.error: a buffer of the kernel shorter than the structure.
        return None
    finally:
        sock.close()
    if peer is None:
        return None
    pid, uid = peer
    if not is_valid_pid(pid):
        return None
    return LegacyListener(pid=pid, uid=uid, own=uid == os.geteuid())


# <sys/un.h> of macOS: the socket module has no names for these.
_SOL_LOCAL = 0
_LOCAL_PEERCRED = 1
_LOCAL_PEERPID = 2
# struct xucred: u_int cr_version, uid_t cr_uid, short cr_ngroups, gid_t
# cr_groups[16] (NGROUPS 16), with the padding: 76 bytes.
_XUCRED_SIZE = 76
_XUCRED_VERSION = 0


def _peer_pid_and_uid(sock: socket.socket) -> tuple[int, int] | None:
    """Return the PID and the effective user of the process at the other end of the Unix socket *sock*.

    Linux: ``SO_PEERCRED`` gives ``struct ucred`` (pid_t pid, uid_t uid,
    gid_t gid; uid_t and gid_t are unsigned, thus a user above 2**31 - 1
    stays positive).  macOS: ``LOCAL_PEERPID`` gives the PID, and
    ``LOCAL_PEERCRED`` gives ``struct xucred``.  Measured on Linux and
    macOS 26: the PID and the user of the process that listens.

    None for an ``xucred`` of a version other than ``XUCRED_VERSION``: its
    layout can differ, and a uid from a wrong offset could match the user
    of this process.  Raise OSError when the system refuses the query, and
    struct.error for a buffer shorter than the structure.  The caller
    calls it only on Linux and macOS.
    """
    if sys.platform.startswith("linux"):
        pid, uid, _gid = struct.unpack("iII", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("iII")))
        return pid, uid
    if sys.platform == "darwin":
        (pid,) = struct.unpack("i", sock.getsockopt(_SOL_LOCAL, _LOCAL_PEERPID, struct.calcsize("i")))
        version, uid = struct.unpack_from("II", sock.getsockopt(_SOL_LOCAL, _LOCAL_PEERCRED, _XUCRED_SIZE))
        if version != _XUCRED_VERSION:
            return None
        return pid, uid
    raise OSError(errno.ENOSYS, f"no query of the peer of a Unix socket on {sys.platform}")


def _exchange(port: int, request: bytes) -> list[str] | None:
    """Send *request* to 127.0.0.1:*port* and return the words of the first line of the answer.

    None when no process listens, the connect or the read fails, or the
    answer is not ASCII.  The caller checks the words (:func:`daemon_pid`).
    """
    try:
        # loopback.create_connection: a port file of a daemon that is gone
        # names a port with no listener, and on Windows
        # socket.create_connection needed 2 s for that refused connect.
        with loopback.create_connection((_LOOPBACK, port), timeout=2.0) as sock:
            sock.sendall(request)
            answer = sock.makefile("rb").readline(256)
    except OSError:
        return None
    try:
        return answer.decode("ascii").split()
    except UnicodeDecodeError:
        return None


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
    # A ProbedLock: `watch restart` and the MCP servers probe it, and a probe
    # must not make this try fail (file_lock.ProbedLock).
    lock_file = db_dir / "watcher.lock"
    try:
        watcher_lock = ProbedLock(lock_file)
        locked = watcher_lock.try_acquire()
    except OSError as exc:
        log.error("Cannot lock %s: %s", lock_file, exc)
        sys.exit(1)
    if not locked:
        log.error("Daemon already running for %s", project_root)
        sys.exit(1)

    # Only the daemon that holds the lock writes the marker of pending work.
    # A marker that is there now is from a daemon that a SIGKILL stopped,
    # and a new process with its old PID makes it read as work that comes.
    # The startup check of the reindex loop finds that work again.
    # remove_file: on Windows an MCP server can read the marker just now.
    remove_file(db_dir / PENDING_MARKER)
    # A kill between the write and the rename of a marker leaves its
    # temporary file (`PidFile.write`).  Only the file of a dead writer goes.
    remove_stale_tmp(db_dir)
    _remove_dead_run_files(db_dir)

    pid_file = db_dir / "watcher.pid"
    # O_EXCL | O_CREAT does not follow symlinks — defense against symlink attack
    try:
        wfd = os.open(str(pid_file), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        # Stale PID file from a previous crash — clean up and retry
        remove_file(pid_file)
        wfd = os.open(str(pid_file), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(wfd, "w", encoding="utf-8") as pf:
        pf.write(str(os.getpid()))

    # ── Shared state ─────────────────────────────────────────────────────
    # No lock needed — asyncio runs in a single thread; all accesses to
    # last_ping_time are cooperative and never preempted mid-operation.
    last_ping_time: float = time.monotonic()
    shutdown: asyncio.Event = asyncio.Event()
    index_run: _IndexRun | None = None

    # ── Ping server ──────────────────────────────────────────────────────
    def _on_ping() -> None:
        nonlocal last_ping_time
        last_ping_time = time.monotonic()

    try:
        server, port_file = await _setup_ping_server(db_dir, _on_ping)
    except OSError:
        watcher_lock.close()
        remove_file(pid_file)
        sys.exit(1)

    # ── Main loop ────────────────────────────────────────────────────────
    # ── Structured logger — attach project context for log aggregation ──
    dlog = logging.LoggerAdapter(
        log,
        {"project_root": str(project_root), "pid": os.getpid()},
    )

    dlog.info("Daemon started  port file=%s", port_file)

    watch_task: asyncio.Task | None = None
    head_task: asyncio.Task | None = None
    try:
        # Signal handlers.  Inside the try: on Windows the stop listener
        # raises OSError for a stop event that exists already
        # (processes._create_own_event), and the cleanup must then close the
        # ping server and remove daemon.port and watcher.pid.  A port file of
        # a daemon that is gone sent each ping of the MCP servers to a closed
        # port.
        _setup_signal_handlers(shutdown)

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

        def _set_index_run(run: _IndexRun | None) -> None:
            """Publish the running subprocess, thus the cleanup can stop it."""
            nonlocal index_run
            index_run = run

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
            on_index_run=_set_index_run,
            build_patterns=build_patterns,
            startup_check=_staleness_check,
        )

    finally:
        shutdown.set()
        if watch_task is not None:
            await _stop_watcher(watch_task)
        if head_task is not None:
            await _stop_watcher(head_task)
        await _cleanup_daemon(index_run, server, port_file, pid_file, watcher_lock, shutdown)


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
    on_index_run: Callable[[_IndexRun | None], None],
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
        #
        # WHY the errors stop here: the check finds only the work of the
        # time when no daemon watched.  Some errors made the exception leave
        # the loop: a locked or damaged database, a file that cannot be
        # read, or a config that lost its project ID after the start
        # (`ProjectNotInitializedError`).  The daemon then stopped, and a
        # new daemon stopped on the same error.  Without the check the
        # watcher continues.  The status tools do their own check: they
        # show the error, or that no run updates the index
        # ("not_scheduled"), and they name the command.
        try:
            needs, reasons = await loop.run_in_executor(None, startup_check, project_root)
        except (sqlite3.Error, OSError, ValueError, RuntimeError):
            dlog.warning("The check at the start failed; the watcher continues without it", exc_info=True)
            needs, reasons = False, []
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
            run = await _run_index_async(project_root, db_dir, force_refs=force_refs)
        except OSError as exc:
            # The daemon stays: it watches on, and the next change tries
            # again.  Without a marker the answers tell the LLM to run
            # `fw-context index`, and that run shows the cause.
            dlog.error("Cannot start the background reindex: %s — the work waits for the next change", exc)
            superseded_runs = 0
            pending.hide_if_done()
            continue
        on_index_run(run)
        returncode = await _wait_index(run, shutdown, db_dir=db_dir)
        on_index_run(None)
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

    ``reindex.pause``: each process that holds the index lock writes it
    (``fw-context index``, ``fw-context analyze``, the MCP tools
    ``reindex_file`` and ``reset_index``, ``fw-context db``).  ``reindex.pid``:
    each ``fw-context index`` run writes it when it holds the index lock
    (``indexer._index_owner.claim_index``).
    Between two turns no run of this daemon owns either file: the run
    removes them when it ends, and ``_wait_index`` removes the files of a
    run that a kill stopped.

    The index lock itself counts too (``index_run_lock_held``):
    ``fw-context analyze`` holds it and writes no ``reindex.pid``.  Before,
    a marker that another process removed left the daemon starting a
    background run each 2 s, and each one ended on the lock.  Between two
    turns no run of this daemon holds the lock.
    """
    return (
        PidFile.is_active(db_dir / "reindex.pause")
        or PidFile.is_active(db_dir / "reindex.pid")
        or index_run_lock_held(db_dir)
    )


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


#: The largest request of a ping that the daemon reads.
_REQUEST_LIMIT = 64


async def _read_request(reader: asyncio.StreamReader) -> bytes:
    """Read the request of a ping up to its newline, or up to :data:`_REQUEST_LIMIT` bytes, or to the end.

    WHY not one ``read(64)``: TCP can give ``ping <nonce>\\n`` in two parts,
    and the first part alone (``ping``) got the answer with no proof; the
    restart then saw no daemon.  WHY not ``readline()``: a client
    that sends 64 KiB with no newline made it raise ValueError
    (LimitOverrunError).  The caller limits the time.
    """
    data = b""
    while b"\n" not in data and len(data) < _REQUEST_LIMIT:
        part = await reader.read(_REQUEST_LIMIT - len(data))
        if not part:
            break
        data += part
    return data


def _answer(request: bytes, token: str, pid_text: str) -> bytes:
    """The answer to the ping *request*: ``pong <proof> <pid>`` for ``ping <nonce>``, else ``pong``.

    WHY no token in any answer: each local process can connect and send a
    ping (module docstring).
    """
    try:
        words = request.decode("ascii").split()
    except UnicodeDecodeError:
        return b"pong\n"
    if len(words) != 2 or words[0] != "ping" or not _NONCE_PATTERN.fullmatch(words[1]):
        return b"pong\n"
    return f"pong {_proof(token, words[1], pid_text)} {pid_text}\n".encode("ascii")


async def _setup_ping_server(db_dir: Path, on_ping) -> tuple[asyncio.Server, Path]:
    """Listen for pings on 127.0.0.1, and write the port and a new token into ``daemon.port``.

    *on_ping* is called (no arguments) each time a ping arrives.  The
    answer to ``ping <nonce>`` is ``pong <proof> <pid>``, to a ``ping`` with
    no valid nonce ``pong`` (see the module docstring).  The system
    selects the port (port 0), thus two daemons never need the same one.
    The file is written after the bind and atomically: a reader sees no
    file, or a file with a port on which this daemon listens.

    Returns the server and the port file.
    """
    port_file = _port_file(db_dir)
    # No unlink of the port file of an earlier daemon: the write below
    # replaces it, and a stale file reads as no daemon (the token).  On
    # Windows an unlink fails while a pinger has the file open.
    (db_dir / _LEGACY_SOCK_NAME).unlink(missing_ok=True)
    token = secrets.token_hex(16)
    pid_text = str(os.getpid())

    async def _handle_ping(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            data = await asyncio.wait_for(_read_request(reader), timeout=2.0)
            if data.startswith(b"ping"):
                on_ping()
                writer.write(_answer(data, token, pid_text))
                await writer.drain()
        except (TimeoutError, OSError):
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except OSError:
                pass

    try:
        server = await asyncio.start_server(_handle_ping, host=_LOOPBACK, port=0)
    except OSError as exc:
        # Not "daemon already running": this process holds `watcher.lock`,
        # thus no other daemon of the project runs.  The error says the cause.
        log.error("Cannot listen on %s for the pings: %s", _LOOPBACK, exc)
        raise
    port = server.sockets[0].getsockname()[1]
    try:
        # 0600: the token is for the processes of this user only.  Another
        # user who reads it could answer on the port with a valid proof
        # after a SIGKILL of the daemon: a ping would then read as a live
        # daemon, and the PID of that answer would get the stop of
        # `watch restart`.  No answer holds the token.  On Windows the
        # mode limits no reader: the ACL of the index directory does (the
        # default one is in the profile of the user, docs/tools.md).
        write_text_atomic(port_file, f"{port} {token}\n", mode=0o600)
    except OSError as exc:
        log.error("Cannot write %s: %s", port_file, exc)
        server.close()
        await server.wait_closed()
        raise
    return server, port_file


def _setup_signal_handlers(shutdown: asyncio.Event) -> None:
    """Register SIGTERM / SIGINT handlers that set *shutdown*, and the stop listener.

    POSIX: ``loop.add_signal_handler``.  Windows has no such call (the
    Proactor loop raises NotImplementedError), and the daemon then had no
    handler at all: ``fw-context watch stop`` ended it with
    ``TerminateProcess``, and a stop request (``processes.request_stop``)
    did nothing, because ``interrupt_main`` runs only a Python handler.  On
    Windows the handler is a ``signal.signal`` handler: the Proactor loop
    wakes for a signal through its wakeup socket, and the handler gives
    *shutdown* to the loop.
    """
    loop = asyncio.get_running_loop()

    def _handle_signal() -> None:
        shutdown.set()

    if sys.platform == "win32":
        def _from_signal(signum: int, frame: object) -> None:
            # The handler stays after asyncio.run() closed the loop, and a
            # late signal must not raise "Event loop is closed" in the
            # cleanup of the process.
            if not loop.is_closed():
                loop.call_soon_threadsafe(_handle_signal)

        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, _from_signal)
        install_stop_listener()
        return
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _handle_signal)


async def _cleanup_daemon(
    index_run: _IndexRun | None,
    server: asyncio.Server,
    port_file: Path,
    pid_file: Path,
    watcher_lock: ProbedLock,
    shutdown: asyncio.Event,
) -> None:
    """Gracefully shut down the daemon: terminate subprocess, close server, clean up files."""
    log.info("Daemon shutting down")
    if index_run is not None and index_run.proc.returncode is None:
        log.info("Terminating index subprocess (pid %d)", index_run.proc.pid)
        _stop_run(index_run)
        try:
            await asyncio.wait_for(index_run.proc.wait(), timeout=RUN_STOP_WAIT_S)
        except TimeoutError:
            log.warning("Index subprocess did not exit, killing")
            index_run.proc.kill()
            await index_run.proc.wait()
    shutdown.set()
    server.close()
    await server.wait_closed()
    # Work that this daemon did not start goes with it.  The startup check
    # of the next daemon finds it again.
    PidFile(port_file.parent / PENDING_MARKER).unlink_if_ours()
    _cleanup_files(port_file, pid_file)
    watcher_lock.close()


def _is_source_file(path_str: str, build_patterns: list[str] | None = None) -> bool:
    """Return True when *path_str* is a C/C++ source file outside excluded dirs.

    Uses hardcoded ``_EXCLUDE_RX`` as baseline and supplements with
    *build_patterns* from the project manifest when available (derived
    from SDK detection and ``[index] exclude_paths`` config).
    """
    p = Path(path_str)
    if p.suffix not in _SOURCE_EXTS_WATCH:
        return False
    # _EXCLUDE_RX and the build patterns use "/", as the index keys do
    # (path_key).  watchfiles gives a path of the system: "\\" on Windows.
    posix = p.as_posix()
    if _EXCLUDE_RX.search(posix):
        return False
    if build_patterns and any(pat in posix for pat in build_patterns):
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


def _cleanup_files(port_file: Path, pid_file: Path) -> None:
    """Remove daemon runtime files.

    On Windows a pinger can read the port file just now: ``remove_file``
    waits for that read.  A port file that still cannot go stays: its token
    reads as no daemon, and the next daemon replaces it.
    """
    for path in (port_file, pid_file):
        try:
            remove_file(path)
        except OSError as exc:
            log.warning("Cannot remove %s: %s — the next daemon replaces it", path, exc)


# ── Staleness check at the start of the daemon ───────────────────────────────


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
        from .shared.stale import build_label, latest_builds, outdated_index_fact

        # Before the first read of a build row.  The run that this reason
        # starts opens the index with `open_db`, which adds the columns.
        outdated = outdated_index_fact(conn)
        if outdated:
            return True, [outdated]

        project_id = derive_project_id(project_root)
        live_head = read_head(project_root)
        for cfg in latest_builds(conn, project_id):
            config_hash = cfg["config_hash"]
            label = build_label(cfg)
            prefix = f"{label}: " if label else ""

            # 1-4. Structural checks: compile_commands.json, schema version,
            #      row format, refs.  `index_state` does checks 1-3 and not the refs.
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


@dataclass(frozen=True)
class _IndexRun:
    """An index run that the daemon started: its process and its stop token (``processes.STOP_TOKEN_ENV``).

    WHY a token: on Windows the python.exe of a virtual environment is a
    launcher, and the PID of the Popen is not the PID of the run, thus the
    run cannot make an event for that PID.  WHY a new token for each run: a
    run that the daemon killed keeps its token event until Windows has torn
    the process down, after the launcher ended.  The next run with the same
    token then found that event and did not start
    (``processes._create_own_event``).  WHY the token in the run object and
    not in a table of the module: the stop request needs the token of THIS
    process, and the object carries the two together.
    """

    proc: asyncio.subprocess.Process
    stop_token: str


async def _run_index_async(
    project_root: Path, db_dir: Path, *, force_refs: bool = False,
) -> _IndexRun:
    """Spawn ``fw-context index --background``, write stdout to ``reindex.log``.

    No ``--build`` here, and the daemon decides nothing about building.  The
    index run turns the build on itself where it is needed and allowed —
    ``cli._index._plan_auto_build`` — because only that path also sets the
    isolated output directory that makes a background build legal.  A
    ``--build`` from here failed with "--build and --background are mutually
    exclusive" in under a second.

    WHY the daemon writes no ``reindex.pid``: the run writes it when it
    holds the index lock (``indexer._index_owner.claim_index``), and removes it when
    it releases the lock.  A file that the daemon wrote before the spawn
    named a run that could lose the lock to a manual run, and the daemon
    then overwrote the file of that manual run.  Until the run holds the
    lock, the marker of the pending work shows it.

    Returns the run (its asyncio Process and its stop token) — caller must
    wait or terminate.
    Raises :class:`OSError` when the subprocess cannot start.
    """
    log_file = db_dir / "reindex.log"
    log_target: int | TextIO
    try:
        fd = os.open(log_file, O_NOFOLLOW | os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        log_target = os.fdopen(fd, "w", encoding="utf-8", closefd=True)
    except OSError as exc:
        # The run does its work also without a log.
        log.warning("Cannot open %s: %s — the background index runs without a log", log_file, exc)
        log_target = asyncio.subprocess.DEVNULL

    env: dict[str, str] = {
        **os.environ,
        "PYTHONUNBUFFERED": "1",
    }
    token = new_stop_token()
    env[STOP_TOKEN_ENV] = token
    if force_refs:
        env["FW_CONTEXT_FORCE_REFINDEX"] = "1"

    log.info("Starting background index for %s (force_refs=%s)", project_root, force_refs)
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", "-m", "fw_context_mcp.cli", "index", "--background",
            cwd=str(project_root),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=log_target,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        return _IndexRun(proc, token)
    finally:
        # The subprocess has its own copy of the file descriptor.
        if not isinstance(log_target, int):
            log_target.close()


def _stop_run(run: _IndexRun) -> None:
    """Ask the index *run* to stop; end it at once when it cannot get the request.

    POSIX: ``Process.terminate()``, which is SIGTERM and sends nothing to a
    child that asyncio already reaped (a PID that the system can give to
    another process).  Windows: the stop event of the token of the run
    (``processes.request_stop``); ``Process.terminate()`` is
    ``TerminateProcess`` there and runs no handler.

    A run without a listener (on Windows a run that has not reached
    ``cmd_index`` yet) ends at once, as SIGTERM with its default action
    ends it on POSIX.  The caller waits for the end, and kills after
    :data:`RUN_STOP_WAIT_S`.
    """
    proc = run.proc
    if sys.platform == "win32":
        if request_stop(proc.pid, token=run.stop_token):
            return
    else:
        try:
            proc.terminate()
            return
        except ProcessLookupError:
            return
    try:
        proc.kill()
    except ProcessLookupError:
        pass


async def _wait_index(
    run: _IndexRun,
    shutdown: asyncio.Event,
    *,
    db_dir: Path,
) -> int | None:
    """Wait for the process of *run* to finish, polling *shutdown* every 500 ms.

    Returns the exit code of the run, or None when *shutdown* stopped it.
    On shutdown the run gets the stop request (:func:`_stop_run`), and
    SIGKILL after :data:`RUN_STOP_WAIT_S`.

    Removes the markers of the run when it ended (:func:`_remove_markers_of_run`).
    """
    proc = run.proc
    try:
        while proc.returncode is None:
            if shutdown.is_set():
                _stop_run(run)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=RUN_STOP_WAIT_S)
                except TimeoutError:
                    log.warning("Index subprocess did not exit after the stop request, killing")
                    proc.kill()
                    await proc.wait()
                return None
            await asyncio.sleep(0.5)
    finally:
        # Not for a run that still runs (a cancel of this wait): its
        # markers are live, and the run removes them itself.
        if proc.returncode is not None:
            _remove_markers_of_run(db_dir, run)
    return proc.returncode


def _remove_dead_run_files(db_dir: Path) -> None:
    """Remove each PID file of a run (``_index_owner.run_pid_path``) whose process is gone.

    Such a file stays when a kill stopped the run while no daemon waited for
    it (a daemon that a kill stopped too).  ``PidFile.is_active`` removes the
    file of a dead PID; a run that still runs removes its own file at its
    end.
    """
    for run_file in db_dir.glob("run.*.pid"):
        PidFile.is_active(run_file)


def _remove_markers_of_run(db_dir: Path, run: _IndexRun) -> None:
    """Remove ``reindex.pid`` and ``reindex.pause`` when they still name the ended *run*, and its PID file.

    WHY: a run that a SIGKILL stopped cannot remove them, and the PID can
    come back as another process.  A ``reindex.pause`` of that PID then
    makes ``_index_is_free`` wait, with no time limit, for a process that
    has no relation to the index.  The files of another run stay.

    WHY the PID from ``run.<token>.pid`` (``_index_owner.run_pid_path``)
    and not ``proc.pid``: on Windows ``proc.pid`` is the PID of the launcher
    of the virtual environment, and the markers hold the PID of its child,
    the run.  No file: the run wrote no marker (it writes the file first),
    or it removed its markers and the file itself.
    """
    from ..indexer._index_owner import run_pid_path

    run_file = run_pid_path(db_dir, run.stop_token)
    pid = PidFile.read_pid(run_file)
    if pid is not None:
        PidFile(db_dir / "reindex.pid", pid=pid).unlink_if_ours()
        PidFile(db_dir / "reindex.pause", pid=pid).unlink_if_ours()
        PidFile(run_file, pid=pid).unlink_if_ours()


# ── DB helper ────────────────────────────────────────────────────────────────


def _optimize_db(db_dir: Path) -> None:
    """Run PRAGMA optimize to shrink the WAL file and defragment indexes.

    Deliberately uses a writable ``open_db`` (whitelisted write path):
    this is a rare maintenance operation outside the request path, and
    WAL allows concurrent readers while it runs.  Not a bug — do not
    "convert" this to the read-only quick open; writes need a writable
    connection.

    WHY no integrity check: the daemon calls this after an index run that
    ended with 0, and that run did the check.  A second check reads the
    full file again, and the daemon does not use its result.  The next
    index run does the check.

    WHY the errors stop here: the reindex loop calls this function, and an
    error of the open went out of the loop.  The daemon then stopped, and
    no change got an index run.  The maintenance is not necessary, thus a
    warning is sufficient.

    WHY the index lock: within one project each writer holds it, and the
    optimize can write (ANALYZE).  A manual run that took the lock when
    the background run ended got ``database is locked`` in the store of a
    unit.  The daemon takes the lock only when it is free, and skips the
    optimize when another process holds it: the maintenance can wait for
    the next run.
    """
    from ..indexer.db import open_db
    from ..indexer.db._locking import IndexRunLocked, index_run_lock

    db_path = db_dir / "index.db"
    if not db_path.exists():
        return
    try:
        with index_run_lock(db_dir):
            try:
                conn = open_db(db_path, skip_integrity_check=True)
            except (sqlite3.Error, OSError):
                log.warning("Cannot open the database for PRAGMA optimize", exc_info=True)
                return
            try:
                conn.execute("PRAGMA optimize")
            except (sqlite3.Error, ValueError, TypeError, RuntimeError, AttributeError):
                log.debug("PRAGMA optimize failed", exc_info=True)
            finally:
                conn.close()
    except IndexRunLocked:
        log.debug("Another process holds the index: no PRAGMA optimize")


# ── Entry point ───────────────────────────────────────────────────────────────


if __name__ == "__main__":
    import sys as _sys

    from ..utils import use_utf8_stdio

    use_utf8_stdio()
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
