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

from fw_context_mcp.cli._watch import _wait_for_free_lock

_HOLD = """
import os, sys, time
from fw_context_mcp.file_lock import lock
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT)
lock(fd)
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
        assert not _wait_for_free_lock(lock_file, 0.3), "the lock of a running process is held"
        proc.kill()  # no wait: nobody reaps the child yet
        assert _wait_for_free_lock(lock_file, 30.0)
    finally:
        proc.kill()
        proc.wait()
        proc.stdout.close()


def test_a_free_lock_needs_no_wait(tmp_path: Path):
    lock_file = tmp_path / "watcher.lock"
    lock_file.touch()

    assert _wait_for_free_lock(lock_file, 0.0)


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
