"""Tests for the build that fw-context starts on its own.

A source file that the build system never saw has no translation unit: it is
absent from compile_commands.json, and only a build writes it there.  A plain
reindex skips it and reports success, thus fw-context runs the build itself.

Two things guard that build:

* The backend must be able to build without touching the output of the build
  that the user runs — see ``builders.background_build_safe``.
* ``--background`` keeps refusing ``--build``, except for this one case.

A failure does not block the next build for a time.  The failed run writes
its text for each MCP answer (``autobuild.record_problem``), and the user
repairs the build when the answers say so.
"""

from __future__ import annotations

import time
from pathlib import Path

from fw_context_mcp.indexer.autobuild import (
    PROBLEM_MARKER,
    BuildProblem,
    clear_problem,
    read_problem,
    record_problem,
)
from fw_context_mcp.utils import AUTOBUILD_REL, autobuild_dir


class TestAutobuildDir:
    def test_the_single_build_case_has_a_name(self):
        assert autobuild_dir() == str(AUTOBUILD_REL / "default")

    def test_each_variant_gets_its_own_directory(self):
        """Variants build into separate directories and must stay separate.

        One shared directory would make the variants overwrite each other.
        """
        assert autobuild_dir("nrf52840") != autobuild_dir("nrf5340")
        assert autobuild_dir("nrf52840") == str(AUTOBUILD_REL / "nrf52840")

    def test_it_lives_under_the_gitignored_directory(self):
        """`fw-context init` adds .fw-context to .gitignore."""
        assert autobuild_dir("x").startswith(".fw-context/")


class TestProblemMarker:
    """The text of the last index run that stopped, for each MCP answer."""

    def test_no_marker_reads_as_no_problem(self, tmp_path: Path):
        assert read_problem(tmp_path) is None

    def test_a_round_trip_keeps_the_text_and_the_kind(self, tmp_path: Path):
        record_problem(tmp_path, "the build directory /x does not exist.\n", build_missing=True)

        assert read_problem(tmp_path) == BuildProblem(
            text="the build directory /x does not exist.", build_missing=True
        )

    def test_a_failed_run_is_not_a_missing_build(self, tmp_path: Path):
        """Only a missing build can go out of date when the build comes back."""
        record_problem(tmp_path, "The last index run failed (exit code 1).")

        problem = read_problem(tmp_path)
        assert problem is not None and problem.build_missing is False

    def test_a_missing_directory_is_made(self, tmp_path: Path):
        """The first run of a project can stop before the index directory exists."""
        record_problem(tmp_path / "index" / "pid", "text")

        problem = read_problem(tmp_path / "index" / "pid")
        assert problem is not None and problem.text == "text"

    def test_a_run_that_ends_well_clears_it(self, tmp_path: Path):
        record_problem(tmp_path, "text")
        clear_problem(tmp_path)

        assert not (tmp_path / PROBLEM_MARKER).exists()

    def test_clearing_a_missing_marker_is_quiet(self, tmp_path: Path):
        clear_problem(tmp_path)  # must not raise

    def test_a_damaged_marker_reads_as_no_problem(self, tmp_path: Path):
        """A warning must never fail the answer that carries it."""
        (tmp_path / PROBLEM_MARKER).write_bytes(b"\xff\xfe\x00broken")
        assert read_problem(tmp_path) is None

        (tmp_path / PROBLEM_MARKER).write_text('{"text": 3}', encoding="utf-8")
        assert read_problem(tmp_path) is None


class TestBackgroundBuildGate:
    """``--background`` refuses ``--build``, except for the automatic case."""

    @staticmethod
    def _cfg(isolated: str | None):
        from fw_context_mcp.indexer.build import BuildConfig

        class _Cfg:
            def __init__(self) -> None:
                self.build = BuildConfig(isolated_build_dir=isolated)
                self.index = None

        return _Cfg()

    @staticmethod
    def _args():
        from types import SimpleNamespace

        return SimpleNamespace(build=True, compile_commands=None, no_clean=False)

    def test_a_plain_background_build_is_refused(self, tmp_path: Path, capsys):
        from fw_context_mcp.cli._index import _resolve_compile_commands

        result = _resolve_compile_commands(
            self._args(), tmp_path, self._cfg(None), "makefile", True
        )

        assert result == (None, False)
        assert "mutually exclusive" in capsys.readouterr().err

    def test_an_isolated_background_build_passes_the_gate(self, tmp_path: Path, monkeypatch):
        """With an isolated directory the two builds cannot meet."""
        import fw_context_mcp.indexer.build as build_mod

        marker = tmp_path / "generated.json"
        marker.write_text("[]", encoding="utf-8")
        monkeypatch.setattr(
            build_mod, "generate_compile_commands", lambda root, cfg: marker
        )

        result = _resolve_compile_commands_with(
            self._args(), tmp_path, self._cfg(autobuild_dir()), "makefile", True
        )

        assert result == (marker, False), "the gate must not stop the automatic build"


def _resolve_compile_commands_with(args, root, cfg, system, bg):
    """Call the resolver, keeping the import local to the patched module."""
    from fw_context_mcp.cli._index import _resolve_compile_commands

    return _resolve_compile_commands(args, root, cfg, system, bg)


class TestAutobuildState:
    """The two answers that decide both the status and the wording."""

    @staticmethod
    def _cfg(isolated: str | None = ".fw-context/autobuild/default"):
        from fw_context_mcp.indexer.build import BuildConfig

        return BuildConfig(isolated_build_dir=isolated)

    def test_a_backend_that_isolates_will_build(self):
        from fw_context_mcp.indexer.autobuild import AutobuildState, state
        from fw_context_mcp.indexer.builders.mbed_os import MbedOSBuildSystem

        assert state(MbedOSBuildSystem, self._cfg()) is AutobuildState.WILL_BUILD

    def test_a_backend_that_compiles_without_isolation_is_unsupported(self):
        """makefile compiles for real once the dry run is off."""
        from fw_context_mcp.indexer.autobuild import AutobuildState, state
        from fw_context_mcp.indexer.build import BuildConfig
        from fw_context_mcp.indexer.builders.makefile import MakefileBuildSystem

        cfg = BuildConfig(make_dry_run=False)

        assert state(MakefileBuildSystem, cfg) is AutobuildState.UNSUPPORTED

    def test_no_backend_is_unsupported(self):
        """Without a build system nothing can build on its own."""
        from fw_context_mcp.indexer.autobuild import AutobuildState, state

        assert state(None, self._cfg()) is AutobuildState.UNSUPPORTED


class TestExcludedMarker:
    """The record that stops a file the build system refuses being reported."""

    def test_a_round_trip_keeps_the_mapping(self, tmp_path: Path):
        from fw_context_mcp.indexer.autobuild import load_excluded, record_excluded

        record_excluded(tmp_path, {"src/a.c": "hash-a", "src/b.c": "hash-b"})

        assert load_excluded(tmp_path) == {"src/a.c": "hash-a", "src/b.c": "hash-b"}

    def test_an_empty_mapping_removes_the_marker(self, tmp_path: Path):
        """Everything it named is covered now; an empty file would only linger."""
        from fw_context_mcp.indexer.autobuild import (
            EXCLUDED_MARKER,
            load_excluded,
            record_excluded,
        )

        record_excluded(tmp_path, {"src/a.c": "hash-a"})
        record_excluded(tmp_path, {})

        assert not (tmp_path / EXCLUDED_MARKER).exists()
        assert load_excluded(tmp_path) == {}

    def test_a_damaged_marker_reads_as_empty(self, tmp_path: Path):
        from fw_context_mcp.indexer.autobuild import EXCLUDED_MARKER, load_excluded

        (tmp_path / EXCLUDED_MARKER).write_text("[1, 2, 3]", encoding="utf-8")

        assert load_excluded(tmp_path) == {}

    def test_a_missing_marker_reads_as_empty(self, tmp_path: Path):
        from fw_context_mcp.indexer.autobuild import load_excluded

        assert load_excluded(tmp_path) == {}


class TestPlanAutoBuild:
    """The planner refuses every case where a build is not safe or not needed."""

    @staticmethod
    def _cfg(system: str):
        from fw_context_mcp.indexer.build import BuildConfig

        class _Cfg:
            def __init__(self) -> None:
                self.build = BuildConfig(system=system)

        return _Cfg()

    def test_no_index_means_no_build(self, tmp_path: Path):
        from fw_context_mcp.cli._index import _plan_auto_build

        missing_db = tmp_path / "index" / "index.db"

        assert _plan_auto_build(
            tmp_path, missing_db, self._cfg("makefile"), None, background=True
        ) == ([], None, "")

    def test_a_backend_that_cannot_isolate_is_refused(self, tmp_path: Path):
        """stm32cubeide cannot build at all, thus an attempt only wastes a run."""
        from fw_context_mcp.cli._index import _plan_auto_build

        db = tmp_path / "index.db"
        db.write_text("", encoding="utf-8")

        assert _plan_auto_build(
            tmp_path, db, self._cfg("stm32cubeide"), None, background=True
        ) == ([], None, "")

    def test_an_unknown_build_system_is_refused(self, tmp_path: Path):
        from fw_context_mcp.cli._index import _plan_auto_build

        db = tmp_path / "index.db"
        db.write_text("", encoding="utf-8")

        assert _plan_auto_build(
            tmp_path, db, self._cfg("no-such-system"), None, background=True
        ) == ([], None, "")

    def test_the_backend_is_asked_with_the_isolated_directory(
        self, tmp_path: Path, monkeypatch
    ):
        """protocol.py lets the answer depend on cfg.isolated_build_dir.

        The question must therefore carry the value the build would really
        use.  It used to be asked with the untouched cfg, where the field is
        still None, so a backend that answered on it would answer wrongly.
        """
        from fw_context_mcp.cli import _index

        seen: list[object] = []

        def _spy(builder, cfg):
            seen.append(cfg.isolated_build_dir)
            return False  # stop early; the recorded value is the point

        # _plan_auto_build imports the helper inside the function body, thus
        # patching the module attribute reaches the call.
        monkeypatch.setattr(
            "fw_context_mcp.indexer.builders.background_build_safe", _spy
        )

        db = tmp_path / "index.db"
        db.write_text("", encoding="utf-8")
        _index._plan_auto_build(tmp_path, db, self._cfg("makefile"), None, background=True)

        assert seen == [".fw-context/autobuild/default"]

    def test_a_refusal_leaves_the_config_untouched(self, tmp_path: Path):
        """The planner must not set an isolated directory it never used."""
        from fw_context_mcp.cli._index import _plan_auto_build

        db = tmp_path / "index.db"
        db.write_text("", encoding="utf-8")
        cfg = self._cfg("stm32cubeide")

        _plan_auto_build(tmp_path, db, cfg, None, background=True)

        assert cfg.build.isolated_build_dir is None


class TestTheProblemOfAMultiRun:
    """The exit of a multi-variant run decides the MCP warning.

    A run that another run took over (EXIT_SUPERSEDED), or that a SIGTERM
    stopped, did not fail: the run that follows decides.  A failed run
    writes its text, and a run that ends well removes it.
    """

    @staticmethod
    def _run(
        monkeypatch, tmp_path: Path, multi_exit: int, *, sigterm_self: bool = False,
        error: Exception | None = None,
    ) -> list[str]:
        """Run ``cmd_index`` with a multi run that ends in *multi_exit*.

        With *sigterm_self* the multi run gets a SIGTERM that no index run
        sent, the way a CI timeout stops it, and *multi_exit* is the exit
        code that ``cmd_index`` must give.  With *error* the multi run
        raises it, and ``cmd_index`` must raise it again.

        Returns the texts that ``autobuild.record_problem`` got, and
        "cleared" for each ``autobuild.clear_problem``.
        """
        from dataclasses import dataclass, field
        from types import SimpleNamespace

        import fw_context_mcp.config as config_mod
        import fw_context_mcp.indexer.build as build_mod
        import fw_context_mcp.utils as utils_mod
        from fw_context_mcp.cli import _index as index_mod

        @dataclass
        class _Index:
            db_dir: Path
            vendor_paths: list = field(default_factory=list)
            project_paths: list = field(default_factory=list)

        @dataclass
        class _Build:
            system: str | None = "zephyr"
            variants: list = field(default_factory=lambda: ["one"])

        @dataclass
        class _Cfg:
            index: _Index
            build: _Build = field(default_factory=_Build)
            cache_server: None = None

        cfg = _Cfg(index=_Index(db_dir=tmp_path / "index"))
        recorded: list[str] = []

        monkeypatch.setattr(utils_mod, "resolve_project_root", lambda arg: tmp_path)
        monkeypatch.setattr(config_mod, "load", lambda project_root=None: cfg)
        monkeypatch.setattr(config_mod, "derive_project_id", lambda root: "pid")
        monkeypatch.setattr(build_mod, "detect_build_system", lambda root: "zephyr")
        monkeypatch.setattr(
            index_mod, "_plan_auto_build",
            lambda *a, **kw: (["src/new.c"], cfg.build, "a new source file"),
        )
        monkeypatch.setattr(index_mod, "_build_run_kwargs", lambda *a, **kw: {})
        def _multi(*a, **kw) -> int:
            if sigterm_self:
                import os
                import signal

                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(5)  # the handler raises before this ends
                raise AssertionError("the SIGTERM handler did not stop the run")
            if error is not None:
                raise error
            return multi_exit

        monkeypatch.setattr(index_mod, "_run_multi", _multi)
        monkeypatch.setattr(
            index_mod.autobuild, "record_problem",
            lambda db_dir, text, **kw: recorded.append(text),
        )
        monkeypatch.setattr(
            index_mod.autobuild, "clear_problem",
            lambda db_dir: recorded.append("cleared"),
        )
        monkeypatch.setattr(index_mod, "_ensure_watcher_after_index", lambda root: None)

        args = SimpleNamespace(
            verbose=False, project=None, background=True, build=False,
            no_clean=False, force=False, takeover=False,
            vendor_paths=None, project_paths=None,
        )
        if error is None:
            assert index_mod.cmd_index(args) == multi_exit
        else:
            import pytest

            with pytest.raises(type(error)):
                index_mod.cmd_index(args)
        return recorded

    def test_a_superseded_run_records_no_problem(self, monkeypatch, tmp_path: Path):
        from fw_context_mcp.exit_codes import EXIT_SUPERSEDED

        assert self._run(monkeypatch, tmp_path, EXIT_SUPERSEDED) == []

    def test_a_terminated_run_exits_143_and_records_no_problem(
        self, monkeypatch, tmp_path: Path, capsys
    ):
        """A foreign SIGTERM is not a takeover and not a failed run."""
        from fw_context_mcp.exit_codes import EXIT_TERMINATED

        assert self._run(monkeypatch, tmp_path, EXIT_TERMINATED, sigterm_self=True) == []
        assert "Terminated:" in capsys.readouterr().err

    def test_a_failed_run_records_where_its_error_is(self, monkeypatch, tmp_path: Path):
        """A background run has no terminal, thus the text names reindex.log."""
        recorded = self._run(monkeypatch, tmp_path, 1)

        assert len(recorded) == 1
        assert "The last index run failed (exit code 1)" in recorded[0]
        assert str(tmp_path / "index" / "pid" / "reindex.log") in recorded[0]

    def test_a_run_that_ends_well_clears_the_problem(self, monkeypatch, tmp_path: Path):
        assert self._run(monkeypatch, tmp_path, 0) == ["cleared"]

    def test_an_exception_records_the_problem_and_goes_on(self, monkeypatch, tmp_path: Path):
        """cli/__init__ makes the exit 1 of it; the MCP answers must say so too."""
        recorded = self._run(
            monkeypatch, tmp_path, 1, error=RuntimeError("config_header not found")
        )

        assert len(recorded) == 1
        assert "RuntimeError: config_header not found" in recorded[0]
