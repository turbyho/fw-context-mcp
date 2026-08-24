"""Two indexing runs must not share an index directory.

``write_lock`` is taken and released per translation unit so a manual
operation can interleave, which means it does nothing to stop a second run
from starting between two of the first run's units.  Two indexers on one
database corrupt each other's bookkeeping: each captured its own file
snapshot and header ownership at start, and each deletes rows the other just
wrote.

The guard that was supposed to prevent this compared ``/proc/<pid>/comm``
against ``("fw-context", "python", "python3")``.  A virtualenv interpreter is
named for its version — ``python3.14`` here — so it never matched and never
excluded anything.  Observed: a run started at 16:42 was still going when a
second one started at 17:04.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from fw_context_mcp.indexer.db._locking import (
    index_run_lock,
)
from fw_context_mcp.indexer.runner import EXIT_ALREADY_RUNNING, EXIT_SUPERSEDED


class TestIndexRunLock:
    def test_it_can_be_taken_when_free(self, tmp_path: Path):
        with index_run_lock(tmp_path):
            assert (tmp_path / "index.lock").exists()

    def test_it_is_released_again(self, tmp_path: Path):
        with index_run_lock(tmp_path):
            pass
        with index_run_lock(tmp_path):
            pass

    def test_it_creates_the_directory(self, tmp_path: Path):
        target = tmp_path / "not-yet"
        with index_run_lock(target):
            assert target.is_dir()

    def test_a_second_process_is_refused(self, tmp_path: Path):
        """The exclusion has to hold ACROSS processes, which is the point.

        flock is per open file description, so a same-process re-entry does
        not prove anything — a real second process does.
        """
        script = textwrap.dedent(
            f"""
            import sys
            from pathlib import Path
            sys.path.insert(0, {str(Path(__file__).resolve().parents[1] / "src")!r})
            from fw_context_mcp.indexer.db._locking import IndexRunLocked, index_run_lock
            try:
                with index_run_lock(Path({str(tmp_path)!r})):
                    print("ACQUIRED")
            except IndexRunLocked as exc:
                print(f"REFUSED {{exc}}")
            """
        )
        with index_run_lock(tmp_path):
            result = subprocess.run(  # noqa: S603 — fixed argv, no shell
                [sys.executable, "-c", script],
                capture_output=True, text=True, timeout=60,
            )
        assert "REFUSED" in result.stdout, result.stdout + result.stderr
        assert str(os.getpid()) in result.stdout, (
            "the refusal should name the holder so the user can find it"
        )

    def test_a_second_process_succeeds_once_released(self, tmp_path: Path):
        script = textwrap.dedent(
            f"""
            import sys
            from pathlib import Path
            sys.path.insert(0, {str(Path(__file__).resolve().parents[1] / "src")!r})
            from fw_context_mcp.indexer.db._locking import index_run_lock
            with index_run_lock(Path({str(tmp_path)!r})):
                print("ACQUIRED")
            """
        )
        with index_run_lock(tmp_path):
            pass
        result = subprocess.run(  # noqa: S603 — fixed argv, no shell
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=60,
        )
        assert "ACQUIRED" in result.stdout, result.stdout + result.stderr

    def test_a_crashed_holder_does_not_wedge_the_index(self, tmp_path: Path):
        """The kernel drops the lock however the process exits.

        This is why flock replaced the PID file: a PID file survives a crash
        and leaves the index permanently refusing new runs.
        """
        script = textwrap.dedent(
            f"""
            import os, sys
            from pathlib import Path
            sys.path.insert(0, {str(Path(__file__).resolve().parents[1] / "src")!r})
            from fw_context_mcp.indexer.db._locking import index_run_lock
            with index_run_lock(Path({str(tmp_path)!r})):
                print("ACQUIRED", flush=True)
                os._exit(9)   # die hard, no cleanup
            """
        )
        result = subprocess.run(  # noqa: S603 — fixed argv, no shell
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=60,
        )
        assert "ACQUIRED" in result.stdout
        assert result.returncode == 9

        with index_run_lock(tmp_path):
            pass

    def test_the_error_names_the_directory(self, tmp_path: Path):
        with index_run_lock(tmp_path):
            script = textwrap.dedent(
                f"""
                import sys
                from pathlib import Path
                sys.path.insert(0, {str(Path(__file__).resolve().parents[1] / "src")!r})
                from fw_context_mcp.indexer.db._locking import IndexRunLocked, index_run_lock
                try:
                    with index_run_lock(Path({str(tmp_path)!r})):
                        pass
                except IndexRunLocked as exc:
                    print(str(exc))
                """
            )
            result = subprocess.run(  # noqa: S603 — fixed argv, no shell
                [sys.executable, "-c", script],
                capture_output=True, text=True, timeout=60,
            )
        assert str(tmp_path) in result.stdout, result.stdout + result.stderr


class TestReindexerIdentity:
    """Only a background run may be killed to make way for a foreground one."""

    @staticmethod
    def _pid_check():
        from fw_context_mcp.cli._index import _pid_is_fw_context_reindexer

        return _pid_is_fw_context_reindexer

    def test_a_background_run_is_recognised(self, tmp_path: Path):
        """A sleeping stand-in with the same argv shape as the daemon spawns."""
        proc = subprocess.Popen(  # noqa: S603 — fixed argv, no shell
            [sys.executable, "-m", "fw_context_mcp.cli", "index", "--background",
             "--this-argument-does-not-exist"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            # argv is set before the process does anything, so /proc is
            # readable immediately even though the command will fail.
            assert self._pid_check()(proc.pid) is True
        finally:
            proc.kill()
            proc.wait(timeout=10)

    def test_a_foreground_run_is_not_killable(self):
        """This test process runs `pytest`, not `fw-context index --background`."""
        assert self._pid_check()(os.getpid()) is False

    def test_an_unrelated_process_is_not_killable(self):
        """PID 1 must never be signalled."""
        assert self._pid_check()(1) is False

    def test_a_dead_pid_is_not_killable(self):
        assert self._pid_check()(2147483646) is False


class TestExitCodes:
    def test_the_three_outcomes_are_distinguishable(self):
        """done / broken / superseded / already-running must not collide."""
        assert len({0, 1, EXIT_SUPERSEDED, EXIT_ALREADY_RUNNING}) == 4


@pytest.mark.parametrize("name", ["index.lock", "write.lock"])
def test_the_two_locks_are_separate_files(tmp_path: Path, name: str):
    """The run lock must not collide with the per-TU write lock.

    Sharing a file would make every write inside a run deadlock against the
    run's own lock.
    """
    from fw_context_mcp.indexer.db._locking import write_lock

    with index_run_lock(tmp_path), write_lock(tmp_path, timeout=5):
        assert (tmp_path / name).exists()
