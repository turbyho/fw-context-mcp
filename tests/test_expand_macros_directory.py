"""The macro pass runs clang in the directory of the compilation of each unit.

A relative path in the flags of a unit is relative to the "directory" of
its entry in compile_commands.json.  The pass ran clang in the project
root: a unit of a build that ran in a sub directory then read another
header, or none.
"""

from __future__ import annotations

from pathlib import Path

from fw_context_mcp.indexer import macros
from tests._toolchains import unit_of


def test_a_relative_include_is_read_from_the_directory_of_the_compilation(tmp_path: Path, toolchain):
    build = tmp_path / "build"
    (build / "generated").mkdir(parents=True)
    (build / "generated" / "config.h").write_text("#define FROM_BUILD 9\n", encoding="utf-8")
    source = tmp_path / "main.c"
    source.write_text("int main(void) { return FROM_BUILD; }\n", encoding="utf-8")
    unit = unit_of(source, toolchain, ["-include", "generated/config.h"], directory=build)

    definitions = macros._answer_of_unit(unit).definitions

    # clang finds generated/config.h only from the directory of the build.
    assert definitions["FROM_BUILD"].value == "9"
