"""The index keeps the error diagnostics of the parse of each unit.

A parse with errors can hide code: after a fatal error clang stops and
instantiates no template.  Nothing recorded that before, thus a degraded
index looked healthy.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fw_context_mcp.indexer.db import open_db
from tests._toolchains import Toolchain, unit_of, write_compile_commands, write_sources


def _unit(tmp_path: Path, name: str, text: str, toolchain: Toolchain, broken: str | None = None):
    """The unit of *name*; *broken* is the text of the error that the compiler must give for it."""
    write_sources(tmp_path, {name: text})
    return unit_of(tmp_path / name, toolchain, ["-std=gnu11"], broken=broken)


@pytest.mark.libclang
class TestTheParseCountsItsErrors:
    def test_a_clean_unit_has_no_error(self, tmp_path, toolchain):
        from fw_context_mcp.indexer.symbols import extract_all

        result = extract_all(_unit(tmp_path, "ok.c", "int f(void) { return 1; }\n", toolchain))
        assert (result.parse_errors, result.parse_fatal, result.parse_first_error) == (0, False, "")

    def test_an_error_is_counted_with_its_file_and_line(self, tmp_path, toolchain):
        from fw_context_mcp.indexer.symbols import extract_all

        text = "int f(void) {\n    return missing_name;\n}\n"
        result = extract_all(_unit(tmp_path, "bad.c", text, toolchain, broken="missing_name"))
        assert result.parse_errors == 1
        assert result.parse_fatal is False
        assert "missing_name" in result.parse_first_error
        assert result.parse_first_error.endswith("(bad.c:2)")

    def test_a_missing_include_is_fatal(self, tmp_path, toolchain):
        from fw_context_mcp.indexer.symbols import extract_all

        text = '#include "not_there.h"\nint f(void) { return 1; }\n'
        result = extract_all(_unit(tmp_path, "inc.c", text, toolchain, broken="not_there.h"))
        assert result.parse_fatal is True
        assert "not_there.h" in result.parse_first_error


def _index(
    tmp_path: Path, toolchain: Toolchain, broken: str = "int broken(void) {\n    return missing_name;\n}\n",
) -> tuple[Path, str]:
    """Index a project of a clean unit and the unit *broken*, and return the database and the build."""
    from fw_context_mcp.indexer.runner import run

    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    write_sources(project, {"good.c": "int good(void) { return 1; }\n", "broken.c": broken})
    entries = [
        {"directory": str(project), "file": name, "arguments": toolchain.arguments(name, "-std=gnu11", output=f"{name}.o")}
        for name in ("good.c", "broken.c")
    ]
    write_compile_commands(project / "compile_commands.json", entries, broken={"broken.c": "missing_name"})
    db_path = tmp_path / "db" / "index.db"
    db_path.parent.mkdir(exist_ok=True)
    config_hash = run(
        compile_commands=project / "compile_commands.json", db_path=db_path, project_root=project,
        project_id="diag", project_name="diag", index_refs=True, record_git=False,
    )
    return db_path, config_hash


def _stored(db_path: Path, config_hash: str) -> dict[str, tuple[int, int, str]]:
    conn = open_db(db_path)
    try:
        rows = {
            r["path"]: (r["parse_errors"], r["parse_fatal"], r["parse_first_error"])
            for r in conn.execute(
                "SELECT path, parse_errors, parse_fatal, parse_first_error FROM files WHERE config_hash = ?",
                (config_hash,),
            )
        }
    finally:
        conn.close()
    return rows


@pytest.mark.libclang
def test_the_index_run_stores_the_errors_in_the_row_of_the_unit(tmp_path: Path, toolchain: Toolchain):
    rows = _stored(*_index(tmp_path, toolchain))

    assert rows["good.c"] == (0, 0, "")
    errors, fatal, first = rows["broken.c"]
    assert (errors, fatal) == (1, 0)
    assert first.endswith("(broken.c:2)")


@pytest.mark.libclang
def test_an_unchanged_unit_keeps_the_values_of_its_last_parse(tmp_path: Path, toolchain: Toolchain):
    """The status counts each unit, also after a run that parsed only the changed ones.

    The marker in the row shows that the second run did not parse the
    unit again: a parse writes its own text there.
    """
    db_path, config_hash = _index(tmp_path, toolchain)
    conn = open_db(db_path)
    try:
        conn.execute(
            "UPDATE files SET parse_first_error = 'marker' WHERE config_hash = ? AND path = 'broken.c'",
            (config_hash,),
        )
        conn.commit()
    finally:
        conn.close()

    db_path, again = _index(tmp_path, toolchain)

    assert again == config_hash
    assert _stored(db_path, config_hash)["broken.c"] == (1, 0, "marker")

