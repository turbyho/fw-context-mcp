"""``fw-context watch restart`` waits for the lock of the old daemon, not for its PID or a fixed time.

The daemon holds ``watcher.lock`` for its whole life.  A new daemon starts
only when that lock is free.  SIGKILL and TerminateProcess end a process
after they return: the restart slept 0.5 s, and a new daemon that came
before the end found the lock held.  A wait for the end of the PID did not
work either: the MCP server is the parent of the daemon and reaps it late,
and the PID of a zombie still exists, while its lock is free.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# Before each monkeypatch of fw_context_mcp.processes: _index_owner and the
# daemon bind request_stop and kill_hard by name at their first import, and
# the restart imports modules at its first call.  A first import inside a
# test that replaced the two kept the fakes for each later test (the
# takeover tests then sent no stop).
import fw_context_mcp.indexer._index_owner  # noqa: F401
import fw_context_mcp.mcp.daemon  # noqa: F401
from fw_context_mcp.file_lock import wait_until_free

_HOLD = """
import sys, time
from pathlib import Path
from fw_context_mcp.file_lock import ProbedLock
held = ProbedLock(Path(sys.argv[1]))
assert held.try_acquire()
print("locked", flush=True)
time.sleep(60)
"""


def test_the_lock_of_a_killed_process_that_nobody_reaped_is_free(tmp_path: Path):
    """The process is gone (a zombie on POSIX): its lock is free, though its PID exists."""
    lock_file = tmp_path / "watcher.lock"
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLD, str(lock_file)], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
    )
    try:
        assert proc.stdout.readline().strip() == b"locked"
        assert not wait_until_free(lock_file, 0.3), "the lock of a running process is held"
        proc.kill()  # no wait: nobody reaps the child yet
        assert wait_until_free(lock_file, 30.0)
    finally:
        proc.kill()
        proc.wait()
        proc.stdout.close()


def test_a_free_lock_needs_no_wait(tmp_path: Path):
    lock_file = tmp_path / "watcher.lock"
    lock_file.touch()

    assert wait_until_free(lock_file, 0.0)


def test_a_stale_pid_file_stops_no_process(tmp_path: Path, monkeypatch):
    """A daemon that a SIGKILL ended left watcher.pid, and its PID now names another process.

    The restart sent that process a stop request and then a kill.  The lock
    is free, thus no daemon runs, and nothing is stopped.
    """
    import argparse

    import fw_context_mcp.mcp.background as background
    import fw_context_mcp.mcp.daemon as daemon
    import fw_context_mcp.processes as processes
    from fw_context_mcp.cli._watch import cmd_watch_restart
    from tests._toml import toml_string

    index = tmp_path / "index"
    (tmp_path / ".fw-context").mkdir()
    (tmp_path / ".fw-context" / "config.toml").write_text(
        f'[project]\nid = "{"d" * 32}"\n\n[index]\ndb_dir = {toml_string(index)}\n', encoding="utf-8",
    )
    db_dir = index / ("d" * 32)
    db_dir.mkdir(parents=True)
    (db_dir / "watcher.pid").write_text(str(os.getpid()), encoding="utf-8")  # a live process, not a daemon
    stopped: list[int] = []
    monkeypatch.setattr(processes, "request_stop", lambda pid: stopped.append(pid) or True)
    monkeypatch.setattr(processes, "kill_hard", lambda pid: stopped.append(pid))
    monkeypatch.setattr(background, "_ensure_daemon_running", lambda root: None)
    monkeypatch.setattr(daemon, "ping_daemon", lambda root: True)

    assert cmd_watch_restart(argparse.Namespace(project=str(tmp_path))) == 0
    assert stopped == []


def _project(tmp_path: Path) -> Path:
    """Make a project with an index directory in *tmp_path*, and return its index directory."""
    from tests._toml import toml_string

    index = tmp_path / "index"
    (tmp_path / ".fw-context").mkdir()
    (tmp_path / ".fw-context" / "config.toml").write_text(
        f'[project]\nid = "{"d" * 32}"\n\n[index]\ndb_dir = {toml_string(index)}\n', encoding="utf-8",
    )
    db_dir = index / ("d" * 32)
    db_dir.mkdir(parents=True)
    return db_dir


def _restart_with_a_lock_holder(
    tmp_path: Path, monkeypatch, content: str | bytes | None, answer: object, wait: float = 0.3,
) -> list[int]:
    """Run the restart while this process holds watcher.lock, with *content* in watcher.pid.

    *answer* is what ``daemon_pid`` gives on each ping, and *wait* the time
    limit of the restart; no daemon listens on
    ``daemon.sock``.  Return each PID that got a stop or a kill (none is
    sent: os.kill and the two functions are fakes).
    """
    import fw_context_mcp.cli._watch as watch
    import fw_context_mcp.mcp.background as background
    import fw_context_mcp.mcp.daemon as daemon
    import fw_context_mcp.processes as processes
    from fw_context_mcp.file_lock import ProbedLock

    db_dir = _project(tmp_path)
    if isinstance(content, bytes):
        (db_dir / "watcher.pid").write_bytes(content)
    elif content is not None:
        (db_dir / "watcher.pid").write_text(content, encoding="utf-8")
    sent: list[int] = []
    monkeypatch.setattr(os, "kill", lambda target, signum: sent.append(target))
    monkeypatch.setattr(processes, "request_stop", lambda pid: sent.append(pid) or True)
    monkeypatch.setattr(processes, "kill_hard", lambda pid: sent.append(pid))
    monkeypatch.setattr(background, "_ensure_daemon_running", lambda root: None)
    monkeypatch.setattr(daemon, "daemon_pid", lambda db_dir: answer)
    monkeypatch.setattr(watch, "_ANSWER_WAIT_S", wait)
    # The lock of a running daemon: a second descriptor of this process
    # conflicts with it (flock and LockFileEx lock per open file).
    daemon_lock = ProbedLock(db_dir / "watcher.lock")
    try:
        assert daemon_lock.try_acquire()
        assert cmd_watch_restart_of(tmp_path) == 1
    finally:
        daemon_lock.close()
    return sent


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        ("0", "which is no PID"),
        ("-1", "which is no PID"),
        (str(2**40), "which is no PID"),
        (str(os.getpid()), f"names PID {os.getpid()}"),
        (b"\xff\xfe", "is not UTF-8"),
        (None, "does not exist"),
    ],
    ids=["zero", "minus-one", "above-range", "a-live-process", "not-utf8", "no-file"],
)
def test_a_lock_holder_that_answers_no_ping_stops_no_process(
    tmp_path: Path, monkeypatch, capsys, content: str | bytes | None, reason: str,
):
    """A process holds the lock, and no daemon answers the ping: the restart stops nothing.

    The restart sent a stop and a kill to the PID in watcher.pid.  ``kill(0)``
    is the process group of the restart itself, ``kill(-1)`` each process of
    the user, and a live process with a PID that a dead daemon left is a
    process that nobody asked to stop.  The restart now stops only the PID
    that the daemon gives of itself, waits for it (a daemon that starts
    holds the lock before it opens its port), and then reports what the PID
    file holds, for the person who checks the process.
    """
    assert _restart_with_a_lock_holder(tmp_path, monkeypatch, content, None) == []
    err = capsys.readouterr().err
    assert "no daemon answered the ping" in err
    assert reason in err


def cmd_watch_restart_of(root: Path) -> int:
    """Run ``fw-context watch restart`` for the project *root*."""
    import argparse

    from fw_context_mcp.cli._watch import cmd_watch_restart

    return cmd_watch_restart(argparse.Namespace(project=str(root)))


@pytest.mark.parametrize("version", ["current", "v0.36"])
def test_the_restart_stops_the_pid_that_the_daemon_gives(tmp_path: Path, monkeypatch, version: str):
    """The restart waits for a daemon that starts, and stops the PID that the daemon gives of itself.

    watcher.pid names a live process that a dead daemon left (this test).
    A daemon of v0.36.0 or earlier answers no ping: the kernel gives the
    PID of the process that listens on its ``daemon.sock``.
    """
    import fw_context_mcp.mcp.background as background
    import fw_context_mcp.mcp.daemon as daemon
    import fw_context_mcp.processes as processes
    from fw_context_mcp.file_lock import ProbedLock

    db_dir = _project(tmp_path)
    daemon_pid = 424242
    (db_dir / "watcher.pid").write_text(str(os.getpid()), encoding="utf-8")
    daemon_lock = ProbedLock(db_dir / "watcher.lock")
    found = iter([None, None, daemon_pid])
    stopped: list[int] = []

    def stop(pid: int) -> bool:
        stopped.append(pid)
        daemon_lock.release()
        return True

    if version == "current":
        monkeypatch.setattr(
            daemon, "daemon_pid",
            lambda db_dir: None if (pid := next(found)) is None else daemon.DaemonAnswer(pid=pid),
        )
        monkeypatch.setattr(daemon, "legacy_listener", lambda db_dir: None)
    else:
        monkeypatch.setattr(daemon, "daemon_pid", lambda db_dir: None)
        monkeypatch.setattr(
            daemon, "legacy_listener",
            lambda db_dir: None if (pid := next(found)) is None else daemon.LegacyListener(pid, _OWN_UID, True),
        )
    monkeypatch.setattr(processes, "request_stop", stop)
    monkeypatch.setattr(processes, "kill_hard", lambda pid: stopped.append(-pid))
    monkeypatch.setattr(background, "_ensure_daemon_running", lambda root: None)
    monkeypatch.setattr(daemon, "ping_daemon", lambda root: True)
    try:
        assert daemon_lock.try_acquire()
        assert cmd_watch_restart_of(tmp_path) == 0
    finally:
        daemon_lock.close()
    assert stopped == [daemon_pid]


def test_the_daemon_answers_with_a_proof_and_never_with_its_token(tmp_path: Path):
    """``ping <nonce>`` gets the HMAC proof of the token and the PID; no answer holds the token.

    Each local process can connect.  The answer held the token, thus a
    different user learned it, answered on the port after the end of the
    daemon, and gave a PID of their choice to ``watch restart``.  An answer
    in the form of before (``pong <token> <pid>``), and a proof for a
    different nonce, are no answer of the daemon.
    """
    import asyncio
    import secrets
    import threading

    from fw_context_mcp.mcp import daemon

    loop = asyncio.new_event_loop()
    ready = threading.Event()
    stop = asyncio.Event()

    async def serve() -> None:
        server, _ = await daemon._setup_ping_server(tmp_path, lambda: None)
        ready.set()
        await stop.wait()
        server.close()
        await server.wait_closed()

    thread = threading.Thread(target=loop.run_until_complete, args=(serve(),))
    thread.start()
    try:
        assert ready.wait(30)
        port, token = daemon._read_port_file(daemon._port_file(tmp_path))
        assert daemon.daemon_pid(tmp_path) == daemon.DaemonAnswer(pid=os.getpid())
        nonce = secrets.token_hex(16)
        for request in (b"ping\n", b"ping pid\n", f"ping {nonce}\n".encode()):
            words = daemon._exchange(port, request)
            assert words is not None
            assert token not in " ".join(words), f"the answer to {request!r} holds the token"
        assert daemon._exchange(port, b"ping\n") == ["pong"]
        assert daemon._exchange(port, f"pingx {nonce}\n".encode()) == ["pong"], "a request that is no ping got a proof"
        proof_for_nonce = daemon._exchange(port, f"ping {nonce}\n".encode())
        assert proof_for_nonce == ["pong", daemon._proof(token, nonce, str(os.getpid())), str(os.getpid())]
    finally:
        loop.call_soon_threadsafe(stop.set)
        thread.join(30)
        # Only a loop that no thread runs can close; an assert here replaced
        # the failure of the test above it.
        if not thread.is_alive():
            loop.close()
    assert not thread.is_alive(), "the ping server did not stop"


@pytest.mark.parametrize("form", ["token-of-before", "proof-of-another-nonce"])
def test_a_forged_answer_is_no_daemon(tmp_path: Path, form: str):
    """A program on the port that knows no secret, or that replays a proof, is no daemon."""
    import asyncio
    import secrets
    import threading

    from fw_context_mcp.mcp import daemon

    token = secrets.token_hex(16)
    replayed = daemon._proof(token, secrets.token_hex(16), "4242")

    async def forger(reader, writer) -> None:
        await reader.readline()
        answer = f"pong {token} 4242\n" if form == "token-of-before" else f"pong {replayed} 4242\n"
        writer.write(answer.encode())
        await writer.drain()
        writer.close()

    loop = asyncio.new_event_loop()
    ready = threading.Event()
    stop = asyncio.Event()

    async def serve() -> None:
        server = await asyncio.start_server(forger, host="127.0.0.1", port=0)
        port = server.sockets[0].getsockname()[1]
        daemon._port_file(tmp_path).write_text(f"{port} {token}\n", encoding="utf-8")
        ready.set()
        await stop.wait()
        server.close()
        await server.wait_closed()

    thread = threading.Thread(target=loop.run_until_complete, args=(serve(),))
    thread.start()
    try:
        assert ready.wait(30)
        assert daemon.daemon_pid(tmp_path) is None
    finally:
        loop.call_soon_threadsafe(stop.set)
        thread.join(30)
        if not thread.is_alive():
            loop.close()
    assert not thread.is_alive(), "the forger did not stop"


#: The user of a fake listener on daemon.sock.  A number, not os.geteuid():
#: Windows has no geteuid, and the rule of _find_daemon (own or not) is the
#: same there.  legacy_listener() itself sets ``own``.
_OWN_UID = 1000

_LEGACY_SERVER = """
import asyncio, os, sys
async def handle(reader, writer):
    try:
        await asyncio.wait_for(reader.read(64), timeout=2.0)
    except Exception:
        pass
    writer.close()
async def main():
    await asyncio.start_unix_server(handle, path=sys.argv[1])
    print(os.getpid(), flush=True)
    await asyncio.sleep(60)
asyncio.run(main())
"""


@pytest.mark.skipif(sys.platform == "win32", reason="a daemon of v0.36.0 listened on a Unix socket, not on Windows")
def test_the_kernel_gives_the_pid_of_a_daemon_of_v0_36(monkeypatch):
    """A daemon of v0.36.0 listens on ``daemon.sock`` and answers no ping (as this server does).

    The restart did not find it: 15 s of wait, an error, and no stop, after
    each upgrade from the release.  The kernel gives the PID of the process
    that listens.  A socket that no process listens on gives none, and a
    listener of a different user too: that path has no token, and the
    restart sends the PID a stop and a kill.  A short directory: the path
    of a Unix socket has at most 104 bytes on macOS.
    """
    import shutil
    import tempfile

    from fw_context_mcp.mcp.daemon import LegacyListener, legacy_listener

    db_dir = Path(tempfile.mkdtemp(prefix="fwctx", dir="/tmp"))
    server = subprocess.Popen(  # noqa: S603 — fixed argv, no shell
        [sys.executable, "-c", _LEGACY_SERVER, str(db_dir / "daemon.sock")],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert server.stdout is not None
        server_pid = int(server.stdout.readline())
        assert legacy_listener(db_dir) == LegacyListener(pid=server_pid, uid=os.geteuid(), own=True)
        server.kill()
        server.wait(timeout=30)
        assert legacy_listener(db_dir) is None, "a socket that no process listens on"
    finally:
        if server.poll() is None:
            server.kill()
            server.wait(timeout=30)
        server.stdout.close()
        shutil.rmtree(db_dir)


@pytest.mark.parametrize("content", ["0", str(2**40), "²", "1²"])
def test_the_holder_of_the_index_lock_outside_the_pid_range_is_unknown(tmp_path: Path, content: str):
    """The takeover sends the holder PID a stop request: a damaged index.lock must give no PID."""
    from fw_context_mcp.indexer.db._locking import _read_holder_pid

    lock_file = tmp_path / "index.lock"
    lock_file.write_text(content, encoding="utf-8")
    assert _read_holder_pid(lock_file) is None


def test_a_daemon_of_an_older_version_is_seen(tmp_path: Path):
    """A daemon of an older version holds the exclusion lock and no status lock.

    The restart only probed the status lock: it saw no daemon after an
    upgrade, and it did not stop a daemon that ran on.
    """
    from fw_context_mcp.file_lock import is_free, try_lock, unlock

    lock_file = tmp_path / "watcher.lock"
    old_daemon = os.open(lock_file, os.O_CREAT | os.O_RDWR)
    try:
        assert try_lock(old_daemon)
        assert not is_free(lock_file)
        unlock(old_daemon)
        assert is_free(lock_file)
    finally:
        os.close(old_daemon)


def test_a_status_lock_that_the_system_did_not_release_yet_is_held(tmp_path: Path):
    """The system can release the two locks of an ended daemon in either order.

    ``_ensure_daemon_running`` probes the status lock: the restart must not
    go on while it is held, or no daemon starts.
    """
    from fw_context_mcp.file_lock import is_free, status_path, try_lock, unlock

    lock_file = tmp_path / "watcher.lock"
    lock_file.touch()
    late_release = os.open(status_path(lock_file), os.O_CREAT | os.O_RDWR)
    try:
        assert try_lock(late_release)
        assert not is_free(lock_file)
        unlock(late_release)
        assert is_free(lock_file)
    finally:
        os.close(late_release)


def test_no_lock_file_is_a_free_lock(tmp_path: Path):
    from fw_context_mcp.file_lock import is_free

    assert is_free(tmp_path / "watcher.lock")
    assert not (tmp_path / "watcher.lock").exists()


def test_the_holder_of_the_index_lock_is_a_process_that_runs(tmp_path: Path):
    """A run that a kill ended cannot clear its PID: the message named a process that was gone."""
    from fw_context_mcp.indexer.db._locking import _read_holder_pid

    ended = subprocess.Popen([sys.executable, "-c", "pass"], stdin=subprocess.DEVNULL)  # noqa: S603 — fixed argv
    ended.wait()
    lock_file = tmp_path / "index.lock"
    lock_file.write_text(str(ended.pid), encoding="utf-8")
    assert _read_holder_pid(lock_file) is None
    lock_file.write_text(str(os.getpid()), encoding="utf-8")
    assert _read_holder_pid(lock_file) == os.getpid()


def test_an_error_of_the_check_of_the_holder_keeps_the_refusal(tmp_path: Path, monkeypatch):
    """``pid_exists`` raises an OSError that is no answer (EINVAL from a sandbox).

    It went out of ``index_run_lock`` in place of IndexRunLocked, and the
    callers catch only IndexRunLocked.
    """
    from fw_context_mcp.indexer.db import _locking

    def no_answer(pid: int) -> bool:
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(_locking, "pid_exists", no_answer)
    lock_file = tmp_path / "index.lock"
    lock_file.write_text(str(os.getpid()), encoding="utf-8")
    assert _locking._read_holder_pid(lock_file) is None


@pytest.mark.parametrize(
    ("version", "shown"),
    [
        ("current", "running (pid 424242)"),
        ("file-pid", "uptime"),
        ("v0.36", "v0.36.0 or earlier"),
    ],
)
def test_the_status_shows_the_pid_that_the_daemon_gives(tmp_path: Path, monkeypatch, capsys, version, shown: str):
    """``watch status`` shows the PID that the daemon gives of itself, not a PID that a dead daemon left.

    The uptime comes from watcher.pid, thus only when the file gives the
    same PID.  A daemon of v0.36.0 or earlier gets a note: the LLM client
    of that version keeps it alive.
    """
    import argparse

    import fw_context_mcp.mcp.background as background
    import fw_context_mcp.mcp.daemon as daemon
    from fw_context_mcp.cli._watch import cmd_watch_status

    db_dir = _project(tmp_path)
    (db_dir / "watcher.pid").write_text(str(os.getpid()), encoding="utf-8")
    pid = os.getpid() if version == "file-pid" else 424242
    if version == "v0.36":
        monkeypatch.setattr(daemon, "daemon_pid", lambda db_dir: None)
        monkeypatch.setattr(daemon, "legacy_listener", lambda db_dir: daemon.LegacyListener(pid, _OWN_UID, True))
    else:
        monkeypatch.setattr(daemon, "daemon_pid", lambda db_dir: daemon.DaemonAnswer(pid=pid))
    # The project has no index: the lines of the index are not the subject here.
    monkeypatch.setattr(background, "read_reindex_state", lambda root: None)
    monkeypatch.setattr(background, "read_reindex_progress", lambda db_dir: None)

    assert cmd_watch_status(argparse.Namespace(project=str(tmp_path))) == 0
    daemon_line = next(line for line in capsys.readouterr().out.splitlines() if line.startswith("Daemon:"))
    assert shown in daemon_line
    assert f"pid {pid}" in daemon_line


def test_a_listener_of_a_different_user_on_daemon_sock_gets_no_stop(tmp_path: Path, monkeypatch, capsys):
    """A process of a different user listens on ``daemon.sock``: no daemon of this user, and no stop.

    That path has no token.  When the index directory is in a directory that
    a different user can write, that user can listen on ``daemon.sock``
    after the daemon ended.  The restart names that process, and the status
    does not call it a running daemon.
    """
    import argparse

    import fw_context_mcp.mcp.background as background
    import fw_context_mcp.mcp.daemon as daemon
    from fw_context_mcp.cli._watch import cmd_watch_status

    other = daemon.LegacyListener(pid=424244, uid=_OWN_UID + 1, own=False)
    monkeypatch.setattr(daemon, "legacy_listener", lambda db_dir: other)
    assert _restart_with_a_lock_holder(tmp_path, monkeypatch, str(os.getpid()), None) == []
    err = capsys.readouterr().err
    assert f"process 424244 of user {other.uid} listens on daemon.sock" in err

    monkeypatch.setattr(background, "read_reindex_state", lambda root: None)
    monkeypatch.setattr(background, "read_reindex_progress", lambda db_dir: None)
    assert cmd_watch_status(argparse.Namespace(project=str(tmp_path))) == 0
    daemon_line = next(line for line in capsys.readouterr().out.splitlines() if line.startswith("Daemon:"))
    assert daemon_line.startswith("Daemon:     not running (process 424244")


class _FakeSocket:
    """The ``getsockopt`` of a Unix socket, with the bytes of each option that the kernel gives."""

    def __init__(self, options: dict[tuple[int, int], bytes]) -> None:
        self._options = options

    def getsockopt(self, level: int, option: int, size: int) -> bytes:
        return self._options[(level, option)][:size]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="struct ucred of SO_PEERCRED")
def test_a_user_above_2_31_stays_positive_on_linux():
    """uid_t is unsigned: a user of 2**31 or more (large LDAP or user-namespace ranges) read as negative.

    The compare with the user of this process then never matched, and the
    restart did not find the own daemon of v0.36.0.
    """
    import socket
    import struct

    from fw_context_mcp.mcp.daemon import _peer_pid_and_uid

    fake = _FakeSocket({(socket.SOL_SOCKET, socket.SO_PEERCRED): struct.pack("iII", 4242, 2**31 + 5, 7)})
    assert _peer_pid_and_uid(fake) == (4242, 2**31 + 5)


@pytest.mark.skipif(sys.platform != "darwin", reason="struct xucred of LOCAL_PEERCRED")
@pytest.mark.parametrize(("version", "expected"), [(0, (4242, 2**31 + 5)), (1, None)], ids=["version-0", "version-1"])
def test_the_xucred_of_macos(version: int, expected):
    """An xucred of an unknown version can have a different layout: no user from a wrong offset."""
    import struct

    from fw_context_mcp.mcp import daemon

    xucred = struct.pack("II", version, 2**31 + 5).ljust(daemon._XUCRED_SIZE, b"\0")
    fake = _FakeSocket({
        (daemon._SOL_LOCAL, daemon._LOCAL_PEERPID): struct.pack("i", 4242),
        (daemon._SOL_LOCAL, daemon._LOCAL_PEERCRED): xucred,
    })
    assert daemon._peer_pid_and_uid(fake) == expected


@pytest.mark.skipif(sys.platform == "win32", reason="a daemon of v0.36.0 listened on a Unix socket, not on Windows")
def test_a_short_answer_of_the_kernel_is_no_listener(monkeypatch):
    """A buffer shorter than the structure raised struct.error, and status and restart stopped with a traceback."""
    import shutil
    import struct
    import tempfile

    from fw_context_mcp.mcp import daemon

    def short(sock: object) -> tuple[int, int]:
        raise struct.error("unpack requires a buffer of 12 bytes")

    db_dir = Path(tempfile.mkdtemp(prefix="fwctx", dir="/tmp"))
    server = subprocess.Popen(  # noqa: S603 — fixed argv, no shell
        [sys.executable, "-c", _LEGACY_SERVER, str(db_dir / "daemon.sock")],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert server.stdout is not None
        server.stdout.readline()
        monkeypatch.setattr(daemon, "_peer_pid_and_uid", short)
        assert daemon.legacy_listener(db_dir) is None
    finally:
        server.kill()
        server.wait(timeout=30)
        server.stdout.close()
        shutil.rmtree(db_dir)


def test_a_request_in_two_parts_gets_the_answer_with_the_pid():
    """TCP can give ``ping pid\\n`` in two parts: the first part alone got the answer with no PID."""
    import asyncio

    from fw_context_mcp.mcp.daemon import _read_request

    async def read() -> bytes:
        reader = asyncio.StreamReader()
        reader.feed_data(b"ping")

        def second_part() -> None:
            reader.feed_data(b" pid\n")

        asyncio.get_running_loop().call_later(0.05, second_part)
        return await asyncio.wait_for(_read_request(reader), timeout=5.0)

    assert asyncio.run(read()) == b"ping pid\n"


def test_the_restart_waits_longer_than_the_daemon_waits_for_its_run():
    """The daemon gives its index run ``RUN_STOP_WAIT_S`` after the stop request, and then removes its markers.

    The restart waited 3 s and then killed the daemon: the run, its
    ``reindex.pid`` and its ``reindex.pause`` stayed.  A stale pause marker
    then holds each later background run.
    """
    from fw_context_mcp.cli import _watch
    from fw_context_mcp.mcp import daemon

    assert _watch._stop_wait_s() > daemon.RUN_STOP_WAIT_S


def test_the_restart_keeps_the_files_of_a_daemon_that_started_after_the_check(tmp_path: Path, monkeypatch):
    """An MCP server can start a daemon in each moment after the restart found the lock free.

    The restart then removed ``daemon.port`` and ``watcher.pid`` of that new
    daemon: no MCP server found its port, no ping came, and the daemon
    exited after ``PING_TIMEOUT``.  A file of a daemon that is gone needs
    no removal: the port file fails the proof of the ping, and the restart
    never stops the PID of ``watcher.pid``.  A new daemon writes the two
    files again.
    """
    import fw_context_mcp.cli._watch as watch
    import fw_context_mcp.mcp.background as background
    import fw_context_mcp.mcp.daemon as daemon

    db_dir = _project(tmp_path)
    files = {db_dir / "watcher.pid": "4242", db_dir / daemon.DAEMON_PORT_NAME: f"50000 {'a' * 32}\n"}

    def a_daemon_starts_after_the_check(lock_file: Path, pid_file: Path, db_dir: Path) -> None:
        for path, text in files.items():
            path.write_text(text, encoding="utf-8")
        return None

    monkeypatch.setattr(watch, "_running_daemon", a_daemon_starts_after_the_check)
    monkeypatch.setattr(background, "_ensure_daemon_running", lambda root: None)
    monkeypatch.setattr(daemon, "ping_daemon", lambda root: True)

    assert cmd_watch_restart_of(tmp_path) == 0
    for path, text in files.items():
        assert path.read_text(encoding="utf-8") == text, f"the restart removed {path.name} of the new daemon"
