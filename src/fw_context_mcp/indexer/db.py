"""SQLite storage layer for fw-context-mcp index."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path


class DatabaseCorruptionError(sqlite3.DatabaseError):
    """Raised when the SQLite database fails integrity check.

    The caller should present the error to the user with a clear action:
    run ``reset_index()`` then ``fw-context index`` to rebuild.
    """

    def __init__(self, db_path: str, details: str = ""):
        self.db_path = db_path
        self.details = details
        super().__init__(
            f"Database corruption detected at {db_path}: {details}"
        )


def split_tokens(name: str, qualified_name: str = "") -> str:
    """Normalize camelCase/snake_case names to space-separated lowercase tokens.

    Builds a searchable token graph in FTS5 where each camelCase component
    becomes an independent node, so ``connect*`` finds ``onConnectionComplete``,
    ``modem*`` finds ``ModemMsgManager``, etc.

    Examples:
        onConnectionComplete       → "on connection complete"
        modem_parser_oob_init      → "modem parser oob init"
        ZCfgDataManager            → "cfg data manager"
        HTTPResponse               → "http response"
        ZBLE::onConnectionComplete → "zble on connection complete"
        _last_ble_connected        → "last ble connected"
    """
    def _tokenize(s: str) -> list[str]:
        s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s)      # camelCase split
        s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", s)   # HTTPResponse → HTTP Response
        parts = re.split(r"[^a-zA-Z0-9]+", s)               # split on non-alnum
        return [p.lower() for p in parts if len(p) > 1]

    tokens: list[str] = []
    seen: set[str] = set()
    for src in (name, qualified_name):
        if src:
            for tok in _tokenize(src):
                if tok not in seen:
                    seen.add(tok)
                    tokens.append(tok)
    return " ".join(tokens)

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
    file_path      TEXT    NOT NULL DEFAULT '',
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
    file_path,
    name_tokens,
    content='symbols',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS symbols_ai AFTER INSERT ON symbols BEGIN
    INSERT INTO symbols_fts(rowid, name, qualified_name, signature, docstring, file_path, name_tokens)
    VALUES (new.id, new.name, new.qualified_name, new.signature, new.docstring, new.file_path, new.name_tokens);
END;

CREATE TRIGGER IF NOT EXISTS symbols_ad AFTER DELETE ON symbols BEGIN
    INSERT INTO symbols_fts(symbols_fts, rowid, name, qualified_name, signature, docstring, file_path, name_tokens)
    VALUES ('delete', old.id, old.name, old.qualified_name, old.signature, old.docstring, old.file_path, old.name_tokens);
END;

CREATE TRIGGER IF NOT EXISTS symbols_au AFTER UPDATE ON symbols BEGIN
    INSERT INTO symbols_fts(symbols_fts, rowid, name, qualified_name, signature, docstring, file_path, name_tokens)
    VALUES ('delete', old.id, old.name, old.qualified_name, old.signature, old.docstring, old.file_path, old.name_tokens);
    INSERT INTO symbols_fts(rowid, name, qualified_name, signature, docstring, file_path, name_tokens)
    VALUES (new.id, new.name, new.qualified_name, new.signature, new.docstring, new.file_path, new.name_tokens);
END;

-- Cross-reference / call graph (opt-in via [index] index_refs = true).
-- to_usr links to symbols.usr (the referenced definition); from_usr is the
-- enclosing function/method that contains the reference (may be NULL).
CREATE TABLE IF NOT EXISTS refs (
    id           INTEGER PRIMARY KEY,
    config_hash  TEXT    NOT NULL,
    to_usr       TEXT    NOT NULL,
    from_file    TEXT    NOT NULL,
    from_line    INTEGER NOT NULL,
    from_usr     TEXT,
    ref_kind     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_refs_to_usr   ON refs(config_hash, to_usr);
CREATE INDEX IF NOT EXISTS idx_refs_from_usr ON refs(config_hash, from_usr);
CREATE INDEX IF NOT EXISTS idx_refs_fromfile ON refs(config_hash, from_file);
"""


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row

    try:
        conn.executescript(_SCHEMA)

        # Migration: mtime column in files
        try:
            conn.execute("ALTER TABLE files ADD COLUMN mtime REAL NOT NULL DEFAULT 0")
            conn.commit()
        except sqlite3.OperationalError:
            pass

        # Migration: end_line column in symbols (full definition extent for get_source)
        try:
            conn.execute("ALTER TABLE symbols ADD COLUMN end_line INTEGER NOT NULL DEFAULT 0")
            conn.commit()
        except sqlite3.OperationalError:
            pass

        # Migration: file_path column in symbols (denormalized for FTS5)
        sym_cols = [r[1] for r in conn.execute("PRAGMA table_info(symbols)").fetchall()]
        if "file_path" not in sym_cols:
            conn.execute("ALTER TABLE symbols ADD COLUMN file_path TEXT NOT NULL DEFAULT ''")
            conn.execute("""
                UPDATE symbols SET file_path = COALESCE(
                    (SELECT f.path FROM files f WHERE f.id = symbols.file_id), ''
                ) WHERE file_path = ''
            """)
            conn.commit()

        # Migration: name_tokens column — camelCase/snake_case split for FTS5 token graph
        sym_cols = [r[1] for r in conn.execute("PRAGMA table_info(symbols)").fetchall()]
        if "name_tokens" not in sym_cols:
            conn.execute("ALTER TABLE symbols ADD COLUMN name_tokens TEXT NOT NULL DEFAULT ''")
            conn.commit()
            # Backfill using Python split_tokens (SQLite can't call Python functions).
            # Use a generator to avoid materialising all rows in memory at once.
            conn.executemany(
                "UPDATE symbols SET name_tokens = ? WHERE id = ?",
                (
                    (split_tokens(r["name"], r["qualified_name"]), r["id"])
                    for r in conn.execute(
                        "SELECT id, name, qualified_name FROM symbols WHERE name_tokens = ''"
                    )
                ),
            )
            conn.commit()

        # Migration: rebuild FTS5 + triggers if name_tokens column is missing
        fts_cols = [r[1] for r in conn.execute("PRAGMA table_info(symbols_fts)").fetchall()]
        if "name_tokens" not in fts_cols:
            conn.executescript("""
                DROP TRIGGER IF EXISTS symbols_ai;
                DROP TRIGGER IF EXISTS symbols_ad;
                DROP TRIGGER IF EXISTS symbols_au;
                DROP TABLE IF EXISTS symbols_fts;
            """)
            conn.executescript("""
                CREATE VIRTUAL TABLE symbols_fts USING fts5(
                    name, qualified_name, signature, docstring, file_path, name_tokens,
                    content='symbols', content_rowid='id'
                );
                CREATE TRIGGER symbols_ai AFTER INSERT ON symbols BEGIN
                    INSERT INTO symbols_fts(rowid, name, qualified_name, signature, docstring, file_path, name_tokens)
                    VALUES (new.id, new.name, new.qualified_name, new.signature, new.docstring, new.file_path, new.name_tokens);
                END;
                CREATE TRIGGER symbols_ad AFTER DELETE ON symbols BEGIN
                    INSERT INTO symbols_fts(symbols_fts, rowid, name, qualified_name, signature, docstring, file_path, name_tokens)
                    VALUES ('delete', old.id, old.name, old.qualified_name, old.signature, old.docstring, old.file_path, old.name_tokens);
                END;
                CREATE TRIGGER symbols_au AFTER UPDATE ON symbols BEGIN
                    INSERT INTO symbols_fts(symbols_fts, rowid, name, qualified_name, signature, docstring, file_path, name_tokens)
                    VALUES ('delete', old.id, old.name, old.qualified_name, old.signature, old.docstring, old.file_path, old.name_tokens);
                    INSERT INTO symbols_fts(rowid, name, qualified_name, signature, docstring, file_path, name_tokens)
                    VALUES (new.id, new.name, new.qualified_name, new.signature, new.docstring, new.file_path, new.name_tokens);
                END;
            """)
            conn.execute("""
                INSERT INTO symbols_fts(rowid, name, qualified_name, signature, docstring, file_path, name_tokens)
                SELECT id, name, qualified_name, COALESCE(signature,''), COALESCE(docstring,''),
                       COALESCE(file_path,''), COALESCE(name_tokens,'')
                FROM symbols
            """)
            conn.commit()

    except sqlite3.DatabaseError as e:
        conn.close()
        raise DatabaseCorruptionError(str(path), str(e)) from e

    # Integrity check — detect corruption early, before any tool uses the DB
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()
        if result and result[0] != "ok":
            details = result[0]
            conn.close()
            raise DatabaseCorruptionError(str(path), details)
    except sqlite3.DatabaseError as e:
        conn.close()
        raise DatabaseCorruptionError(str(path), str(e)) from e

    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Generator[sqlite3.Connection, None, None]:
    try:
        yield conn
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
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
        """INSERT INTO build_configs(config_hash, project_id, compile_commands_path)
           VALUES (?,?,?)
           ON CONFLICT(config_hash) DO UPDATE SET
               created_at = datetime('now'),
               compile_commands_path = excluded.compile_commands_path""",
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

    Each row: (config_hash, file_id, file_path, name_tokens, usr, name,
               qualified_name, kind, line, col, end_line, is_definition,
               signature, docstring)

    Returns count of rows inserted or upgraded to definition.
    """
    cur = conn.executemany(
        """INSERT INTO symbols
           (config_hash, file_id, file_path, name_tokens, usr, name, qualified_name, kind,
            line, col, end_line, is_definition, signature, docstring)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(config_hash, usr) DO UPDATE SET
               file_id       = excluded.file_id,
               file_path     = excluded.file_path,
               name_tokens   = excluded.name_tokens,
               line          = excluded.line,
               col           = excluded.col,
               end_line      = excluded.end_line,
               is_definition = 1,
               signature     = excluded.signature,
               docstring     = excluded.docstring
           WHERE excluded.is_definition = 1 AND symbols.is_definition = 0""",
        rows,
    )
    return cur.rowcount


def insert_refs_batch(conn: sqlite3.Connection, rows: list[tuple]) -> int:
    """Insert reference rows for the cross-reference / call graph.

    Each row: (config_hash, to_usr, from_file, from_line, from_usr, ref_kind)
    Returns number of rows inserted.
    """
    cur = conn.executemany(
        """INSERT INTO refs (config_hash, to_usr, from_file, from_line, from_usr, ref_kind)
           VALUES (?,?,?,?,?,?)""",
        rows,
    )
    return cur.rowcount


def delete_refs_for_file(conn: sqlite3.Connection, config_hash: str, from_file: str) -> None:
    """Delete all references originating in a given file (for incremental reindex)."""
    conn.execute(
        "DELETE FROM refs WHERE config_hash=? AND from_file=?",
        (config_hash, from_file),
    )


def find_refs(
    conn: sqlite3.Connection,
    config_hash: str,
    name: str,
    ref_kind: str | None = None,
    limit: int = 50,
) -> list[sqlite3.Row]:
    """Find references to a symbol by name.

    Resolves the target name → its USR(s) via symbols, then joins refs.to_usr.
    Each result carries the referencing location and, when known, the enclosing
    caller symbol (joined from refs.from_usr → symbols.usr).

    Tries exact match on short name first (``ModemMsgManager``), then falls back
    to exact match on qualified name (``zbox::ModemMsgManager``).
    """
    kind_filter = "AND r.ref_kind = ?" if ref_kind else ""
    params: list = [config_hash, config_hash, name, name]
    if ref_kind:
        params.append(ref_kind)
    params.append(limit)
    return conn.execute(
        f"""SELECT r.from_file, r.from_line, r.ref_kind,
                   r.from_usr,
                   caller.name           AS caller_name,
                   caller.qualified_name AS caller_qname,
                   caller.kind           AS caller_kind,
                   caller.file_path      AS caller_file
            FROM refs r
            JOIN symbols tgt
              ON tgt.config_hash = r.config_hash AND tgt.usr = r.to_usr
            LEFT JOIN symbols caller
              ON caller.config_hash = r.config_hash AND caller.usr = r.from_usr
            WHERE r.config_hash = ? AND tgt.config_hash = ?
              AND (tgt.name = ? OR tgt.qualified_name = ?) {kind_filter}
            ORDER BY r.from_file, r.from_line
            LIMIT ?""",
        params,
    ).fetchall()


def count_refs(conn: sqlite3.Connection, config_hash: str) -> int:
    """Return total reference count for a build config (0 when refs not indexed)."""
    return conn.execute(
        "SELECT COUNT(*) FROM refs WHERE config_hash=?", (config_hash,)
    ).fetchone()[0]


def get_active_config(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row | None:
    """Return the most recently indexed build_config for a project."""
    return conn.execute(
        """SELECT * FROM build_configs WHERE project_id=?
           ORDER BY created_at DESC LIMIT 1""",
        (project_id,),
    ).fetchone()


def _expand_query(query: str) -> str:
    """Add trailing wildcard to each bare word for broader prefix matching.

    Leaves existing wildcards (*) and FTS5 syntax (NEAR, ", parentheses,
    column filters with ``name_tokens : term*``) intact.  Single colons in
    column-filter syntax are detected via regex; C++ ``::`` passes through
    so its tokens get wildcard expansion.
    """
    # Tokens that already are FTS5 syntax — don't touch them.
    # Single colon (not part of ::) covers column-filter expressions like
    # "name_tokens : term*" which would be corrupted by wildcard appending.
    _bare_syntax = ('"', 'NEAR', 'AND', 'OR', '(', ')')
    _has_col_filter = re.search(r'(?<!:):(?!:)', query)
    if any(c in query for c in _bare_syntax) or _has_col_filter:
        return query

    parts = query.replace("::", " ").split()
    expanded = []
    for p in parts:
        if p.endswith('*'):
            expanded.append(p)
        else:
            expanded.append(f'{p}*')
    return ' '.join(expanded)


def search_symbols(
    conn: sqlite3.Connection,
    query: str,
    config_hash: str,
    limit: int = 20,
) -> list[sqlite3.Row]:
    """FTS5 search over symbols for a given build config.

    Bare words are expanded to trailing-wildcard prefix queries so that
    ``modem init`` matches ``modem_parser_oob_init``.
    Note: file_path is read from the denormalized symbols.file_path column,
    not re-joined from files — the JOIN was removed to avoid the redundant
    round-trip and the shadowing hazard.
    """
    expanded = _expand_query(query)
    return conn.execute(
        """SELECT s.*
           FROM symbols_fts
           JOIN symbols s ON s.id = symbols_fts.rowid
           WHERE symbols_fts MATCH ? AND s.config_hash = ?
           ORDER BY rank
           LIMIT ?""",
        (expanded, config_hash, limit),
    ).fetchall()


def get_file_mtime_indexed(conn: sqlite3.Connection, config_hash: str, path: str) -> float | None:
    """Return the stored mtime for a file, or None if not in the index."""
    row = conn.execute(
        "SELECT mtime FROM files WHERE config_hash=? AND path=?",
        (config_hash, path),
    ).fetchone()
    return row["mtime"] if row else None


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
