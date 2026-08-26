"""A file change during a background index must not be lost.

Observed defect: ``fw-context watch status`` reported ``Index: idle`` and
``Modified: 1 file(s)`` 40 minutes after the last index run.  The daemon log
and the index log give the sequence:

- 16:08:25 — the daemon detected a change and started the index subprocess.
- 16:09:07 — the subprocess indexed the translation unit of ``zled.h``.
- 16:09:21 — the user wrote ``zled.h`` again.
- 16:15:55 — the subprocess completed, and the daemon went back to the watch.

The daemon watched for changes with an ``async for`` over ``awatch``.  To start
the index it left that loop, and the inotify subscription closed.  watchfiles
keeps no events for a closed subscription, thus the 16:09:21 write produced no
event for anybody, and no second run started.

The repair gives the watcher its own task, which lives as long as the daemon,
and a pending flag that the reindex loop clears BEFORE it starts a run.  These
tests hold both halves in place.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

DLOG = logging.LoggerAdapter(logging.getLogger("test.daemon"), {})


@pytest.fixture
def fast_loop(monkeypatch):
    """Remove the two waits that would make each test take seconds."""
    from fw_context_mcp.mcp import daemon as daemon_mod

    monkeypatch.setattr(daemon_mod, "_DEBOUNCE_S", 0.0)
    monkeypatch.setattr(daemon_mod, "_WATCH_TIMEOUT", 0.01)
    return daemon_mod


def _stop_after(predicate, *, max_polls: int = 200):
    """Stop the loop on *predicate*, and stop it in each case after *max_polls*.

    The cap turns a defect into an assertion that fails.  Without the cap, a
    loop that never gets its flag again keeps the test process in the poll.
    """
    state = {"n": 0}

    def _expired() -> bool:
        state["n"] += 1
        return bool(predicate()) or state["n"] > max_polls

    return _expired


def _no_pause(monkeypatch) -> None:
    """Report that no manual operation holds the index."""
    from fw_context_mcp.mcp import background as background_mod

    monkeypatch.setattr(background_mod, "_check_bg_pause", lambda root: False)


# ── The reindex loop must pick a change up after the run it raced ────────────


class TestChangeDuringIndex:
    """The loop must index a change that arrives while a run is active."""

    @staticmethod
    def _run_loop(daemon_mod, *, pending, ping_expired, tmp_path) -> None:
        asyncio.run(
            daemon_mod._reindex_loop(
                tmp_path,
                tmp_path,
                pending=pending,
                shutdown=asyncio.Event(),
                dlog=DLOG,
                ping_expired=ping_expired,
                on_index_proc=lambda proc: None,
            )
        )

    def test_a_change_during_a_run_starts_another_run(
        self, monkeypatch, tmp_path: Path, fast_loop
    ):
        """This is the defect.  One edit at 16:09:21, one more index run."""
        daemon_mod = fast_loop
        _no_pause(monkeypatch)
        pending = asyncio.Event()
        pending.set()
        flag_at_spawn: list[bool] = []

        async def _fake_run(project_root, db_dir, *, force_refs=False):
            flag_at_spawn.append(pending.is_set())
            return SimpleNamespace(pid=4321, returncode=0)

        async def _fake_wait(proc, shutdown, *, db_dir):
            if len(flag_at_spawn) == 1:
                pending.set()  # the user writes the file mid-run
            return False

        monkeypatch.setattr(daemon_mod, "_run_index_async", _fake_run)
        monkeypatch.setattr(daemon_mod, "_wait_index", _fake_wait)

        self._run_loop(
            daemon_mod,
            pending=pending,
            ping_expired=_stop_after(lambda: len(flag_at_spawn) >= 2),
            tmp_path=tmp_path,
        )

        assert len(flag_at_spawn) == 2, "the mid-run change started no second run"
        assert flag_at_spawn == [False, False], "the flag was not clear at spawn time"
        assert not pending.is_set()

    def test_a_quiet_run_is_not_repeated(self, monkeypatch, tmp_path: Path, fast_loop):
        """No change during the run, thus one run only."""
        daemon_mod = fast_loop
        _no_pause(monkeypatch)
        pending = asyncio.Event()
        pending.set()
        spawns: list[int] = []

        async def _fake_run(project_root, db_dir, *, force_refs=False):
            spawns.append(1)
            return SimpleNamespace(pid=1, returncode=0)

        async def _fake_wait(proc, shutdown, *, db_dir):
            return False

        monkeypatch.setattr(daemon_mod, "_run_index_async", _fake_run)
        monkeypatch.setattr(daemon_mod, "_wait_index", _fake_wait)

        self._run_loop(
            daemon_mod,
            pending=pending,
            ping_expired=_stop_after(lambda: len(spawns) >= 1),
            tmp_path=tmp_path,
        )

        assert spawns == [1]

    def test_a_held_pause_keeps_the_change(self, monkeypatch, tmp_path: Path, fast_loop):
        """A manual single-file reindex covers one file, not the other changes."""
        daemon_mod = fast_loop
        from fw_context_mcp.mcp import background as background_mod

        monkeypatch.setattr(background_mod, "_check_bg_pause", lambda root: True)
        waits: list[int] = []

        async def _never_clears(root, shutdown):
            waits.append(1)
            return False

        async def _must_not_run(project_root, db_dir, *, force_refs=False):
            raise AssertionError("the loop started an index under a held pause")

        monkeypatch.setattr(daemon_mod, "_wait_for_pause_to_clear", _never_clears)
        monkeypatch.setattr(daemon_mod, "_run_index_async", _must_not_run)

        pending = asyncio.Event()
        pending.set()
        self._run_loop(
            daemon_mod,
            pending=pending,
            ping_expired=_stop_after(lambda: len(waits) >= 1),
            tmp_path=tmp_path,
        )

        assert pending.is_set(), "the loop dropped a change that nobody indexed"


# ── The watcher task must set the flag, and filter on the file type ──────────


class TestWatcherTask:
    """``_watch_changes`` must flag C/C++ changes only, and stop on shutdown."""

    @staticmethod
    def _run_watcher(monkeypatch, tmp_path: Path, changed_path: str) -> bool:
        import watchfiles

        from fw_context_mcp.mcp import daemon as daemon_mod

        shutdown = asyncio.Event()
        pending = asyncio.Event()

        def _fake_awatch(*paths, **kwargs):
            async def _gen():
                yield {(1, changed_path)}
                shutdown.set()

            return _gen()

        monkeypatch.setattr(watchfiles, "awatch", _fake_awatch)
        asyncio.run(
            daemon_mod._watch_changes(tmp_path, [], pending, shutdown, DLOG)
        )
        return pending.is_set()

    def test_a_header_change_sets_the_flag(self, monkeypatch, tmp_path: Path):
        assert self._run_watcher(monkeypatch, tmp_path, "/p/src/zled.h") is True

    def test_a_document_change_does_not_set_the_flag(self, monkeypatch, tmp_path: Path):
        assert self._run_watcher(monkeypatch, tmp_path, "/p/README.md") is False

    def test_a_build_dir_change_does_not_set_the_flag(self, monkeypatch, tmp_path: Path):
        assert self._run_watcher(monkeypatch, tmp_path, "/p/build/gen.cpp") is False


class TestStopWatcher:
    """The shutdown must not stop on an error from the watcher task."""

    def test_a_failed_task_does_not_raise(self):
        from fw_context_mcp.mcp import daemon as daemon_mod

        async def _boom():
            raise RuntimeError("the watcher died")

        async def _main():
            task = asyncio.create_task(_boom())
            await asyncio.sleep(0)
            await daemon_mod._stop_watcher(task)

        asyncio.run(_main())  # must not raise

    def test_a_live_task_is_cancelled(self):
        from fw_context_mcp.mcp import daemon as daemon_mod

        async def _forever():
            await asyncio.Event().wait()

        async def _main():
            task = asyncio.create_task(_forever())
            await asyncio.sleep(0)
            await daemon_mod._stop_watcher(task)
            assert task.cancelled()

        asyncio.run(_main())
