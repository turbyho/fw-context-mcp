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

import json
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


class TestManualDependencyFiles:
    """The manual backend must not write ``.d`` files into the source tree.

    ``-fsyntax-only`` writes no object file, but ``-MD -MF`` writes one
    dependency file per source.  Beside the source they sit where the build
    of the user reads them, and the compiler does not write them atomically,
    thus a concurrent ``make`` can read a truncated file.
    """

    @staticmethod
    def _run(tmp_path: Path, monkeypatch, cfg: BuildConfig, extra: str = "") -> list[list[str]]:
        """Build a tiny project and return the commands the backend issued."""
        import shutil as _shutil

        src = tmp_path / "src"
        src.mkdir(parents=True)
        (src / "main.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
        if extra:
            nested = tmp_path / extra
            nested.parent.mkdir(parents=True, exist_ok=True)
            nested.write_text("int f(void) { return 1; }\n", encoding="utf-8")

        commands: list[list[str]] = []

        def _fake_run(cmd, **kwargs):
            commands.append(list(cmd))

        monkeypatch.setattr(
            "fw_context_mcp.indexer.builders.manual.run_build_command", _fake_run
        )
        # The backend refuses to start without a compiler on PATH.
        monkeypatch.setattr(_shutil, "which", lambda name: f"/usr/bin/{name}")

        ManualBuildSystem().generate(tmp_path, cfg)
        return commands

    @staticmethod
    def _dep_targets(commands: list[list[str]]) -> list[Path]:
        return [Path(cmd[cmd.index("-MF") + 1]) for cmd in commands if "-MF" in cmd]

    def test_no_dependency_file_is_aimed_at_the_source_tree(
        self, tmp_path: Path, monkeypatch
    ):
        """Assert on the -MF target, not on files on disk.

        run_build_command is mocked, thus no compiler runs and no .d appears
        either way — a glob over src/ would pass against any implementation.
        What the backend ASKS for is the thing under test.
        """
        cfg = BuildConfig(system="bare", source_dirs=["src"])

        targets = self._dep_targets(self._run(tmp_path, monkeypatch, cfg))

        assert targets, "the backend must still emit dependency files"
        for target in targets:
            assert (tmp_path / "src") not in target.parents, (
                f"{target} sits in the source tree of the user"
            )

    def test_the_default_goes_under_the_fw_context_directory(
        self, tmp_path: Path, monkeypatch
    ):
        cfg = BuildConfig(system="bare", source_dirs=["src"])

        targets = self._dep_targets(self._run(tmp_path, monkeypatch, cfg))

        assert targets == [tmp_path / ".fw-context/build/deps/src/main.d"]

    def test_an_isolated_build_wins(self, tmp_path: Path, monkeypatch):
        cfg = BuildConfig(
            system="bare",
            source_dirs=["src"],
            isolated_build_dir=".fw-context/autobuild/default",
        )

        targets = self._dep_targets(self._run(tmp_path, monkeypatch, cfg))

        assert targets == [
            tmp_path / ".fw-context/autobuild/default/src/main.d"
        ]

    def test_two_sources_of_one_name_do_not_collide(self, tmp_path: Path, monkeypatch):
        """`a/foo.c` and `b/foo.c` would share one `foo.d` without mirroring."""
        for sub in ("a", "b"):
            nested = tmp_path / "lib" / sub
            nested.mkdir(parents=True)
            (nested / "foo.c").write_text("int foo(void) { return 0; }\n", encoding="utf-8")
        cfg = BuildConfig(system="bare", source_dirs=["lib"])

        targets = self._dep_targets(self._run(tmp_path, monkeypatch, cfg))

        assert len(targets) == 2
        assert len(set(targets)) == 2, f"the two collided: {targets}"


class TestPlatformIOIsolatesObjectFiles:
    """The invariant the contract actually rests on.

    background_build_safe() promises that an automatic build cannot damage
    the build of the user.  For PlatformIO that means the object files, and
    PLATFORMIO_BUILD_DIR is what secures them — the compilation database in
    the project root is a gitignored artifact and is left alone on purpose
    (see the docstring of platformio.background_build_safe).
    """

    @staticmethod
    def _run(cfg, tmp_path: Path, monkeypatch) -> list[dict]:
        """Return the environment of every pio invocation."""
        import fw_context_mcp.indexer.builders.platformio as mod

        root = tmp_path / "proj"
        (root / "src").mkdir(parents=True)
        (root / "platformio.ini").write_text("[env:x]\n", encoding="utf-8")
        envs: list[dict] = []

        def fake_run(cmd, cwd=None, description="", env=None, build_cfg=None, timeout=None):
            envs.append(dict(env or {}))
            (root / "compile_commands.json").write_text("[]", encoding="utf-8")

            class _Result:
                returncode = 0
                stdout = ""
                stderr = ""

            return _Result()

        monkeypatch.setattr(mod, "run_build_command", fake_run)
        monkeypatch.setattr(mod.shutil, "which", lambda n: f"/usr/bin/{n}")
        mod.PlatformIOBuildSystem().build(root, cfg)
        return envs

    def test_an_isolated_build_redirects_every_call(self, tmp_path: Path, monkeypatch):
        from dataclasses import replace

        from fw_context_mcp.indexer.build import BuildConfig
        from fw_context_mcp.utils import autobuild_dir

        cfg = replace(BuildConfig(), isolated_build_dir=autobuild_dir(), clean=False)
        envs = self._run(cfg, tmp_path, monkeypatch)

        assert envs, "the backend must invoke pio at least once"
        for env in envs:
            assert env.get("PLATFORMIO_BUILD_DIR") == autobuild_dir(), (
                "every pio call has to carry it, or one of them writes into "
                ".pio/build while the user is building there"
            )

    def test_an_explicit_build_does_not_redirect(self, tmp_path: Path, monkeypatch):
        from dataclasses import replace

        from fw_context_mcp.indexer.build import BuildConfig

        envs = self._run(replace(BuildConfig(), clean=False), tmp_path, monkeypatch)

        assert envs
        for env in envs:
            assert "PLATFORMIO_BUILD_DIR" not in env, (
                "the user asked for this build; it belongs in their own "
                "output directory"
            )


class TestObjectPathStaysOutOfTheHashes:
    """Why the project-root compile_commands.json rewrite costs nothing.

    An isolated build differs from the build of the user in one token per
    entry — the `.o` output path.  If that reached config_hash the two
    builds would own two half-populated indexes; if it reached flags_hash
    every translation unit would look changed and be reparsed.  Neither
    happens, and these tests are what keeps it that way.
    """

    @staticmethod
    def _entry(obj: str) -> dict:
        return {
            "directory": "/proj",
            "file": "/proj/src/main.c",
            "arguments": [
                "cc", "-c", "/proj/src/main.c", "-o", obj,
                "-I", "/proj/src", "-DFOO=1", "-std=c11",
            ],
        }

    def test_the_object_path_does_not_reach_flags_hash(self):
        from fw_context_mcp.indexer.config_hash import compute_flags_hash

        user = self._entry(".pio/build/x/main.c.o")
        isolated = self._entry(".fw-context/autobuild/default/x/main.c.o")

        assert compute_flags_hash(user) == compute_flags_hash(isolated), (
            "a differing flags_hash marks the unit changed and reparses it"
        )

    def test_the_object_path_does_not_reach_config_hash(self, tmp_path: Path):
        from fw_context_mcp.indexer.compile_commands import parse as parse_cc
        from fw_context_mcp.indexer.manifest import compute_config_hash

        def _hash(obj: str) -> str:
            cc = tmp_path / f"cc_{obj.count('/')}_{len(obj)}.json"
            cc.write_text(json.dumps([self._entry(obj)]), encoding="utf-8")
            return compute_config_hash(list(parse_cc(cc)), tmp_path, "pid")

        assert _hash(".pio/build/x/main.c.o") == _hash(
            ".fw-context/autobuild/default/x/main.c.o"
        ), "a differing config_hash splits the index into two half-filled sets"
