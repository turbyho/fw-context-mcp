"""The macro pass runs clang in the directory of the compilation of each unit.

A relative path in the flags of a unit is relative to the "directory" of
its entry in compile_commands.json.  The pass ran clang in the project
root: a unit of a build that ran in a sub directory then read another
header, or none.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from fw_context_mcp.indexer import _postprocess

pytestmark = pytest.mark.skipif(shutil.which("clang") is None, reason="the preprocessor is clang")


def test_a_relative_include_is_read_from_the_directory_of_the_compilation(tmp_path: Path):
    build = tmp_path / "build"
    (build / "generated").mkdir(parents=True)
    (build / "generated" / "config.h").write_text("#define FROM_BUILD 9\n", encoding="utf-8")
    source = tmp_path / "main.c"
    source.write_text("int main(void) { return FROM_BUILD; }\n", encoding="utf-8")
    unit = SimpleNamespace(
        file=source, directory=build, clang_args=["-include", "generated/config.h"],
    )
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE macros (config_hash TEXT, name TEXT, expanded_value TEXT)")
    conn.execute("INSERT INTO macros VALUES ('h', 'FROM_BUILD', '')")

    _postprocess._step_expand_macros(conn, {"units": [unit], "config_hash": "h", "project_root": tmp_path})

    # clang finds generated/config.h only from the directory of the build.
    assert conn.execute("SELECT expanded_value FROM macros WHERE name = 'FROM_BUILD'").fetchone() == ("9",)
