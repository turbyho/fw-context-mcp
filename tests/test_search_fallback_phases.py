"""Tests for fallback search phases in search/phases/search_fallbacks.py.

Covers: NameTokensFallbackPhase, DocstringFallbackPhase,
IndividualTermsFallbackPhase, MacrosFtsFallbackPhase — should_run gating,
run with empty/non-empty results, and the internal _do_* helper functions.
"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from fw_context_mcp.search.context import PipelineContext
from fw_context_mcp.search.phases.search_fallbacks import (
    DocstringFallbackPhase,
    IndividualTermsFallbackPhase,
    MacrosFtsFallbackPhase,
    NameTokensFallbackPhase,
    _do_docstring_fallback,
    _do_individual_terms_fallback,
    _do_macros_fts_fallback,
    _do_name_tokens_fallback,
)
from fw_context_mcp.search.shared_fallbacks import _symbol_row_to_dict


# ── Patch helpers ─────────────────────────────────────────────────────────────
# _do_* adapter functions delegate to _search_code_* in shared_fallbacks,
# which call abs_path.  When root is None (adapter default), _symbol_row_to_dict
# skips abs_path entirely, so the mock is only needed for phase tests that
# pass ctx.project_root.  Kept for safety — no harm mocking it either way.

_ABS_PATH_MODULE = "fw_context_mcp.search.shared_fallbacks.abs_path"


class _FakeExecutor:
    """Minimal executor stand-in for phase tests.

    Phases no longer call ``open_db`` directly — they run queries through
    ``ctx.executor.execute_sync(query_fn, config_hash)``.  This fake runs
    the closure against the test's in-memory connection.
    """

    def execute_sync(self, query_fn, config_hash, *args):
        return query_fn(_CURRENT_DB, config_hash, *args)


_CURRENT_DB = None  # set by _phase_patches for the duration of a phase test


def _mock_abs_path():
    """Return a side_effect mock for abs_path."""
    return patch(
        _ABS_PATH_MODULE,
        side_effect=lambda root, path: f"/fake/{path}" if path else "",
    )


@contextmanager
def _phase_patches(db: sqlite3.Connection):
    """Context manager providing the test DB connection + abs_path patch."""
    global _CURRENT_DB
    _CURRENT_DB = db
    try:
        with _mock_abs_path():
            yield
    finally:
        _CURRENT_DB = None


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_ctx(**overrides) -> PipelineContext:
    """Create a minimal PipelineContext for testing."""
    from fw_context_mcp.config.settings import Config

    defaults = {
        "config_hash": "test_hash",
        "project_root": Path("/tmp"),
        "db_path": Path("/tmp/test.db"),
        "query": "modem init",
        "original_query": "modem init",
        "config": Config(),
        "executor": _FakeExecutor(),
        "limit": 20,
    }
    defaults.update(overrides)
    return PipelineContext(**defaults)


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
            expanded_value TEXT,
            file_id INTEGER,
            line INTEGER DEFAULT 1,
            is_project INTEGER DEFAULT 1
        );
        CREATE VIRTUAL TABLE macros_fts USING fts5(
            name, value,
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


# ── NameTokensFallbackPhase ──────────────────────────────────────────────────


class TestNameTokensFallbackPhase:
    def test_should_run_when_fts5_empty(self) -> None:
        ctx = _make_ctx(fts5_results=[])
        phase = NameTokensFallbackPhase()
        assert phase.should_run(ctx)

    def test_should_not_run_when_fts5_populated(self) -> None:
        ctx = _make_ctx(fts5_results=[{"name": "x"}])
        phase = NameTokensFallbackPhase()
        assert not phase.should_run(ctx)

    def test_run_with_matching_tokens(self) -> None:
        ctx = _make_ctx(query="modem init", fts5_results=[])
        db = _make_symbols_db()
        _seed_symbols(db, [
            {"id": 1, "name": "modem", "name_tokens": "modem init handler"},
        ])
        with _phase_patches(db):
            phase = NameTokensFallbackPhase()
            result = asyncio.run(phase.run(ctx))
            assert len(result.fts5_results) == 1
            assert result.fts5_results[0]["name"] == "modem"
            assert result.fts5_results[0]["_fallback"] == "name_tokens_like"

    def test_run_with_no_matching_tokens_returns_unchanged(self) -> None:
        ctx = _make_ctx(query="zzzxyz", fts5_results=[])
        db = _make_symbols_db()
        _seed_symbols(db, [{"id": 1, "name": "modem"}])
        with _phase_patches(db):
            phase = NameTokensFallbackPhase()
            result = asyncio.run(phase.run(ctx))
            assert result.fts5_results == []


# ── DocstringFallbackPhase ───────────────────────────────────────────────────


class TestDocstringFallbackPhase:
    def test_should_run_when_fts5_empty(self) -> None:
        ctx = _make_ctx(fts5_results=[])
        phase = DocstringFallbackPhase()
        assert phase.should_run(ctx)

    def test_should_not_run_when_fts5_populated(self) -> None:
        ctx = _make_ctx(fts5_results=[{"name": "x"}])
        phase = DocstringFallbackPhase()
        assert not phase.should_run(ctx)

    def test_run_single_term_finds_match(self) -> None:
        ctx = _make_ctx(query="coverage", fts5_results=[])
        db = _make_symbols_db()
        _seed_symbols(db, [
            {"id": 1, "name": "pct_fn", "docstring": "Handles 100% coverage"},
        ])
        with _phase_patches(db):
            phase = DocstringFallbackPhase()
            result = asyncio.run(phase.run(ctx))
            assert len(result.fts5_results) == 1
            assert result.fts5_results[0]["name"] == "pct_fn"
            assert result.fts5_results[0]["_fallback"] == "docstring_like"

    def test_run_multi_term_skipped(self) -> None:
        ctx = _make_ctx(query="modem init", fts5_results=[])
        db = _make_symbols_db()
        _seed_symbols(db, [
            {"id": 1, "name": "modem_init_fn", "docstring": "Initialize modem"},
        ])
        with _phase_patches(db):
            phase = DocstringFallbackPhase()
            result = asyncio.run(phase.run(ctx))
            assert result.fts5_results == []


# ── IndividualTermsFallbackPhase ─────────────────────────────────────────────


class TestIndividualTermsFallbackPhase:
    def test_should_run_when_fts5_empty(self) -> None:
        ctx = _make_ctx(fts5_results=[])
        phase = IndividualTermsFallbackPhase()
        assert phase.should_run(ctx)

    def test_should_not_run_when_fts5_populated(self) -> None:
        ctx = _make_ctx(fts5_results=[{"name": "x"}])
        phase = IndividualTermsFallbackPhase()
        assert not phase.should_run(ctx)

    def test_run_multi_term_finds_matches(self) -> None:
        ctx = _make_ctx(query="buffer spi", fts5_results=[])
        db = _make_symbols_db()
        # Create FTS5 content so search_symbols works
        db.execute("INSERT INTO symbols_fts(rowid, name) VALUES (1, 'buffer')")
        db.execute("INSERT INTO symbols_fts(rowid, name) VALUES (2, 'spi')")

        _seed_symbols(db, [
            {"id": 1, "name": "circular_buffer", "usr": "usr_buf"},
            {"id": 2, "name": "spi_init", "usr": "usr_spi"},
        ])
        with _phase_patches(db):
            phase = IndividualTermsFallbackPhase()
            result = asyncio.run(phase.run(ctx))
            assert len(result.fts5_results) > 0
            names = [r["name"] for r in result.fts5_results]
            assert "circular_buffer" in names or "spi_init" in names
            for r in result.fts5_results:
                assert r["_fallback"] == "individual_terms"

    def test_run_single_term_skipped(self) -> None:
        ctx = _make_ctx(query="modem", fts5_results=[])
        db = _make_symbols_db()
        _seed_symbols(db, [{"id": 1, "name": "modem_init"}])
        with _phase_patches(db):
            phase = IndividualTermsFallbackPhase()
            result = asyncio.run(phase.run(ctx))
            assert result.fts5_results == []


# ── MacrosFtsFallbackPhase ───────────────────────────────────────────────────


class TestMacrosFtsFallbackPhase:
    def test_should_run_when_fts5_empty(self) -> None:
        ctx = _make_ctx(fts5_results=[])
        phase = MacrosFtsFallbackPhase()
        assert phase.should_run(ctx)

    def test_should_not_run_when_fts5_populated(self) -> None:
        ctx = _make_ctx(fts5_results=[{"name": "x"}])
        phase = MacrosFtsFallbackPhase()
        assert not phase.should_run(ctx)

    def test_run_with_macro_match(self) -> None:
        ctx = _make_ctx(query="DEBUG", fts5_results=[])
        db = _make_macros_db()
        db.execute(
            "INSERT INTO macros VALUES (1, 'test_hash', 'DEBUG', '1', '1', 1, 42, 1)"
        )
        db.execute("INSERT INTO macros_fts(rowid, name, value) VALUES (1, 'debug', '1')")
        db.commit()
        with _phase_patches(db):
            phase = MacrosFtsFallbackPhase()
            result = asyncio.run(phase.run(ctx))
            assert len(result.fts5_results) == 1
            assert result.fts5_results[0]["name"] == "DEBUG"
            assert result.fts5_results[0]["kind"] == "macro"
            assert result.fts5_results[0]["_fallback"] == "macros_fts"

    def test_run_without_macro_match_returns_empty(self) -> None:
        ctx = _make_ctx(query="NONEXISTENT_MACRO", fts5_results=[])
        db = _make_macros_db()
        with _phase_patches(db):
            phase = MacrosFtsFallbackPhase()
            result = asyncio.run(phase.run(ctx))
            assert result.fts5_results == []

    def test_run_handles_missing_macros_fts_table(self) -> None:
        """When macros_fts table doesn't exist, returns ctx unchanged."""
        ctx = _make_ctx(query="DEBUG", fts5_results=[])
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        with _phase_patches(db):
            phase = MacrosFtsFallbackPhase()
            result = asyncio.run(phase.run(ctx))
            assert result.fts5_results == []


# ── Internal helpers (direct) ────────────────────────────────────────────────
# These tests call _do_* functions directly; they also need abs_path mocked
# because the helpers call abs_path(root=None) or _symbol_row_to_dict(r, None).


class TestDoNameTokensFallback:
    def test_finds_token_match(self) -> None:
        db = _make_symbols_db()
        _seed_symbols(db, [
            {"id": 1, "name": "uart_init", "name_tokens": "uart init"},
        ])
        with _mock_abs_path():
            rows = _do_name_tokens_fallback(db, "uart", "test_hash", 10)
        assert len(rows) == 1
        assert rows[0]["name"] == "uart_init"

    def test_requires_n_minus_1_matches(self) -> None:
        """3-term query where only 1 term matches → below N-1=2 threshold."""
        db = _make_symbols_db()
        _seed_symbols(db, [
            {"id": 1, "name": "uart_init", "name_tokens": "uart init"},
        ])
        with _mock_abs_path():
            # "uart xyz abc" has 3 terms, "uart" matches 1 → 1 < 2 (=N-1) → empty
            rows = _do_name_tokens_fallback(db, "uart xyz abc", "test_hash", 10)
        assert rows == []

    def test_short_terms_filtered(self) -> None:
        """Terms of length <= 1 are filtered out."""
        db = _make_symbols_db()
        rows = _do_name_tokens_fallback(db, "a", "test_hash", 10)
        assert rows == []


class TestDoDocstringFallback:
    def test_finds_single_term_in_docstring(self) -> None:
        db = _make_symbols_db()
        _seed_symbols(db, [
            {"id": 1, "name": "handler", "docstring": "Interrupt handler for UART"},
        ])
        with _mock_abs_path():
            rows = _do_docstring_fallback(db, "interrupt", "test_hash", 10)
        assert len(rows) == 1
        assert rows[0]["name"] == "handler"

    def test_multi_term_skipped(self) -> None:
        db = _make_symbols_db()
        _seed_symbols(db, [{"id": 1, "name": "fn", "docstring": "test"}])
        rows = _do_docstring_fallback(db, "two words", "test_hash", 10)
        assert rows == []

    def test_short_terms_filtered(self) -> None:
        db = _make_symbols_db()
        rows = _do_docstring_fallback(db, "a", "test_hash", 10)
        assert rows == []


class TestDoIndividualTermsFallback:
    def test_multi_term_split_and_search(self) -> None:
        db = _make_symbols_db()
        db.execute("INSERT INTO symbols_fts(rowid, name) VALUES (1, 'buffer')")
        db.execute("INSERT INTO symbols_fts(rowid, name) VALUES (2, 'spi')")
        _seed_symbols(db, [
            {"id": 1, "name": "ring_buffer", "usr": "usr_buf"},
            {"id": 2, "name": "spi_send", "usr": "usr_spi"},
        ])
        with _mock_abs_path():
            rows = _do_individual_terms_fallback(db, "buffer spi", "test_hash", 10)
        assert len(rows) > 0

    def test_single_term_skipped(self) -> None:
        db = _make_symbols_db()
        rows = _do_individual_terms_fallback(db, "single", "test_hash", 10)
        assert rows == []


class TestDoMacrosFtsFallback:
    def test_finds_macro(self) -> None:
        db = _make_macros_db()
        db.execute(
            "INSERT INTO macros VALUES (1, 'test_hash', 'VERSION', '1.0', '1.0', 1, 1, 1)"
        )
        db.execute("INSERT INTO macros_fts(rowid, name, value) VALUES (1, 'version', '1.0')")
        db.commit()
        with _mock_abs_path():
            rows = _do_macros_fts_fallback(db, "version", "test_hash", 10)
        assert len(rows) == 1
        assert rows[0]["name"] == "VERSION"
        assert rows[0]["kind"] == "macro"

    def test_handles_missing_table(self) -> None:
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        rows = _do_macros_fts_fallback(db, "ANYTHING", "test_hash", 10)
        assert rows == []

    def test_no_match_returns_empty(self) -> None:
        db = _make_macros_db()
        rows = _do_macros_fts_fallback(db, "nonexistent", "test_hash", 10)
        assert rows == []
