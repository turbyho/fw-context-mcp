"""The work that the daemon has, and a checkout, reach the other processes.

The daemon kept a change that it did not index yet as a flag in its
memory.  No MCP server saw it, thus the answers said "run fw-context
index" while the daemon was about to run it, during the debounce and
while a manual operation held the index.  The marker ``reindex.pending``
makes that work visible (``background.read_reindex_state``).

A checkout changes ``.git/HEAD``, and the watcher of the source files
does not look there.  ``_watch_head`` does.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from fw_context_mcp.exit_codes import EXIT_ALREADY_RUNNING, EXIT_SUPERSEDED
from fw_context_mcp.mcp.background import PENDING_MARKER
from fw_context_mcp.mcp.shared.pid_file import PidFile

DLOG = logging.LoggerAdapter(logging.getLogger("test.daemon"), {})


@pytest.fixture
def fast_loop(monkeypatch):
    """Remove the waits of the loop and the maintenance after a run."""
    from fw_context_mcp.mcp import daemon as daemon_mod

    monkeypatch.setattr(daemon_mod, "_DEBOUNCE_S", 0.0)
    monkeypatch.setattr(daemon_mod, "_WATCH_TIMEOUT", 0.01)
    monkeypatch.setattr(daemon_mod, "_HELD_POLL_S", 0.01)
    monkeypatch.setattr(daemon_mod, "_optimize_db", lambda db_dir: None)
    return daemon_mod


@pytest.fixture
def state_of(tmp_path, monkeypatch):
    """Read the state as an MCP server reads it, for the db dir *tmp_path*."""
    from fw_context_mcp.mcp import background as background_mod

    (tmp_path / "index.db").write_bytes(b"")
    monkeypatch.setattr(background_mod, "_db_path", lambda root: tmp_path / "index.db")
    return lambda: background_mod.read_reindex_state(tmp_path)


def _run_loop(daemon_mod, tmp_path: Path, pending, stop, *, max_polls: int = 200, **kwargs) -> None:
    """Run the reindex loop until *stop* is true, and in each case after *max_polls* pings."""
    calls = {"n": 0}

    def _expired() -> bool:
        calls["n"] += 1
        return bool(stop()) or calls["n"] > max_polls

    asyncio.run(
        daemon_mod._reindex_loop(
            tmp_path, tmp_path, pending=pending, shutdown=asyncio.Event(), dlog=DLOG,
            ping_expired=_expired, on_index_proc=lambda proc: None, **kwargs,
        )
    )


def _hold(path: Path) -> None:
    """Write a marker that a live process other than this one holds."""
    PidFile(path, pid=os.getppid()).write()


# The PID of the index run that a test spawns.  It must differ from the
# PID of `_hold`: `_wait_index` removes only the files of its own run.
_RUN_PID = os.getppid() + 1


class _SecondTurn(Exception):
    """Stops the reindex loop of a test at the start of its second run."""


def _git(repo: Path, *args: str) -> None:
    # No signature: a machine with commit.gpgsign would ask for a key.
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=repo, check=True, capture_output=True,
    )


class TestThePendingMarker:
    def test_the_set_of_the_flag_writes_the_marker(self, tmp_path):
        from fw_context_mcp.mcp import daemon as daemon_mod

        pending = daemon_mod._PendingWork(tmp_path)
        assert not (tmp_path / PENDING_MARKER).exists()
        pending.set()
        assert PidFile.is_active(tmp_path / PENDING_MARKER)
        pending.clear()
        pending.hide_if_done()
        assert not (tmp_path / PENDING_MARKER).exists()

    def test_the_marker_lives_until_the_outcome_of_the_run(self, monkeypatch, tmp_path, fast_loop):
        """The start of the subprocess and the end of the run are outside of `reindex.pid`."""
        daemon_mod = fast_loop
        marker = tmp_path / PENDING_MARKER
        seen: dict[str, bool] = {}

        async def _fake_run(project_root, db_dir, *, force_refs=False):
            seen["spawn"] = PidFile.is_active(marker)
            return SimpleNamespace(pid=4321, returncode=0)

        async def _fake_wait(proc, shutdown, *, db_dir):
            seen["run"] = PidFile.is_active(marker)
            return 0

        def _optimize(db_dir) -> None:
            seen["maintenance"] = PidFile.is_active(marker)

        monkeypatch.setattr(daemon_mod, "_run_index_async", _fake_run)
        monkeypatch.setattr(daemon_mod, "_wait_index", _fake_wait)
        monkeypatch.setattr(daemon_mod, "_optimize_db", _optimize)
        pending = daemon_mod._PendingWork(tmp_path)
        pending.set()

        _run_loop(daemon_mod, tmp_path, pending, lambda: "maintenance" in seen)

        assert seen == {"spawn": True, "run": True, "maintenance": False}, (
            "the work was not visible until the outcome, or the done run still read as pending"
        )
        assert not marker.exists()

    # A held pause: `test_watcher_change_during_index.py::test_a_held_pause_keeps_the_change`.

    def test_a_manual_run_without_a_pause_marker_holds_the_index(self, monkeypatch, tmp_path, fast_loop):
        """`reindex_file` replaced the pause marker of a manual run, and then removed it."""
        daemon_mod = fast_loop
        _hold(tmp_path / "reindex.pid")
        runs: list[int] = []

        async def _fake_run(project_root, db_dir, *, force_refs=False):
            runs.append(1)
            return SimpleNamespace(pid=4321, returncode=0)

        monkeypatch.setattr(daemon_mod, "_run_index_async", _fake_run)
        pending = daemon_mod._PendingWork(tmp_path)
        pending.set()

        _run_loop(daemon_mod, tmp_path, pending, lambda: False, max_polls=20)

        assert runs == [], "the loop started a run while a manual run held the index"
        assert pending.is_set()

    def test_a_long_manual_operation_does_not_drop_the_work(self, monkeypatch, tmp_path, fast_loop):
        """The operation that stopped the run holds the index for many polls."""
        daemon_mod = fast_loop
        pause = tmp_path / "reindex.pause"
        runs: list[int] = []
        polls = {"n": 0}

        async def _fake_run(project_root, db_dir, *, force_refs=False):
            runs.append(1)
            return SimpleNamespace(pid=4321, returncode=0)

        async def _superseded_once(proc, shutdown, *, db_dir):
            if len(runs) == 1:
                _hold(pause)  # the operation that took the index over
                return EXIT_SUPERSEDED
            return 0

        def _stop() -> bool:
            polls["n"] += 1
            if polls["n"] == 100:
                pause.unlink()  # the operation ends
            return len(runs) >= 2 and not pending.is_set()

        monkeypatch.setattr(daemon_mod, "_run_index_async", _fake_run)
        monkeypatch.setattr(daemon_mod, "_wait_index", _superseded_once)
        pending = daemon_mod._PendingWork(tmp_path)
        pending.set()

        _run_loop(daemon_mod, tmp_path, pending, _stop, max_polls=300)

        assert runs == [1, 1], "the work of the stopped run was given up"
        assert polls["n"] >= 100, "the second run did not wait for the operation"
        assert not (tmp_path / PENDING_MARKER).exists()

    def test_a_superseded_run_keeps_its_work_visible(self, monkeypatch, tmp_path, fast_loop):
        """The run gave way to a manual operation: the next turn runs the work again."""
        daemon_mod = fast_loop
        marker = tmp_path / PENDING_MARKER
        seen_at_spawn: list[bool] = []

        async def _fake_run(project_root, db_dir, *, force_refs=False):
            seen_at_spawn.append(PidFile.is_active(marker))
            return SimpleNamespace(pid=4321, returncode=0)

        async def _superseded(proc, shutdown, *, db_dir):
            return EXIT_SUPERSEDED

        monkeypatch.setattr(daemon_mod, "_run_index_async", _fake_run)
        monkeypatch.setattr(daemon_mod, "_wait_index", _superseded)
        pending = daemon_mod._PendingWork(tmp_path)
        pending.set()

        _run_loop(daemon_mod, tmp_path, pending, lambda: len(seen_at_spawn) >= daemon_mod._SUPERSEDED_RUNS_MAX)

        assert seen_at_spawn == [True] * daemon_mod._SUPERSEDED_RUNS_MAX, "a retry started without a state"
        assert not pending.is_set(), "the loop did not give the work up"
        assert not marker.exists(), "the loop gave the work up, thus it is not pending"

    def test_a_run_that_lost_the_index_lock_is_not_a_retry_that_counts(self, monkeypatch, tmp_path, fast_loop):
        """A manual run took the lock between the check and the start: the work runs after it."""
        daemon_mod = fast_loop
        busy_runs = daemon_mod._SUPERSEDED_RUNS_MAX + 2
        runs: list[int] = []

        async def _fake_run(project_root, db_dir, *, force_refs=False):
            runs.append(1)
            return SimpleNamespace(pid=4321, returncode=0)

        async def _busy_then_done(proc, shutdown, *, db_dir):
            return EXIT_ALREADY_RUNNING if len(runs) <= busy_runs else 0

        monkeypatch.setattr(daemon_mod, "_run_index_async", _fake_run)
        monkeypatch.setattr(daemon_mod, "_wait_index", _busy_then_done)
        pending = daemon_mod._PendingWork(tmp_path)
        pending.set()

        _run_loop(daemon_mod, tmp_path, pending, lambda: len(runs) > busy_runs and not pending.is_set())

        assert len(runs) == busy_runs + 1, "the loop gave the work up while another run held the index"
        assert not (tmp_path / PENDING_MARKER).exists()

    def test_a_spawn_that_fails_does_not_stop_the_daemon(self, monkeypatch, tmp_path, fast_loop):
        daemon_mod = fast_loop
        runs: list[int] = []

        async def _fail_once(project_root, db_dir, *, force_refs=False):
            runs.append(1)
            if len(runs) == 1:
                raise PermissionError("test: no exec")
            return SimpleNamespace(pid=4321, returncode=0)

        async def _done(proc, shutdown, *, db_dir):
            return 0

        monkeypatch.setattr(daemon_mod, "_run_index_async", _fail_once)
        monkeypatch.setattr(daemon_mod, "_wait_index", _done)
        pending = daemon_mod._PendingWork(tmp_path)
        pending.set()
        states: list[bool] = []

        def _stop() -> bool:
            if len(runs) == 1 and not states:
                states.append(PidFile.is_active(tmp_path / PENDING_MARKER))
                pending.set()  # the next change
            return len(runs) >= 2 and not pending.is_set()

        _run_loop(daemon_mod, tmp_path, pending, _stop)

        assert states == [False], "the work that failed to start still read as pending"
        assert runs == [1, 1], "the loop did not live on after the failed start"

    def test_a_change_during_a_real_run_stays_visible(self, monkeypatch, tmp_path, fast_loop, state_of):
        """The index run releases `reindex.pid` and then works on, and the daemon sees its end later.

        The work must read as "pending" in that time: it read as nothing
        for 1.4 s, until the loop turned.
        """
        daemon_mod = fast_loop
        pid_path = tmp_path / "reindex.pid"
        runs: list[int] = []
        states: list[str | None] = []
        pending = daemon_mod._PendingWork(tmp_path)
        released = tmp_path / "released"

        async def _spawn(project_root, db_dir, *, force_refs=False):
            runs.append(1)
            if len(runs) > 1:
                raise _SecondTurn  # the test has what it needs
            child = (
                "import pathlib, sys, time\n"
                "time.sleep(0.3)\n"
                "pathlib.Path(sys.argv[1]).unlink()\n"
                "pathlib.Path(sys.argv[2]).write_text('')\n"
                "time.sleep(1.0)\n"
            )
            proc = await asyncio.create_subprocess_exec(sys.executable, "-I", "-c", child, str(pid_path), str(released))
            # The run writes it when it holds the index lock.
            PidFile(pid_path, pid=proc.pid).write()
            return proc

        async def _a_change_and_samples() -> None:
            await asyncio.sleep(0.1)
            pending.set()  # a change while the run works
            deadline = time.monotonic() + 10
            while len(runs) < 2 and time.monotonic() < deadline:
                if released.exists():
                    states.append(state_of())
                await asyncio.sleep(0.05)

        monkeypatch.setattr(daemon_mod, "_run_index_async", _spawn)
        pending.set()

        async def _scenario() -> None:
            sampler = asyncio.create_task(_a_change_and_samples())
            with pytest.raises(_SecondTurn):
                await daemon_mod._reindex_loop(
                    tmp_path, tmp_path, pending=pending, shutdown=asyncio.Event(), dlog=DLOG,
                    ping_expired=lambda: False, on_index_proc=lambda proc: None,
                )
            await sampler

        asyncio.run(_scenario())

        assert runs == [1, 1], "the change did not start a second run"
        assert states, "no sample after the release"
        assert None not in states, f"the pending work read as nothing: {states}"


class TestTheCheckAtTheStart:
    @staticmethod
    def _check(result: tuple[bool, list[str]], calls: list[bool], db_dir: Path):
        def _startup_check(root: Path) -> tuple[bool, list[str]]:
            calls.append(PidFile.is_active(db_dir / "reindex.pid"))
            return result

        return _startup_check

    def test_the_check_waits_for_the_holder_of_the_index(self, monkeypatch, tmp_path, fast_loop):
        """A check of a half-written index finds the work that the manual run does."""
        daemon_mod = fast_loop
        holder = tmp_path / "reindex.pid"
        _hold(holder)
        calls: list[bool] = []
        forced: list[bool] = []
        polls = {"n": 0}

        async def _fake_run(project_root, db_dir, *, force_refs=False):
            forced.append(force_refs)
            return SimpleNamespace(pid=4321, returncode=0)

        async def _done(proc, shutdown, *, db_dir):
            return 0

        def _stop() -> bool:
            polls["n"] += 1
            if polls["n"] == 20:
                holder.unlink()  # the manual run ends
            return bool(forced)

        monkeypatch.setattr(daemon_mod, "_run_index_async", _fake_run)
        monkeypatch.setattr(daemon_mod, "_wait_index", _done)
        pending = daemon_mod._PendingWork(tmp_path)

        _run_loop(
            daemon_mod, tmp_path, pending, _stop,
            startup_check=self._check((True, ["app: refs missing"]), calls, tmp_path),
        )

        assert calls == [False], "the check read the index while another run held it"
        assert forced == [True], "the reason of the check did not reach the run"

    def test_an_index_that_is_current_has_no_work(self, monkeypatch, tmp_path, fast_loop):
        daemon_mod = fast_loop
        calls: list[bool] = []

        async def _must_not_run(project_root, db_dir, *, force_refs=False):
            raise AssertionError("a run for a current index")

        monkeypatch.setattr(daemon_mod, "_run_index_async", _must_not_run)
        pending = daemon_mod._PendingWork(tmp_path)

        _run_loop(
            daemon_mod, tmp_path, pending, lambda: False, max_polls=5,
            startup_check=self._check((False, []), calls, tmp_path),
        )

        assert calls == [False]
        assert not pending.is_set()
        assert not (tmp_path / PENDING_MARKER).exists()

    @pytest.mark.parametrize("kind", ["database", "file", "config"])
    def test_an_error_of_the_check_does_not_stop_the_daemon(
        self, monkeypatch, tmp_path, fast_loop, kind, caplog,
    ):
        """The check is a help for the time when no daemon watched: the watcher must continue."""
        import sqlite3  # after fw_context_mcp: the import redirects it to pysqlite3

        from fw_context_mcp.config.settings import ProjectNotInitializedError

        daemon_mod = fast_loop
        runs: list[bool] = []
        # A config that lost its project ID after the start reads as the
        # defaults, and `_db_path` raises this.
        errors = {
            "database": sqlite3.OperationalError("database is locked"),
            "file": PermissionError("config.toml"),
            "config": ProjectNotInitializedError(tmp_path),
        }

        def _broken(root: Path) -> tuple[bool, list[str]]:
            raise errors[kind]

        async def _fake_run(project_root, db_dir, *, force_refs=False):
            runs.append(force_refs)
            return SimpleNamespace(pid=4321, returncode=0)

        async def _done(proc, shutdown, *, db_dir):
            return 0

        monkeypatch.setattr(daemon_mod, "_run_index_async", _fake_run)
        monkeypatch.setattr(daemon_mod, "_wait_index", _done)
        pending = daemon_mod._PendingWork(tmp_path)
        pending.set()  # a change that the watcher saw

        with caplog.at_level(logging.WARNING, logger="test.daemon"):
            _run_loop(daemon_mod, tmp_path, pending, lambda: bool(runs), startup_check=_broken)

        assert runs == [False], "the change after the error got no run"
        warnings = [r for r in caplog.records if "The check at the start failed" in r.getMessage()]
        assert warnings and warnings[0].exc_info is not None, "the error must reach the log of the daemon"

    @pytest.mark.parametrize("project_id", [None, ""])
    def test_a_config_without_an_id_gives_the_error_of_the_project(self, monkeypatch, tmp_path, project_id):
        """An assert gave an AssertionError, and the daemon did not handle it.

        An empty ID passed the assert, and the database was then in the
        directory of all indexes, not of this project.
        """
        from fw_context_mcp.config.settings import Config, ProjectNotInitializedError
        from fw_context_mcp.mcp.shared import readiness

        cfg = Config()
        cfg.project.id = project_id
        monkeypatch.setattr(readiness, "_check_server_ready", lambda root: tmp_path)
        monkeypatch.setattr(readiness.config, "load", lambda root: cfg)

        with pytest.raises(ProjectNotInitializedError):
            readiness._db_path(tmp_path)


class TestTheSocketOfTheDaemon:
    def test_a_socket_path_that_is_too_long_logs_the_cause(self, tmp_path, caplog):
        """The daemon holds `watcher.lock` before the bind, thus no other daemon runs.

        The log said "daemon already running?" for each error of the bind,
        and that sent the operator to look for a process that was not there.
        """
        from fw_context_mcp.mcp import daemon as daemon_mod

        # Longer than `sun_path` on Linux (108 bytes) and on macOS (104 bytes).
        db_dir = tmp_path / ("d" * 120)
        db_dir.mkdir()

        with caplog.at_level(logging.ERROR, logger="fw_context_mcp.mcp.daemon"), pytest.raises(OSError):
            asyncio.run(daemon_mod._setup_unix_socket(db_dir, lambda: None))

        messages = [r.getMessage() for r in caplog.records]
        assert len(messages) == 1, messages
        assert "AF_UNIX path too long" in messages[0]
        assert "already running" not in messages[0]


class TestTheStartOfTheDaemon:
    @staticmethod
    def _start(monkeypatch, tmp_path) -> dict:
        """Run `daemon_main` up to its reindex loop, and record what the loop gets."""
        from fw_context_mcp.mcp import daemon as daemon_mod

        class _Server:
            def close(self) -> None:
                pass

            async def wait_closed(self) -> None:
                pass

        async def _socket(db_dir, on_ping):
            return _Server(), db_dir / "daemon.sock"

        async def _idle_watch(*args) -> None:
            await args[-2].wait()  # the shutdown event

        seen: dict = {}

        async def _loop(project_root, db_dir, *, pending, startup_check, **kwargs):
            seen["pending"] = pending.is_set()
            seen["marker"] = PidFile.is_active(db_dir / PENDING_MARKER)
            seen["check"] = startup_check is daemon_mod._staleness_check

        monkeypatch.setattr(daemon_mod, "_get_db_dir", lambda root: tmp_path)
        monkeypatch.setattr(daemon_mod, "_setup_unix_socket", _socket)
        monkeypatch.setattr(daemon_mod, "_setup_signal_handlers", lambda shutdown: None)
        monkeypatch.setattr(daemon_mod, "_load_build_patterns", lambda db_dir: [])
        monkeypatch.setattr(daemon_mod, "_watch_changes", _idle_watch)
        monkeypatch.setattr(daemon_mod, "_watch_head", _idle_watch)
        monkeypatch.setattr(daemon_mod, "_reindex_loop", _loop)
        asyncio.run(daemon_mod.daemon_main(tmp_path))
        return seen

    def test_the_loop_gets_the_check_of_the_start(self, monkeypatch, tmp_path):
        seen = self._start(monkeypatch, tmp_path)
        assert seen == {"pending": False, "marker": False, "check": True}

    def test_the_start_removes_an_old_marker(self, monkeypatch, tmp_path):
        """A SIGKILL leaves the marker, and a new process can get its old PID."""
        from fw_context_mcp import utils

        PidFile(tmp_path / PENDING_MARKER).write()
        # The temporary file of a writer that a kill stopped before its rename.
        dead = tmp_path / f".{PENDING_MARKER}.abc123.{2**22 + 12345}@{utils._owner_tag()}.tmp"
        dead.write_text("1", encoding="utf-8")
        live = tmp_path / f".{PENDING_MARKER}.def456.{utils.owner_token()}.tmp"
        live.write_text("1", encoding="utf-8")

        seen = self._start(monkeypatch, tmp_path)

        assert seen["marker"] is False
        assert not dead.exists(), "the file of a dead writer stayed"
        assert live.exists(), "the file of a live writer went away"

    def test_the_shutdown_removes_the_marker(self, tmp_path):
        from fw_context_mcp.mcp import daemon as daemon_mod

        class _Server:
            def close(self) -> None:
                pass

            async def wait_closed(self) -> None:
                pass

        PidFile(tmp_path / PENDING_MARKER).write()
        lock_fd = os.open(tmp_path / "watcher.lock", os.O_CREAT | os.O_RDWR)
        asyncio.run(daemon_mod._cleanup_daemon(
            None, _Server(), tmp_path / "daemon.sock", tmp_path / "watcher.pid", lock_fd, asyncio.Event(),
        ))

        assert not (tmp_path / PENDING_MARKER).exists()


class TestTheSpawnOfARun:
    @staticmethod
    def _spawn(monkeypatch, tmp_path, returncode: int) -> tuple[dict, int | None]:
        """Run `_run_index_async` and `_wait_index` with a subprocess that ends with *returncode*.

        The result holds the keyword arguments of the spawn, and under
        ``"pid_file_at_spawn"`` whether `reindex.pid` was there when the
        subprocess started.
        """
        from fw_context_mcp.mcp import daemon as daemon_mod

        captured: dict = {}

        async def _fake_exec(*args, **kwargs):
            captured.update(kwargs)
            captured["pid_file_at_spawn"] = (tmp_path / "reindex.pid").exists()
            return SimpleNamespace(pid=_RUN_PID, returncode=returncode)

        async def _drive() -> int | None:
            proc = await daemon_mod._run_index_async(tmp_path, tmp_path)
            return await daemon_mod._wait_index(proc, asyncio.Event(), db_dir=tmp_path)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        return captured, asyncio.run(_drive())

    def test_the_files_of_a_manual_run_stay(self, monkeypatch, tmp_path):
        """The manual run won the index lock, and the run of the daemon stopped at once."""
        _hold(tmp_path / "reindex.pid")
        _hold(tmp_path / "reindex.pause")

        _, returncode = self._spawn(monkeypatch, tmp_path, EXIT_ALREADY_RUNNING)

        assert returncode == EXIT_ALREADY_RUNNING
        for name in ("reindex.pid", "reindex.pause"):
            assert PidFile.read_pid(tmp_path / name) == os.getppid(), (
                f"the daemon replaced or removed the {name} of the manual run"
            )

    def test_the_daemon_writes_no_pid_file(self, monkeypatch, tmp_path):
        """The run writes `reindex.pid` when it holds the index lock (`cli._index._claim_index`)."""
        captured, _ = self._spawn(monkeypatch, tmp_path, 0)

        assert captured["pid_file_at_spawn"] is False

    def test_the_files_of_a_killed_run_go(self, monkeypatch, tmp_path):
        """A kill stops the run before it releases the index, and its PID can come back.

        A `reindex.pause` of that PID then holds the loop, with no time
        limit, for a process that has no relation to the index.
        """
        for name in ("reindex.pid", "reindex.pause"):
            PidFile(tmp_path / name, pid=_RUN_PID).write()

        _, returncode = self._spawn(monkeypatch, tmp_path, -9)

        assert returncode == -9
        assert not (tmp_path / "reindex.pid").exists()
        assert not (tmp_path / "reindex.pause").exists()

    def test_a_log_that_cannot_open_does_not_stop_the_run(self, monkeypatch, tmp_path):
        (tmp_path / "reindex.log").mkdir()  # the open fails

        captured, returncode = self._spawn(monkeypatch, tmp_path, 0)

        assert returncode == 0
        assert captured["stdout"] == asyncio.subprocess.DEVNULL


class TestTheWriteOfAPidFile:
    """A reader deletes a file that holds no PID, thus a write must never show one."""

    def test_a_reader_sees_the_old_pid_or_the_new_pid(self, monkeypatch, tmp_path):
        path = tmp_path / "reindex.pid"
        PidFile(path, pid=111).write()
        seen: list[str] = []
        real_replace = os.replace

        def _replace(src, dst):
            seen.append(Path(dst).read_text(encoding="utf-8"))
            real_replace(src, dst)

        monkeypatch.setattr(os, "replace", _replace)
        PidFile(path, pid=222).write()

        assert seen == ["111"], "the file was empty or partly written before the rename"
        assert path.read_text(encoding="utf-8") == "222"
        assert sorted(p.name for p in tmp_path.iterdir()) == ["reindex.pid"], "a temporary file stayed"

    def test_a_failed_write_leaves_no_temporary_file(self, monkeypatch, tmp_path):
        path = tmp_path / "reindex.pid"

        def _fail(src, dst):
            raise OSError("test: no rename")

        monkeypatch.setattr(os, "replace", _fail)
        with pytest.raises(OSError):
            PidFile(path).write()
        assert list(tmp_path.iterdir()) == []

    def test_the_temporary_name_names_its_writer(self, monkeypatch, tmp_path):
        """`utils.remove_stale_tmp` removes the file of a writer that a kill stopped."""
        from fw_context_mcp import utils

        names: list[str] = []
        real_replace = os.replace

        def _replace(src, dst):
            names.append(Path(src).name)
            real_replace(src, dst)

        monkeypatch.setattr(os, "replace", _replace)
        PidFile(tmp_path / "reindex.pid").write()

        match = utils._TMP_OWNER.search(names[0])
        assert match is not None and match.group(1) == utils.owner_token(), names

    def test_a_host_name_with_other_characters(self, monkeypatch, tmp_path):
        """`_owner_tag` makes each character outside `[A-Za-z0-9.-]` a `_`."""
        from fw_context_mcp import utils

        monkeypatch.setattr(utils.socket, "gethostname", lambda: "build host_1")
        tag = utils._owner_tag()
        assert "_" in tag
        dead = tmp_path / f".reindex.pid.abc123.{2**22 + 12345}@{tag}.tmp"
        dead.write_text("1", encoding="utf-8")

        assert utils.remove_stale_tmp(tmp_path) == 1
        assert not dead.exists()

    @pytest.mark.parametrize("reader", ["is_active", "read_pid"])
    def test_a_reader_does_not_delete_a_marker_that_came_after_its_read(self, monkeypatch, tmp_path, reader):
        """The owner removes its marker, the reader misses it, and a new owner writes it."""
        path = tmp_path / PENDING_MARKER
        PidFile(path).write()
        real_read = Path.read_text

        def _read_between_two_owners(self, *args, **kwargs):
            if self == path:
                path.unlink()
                PidFile(path).write()
                raise FileNotFoundError(path)
            return real_read(self, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(Path, "read_text", _read_between_two_owners)
            assert not getattr(PidFile, reader)(path)
        assert PidFile.is_active(path), "the reader deleted the marker of a live writer"


class TestTheStateThatOtherProcessesRead:
    def test_nothing(self, state_of):
        assert state_of() is None

    def test_pending(self, tmp_path, state_of):
        PidFile(tmp_path / PENDING_MARKER).write()
        assert state_of() == "pending"

    def test_a_marker_of_a_dead_daemon_is_no_work(self, tmp_path, state_of):
        PidFile(tmp_path / PENDING_MARKER, pid=2**22 + 12345).write()
        assert state_of() is None

    def test_a_run_wins_over_pending(self, tmp_path, state_of):
        PidFile(tmp_path / PENDING_MARKER).write()
        PidFile(tmp_path / "reindex.pid").write()
        assert state_of() == "reindexing"

    @pytest.mark.parametrize(
        ("marker", "line"),
        [(None, "idle"), (PENDING_MARKER, "scheduled"), ("reindex.pid", "running")],
    )
    def test_the_watch_status_of_the_cli(self, tmp_path, capsys, marker, line):
        """`fw-context watch status` reads the same state as the MCP server."""
        from fw_context_mcp.cli._watch import cmd_watch_status
        from fw_context_mcp.config.settings import generate_project_id

        project_id = generate_project_id()
        root = tmp_path / "proj"
        (root / ".fw-context").mkdir(parents=True)
        index_dir = tmp_path / "index"
        (root / ".fw-context" / "config.toml").write_text(
            f'[project]\nid = "{project_id}"\n\n[index]\ndb_dir = "{index_dir.as_posix()}"\n',
            encoding="utf-8",
        )
        db_dir = index_dir / project_id
        db_dir.mkdir(parents=True)
        (db_dir / "index.db").write_bytes(b"")
        if marker is not None:
            _hold(db_dir / marker)

        assert cmd_watch_status(SimpleNamespace(project=str(root))) == 0

        assert f"Index:      {line}\n" in capsys.readouterr().out


class TestTheWatchErrors:
    def test_a_failure_that_repeats_is_one_warning(self, caplog):
        from fw_context_mcp.mcp import daemon as daemon_mod

        errors = daemon_mod._WatchErrors(DLOG, "the tree")
        with caplog.at_level(logging.DEBUG, logger="test.daemon"):
            for _ in range(3):
                errors.report(OSError("inotify limit"))
            errors.report(OSError("another failure"))
            errors.clear()
            errors.report(OSError("another failure"))

        levels = [record.levelno for record in caplog.records]
        assert levels == [logging.WARNING, logging.DEBUG, logging.DEBUG, logging.WARNING, logging.WARNING]


class TestTheHeadWatcher:
    @staticmethod
    def _watch(monkeypatch, head: Path, batches: list, before_each=None) -> tuple[bool, list]:
        """Run `_watch_head` with a fake watchfiles that gives *batches*, then stops.

        *before_each* runs before each batch, thus a test can change HEAD.
        An empty batch is the output after a timeout.
        """
        import watchfiles

        from fw_context_mcp.indexer import git_context
        from fw_context_mcp.mcp import daemon as daemon_mod

        head.parent.mkdir(parents=True, exist_ok=True)
        if not head.exists():
            head.write_text("ref: refs/heads/main\n", encoding="utf-8")
        shutdown = asyncio.Event()
        pending = asyncio.Event()
        watched: list = []
        queue = list(batches)

        def _fake_awatch(*paths, **kwargs):
            watched.append((paths, kwargs.get("recursive"), kwargs.get("watch_filter", "default")))

            async def _gen():
                while queue:
                    batch = queue.pop(0)
                    if before_each is not None:
                        before_each()
                    yield batch
                shutdown.set()

            return _gen()

        monkeypatch.setattr(watchfiles, "awatch", _fake_awatch)
        monkeypatch.setattr(git_context, "head_path", lambda root: head if head.parent.is_dir() else None)
        asyncio.run(asyncio.wait_for(daemon_mod._watch_head(head.parent.parent, pending, shutdown, DLOG), timeout=10))
        return pending.is_set(), watched

    @staticmethod
    def _checkout(head: Path):
        def _write() -> None:
            head.write_text("ref: refs/heads/other\n", encoding="utf-8")

        return _write

    def test_a_checkout_sets_the_flag(self, monkeypatch, tmp_path):
        head = tmp_path / ".git" / "HEAD"
        flag, watched = self._watch(monkeypatch, head, [{(1, str(head))}], self._checkout(head))
        assert flag
        assert watched == [((head.parent.resolve(),), False, None)], (
            "the watch must be on the git directory, not recursive, and without the filter of .git paths"
        )

    def test_a_checkout_without_an_event_of_head_is_found(self, monkeypatch, tmp_path):
        """The checkout came before the watch existed: the output after the timeout finds it."""
        head = tmp_path / ".git" / "HEAD"
        flag, _ = self._watch(monkeypatch, head, [set()], self._checkout(head))
        assert flag

    def test_the_same_checkout_again_does_not(self, monkeypatch, tmp_path):
        # `git checkout main` on main rewrites HEAD with the same text.
        head = tmp_path / ".git" / "HEAD"
        flag, _ = self._watch(monkeypatch, head, [{(2, str(head))}])
        assert not flag

    def test_other_files_of_the_git_directory_do_not(self, monkeypatch, tmp_path):
        # A commit on a branch writes `.git/logs/HEAD`, a file of the same name.
        head = tmp_path / ".git" / "HEAD"
        others = [{(2, str(head.parent / name))} for name in ("index", "HEAD.lock", "logs/HEAD")]
        flag, _ = self._watch(monkeypatch, head, others)
        assert not flag

    def test_a_change_of_the_git_directory_itself_keeps_the_watch(self, monkeypatch, tmp_path):
        # `touch .git` or a chmod: the directory is still the same.
        head = tmp_path / ".git" / "HEAD"
        _, watched = self._watch(monkeypatch, head, [{(2, str(head.parent.resolve()))}])
        assert len(watched) == 1

    def test_a_deletion_of_the_git_directory_restarts_the_watch(self, monkeypatch, tmp_path):
        head = tmp_path / ".git" / "HEAD"
        _, watched = self._watch(monkeypatch, head, [{(3, str(head.parent.resolve()))}, set()])
        assert len(watched) == 2

    def test_a_replaced_git_directory_without_an_event_restarts_the_watch(self, monkeypatch, tmp_path):
        """A new directory with another inode: no event reaches the old watch."""
        head = tmp_path / ".git" / "HEAD"
        replaced: list[bool] = []

        def _replace() -> None:
            if replaced:
                return
            replaced.append(True)
            # The old directory stays as another name, thus its inode is not free for the new one.
            head.parent.rename(tmp_path / "old-git")
            head.parent.mkdir()
            head.write_text("ref: refs/heads/other\n", encoding="utf-8")

        flag, watched = self._watch(monkeypatch, head, [set(), set()], _replace)
        assert len(watched) == 2, "the watch stayed on the old directory"
        assert flag, "the checkout of the new directory was lost"

    def test_a_tree_without_git_is_watched_after_git_init(self, monkeypatch, tmp_path):
        from fw_context_mcp.indexer import git_context
        from fw_context_mcp.mcp import daemon as daemon_mod

        head = tmp_path / ".git" / "HEAD"
        shutdown = asyncio.Event()
        pending = asyncio.Event()

        import watchfiles

        def _fake_awatch(*paths, **kwargs):
            async def _gen():
                # An empty output after each timeout, as `yield_on_timeout` gives.
                while not shutdown.is_set():
                    await asyncio.sleep(0.01)
                    yield set()

            return _gen()

        monkeypatch.setattr(watchfiles, "awatch", _fake_awatch)
        monkeypatch.setattr(git_context, "head_path", lambda root: head if head.exists() else None)
        monkeypatch.setattr(git_context, "_NO_GIT_RECHECK_S", 0.01)

        async def _scenario() -> bool:
            task = asyncio.create_task(daemon_mod._watch_head(tmp_path, pending, shutdown, DLOG))
            await asyncio.sleep(0.1)
            assert not pending.is_set()
            head.parent.mkdir()
            head.write_text("ref: refs/heads/main\n", encoding="utf-8")  # `git init`
            try:
                await asyncio.wait_for(pending.wait(), timeout=5)
                return True
            except TimeoutError:
                return False
            finally:
                shutdown.set()
                await asyncio.wait_for(task, timeout=5)

        assert asyncio.run(_scenario()), "the checkout of a late `git init` did not reach the daemon"

    @staticmethod
    async def _until_flag(pending: asyncio.Event, act, seconds: float = 10.0) -> bool:
        """Do *act* until *pending* is set.

        WHY again and again: the inotify watch starts in a thread, and a
        checkout before it exists has no event.  A fixed wait is too short
        on a machine under load.
        """
        loop = asyncio.get_running_loop()
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            await loop.run_in_executor(None, act)
            try:
                await asyncio.wait_for(pending.wait(), timeout=0.5)
                return True
            except TimeoutError:
                continue
        return False

    @staticmethod
    def _switcher(repo: Path):
        branches = ["other", "main"]

        def _switch() -> None:
            branches.reverse()
            _git(repo, "checkout", "-q", "-B", branches[0])

        return _switch

    def test_a_real_checkout_on_a_real_repo(self, monkeypatch, tmp_path):
        """The real watchfiles: its default filter dropped each path in `.git`."""
        from fw_context_mcp.mcp import daemon as daemon_mod

        _git(tmp_path, "init", "-q", "-b", "main")
        _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "first")
        monkeypatch.setattr(daemon_mod, "_DEBOUNCE_S", 0.05)
        # Longer than the budget of `_until_flag`: the output after a
        # timeout reads HEAD too, and it would find the checkout without an
        # event.  Only the event of the checkout can set the flag here.
        monkeypatch.setattr(daemon_mod, "_WATCH_TIMEOUT", 60)

        async def _scenario() -> bool:
            shutdown = asyncio.Event()
            pending = asyncio.Event()
            task = asyncio.create_task(daemon_mod._watch_head(tmp_path, pending, shutdown, DLOG))
            try:
                return await self._until_flag(pending, self._switcher(tmp_path))
            finally:
                shutdown.set()
                await asyncio.wait_for(task, timeout=10)

        assert asyncio.run(_scenario()), "a checkout did not reach the daemon"

    def test_a_git_directory_that_rm_and_git_init_replace(self, monkeypatch, tmp_path):
        """The old watch is on a removed directory and gets no more events."""
        from fw_context_mcp.indexer import git_context
        from fw_context_mcp.mcp import daemon as daemon_mod

        # The watch can stop before `git init` makes the new directory.
        monkeypatch.setattr(git_context, "_NO_GIT_RECHECK_S", 0.2)
        _git(tmp_path, "init", "-q", "-b", "main")
        _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "first")
        monkeypatch.setattr(daemon_mod, "_DEBOUNCE_S", 0.05)
        monkeypatch.setattr(daemon_mod, "_WATCH_TIMEOUT", 1)

        def _replace() -> None:
            shutil.rmtree(tmp_path / ".git")
            _git(tmp_path, "init", "-q", "-b", "fresh")
            _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "new")

        async def _scenario() -> tuple[bool, bool]:
            shutdown = asyncio.Event()
            pending = asyncio.Event()
            task = asyncio.create_task(daemon_mod._watch_head(tmp_path, pending, shutdown, DLOG))
            try:
                assert await self._until_flag(pending, self._switcher(tmp_path)), "no watch at the start"
                await asyncio.sleep(0.3)  # the batch of that checkout ends
                pending.clear()
                await asyncio.get_running_loop().run_in_executor(None, _replace)
                replaced = await self._until_flag(pending, lambda: None)
                await asyncio.sleep(0.3)
                pending.clear()
                followed = await self._until_flag(pending, self._switcher(tmp_path))
                return replaced, followed
            finally:
                shutdown.set()
                await asyncio.wait_for(task, timeout=10)

        replaced, followed = asyncio.run(_scenario())
        assert replaced, "the new git directory did not reach the daemon"
        assert followed, "a checkout in the new git directory did not reach the daemon"


class TestTheMaintenanceAfterARun:
    """`_optimize_db` runs in the reindex loop after each run that ended with 0."""

    def test_it_does_no_integrity_check(self, monkeypatch, tmp_path):
        """The run that just ended did the check.  A check reads the full file: 1.5 s on a database of 6.5 GB."""
        from fw_context_mcp.indexer import db as db_mod
        from fw_context_mcp.mcp import daemon as daemon_mod

        db_mod.open_db(tmp_path / "index.db", skip_integrity_check=True).close()
        calls = []
        real_open_db = db_mod.open_db

        def _open_db(path, **kwargs):
            calls.append(kwargs)
            return real_open_db(path, **kwargs)

        monkeypatch.setattr(db_mod, "open_db", _open_db)

        daemon_mod._optimize_db(tmp_path)

        assert calls == [{"skip_integrity_check": True}]

    @pytest.mark.parametrize("kind", ["database", "file", "locked"])
    def test_an_error_of_the_open_gives_a_warning(self, monkeypatch, tmp_path, kind, caplog):
        """An error of the open went out of the reindex loop, and the daemon stopped.

        The fixture `fast_loop` replaces `_optimize_db` in the tests of the loop,
        thus this test calls the function directly.  Its exception stopped the loop.
        """
        import sqlite3  # after fw_context_mcp: the import redirects it to pysqlite3

        from fw_context_mcp.indexer import db as db_mod
        from fw_context_mcp.mcp import daemon as daemon_mod

        (tmp_path / "index.db").write_bytes(b"")
        error = {
            "database": sqlite3.OperationalError("database is locked"),
            "file": PermissionError("index.db"),
            "locked": db_mod.DatabaseCorruptionError("index.db", "Database locked after 4 attempts", locked=True),
        }[kind]

        def _fails(path, **kwargs):
            raise error

        monkeypatch.setattr(db_mod, "open_db", _fails)

        with caplog.at_level(logging.WARNING, logger="fw_context_mcp.mcp.daemon"):
            daemon_mod._optimize_db(tmp_path)

        warnings = [r for r in caplog.records if "PRAGMA optimize" in r.getMessage()]
        assert warnings and warnings[0].exc_info is not None, "the error must reach the log of the daemon"
