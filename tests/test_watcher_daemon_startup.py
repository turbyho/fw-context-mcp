"""The watcher daemon must start even when the MCP server missed its window.

Observed defect: ``fw-context watch status`` reported ``Daemon: not running``
and ``Index: idle`` for a project with two modified files.  The daemon is the
only component that starts a background reindex (``daemon.py`` spawns
``fw-context index --background``), thus no reindex ever ran.

Two independent holes produced that state:

1. ``server.main()`` spawns the daemon only at startup, and only when
   ``index.db`` already exists.  The MCP server for the project had started
   three minutes BEFORE the first index run, took the ``no_index`` early
   return, and never spawned a daemon or a ping thread.
2. ``_ping_loop`` documented that it calls ``_ensure_daemon_running`` after a
   failed ping, but the code only wrote a debug log line.  A daemon that
   stopped was therefore never started again.

These tests hold the two repairs in place.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

from fw_context_mcp.exit_codes import EXIT_SUPERSEDED

# ── Fix 1: the ping loop must start the daemon again ─────────────────────────

#: Never set.  ``_park()`` waits on it to stop a test ping thread for good.
_NEVER = threading.Event()


def _park() -> None:
    """Block the calling ping thread for good, and give control back.

    A test thread must not stay in the loop, because the test lowers
    ``PING_INTERVAL`` to 1 ms and the thread would spin for the rest of the
    session.  An exception is not an alternative: pytest reports every
    exception that leaves a thread as a warning.  The ping thread is a
    daemon thread, thus the interpreter kills the parked thread at exit.
    """
    _NEVER.wait()


class TestPingLoopRevival:
    """``_ping_loop`` must act on a failed ping, not only log it."""

    @staticmethod
    def _run_loop(monkeypatch, root: Path, ensure_stub) -> None:
        """Start the ping thread with a 1 ms interval and the given stub."""
        from fw_context_mcp.mcp import daemon as daemon_mod
        from fw_context_mcp.mcp import server as server_mod

        monkeypatch.setattr(daemon_mod, "PING_INTERVAL", 0.001)
        monkeypatch.setattr(server_mod, "_ensure_daemon_running", ensure_stub)
        server_mod._start_ping_thread(root)

    def test_a_failed_ping_starts_the_daemon(self, monkeypatch, tmp_path: Path):
        from fw_context_mcp.mcp import daemon as daemon_mod

        monkeypatch.setattr(daemon_mod, "ping_daemon", lambda root: False)
        seen: list[Path] = []
        done = threading.Event()

        def _ensure(root: Path) -> None:
            seen.append(root)
            done.set()
            _park()

        self._run_loop(monkeypatch, tmp_path, _ensure)

        assert done.wait(timeout=5.0), "the ping loop never called _ensure_daemon_running"
        assert seen == [tmp_path]

    def test_a_live_daemon_is_not_started_again(self, monkeypatch, tmp_path: Path):
        """A ping that gets an answer must not spawn a second daemon."""
        from fw_context_mcp.mcp import daemon as daemon_mod

        pings = threading.Semaphore(0)
        count: list[int] = []

        def _ping(root: Path) -> bool:
            count.append(1)
            pings.release()
            if len(count) >= 3:
                _park()
            return True

        monkeypatch.setattr(daemon_mod, "ping_daemon", _ping)
        spawned: list[Path] = []

        def _ensure(root: Path) -> None:
            spawned.append(root)
            _park()

        self._run_loop(monkeypatch, tmp_path, _ensure)

        # Wait for three loop passes.  Each one gets an answer, thus none of
        # them is allowed to start a daemon.
        for _ in range(3):
            assert pings.acquire(timeout=5.0)
        assert spawned == []

    def test_the_loop_survives_a_runtime_error(self, monkeypatch, tmp_path: Path):
        """A project without an index makes ``_db_path()`` raise RuntimeError.

        The state is temporary — the operator can still run index — thus the
        loop must try again instead of stopping.
        """
        from fw_context_mcp.mcp import daemon as daemon_mod

        monkeypatch.setattr(daemon_mod, "ping_daemon", lambda root: False)
        attempts: list[int] = []
        done = threading.Event()

        def _ensure(root: Path) -> None:
            attempts.append(1)
            if len(attempts) < 3:
                raise RuntimeError("No index found — run fw-context index")
            done.set()
            _park()

        self._run_loop(monkeypatch, tmp_path, _ensure)

        assert done.wait(timeout=5.0), "the loop stopped after the first RuntimeError"
        assert len(attempts) == 3


# ── Shared configuration stubs ───────────────────────────────────────────────


@dataclass
class _FakeIndexCfg:
    db_dir: Path
    vendor_paths: list = field(default_factory=list)
    project_paths: list = field(default_factory=list)


@dataclass
class _FakeBuildCfg:
    system: str | None = "makefile"
    variants: list = field(default_factory=list)


@dataclass
class _FakeCfg:
    index: _FakeIndexCfg
    build: _FakeBuildCfg = field(default_factory=_FakeBuildCfg)
    cache_server: None = None


# ── Fix 1: the ping thread must start without an index ───────────────────────


class TestServerStartup:
    """``main()`` must arm the recovery path in every state it can reach."""

    @staticmethod
    def _patch_main(monkeypatch, tmp_path: Path) -> dict:
        """Replace everything ``main()`` touches, and record the two calls."""
        import fw_context_mcp.config as config_mod
        import fw_context_mcp.deps as deps_mod
        from fw_context_mcp.mcp import server as server_mod

        cfg = _FakeCfg(index=_FakeIndexCfg(db_dir=tmp_path / "index"))
        calls: dict = {"ensure": [], "ping": []}

        monkeypatch.setattr(server_mod, "_check_server_ready", lambda: tmp_path)
        monkeypatch.setattr(server_mod, "resolve_project_root", lambda arg: tmp_path)
        monkeypatch.setattr(deps_mod, "run_preflight", lambda: [])
        monkeypatch.setattr(config_mod, "derive_project_id", lambda root: "pid")
        monkeypatch.setattr(config_mod, "load", lambda project_root=None: cfg)
        monkeypatch.setattr(
            server_mod, "_ensure_daemon_running", lambda root: calls["ensure"].append(root)
        )
        monkeypatch.setattr(
            server_mod, "_start_ping_thread", lambda root: calls["ping"].append(root)
        )
        monkeypatch.setattr(server_mod.mcp, "run", lambda: None)
        return calls

    def test_the_ping_thread_starts_without_an_index(self, monkeypatch, tmp_path: Path):
        """This is the reported defect: no index at startup, thus no daemon.

        The ping thread is the only recovery path, because ``main()`` runs
        once.  Without the thread, the server stays without a watcher for its
        full life, even after the operator creates the index.
        """
        from fw_context_mcp.mcp import server as server_mod

        calls = self._patch_main(monkeypatch, tmp_path)
        server_mod.main()

        assert calls["ping"] == [tmp_path]
        # No index exists yet, thus a direct spawn would only raise.
        assert calls["ensure"] == []

    def test_both_start_when_the_index_exists(self, monkeypatch, tmp_path: Path):
        from fw_context_mcp.mcp import server as server_mod

        db_path = tmp_path / "index" / "pid" / "index.db"
        db_path.parent.mkdir(parents=True)
        db_path.write_bytes(b"")

        calls = self._patch_main(monkeypatch, tmp_path)
        server_mod.main()

        assert calls["ensure"] == [tmp_path]
        assert calls["ping"] == [tmp_path]


# ── Fix 3: `fw-context index` must start the daemon it can have missed ───────


class TestEnsureWatcherAfterIndex:
    """The helper must start the daemon, and must never break the index run."""

    def test_it_starts_the_daemon(self, monkeypatch, tmp_path: Path):
        import fw_context_mcp.mcp.background as background_mod
        from fw_context_mcp.cli._index import _ensure_watcher_after_index

        seen: list[Path] = []
        monkeypatch.setattr(background_mod, "_ensure_daemon_running", seen.append)
        _ensure_watcher_after_index(tmp_path)

        assert seen == [tmp_path]

    @pytest.mark.parametrize("exc", [RuntimeError("not initialized"), OSError("no socket")])
    def test_a_failure_does_not_stop_the_index_run(self, monkeypatch, tmp_path: Path, exc):
        """The index is already written.  Only automatic reindex is lost."""
        import fw_context_mcp.mcp.background as background_mod
        from fw_context_mcp.cli._index import _ensure_watcher_after_index

        def _raise(root: Path) -> None:
            raise exc

        monkeypatch.setattr(background_mod, "_ensure_daemon_running", _raise)
        _ensure_watcher_after_index(tmp_path)  # must not raise


class TestCmdIndexWiring:
    """``cmd_index`` must call the helper on success, and only on success."""

    @staticmethod
    def _patch_cmd_index(monkeypatch, tmp_path: Path, cfg: _FakeCfg) -> list[Path]:
        """Stub every step of ``cmd_index`` except the return-path logic."""
        import fw_context_mcp.config as config_mod
        import fw_context_mcp.indexer.build as build_mod
        import fw_context_mcp.indexer.runner as runner_mod
        import fw_context_mcp.utils as utils_mod
        from fw_context_mcp.cli import _index as index_mod

        cc_path = tmp_path / "compile_commands.json"
        cc_path.write_text("[]", encoding="utf-8")

        monkeypatch.setattr(utils_mod, "resolve_project_root", lambda arg: tmp_path)
        monkeypatch.setattr(config_mod, "load", lambda project_root=None: cfg)
        monkeypatch.setattr(config_mod, "derive_project_id", lambda root: "pid")
        monkeypatch.setattr(build_mod, "detect_build_system", lambda root: "makefile")
        monkeypatch.setattr(index_mod, "_kill_bg_reindex", lambda db_path: None)
        monkeypatch.setattr(index_mod, "_build_run_kwargs", lambda *a, **kw: {})
        monkeypatch.setattr(
            index_mod, "_resolve_compile_commands", lambda *a, **kw: (cc_path, True)
        )
        monkeypatch.setattr(
            index_mod, "_validate_and_fix_artifacts", lambda *a, **kw: (cc_path, [], True)
        )
        monkeypatch.setattr(runner_mod, "run", lambda **kw: "0" * 64)
        monkeypatch.setattr(index_mod, "_post_index_optimize", lambda *a, **kw: None)

        started: list[Path] = []
        monkeypatch.setattr(index_mod, "_ensure_watcher_after_index", started.append)
        return started

    @staticmethod
    def _args() -> SimpleNamespace:
        return SimpleNamespace(
            verbose=False,
            project=None,
            background=False,
            force=False,
            vendor_paths=None,
            project_paths=None,
        )

    def test_a_successful_run_starts_the_daemon(self, monkeypatch, tmp_path: Path):
        from fw_context_mcp.cli._index import cmd_index

        cfg = _FakeCfg(index=_FakeIndexCfg(db_dir=tmp_path / "index"))
        started = self._patch_cmd_index(monkeypatch, tmp_path, cfg)

        assert cmd_index(self._args()) == 0
        assert started == [tmp_path]

    def test_a_failed_validation_does_not_start_the_daemon(self, monkeypatch, tmp_path: Path):
        from fw_context_mcp.cli import _index as index_mod
        from fw_context_mcp.cli._index import cmd_index

        cfg = _FakeCfg(index=_FakeIndexCfg(db_dir=tmp_path / "index"))
        started = self._patch_cmd_index(monkeypatch, tmp_path, cfg)
        monkeypatch.setattr(
            index_mod, "_validate_and_fix_artifacts", lambda *a, **kw: (None, [], False)
        )

        assert cmd_index(self._args()) == 1
        assert started == []

    def test_a_superseded_run_does_not_start_the_daemon(self, monkeypatch, tmp_path: Path):
        """Another run took over.  That run starts the daemon itself."""
        import fw_context_mcp.indexer.runner as runner_mod
        from fw_context_mcp.cli._index import cmd_index

        cfg = _FakeCfg(index=_FakeIndexCfg(db_dir=tmp_path / "index"))
        started = self._patch_cmd_index(monkeypatch, tmp_path, cfg)

        def _superseded(**kw):
            raise runner_mod.IndexSuperseded("a newer run took over")

        monkeypatch.setattr(runner_mod, "run", _superseded)

        assert cmd_index(self._args()) == EXIT_SUPERSEDED
        assert started == []

    def test_a_successful_variant_run_starts_the_daemon(self, monkeypatch, tmp_path: Path):
        from fw_context_mcp.cli import _index as index_mod
        from fw_context_mcp.cli._index import cmd_index

        cfg = _FakeCfg(
            index=_FakeIndexCfg(db_dir=tmp_path / "index"),
            build=_FakeBuildCfg(variants=[object()]),
        )
        started = self._patch_cmd_index(monkeypatch, tmp_path, cfg)
        monkeypatch.setattr(index_mod, "_run_multi", lambda *a, **kw: 0)

        assert cmd_index(self._args()) == 0
        assert started == [tmp_path]

    def test_a_failed_variant_run_does_not_start_the_daemon(self, monkeypatch, tmp_path: Path):
        from fw_context_mcp.cli import _index as index_mod
        from fw_context_mcp.cli._index import cmd_index

        cfg = _FakeCfg(
            index=_FakeIndexCfg(db_dir=tmp_path / "index"),
            build=_FakeBuildCfg(variants=[object()]),
        )
        started = self._patch_cmd_index(monkeypatch, tmp_path, cfg)
        monkeypatch.setattr(index_mod, "_run_multi", lambda *a, **kw: 1)

        assert cmd_index(self._args()) == 1
        assert started == []
