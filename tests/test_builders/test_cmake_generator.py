"""The generic CMake backend stops when the generator writes no compile_commands.json.

CMake implements CMAKE_EXPORT_COMPILE_COMMANDS only in the Makefile and
Ninja generators, and they write the database when CMake generates the
build system.  Without ``-G`` CMake on Windows takes a Visual Studio
generator when Visual Studio is installed: the build ran to its end, and
then the backend said "Ensure CMAKE_EXPORT_COMPILE_COMMANDS is enabled",
which was enabled.  The backend now checks the database after the
configure step, names the generator, and removes the build tree.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import fw_context_mcp.indexer.builders.generic_cmake as mod
from fw_context_mcp.indexer.build import BuildConfig

_WRITES_DATABASE = {"Ninja", "Unix Makefiles", "NMake Makefiles", "MinGW Makefiles"}


class _Result:
    returncode = 0
    stdout = ""
    stderr = ""


def _run(monkeypatch, generator: str | None, *, database: bool) -> list[list[str]]:
    """Fake CMake: configure records *generator*, and writes the database when *database*."""
    calls: list[list[str]] = []

    def fake_run(cmd, cwd=None, description="", env=None, build_cfg=None, timeout=None, **kw):
        command = [str(c) for c in cmd]
        calls.append(command)
        if "-B" in command:
            build_dir = Path(command[command.index("-B") + 1])
            build_dir.mkdir(parents=True, exist_ok=True)
            if generator is not None:
                (build_dir / "CMakeCache.txt").write_text(
                    f"CMAKE_GENERATOR:INTERNAL={generator}\nCMAKE_GENERATOR_INSTANCE:INTERNAL=\n", encoding="utf-8"
                )
            if database:
                (build_dir / "compile_commands.json").write_text("[]", encoding="utf-8")
        return _Result()

    monkeypatch.setattr(mod, "run_build_command", fake_run)
    monkeypatch.setattr(shutil, "which", lambda name, path=None: f"/usr/bin/{name}")
    return calls


@pytest.mark.parametrize("generator", ["Visual Studio 17 2022", "Xcode"])
def test_a_generator_without_a_database_stops_before_the_build(tmp_path: Path, monkeypatch, generator: str):
    calls = _run(monkeypatch, generator, database=False)

    with pytest.raises(RuntimeError, match=r'(?s)wrote no compile_commands\.json.*cmake_generator = "Ninja"') as error:
        mod.GenericCMakeBuildSystem().build(tmp_path, BuildConfig(clean=False))

    assert repr(generator) in str(error.value)
    assert [c for c in calls if "--build" in c] == []
    # A next run with cmake_generator in the same tree would fail in CMake.
    assert not (tmp_path / ".fw-context" / "build" / "default" / "out").exists()


@pytest.mark.parametrize("generator", sorted(_WRITES_DATABASE))
def test_a_generator_with_a_database_builds(tmp_path: Path, monkeypatch, generator: str):
    calls = _run(monkeypatch, generator, database=True)

    result = mod.GenericCMakeBuildSystem().build(tmp_path, BuildConfig())

    assert result.name == "compile_commands.json"
    assert [c for c in calls if "--build" in c]


def test_no_cache_names_no_generator(tmp_path: Path, monkeypatch):
    _run(monkeypatch, None, database=False)

    with pytest.raises(RuntimeError, match=r"'\(unknown\)'"):
        mod.GenericCMakeBuildSystem().build(tmp_path, BuildConfig())
