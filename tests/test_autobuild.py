"""Tests for the build that fw-context starts on its own.

A source file that the build system never saw has no translation unit: it is
absent from compile_commands.json, and only a build writes it there.  A plain
reindex skips it and reports success, thus fw-context runs the build itself.

Three things guard that build:

* The backend must be able to build without touching the output of the build
  that the user runs — see ``builders.background_build_safe``.
* ``--background`` keeps refusing ``--build``, except for this one case.
* A failure must not repeat on every daemon cycle.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from fw_context_mcp.indexer.autobuild import blocked as _autobuild_blocked
from fw_context_mcp.indexer.autobuild import clear_failure as _clear_autobuild_failure
from fw_context_mcp.indexer.autobuild import record_failure as _record_autobuild_failure
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


class TestFailureBackoff:
    """A failed build leaves compile_commands.json untouched.

    The trigger therefore stays armed, and without a marker the next daemon
    cycle would run the same failing build again.
    """

    def test_nothing_is_blocked_without_a_marker(self, tmp_path: Path):
        assert _autobuild_blocked(tmp_path, ["src/a.c"]) is False

    def test_the_same_file_list_is_blocked(self, tmp_path: Path):
        _record_autobuild_failure(tmp_path, ["src/a.c", "src/b.c"])

        assert _autobuild_blocked(tmp_path, ["src/a.c", "src/b.c"]) is True

    def test_a_different_file_list_is_allowed(self, tmp_path: Path):
        """The tree moved on, thus the next attempt is worth one more try."""
        _record_autobuild_failure(tmp_path, ["src/a.c"])

        assert _autobuild_blocked(tmp_path, ["src/a.c", "src/c.c"]) is False

    def test_the_block_expires(self, tmp_path: Path):
        _record_autobuild_failure(tmp_path, ["src/a.c"])
        marker = tmp_path / "autobuild.failed"
        old = time.time() - 3600  # older than the backoff window
        marker.write_text(f"{old}\nsrc/a.c", encoding="utf-8")

        assert _autobuild_blocked(tmp_path, ["src/a.c"]) is False

    def test_a_success_clears_the_block(self, tmp_path: Path):
        _record_autobuild_failure(tmp_path, ["src/a.c"])
        _clear_autobuild_failure(tmp_path)

        assert _autobuild_blocked(tmp_path, ["src/a.c"]) is False

    def test_clearing_a_missing_marker_is_quiet(self, tmp_path: Path):
        _clear_autobuild_failure(tmp_path)  # must not raise

    def test_a_damaged_marker_does_not_block(self, tmp_path: Path):
        """An unreadable marker must not disable the build for ever."""
        (tmp_path / "autobuild.failed").write_text("not-a-timestamp\n", encoding="utf-8")

        assert _autobuild_blocked(tmp_path, ["src/a.c"]) is False


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
    """The three answers that decide both the status and the wording."""

    @staticmethod
    def _cfg(isolated: str | None = ".fw-context/autobuild/default"):
        from fw_context_mcp.indexer.build import BuildConfig

        return BuildConfig(isolated_build_dir=isolated)

    def test_a_backend_that_isolates_will_build(self, tmp_path: Path):
        from fw_context_mcp.indexer.autobuild import AutobuildState, state
        from fw_context_mcp.indexer.builders.mbed_os import MbedOSBuildSystem

        assert state(
            MbedOSBuildSystem, self._cfg(), tmp_path, ["src/a.c"]
        ) is AutobuildState.WILL_BUILD

    def test_a_backend_that_compiles_without_isolation_is_unsupported(
        self, tmp_path: Path
    ):
        """makefile compiles for real once the dry run is off."""
        from fw_context_mcp.indexer.autobuild import AutobuildState, state
        from fw_context_mcp.indexer.build import BuildConfig
        from fw_context_mcp.indexer.builders.makefile import MakefileBuildSystem

        cfg = BuildConfig(make_dry_run=False)

        assert state(
            MakefileBuildSystem, cfg, tmp_path, ["src/a.c"]
        ) is AutobuildState.UNSUPPORTED

    def test_no_backend_is_unsupported(self, tmp_path: Path):
        """Without a build system nothing can build on its own."""
        from fw_context_mcp.indexer.autobuild import AutobuildState, state

        assert state(
            None, self._cfg(), tmp_path, ["src/a.c"]
        ) is AutobuildState.UNSUPPORTED

    def test_a_fresh_failure_for_the_same_files_is_backoff(self, tmp_path: Path):
        from fw_context_mcp.indexer.autobuild import (
            AutobuildState,
            record_failure,
            state,
        )
        from fw_context_mcp.indexer.builders.mbed_os import MbedOSBuildSystem

        record_failure(tmp_path, ["src/a.c"])

        assert state(
            MbedOSBuildSystem, self._cfg(), tmp_path, ["src/a.c"]
        ) is AutobuildState.BACKOFF

    def test_a_failure_for_other_files_does_not_block(self, tmp_path: Path):
        from fw_context_mcp.indexer.autobuild import (
            AutobuildState,
            record_failure,
            state,
        )
        from fw_context_mcp.indexer.builders.mbed_os import MbedOSBuildSystem

        record_failure(tmp_path, ["src/a.c"])

        assert state(
            MbedOSBuildSystem, self._cfg(), tmp_path, ["src/b.c"]
        ) is AutobuildState.WILL_BUILD


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

        assert _plan_auto_build(tmp_path, missing_db, self._cfg("makefile"), None) == ([], None)

    def test_a_backend_that_cannot_isolate_is_refused(self, tmp_path: Path):
        """stm32cubeide cannot build at all, thus an attempt only wastes a run."""
        from fw_context_mcp.cli._index import _plan_auto_build

        db = tmp_path / "index.db"
        db.write_text("", encoding="utf-8")

        assert _plan_auto_build(tmp_path, db, self._cfg("stm32cubeide"), None) == ([], None)

    def test_an_unknown_build_system_is_refused(self, tmp_path: Path):
        from fw_context_mcp.cli._index import _plan_auto_build

        db = tmp_path / "index.db"
        db.write_text("", encoding="utf-8")

        assert _plan_auto_build(tmp_path, db, self._cfg("no-such-system"), None) == ([], None)

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
        _index._plan_auto_build(tmp_path, db, self._cfg("makefile"), None)

        assert seen == [".fw-context/autobuild/default"]

    def test_a_refusal_leaves_the_config_untouched(self, tmp_path: Path):
        """The planner must not set an isolated directory it never used."""
        from fw_context_mcp.cli._index import _plan_auto_build

        db = tmp_path / "index.db"
        db.write_text("", encoding="utf-8")
        cfg = self._cfg("stm32cubeide")

        _plan_auto_build(tmp_path, db, cfg, None)

        assert cfg.build.isolated_build_dir is None
