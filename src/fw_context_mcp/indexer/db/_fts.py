"""FTS5 (Full-Text Search) rebuild utilities."""

import sqlite3

__all__ = [
    "rebuild_files_fts",
    "rebuild_fts",
    "rebuild_macros_fts",
]



def rebuild_fts(conn: sqlite3.Connection) -> None:
    """Rebuild the FTS5 virtual table from the current ``symbols`` content.

    Drops and recreates ``symbols_fts``, populates it with a single
    ``INSERT ... SELECT`` from ``symbols``, then reinstates the three
    content-sync triggers (ai, ad, au).  Call after bulk indexing to
    restore FTS5 search capability in one pass instead of paying per-row
    trigger overhead for every symbol INSERT/DELETE/UPDATE.

    Idempotent — safe to call on an already-healthy FTS table.
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(symbols)").fetchall()]
    has_source = "source" in cols

    source_col = ", source" if has_source else ""
    source_sel = ", COALESCE(source,'')" if has_source else ""
    source_val_new = ", new.source" if has_source else ""
    source_val_old = ", old.source" if has_source else ""

    conn.executescript(f"""
        DROP TRIGGER IF EXISTS symbols_ai;
        DROP TRIGGER IF EXISTS symbols_ad;
        DROP TRIGGER IF EXISTS symbols_au;
        DROP TABLE IF EXISTS symbols_fts;

        CREATE VIRTUAL TABLE symbols_fts USING fts5(
            name, qualified_name, signature, docstring, file_path, name_tokens,
            summary, inputs, outputs{source_col},
            content='symbols', content_rowid='id'
        );

        INSERT INTO symbols_fts(rowid, name, qualified_name, signature, docstring,
                                file_path, name_tokens, summary, inputs, outputs{source_col})
        SELECT id, name, qualified_name, COALESCE(signature,''), COALESCE(docstring,''),
               COALESCE(file_path,''), COALESCE(name_tokens,''),
               COALESCE(summary,''), COALESCE(inputs,''), COALESCE(outputs,''){source_sel}
        FROM symbols;

        CREATE TRIGGER symbols_ai AFTER INSERT ON symbols BEGIN
            INSERT INTO symbols_fts(rowid, name, qualified_name, signature, docstring,
                                    file_path, name_tokens, summary, inputs, outputs{source_col})
            VALUES (new.id, new.name, new.qualified_name, new.signature, new.docstring,
                    new.file_path, new.name_tokens, new.summary, new.inputs, new.outputs{source_val_new});
        END;

        CREATE TRIGGER symbols_ad AFTER DELETE ON symbols BEGIN
            INSERT INTO symbols_fts(symbols_fts, rowid, name, qualified_name, signature,
                                    docstring, file_path, name_tokens, summary, inputs, outputs{source_col})
            VALUES ('delete', old.id, old.name, old.qualified_name, old.signature,
                    old.docstring, old.file_path, old.name_tokens, old.summary, old.inputs, old.outputs{source_val_old});
        END;

        CREATE TRIGGER symbols_au AFTER UPDATE ON symbols BEGIN
            INSERT INTO symbols_fts(symbols_fts, rowid, name, qualified_name, signature,
                                    docstring, file_path, name_tokens, summary, inputs, outputs{source_col})
            VALUES ('delete', old.id, old.name, old.qualified_name, old.signature,
                    old.docstring, old.file_path, old.name_tokens, old.summary, old.inputs, old.outputs{source_val_old});
            INSERT INTO symbols_fts(rowid, name, qualified_name, signature, docstring,
                                    file_path, name_tokens, summary, inputs, outputs{source_col})
            VALUES (new.id, new.name, new.qualified_name, new.signature, new.docstring,
                    new.file_path, new.name_tokens, new.summary, new.inputs, new.outputs{source_val_new});
        END;
    """)


def rebuild_files_fts(conn: sqlite3.Connection) -> None:
    """Rebuild the files_fts FTS5 index from existing files.content.

    Uses the already-populated ``files.content`` column (ifdef-filtered by
    ``_build_filtered_file_content`` during indexing).  Does NOT re-read
    files from disk — the content in the DB is authoritative.

    Drops triggers first, recreates the FTS5 table, backfills from
    ``files``, then reinstates content-sync triggers.
    """

    conn.executescript("""
        DROP TRIGGER IF EXISTS files_ai;
        DROP TRIGGER IF EXISTS files_ad;
        DROP TRIGGER IF EXISTS files_au;
        DROP TABLE IF EXISTS files_fts;

        CREATE VIRTUAL TABLE files_fts USING fts5(
            path, content,
            content='files', content_rowid='id'
        );
    """)

    conn.execute("""
        INSERT INTO files_fts(rowid, path, content)
        SELECT id, path, COALESCE(content,'') FROM files
    """)
    conn.commit()

    conn.executescript("""
        CREATE TRIGGER files_ai AFTER INSERT ON files BEGIN
            INSERT INTO files_fts(rowid, path, content)
            VALUES (new.id, new.path, new.content);
        END;
        CREATE TRIGGER files_ad AFTER DELETE ON files BEGIN
            INSERT INTO files_fts(files_fts, rowid, path, content)
            VALUES ('delete', old.id, old.path, old.content);
        END;
        CREATE TRIGGER files_au AFTER UPDATE ON files BEGIN
            INSERT INTO files_fts(files_fts, rowid, path, content)
            VALUES ('delete', old.id, old.path, old.content);
            INSERT INTO files_fts(rowid, path, content)
            VALUES (new.id, new.path, new.content);
        END;
    """)


def rebuild_macros_fts(conn: sqlite3.Connection) -> None:
    """Rebuild the macros_fts FTS5 index from existing macros rows.

    Drops triggers first, recreates the FTS5 table, backfills from
    ``macros``, then reinstates content-sync triggers.
    """

    conn.executescript("""
        DROP TRIGGER IF EXISTS macros_ai;
        DROP TRIGGER IF EXISTS macros_ad;
        DROP TRIGGER IF EXISTS macros_au;
        DROP TABLE IF EXISTS macros_fts;

        CREATE VIRTUAL TABLE macros_fts USING fts5(
            name, value, expanded_value,
            content='macros', content_rowid='id'
        );
    """)

    conn.execute("""
        INSERT INTO macros_fts(rowid, name, value, expanded_value)
        SELECT id, name, COALESCE(value,''), COALESCE(expanded_value,'') FROM macros
    """)
    conn.commit()

    conn.executescript("""
        CREATE TRIGGER macros_ai AFTER INSERT ON macros BEGIN
            INSERT INTO macros_fts(rowid, name, value, expanded_value)
            VALUES (new.id, new.name, new.value, new.expanded_value);
        END;
        CREATE TRIGGER macros_ad AFTER DELETE ON macros BEGIN
            INSERT INTO macros_fts(macros_fts, rowid, name, value, expanded_value)
            VALUES ('delete', old.id, old.name, old.value, old.expanded_value);
        END;
        CREATE TRIGGER macros_au AFTER UPDATE ON macros BEGIN
            INSERT INTO macros_fts(macros_fts, rowid, name, value, expanded_value)
            VALUES ('delete', old.id, old.name, old.value, old.expanded_value);
            INSERT INTO macros_fts(rowid, name, value, expanded_value)
            VALUES (new.id, new.name, new.value, new.expanded_value);
        END;
    """)
