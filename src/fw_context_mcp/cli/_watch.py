"""``fw-context watch`` — background watcher daemon management.

Manages the file-watching daemon that keeps the symbol index current by
reindexing modified source files automatically.  The daemon monitors the
project directory for changes and starts ``fw-context index --background``,
which re-parses the changed units of each build.

WHY a daemon: during development, the index quickly falls out of sync with
source files if the user must remember to reindex manually.  The daemon
uses inotify/FSEvents to detect changes and reindexes within seconds.

WHY debouncing: editors save frequently (auto-save, rapid edits).
Reindexing on every event would waste CPU.  A cooldown period collects
multiple rapid saves into a single reindex.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from ..file_lock import STATUS_WAIT_S, is_free, wait_until_free

if TYPE_CHECKING:
    from ..mcp.daemon import LegacyListener


def cmd_watch(args: argparse.Namespace) -> int:
    """Dispatcher for ``fw-context watch`` sub-subcommands."""
    if getattr(args, "watch_command", None) is None:
        print("Usage: fw-context watch <status|restart>")
        return 1
    return args.func(args)


def cmd_watch_status(args: argparse.Namespace) -> int:
    """Print the watcher daemon status for a project."""

    from ..config import derive_project_id
    from ..config import load as load_config
    from ..mcp.background import read_reindex_progress, read_reindex_state
    from ..mcp.daemon import DAEMON_PORT_NAME, _read_port_file
    from ..mcp.shared.pid_file import read_pid_file
    from ..utils import resolve_project_root

    root = resolve_project_root(args.project)
    cfg = load_config(project_root=root)
    project_id = derive_project_id(root)
    db_dir = cfg.index.db_dir / project_id

    pid_file = db_dir / "watcher.pid"
    port_file = db_dir / DAEMON_PORT_NAME

    print(f"Project:    {root.name}")
    print(f"Path:       {root}")
    print(f"DB dir:     {db_dir}")

    # Daemon status.  The PID comes from the daemon itself (_find_daemon):
    # watcher.pid can name a different process.  The file gives the uptime
    # only when it names the same PID.
    found = _find_daemon(db_dir)
    alive = found is not None and found.version == "current"
    if found is not None:
        try:
            file_pid, _ = read_pid_file(pid_file)
        except OSError:
            file_pid = None  # no uptime: the PID of the file is not known
        old = "" if found.version == "current" else (
            ", v0.36.0 or earlier: restart the LLM client of that version, then run 'fw-context watch restart'"
        )
        if found.other_user is not None:
            print(f"Daemon:     not running ({_other_user_text(found.other_user)})")
        elif found.pid == file_pid:
            import time as _time

            try:
                uptime_s = _time.time() - pid_file.stat().st_mtime
            except OSError:
                print(f"Daemon:     running (pid {found.pid}{old})")
            else:
                print(f"Daemon:     running (pid {found.pid}, uptime {int(uptime_s)}s{old})")
        else:
            print(f"Daemon:     running (pid {found.pid}{old})")
    else:
        if port_file.exists():
            print("Daemon:     not responding (port file exists but no answer)")
        elif pid_file.exists():
            print("Daemon:     dead (pid file exists but port file missing)")
        else:
            print("Daemon:     not running")

    # Port file of the ping (TCP on 127.0.0.1): the port, for netstat or a
    # firewall tool when the daemon does not answer.
    known = _read_port_file(port_file)
    port_text = f" (port {known[0]})" if known else ""
    print(f"Port file:  {port_file}{port_text}{' (active)' if alive else ''}")

    # Modified files count
    db_path = db_dir / "index.db"
    if db_path.exists():
        try:
            _print_modified_counts(db_path, project_id, root)
        except (sqlite3.Error, OSError):
            pass

    # Index subprocess.  "scheduled" is work of the daemon that waits for
    # the debounce, for a manual operation, or for the start of its run.
    state = read_reindex_state(root)
    if state == "reindexing":
        print("Index:      running")
    elif state == "pending":
        print("Index:      scheduled")
    else:
        print("Index:      idle")

    # Last line the index run itself wrote.  Not the last line of the
    # file: reindex.log also holds the output of the build tools, and the
    # helper says why taking that would be wrong.
    last = read_reindex_progress(db_dir)
    if last is not None:
        print(f"Last index: {last}")

    return 0


def cmd_watch_restart(args: argparse.Namespace) -> int:
    """Restart the watcher daemon for a project."""
    import time as _time

    from ..config import derive_project_id
    from ..config import load as load_config
    from ..mcp.background import _ensure_daemon_running
    from ..mcp.daemon import ping_daemon
    from ..processes import kill_hard, request_stop
    from ..utils import resolve_project_root

    root = resolve_project_root(args.project)
    cfg = load_config(project_root=root)
    project_id = derive_project_id(root)
    db_dir = cfg.index.db_dir / project_id

    pid_file = db_dir / "watcher.pid"
    lock_file = db_dir / "watcher.lock"

    # The daemon holds watcher.lock for its whole life, thus the lock and
    # not the PID file tells if a daemon runs.  WHY: a PID file of a daemon
    # that a SIGKILL or a reboot ended can name a different process now
    # (Windows uses PIDs again soon), and that process must not get a stop
    # or a kill.  The lock is free when the process ended: also a daemon
    # that its MCP server did not reap yet (a zombie, whose PID still
    # exists) has no lock, and on Windows the lock was free at once after
    # the end (measured, 20 of 20 times).  file_lock.is_free tells why the
    # restart tries the locks itself and does not only probe them.  Its try
    # of the exclusion lock can make a daemon that an MCP server starts in
    # that moment exit: the restart starts a daemon itself after the check,
    # thus that costs nothing.
    try:
        pid = _running_daemon(lock_file, pid_file, db_dir)
    except _NoDaemonAnswer as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: cannot check the lock {lock_file}: {exc}", file=sys.stderr)
        return 1
    if pid is not None:
        print(f"Stopping daemon (pid {pid})...")
        # The request runs the SIGTERM handler of the daemon, also on
        # Windows (processes.request_stop).
        request_stop(pid)
        if not wait_until_free(lock_file, _stop_wait_s()):
            print("Daemon did not stop, ending it...")
            try:
                kill_hard(pid)
            except OSError as exc:
                print(f"error: cannot end the daemon (pid {pid}): {exc}", file=sys.stderr)
                return 1
            # SIGKILL and TerminateProcess end the process after they
            # return: a new daemon that came before the end found the lock
            # held and did not start.
            if not wait_until_free(lock_file, _KILL_WAIT_S):
                print(f"error: the daemon (pid {pid}) did not end", file=sys.stderr)
                return 1
        print("Old daemon stopped.")

    # No removal of the files of the old daemon.  WHY: an MCP server can
    # start a daemon in each moment after the lock was free, and a removal
    # then took daemon.port and watcher.pid of that new daemon.  No ping
    # found it, and it exited after PING_TIMEOUT.  The files of a daemon
    # that is gone do no harm: daemon.port fails the proof of the ping,
    # the restart never stops the PID of watcher.pid, and the new daemon
    # writes the two files again.  Not watcher.lock either: a removed file
    # gives the new daemon a new inode, thus a lock of its own beside an
    # old daemon that survived its SIGKILL.

    # Spawn new daemon
    print("Starting new daemon...")
    _ensure_daemon_running(root)

    # Wait and verify
    _time.sleep(0.5)
    if ping_daemon(root):
        print("Daemon restarted successfully.")
    else:
        print("Daemon may still be starting — check 'fw-context watch status' in a moment.")

    return 0


#: The time of the shutdown of a daemon after the end of its index run: the
#: poll of the run (0.5 s), the kill of a run that did not stop, the close
#: of the port, and the removal of its files.
_SHUTDOWN_MARGIN_S = 5.0
#: How long the system gets to end the daemon after ``kill_hard``.
_KILL_WAIT_S = 5.0


def _stop_wait_s() -> float:
    """Return how long a daemon gets to release its lock after the stop request.

    WHY longer than ``daemon.RUN_STOP_WAIT_S``: the daemon waits that long
    for its index run to stop, and then it removes the markers of the run.
    A wait of 3 s killed the daemon in that time: the run and its markers
    stayed.  WHY a function: this module imports ``mcp.daemon`` only in
    the functions that use it, not when the CLI starts.
    """
    from ..mcp.daemon import RUN_STOP_WAIT_S

    return RUN_STOP_WAIT_S + _SHUTDOWN_MARGIN_S


#: How long the restart waits for the daemon that holds the lock to answer
#: a ping.  A daemon that starts holds the lock before it writes its port
#: file: its try of the lock can wait for the status lock
#: (``file_lock.STATUS_WAIT_S``), and then it removes stale files and opens
#: its port.  The 5 s after that limit are for the rest of its start.
_ANSWER_WAIT_S = STATUS_WAIT_S + 5.0


class _NoDaemonAnswer(RuntimeError):
    """A process holds ``watcher.lock``, and no daemon of the index directory gives its PID within the wait."""


@dataclass(frozen=True)
class _FoundDaemon:
    """A daemon of an index directory that gave its PID (:func:`_find_daemon`).

    *version* is ``"current"`` (an answer to the ping over TCP) or
    ``"v0.36"`` (a daemon of v0.36.0 or earlier on ``daemon.sock``).  *pid*
    is None only when a process of a different user listens on
    ``daemon.sock`` (*other_user* is then that process): an answer over TCP
    with no valid PID fails its proof and is no answer.
    """

    pid: int | None
    version: Literal["current", "v0.36"]
    other_user: LegacyListener | None = None


def _find_daemon(db_dir: Path) -> _FoundDaemon | None:
    """Return the daemon of *db_dir* with the PID that it gives of itself, or None when no daemon answers.

    The one rule of ``watch status`` and ``watch restart``.  WHY the PID
    from the daemon and not from ``watcher.pid``: a daemon that a kill ended
    left the file, the system gave its PID to a new process, and a daemon
    that starts holds the lock before it writes its own PID.  The restart
    sent a stop and a kill to that process.

    1. The answer to ``ping <nonce>`` (``daemon.daemon_pid``): an HMAC of
       the nonce and the PID, with the token of ``daemon.port`` as the key,
       authenticates it.
    2. A daemon of v0.36.0 or earlier listens on ``daemon.sock`` and answers
       no ping: the kernel gives the PID and the user of the process that
       listens (``daemon.legacy_listener``).  A process of a different
       user gives no PID: it is no daemon of this user, and the restart
       must not stop it.
    """
    from ..mcp.daemon import daemon_pid, legacy_listener

    answer = daemon_pid(db_dir)
    if answer is not None:
        return _FoundDaemon(pid=answer.pid, version="current")
    listener = legacy_listener(db_dir)
    if listener is None:
        return None
    if listener.own:
        return _FoundDaemon(pid=listener.pid, version="v0.36")
    return _FoundDaemon(pid=None, version="v0.36", other_user=listener)


def _running_daemon(lock_file: Path, pid_file: Path, db_dir: Path) -> int | None:
    """Return the PID of the daemon that holds *lock_file*, or None when no process holds it.

    The PID comes from the daemon (:func:`_find_daemon`), thus the restart
    stops no other process.  The restart waits until the daemon gives it,
    or until the lock is free (a daemon that ends): a daemon that starts
    holds the lock before it opens its port.

    Raise :class:`_NoDaemonAnswer` when no PID comes for
    :data:`_ANSWER_WAIT_S` while the lock stays held, and at once when the
    daemon answers with no PID (its answer does not change) and the lock
    is still held then (a daemon that ends releases it).  WHY no stop
    then: the only other source of its PID is ``watcher.pid``, which can
    name a different process.  The message tells the PID file, and the
    person checks that process before ending it.  Raise OSError when the
    lock file cannot be opened.
    """
    import time

    deadline = time.monotonic() + _ANSWER_WAIT_S
    while True:
        if is_free(lock_file):
            return None
        found = _find_daemon(db_dir)
        if found is not None:
            if found.other_user is None:
                return found.pid
            if is_free(lock_file):
                return None
            raise _NoDaemonAnswer(
                f"{lock_file} is held, and {_other_user_text(found.other_user)}.  {_stop_by_hand(pid_file)}",
            )
        if time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    # The text after the wait, and not in each turn of the loop: the
    # message needs it one time.
    raise _NoDaemonAnswer(
        f"a process holds {lock_file} for {_ANSWER_WAIT_S:.0f} s, and no daemon answered the ping.  "
        f"When that process is a daemon that hangs: {_stop_by_hand(pid_file)}",
    )


def _other_user_text(other: LegacyListener) -> str:
    """Tell, for a message, that the listener *other* on ``daemon.sock`` is a process of a different user.

    WHY its PID and user in the text: the restart stops it not, and the
    person must know why.
    """
    return f"process {other.pid} of user {other.uid} listens on daemon.sock: it is no daemon of your user"


def _stop_by_hand(pid_file: Path) -> str:
    """Tell how a person stops a daemon that gives no PID, with what *pid_file* holds.

    WHY a person and not the restart: the only other source of the PID is
    the file, and it can name a different process.  A person can check the
    command line of the process; the restart does not stop a process on a
    guess.
    """
    return (
        f"{_describe_pid_file(pid_file)}.  Make sure that the PID names the daemon (its command "
        "line holds 'fw_context_mcp.mcp.daemon'; the system can give the PID of an ended daemon to "
        "another process), end it, and run 'fw-context watch restart' again"
    )


def _describe_pid_file(pid_file: Path) -> str:
    """Tell what *pid_file* holds, for a message: its PID, or why it gives none.

    WHY the restart reports the file and does not stop its PID: the file
    can name a different process (a daemon that a kill ended left it, and
    the system gave its PID to a new process).  The person checks that
    process before ending it.
    """
    from ..mcp.shared.pid_file import read_pid_file

    try:
        pid, problem = read_pid_file(pid_file)
    except OSError as exc:
        return f"{pid_file} cannot be read: {exc}"
    if pid is not None:
        return f"{pid_file} names PID {pid}"
    if problem is not None:
        return f"{pid_file} {problem}"
    return f"{pid_file} does not exist"



def _print_modified_counts(db_path, project_id: str, root) -> None:
    """Print the count of the modified files of each build, read-only.

    The status migrates no schema beside an index run.  An index that it
    cannot read (an older schema after an upgrade) gives one line, and the
    status goes on: the daemon migrates the index, and the lines after the
    count show that run.  Before, the status stopped here with exit 1.
    """
    from ..indexer.db import open_db_readonly
    from ..mcp.shared.stale import _count_modified_files, build_label, latest_builds
    from ._builds import reading_problem

    conn = open_db_readonly(db_path)
    try:
        problem = reading_problem(conn)
        if problem is not None:
            print(f"Modified:   not known: {problem[1]}")
            return
        # Each build: the builds of one project are not always indexed
        # together, thus one build can be behind while the newest is
        # current.  The daemon checks each build too.  One hash of each
        # file for all builds: a file that two builds compile has one text
        # on disk.
        hash_cache: dict[str, str] = {}
        for build in sorted(latest_builds(conn, project_id), key=build_label):
            mod_count = _count_modified_files(
                conn, build["config_hash"], root, use_cache=False, hash_cache=hash_cache,
            )
            print(f"Modified:   {mod_count} file(s) in build {build_label(build) or '(no name)'}")
    finally:
        conn.close()
