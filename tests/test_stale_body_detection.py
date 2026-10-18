"""Regression tests for stale-aware body reading in the source handler.

``get_source`` takes the body and its line number from the index while the
file on disk still matches it, because only the indexed body is
ifdef-filtered.  Once the file changes, the disk holds the current text and
the index holds the filtered one, and the two cannot both be right.

An edit that adds lines above a symbol moves it, and the stored line number
then points at unrelated code.  That code reads as a valid function body,
thus the caller cannot see the error.

These tests make sure that the handler detects this condition and never
gives the body of one symbol under the name of another.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from fw_context_mcp.mcp.handlers import source as _source
from fw_context_mcp.mcp.handlers.source import (
    _body_matches_symbol,
    _read_probe_lines,
    _stored_file_state,
)


def _read_verified_body(row, file_path: str, stored_state: tuple[float, str]):
    """``source._read_verified_body`` with the default cap: these tests are not about the cap."""
    return _source._read_verified_body(row, file_path, stored_state, body_cap=_source._MAX_SYMBOL_BODY_LINES)


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
    def test_unchanged_file_reads_the_index(self, source_file: Path):
        """The stored body wins while the file matches it.

        Only ``symbols.source`` is ifdef-filtered.  The disk holds every
        branch, thus a body read from it would show the code of an inactive
        ``#if`` as live code.  For a file that did not change, the two texts
        describe the same symbol and the index is the one that answers the
        question the tool promises to answer.
        """
        text, origin, warning = _read_verified_body(
            _make_row(), str(source_file), (_indexed_mtime(source_file), "")
        )
        assert origin == "index"
        assert warning is None, "an unchanged file needs no warning"
        assert "uart_start();" in text

    def test_unchanged_file_without_a_stored_body_reads_the_disk(
        self, source_file: Path
    ):
        """A declaration stores no body, and only the disk can answer.

        Such a symbol has no extent to hold a branch, thus nothing is lost.
        """
        text, origin, warning = _read_verified_body(
            _make_row(source=""), str(source_file), (_indexed_mtime(source_file), "")
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

        text, origin, warning = _read_verified_body(row, str(source_file), (now - 100, ""))

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
            _make_row(), str(source_file), (indexed_mtime, "")
        )

        assert origin == "index"
        assert "uart_start();" in text
        assert "int x = 1;" not in text, "must not give the body of other_function"
        assert warning is not None and "modem_init" in warning

    def test_both_origins_use_the_same_line_number_format(self, source_file: Path):
        """A caller must not have to tell the two origins apart by shape.

        _read_symbol_body prefixes every line with its number.  The indexed
        body is stored bare, thus it needs the same prefix on the way out.

        The disk body comes from a row with no stored text — that is the one
        path left that reads the file while the file still matches the index.
        """
        disk_text, disk_origin, _ = _read_verified_body(
            _make_row(source=""), str(source_file), (_indexed_mtime(source_file), "")
        )
        indexed_mtime = os.path.getmtime(source_file)
        _shift_file(source_file)
        index_text, index_origin, _ = _read_verified_body(
            _make_row(), str(source_file), (indexed_mtime, "")
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

        text, origin, warning = _read_verified_body(row, str(source_file), (indexed_mtime, ""))

        assert text == ""
        assert origin == ""
        assert warning is not None and "fw-context index" in warning

    def test_deleted_file_gives_the_indexed_body(self, source_file: Path):
        indexed_mtime = os.path.getmtime(source_file)
        source_file.unlink()

        text, origin, warning = _read_verified_body(
            _make_row(), str(source_file), (indexed_mtime, "")
        )

        assert origin == "index"
        assert "uart_start();" in text
        assert warning is not None


class TestStoredFileState:
    def test_gives_the_stored_value(self, populated_db):
        from fw_context_mcp.indexer.db import transaction, upsert_file

        with transaction(populated_db):
            file_id = upsert_file(
                populated_db, "hash-deadbeef", "/tmp/modem.c", "c",
                mtime=1234.5, source_hash="abc123",
            )

        assert _stored_file_state(populated_db, file_id) == (pytest.approx(1234.5), "abc123")

    def test_a_row_without_a_hash_gives_an_empty_string(self, populated_db):
        """An index written before the hash existed must still work."""
        from fw_context_mcp.indexer.db import transaction, upsert_file

        with transaction(populated_db):
            file_id = upsert_file(
                populated_db, "hash-deadbeef", "/tmp/legacy.c", "c", mtime=99.0
            )

        assert _stored_file_state(populated_db, file_id) == (pytest.approx(99.0), "")

    def test_absent_row_gives_zero(self, populated_db):
        """0.0 makes the caller take the safe path instead of trusting a gap."""
        assert _stored_file_state(populated_db, 999999) == (0.0, "")


class TestReadFileDecidesByContent:
    """read_file was the last path still deciding by timestamp alone.

    Its SELECT did not read source_hash and its check did not take one, so
    the check fell back to the stamp — the one signal a git checkout
    rewrites without touching a byte.  Every other path had already moved to
    the content.
    """

    @staticmethod
    def _project(tmp_path: Path, *, with_hash: bool = True):
        """An indexed file with content, mtime and (optionally) a hash."""
        from fw_context_mcp.indexer.db import (
            open_db,
            transaction,
            upsert_build_config,
            upsert_file,
            upsert_project,
        )
        from fw_context_mcp.utils import compute_source_hash

        root = tmp_path / "proj"
        (root / "src").mkdir(parents=True)
        src = root / "src" / "main.c"
        src.write_text("int main(void) { return 0; }\n", encoding="utf-8")

        db_path = tmp_path / "index.db"
        conn = open_db(db_path)
        try:
            with transaction(conn):
                upsert_project(conn, "pid", "p", str(root))
                upsert_build_config(conn, "ch", "pid", str(root / "compile_commands.json"))
                upsert_file(
                    conn, "ch", "src/main.c", "c",
                    mtime=src.stat().st_mtime,
                    source_hash=compute_source_hash(src) if with_hash else "",
                )
                conn.execute(
                    "UPDATE files SET content=? WHERE config_hash='ch' AND path='src/main.c'",
                    ("int main(void) { return 0; }\n",),
                )
        finally:
            conn.close()
        return root, db_path, src

    @staticmethod
    def _row(db_path: Path):
        from fw_context_mcp.indexer.db import open_db

        conn = open_db(db_path)
        try:
            return conn.execute(
                "SELECT content, language, path, mtime, source_hash "
                "FROM files WHERE config_hash='ch' AND path='src/main.c'"
            ).fetchone()
        finally:
            conn.close()

    def test_a_git_touch_is_not_a_change(self, tmp_path: Path):
        """The regression: mtime moves, the bytes do not."""
        import os

        from fw_context_mcp.mcp.shared.stale import _file_differs

        root, db_path, src = self._project(tmp_path)
        row = self._row(db_path)
        os.utime(src, (row["mtime"] + 3600, row["mtime"] + 3600))

        assert _file_differs(str(src), row["mtime"], row["source_hash"] or "") is False, (
            "git checkout rewrites the mtime of a file it did not change; "
            "read_file must not warn about it"
        )

    def test_a_real_edit_is_a_change(self, tmp_path: Path):
        from fw_context_mcp.mcp.shared.stale import _file_differs

        root, db_path, src = self._project(tmp_path)
        row = self._row(db_path)
        src.write_text("int main(void) { return 1; }\n", encoding="utf-8")

        assert _file_differs(str(src), row["mtime"], row["source_hash"] or "") is True

    def test_without_a_hash_the_timestamp_still_decides(self, tmp_path: Path):
        """An index written before the column was filled must keep working."""
        import os

        from fw_context_mcp.mcp.shared.stale import _file_differs

        root, db_path, src = self._project(tmp_path, with_hash=False)
        row = self._row(db_path)
        assert not row["source_hash"]
        os.utime(src, (row["mtime"] + 3600, row["mtime"] + 3600))

        assert _file_differs(str(src), row["mtime"], "") is True, (
            "no hash to ask, thus the stamp is all there is — the old behaviour"
        )

    def test_read_file_itself_does_not_warn_after_a_git_touch(self, tmp_path: Path):
        """End to end, because half the defect was in read_file's SELECT.

        The column was never fetched, so no matter which helper the check
        called it had nothing to compare.  Exercising the helper alone would
        pass over exactly that half.
        """
        import os

        from fw_context_mcp.mcp.handlers.source import read_file

        root, src = _indexed_project(tmp_path)
        stored = os.path.getmtime(src)
        os.utime(src, (stored + 3600, stored + 3600))

        result = read_file(file_path="src/main.c", project_root=str(root))

        assert "error" not in result, result
        assert result.get("content"), "the indexed content must still come back"
        assert "stale" not in result and "stale_warning" not in result, (
            f"a git touch changed no byte, thus no warning: {result.get('stale_warning')}"
        )

    def test_read_file_itself_warns_on_a_real_edit(self, tmp_path: Path):
        """The other failure mode of the timestamp, made deterministic.

        The stamp is put back to what the index recorded, so only the bytes
        differ.  A timestamp comparison sees nothing — which is what a write
        landing inside MTIME_TOLERANCE_S of the index run does by itself,
        just without the wait.
        """
        import os

        from fw_context_mcp.mcp.handlers.source import read_file

        root, src = _indexed_project(tmp_path)
        stored = os.path.getmtime(src)
        src.write_text("int main(void) { return 1; }\n", encoding="utf-8")
        os.utime(src, (stored, stored))

        result = read_file(file_path="src/main.c", project_root=str(root))

        assert result.get("stale") is True, (
            "the bytes differ, thus the content check must report it even "
            "though the stamp is unchanged"
        )
        assert "changed after the last index run" in result["stale_warning"]


def _indexed_project(tmp_path: Path) -> tuple[Path, Path]:
    """A project resolve_db_context() can find, with one indexed file.

    The config points db_dir at tmp_path, so the database lands where
    _resolve_context computes it: ``db_dir / project_id / index.db``.
    """
    from fw_context_mcp.indexer.db import (
        open_db,
        transaction,
        upsert_build_config,
        upsert_file,
        upsert_project,
    )
    from fw_context_mcp.utils import compute_source_hash

    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / ".fw-context").mkdir()
    # A fixed id, like the other integration tests use: _resolve_context
    # computes db_dir / project_id / index.db, and db_dir points at tmp_path.
    project_id = "proj-001"
    (root / ".fw-context" / "config.toml").write_text(
        f'[project]\nid = "{project_id}"\n\n[build]\n\n[index]\ndb_dir = "{tmp_path}"\n',
        encoding="utf-8",
    )
    src = root / "src" / "main.c"
    src.write_text("int main(void) { return 0; }\n", encoding="utf-8")

    db_path = tmp_path / project_id / "index.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = open_db(db_path)
    try:
        with transaction(conn):
            upsert_project(conn, project_id, root.name, str(root))
            upsert_build_config(conn, "ch", project_id, str(root / "compile_commands.json"))
            upsert_file(
                conn, "ch", "src/main.c", "c",
                mtime=src.stat().st_mtime,
                source_hash=compute_source_hash(src),
            )
            conn.execute(
                "UPDATE files SET content=? WHERE config_hash='ch' AND path='src/main.c'",
                ("int main(void) { return 0; }\n",),
            )
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    return root, src


class TestAFullyFilteredFileIsAnnounced:
    """A file whose every line is dead must say so, not read as empty.

    The content pass keeps the line count of a file, thus a file whose every
    active line is inside an inactive branch is stored as blank lines.  That
    text is truthy, so it takes the authoritative index path of
    ``read_file`` — and the caller then receives content of the correct
    length that holds no text, with nothing to say why.

    A caller cannot tell that answer apart from an empty file, and the two
    mean opposite things: one file has no code, the other has code that the
    active build does not compile.
    """

    def _blank_the_stored_text(self, tmp_path: Path, text: str) -> None:
        from fw_context_mcp.indexer.db import open_db, transaction

        conn = open_db(tmp_path / "proj-001" / "index.db")
        try:
            with transaction(conn):
                conn.execute(
                    "UPDATE files SET content=? WHERE config_hash='ch' AND path='src/main.c'",
                    (text,),
                )
        finally:
            conn.close()

    def test_a_file_of_only_blank_lines_is_reported(self, tmp_path: Path) -> None:
        from fw_context_mcp.mcp.handlers.source import read_file

        root, _src = _indexed_project(tmp_path)
        self._blank_the_stored_text(tmp_path, "\n")

        result = read_file(file_path="src/main.c", project_root=str(root))

        assert "error" not in result, result
        assert result.get("all_lines_inactive") is True, (
            "every line of the stored text is blank, thus the caller must "
            "learn that the active build compiles no line of this file"
        )
        assert result.get("warning"), "the condition needs words, not only a flag"

    def test_a_file_with_live_code_is_not_reported(self, tmp_path: Path) -> None:
        """The flag must mark the real condition, not every file."""
        from fw_context_mcp.mcp.handlers.source import read_file

        root, _src = _indexed_project(tmp_path)

        result = read_file(file_path="src/main.c", project_root=str(root))

        assert "error" not in result, result
        assert "all_lines_inactive" not in result
        assert "warning" not in result

    def test_a_file_with_one_live_line_is_not_reported(self, tmp_path: Path) -> None:
        """One surviving line is enough to make the file a normal answer."""
        from fw_context_mcp.mcp.handlers.source import read_file

        root, _src = _indexed_project(tmp_path)
        self._blank_the_stored_text(tmp_path, "\n\nint live(void);\n\n")

        result = read_file(file_path="src/main.c", project_root=str(root))

        assert "error" not in result, result
        assert "all_lines_inactive" not in result


class TestTheLineCapReachesTheIndexBody:
    """``index.max_symbol_body_lines`` must bound both body origins.

    The disk path clamps its read with that setting.  The index path, which
    an unchanged file always takes, numbered the whole stored body and
    bounded nothing.  The configured cap therefore had no effect on the
    common case: a caller that raised it saw no change, and a caller that
    lowered it still received the whole body of a generated dispatch table.

    Only a blind cut at 8000 characters was left, and it sets no flag and
    cuts in the middle of a line.
    """

    def test_a_long_stored_body_is_capped(self) -> None:
        from fw_context_mcp.mcp.handlers import source as mod

        body = "".join(f"line {n}\n" for n in range(1, 101))

        numbered = mod._number_lines(body, 1, 10)

        assert len(numbered.splitlines()) == 10, (
            "the stored body holds 100 lines and the cap is 10, thus the "
            "index path must clamp it the way the disk path does"
        )
        assert "line 1" in numbered
        assert "line 100" not in numbered

    def test_a_short_body_is_untouched(self) -> None:
        """The cap must bound the long body only."""
        from fw_context_mcp.mcp.handlers import source as mod

        body = "a\nb\nc\n"

        numbered = mod._number_lines(body, 5, 10)

        assert len(numbered.splitlines()) == 3
        assert numbered.splitlines()[0].endswith("a")
        assert numbered.splitlines()[0].strip().startswith("5")
