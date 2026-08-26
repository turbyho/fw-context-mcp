"""Equivalence of an incremental index with a rebuild from an empty database.

These tests state the invariant the stale-row cleanup exists to hold: after a
source change, an incremental run must leave the rows a build from scratch
would produce.  A missing row means the incremental path failed to add new
data; an extra row means it failed to remove data that is gone.  Neither shows
up in the other tests, because both leave an index that still answers queries
— just not with the truth.

The reference side deletes the database first.  ``--force`` is NOT usable as
the reference: it re-parses every TU but writes into the same database under
the same config_hash, so a defect in the row cleanup leaves the same stale
rows on both sides and the comparison stays green.  This was verified by
probe — with the cleanup disabled, 15 tests in test_incremental_reindex.py
fail while a ``--force`` comparison still passes.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from fw_context_mcp.indexer.db import open_db

# Fixtures are imported, not moved to conftest: `c_project` builds the whole
# fake C project and depends on three module-level helpers there, so the move
# would rewrite most of test_incremental_reindex.py for no behaviour change.
# The import makes pytest resolve the fixtures here; ruff reads it as an unused
# import (F401) and reads each fixture parameter as a redefinition (F811),
# because it cannot tell a fixture parameter from a shadowing binding.
from tests.test_incremental_reindex import (  # noqa: F401
    _advance_mtime,
    _cli,
    _db_path_for_project,
    c_project,
    indexed_project,
)


def _index(proj: Path, *extra: str):
    """Index without refs, embeddings, or LLM analysis.

    The invariant under test is symbol and content bookkeeping.  The excluded
    stages cost minutes and write no rows this comparison reads.
    """
    return _cli(
        ["index", "--no-refs", "--no-analyze", "--no-embeddings", *extra,
         str(proj / "compile_commands.json")],
        cwd=proj,
    )


def _rebuild_from_scratch(proj: Path) -> Path:
    """Drop the database, index again, and return the new database path.

    An empty database cannot carry a stale row forward, which is what makes
    this side the reference the incremental result is measured against.
    """
    shutil.rmtree(_db_path_for_project(proj).parent)
    result = _index(proj)
    assert result.returncode == 0, result.stderr
    db_path = _db_path_for_project(proj)
    assert db_path.exists(), f"rebuild produced no database at {db_path}"
    return db_path


def _active_config_hash(conn) -> str:
    """The config_hash written last — the build both runs produce."""
    return conn.execute(
        "SELECT config_hash FROM build_configs ORDER BY rowid DESC LIMIT 1"
    ).fetchone()[0]


def _snapshot(db_path: Path) -> set[tuple]:
    """Every symbol of the active build, as a comparable set.

    Rows are converted to plain tuples: the driver's row type compares and
    hashes, but does not sort, and the failure messages sort the difference to
    keep it readable.
    """
    conn = open_db(db_path)
    try:
        return {
            tuple(row)
            for row in conn.execute(
                "SELECT usr, name, file_path, line, end_line, is_definition, signature "
                "FROM symbols WHERE config_hash=?",
                (_active_config_hash(conn),),
            ).fetchall()
        }
    finally:
        conn.close()


def _macro_snapshot(db_path: Path) -> set[tuple]:
    """Every macro of the active build, keyed by path instead of file_id.

    ``macros.file_id`` is a row id, so it differs between two databases even
    for the same file.  The join makes the two sides comparable.
    """
    conn = open_db(db_path)
    try:
        return {
            tuple(row)
            for row in conn.execute(
                "SELECT f.path, m.name, m.value, m.line, m.is_function_like "
                "FROM macros m JOIN files f ON f.id = m.file_id "
                "WHERE m.config_hash=?",
                (_active_config_hash(conn),),
            ).fetchall()
        }
    finally:
        conn.close()


def _content_snapshot(db_path: Path) -> dict[str, str]:
    """Stored file content, project files only.

    Out-of-project files (system and toolchain headers) keep an absolute path
    in ``files.path``; project files keep a relative one.  Excluding the
    absolute ones keeps the comparison independent of which system headers the
    host toolchain happens to pull in.
    """
    conn = open_db(db_path)
    try:
        return {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT path, content FROM files "
                "WHERE config_hash=? AND path NOT LIKE '/%'",
                (_active_config_hash(conn),),
            ).fetchall()
        }
    finally:
        conn.close()


# A header that owns entities outright: an inline function, a macro and a
# struct have no definition in any .c file, so removing one from the header
# must remove the row.  A plain declaration cannot serve here — its row is
# shared with the definition in the .c file and legitimately survives.
_SEEDED_HEADER = """\
#ifndef MODEM_H
#define MODEM_H

#define MODEM_DOOMED_MACRO 1
#define MODEM_MOVED_MACRO  2

struct modem_doomed_stats { int sent; };
struct modem_moved_stats  { int recv; };

void modem_init(int baudrate);
int modem_send(const char* data, int len);
int modem_recv(char* buf, int max_len);

static inline int modem_doomed_inline(int x) { return x + 1; }
static inline int modem_moved_inline(int x) { return x + 2; }

#endif
"""

# The same header after the edit: the DOOMED entities are gone, the MOVED
# ones sit on different lines, and three entities are new.
_EDITED_HEADER = """\
#ifndef MODEM_H
#define MODEM_H

#define MODEM_ADDED_MACRO 3

struct modem_added_stats { int dropped; };

void modem_init(int baudrate);
int modem_send(const char* data, int len);
int modem_recv(char* buf, int max_len);

#define MODEM_MOVED_MACRO 2

struct modem_moved_stats { int recv; };

static inline int modem_moved_inline(int x) { return x + 2; }
static inline int modem_added_inline(int x) { return x + 3; }

#endif
"""


def _seed(proj: Path, header: Path, text: str) -> None:
    """Write *text* and rebuild, so the index holds it as the starting state."""
    header.write_text(text, encoding="utf-8")
    _advance_mtime(header, 5.0)
    _rebuild_from_scratch(proj)


def test_incremental_matches_rebuild_after_header_rewrite(
    indexed_project: Path,  # noqa: F811
):
    """Header-owned entities removed, moved, and added in one edit.

    The removals are what the comparison hinges on: an incremental run that
    inserts the new rows but keeps the old ones produces a superset, and no
    single-symbol assertion elsewhere sees it.
    """
    proj = indexed_project
    modem_h = proj / "src" / "modem.h"
    _seed(proj, modem_h, _SEEDED_HEADER)

    db_path = _db_path_for_project(proj)
    assert any("modem_doomed_inline" in row for row in _snapshot(db_path)), (
        "seed did not reach the index — the comparison below would be vacuous"
    )
    assert any("MODEM_DOOMED_MACRO" in row for row in _macro_snapshot(db_path)), (
        "seeded macro did not reach the index"
    )

    modem_h.write_text(_EDITED_HEADER, encoding="utf-8")
    _advance_mtime(modem_h, 10.0)
    result = _index(proj)
    assert result.returncode == 0, result.stderr

    incr_syms = _snapshot(db_path)
    incr_macros = _macro_snapshot(db_path)
    incr_content = _content_snapshot(db_path)

    fresh_db = _rebuild_from_scratch(proj)
    full_syms = _snapshot(fresh_db)
    full_macros = _macro_snapshot(fresh_db)
    full_content = _content_snapshot(fresh_db)

    assert not (full_syms - incr_syms), (
        f"incremental MISSED symbols: {sorted(full_syms - incr_syms)[:8]}"
    )
    assert not (incr_syms - full_syms), (
        f"incremental kept STALE symbols: {sorted(incr_syms - full_syms)[:8]}"
    )
    assert not (full_macros - incr_macros), (
        f"incremental MISSED macros: {sorted(full_macros - incr_macros)[:8]}"
    )
    assert not (incr_macros - full_macros), (
        f"incremental kept STALE macros: {sorted(incr_macros - full_macros)[:8]}"
    )

    assert incr_content.keys() == full_content.keys(), (
        f"content key mismatch: {set(incr_content) ^ set(full_content)}"
    )
    differing = [p for p in incr_content if incr_content[p] != full_content[p]]
    assert not differing, f"stored content differs for {differing}"


def test_incremental_matches_rebuild_after_header_shrinks_to_nothing(
    indexed_project: Path,  # noqa: F811
):
    """Every header-owned entity is deleted at once.

    A cleanup that runs only when the fresh parse has rows to insert would
    leave the whole set behind here, which the previous test cannot show.
    """
    proj = indexed_project
    modem_h = proj / "src" / "modem.h"
    _seed(proj, modem_h, _SEEDED_HEADER)
    db_path = _db_path_for_project(proj)

    modem_h.write_text(
        "#ifndef MODEM_H\n"
        "#define MODEM_H\n"
        "\n"
        "void modem_init(int baudrate);\n"
        "int modem_send(const char* data, int len);\n"
        "int modem_recv(char* buf, int max_len);\n"
        "\n"
        "#endif\n",
        encoding="utf-8",
    )
    _advance_mtime(modem_h, 10.0)
    result = _index(proj)
    assert result.returncode == 0, result.stderr

    incr_syms = _snapshot(db_path)
    incr_macros = _macro_snapshot(db_path)

    fresh_db = _rebuild_from_scratch(proj)
    full_syms = _snapshot(fresh_db)
    full_macros = _macro_snapshot(fresh_db)

    assert not (incr_syms - full_syms), (
        f"incremental kept STALE symbols: {sorted(incr_syms - full_syms)[:8]}"
    )
    assert not (full_syms - incr_syms), (
        f"incremental MISSED symbols: {sorted(full_syms - incr_syms)[:8]}"
    )
    assert not (incr_macros - full_macros), (
        f"incremental kept STALE macros: {sorted(incr_macros - full_macros)[:8]}"
    )
    assert not (full_macros - incr_macros), (
        f"incremental MISSED macros: {sorted(full_macros - incr_macros)[:8]}"
    )
