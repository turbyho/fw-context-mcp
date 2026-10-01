"""Tests for the fallback strategies in search/shared_fallbacks.py.

Covers ``_symbol_row_to_dict`` and the four strategies that the MCP tool
search_code tries when its primary FTS5 search finds nothing: name-token
LIKE, docstring LIKE, individual terms, and macro FTS.

These tests called the strategies through four pipeline phases and four
adapter functions before.  Only the SEARCH_CODE pipeline used those
phases, and no tool used that pipeline, thus the phases were removed.  The
tests now call the strategies directly, with the same cases.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

from fw_context_mcp.search.shared_fallbacks import (
    _search_code_docstring,
    _search_code_individual_terms,
    _search_code_macros_fts,
    _search_code_name_tokens,
    _symbol_row_to_dict,
)

# ── Helpers ──────────────────────────────────────────────────────────────────


def _mock_abs_path():
    """Replace abs_path, so that a result file path does not depend on the disk."""
    return patch(
        "fw_context_mcp.search.shared_fallbacks.abs_path",
        side_effect=lambda root, path: f"/fake/{path}" if path else "",
    )


def _rows(result: tuple[list[dict], str] | None) -> list[dict]:
    """A strategy gives (rows, method), or None for no match."""
    return result[0] if result else []


def _name_tokens(db: sqlite3.Connection, query: str) -> list[dict]:
    return _rows(_search_code_name_tokens(db, query, "test_hash", 10, None, False, None))


def _docstring(db: sqlite3.Connection, query: str) -> list[dict]:
    return _rows(_search_code_docstring(db, query, "test_hash", 10, None, False, None))


def _individual_terms(db: sqlite3.Connection, query: str) -> list[dict]:
    return _rows(_search_code_individual_terms(db, query, "test_hash", 10, None, False, None))


def _macros_fts(db: sqlite3.Connection, query: str) -> list[dict]:
    return _rows(_search_code_macros_fts(db, query, "test_hash", 10, None, False, None))


def _make_symbols_db() -> sqlite3.Connection:
    """In-memory DB with minimal symbols schema for fallback tests."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE symbols (
            id INTEGER PRIMARY KEY,
            config_hash TEXT,
            name TEXT,
            qualified_name TEXT,
            kind TEXT,
            file_path TEXT,
            line INTEGER DEFAULT 1,
            signature TEXT DEFAULT '',
            docstring TEXT DEFAULT '',
            name_tokens TEXT DEFAULT '',
            is_definition INTEGER DEFAULT 1,
            is_template INTEGER DEFAULT 0,
            is_virtual INTEGER DEFAULT 0,
            is_pure_virtual INTEGER DEFAULT 0,
            is_project INTEGER DEFAULT 1,
            usr TEXT DEFAULT '',
            template_usr TEXT DEFAULT '',
            parent_usr TEXT DEFAULT '',
            enum_value INTEGER,
            summary TEXT,
            inputs TEXT,
            outputs TEXT,
            pagerank REAL DEFAULT 0.0
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts USING fts5(
            name, qualified_name, signature, docstring,
            content='symbols', content_rowid='id'
        );
    """)
    conn.commit()
    return conn


def _seed_symbols(conn: sqlite3.Connection, rows: list[dict]) -> None:
    """Insert test symbols into the DB."""
    for r in rows:
        conn.execute(
            """INSERT INTO symbols
               (id, config_hash, name, qualified_name, kind, file_path,
                signature, docstring, name_tokens, usr)
               VALUES (?, 'test_hash', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                r.get("id", 1),
                r["name"],
                r.get("qualified_name", r["name"]),
                r.get("kind", "function"),
                r.get("file_path", "src/test.c"),
                r.get("signature", ""),
                r.get("docstring", ""),
                r.get("name_tokens", r["name"]),
                r.get("usr", f"usr_{r['name']}"),
            ),
        )
    conn.commit()


def _make_macros_db() -> sqlite3.Connection:
    """In-memory DB with macros and macros_fts tables."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE files (
            id INTEGER PRIMARY KEY,
            path TEXT,
            config_hash TEXT
        );
        INSERT INTO files VALUES (1, 'src/test.h', 'test_hash');

        CREATE TABLE macros (
            id INTEGER PRIMARY KEY,
            config_hash TEXT,
            name TEXT,
            value TEXT,
            params TEXT DEFAULT '',
            expanded_value TEXT,
            file_id INTEGER,
            line INTEGER DEFAULT 1,
            is_function_like INTEGER DEFAULT 0,
            is_project INTEGER DEFAULT 1
        );
        CREATE VIRTUAL TABLE macros_fts USING fts5(
            name, value, params,
            content='macros', content_rowid='id'
        );
    """)
    conn.commit()
    return conn


# ── _symbol_row_to_dict ──────────────────────────────────────────────────────


class TestSymbolRowToDict:
    def test_basic_conversion(self) -> None:
        conn = _make_symbols_db()
        _seed_symbols(conn, [{"id": 1, "name": "test_fn"}])
        row = conn.execute("SELECT * FROM symbols WHERE id = 1").fetchone()
        result = _symbol_row_to_dict(row, Path("/root"))
        assert result["name"] == "test_fn"
        assert result["kind"] == "function"
        assert "file" in result
        assert result["is_definition"] is True
        assert result["is_template"] is False

    def test_extra_kwargs_merged(self) -> None:
        conn = _make_symbols_db()
        _seed_symbols(conn, [{"id": 1, "name": "test_fn"}])
        row = conn.execute("SELECT * FROM symbols WHERE id = 1").fetchone()
        result = _symbol_row_to_dict(row, Path("/root"), _fallback="test_method")
        assert result["_fallback"] == "test_method"

    def test_enum_value_included_when_not_none(self) -> None:
        conn = _make_symbols_db()
        conn.execute(
            "INSERT INTO symbols (id, config_hash, name, kind, usr, enum_value) "
            "VALUES (10, 'test_hash', 'RED', 'enum_constant', 'usr_red', 1)"
        )
        conn.commit()
        row = conn.execute("SELECT * FROM symbols WHERE id = 10").fetchone()
        result = _symbol_row_to_dict(row, Path("/root"))
        assert result["enum_value"] == 1


# ── Name-token LIKE ──────────────────────────────────────────────────────────


class TestNameTokensFallback:
    def test_finds_token_match(self) -> None:
        db = _make_symbols_db()
        _seed_symbols(db, [
            {"id": 1, "name": "uart_init", "name_tokens": "uart init"},
        ])
        with _mock_abs_path():
            rows = _name_tokens(db, "uart")
        assert len(rows) == 1
        assert rows[0]["name"] == "uart_init"
        assert rows[0]["_fallback"] == "name_tokens_like"

    def test_two_terms_match_the_tokens(self) -> None:
        db = _make_symbols_db()
        _seed_symbols(db, [
            {"id": 1, "name": "modem", "name_tokens": "modem init handler"},
        ])
        with _mock_abs_path():
            rows = _name_tokens(db, "modem init")
        assert [r["name"] for r in rows] == ["modem"]

    def test_requires_n_minus_1_matches(self) -> None:
        """3-term query where only 1 term matches → below N-1=2 threshold."""
        db = _make_symbols_db()
        _seed_symbols(db, [
            {"id": 1, "name": "uart_init", "name_tokens": "uart init"},
        ])
        with _mock_abs_path():
            # "uart xyz abc" has 3 terms, "uart" matches 1 → 1 < 2 (=N-1) → empty
            rows = _name_tokens(db, "uart xyz abc")
        assert rows == []

    def test_no_matching_token(self) -> None:
        db = _make_symbols_db()
        _seed_symbols(db, [{"id": 1, "name": "modem"}])
        assert _name_tokens(db, "zzzxyz") == []

    def test_short_terms_filtered(self) -> None:
        """Terms of length <= 1 are filtered out."""
        db = _make_symbols_db()
        assert _name_tokens(db, "a") == []


# ── Docstring LIKE ───────────────────────────────────────────────────────────


class TestDocstringFallback:
    def test_finds_single_term_in_docstring(self) -> None:
        db = _make_symbols_db()
        _seed_symbols(db, [
            {"id": 1, "name": "handler", "docstring": "Interrupt handler for UART"},
        ])
        with _mock_abs_path():
            rows = _docstring(db, "interrupt")
        assert len(rows) == 1
        assert rows[0]["name"] == "handler"
        assert rows[0]["_fallback"] == "docstring_like"

    def test_finds_the_last_word_of_a_docstring(self) -> None:
        db = _make_symbols_db()
        _seed_symbols(db, [
            {"id": 1, "name": "pct_fn", "docstring": "Handles 100% coverage"},
        ])
        with _mock_abs_path():
            rows = _docstring(db, "coverage")
        assert [r["name"] for r in rows] == ["pct_fn"]

    def test_multi_term_skipped(self) -> None:
        db = _make_symbols_db()
        _seed_symbols(db, [
            {"id": 1, "name": "modem_init_fn", "docstring": "Initialize modem"},
        ])
        assert _docstring(db, "modem init") == []

    def test_short_terms_filtered(self) -> None:
        db = _make_symbols_db()
        assert _docstring(db, "a") == []


# ── Individual terms ─────────────────────────────────────────────────────────


class TestIndividualTermsFallback:
    def test_multi_term_split_and_search(self) -> None:
        db = _make_symbols_db()
        db.execute("INSERT INTO symbols_fts(rowid, name) VALUES (1, 'buffer')")
        db.execute("INSERT INTO symbols_fts(rowid, name) VALUES (2, 'spi')")
        _seed_symbols(db, [
            {"id": 1, "name": "circular_buffer", "usr": "usr_buf"},
            {"id": 2, "name": "spi_init", "usr": "usr_spi"},
        ])
        with _mock_abs_path():
            rows = _individual_terms(db, "buffer spi")
        assert len(rows) > 0
        names = [r["name"] for r in rows]
        assert "circular_buffer" in names or "spi_init" in names
        for r in rows:
            assert r["_fallback"] == "individual_terms"

    def test_single_term_skipped(self) -> None:
        db = _make_symbols_db()
        _seed_symbols(db, [{"id": 1, "name": "modem_init"}])
        assert _individual_terms(db, "modem") == []


# ── Macro FTS ────────────────────────────────────────────────────────────────


class TestMacrosFtsFallback:
    def test_finds_macro(self) -> None:
        db = _make_macros_db()
        # Named columns, not a positional VALUES: the row shape changed when
        # `params` arrived, and a positional insert says nothing about which
        # value lands where.
        db.execute(
            "INSERT INTO macros (id, config_hash, name, value, params, "
            "expanded_value, file_id, line, is_function_like) "
            "VALUES (1, 'test_hash', 'VERSION', '1.0', '', '1.0', 1, 1, 0)"
        )
        db.execute("INSERT INTO macros_fts(rowid, name, value) VALUES (1, 'version', '1.0')")
        db.commit()
        with _mock_abs_path():
            rows = _macros_fts(db, "version")
        assert len(rows) == 1
        assert rows[0]["name"] == "VERSION"
        assert rows[0]["kind"] == "macro"
        assert rows[0]["signature"] == "#define VERSION"
        assert rows[0]["_fallback"] == "macros_fts"

    def test_an_uppercase_query_finds_the_macro(self) -> None:
        """A macro name is uppercase, thus the operator often types it so."""
        db = _make_macros_db()
        db.execute(
            "INSERT INTO macros (id, config_hash, name, value, params, "
            "expanded_value, file_id, line, is_function_like) "
            "VALUES (1, 'test_hash', 'DEBUG', '1', '', '1', 1, 42, 0)"
        )
        db.execute("INSERT INTO macros_fts(rowid, name, value) VALUES (1, 'debug', '1')")
        db.commit()
        with _mock_abs_path():
            rows = _macros_fts(db, "DEBUG")
        assert [r["name"] for r in rows] == ["DEBUG"]

    def test_a_function_like_macro_shows_its_parameters(self) -> None:
        # The parameter list used to be glued to the front of `value`, thus
        # a reader could not tell how the macro is invoked.
        db = _make_macros_db()
        db.execute(
            "INSERT INTO macros (id, config_hash, name, value, params, "
            "expanded_value, file_id, line, is_function_like) "
            "VALUES (1, 'test_hash', 'MIN', '((a)<(b)?(a):(b))', 'a, b', "
            "'', 1, 1, 1)"
        )
        db.execute(
            "INSERT INTO macros_fts(rowid, name, value, params) "
            "VALUES (1, 'min', '((a)<(b)?(a):(b))', 'a, b')"
        )
        db.commit()
        with _mock_abs_path():
            rows = _macros_fts(db, "min")
        assert len(rows) == 1
        assert rows[0]["signature"] == "#define MIN(a, b)"
        assert rows[0]["_macro_value"] == "((a)<(b)?(a):(b))"

    def test_handles_missing_table(self) -> None:
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        assert _macros_fts(db, "ANYTHING") == []

    def test_no_match_returns_empty(self) -> None:
        db = _make_macros_db()
        assert _macros_fts(db, "nonexistent") == []
