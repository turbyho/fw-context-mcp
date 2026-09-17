"""A definition that begins and ends on ONE line must reach the index with
its text.

``symbols.source`` is what ``search_bodies`` searches and what ``get_source``
quotes.  Two guards asked for an extent of more than one line — ``_read_body``
itself, and the caller that decides whether to read a body at all — thus every
inline accessor of an embedded header reached the index with an empty body.

Measured on one index before the repair: 1102 definitions with an empty
``source``, and every one of them one line long.  ``get_source`` covered them
by reading the disk, which reports ``source_origin: "disk"`` and holds every
``#if`` branch; ``search_bodies`` could not reach them at all.

``_postprocess.py`` already made the same repair for the call graph
(``_ENCLOSING_CALLABLE_SQL``).  These tests pin the same rule for the text.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.libclang


def _index(root: Path, db_path: Path, files: dict[str, str], main: str) -> list[dict]:
    """Index *files* through the real symbol pass and return the stored rows.

    *files* maps a project-relative path to its text; *main* names the entry
    in it that compile_commands.json points at.  The return is a list, not a
    map: one name has a row for its declaration AND a row for its definition,
    and a map keyed by name would keep only the last of them.
    """
    from fw_context_mcp.indexer.compile_commands import parse as parse_cc
    from fw_context_mcp.indexer.db import (
        open_db,
        transaction,
        upsert_build_config,
        upsert_project,
    )
    from fw_context_mcp.indexer.ops import store_symbols_for_unit

    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    # The standard must match the language of the entry file: `-std=c++17`
    # over a .c file makes libclang refuse the whole unit.
    is_cpp = Path(main).suffix in {".cpp", ".cc", ".cxx"}
    driver = ["c++", "-std=c++17"] if is_cpp else ["cc", "-std=c11"]
    cc = root / "compile_commands.json"
    cc.write_text(
        json.dumps(
            [
                {
                    "directory": str(root),
                    "file": str(root / main),
                    "arguments": [*driver, "-c", str(root / main), "-I", str(root / "src")],
                }
            ]
        ),
        encoding="utf-8",
    )

    conn = open_db(db_path)
    try:
        with transaction(conn):
            upsert_project(conn, "pid", "p", str(root))
            upsert_build_config(conn, "ch", "pid", str(cc))

        unit = next(iter(parse_cc(cc)))
        with transaction(conn):
            store_symbols_for_unit(conn, unit, "ch", root)

        return [
            dict(row)
            for row in conn.execute(
                """SELECT qualified_name, name, line, end_line, is_definition, source
                   FROM symbols WHERE config_hash='ch'"""
            ).fetchall()
        ]
    finally:
        conn.close()


def _definition(rows: list[dict], qualified_name: str) -> dict:
    """Give the one definition row of *qualified_name*, and fail without it."""
    found = [r for r in rows if r["qualified_name"] == qualified_name and r["is_definition"]]
    assert len(found) == 1, f"expected one definition of {qualified_name}, got {len(found)}"
    return found[0]


class TestOneLineDefinitionKeepsItsText:
    def test_inline_accessor_is_stored(self, tmp_path: Path) -> None:
        root = tmp_path / "proj"
        header = (
            "#pragma once\n"                                    # 1
            "class Probe {\n"                                   # 2
            " public:\n"                                        # 3
            "  bool ready() const { return ready_; }\n"          # 4
            "  int slow() const {\n"                            # 5
            "    return 1;\n"                                   # 6
            "  }\n"                                             # 7
            "  void declared_only();\n"                         # 8
            " private:\n"                                       # 9
            "  bool ready_ = false;\n"                          # 10
            "};\n"                                              # 11
        )
        rows = _index(
            root,
            tmp_path / "index.db",
            {
                "src/probe.hpp": header,
                "src/main.cpp": (
                    '#include "probe.hpp"\n'
                    "int main() { Probe p; return p.ready() ? p.slow() : 0; }\n"
                ),
            },
            main="src/main.cpp",
        )

        one_line = _definition(rows, "Probe::ready")
        assert one_line["line"] == one_line["end_line"] == 4
        assert one_line["is_definition"] == 1
        assert one_line["source"] == "  bool ready() const { return ready_; }\n"

    def test_multi_line_definition_is_unchanged(self, tmp_path: Path) -> None:
        # The repair must not move the boundary of a body that already worked.
        root = tmp_path / "proj"
        rows = _index(
            root,
            tmp_path / "index.db",
            {
                "src/probe.hpp": (
                    "#pragma once\n"
                    "class Probe {\n"
                    " public:\n"
                    "  int slow() const {\n"
                    "    return 1;\n"
                    "  }\n"
                    "};\n"
                ),
                "src/main.cpp": '#include "probe.hpp"\nint main() { Probe p; return p.slow(); }\n',
            },
            main="src/main.cpp",
        )
        assert _definition(rows, "Probe::slow")["source"] == (
            "  int slow() const {\n    return 1;\n  }\n"
        )

    def test_declaration_still_stores_no_body(self, tmp_path: Path) -> None:
        # Only a definition holds code.  A one-line DECLARATION must stay
        # empty: its line holds a prototype, and a body stored for it would
        # give search_bodies a hit in code that does not exist.
        root = tmp_path / "proj"
        rows = _index(
            root,
            tmp_path / "index.db",
            {
                "src/api.h": "#pragma once\nint absent(void);\n",
                "src/main.c": '#include "api.h"\nint main(void) { return 0; }\n',
            },
            main="src/main.c",
        )
        declaration = [r for r in rows if r["name"] == "absent"]
        assert declaration, "the header declaration must be in the index"
        assert all(not r["is_definition"] for r in declaration)
        assert all(r["source"] == "" for r in declaration)

    def test_one_line_body_obeys_the_ifdef_filter(self, tmp_path: Path) -> None:
        # A one-line body is filtered like any other.  The branch the build
        # does not take must reach the index blank, never as live code.
        root = tmp_path / "proj"
        header = (
            "#pragma once\n"                          # 1
            "#if defined(TAKEN)\n"                    # 2
            "static inline int pick(void) { return 1; }\n"   # 3
            "#else\n"                                 # 4
            "static inline int pick(void) { return 2; }\n"   # 5
            "#endif\n"                                # 6
        )
        rows = _index(
            root,
            tmp_path / "index.db",
            {
                "src/pick.h": header,
                "src/main.c": '#include "pick.h"\nint main(void) { return pick(); }\n',
            },
            main="src/main.c",
        )
        # TAKEN is not defined, thus line 5 compiles and line 3 does not.
        pick = _definition(rows, "pick")
        assert pick["line"] == 5
        assert pick["source"] == "static inline int pick(void) { return 2; }\n"
