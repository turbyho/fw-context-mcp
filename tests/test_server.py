"""Tests for fw_context_mcp.mcp.server helper functions."""

import sqlite3
from pathlib import Path

import pytest

from fw_context_mcp.mcp.server import LOOKUP_EXACT_SQL, LOOKUP_PREFIX_SQL, _read_symbol_body
from fw_context_mcp.utils import abs_path as _abs_path


class TestAbsPath:
    def test_relative_joined_with_root(self):
        root = Path("/home/user/project")
        assert _abs_path(root, "src/zble.cpp") == "/home/user/project/src/zble.cpp"

    def test_absolute_returned_unchanged(self):
        root = Path("/home/user/project")
        assert _abs_path(root, "/abs/path/file.cpp") == "/abs/path/file.cpp"

    def test_empty_path_passthrough(self):
        root = Path("/home/user/project")
        assert _abs_path(root, "") == ""

    def test_nested_relative(self):
        root = Path("/p")
        assert _abs_path(root, "lib/modem/zmodem_driver.cpp") == "/p/lib/modem/zmodem_driver.cpp"


class TestReadSymbolBody:
    def _write(self, tmp_path, content):
        f = tmp_path / "src.cpp"
        f.write_text(content)
        return str(f)

    def test_balances_braces_for_function(self, tmp_path):
        src = (
            "int before() { return 0; }\n"      # line 1
            "void target()\n"                    # line 2 (definition line)
            "{\n"                                # line 3
            "    if (x) {\n"                     # line 4
            "        do_thing();\n"              # line 5
            "    }\n"                            # line 6
            "}\n"                                # line 7
            "int after() { return 1; }\n"        # line 8
        )
        path = self._write(tmp_path, src)
        body = _read_symbol_body(path, 2)
        assert "void target()" in body
        assert "do_thing();" in body
        assert "}" in body
        # must NOT bleed into after()
        assert "int after()" not in body

    def test_single_line_body(self, tmp_path):
        src = "void f() { return; }\nint g() {}\n"
        path = self._write(tmp_path, src)
        body = _read_symbol_body(path, 1)
        assert "void f()" in body
        assert "int g()" not in body

    def test_declaration_without_braces_small_window(self, tmp_path):
        src = "int field_a;\nint field_b;\nint field_c;\nint field_d;\nint field_e;\n"
        path = self._write(tmp_path, src)
        body = _read_symbol_body(path, 1)
        # no braces → small window (≤3 lines), not the whole file
        assert "field_a" in body
        assert body.count("\n") <= 2

    def test_missing_file_returns_empty(self, tmp_path):
        assert _read_symbol_body(str(tmp_path / "nope.cpp"), 1) == ""

    def test_out_of_range_line(self, tmp_path):
        path = self._write(tmp_path, "int x;\n")
        assert _read_symbol_body(path, 999) == ""

    def test_end_line_exact_range_preferred(self, tmp_path):
        # end_line from libclang extent → exact range, ignores brace heuristic
        src = "a\nb\nc\nd\ne\nf\n"
        path = self._write(tmp_path, src)
        body = _read_symbol_body(path, 2, end_line=4)
        # lines 2..4 = b, c, d
        assert "b" in body and "c" in body and "d" in body
        assert "a" not in body and "e" not in body

    def test_end_line_clamped_to_file(self, tmp_path):
        path = self._write(tmp_path, "x\ny\n")
        body = _read_symbol_body(path, 1, end_line=999)
        assert "x" in body and "y" in body

    def test_end_line_zero_falls_back_to_braces(self, tmp_path):
        src = "void f()\n{\n  g();\n}\nint after;\n"
        path = self._write(tmp_path, src)
        body = _read_symbol_body(path, 1, end_line=0)
        assert "void f()" in body and "g();" in body
        assert "int after" not in body


# ── _do_lookup SQL query tests ──────────────────────────────────────────────
# These verify the exact SQL queries used by lookup_symbol's _do_lookup inner
# function.  We run them against an in-memory DB with the real symbols schema.

SYMBOLS_SCHEMA = """
CREATE TABLE IF NOT EXISTS symbols (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    config_hash    TEXT    NOT NULL,
    file_id        INTEGER NOT NULL,
    file_path      TEXT    NOT NULL,
    name_tokens    TEXT    NOT NULL DEFAULT '',
    usr            TEXT    NOT NULL,
    name           TEXT    NOT NULL,
    qualified_name TEXT    NOT NULL,
    kind           TEXT    NOT NULL,
    line           INTEGER NOT NULL,
    col            INTEGER NOT NULL,
    end_line       INTEGER NOT NULL DEFAULT 0,
    is_definition  INTEGER NOT NULL DEFAULT 0,
    signature      TEXT    NOT NULL DEFAULT '',
    docstring      TEXT    NOT NULL DEFAULT '',
    parent_usr     TEXT    NOT NULL DEFAULT '',
    UNIQUE(config_hash, usr)
);
CREATE INDEX IF NOT EXISTS idx_symbols_name  ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_qname ON symbols(qualified_name);
"""

# LOOKUP_EXACT_SQL and LOOKUP_PREFIX_SQL are imported from fw_context_mcp.mcp.server
# — no local duplicates that could drift out of sync.


def _insert_symbol(c, **kw):
    defaults = {
        "config_hash": "hash-aaa",
        "file_id": 1,
        "file_path": "src/test.cpp",
        "name_tokens": "",
        "usr": "u-default",
        "kind": "method",
        "line": 10,
        "col": 1,
        "end_line": 0,
        "is_definition": 1,
        "signature": "void f()",
        "docstring": "",
        "parent_usr": "",
    }
    defaults.update(kw)
    c.execute(
        """INSERT INTO symbols (config_hash, file_id, file_path, name_tokens,
           usr, name, qualified_name, kind, line, col, end_line, is_definition,
           signature, docstring, parent_usr)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        tuple(defaults[f] for f in [
            "config_hash", "file_id", "file_path", "name_tokens",
            "usr", "name", "qualified_name", "kind", "line", "col",
            "end_line", "is_definition", "signature", "docstring",
            "parent_usr",
        ]),
    )


class TestLookupSymbolSQL:
    """Verify _do_lookup SQL queries with qualified_name matching."""

    @pytest.fixture
    def db(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(SYMBOLS_SCHEMA)
        return conn

    # ── exact=True ───────────────────────────────────────────────────────

    def test_exact_match_on_qualified_name(self, db):
        """lookup_symbol("mbed::DigitalOut::write", exact=True) finds the symbol."""
        _insert_symbol(db, usr="u1", name="write",
                       qualified_name="mbed::DigitalOut::write")
        rows = db.execute(LOOKUP_EXACT_SQL,
                          ("hash-aaa", "mbed::DigitalOut::write",
                           "mbed::DigitalOut::write", 50)).fetchall()
        assert len(rows) == 1
        assert rows[0]["name"] == "write"
        assert rows[0]["qualified_name"] == "mbed::DigitalOut::write"

    def test_exact_match_on_short_name_still_works(self, db):
        """Exact match by short name still works (backward compatibility)."""
        _insert_symbol(db, usr="u1", name="write",
                       qualified_name="mbed::DigitalOut::write")
        rows = db.execute(LOOKUP_EXACT_SQL, ("hash-aaa", "write", "write", 50)).fetchall()
        assert len(rows) == 1

    def test_exact_no_match_on_wrong_qualified_name(self, db):
        """No false positives — wrong qualified name must not match."""
        _insert_symbol(db, usr="u1", name="write",
                       qualified_name="mbed::DigitalOut::write")
        rows = db.execute(LOOKUP_EXACT_SQL,
                          ("hash-aaa", "mbed::GPIO::write",
                           "mbed::GPIO::write", 50)).fetchall()
        assert rows == []

    # ── exact=False ──────────────────────────────────────────────────────

    def test_prefix_match_on_qualified_name(self, db):
        """Prefix match finds symbol via qualified_name LIKE 'prefix%'."""
        _insert_symbol(db, usr="u1", name="read",
                       qualified_name="mbed::DigitalOut::read")
        _insert_symbol(db, usr="u2", name="read_voltage",
                       qualified_name="mbed::AnalogIn::read_voltage")
        rows = db.execute(LOOKUP_PREFIX_SQL,
                          ("hash-aaa", "mbed::DigitalOut%",
                           "mbed::DigitalOut%", 50)).fetchall()
        assert len(rows) == 1
        assert rows[0]["qualified_name"] == "mbed::DigitalOut::read"

    def test_prefix_match_on_short_name_still_works(self, db):
        """Prefix match by short name still works (backward compatibility)."""
        _insert_symbol(db, usr="u1", name="read_voltage",
                       qualified_name="mbed::AnalogIn::read_voltage")
        rows = db.execute(LOOKUP_PREFIX_SQL,
                          ("hash-aaa", "read%", "read%", 50)).fetchall()
        assert len(rows) >= 1
        assert any(r["name"] == "read_voltage" for r in rows)

    def test_prefix_match_multiple_results(self, db):
        """Prefix matching on qualified_name can return multiple results."""
        _insert_symbol(db, usr="u1", name="write",
                       qualified_name="mbed::DigitalOut::write")
        _insert_symbol(db, usr="u2", name="write",
                       qualified_name="mbed::SPI::write")
        rows = db.execute(LOOKUP_PREFIX_SQL,
                          ("hash-aaa", "mbed::%", "mbed::%", 50)).fetchall()
        assert len(rows) >= 2

    # ── ordering ─────────────────────────────────────────────────────────

    def test_definitions_sorted_first(self, db):
        """is_definition=1 rows appear before is_definition=0 rows."""
        _insert_symbol(db, usr="u1", name="write",
                       qualified_name="mbed::DigitalOut::write",
                       is_definition=0, signature="void write(int)")
        _insert_symbol(db, usr="u2", name="write",
                       qualified_name="mbed::DigitalOut::write",
                       is_definition=1, signature="void write(int)")
        rows = db.execute(LOOKUP_PREFIX_SQL,
                          ("hash-aaa", "mbed::DigitalOut::write%",
                           "mbed::DigitalOut::write%", 50)).fetchall()
        assert len(rows) == 2
        assert rows[0]["is_definition"] == 1
        assert rows[1]["is_definition"] == 0



class TestLikeEscaping:
    """Verify LIKE wildcard escaping in search_code progressive relaxation.

    The escape order: backslash first, then %, then _, then '.
    The LIKE clause must use ESCAPE '\\' so \\_ is literal underscore,
    \\% is literal percent, and \\\\ is literal backslash.
    """

    @pytest.fixture
    def db(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(SYMBOLS_SCHEMA)
        return conn

    @staticmethod
    def _escape_like(term: str) -> str:
        """The escaping used in search_code LIKE fallback (post-fix)."""
        return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").replace("'", "''")

    # ── Underscore ─────────────────────────────────────────────────────────

    def test_underscore_not_wildcard_in_name_tokens(self, db):
        """Literal _ in query term must not match space in name_tokens."""
        _insert_symbol(db, usr="u_1", name="test_func",
                       qualified_name="mcu::test_func",
                       name_tokens="test func")
        esc = self._escape_like("test_func")
        row = db.execute(
            f"SELECT * FROM symbols WHERE name_tokens LIKE '%{esc}%' ESCAPE '\\' "
            "AND config_hash=? LIMIT 1",
            ("hash-aaa",),
        ).fetchone()
        assert row is None, (
            f"Escaped LIKE '%{esc}%' matched 'test func' — "
            f"underscore is being treated as a wildcard"
        )

    def test_underscore_matches_literal_underscore(self, db):
        """Escaped _ in query term matches literal _ in name_tokens."""
        _insert_symbol(db, usr="u_1", name="test_func",
                       qualified_name="mcu::test_func",
                       name_tokens="test func")
        _insert_symbol(db, usr="u_2", name="test_func",
                       qualified_name="mcu::test_func",
                       name_tokens="test_func")
        esc = self._escape_like("test_func")
        rows = db.execute(
            f"SELECT * FROM symbols WHERE name_tokens LIKE '%{esc}%' ESCAPE '\\' "
            "AND config_hash=? ORDER BY line",
            ("hash-aaa",),
        ).fetchall()
        assert len(rows) == 1, f"Expected 1 match (literal underscore), got {len(rows)}"
        assert rows[0]["name_tokens"] == "test_func"

    # ── Percent ────────────────────────────────────────────────────────────

    def test_percent_not_wildcard_in_name_tokens(self, db):
        """Literal % in query term must not match arbitrary text."""
        _insert_symbol(db, usr="u_pct", name="pct",
                       qualified_name="mcu::pct",
                       name_tokens="100 percent")
        esc = self._escape_like("100%")
        row = db.execute(
            f"SELECT * FROM symbols WHERE name_tokens LIKE '%{esc}%' ESCAPE '\\' "
            "AND config_hash=? LIMIT 1",
            ("hash-aaa",),
        ).fetchone()
        assert row is None, (
            f"Escaped LIKE '%{esc}%' matched '100 percent' — "
            f"percent is being treated as a wildcard"
        )

    def test_percent_matches_literal_percent(self, db):
        """Escaped % in query term matches literal % in name_tokens."""
        _insert_symbol(db, usr="u_pct", name="pct",
                       qualified_name="mcu::pct",
                       name_tokens="100% full")
        esc = self._escape_like("100%")
        rows = db.execute(
            f"SELECT * FROM symbols WHERE name_tokens LIKE '%{esc}%' ESCAPE '\\' "
            "AND config_hash=? ORDER BY line",
            ("hash-aaa",),
        ).fetchall()
        assert len(rows) == 1, f"Expected 1 match (literal %), got {len(rows)}"
        assert rows[0]["name_tokens"] == "100% full"

    # ── Backslash ──────────────────────────────────────────────────────────

    def test_backslash_escaped_in_like(self, db):
        """Literal backslash in query term is correctly double-escaped."""
        _insert_symbol(db, usr="u_bs", name="path_join",
                       qualified_name="util::path_join",
                       name_tokens="path join")
        esc = self._escape_like("path\\join")
        row = db.execute(
            f"SELECT * FROM symbols WHERE name_tokens LIKE '%{esc}%' ESCAPE '\\' "
            "AND config_hash=? LIMIT 1",
            ("hash-aaa",),
        ).fetchone()
        assert row is None, (
            f"Escaped backslash LIKE '%{esc}%' unexpectedly matched"
        )

    # ── Docstring LIKE (same escaping, different column) ───────────────────

    def test_docstring_like_escapes_underscore(self, db):
        """Same escaping used in docstring LIKE fallback."""
        _insert_symbol(db, usr="u_ds1", name="fn", qualified_name="mcu::fn",
                       docstring="Initialize the UART peripheral with baud rate.")
        _insert_symbol(db, usr="u_ds2", name="fn2", qualified_name="mcu::fn2",
                       docstring="Configure test_function callback.")
        esc = self._escape_like("test_func")
        rows = db.execute(
            f"SELECT * FROM symbols WHERE docstring LIKE '%{esc}%' ESCAPE '\\' "
            "AND config_hash=? ORDER BY line",
            ("hash-aaa",),
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["name"] == "fn2"

    # ── Single quote (SQL injection prevention) ────────────────────────────

    def test_single_quote_escaped_in_like(self, db):
        """Single quotes in search terms are escaped to SQL ''."""
        _insert_symbol(db, usr="u_sq", name="it_s",
                       qualified_name="mcu::it_s",
                       name_tokens="it s")
        esc = self._escape_like("it's")
        row = db.execute(
            f"SELECT * FROM symbols WHERE name_tokens LIKE '%{esc}%' ESCAPE '\\' "
            "AND config_hash=? LIMIT 1",
            ("hash-aaa",),
        ).fetchone()
        assert row is None  # "it's" doesn't match "it s"

    # ── Empty term ─────────────────────────────────────────────────────────

    def test_empty_term_escaped_safely(self, db):
        """Empty string after escaping is safe for LIKE."""
        esc = self._escape_like("")
        assert esc == ""
        # Empty LIKE pattern matches everything — but this is a corner case
        # that should be caught by the caller (terms are filtered by len>1)
        rows = db.execute(
            f"SELECT * FROM symbols WHERE name_tokens LIKE '%{esc}%' ESCAPE '\\' "
            "AND config_hash=?",
            ("hash-aaa",),
        ).fetchall()
        # With empty pattern, matches everything — caller must guard against this
        assert len(rows) == 0  # no symbols inserted

    # ── Combined special chars ─────────────────────────────────────────────

    def test_combined_special_chars(self, db):
        """Term with _, %, \\ and ' all together is correctly escaped."""
        _insert_symbol(db, usr="u_co", name="weird",
                       qualified_name="mcu::weird",
                       name_tokens="100% test_func path\\join it s")
        esc = self._escape_like("100%_test\\func'path")
        # After escaping: 100\%\_test\\func''path
        # LIKE should NOT match the space-separated tokens because
        # the escaped special chars don't match the spaces
        row = db.execute(
            f"SELECT * FROM symbols WHERE name_tokens LIKE '%{esc}%' ESCAPE '\\' "
            "AND config_hash=? LIMIT 1",
            ("hash-aaa",),
        ).fetchone()
        assert row is None, (
            "Escaped combined special chars unexpectedly matched"
        )


class TestFallbackToSearchCode:
    """Test _fallback_to_search_code error handling and stale detection."""

    def test_missing_db_returns_graceful_error(self):
        """When no index exists, fallback returns structured error."""
        from fw_context_mcp.mcp.server import _fallback_to_search_code

        nonexistent = Path("/tmp/nonexistent_fwctx_test.db")
        result = _fallback_to_search_code(
            root=Path("/tmp"),
            db_path=nonexistent,
            query="uart_init",
            limit=10,
            warning="Test warning",
        )
        assert isinstance(result, list)
        assert len(result) == 1
        assert "error" in result[0], f"Expected error dict, got {result[0]}"
        # Error must mention the DB path so the user/LLM knows what's wrong
        err_msg = str(result[0].get("error", ""))
        assert "nonexistent" in err_msg.lower() or "index" in err_msg.lower(), (
            f"Error message should be descriptive, got: {err_msg}"
        )

    def test_empty_query_fallback(self):
        """Empty query doesn't crash the fallback."""
        from fw_context_mcp.mcp.server import _fallback_to_search_code

        result = _fallback_to_search_code(
            root=Path("/tmp"),
            db_path=Path("/tmp/nonexistent_fwctx_test_2.db"),
            query="",
            limit=10,
            warning="Empty query",
        )
        # Must return a list (error or warning), not crash
        assert isinstance(result, list)

    def test_get_active_build_docstring_covers_schema_staleness(self):
        """get_active_build docstring explains all three stale conditions."""
        from fw_context_mcp.mcp.server import get_active_build

        doc = get_active_build.__doc__ or ""
        assert "schema_version" in doc, "docstring must mention schema_version"
        assert "current_schema" in doc, "docstring must mention current_schema"
        assert "full" in doc.lower(), (
            "Docstring should mention that schema staleness needs a full re-index"
        )

    def test_get_active_build_docstring_stale_field(self):
        """get_active_build docstring mentions stale is a bool union."""
        from fw_context_mcp.mcp.server import get_active_build

        doc = get_active_build.__doc__ or ""
        assert "stale" in doc, "docstring must mention stale field"
        assert "modified_files_count" in doc, "docstring must mention modified_files_count"
