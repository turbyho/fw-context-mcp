"""Tests for PidFile — the cross-process coordination marker.

Focus on ``is_active_other``: the distinction between "someone is paused"
and "someone ELSE is paused".  Conflating the two disabled build retention
entirely, because ``fw-context index`` holds its own ``reindex.pause`` for
the whole run.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from fw_context_mcp.mcp.shared.pid_file import PidFile


@pytest.fixture
def live_other_pid():
    """Yield the PID of a live process we are allowed to signal.

    A child process is used rather than PID 1: ``_pid_exists`` treats any
    OSError as "gone", so ``os.kill`` on a process owned by another user
    raises EPERM and reads as dead.
    """
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        yield proc.pid
    finally:
        proc.kill()
        proc.wait(timeout=10)


class TestIsActiveOther:
    def test_missing_file_is_not_active(self, tmp_path):
        assert PidFile.is_active_other(tmp_path / "absent.pause") is False

    def test_our_own_marker_is_not_other(self, tmp_path):
        """The guard must not fire on the marker this process wrote.

        This is the retention defect in one assertion: the indexer writes
        reindex.pause with its own PID, so a guard on is_active() was always
        true inside the run it was meant to allow.
        """
        path = tmp_path / "reindex.pause"
        PidFile(path).write()
        assert PidFile.is_active(path) is True, "own marker should be live"
        assert PidFile.is_active_other(path) is False

    def test_live_other_process_is_other(self, tmp_path, live_other_pid):
        path = tmp_path / "reindex.pause"
        path.write_text(str(live_other_pid), encoding="utf-8")
        assert PidFile.is_active_other(path) is True

    def test_dead_pid_is_not_other_and_is_cleaned_up(self, tmp_path):
        """A stale marker must not keep the guard armed forever."""
        proc = subprocess.Popen([sys.executable, "-c", ""])
        proc.wait(timeout=10)
        dead_pid = proc.pid

        path = tmp_path / "reindex.pause"
        path.write_text(str(dead_pid), encoding="utf-8")
        assert PidFile.is_active_other(path) is False
        assert not path.exists(), "is_active should remove a stale marker"

    def test_corrupt_content_is_not_other(self, tmp_path):
        path = tmp_path / "reindex.pause"
        path.write_text("not-a-pid", encoding="utf-8")
        assert PidFile.is_active_other(path) is False

    def test_unlink_if_ours_leaves_another_process_marker(self, tmp_path, live_other_pid):
        """Ownership check on removal — unchanged behaviour, guarded here."""
        path = tmp_path / "reindex.pause"
        path.write_text(str(live_other_pid), encoding="utf-8")
        PidFile(path, pid=os.getpid()).unlink_if_ours()
        assert path.exists(), "another process's marker must survive our cleanup"


class TestPidLiveness:
    """``os.kill(pid, 0)`` has three outcomes and they mean different things.

    EPERM says the process EXISTS but belongs to someone else.  Reporting that
    as dead is destructive rather than merely wrong, because
    :meth:`PidFile.is_active` deletes the marker of a process it believes is
    gone — resuming a background reindex that was deliberately held, or
    letting retention delete a build another process is still writing.
    """

    def test_eperm_means_the_process_exists(self, monkeypatch):
        def _raise_eperm(pid, sig):
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr(os, "kill", _raise_eperm)
        assert PidFile._pid_exists(4242) is True

    def test_esrch_means_the_process_is_gone(self, monkeypatch):
        def _raise_esrch(pid, sig):
            raise ProcessLookupError(3, "No such process")

        monkeypatch.setattr(os, "kill", _raise_esrch)
        assert PidFile._pid_exists(4242) is False

    def test_another_users_marker_survives_a_liveness_check(self, tmp_path, monkeypatch):
        """The consequence, stated as the defect it was.

        A liveness check is a read.  It must not delete a live process's
        marker just because that process is not ours.
        """
        def _raise_eperm(pid, sig):
            raise PermissionError(1, "Operation not permitted")

        monkeypatch.setattr(os, "kill", _raise_eperm)
        path = tmp_path / "reindex.pause"
        path.write_text("4242", encoding="utf-8")

        assert PidFile.is_active(path) is True
        assert path.exists(), "a live process's marker was deleted"
        assert PidFile.is_active_other(path) is True

    def test_pid_one_is_reported_alive(self, tmp_path):
        """Integration touch, without mocking.

        pid 1 always exists.  As an ordinary user os.kill raises EPERM; as
        root it succeeds — either way the answer must be "alive".
        """
        assert PidFile._pid_exists(1) is True
