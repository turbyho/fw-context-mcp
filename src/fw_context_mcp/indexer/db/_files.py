"""File-record storage layer for fw-context-mcp index."""

from __future__ import annotations

import sqlite3
from typing import NamedTuple

from ...utils import escape_like as _escape_like

__all__ = [
    "FileHashRecord",
    "delete_orphan_files",
    "delete_symbols_for_file",
    "get_file_hashes",
    "get_file_map",
    "get_file_mtime_indexed",
    "get_file_mtimes",
    "upsert_file",
]


def upsert_file(
    conn: sqlite3.Connection,
    config_hash: str,
    path: str,
    language: str,
    generated: bool = False,
    mtime: float = 0.0,
    content_hash: str = "",
    source_hash: str = "",
    flags_hash: str = "",
) -> int:
    """Insert or update a file record, returning its row id.

    ``generated`` is converted from ``bool`` to ``int`` (0/1) for storage.
    ``language`` must be ``"c"`` or ``"cpp"``.

    Uses ``ON CONFLICT`` so re-indexing the same ``(config_hash, path)``
    updates language and mtime without duplication.

    Args:
        conn: Open database connection.
        config_hash: Build config hash the file belongs to.
        path: Relative or absolute file path.
        language: ``"c"`` or ``"cpp"`` — set during compilation database parsing.
        generated: Whether the file is auto-generated (default False).
        mtime: Last-modified timestamp (float, seconds since epoch).
        content_hash: SHA-256 hash of source + flags + manifest_entry content (default "").
        source_hash: SHA-256 hash of the source file content.
        flags_hash: SHA-256 hash of normalized compiler flags for this TU.

    Returns:
        int: The ``files.id`` of the inserted or updated row.
    """
    cur = conn.execute(
        """INSERT INTO files(config_hash, path, language, generated, mtime,
                             content_hash, source_hash, flags_hash)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(config_hash, path) DO UPDATE SET
               language=excluded.language,
               mtime=excluded.mtime,
               content_hash=excluded.content_hash,
               source_hash=excluded.source_hash,
               flags_hash=excluded.flags_hash
           RETURNING id""",
        (
            config_hash,
            path,
            language,
            int(generated),
            mtime,
            content_hash,
            source_hash,
            flags_hash,
        ),
    )
    row = cur.fetchone()
    if row is not None:
        return row[0]
    # Fallback for SQLite < 3.35 without RETURNING support.
    # Use SELECT instead of last_insert_rowid() — the latter returns
    # the rowid of the last INSERT, which is wrong when ON CONFLICT
    # triggered an UPDATE (rowid stays unchanged or is zero).
    return conn.execute(
        "SELECT id FROM files WHERE config_hash=? AND path=?",
        (config_hash, path),
    ).fetchone()[0]


def get_file_mtimes(conn: sqlite3.Connection, config_hash: str) -> dict[str, tuple[int, float]]:
    """Return {path: (file_id, mtime)} for all files under config_hash.

    .. deprecated::
        Use :func:`get_file_hashes` instead — it returns content hashes
        needed for content-addressable staleness detection.
    """
    rows = conn.execute("SELECT id, path, mtime FROM files WHERE config_hash=?", (config_hash,)).fetchall()
    return {r["path"]: (r["id"], r["mtime"]) for r in rows}


class FileHashRecord(NamedTuple):
    """Stored file metadata for content-addressable staleness detection."""
    file_id: int
    mtime: float
    content_hash: str
    source_hash: str
    flags_hash: str


def get_file_hashes(conn: sqlite3.Connection, config_hash: str) -> dict[str, FileHashRecord]:
    """Return ``{path: FileHashRecord}`` for content-addressable staleness detection.

    Used by the runner for content-addressable staleness detection — even
    when mtime differs, a matching content_hash means the TU can be skipped.

    The old ``deps_hash`` and ``deps_exist`` columns are deprecated and no
    longer populated — staleness is now determined from ``manifest.json``.
    """
    rows = conn.execute(
        """SELECT id, path, mtime, content_hash, source_hash, flags_hash
           FROM files WHERE config_hash=?""",
        (config_hash,),
    ).fetchall()
    return {
        r["path"]: FileHashRecord(
            r["id"],
            r["mtime"],
            r["content_hash"],
            r["source_hash"],
            r["flags_hash"],
        )
        for r in rows
    }


def delete_symbols_for_file(conn: sqlite3.Connection, file_id: int) -> None:
    """Delete all symbols for a file (FTS ad trigger cleans up FTS index)."""
    conn.execute("DELETE FROM symbols WHERE file_id=?", (file_id,))



def delete_orphan_files(conn: sqlite3.Connection, config_hash: str) -> int:
    """Delete file records that have no symbols, no macros, and no ifdef-filtered
    content for *config_hash*.

    Only deletes truly empty file entries — files with filled ``content``
    are preserved even when they have no symbols (forward-declaration
    headers are still useful for ``search_content``).

    Called after indexing or reindex to clean up stale entries from
    removed source files.  Returns the number of rows deleted.
    """
    cur = conn.execute(
        """DELETE FROM files WHERE config_hash = ?
           AND id NOT IN (SELECT DISTINCT file_id FROM symbols WHERE config_hash = ?)
           AND id NOT IN (SELECT DISTINCT file_id FROM macros WHERE config_hash = ?)
           AND content = ''""",
        (config_hash, config_hash, config_hash),
    )
    return cur.rowcount


def get_file_mtime_indexed(conn: sqlite3.Connection, config_hash: str, path: str) -> float | None:
    """Return the stored mtime for a file, or None if not in the index."""
    row = conn.execute(
        "SELECT mtime FROM files WHERE config_hash=? AND path=?",
        (config_hash, path),
    ).fetchone()
    return row["mtime"] if row else None


def get_file_map(
    conn: sqlite3.Connection,
    config_hash: str,
    file_path: str,
    signatures: bool = False,
    max_per_kind: int = 30,
) -> dict:
    """Return all symbols in a file grouped by kind — fast structural overview.

    *file_path* is relative to the project root (e.g. ``src/modem_msg.cpp``),
    matching the ``symbols.file_path`` column.  Exact match first, then
    LIKE-based path match so both ``src/main.cpp`` and ``main.cpp`` work.

    *signatures* adds full signatures (off by default to keep output compact).
    *max_per_kind* limits items per kind; the ``count`` field always shows the
    real total.  Set to 0 for no limit.
    """
    rows = conn.execute(
        """SELECT name, qualified_name, kind, line, col, end_line,
                  is_definition, signature, enum_value
           FROM symbols
           WHERE config_hash = ? AND file_path = ?
           ORDER BY kind, line""",
        (config_hash, file_path),
    ).fetchall()

    if not rows:
        rows = conn.execute(
            """SELECT name, qualified_name, kind, line, col, end_line,
                      is_definition, signature, enum_value
               FROM symbols
               WHERE config_hash = ? AND (file_path = ? OR file_path LIKE ? ESCAPE '\\')
               ORDER BY kind, line""",
            (config_hash, file_path, f"%{_escape_like(file_path)}"),
        ).fetchall()

    groups: dict[str, dict] = {}
    for r in rows:
        kind = r["kind"]
        if kind not in groups:
            groups[kind] = {"count": 0, "items": []}
        groups[kind]["count"] += 1

        if kind == "enum_constant":
            # Group by parent enum: extract everything before last "::"
            qn = r["qualified_name"] or ""
            if "::" in qn:
                parent_enum = qn.rsplit("::", 1)[0]
            else:
                parent_enum = "(anonymous)"
            # Initialize subgroup dict — stored in a special key on the kind group
            subgroups = groups[kind].setdefault("_subgroups", {})
            if parent_enum not in subgroups:
                subgroups[parent_enum] = {"name": parent_enum, "count": 0, "constants": []}
            subgroups[parent_enum]["count"] += 1
            if max_per_kind == 0 or subgroups[parent_enum]["count"] <= max_per_kind:
                entry: dict = {
                    "name": r["name"],
                    "qualified_name": r["qualified_name"],
                    "line": r["line"],
                }
                if r["enum_value"] is not None:
                    entry["enum_value"] = r["enum_value"]
                subgroups[parent_enum]["constants"].append(entry)
        else:
            if max_per_kind == 0 or len(groups[kind]["items"]) < max_per_kind:
                entry = {
                    "name": r["name"],
                    "qualified_name": r["qualified_name"],
                    "line": r["line"],
                }
                if signatures and r["signature"]:
                    entry["signature"] = r["signature"]
                groups[kind]["items"].append(entry)

    # Convert enum_constant subgroups from dict to sorted list
    for group in groups.values():
        if "_subgroups" in group:
            group["subgroups"] = sorted(group.pop("_subgroups").values(), key=lambda g: g["name"])

    return {
        "file": file_path,
        "total_symbols": len(rows),
        "symbols": groups,
    }
