"""Tests of fw_context_mcp.processes on the system that runs them.

A stop request must run the SIGTERM handler of the target process on each
system: SIGTERM on POSIX, the stop event and ``interrupt_main`` on Windows.
The handler of the child process exits with a code of its own, thus the
code shows that the handler ran and that no hard stop ended the process.

Two ways to name the target, as fw-context uses them:

- The PID that the process wrote itself (the index lock): the child prints
  ``os.getpid()``.
- The PID of ``Popen`` and a stop token (the daemon and its index run): on
  Windows the ``python.exe`` of a virtual environment is a launcher, and
  the PID of ``Popen`` is not the PID of the interpreter.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

import fw_context_mcp  # noqa: F401 — the sqlite3 redirect runs first
from fw_context_mcp.indexer import _index_owner
from fw_context_mcp.processes import STOP_TOKEN_ENV, kill_hard, new_stop_token, pid_exists, request_stop

HANDLER_EXIT = 42

CHILD = textwrap.dedent(f"""
    import signal, sys, time
    from fw_context_mcp.processes import install_stop_listener

    def handler(signum, frame):
        sys.exit({HANDLER_EXIT})

    signal.signal(signal.SIGTERM, handler)
    install_stop_listener()
    import os
    assert "{STOP_TOKEN_ENV}" not in os.environ, "the listener must remove the token"
    print("READY", os.getpid(), flush=True)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        sum(range(1000))
    sys.exit(0)
""")


class Child:
    """A child process with its ``Popen``, its own PID and its stop token."""

    def __init__(self) -> None:
        self.token = new_stop_token()
        self.proc = subprocess.Popen(  # noqa: S603 — fixed argv, no shell
            [sys.executable, "-c", CHILD, "index", "--background", "an argument"],
            stdout=subprocess.PIPE, text=True, encoding="utf-8",
            env={**os.environ, STOP_TOKEN_ENV: self.token},
        )
        assert self.proc.stdout is not None
        word, pid = self.proc.stdout.readline().split()
        assert word == "READY"
        self.own_pid = int(pid)
        self.pid = self.proc.pid

    def wait(self, timeout: float) -> int:
        return self.proc.wait(timeout=timeout)


@pytest.fixture
def child():
    proc = Child()
    try:
        yield proc
    finally:
        if proc.proc.poll() is None:
            proc.proc.kill()
            proc.proc.wait(timeout=30)


def test_a_stop_request_to_the_own_pid_runs_the_sigterm_handler(child) -> None:
    assert request_stop(child.own_pid) is True
    assert child.wait(timeout=30) == HANDLER_EXIT


def test_a_stop_request_with_the_token_runs_the_sigterm_handler(child) -> None:
    assert request_stop(child.pid, token=child.token) is True
    assert child.wait(timeout=30) == HANDLER_EXIT


def test_kill_hard_ends_the_process_without_the_handler(child) -> None:
    kill_hard(child.pid)
    code = child.wait(timeout=30)
    assert code != HANDLER_EXIT
    assert code != 0


def test_pid_exists(child) -> None:
    assert pid_exists(child.pid)
    assert pid_exists(os.getpid())
    kill_hard(child.pid)
    child.wait(timeout=30)
    assert not pid_exists(child.pid)


@pytest.mark.skipif(os.name != "nt", reason="the System process of Windows")
def test_a_process_that_this_user_cannot_open_runs() -> None:
    """PID 4 is the System process: OpenProcess gives access denied, and the process runs.

    A process of another user is alive.  Reporting it as dead made
    ``PidFile.is_active`` delete its marker.
    """
    assert pid_exists(4) is True


@pytest.mark.parametrize("pid", [0, -1, 2**40])
def test_pid_exists_refuses_a_pid_outside_the_range(pid: int) -> None:
    """A PID of 0 or less is a process group for kill(): no question about one process."""
    assert pid_exists(pid) is False


def test_a_request_to_a_process_that_is_gone_is_false(child) -> None:
    kill_hard(child.pid)
    child.wait(timeout=30)
    assert request_stop(child.pid) is False


def test_the_argv_of_another_process(child) -> None:
    """``_index_owner.process_argv`` reads /proc, ps or the Windows command line."""
    argv = _index_owner.process_argv(child.own_pid)
    assert argv is not None
    assert "index" in argv
    assert "--background" in argv
    if sys.platform != "darwin":
        # ps joins argv with spaces, thus macOS splits an argument with a space.
        assert argv[-1] == "an argument"


@pytest.mark.parametrize(("arg", "name"), [
    ("C:\\venv\\Scripts\\fw-context.exe", "C:\\venv\\Scripts\\fw-context"),
    ("C:\\venv\\Scripts\\FW-CONTEXT.EXE", "C:\\venv\\Scripts\\FW-CONTEXT"),
    ("/usr/local/bin/fw-context", "/usr/local/bin/fw-context"),
])
def test_the_program_name_has_no_exe(arg: str, name: str) -> None:
    assert _index_owner._program_name(arg) == name


def test_an_index_run_of_the_windows_launcher_is_recognised(monkeypatch) -> None:
    """pip makes Scripts\\fw-context.exe on Windows; its argv must read as an index run."""
    monkeypatch.setattr(
        _index_owner, "process_argv",
        lambda pid: ["C:\\venv\\Scripts\\fw-context.exe", "index", "--background"],
    )
    assert _index_owner.index_run_kind(1234) == "background"


SECOND_EXIT = 43

IGNORE_ONE = textwrap.dedent(f"""
    import os, signal, sys, time
    from fw_context_mcp.processes import install_stop_listener

    seen = []

    def handler(signum, frame):
        seen.append(signum)
        if len(seen) == 1:
            print("IGNORED", flush=True)
            return
        sys.exit({SECOND_EXIT})

    signal.signal(signal.SIGTERM, handler)
    install_stop_listener()
    print("READY", os.getpid(), flush=True)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        sum(range(1000))
    sys.exit(0)
""")


def test_a_second_request_reaches_the_handler_after_one_that_it_ignored() -> None:
    """A handler can ignore a request (a late takeover); the next one must still run it.

    On POSIX each SIGTERM runs the handler.  The Windows listener stopped
    after its first event, and each later request was lost.
    """
    proc = subprocess.Popen(  # noqa: S603 — fixed argv, no shell
        [sys.executable, "-c", IGNORE_ONE], stdout=subprocess.PIPE, text=True, encoding="utf-8",
    )
    try:
        assert proc.stdout is not None
        word, pid = proc.stdout.readline().split()
        assert word == "READY"
        assert request_stop(int(pid)) is True
        assert proc.stdout.readline().strip() == "IGNORED"
        assert request_stop(int(pid)) is True
        assert proc.wait(timeout=30) == SECOND_EXIT
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=30)
