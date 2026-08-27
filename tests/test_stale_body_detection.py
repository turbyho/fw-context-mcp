"""Regression tests for stale-aware body reading in the source handler.

The index holds the line number of a symbol, but ``get_source`` reads the
body from the disk.  An edit that adds lines above the symbol moves it, and
the stored line number then points at unrelated code.  That code reads as a
valid function body, thus the caller cannot see the error.

These tests make sure that the handler detects this condition and never
gives the body of one symbol under the name of another.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from fw_context_mcp.mcp.handlers.source import (
    _body_matches_symbol,
    _read_probe_lines,
    _read_verified_body,
    _stored_file_mtime,
)

# The two functions are deliberately adjacent.  After the shift below, the
# stored line number of `modem_init` lands on `other_function` — the exact
# failure this module tests for.
_ORIGINAL = """void other_function(void) {
    int x = 1;
}

void modem_init(void) {
    uart_start();
}
"""

# Four lines of padding move every symbol down by four.
_PADDING = "#include <stdint.h>\n#include <stddef.h>\n\n\n"

_MODEM_INIT_LINE = 5
_MODEM_INIT_END_LINE = 7
_MODEM_INIT_BODY = "void modem_init(void) {\n    uart_start();\n}"


def _make_row(**overrides) -> dict:
    """Build a symbols row for ``modem_init`` in the unshifted file."""
    row = {
        "line": _MODEM_INIT_LINE,
        "end_line": _MODEM_INIT_END_LINE,
        "name": "modem_init",
        "source": _MODEM_INIT_BODY,
    }
    row.update(overrides)
    return row


@pytest.fixture
def source_file(tmp_path: Path) -> Path:
    """Write the unshifted C file and give its path."""
    path = tmp_path / "modem.c"
    path.write_text(_ORIGINAL)
    return path


def _shift_file(path: Path) -> None:
    """Add four lines above the symbols and make the file look newer."""
    path.write_text(_PADDING + path.read_text())
    # The mtime must be far enough ahead to clear MTIME_TOLERANCE_S (1.0 s).
    now = os.path.getmtime(path)
    os.utime(path, (now + 100, now + 100))


def _indexed_mtime(path: Path) -> float:
    """Give an mtime that reports the file as unchanged."""
    return os.path.getmtime(path) + 100


class TestReadProbeLines:
    def test_reads_from_the_given_line(self, source_file: Path):
        lines = _read_probe_lines(str(source_file), _MODEM_INIT_LINE)
        assert lines[0] == "void modem_init(void) {"

    def test_gives_no_line_numbers(self, source_file: Path):
        """The text feeds a comparison, thus it must stay raw."""
        lines = _read_probe_lines(str(source_file), 1)
        assert lines[0] == "void other_function(void) {"

    def test_missing_file_gives_empty_list(self, tmp_path: Path):
        assert _read_probe_lines(str(tmp_path / "absent.c"), 1) == []


class TestBodyMatchesSymbol:
    def test_indexed_body_matches_the_disk(self, source_file: Path):
        assert _body_matches_symbol(str(source_file), _make_row()) is True

    def test_moved_symbol_does_not_match(self, source_file: Path):
        """This is the defect: line 5 now holds a different function."""
        _shift_file(source_file)
        assert _body_matches_symbol(str(source_file), _make_row()) is False

    def test_name_probe_without_an_indexed_body(self, source_file: Path):
        """An index that holds no body falls back to a name comparison."""
        row = _make_row(source="")
        assert _body_matches_symbol(str(source_file), row) is True

    def test_name_probe_rejects_a_moved_symbol(self, source_file: Path):
        _shift_file(source_file)
        row = _make_row(source="")
        assert _body_matches_symbol(str(source_file), row) is False

    def test_name_on_a_later_line_still_matches(self, tmp_path: Path):
        """A signature can continue over more than one line."""
        path = tmp_path / "multi.c"
        path.write_text("__attribute__((weak))\nstatic void\nmodem_init(void) {\n}\n")
        row = _make_row(line=1, end_line=4, source="")
        assert _body_matches_symbol(str(path), row) is True

    def test_missing_file_does_not_match(self, tmp_path: Path):
        assert _body_matches_symbol(str(tmp_path / "absent.c"), _make_row()) is False


class TestReadVerifiedBody:
    def test_unchanged_file_reads_the_disk(self, source_file: Path):
        text, origin, warning = _read_verified_body(
            _make_row(), str(source_file), _indexed_mtime(source_file)
        )
        assert origin == "disk"
        assert warning is None
        assert "uart_start();" in text

    def test_changed_file_with_the_symbol_in_place(self, source_file: Path):
        """An edit inside the body keeps the line number correct."""
        source_file.write_text(_ORIGINAL.replace("uart_start();", "uart_start_v2();"))
        now = os.path.getmtime(source_file)
        os.utime(source_file, (now + 100, now + 100))
        row = _make_row(source="")

        text, origin, warning = _read_verified_body(row, str(source_file), now - 100)

        assert origin == "disk"
        assert "uart_start_v2();" in text, "the disk holds the current body"
        assert warning is not None and "current" in warning

    def test_the_unguarded_reader_shows_the_defect(self, source_file: Path):
        """Show what ``_read_symbol_body`` alone gives on a moved symbol.

        This test documents the defect that ``_read_verified_body`` corrects.
        It asserts the behaviour of the low-level reader, which stays
        unchanged on purpose: the guard belongs one level up.
        """
        from fw_context_mcp.mcp.handlers.source import _read_symbol_body

        _shift_file(source_file)

        unguarded = _read_symbol_body(
            str(source_file), _MODEM_INIT_LINE, end_line=_MODEM_INIT_END_LINE
        )

        assert "other_function" in unguarded, "the stored line now holds another symbol"
        assert "uart_start();" not in unguarded

    def test_moved_symbol_falls_back_to_the_index(self, source_file: Path):
        """The core regression: never give the body of another function."""
        indexed_mtime = os.path.getmtime(source_file)
        _shift_file(source_file)

        text, origin, warning = _read_verified_body(
            _make_row(), str(source_file), indexed_mtime
        )

        assert origin == "index"
        assert "uart_start();" in text
        assert "int x = 1;" not in text, "must not give the body of other_function"
        assert warning is not None and "modem_init" in warning

    def test_both_origins_use_the_same_line_number_format(self, source_file: Path):
        """A caller must not have to tell the two origins apart by shape.

        _read_symbol_body prefixes every line with its number.  The indexed
        body is stored bare, thus it needs the same prefix on the way out.
        """
        disk_text, disk_origin, _ = _read_verified_body(
            _make_row(), str(source_file), _indexed_mtime(source_file)
        )
        indexed_mtime = os.path.getmtime(source_file)
        _shift_file(source_file)
        index_text, index_origin, _ = _read_verified_body(
            _make_row(), str(source_file), indexed_mtime
        )

        assert (disk_origin, index_origin) == ("disk", "index")
        for label, text in (("disk", disk_text), ("index", index_text)):
            first = text.splitlines()[0]
            assert first[:4].strip().isdigit(), f"{label} body has no line number: {first!r}"
            assert first[4:6] == "  ", f"{label} body has the wrong prefix: {first!r}"

        # The indexed body keeps the line numbers the index holds.
        assert index_text.splitlines()[0].startswith(f"{_MODEM_INIT_LINE:4d}  ")

    def test_moved_symbol_without_an_indexed_body(self, source_file: Path):
        """With no stored body there is nothing safe to give."""
        indexed_mtime = os.path.getmtime(source_file)
        _shift_file(source_file)
        row = _make_row(source="")

        text, origin, warning = _read_verified_body(row, str(source_file), indexed_mtime)

        assert text == ""
        assert origin == ""
        assert warning is not None and "fw-context index" in warning

    def test_deleted_file_gives_the_indexed_body(self, source_file: Path):
        indexed_mtime = os.path.getmtime(source_file)
        source_file.unlink()

        text, origin, warning = _read_verified_body(
            _make_row(), str(source_file), indexed_mtime
        )

        assert origin == "index"
        assert "uart_start();" in text
        assert warning is not None


class TestStoredFileMtime:
    def test_gives_the_stored_value(self, populated_db):
        from fw_context_mcp.indexer.db import transaction, upsert_file

        with transaction(populated_db):
            file_id = upsert_file(
                populated_db, "hash-deadbeef", "/tmp/modem.c", "c", mtime=1234.5
            )

        assert _stored_file_mtime(populated_db, file_id) == pytest.approx(1234.5)

    def test_absent_row_gives_zero(self, populated_db):
        """0.0 makes the caller take the safe path instead of trusting a gap."""
        assert _stored_file_mtime(populated_db, 999999) == 0.0
