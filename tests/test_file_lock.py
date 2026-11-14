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
from fw_context_mcp.file_lock import ProbedLock, held_by_another, lock, try_lock, unlock

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
    holder = ProbedLock(lock_path)
    try:
        assert not held_by_another(lock_path)
        assert holder.try_acquire()
        assert held_by_another(lock_path)
        holder.release()
        assert not held_by_another(lock_path)
        # The probe released its own try: the lock is free for a holder.
        assert holder.try_acquire()
    finally:
        holder.close()
    assert not held_by_another(lock_path)


def test_a_second_holder_does_not_get_a_probed_lock(lock_path: Path) -> None:
    first, second = ProbedLock(lock_path), ProbedLock(lock_path)
    try:
        assert first.try_acquire()
        assert not second.try_acquire()
        assert not second.held
        first.release()
        assert second.try_acquire()
    finally:
        first.close()
        second.close()


def test_the_probe_never_takes_the_exclusion_lock(lock_path: Path, monkeypatch) -> None:
    """A holder that tries the lock during a probe must get it.

    The probe held the lock for a short time.  ``index_run_lock`` and the
    daemon try the lock once, with no wait, thus ``fw-context index`` failed
    with "the index is in use" and a new daemon exited with "Daemon already
    running" because of a probe.  The fake ``unlock`` runs while the probe
    holds what it took, and makes the first step of a holder there.
    """
    import fw_context_mcp.file_lock as file_lock

    lock_path.touch()
    file_lock.status_path(lock_path).touch()
    free_during_probe: list[bool] = []
    real_unlock = file_lock.unlock

    def unlock_after_a_try(fd: int) -> None:
        other = os.open(lock_path, os.O_RDWR)
        try:
            got = try_lock(other)
            free_during_probe.append(got)
            if got:
                real_unlock(other)
        finally:
            os.close(other)
        real_unlock(fd)

    monkeypatch.setattr(file_lock, "unlock", unlock_after_a_try)
    assert not held_by_another(lock_path)
    assert free_during_probe == [True]


def test_a_holder_waits_for_a_probe_and_gets_the_lock(lock_path: Path) -> None:
    """A probe holds the status lock for a short time: the holder waits for it, and does not fail."""
    from fw_context_mcp.file_lock import status_path

    probe = os.open(status_path(lock_path), os.O_CREAT | os.O_RDWR)
    holder = ProbedLock(lock_path)
    try:
        assert try_lock(probe)
        result: list[bool] = []
        thread = threading.Thread(target=lambda: result.append(holder.try_acquire()))
        thread.start()
        thread.join(0.3)
        assert thread.is_alive(), "the holder must wait for the probe"
        unlock(probe)
        thread.join(RELEASE_WAIT_S)
        assert result == [True]
        assert held_by_another(lock_path)
    finally:
        os.close(probe)
        holder.close()


def test_a_stop_during_the_wait_for_the_status_lock_frees_the_exclusion_lock(lock_path: Path, monkeypatch) -> None:
    """An exclusion lock without its status lock blocks each holder, and no probe sees it."""
    import fw_context_mcp.file_lock as file_lock

    def stopped(fd: int, limit: float | None, pause: float) -> bool:
        raise KeyboardInterrupt

    holder = ProbedLock(lock_path)
    try:
        monkeypatch.setattr(file_lock, "_poll_lock", stopped)
        with pytest.raises(KeyboardInterrupt):
            holder.try_acquire()
        monkeypatch.undo()
        assert not holder.held
        assert holder.try_acquire()
    finally:
        holder.close()


def test_a_status_lock_that_stays_held_ends_the_try_with_false(lock_path: Path, monkeypatch) -> None:
    """A probe that is stopped while it holds the status lock (Ctrl-Z, a debugger).

    The holder waited for the status lock with no end, and it held the
    exclusion lock all that time: each other holder failed too, and the time
    limit of ``write_lock`` did not apply.  Now the try ends with False, and
    the exclusion lock is free again.
    """
    import fw_context_mcp.file_lock as file_lock

    monkeypatch.setattr(file_lock, "STATUS_WAIT_S", 0.3)
    stuck_probe = os.open(file_lock.status_path(lock_path), os.O_CREAT | os.O_RDWR)
    holder = ProbedLock(lock_path)
    other = os.open(lock_path, os.O_RDWR)
    try:
        assert try_lock(stuck_probe)
        started = time.monotonic()
        assert holder.try_acquire() is False
        assert time.monotonic() - started < RELEASE_WAIT_S
        assert not holder.held
        assert try_lock(other), "the exclusion lock stayed held after the False"
        unlock(other)
        unlock(stuck_probe)
        assert holder.try_acquire()
    finally:
        os.close(other)
        os.close(stuck_probe)
        holder.close()


def test_a_probed_lock_refuses_a_second_acquire_and_a_release_without_a_hold(lock_path: Path) -> None:
    holder = ProbedLock(lock_path)
    try:
        with pytest.raises(RuntimeError, match="not held"):
            holder.release()
        assert holder.try_acquire()
        with pytest.raises(RuntimeError, match="already held"):
            holder.try_acquire()
    finally:
        holder.close()
    assert not held_by_another(lock_path)


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


def test_a_probed_lock_of_a_killed_process_is_free(lock_path: Path) -> None:
    """A probe sees the lock of a different process, and the two locks go when the system stops it."""
    script = textwrap.dedent(f"""
        import sys
        from pathlib import Path
        import fw_context_mcp
        from fw_context_mcp.file_lock import ProbedLock
        held = ProbedLock(Path({str(lock_path)!r}))
        assert held.try_acquire()
        print("locked", flush=True)
        sys.stdin.readline()
    """)
    holder = subprocess.Popen(
        [sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    other = ProbedLock(lock_path)
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"
        assert held_by_another(lock_path)
        assert not other.try_acquire()
        holder.kill()
        holder.wait(timeout=30)
        deadline = time.monotonic() + RELEASE_WAIT_S
        while held_by_another(lock_path) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not held_by_another(lock_path)
        # Windows can release the two locks of the killed process at two times.
        while not other.try_acquire():
            assert time.monotonic() < deadline, "the exclusion lock of the killed process stays held"
            time.sleep(0.05)
    finally:
        other.close()
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=30)


def test_a_longer_status_wait_of_the_caller_stays_at_the_limit(lock_path: Path, monkeypatch) -> None:
    """``write_lock`` gives the rest of its time limit (up to 120 s) as the wait for the status lock.

    The exclusion lock is held during that wait, thus each other writer
    failed for all of it.  The wait stays at STATUS_WAIT_S.
    """
    import fw_context_mcp.file_lock as file_lock

    monkeypatch.setattr(file_lock, "STATUS_WAIT_S", 0.3)
    stuck_probe = os.open(file_lock.status_path(lock_path), os.O_CREAT | os.O_RDWR)
    holder = ProbedLock(lock_path)
    try:
        assert try_lock(stuck_probe)
        started = time.monotonic()
        assert holder.try_acquire(status_wait=120.0) is False
        assert time.monotonic() - started < RELEASE_WAIT_S
        unlock(stuck_probe)
    finally:
        os.close(stuck_probe)
        holder.close()
