"""From a ``#define`` on disk to what a tool answers with.

``tests/test_macro_function_like.py`` pins the rules of the split.  This
module pins the path AROUND it: the parameter list must survive the insert,
reach the FTS index, and come back out of a tool as a readable invocation.

Before the repair the parameter list lived inside ``macros.value``, glued to
the replacement text.  Splitting it into a column of its own could have lost
it twice over — an insert that drops it, or an FTS table that stops indexing
it — and both losses look like an ordinary empty answer.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.libclang

HEADER = """\
#define BUFFER_SIZE 256
#define MIN(a, b) ((a) < (b) ? (a) : (b))
#define NOARGS() 0
#define SPACED (x)
"""

MAIN = """\
#include "conf.h"
int main(void) { return MIN(BUFFER_SIZE, 1) + NOARGS(); }
"""


PROJECT_ID = "macro-params-001"

# A config hash of its own, and not the ``ch`` that other fixtures use.
# ``did_you_mean._names_cache`` is keyed by config_hash ALONE and lives for
# the process, thus two fixtures that share the literal feed each other's
# candidate names and one of them then finds nothing.  A real build cannot
# collide this way — ``compute_config_hash`` hashes the project root in —
# but a hand-written hash can.
CONFIG_HASH = "hash-macro-params"


@pytest.fixture
def indexed(tmp_path: Path):
    """Index the two files above and give the open connection and the root.

    The project carries a real ``.fw-context/config.toml``, thus a tool that
    resolves its own database from *project_root* reaches this index.
    """
    from fw_context_mcp.indexer.compile_commands import parse as parse_cc
    from fw_context_mcp.indexer.db import (
        open_db,
        transaction,
        upsert_build_config,
        upsert_project,
    )
    from fw_context_mcp.indexer.ops import store_symbols_for_unit

    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / ".fw-context").mkdir()
    (root / ".fw-context" / "config.toml").write_text(
        f'[project]\nid = "{PROJECT_ID}"\n\n[build]\n\n[index]\ndb_dir = "{tmp_path}"\n',
        encoding="utf-8",
    )
    (root / "src" / "conf.h").write_text(HEADER, encoding="utf-8")
    (root / "src" / "main.c").write_text(MAIN, encoding="utf-8")

    cc = root / "compile_commands.json"
    cc.write_text(
        json.dumps([{
            "directory": str(root),
            "file": str(root / "src" / "main.c"),
            "arguments": [
                "cc", "-std=c11", "-c", str(root / "src" / "main.c"),
                "-I", str(root / "src"),
            ],
        }]),
        encoding="utf-8",
    )

    db_path = tmp_path / PROJECT_ID / "index.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = open_db(db_path)
    try:
        with transaction(conn):
            upsert_project(conn, PROJECT_ID, root.name, str(root))
            upsert_build_config(conn, CONFIG_HASH, PROJECT_ID, str(cc))
        unit = next(iter(parse_cc(cc)))
        with transaction(conn):
            store_symbols_for_unit(conn, unit, CONFIG_HASH, root)
        yield conn, root
    finally:
        conn.close()


def _row(conn, name: str):
    row = conn.execute(
        "SELECT name, value, params, is_function_like FROM macros "
        "WHERE config_hash=? AND name=?",
        (CONFIG_HASH, name),
    ).fetchone()
    assert row is not None, f"{name} is not in the index"
    return row


class TestTheRowHoldsBothHalves:
    def test_a_function_like_macro(self, indexed) -> None:
        conn, _ = indexed
        row = _row(conn, "MIN")
        assert row["is_function_like"] == 1
        assert row["params"] == "a, b"
        assert row["value"] == "( ( a ) < ( b ) ? ( a ) : ( b ) )"

    def test_an_object_like_macro(self, indexed) -> None:
        conn, _ = indexed
        row = _row(conn, "BUFFER_SIZE")
        assert row["is_function_like"] == 0
        assert row["params"] == ""
        assert row["value"] == "256"

    def test_a_macro_that_takes_no_argument(self, indexed) -> None:
        conn, _ = indexed
        row = _row(conn, "NOARGS")
        assert row["is_function_like"] == 1
        assert row["params"] == ""

    def test_a_space_keeps_the_parens_in_the_value(self, indexed) -> None:
        conn, _ = indexed
        row = _row(conn, "SPACED")
        assert row["is_function_like"] == 0
        assert row["value"] == "( x )"


class TestTheFtsIndexStillReachesAParameterName:
    """The parameter used to be searchable because it sat inside `value`."""

    def test_a_parameter_name_matches(self, indexed) -> None:
        conn, _ = indexed
        names = [
            r["name"] for r in conn.execute(
                "SELECT m.name FROM macros_fts JOIN macros m "
                "ON m.id = macros_fts.rowid WHERE macros_fts MATCH ?",
                ("b",),
            ).fetchall()
        ]
        assert "MIN" in names

    def test_the_replacement_text_matches_too(self, indexed) -> None:
        conn, _ = indexed
        names = [
            r["name"] for r in conn.execute(
                "SELECT m.name FROM macros_fts JOIN macros m "
                "ON m.id = macros_fts.rowid WHERE macros_fts MATCH ?",
                ("256",),
            ).fetchall()
        ]
        assert "BUFFER_SIZE" in names


class TestWhatAToolAnswersWith:
    """``_try_macro_fallback`` is what get_source and explain_symbol give."""

    def test_a_function_like_macro_shows_its_invocation(self, indexed) -> None:
        from fw_context_mcp.mcp.handlers.source import _try_macro_fallback

        conn, root = indexed
        answer = _try_macro_fallback(conn, CONFIG_HASH, "MIN", root)
        assert answer is not None
        assert answer["signature"] == "#define MIN(a, b)"
        assert answer["is_function_like"] is True
        assert answer["value"] == "( ( a ) < ( b ) ? ( a ) : ( b ) )"
        # The reconstructed line must read like the file, thus the parameter
        # list stands between the name and the replacement text.
        assert answer["source"] == "#define MIN(a, b) ( ( a ) < ( b ) ? ( a ) : ( b ) )"

    def test_an_object_like_macro_has_no_parentheses(self, indexed) -> None:
        from fw_context_mcp.mcp.handlers.source import _try_macro_fallback

        conn, root = indexed
        answer = _try_macro_fallback(conn, CONFIG_HASH, "BUFFER_SIZE", root)
        assert answer is not None
        assert answer["signature"] == "#define BUFFER_SIZE"
        assert answer["source"] == "#define BUFFER_SIZE 256"

    def test_a_macro_that_takes_no_argument_keeps_its_parentheses(self, indexed) -> None:
        from fw_context_mcp.mcp.handlers.source import _try_macro_fallback

        conn, root = indexed
        answer = _try_macro_fallback(conn, CONFIG_HASH, "NOARGS", root)
        assert answer is not None
        assert answer["signature"] == "#define NOARGS()"


class TestEveryToolWritesOneSpelling:
    """A reader must never have to ask which tool wrote a macro answer."""

    @staticmethod
    def _looked_up(root: Path, name: str) -> dict:
        from fw_context_mcp.mcp.handlers._lookup import lookup_symbol

        rows = lookup_symbol(name, project_root=str(root), exact=True)
        macros = [r for r in rows if r.get("kind") == "macro"]
        assert macros, f"lookup_symbol found no macro for {name}: {rows}"
        return macros[0]

    def test_lookup_symbol_matches_get_source(self, indexed) -> None:
        from fw_context_mcp.mcp.handlers.source import _try_macro_fallback

        conn, root = indexed
        for name in ("MIN", "BUFFER_SIZE", "NOARGS"):
            looked_up = self._looked_up(root, name)
            from_source = _try_macro_fallback(conn, CONFIG_HASH, name, root)
            assert from_source is not None
            assert looked_up["signature"] == from_source["signature"], name

    def test_lookup_symbol_shows_the_parameters(self, indexed) -> None:
        _, root = indexed
        answer = self._looked_up(root, "MIN")
        assert answer["signature"] == "#define MIN(a, b)"
        assert answer["is_function_like"] is True
        assert answer["value"] == "( ( a ) < ( b ) ? ( a ) : ( b ) )"
