"""SQLite storage layer for fw-context-mcp index."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator


_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS projects (
    project_id   TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    root_path    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS build_configs (
    config_hash             TEXT PRIMARY KEY,
    project_id              TEXT NOT NULL REFERENCES projects(project_id),
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    compile_commands_path   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id           INTEGER PRIMARY KEY,
    config_hash  TEXT NOT NULL REFERENCES build_configs(config_hash),
    path         TEXT NOT NULL,
    language     TEXT NOT NULL,     -- 'c' | 'cpp'
    generated    INTEGER NOT NULL DEFAULT 0,
    mtime        REAL    NOT NULL DEFAULT 0,
    UNIQUE(config_hash, path)
);

CREATE TABLE IF NOT EXISTS symbols (
    id             INTEGER PRIMARY KEY,
    config_hash    TEXT    NOT NULL REFERENCES build_configs(config_hash),
    file_id        INTEGER NOT NULL REFERENCES files(id),
    usr            TEXT    NOT NULL,
    name           TEXT    NOT NULL,
    qualified_name TEXT    NOT NULL,
    kind           TEXT    NOT NULL,
    line           INTEGER NOT NULL,
    col            INTEGER NOT NULL,
    is_definition  INTEGER NOT NULL DEFAULT 0,
    signature      TEXT    NOT NULL DEFAULT '',
    docstring      TEXT    NOT NULL DEFAULT '',
    UNIQUE(config_hash, usr)
);

CREATE INDEX IF NOT EXISTS idx_symbols_name        ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_qname       ON symbols(qualified_name);
CREATE INDEX IF NOT EXISTS idx_symbols_kind        ON symbols(kind);
CREATE INDEX IF NOT EXISTS idx_symbols_file        ON symbols(file_id);
CREATE INDEX IF NOT EXISTS idx_files_config        ON files(config_hash);

CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts USING fts5(
    name,
    qualified_name,
    signature,
    docstring,
    content='symbols',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS symbols_ai AFTER INSERT ON symbols BEGIN
    INSERT INTO symbols_fts(rowid, name, qualified_name, signature, docstring)
    VALUES (new.id, new.name, new.qualified_name, new.signature, new.docstring);
END;

CREATE TRIGGER IF NOT EXISTS symbols_ad AFTER DELETE ON symbols BEGIN
    INSERT INTO symbols_fts(symbols_fts, rowid, name, qualified_name, signature, docstring)
    VALUES ('delete', old.id, old.name, old.qualified_name, old.signature, old.docstring);
END;

CREATE TRIGGER IF NOT EXISTS symbols_au AFTER UPDATE ON symbols BEGIN
    INSERT INTO symbols_fts(symbols_fts, rowid, name, qualified_name, signature, docstring)
    VALUES ('delete', old.id, old.name, old.qualified_name, old.signature, old.docstring);
    INSERT INTO symbols_fts(rowid, name, qualified_name, signature, docstring)
    VALUES (new.id, new.name, new.qualified_name, new.signature, new.docstring);
END;
"""


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    # Schema migration: add mtime column to files if missing
    try:
        conn.execute("ALTER TABLE files ADD COLUMN mtime REAL NOT NULL DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Generator[sqlite3.Connection, None, None]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def upsert_project(conn: sqlite3.Connection, project_id: str, name: str, root_path: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO projects(project_id, name, root_path) VALUES (?,?,?)",
        (project_id, name, root_path),
    )


def upsert_build_config(
    conn: sqlite3.Connection,
    config_hash: str,
    project_id: str,
    compile_commands_path: str,
) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO build_configs(config_hash, project_id, compile_commands_path)
           VALUES (?,?,?)""",
        (config_hash, project_id, compile_commands_path),
    )


def upsert_file(
    conn: sqlite3.Connection,
    config_hash: str,
    path: str,
    language: str,
    generated: bool = False,
    mtime: float = 0.0,
) -> int:
    cur = conn.execute(
        """INSERT INTO files(config_hash, path, language, generated, mtime)
           VALUES (?,?,?,?,?)
           ON CONFLICT(config_hash, path) DO UPDATE SET
               language=excluded.language,
               mtime=excluded.mtime
           RETURNING id""",
        (config_hash, path, language, int(generated), mtime),
    )
    row = cur.fetchone()
    return row[0]


def get_file_mtimes(conn: sqlite3.Connection, config_hash: str) -> dict[str, tuple[int, float]]:
    """Return {path: (file_id, mtime)} for all files under config_hash."""
    rows = conn.execute(
        "SELECT id, path, mtime FROM files WHERE config_hash=?", (config_hash,)
    ).fetchall()
    return {r["path"]: (r["id"], r["mtime"]) for r in rows}


def delete_symbols_for_file(conn: sqlite3.Connection, file_id: int) -> None:
    """Delete all symbols for a file (FTS ad trigger cleans up FTS index)."""
    conn.execute("DELETE FROM symbols WHERE file_id=?", (file_id,))


def insert_symbols_batch(
    conn: sqlite3.Connection,
    rows: list[tuple],
) -> int:
    """Insert symbol rows, promoting declaration→definition on USR conflict.

    Returns count of rows inserted or upgraded to definition.
    """
    cur = conn.executemany(
        """INSERT INTO symbols
           (config_hash, file_id, usr, name, qualified_name, kind,
            line, col, is_definition, signature, docstring)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(config_hash, usr) DO UPDATE SET
               file_id       = excluded.file_id,
               line          = excluded.line,
               col           = excluded.col,
               is_definition = 1,
               signature     = excluded.signature,
               docstring     = excluded.docstring
           WHERE excluded.is_definition = 1 AND symbols.is_definition = 0""",
        rows,
    )
    return cur.rowcount


def get_active_config(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row | None:
    """Return the most recently indexed build_config for a project."""
    return conn.execute(
        """SELECT * FROM build_configs WHERE project_id=?
           ORDER BY created_at DESC LIMIT 1""",
        (project_id,),
    ).fetchone()


def search_symbols(
    conn: sqlite3.Connection,
    query: str,
    config_hash: str,
    limit: int = 20,
) -> list[sqlite3.Row]:
    """FTS5 search over symbols for a given build config."""
    return conn.execute(
        """SELECT s.*, f.path as file_path
           FROM symbols_fts
           JOIN symbols s ON s.id = symbols_fts.rowid
           JOIN files f ON f.id = s.file_id
           WHERE symbols_fts MATCH ? AND s.config_hash = ?
           ORDER BY rank
           LIMIT ?""",
        (query, config_hash, limit),
    ).fetchall()


def get_all_projects(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return all projects with their latest build_config stats."""
    return conn.execute(
        """SELECT p.project_id, p.name, p.root_path,
                  b.config_hash, b.created_at, b.compile_commands_path,
                  COUNT(DISTINCT s.id) AS symbol_count,
                  COUNT(DISTINCT f.id) AS file_count
           FROM projects p
           LEFT JOIN build_configs b ON b.project_id = p.project_id
               AND b.created_at = (
                   SELECT MAX(created_at) FROM build_configs WHERE project_id = p.project_id
               )
           LEFT JOIN symbols s ON s.config_hash = b.config_hash
           LEFT JOIN files f ON f.config_hash = b.config_hash
           GROUP BY p.project_id""",
    ).fetchall()
