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

from fw_context_mcp.exit_codes import EXIT_ALREADY_RUNNING, EXIT_SUPERSEDED, EXIT_TERMINATED
from fw_context_mcp.indexer.db._locking import (
    index_run_lock,
)


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
    """Only an fw-context INDEX run may ever be signalled, and its kind decides how."""

    @staticmethod
    def _kind():
        from fw_context_mcp.cli._index import _index_run_kind

        return _index_run_kind

    @staticmethod
    def _stand_in(*argv: str) -> subprocess.Popen:
        """Spawn a sleeping process that carries *argv*, and wait until it does.

        _index_run_kind reads the argv of the process and nothing else, so a
        process carrying the same argv tests the same thing as a real CLI
        run.  TWO races had to go:

        - The stand-in used to be a real CLI invocation with a bad argument.
          argparse rejects it after 114 ms — measured — so the check read
          the argv of a process that had already exited.
        - Popen returns once fork succeeds, and execve finishes after that.
          Until it does, the argv is still the PARENT's, which is pytest's
          and matches nothing.  The process is alive and the answer is wrong,
          so waiting on poll() would not catch it.

        The child therefore prints a marker before it sleeps, and this
        function returns only once that marker arrives.  At that point the
        exec is done and the argv is the child's own.
        """
        proc = subprocess.Popen(  # noqa: S603 — fixed argv, no shell
            [sys.executable, "-c",
             "import time; print('READY', flush=True); time.sleep(30)", *argv],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "READY"
        return proc

    def test_a_background_run_is_recognised(self, tmp_path: Path):
        """The argv shape the daemon spawns.

        Failed on macOS before: the argv came from /proc only, which macOS
        does not have, so no run was ever recognised and none taken over.
        """
        proc = self._stand_in("-m", "fw_context_mcp.cli", "index", "--background")
        try:
            assert self._kind()(proc.pid) == "background"
        finally:
            proc.kill()
            proc.wait(timeout=10)

    def test_a_foreground_run_is_recognised_as_foreground(self, tmp_path: Path):
        """Without --background the run belongs to a person — --takeover only."""
        proc = self._stand_in("-m", "fw_context_mcp.cli", "index")
        try:
            assert self._kind()(proc.pid) == "foreground"
        finally:
            proc.kill()
            proc.wait(timeout=10)

    def test_another_fw_context_subcommand_is_not_an_index_run(self, tmp_path: Path):
        """Only an INDEX run is killable, not `status` or `init`."""
        proc = self._stand_in("-m", "fw_context_mcp.cli", "status", "--background")
        try:
            assert self._kind()(proc.pid) is None
        finally:
            proc.kill()
            proc.wait(timeout=10)

    def test_this_test_process_is_not_an_index_run(self):
        """This test process runs `pytest`, not `fw-context index`."""
        assert self._kind()(os.getpid()) is None

    def test_an_unrelated_process_is_not_an_index_run(self):
        """PID 1 must never be signalled."""
        assert self._kind()(1) is None

    def test_a_dead_pid_is_not_an_index_run(self):
        assert self._kind()(2147483646) is None


class TestTakeover:
    """A manual run takes the index over; see _acquire_index."""

    @staticmethod
    def _holder(db_dir: Path, kind: str, *, ignore_sigterm: bool = False) -> subprocess.Popen:
        """A real process that owns the index the way cmd_index does.

        It enters _owned_index and then calls raise_if_superseded in a loop,
        like the indexer before each translation unit.  Its argv carries the
        shape of an index run of *kind*, so _index_run_kind recognises it.
        With *ignore_sigterm* it plays a run stuck where the SIGTERM handler
        never gets to run.
        """
        extra = ["-m", "fw_context_mcp.cli", "index"] + (["--background"] if kind == "background" else [])
        script = textwrap.dedent(
            f"""
            import signal, sys, time
            from pathlib import Path
            from types import SimpleNamespace
            sys.path.insert(0, {str(Path(__file__).resolve().parents[1] / "src")!r})
            from fw_context_mcp.cli._index import _owned_index
            from fw_context_mcp.indexer.runner import IndexStopped

            args = SimpleNamespace(background={kind == "background"!r}, takeover=False)
            try:
                with _owned_index(Path({str(db_dir)!r}), args):
                    if {ignore_sigterm!r}:
                        signal.signal(signal.SIGTERM, signal.SIG_IGN)
                    print("HOLDING", flush=True)
                    deadline = time.monotonic() + 30.0
                    while time.monotonic() < deadline:
                        time.sleep(0.05)
            except IndexStopped as exc:
                sys.exit(exc.exit_code)
            sys.exit(0)
            """
        )
        proc = subprocess.Popen(  # noqa: S603 — fixed argv, no shell
            [sys.executable, "-c", script, *extra],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        assert proc.stdout is not None
        line = proc.stdout.readline()
        assert line.strip() == "HOLDING", line + (proc.stderr.read() if proc.poll() is not None else "")
        return proc

    @staticmethod
    def _args(*, background: bool = False, takeover: bool = False):
        from types import SimpleNamespace

        return SimpleNamespace(background=background, takeover=takeover)

    @staticmethod
    def _stop(proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)

    def test_a_background_run_is_taken_over(self, tmp_path: Path):
        """SIGTERM → the holder unwinds as superseded, and the lock is ours."""
        from fw_context_mcp.cli._index import _owned_index

        holder = self._holder(tmp_path, "background")
        try:
            with _owned_index(tmp_path, self._args()):
                assert (tmp_path / "reindex.pause").read_text().strip() == str(os.getpid())
            assert holder.wait(timeout=10) == EXIT_SUPERSEDED
        finally:
            self._stop(holder)

    def test_a_stuck_background_run_is_killed(self, tmp_path: Path, monkeypatch):
        """A holder that ignores SIGTERM gets SIGKILL after the grace period."""
        import signal

        from fw_context_mcp.cli import _index as index_mod

        monkeypatch.setattr(index_mod, "_TAKEOVER_GRACE_S", 1.0)
        holder = self._holder(tmp_path, "background", ignore_sigterm=True)
        try:
            with index_mod._owned_index(tmp_path, self._args()):
                pass
            assert holder.wait(timeout=10) == -signal.SIGKILL
        finally:
            self._stop(holder)

    def test_a_foreground_run_is_refused_without_takeover(self, tmp_path: Path):
        """It belongs to a person; the refusal names the way out."""
        from fw_context_mcp.cli._index import _owned_index
        from fw_context_mcp.indexer.db._locking import IndexRunLocked

        holder = self._holder(tmp_path, "foreground")
        try:
            with pytest.raises(IndexRunLocked, match="--takeover"), _owned_index(tmp_path, self._args()):
                pass
            assert holder.poll() is None, "the holder must not have been signalled"
            assert (tmp_path / "reindex.pause").read_text().strip() == str(holder.pid), (
                "the refused run overwrote the holder's pause marker"
            )
        finally:
            self._stop(holder)

    def test_a_foreground_run_is_taken_over_with_takeover(self, tmp_path: Path):
        from fw_context_mcp.cli._index import _owned_index

        holder = self._holder(tmp_path, "foreground")
        try:
            with _owned_index(tmp_path, self._args(takeover=True)):
                pass
            assert holder.wait(timeout=10) == EXIT_SUPERSEDED
        finally:
            self._stop(holder)

    def test_a_background_run_takes_over_nothing(self, tmp_path: Path):
        """The daemon's run must never terminate a manual one, --takeover or not."""
        from fw_context_mcp.cli._index import _owned_index
        from fw_context_mcp.indexer.db._locking import IndexRunLocked

        holder = self._holder(tmp_path, "foreground")
        try:
            with pytest.raises(IndexRunLocked), _owned_index(
                tmp_path, self._args(background=True, takeover=True)
            ):
                pass
            assert holder.poll() is None
        finally:
            self._stop(holder)

    def test_the_owner_releases_everything(self, tmp_path: Path):
        """Markers, the lock and the SIGTERM handler are all restored."""
        import signal

        from fw_context_mcp.cli._index import _owned_index
        from fw_context_mcp.indexer.db._locking import index_run_lock

        before = signal.getsignal(signal.SIGTERM)
        with _owned_index(tmp_path, self._args()):
            assert signal.getsignal(signal.SIGTERM) is not before
        assert signal.getsignal(signal.SIGTERM) is before
        assert not (tmp_path / "reindex.pause").exists()
        assert not (tmp_path / "reindex.pid").exists()
        with index_run_lock(tmp_path):
            pass

    def test_a_foreign_sigterm_is_not_a_takeover(self, tmp_path: Path):
        """A CI timeout or a ``kill`` printed "Superseded" and exited 75.

        The daemon retries 75, thus it did again the work that somebody
        wanted stopped.
        """
        import signal

        holder = self._holder(tmp_path, "background")
        try:
            holder.send_signal(signal.SIGTERM)
            assert holder.wait(timeout=10) == EXIT_TERMINATED
        finally:
            self._stop(holder)

    def test_the_takeover_marker_lives_as_long_as_the_taker_owns_the_index(
        self, tmp_path: Path
    ):
        """A holder that released the lock early must still find it in its handler."""
        from fw_context_mcp.cli._index import _TAKEOVER_MARKER, _owned_index

        holder = self._holder(tmp_path, "background")
        try:
            with _owned_index(tmp_path, self._args()):
                assert (tmp_path / _TAKEOVER_MARKER).exists()
            assert not (tmp_path / _TAKEOVER_MARKER).exists()
            assert holder.wait(timeout=10) == EXIT_SUPERSEDED
        finally:
            self._stop(holder)


class TestSigtermOutsideTheLock:
    """``cmd_index`` after it released the index, or before it took it."""

    @staticmethod
    def _runner(db_dir: Path) -> subprocess.Popen:
        """A process under the handler of cmd_index, outside the lock."""
        script = textwrap.dedent(
            f"""
            import signal, sys, time
            from pathlib import Path
            sys.path.insert(0, {str(Path(__file__).resolve().parents[1] / "src")!r})
            from fw_context_mcp.cli._index import _SigtermOutsideTheLock

            outside = _SigtermOutsideTheLock()
            outside.db_dir = Path({str(db_dir)!r})
            signal.signal(signal.SIGTERM, outside)
            print("READY", flush=True)
            time.sleep(1.5)
            sys.exit(0)
            """
        )
        proc = subprocess.Popen(  # noqa: S603 — fixed argv, no shell
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "READY"
        return proc

    def test_a_late_takeover_signal_does_not_kill_a_run_that_released(self, tmp_path: Path):
        """The race: the holder released the lock, then the SIGTERM arrived.

        With the default action that run died with -15 in the middle of its
        work after the lock, and the daemon logged it as a failure.
        """
        import signal

        from fw_context_mcp.cli._index import _write_takeover_marker

        runner = self._runner(tmp_path)
        try:
            _write_takeover_marker(tmp_path, runner.pid)
            runner.send_signal(signal.SIGTERM)
            assert runner.wait(timeout=10) == 0
        finally:
            if runner.poll() is None:
                runner.kill()
            runner.wait(timeout=10)

    def test_a_foreign_sigterm_outside_the_lock_stops_with_143(self, tmp_path: Path):
        import signal

        runner = self._runner(tmp_path)
        try:
            runner.send_signal(signal.SIGTERM)
            assert runner.wait(timeout=10) == EXIT_TERMINATED
            assert runner.stderr is not None
            assert "Terminated:" in runner.stderr.read()
        finally:
            if runner.poll() is None:
                runner.kill()
            runner.wait(timeout=10)

    def test_a_marker_for_another_victim_is_no_takeover(self, tmp_path: Path):
        """A SIGTERM that a different run gets at the same time is still foreign."""
        import signal

        from fw_context_mcp.cli._index import _write_takeover_marker

        runner = self._runner(tmp_path)
        try:
            _write_takeover_marker(tmp_path, runner.pid + 1)
            runner.send_signal(signal.SIGTERM)
            assert runner.wait(timeout=10) == EXIT_TERMINATED
        finally:
            if runner.poll() is None:
                runner.kill()
            runner.wait(timeout=10)

    def test_a_marker_of_a_dead_taker_is_no_takeover(self, tmp_path: Path):
        import json
        import signal

        from fw_context_mcp.cli._index import _TAKEOVER_MARKER

        runner = self._runner(tmp_path)
        try:
            (tmp_path / _TAKEOVER_MARKER).write_text(
                json.dumps({"taker": 2147483646, "victim": runner.pid}), encoding="utf-8"
            )
            runner.send_signal(signal.SIGTERM)
            assert runner.wait(timeout=10) == EXIT_TERMINATED
        finally:
            if runner.poll() is None:
                runner.kill()
            runner.wait(timeout=10)

    def test_cmd_index_gives_back_the_handler_of_the_caller(self, monkeypatch, tmp_path: Path):
        import signal

        from fw_context_mcp.cli import _index as index_mod

        monkeypatch.setattr(index_mod, "_cmd_index", lambda args, outside: 0)
        before = signal.getsignal(signal.SIGTERM)

        assert index_mod.cmd_index(object()) == 0
        assert signal.getsignal(signal.SIGTERM) is before


class TestExitCodes:
    def test_the_three_outcomes_are_distinguishable(self):
        """done / broken / superseded / already-running / terminated must not collide."""
        assert len({0, 1, EXIT_SUPERSEDED, EXIT_ALREADY_RUNNING, EXIT_TERMINATED}) == 5


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
        from fw_context_mcp.cli._index import _claim_index
        from fw_context_mcp.indexer.db._locking import index_run_lock

        db_dir = tmp_path / "index"
        db_dir.mkdir()
        pause = db_dir / "reindex.pause"

        script = textwrap.dedent(
            f"""
            import sys
            from pathlib import Path
            sys.path.insert(0, {str(Path(__file__).resolve().parents[1] / "src")!r})
            from types import SimpleNamespace
            from fw_context_mcp.cli._index import _owned_index
            from fw_context_mcp.indexer.db._locking import IndexRunLocked

            db_dir = Path({str(db_dir)!r})
            try:
                with _owned_index(db_dir, SimpleNamespace(background=False, takeover=False)):
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
        does: _owned_index, which takes over only an fw-context index run —
        A is not one, so B is refused.
        """
        from fw_context_mcp.exit_codes import EXIT_SUPERSEDED

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
            from types import SimpleNamespace
            from fw_context_mcp.cli._index import _owned_index
            from fw_context_mcp.indexer.db._locking import IndexRunLocked
            from fw_context_mcp.exit_codes import EXIT_ALREADY_RUNNING

            db_dir = Path({str(db_dir)!r})
            try:
                with _owned_index(db_dir, SimpleNamespace(background=False, takeover=False)):
                    pass
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

    def test_a_refused_takeover_writes_nothing(self, tmp_path: Path):
        """A run refused by _owned_index must leave the directory as it found it.

        The holder here is this process through another open file
        description, which is not an fw-context index run, so the refusal
        comes before anything could be written.
        """
        from types import SimpleNamespace

        from fw_context_mcp.cli._index import _owned_index
        from fw_context_mcp.indexer.db._locking import IndexRunLocked, index_run_lock

        db_dir = tmp_path / "index"
        db_dir.mkdir()

        with index_run_lock(db_dir):
            with pytest.raises(IndexRunLocked), _owned_index(
                db_dir, SimpleNamespace(background=False, takeover=True)
            ):
                pass

            assert not (db_dir / "reindex.pause").exists()
            assert not (db_dir / "reindex.pid").exists()


def test_the_handler_is_in_place_while_the_lock_is_taken(monkeypatch, tmp_path: Path):
    """The lock writes the PID as soon as it is won, and a taker can signal at once.

    With the handler of the caller still in place, that SIGTERM read as a
    takeover after the release and was ignored, and the taker had to use
    SIGKILL.
    """
    import signal

    from fw_context_mcp.cli import _index as index_mod

    seen: list[object] = []
    real_acquire = index_mod._acquire_index

    def _acquire(db_dir, stack, args):
        real_acquire(db_dir, stack, args)
        seen.append(signal.getsignal(signal.SIGTERM))

    monkeypatch.setattr(index_mod, "_acquire_index", _acquire)
    before = signal.getsignal(signal.SIGTERM)
    from types import SimpleNamespace

    with index_mod._owned_index(tmp_path, SimpleNamespace(background=False, takeover=False)):
        pass

    assert seen and seen[0] is not before
    assert signal.getsignal(signal.SIGTERM) is before


def test_a_refused_run_gives_back_the_handler(tmp_path: Path):
    """The handler goes in before the lock, thus a refusal must take it out again."""
    import signal
    from types import SimpleNamespace

    from fw_context_mcp.cli._index import _owned_index
    from fw_context_mcp.indexer.db._locking import IndexRunLocked

    before = signal.getsignal(signal.SIGTERM)
    holder = TestTakeover._holder(tmp_path, "foreground")
    try:
        with pytest.raises(IndexRunLocked), _owned_index(
            tmp_path, SimpleNamespace(background=False, takeover=False)
        ):
            pass
        assert signal.getsignal(signal.SIGTERM) is before
    finally:
        TestTakeover._stop(holder)
