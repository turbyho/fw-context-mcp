"""Tests of fw_context_mcp.file_lock on the platform that runs them.

The same tests run on Linux, macOS and Windows.  They give the behavior that
the callers (the index lock, the write lock, the watcher lock) need from the
lock, not the system call that gives it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

import fw_context_mcp  # noqa: F401 — the sqlite3 redirect runs first
from fw_context_mcp.file_lock import held_by_another, lock, try_lock, unlock

# Windows releases the lock of a closed handle or of a process that ended
# some time later ("depends upon available system resources").  A test that
# asks for the lock after such a release waits up to this limit.
RELEASE_WAIT_S = 10.0


def _gets_lock_within(fd: int, limit: float) -> bool:
    deadline = time.monotonic() + limit
    while not try_lock(fd):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


@pytest.fixture
def lock_path(tmp_path: Path) -> Path:
    return tmp_path / "test.lock"


@pytest.fixture
def two_fds(lock_path: Path) -> Iterator[tuple[int, int]]:
    first = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    second = os.open(lock_path, os.O_RDWR)
    try:
        yield first, second
    finally:
        os.close(first)
        os.close(second)


def test_second_fd_of_this_process_does_not_get_the_lock(two_fds: tuple[int, int]) -> None:
    first, second = two_fds
    assert try_lock(first)
    assert not try_lock(second)


def test_unlock_frees_the_lock(two_fds: tuple[int, int]) -> None:
    first, second = two_fds
    assert try_lock(first)
    unlock(first)
    assert try_lock(second)
    unlock(second)


def test_close_frees_the_lock(lock_path: Path) -> None:
    first = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    assert try_lock(first)
    os.close(first)
    second = os.open(lock_path, os.O_RDWR)
    try:
        assert _gets_lock_within(second, RELEASE_WAIT_S)
    finally:
        os.close(second)


def test_blocking_lock_gets_a_free_lock(two_fds: tuple[int, int]) -> None:
    first, second = two_fds
    lock(first)
    assert not try_lock(second)
    unlock(first)
    assert try_lock(second)


def test_blocking_lock_waits_for_the_release(two_fds: tuple[int, int]) -> None:
    """``lock`` waits while a different file descriptor holds the lock, and gets it after the release."""
    first, second = two_fds
    assert try_lock(first)
    got_it = threading.Event()

    def wait_for_lock() -> None:
        lock(second)
        got_it.set()

    waiter = threading.Thread(target=wait_for_lock, daemon=True)
    waiter.start()
    assert not got_it.wait(0.5)
    unlock(first)
    assert got_it.wait(RELEASE_WAIT_S)
    waiter.join(RELEASE_WAIT_S)
    assert not try_lock(first)
    unlock(second)


def test_held_by_another(lock_path: Path) -> None:
    assert not held_by_another(lock_path)
    assert not lock_path.exists()
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        assert not held_by_another(lock_path)
        assert try_lock(fd)
        assert held_by_another(lock_path)
        unlock(fd)
        assert not held_by_another(lock_path)
        # The probe released its own try: the lock is free for a holder.
        assert try_lock(fd)
    finally:
        os.close(fd)


def test_content_of_a_locked_file_stays_readable(lock_path: Path) -> None:
    """The index lock file holds the PID of its holder, and the next caller reads it.

    On Windows a lock blocks the read of the bytes that it locks.  The lock
    must thus not lock the data of the file.
    """
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    try:
        assert try_lock(fd)
        os.write(fd, b"4242")
        assert lock_path.read_text(encoding="utf-8") == "4242"
    finally:
        os.close(fd)


def _holder_script(lock_path: Path) -> str:
    return textwrap.dedent(f"""
        import os, sys
        import fw_context_mcp
        from fw_context_mcp.file_lock import try_lock
        fd = os.open({str(lock_path)!r}, os.O_CREAT | os.O_RDWR)
        assert try_lock(fd)
        print("locked", flush=True)
        sys.stdin.readline()
    """)


def test_lock_of_a_different_process_blocks_and_goes_when_it_is_killed(lock_path: Path) -> None:
    """A process that the system stops (SIGKILL, TerminateProcess) releases its lock."""
    holder = subprocess.Popen(
        [sys.executable, "-c", _holder_script(lock_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"
        fd = os.open(lock_path, os.O_RDWR)
        try:
            assert not try_lock(fd)
            holder.kill()
            holder.wait(timeout=30)
            assert _gets_lock_within(fd, RELEASE_WAIT_S)
        finally:
            os.close(fd)
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=30)
