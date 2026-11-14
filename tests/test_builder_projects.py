"""The bare, CMake and Makefile builders: ``fw-context init`` and ``index --build`` over a cross build with the ARM toolchain of the test SDKs.

Each test copies a project of ``tests/builds`` to a temporary directory,
runs ``fw-context init --quick``, sets the build in the config as a user of
a cross toolchain sets it, and runs ``fw-context index --build``.  The
tests read the index: the code of the project, one target for each unit
(the one of the GCC driver), the macro values and the verification of the
headers.

WHY the ARM toolchain of the test SDKs (decision of the user, 2026-11-13):
the builders serve firmware, and a compiler of the host tested a build
that no firmware project has.  The projects have a few files, thus the
tests run in ``make test`` and in each job of CI.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fw_context_mcp.config._toml_editor import set_key
from fw_context_mcp.indexer._run_marker import read_state
from tests import sdk
from tests._projects import (
    IndexedProject,
    copy_project,
    daemon_env,
    definition_kinds,
    expanded_values,
    index_project,
    init_project,
    manifest_verification,
    targets_of_each_unit,
)
from tests._toolchains import Toolchain

_TARGET = "--target=arm-zephyr-eabi"


def _shell() -> dict[str, str]:
    """The environment of the shell of a user of the test SDKs, for init."""
    return sdk.tool_env(sdk.sdk_root(), sdk.load_versions(), None)


def _check_the_index(project: IndexedProject, kinds: dict[str, set[str]], header: tuple[str, str] | None) -> None:
    """The checks that each project of this module passes.

    *kinds* gives the kinds of the definitions of each name of the project:
    ``typedef enum {...} OperationMode`` defines the enum and the typedef.
    *header* is a macro of the project and its file: the macros step gave
    the definition its values.
    """
    assert set(project.builds) == {""}
    assert definition_kinds(project, "", set(kinds)) == kinds
    assert targets_of_each_unit(project, "") == {(_TARGET,)}
    assert read_state(project.db_path.parent, project.builds[""]) is None
    assert manifest_verification(project, "") == "full"
    if header is not None:
        name, file_suffix = header
        assert expanded_values(project, "", name, file_suffix)


def test_bare(tmp_path: Path, arm_toolchain: Toolchain) -> None:
    """The bare builder compiles each file of ``source_dirs`` with ``compiler`` and ``extra_flags``."""
    project = copy_project("bare", tmp_path / "bare")
    init_project(project, _shell())
    config = project / ".fw-context" / "config.toml"
    set_key(config, "build", "system", "bare")
    set_key(config, "build", "source_dirs", ["src"])
    set_key(config, "build", "include_dirs", ["src"])
    set_key(config, "build", "compiler", str(arm_toolchain.driver("main.c")))
    set_key(config, "build", "extra_flags", list(arm_toolchain.flags))

    indexed = index_project(project, daemon_env(sdk.sdk_root()))

    _check_the_index(
        indexed,
        {
            "main": {"function"}, "uart_init": {"function"}, "compute_checksum": {"function"},
            "set_mode": {"function"}, "Config": {"struct"}, "OperationMode": {"enum", "typedef"},
        },
        ("LIB_H", "src/lib.h"),
    )


def test_cmake(tmp_path: Path, arm_toolchain: Toolchain) -> None:
    """CMake with a toolchain file of the project, the compiler in ``CC``, the flags in ``CFLAGS``.

    WHY Ninja: the default generator of CMake on Windows (Visual Studio)
    writes no compile_commands.json.  WHY ``extra_path``: cmake and ninja
    are in the venv of the test SDKs, and a build in the background has
    no PATH of a shell.
    """
    project = copy_project("generic_cmake", tmp_path / "cmake")
    init_project(project, _shell())
    config = project / ".fw-context" / "config.toml"
    set_key(config, "build", "cmake_generator", "Ninja")
    set_key(config, "build", "extra_path", [str(sdk.venv_bin(sdk.sdk_root()))])
    set_key(config, "build", "env", {
        "CC": str(arm_toolchain.driver("main.c")),
        "CFLAGS": " ".join(arm_toolchain.flags),
        "CMAKE_TOOLCHAIN_FILE": str(project / "cmake" / "cross-gcc.cmake"),
    })

    indexed = index_project(project, daemon_env(sdk.sdk_root()))

    _check_the_index(indexed, {"factorial": {"function"}, "main": {"function"}}, None)


@pytest.mark.make
def test_makefile(tmp_path: Path, arm_toolchain: Toolchain) -> None:
    """A Makefile that runs (no dry run) with ``CC``, ``AR`` and ``CPU_FLAGS`` of the toolchain on its command line."""
    project = copy_project("makefile", tmp_path / "makefile")
    init_project(project, _shell())
    config = project / ".fw-context" / "config.toml"
    set_key(config, "build", "system", "makefile")
    set_key(config, "build", "make_dry_run", False)
    set_key(config, "build", "out_dir_var", "BUILD_DIR")
    set_key(config, "build", "make_vars", {
        "CC": str(arm_toolchain.driver("main.c")),
        "AR": str(arm_toolchain.tool("ar")),
        "CPU_FLAGS": " ".join(arm_toolchain.flags),
    })

    indexed = index_project(project, daemon_env(sdk.sdk_root()))

    _check_the_index(
        indexed,
        {
            "register_device": {"function"}, "find_device": {"function"}, "main": {"function"},
            "Device": {"struct", "typedef"},
        },
        None,
    )
