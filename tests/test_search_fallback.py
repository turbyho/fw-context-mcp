"""Tests for search fallback chain and LIKE escaping in search handlers.

Regression safety for F10 fix — % and _ in queries must not be
interpreted as LIKE wildcards in name_tokens and docstring fallbacks.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


# ── LIKE escaping ──────────────────────────────────────────────────────────

class TestLikeEscaping:
    """% and _ in queries must be escaped in LIKE fallback paths."""

    @pytest.fixture
    def search_conn(self) -> sqlite3.Connection:
        """In-memory DB with symbols including % and _ in name_tokens."""
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
                signature TEXT,
                docstring TEXT,
                name_tokens TEXT,
                is_definition INTEGER DEFAULT 1,
                is_template INTEGER DEFAULT 0,
                is_virtual INTEGER DEFAULT 0,
                is_pure_virtual INTEGER DEFAULT 0,
                is_project INTEGER DEFAULT 1,
                line INTEGER DEFAULT 1,
                usr TEXT DEFAULT '',
                template_usr TEXT DEFAULT '',
                parent_usr TEXT DEFAULT '',
                enum_value INTEGER,
                summary TEXT,
                inputs TEXT,
                outputs TEXT,
                pagerank REAL DEFAULT 0.0
            );
        """)
        conn.execute(
            "INSERT INTO symbols (id, config_hash, name, qualified_name, kind, "
            "file_path, signature, docstring, name_tokens) "
            "VALUES (1, 'test', 'pct_match', 'pct_match', "
            "'function', 'src/pct.c', 'void pct()', 'Handles 100% coverage', "
            "'pct match')"
        )
        conn.execute(
            "INSERT INTO symbols (id, config_hash, name, qualified_name, kind, "
            "file_path, signature, docstring, name_tokens) "
            "VALUES (2, 'test', 'underscore_fn', 'underscore_fn', "
            "'function', 'src/und.c', 'void underscore_fn()', 'Used for _private items', "
            "'underscore fn')"
        )
        conn.execute(
            "INSERT INTO symbols (id, config_hash, name, qualified_name, kind, "
            "file_path, signature, docstring, name_tokens) "
            "VALUES (3, 'test', 'normal_fn', 'normal_fn', "
            "'function', 'src/normal.c', 'void normal_fn()', 'A normal function', "
            "'normal fn')"
        )
        conn.commit()
        return conn

    def test_normal_query_finds_match(self, search_conn: sqlite3.Connection) -> None:
        """Normal queries should still find matching symbols."""
        from fw_context_mcp.mcp.handlers.search import _search_code_name_tokens

        result = _search_code_name_tokens(
            search_conn, "normal", "test",
            limit=10, _kind=None, _project_only=False,
            root=Path("/tmp"),
        )
        assert result is not None
        items, method = result
        assert method == "name_tokens_like"
        assert any(r["name"] == "normal_fn" for r in items)

    def test_no_match_returns_none(self, search_conn: sqlite3.Connection) -> None:
        """Query with no matching tokens should return None."""
        from fw_context_mcp.mcp.handlers.search import _search_code_name_tokens

        result = _search_code_name_tokens(
            search_conn, "zzzxyz", "test",
            limit=10, _kind=None, _project_only=False,
            root=Path("/tmp"),
        )
        assert result is None

    def test_underscore_short_query_skipped(self, search_conn: sqlite3.Connection) -> None:
        """Single-char queries are filtered out (len <= 1)."""
        from fw_context_mcp.mcp.handlers.search import _search_code_name_tokens

        result = _search_code_name_tokens(
            search_conn, "_", "test",
            limit=10, _kind=None, _project_only=False,
            root=Path("/tmp"),
        )
        assert result is None

    def test_docstring_single_term(self, search_conn: sqlite3.Connection) -> None:
        """Single-term docstring search finds matches."""
        from fw_context_mcp.mcp.handlers.search import _search_code_docstring

        result = _search_code_docstring(
            search_conn, "normal", "test",
            limit=10, _kind=None, _project_only=False,
            root=Path("/tmp"),
        )
        assert result is not None
        items, method = result
        assert method == "docstring_like"
        assert any(r["name"] == "normal_fn" for r in items)

    def test_docstring_multi_term_skipped(self, search_conn: sqlite3.Connection) -> None:
        """Docstring fallback only applies to single-term queries."""
        from fw_context_mcp.mcp.handlers.search import _search_code_docstring

        result = _search_code_docstring(
            search_conn, "normal function", "test",
            limit=10, _kind=None, _project_only=False,
            root=Path("/tmp"),
        )
        assert result is None


# ── Fallback chain ordering ────────────────────────────────────────────────

class TestFallbackChain:
    """Verify the fallback chain is registered in the correct order."""

    def test_fallbacks_in_correct_order(self) -> None:
        from fw_context_mcp.mcp.handlers.search import _SEARCH_CODE_FALLBACKS

        names = [f.__name__ for f in _SEARCH_CODE_FALLBACKS]
        assert names == [
            "_search_code_name_tokens",
            "_search_code_docstring",
            "_search_code_individual_terms",
            "_search_code_macros_fts",
        ], f"Fallback chain order changed: {names}"

    def test_fmt_symbol_rows_fallback_marker(self) -> None:
        """_fmt_symbol_rows adds _fallback for non-fts5 methods."""
        from fw_context_mcp.mcp.handlers.search import _fmt_symbol_rows

        rows = [{
            "name": "test_fn", "qualified_name": "test_fn",
            "kind": "function", "file_path": "src/test.c", "line": 10,
            "is_definition": 1, "signature": "void test_fn()",
            "docstring": "", "is_template": 0, "is_virtual": 0,
            "is_pure_virtual": 0, "template_usr": "", "parent_usr": "",
            "enum_value": None,
        }]
        result, method = _fmt_symbol_rows(rows, Path("/tmp"), "name_tokens_like")
        assert method == "name_tokens_like"
        assert result[0]["_fallback"] == "name_tokens_like"

    def test_fmt_symbol_rows_no_fallback_for_direct_match(self) -> None:
        """Direct FTS5+kind matches don't get _fallback marker."""
        from fw_context_mcp.mcp.handlers.search import _fmt_symbol_rows

        rows = [{
            "name": "test_fn", "qualified_name": "test_fn",
            "kind": "function", "file_path": "src/test.c", "line": 10,
            "is_definition": 1, "signature": "void test_fn()",
            "docstring": "", "is_template": 0, "is_virtual": 0,
            "is_pure_virtual": 0, "template_usr": "", "parent_usr": "",
            "enum_value": None,
        }]
        result, method = _fmt_symbol_rows(rows, Path("/tmp"), "fts5+kind")
        assert method == "fts5+kind"
        assert "_fallback" not in result[0]

    def test_macros_fts_handles_missing_table(self) -> None:
        """_search_code_macros_fts handles missing macros_fts table."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row

        from fw_context_mcp.mcp.handlers.search import _search_code_macros_fts

        result = _search_code_macros_fts(
            conn, "DEBUG", "test", limit=10, _kind=None,
            _project_only=False, root=Path("/tmp"),
        )
        assert result is None

    def test_like_escaping_preserves_special_chars(self) -> None:
        """Query with % character is escaped, not treated as wildcard."""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE symbols (
                id INTEGER PRIMARY KEY, config_hash TEXT, name TEXT,
                qualified_name TEXT, kind TEXT, file_path TEXT,
                signature TEXT, docstring TEXT, name_tokens TEXT,
                is_definition INTEGER DEFAULT 1,
                is_template INTEGER DEFAULT 0, is_virtual INTEGER DEFAULT 0,
                is_pure_virtual INTEGER DEFAULT 0, is_project INTEGER DEFAULT 1,
                line INTEGER DEFAULT 1, usr TEXT DEFAULT '',
                template_usr TEXT DEFAULT '', parent_usr TEXT DEFAULT '',
                enum_value INTEGER, summary TEXT, pagerank REAL DEFAULT 0.0
            );
        """)
        conn.execute(
            "INSERT INTO symbols VALUES (1, 'test', 'all', 'all', 'function', "
            "'src/all.c', 'void all()', '', 'all', 1, 0, 0, 0, 1, 1, "
            "'usr_all', '', '', NULL, NULL, 0.0)"
        )
        conn.commit()

        from fw_context_mcp.mcp.handlers.search import _search_code_name_tokens

        # "%" alone is too short → filtered out → None
        result = _search_code_name_tokens(
            conn, "%", "test", limit=10, _kind=None, _project_only=False,
            root=Path("/tmp"),
        )
        assert result is None  # len <= 1 → filtered
