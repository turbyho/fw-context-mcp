"""The liveness probe for the owner of a temporary file.

The probe is ``processes.pid_exists``.  On POSIX, ``os.kill(pid, 0)`` sends
no signal and only tests the PID.  On Windows, signal 0 is ``CTRL_C_EVENT``:
the call sends Ctrl+C to the process group of *pid*, thus the probe there
asks the process handle for its exit code.  The tests of the real probe run
on each system.
"""

from __future__ import annotations

import os

import pytest

from fw_context_mcp import utils
from fw_context_mcp.processes import MAX_PID


def _token(pid: int) -> str:
    return f"{pid}@{utils.owner_token().partition('@')[2]}"


class TestTheDecision:
    @pytest.fixture
    def answers(self, monkeypatch):
        answers: dict[int, bool] = {}
        monkeypatch.setattr(utils, "pid_exists", lambda pid: answers[pid])
        return answers

    def test_a_stopped_process(self, answers):
        answers[4242] = False
        assert utils.owner_is_dead(_token(4242)) is True

    def test_a_running_process(self, answers):
        answers[4242] = True
        assert utils.owner_is_dead(_token(4242)) is False

    def test_another_namespace_is_never_probed(self, answers):
        assert utils.owner_is_dead("4242@another-host-1") is False

    @pytest.mark.parametrize("pid", [0, -1, MAX_PID + 1, 2**40])
    def test_a_pid_outside_the_range_keeps_the_file(self, answers, pid):
        assert utils.owner_is_dead(_token(pid)) is False

    def test_a_probe_without_an_answer_keeps_the_file(self, monkeypatch):
        """An OSError that is neither ESRCH nor EPERM says nothing about the owner."""
        def no_answer(pid):
            raise OSError(22, "Invalid argument")

        monkeypatch.setattr(utils, "pid_exists", no_answer)
        assert utils.owner_is_dead(_token(4242)) is False


class TestTheRealProbe:
    def test_this_process_runs(self):
        assert utils.owner_is_dead(_token(os.getpid())) is False

    def test_a_pid_that_no_process_has(self):
        assert utils.owner_is_dead(_token(2147483646)) is True
