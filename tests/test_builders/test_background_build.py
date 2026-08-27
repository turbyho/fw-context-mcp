"""Tests for the automatic background build across every build backend.

fw-context starts a build by itself when it finds a source file that
compile_commands.json does not cover.  That build runs while the user works,
possibly while an IDE builds the same project, and fw-context cannot lock the
build of the IDE — the IDE knows nothing about it.

Every backend therefore has to answer one question: may fw-context run this
build on its own?  A backend answers yes only when it writes every artifact
under ``BuildConfig.isolated_build_dir``, or when it compiles nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fw_context_mcp.indexer.build import BuildConfig
from fw_context_mcp.indexer.builders import background_build_safe
from fw_context_mcp.indexer.builders.arduino import ArduinoBuildSystem
from fw_context_mcp.indexer.builders.esp_idf import ESPIDFBuildSystem
from fw_context_mcp.indexer.builders.generic_cmake import GenericCMakeBuildSystem
from fw_context_mcp.indexer.builders.iar import IARBuildSystem
from fw_context_mcp.indexer.builders.keil import KeilBuildSystem
from fw_context_mcp.indexer.builders.makefile import MakefileBuildSystem
from fw_context_mcp.indexer.builders.manual import ManualBuildSystem
from fw_context_mcp.indexer.builders.mbed_os import MbedOSBuildSystem
from fw_context_mcp.indexer.builders.platformio import PlatformIOBuildSystem
from fw_context_mcp.indexer.builders.stubs import STM32CubeIDEStub, TICCSStub
from fw_context_mcp.indexer.builders.zephyr import ZephyrBuildSystem
from fw_context_mcp.utils import resolve_build_dir

# Every backend, with the answer it must give for a default configuration.
# A new backend that reaches the registry without a decision here fails
# test_every_backend_is_covered.
_BACKENDS = [
    (MbedOSBuildSystem, True),      # mbed compile --build <dir>
    (PlatformIOBuildSystem, True),  # PLATFORMIO_BUILD_DIR
    (ZephyrBuildSystem, True),      # west build -d <dir>
    (ArduinoBuildSystem, True),     # arduino-cli --build-path
    (ESPIDFBuildSystem, True),      # idf.py -B <dir>
    (GenericCMakeBuildSystem, True),  # cmake -B <dir>
    (MakefileBuildSystem, True),    # compiledb -n, no compilation by default
    (KeilBuildSystem, True),        # converts a .uvprojx, compiles nothing
    (IARBuildSystem, True),         # converts an .ewp, compiles nothing
    (ManualBuildSystem, True),      # -fsyntax-only, no object file
    (STM32CubeIDEStub, False),      # build() raises
    (TICCSStub, False),             # build() raises
]


@pytest.mark.parametrize(
    ("backend", "expected"),
    _BACKENDS,
    ids=[b.__name__ for b, _ in _BACKENDS],
)
def test_each_backend_declares_its_answer(backend, expected):
    assert background_build_safe(backend(), BuildConfig()) is expected


def test_every_backend_is_covered():
    """A new backend must make a deliberate decision, not inherit a default.

    The classes implement the BuildSystem protocol structurally, thus nothing
    forces a new one to define the method.  This test does.
    """
    from fw_context_mcp.indexer.builders import registry

    registered = {registry.get(key) for key in registry.keys()}
    registered.discard(None)

    undeclared = {
        cls.__name__
        for cls in registered
        if "background_build_safe" not in vars(cls)
    }
    assert not undeclared, (
        f"these backends inherit no decision and would be refused silently: "
        f"{sorted(undeclared)}"
    )

    untested = {cls.__name__ for cls in registered} - {
        cls.__name__ for cls, _ in _BACKENDS
    }
    assert not untested, f"registered but not covered by this test: {sorted(untested)}"


class TestUnknownBackend:
    def test_a_backend_without_the_method_is_refused(self):
        """The safe answer, because such a build could hit the IDE output."""

        class Legacy:
            pass

        assert background_build_safe(Legacy(), BuildConfig()) is False

    def test_no_backend_is_refused(self):
        assert background_build_safe(None, BuildConfig()) is False

    def test_a_raising_backend_is_refused(self):
        class Broken:
            def background_build_safe(self, cfg):
                raise RuntimeError("no idea")

        assert background_build_safe(Broken(), BuildConfig()) is False


class TestMakefileDependsOnDryRun:
    """The makefile backend is the one whose answer depends on the config."""

    def test_dry_run_compiles_nothing_and_is_safe(self):
        cfg = BuildConfig(make_dry_run=True)
        assert background_build_safe(MakefileBuildSystem(), cfg) is True

    def test_a_real_make_build_is_refused(self):
        """The Makefile owns the output directory; fw-context cannot move it."""
        cfg = BuildConfig(make_dry_run=False)
        assert background_build_safe(MakefileBuildSystem(), cfg) is False


class TestResolveBuildDir:
    def test_the_default_wins_without_the_setting(self, tmp_path: Path):
        assert resolve_build_dir(tmp_path, BuildConfig(), "build") == tmp_path / "build"

    def test_a_relative_setting_resolves_against_the_root(self, tmp_path: Path):
        cfg = BuildConfig(isolated_build_dir=".fw-context/autobuild/default")
        assert resolve_build_dir(tmp_path, cfg, "build") == (
            tmp_path / ".fw-context/autobuild/default"
        )

    def test_an_absolute_setting_is_kept(self, tmp_path: Path):
        cfg = BuildConfig(isolated_build_dir="/var/tmp/fwctx")
        assert resolve_build_dir(tmp_path, cfg, "build") == Path("/var/tmp/fwctx")

    def test_a_backend_default_other_than_build_is_kept(self, tmp_path: Path):
        """Zephyr passes its per-variant directory as the default."""
        assert resolve_build_dir(tmp_path, BuildConfig(), "build/nrf52840") == (
            tmp_path / "build/nrf52840"
        )
