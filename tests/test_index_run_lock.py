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


class TestARefusedRunLeavesNothingBehind:
    """A run that LOSES the lock must not claim the index.

    Observed:

        A: holds the lock, indexing
        B: refused, an index run is already in progress ... (pid 2631030)
        A: ABORTED after 2 TUs -- another process (pid 2631031) took over
        A exit=75

    cmd_index called _manage_bg_reindex() BEFORE index_run_lock, and that
    function wrote reindex.pause with its own PID unconditionally.  When the
    lock then refused B, the cleanup sat inside the `with` block that never
    ran, so B's marker stayed for as long as B lived.  A calls
    raise_if_superseded before every TU, and a live foreign PID in the marker
    reads as "somebody took the index over".  A run that won the lock threw
    away an hour of work because of a run that was correctly refused.

    test_index_run_lock.py and test_superseded_run.py each covered one
    mechanism.  Nothing covered the two together, which is why it shipped.
    """

    def test_the_pause_marker_is_not_left_behind_by_a_refused_run(self, tmp_path: Path):
        """B is refused, so it must leave no marker at all."""
        from fw_context_mcp.cli._index import _claim_index, _kill_bg_reindex
        from fw_context_mcp.indexer.db._locking import IndexRunLocked, index_run_lock

        db_dir = tmp_path / "index"
        db_dir.mkdir()
        pause = db_dir / "reindex.pause"

        script = textwrap.dedent(
            f"""
            import sys
            from pathlib import Path
            sys.path.insert(0, {str(Path(__file__).resolve().parents[1] / "src")!r})
            from fw_context_mcp.cli._index import _claim_index, _kill_bg_reindex
            from fw_context_mcp.indexer.db._locking import IndexRunLocked, index_run_lock

            db_dir = Path({str(db_dir)!r})
            _kill_bg_reindex(db_dir / "index.db")
            try:
                with index_run_lock(db_dir):
                    _claim_index(db_dir)
                    print("ACQUIRED")
            except IndexRunLocked:
                print("REFUSED")
            """
        )
        with index_run_lock(db_dir):
            _claim_index(db_dir)
            result = subprocess.run(  # noqa: S603 — fixed argv, no shell
                [sys.executable, "-c", script],
                capture_output=True, text=True, timeout=60,
            )
            assert "REFUSED" in result.stdout, result.stdout + result.stderr
            # The marker must still name THIS process, not the refused one.
            assert pause.read_text().strip() == str(os.getpid()), (
                "the refused run overwrote the holder's pause marker"
            )

    def test_a_refused_run_does_not_abort_the_holder(self, tmp_path: Path):
        """End to end: A holds the lock and must survive B being refused.

        Both sides are real processes.  A takes the lock, claims the index
        and then calls raise_if_superseded in a short loop, the same call the
        indexer makes before every translation unit.  B does what cmd_index
        does: kill the background run, take the lock, claim on success.
        """
        from fw_context_mcp.indexer.db._locking import index_run_lock
        from fw_context_mcp.indexer.runner import EXIT_SUPERSEDED

        db_dir = tmp_path / "index"
        db_dir.mkdir()

        holder = textwrap.dedent(
            f"""
            import sys, time
            from pathlib import Path
            sys.path.insert(0, {str(Path(__file__).resolve().parents[1] / "src")!r})
            from fw_context_mcp.cli._index import _claim_index, _release_index
            from fw_context_mcp.indexer.db._locking import index_run_lock
            from fw_context_mcp.indexer.runner import (
                EXIT_SUPERSEDED, IndexSuperseded, raise_if_superseded,
            )

            db_dir = Path({str(db_dir)!r})
            with index_run_lock(db_dir):
                _claim_index(db_dir)
                print("HOLDING", flush=True)
                try:
                    deadline = time.monotonic() + 8.0
                    while time.monotonic() < deadline:
                        raise_if_superseded(db_dir)
                        time.sleep(0.05)
                except IndexSuperseded as exc:
                    print(f"ABORTED {{exc}}", flush=True)
                    sys.exit(EXIT_SUPERSEDED)
                finally:
                    _release_index(db_dir)
            sys.exit(0)
            """
        )
        # B STAYS ALIVE after it is refused.  That is the condition of the
        # incident: PidFile.is_active answers False for a dead PID, so a
        # marker left by a process that exited at once is ignored and the
        # test would pass with or without the fix.
        challenger = textwrap.dedent(
            f"""
            import sys, time
            from pathlib import Path
            sys.path.insert(0, {str(Path(__file__).resolve().parents[1] / "src")!r})
            from fw_context_mcp.cli._index import _claim_index, _kill_bg_reindex
            from fw_context_mcp.indexer.db._locking import IndexRunLocked, index_run_lock
            from fw_context_mcp.indexer.runner import EXIT_ALREADY_RUNNING

            db_dir = Path({str(db_dir)!r})
            _kill_bg_reindex(db_dir / "index.db")
            try:
                with index_run_lock(db_dir):
                    _claim_index(db_dir)
                code = 0
            except IndexRunLocked:
                code = EXIT_ALREADY_RUNNING
            print("REFUSED", flush=True)
            time.sleep(5.0)
            sys.exit(code)
            """
        )

        a = subprocess.Popen(  # noqa: S603 — fixed argv, no shell
            [sys.executable, "-c", holder],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        b = None
        try:
            line = a.stdout.readline()
            assert line.strip() == "HOLDING", line

            b = subprocess.Popen(  # noqa: S603 — fixed argv, no shell
                [sys.executable, "-c", challenger],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            assert b.stdout.readline().strip() == "REFUSED"

            out, err = a.communicate(timeout=30)
            assert b.wait(timeout=30) == EXIT_ALREADY_RUNNING, (
                "B should be refused"
            )
        finally:
            for proc in (a, b):
                if proc is not None and proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=10)

        assert a.returncode != EXIT_SUPERSEDED, (
            "the run that WON the lock abandoned itself because of the run "
            f"that was refused: {out}{err}"
        )
        assert a.returncode == 0, f"{a.returncode}: {out}{err}"

    def test_a_manual_takeover_still_supersedes(self, tmp_path: Path):
        """The regression guard in the other direction.

        reset_index and reindex_file write the marker from a LIVE process
        that does not hold index_run_lock, and a run must still stop for
        them.  A fix that made raise_if_superseded ignore foreign markers
        would pass the test above and break this one.
        """
        from fw_context_mcp.indexer.runner import IndexSuperseded, raise_if_superseded
        from fw_context_mcp.mcp.shared.pid_file import PidFile

        db_dir = tmp_path / "index"
        db_dir.mkdir()

        # A live process that is not this one: a sleeping child.
        other = subprocess.Popen(  # noqa: S603 — fixed argv, no shell
            [sys.executable, "-c", "import time; time.sleep(30)"],
        )
        try:
            (db_dir / "reindex.pause").write_text(str(other.pid))
            assert PidFile.is_active(db_dir / "reindex.pause")
            with pytest.raises(IndexSuperseded):
                raise_if_superseded(db_dir)
        finally:
            other.kill()
            other.wait(timeout=10)

    def test_the_kill_step_writes_nothing(self, tmp_path: Path):
        """_kill_bg_reindex must leave the directory as it found it.

        Its whole purpose is to be safe to call BEFORE the lock.
        """
        from fw_context_mcp.cli._index import _kill_bg_reindex

        db_dir = tmp_path / "index"
        db_dir.mkdir()

        _kill_bg_reindex(db_dir / "index.db")

        assert not (db_dir / "reindex.pause").exists()
        assert not (db_dir / "reindex.pid").exists()
