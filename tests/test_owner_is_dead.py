"""The liveness probe for the owner of a temporary file.

On POSIX, `os.kill(pid, 0)` sends no signal and only tests the PID.  On
Windows, signal 0 is `CTRL_C_EVENT`: the call sends Ctrl+C to the process
group of *pid*, and its result says nothing about the process.  The probe
there asks the process handle for its exit code.
"""

from __future__ import annotations

import os

import pytest

from fw_context_mcp import utils


def _token(pid: int) -> str:
    return f"{pid}@{utils.owner_token().partition('@')[2]}"


class TestWindows:
    @pytest.fixture
    def windows(self, monkeypatch):
        def no_signal(*_args):
            raise AssertionError("os.kill sends CTRL_C_EVENT on Windows")

        monkeypatch.setattr(utils, "_ON_WINDOWS", True)
        monkeypatch.setattr(utils.os, "kill", no_signal)
        answers: dict[int, bool] = {}
        monkeypatch.setattr(utils, "_windows_process_is_gone", lambda pid: answers[pid])
        return answers

    def test_a_stopped_process(self, windows):
        windows[4242] = True
        assert utils.owner_is_dead(_token(4242)) is True

    def test_a_running_process(self, windows):
        windows[4242] = False
        assert utils.owner_is_dead(_token(4242)) is False

    def test_another_namespace_is_never_probed(self, windows):
        assert utils.owner_is_dead("4242@another-host-1") is False


class TestPosix:
    @pytest.mark.skipif(os.name == "nt", reason="POSIX probe")
    def test_this_process_runs(self):
        assert utils.owner_is_dead(_token(os.getpid())) is False

    @pytest.mark.skipif(os.name == "nt", reason="POSIX probe")
    def test_a_pid_that_no_process_has(self):
        assert utils.owner_is_dead(_token(2147483646)) is True
