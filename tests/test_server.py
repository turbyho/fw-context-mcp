"""Tests for fw_context_mcp.mcp.server helper functions."""

import sqlite3
from pathlib import Path

import pytest

from fw_context_mcp.mcp.server import _abs_path, _read_symbol_body


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
    UNIQUE(config_hash, usr)
);
CREATE INDEX IF NOT EXISTS idx_symbols_name  ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_qname ON symbols(qualified_name);
"""

EXACT_SQL = """SELECT s.* FROM symbols s
   WHERE s.config_hash=? AND (s.name=? OR s.qualified_name=?)
   ORDER BY s.is_definition DESC, s.line
   LIMIT ?"""

PREFIX_SQL = """SELECT s.* FROM symbols s
   WHERE s.config_hash=? AND (s.name LIKE ? OR s.qualified_name LIKE ?)
   ORDER BY s.is_definition DESC, s.line
   LIMIT ?"""


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
    }
    defaults.update(kw)
    c.execute(
        """INSERT INTO symbols (config_hash, file_id, file_path, name_tokens,
           usr, name, qualified_name, kind, line, col, end_line, is_definition,
           signature, docstring)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        tuple(defaults[f] for f in [
            "config_hash", "file_id", "file_path", "name_tokens",
            "usr", "name", "qualified_name", "kind", "line", "col",
            "end_line", "is_definition", "signature", "docstring",
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
        rows = db.execute(EXACT_SQL,
                          ("hash-aaa", "mbed::DigitalOut::write",
                           "mbed::DigitalOut::write", 50)).fetchall()
        assert len(rows) == 1
        assert rows[0]["name"] == "write"
        assert rows[0]["qualified_name"] == "mbed::DigitalOut::write"

    def test_exact_match_on_short_name_still_works(self, db):
        """Exact match by short name still works (backward compatibility)."""
        _insert_symbol(db, usr="u1", name="write",
                       qualified_name="mbed::DigitalOut::write")
        rows = db.execute(EXACT_SQL, ("hash-aaa", "write", "write", 50)).fetchall()
        assert len(rows) == 1

    def test_exact_no_match_on_wrong_qualified_name(self, db):
        """No false positives — wrong qualified name must not match."""
        _insert_symbol(db, usr="u1", name="write",
                       qualified_name="mbed::DigitalOut::write")
        rows = db.execute(EXACT_SQL,
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
        rows = db.execute(PREFIX_SQL,
                          ("hash-aaa", "mbed::DigitalOut%",
                           "mbed::DigitalOut%", 50)).fetchall()
        assert len(rows) == 1
        assert rows[0]["qualified_name"] == "mbed::DigitalOut::read"

    def test_prefix_match_on_short_name_still_works(self, db):
        """Prefix match by short name still works (backward compatibility)."""
        _insert_symbol(db, usr="u1", name="read_voltage",
                       qualified_name="mbed::AnalogIn::read_voltage")
        rows = db.execute(PREFIX_SQL,
                          ("hash-aaa", "read%", "read%", 50)).fetchall()
        assert len(rows) >= 1
        assert any(r["name"] == "read_voltage" for r in rows)

    def test_prefix_match_multiple_results(self, db):
        """Prefix matching on qualified_name can return multiple results."""
        _insert_symbol(db, usr="u1", name="write",
                       qualified_name="mbed::DigitalOut::write")
        _insert_symbol(db, usr="u2", name="write",
                       qualified_name="mbed::SPI::write")
        rows = db.execute(PREFIX_SQL,
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
        rows = db.execute(PREFIX_SQL,
                          ("hash-aaa", "mbed::DigitalOut::write%",
                           "mbed::DigitalOut::write%", 50)).fetchall()
        assert len(rows) == 2
        assert rows[0]["is_definition"] == 1
        assert rows[1]["is_definition"] == 0

