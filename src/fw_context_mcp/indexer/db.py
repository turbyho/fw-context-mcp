"""SQLite storage layer for fw-context-mcp index."""

from __future__ import annotations

import re
import sqlite3
import struct
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

def _parse_expected_columns(schema_sql: str, migration_statements: list[str]) -> dict[str, set[str]]:
    """Extract expected table→columns from CREATE TABLE and ALTER TABLE statements.

    Parses the ``_SCHEMA`` SQL for ``CREATE TABLE ... (col1, col2, ...)``
    and the migration list for ``ALTER TABLE t ADD COLUMN c``.  Returns a
    dict of ``{table_name: {column_name, ...}}``.

    Adding a migration automatically updates the schema fingerprint —
    no manual constant to bump.
    """
    tables: dict[str, set[str]] = {}

    # Parse CREATE TABLE statements from _SCHEMA
    for match in re.finditer(
        r"CREATE TABLE.*?(\w+)\s*\((.*?)\);",
        schema_sql, re.DOTALL | re.IGNORECASE,
    ):
        table = match.group(1)
        if table.startswith("idx_"):
            continue
        body = match.group(2)
        cols: set[str] = set()
        for line in body.splitlines():
            line = line.strip()
            if not line or line.startswith("--"):
                continue
            col_name = line.split()[0].strip()
            if not col_name:
                continue
            if col_name.upper() in (
                "PRIMARY", "UNIQUE", "FOREIGN", "CHECK", "CONSTRAINT",
            ):
                continue
            if col_name.upper().startswith("UNIQUE"):
                continue  # UNIQUE(col, ...) constraint, not a column
            cols.add(col_name)
        tables[table] = cols

    # Parse ALTER TABLE ADD COLUMN from migration statements
    for stmt in migration_statements:
        match = re.match(
            r"ALTER TABLE (\w+) ADD COLUMN (\w+)",
            stmt.strip(), re.IGNORECASE,
        )
        if match:
            table, col = match.group(1), match.group(2)
            tables.setdefault(table, set()).add(col)

    return tables


def _derive_schema_version(
    schema_sql: str,
    migration_statements: list[str],
) -> int:
    """Return a stable fingerprint of the full DB schema.

    Derived from the actual ``_SCHEMA`` and ``ALTER TABLE`` migrations
    in ``open_db()`` — zero-maintenance: adding a migration automatically
    changes the fingerprint.
    """
    import hashlib

    tables = _parse_expected_columns(schema_sql, migration_statements)
    canonical = "".join(
        f"{table}:{','.join(sorted(cols))};"
        for table, cols in sorted(tables.items())
    )
    return int.from_bytes(hashlib.sha256(canonical.encode()).digest()[:4], "big")


# Simple add-column migrations — run idempotently and feed into the schema
# fingerprint.  Add new ALTER TABLE statements here and CURRENT_SCHEMA_VERSION
# changes automatically.  (Complex migrations with backfill logic live inline in
# open_db() — their ALTER TABLE must also be listed here so the fingerprint
# stays accurate.)
_MIGRATION_ADD_COLUMNS = [
    "ALTER TABLE files ADD COLUMN mtime REAL NOT NULL DEFAULT 0",
    "ALTER TABLE symbols ADD COLUMN end_line INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE symbols ADD COLUMN file_path TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE symbols ADD COLUMN name_tokens TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE symbols ADD COLUMN enum_value INTEGER",
]

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

-- Cross-reference / call graph (on by default; disable with [index] index_refs = false).
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

-- Symbol embeddings for semantic search (opt-in via [index] index_embeddings = true).
-- Stored as BLOB: 1024 float32 values packed with struct.pack('f', ...).
-- ON DELETE CASCADE: when a symbol row is deleted, its embedding is removed.
CREATE TABLE IF NOT EXISTS embeddings (
    symbol_id    INTEGER PRIMARY KEY REFERENCES symbols(id) ON DELETE CASCADE,
    embedding    BLOB   NOT NULL,
    model        TEXT   NOT NULL,
    updated_at   TEXT   NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_embeddings_symbol ON embeddings(symbol_id);
"""

CURRENT_SCHEMA_VERSION = _derive_schema_version(_SCHEMA, _MIGRATION_ADD_COLUMNS)


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")  # 30s — wait on lock, don't fail

    # Load sqlite-vec extension for vector search (graceful when missing)
    try:
        conn.enable_load_extension(True)
        import sqlite_vec

        sqlite_vec.load(conn)
    except (ImportError, Exception):
        pass

    try:
        conn.executescript(_SCHEMA)

        # Simple add-column migrations — idempotent, run from _MIGRATION_ADD_COLUMNS
        # so the schema fingerprint (CURRENT_SCHEMA_VERSION) stays in sync automatically.
        for stmt in _MIGRATION_ADD_COLUMNS:
            try:
                conn.execute(stmt)
                conn.commit()
            except sqlite3.OperationalError as e:
                # Only skip "duplicate column" — re-raise disk-full etc.
                if "duplicate column" not in str(e):
                    raise

        # Migration: file_path backfill — column added by _MIGRATION_ADD_COLUMNS loop.
        # Backfill empties left over from old indexes or the DEFAULT ''.
        if conn.execute(
            "SELECT COUNT(*) FROM symbols WHERE file_path = ''"
        ).fetchone()[0] > 0:
            conn.execute("""
                UPDATE symbols SET file_path = COALESCE(
                    (SELECT f.path FROM files f WHERE f.id = symbols.file_id), ''
                ) WHERE file_path = ''
            """)
            conn.commit()

        # Migration: name_tokens backfill — column added by _MIGRATION_ADD_COLUMNS loop.
        # Backfill using Python split_tokens (SQLite can't call Python functions).
        # Use a generator to avoid materialising all rows in memory at once.
        if conn.execute(
            "SELECT COUNT(*) FROM symbols WHERE name_tokens = ''"
        ).fetchone()[0] > 0:
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

        # Migration: FTS5 rebuild —
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

    # Migrate vec0 table when sqlite-vec is available (idempotent CREATE IF NOT EXISTS)
    try:
        init_vec_table(conn)
    except Exception:
        pass

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


def get_db_schema_version(conn: sqlite3.Connection) -> int:
    """Return the schema version stored in the database (``PRAGMA user_version``).

    Compare with ``CURRENT_SCHEMA_VERSION`` — if lower, the index was built
    with an older schema and may miss data populated by newer migrations.
    """
    return conn.execute("PRAGMA user_version").fetchone()[0]


@contextmanager
def transaction(
    conn: sqlite3.Connection, checkpoint: bool = True
) -> Generator[sqlite3.Connection, None, None]:
    """Commit-or-rollback context manager.

    By default a WAL truncate-checkpoint runs after each successful commit to
    keep the -wal file small. Pass ``checkpoint=False`` inside tight per-item
    loops (e.g. the indexer's per-TU commits) and run a single checkpoint once
    the loop finishes — per-commit checkpoints there are O(n) and dominate
    indexing time.
    """
    try:
        yield conn
        conn.commit()
        if checkpoint:
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
               signature, docstring, enum_value)

    Returns count of rows inserted or upgraded to definition.
    """
    cur = conn.executemany(
        """INSERT INTO symbols
           (config_hash, file_id, file_path, name_tokens, usr, name, qualified_name, kind,
            line, col, end_line, is_definition, signature, docstring, enum_value)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(config_hash, usr) DO UPDATE SET
               file_id       = excluded.file_id,
               file_path     = excluded.file_path,
               name_tokens   = excluded.name_tokens,
               line          = excluded.line,
               col           = excluded.col,
               end_line      = excluded.end_line,
               is_definition = 1,
               signature     = excluded.signature,
               docstring     = excluded.docstring,
               enum_value    = excluded.enum_value
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


# ---------------------------------------------------------------------------
# Embedding helpers — pack/unpack float vectors as BLOBs for the embeddings
# table.  1024 float32 values = 4096 bytes per embedding with mxbai-embed-large.
# ---------------------------------------------------------------------------

_EMBEDDING_DIM = 1024


def _vec_to_blob(vec: list[float]) -> bytes:
    """Pack a float vector into a BLOB for storage."""
    return struct.pack(f"f" * len(vec), *vec)


def _blob_to_vec(blob: bytes) -> list[float]:
    """Unpack a BLOB back into a float vector."""
    return list(struct.unpack(f"f" * (len(blob) // 4), blob))


def upsert_embeddings(
    conn: sqlite3.Connection,
    rows: list[tuple[int, bytes, str]],
) -> int:
    """Insert or replace embedding rows.

    Each row is (symbol_id, embedding_blob, model).
    Returns number of rows inserted.
    """
    # Clean orphaned embeddings whose symbol no longer exists (can happen
    # after partial reindex / reset that didn't cascade properly).
    conn.execute(
        """DELETE FROM embeddings WHERE symbol_id NOT IN (
            SELECT id FROM symbols
        )"""
    )
    cur = conn.executemany(
        """INSERT OR REPLACE INTO embeddings(symbol_id, embedding, model, updated_at)
           VALUES (?, ?, ?, datetime('now'))""",
        rows,
    )
    return cur.rowcount


def get_embeddings(
    conn: sqlite3.Connection,
    config_hash: str,
    model: str,
) -> dict[int, list[float]]:
    """Return {symbol_id: embedding_vector} for a build config and model."""
    rows = conn.execute(
        """SELECT e.symbol_id, e.embedding
           FROM embeddings e
           JOIN symbols s ON s.id = e.symbol_id
           WHERE s.config_hash = ? AND e.model = ?""",
        (config_hash, model),
    ).fetchall()
    return {r["symbol_id"]: _blob_to_vec(r["embedding"]) for r in rows}


# ---------------------------------------------------------------------------
# Vector search via sqlite-vec (vec0 virtual table)
# ---------------------------------------------------------------------------

_VEC_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS vec_symbols USING vec0(
    embedding float[{dim}] distance_metric=cosine,
    config_hash TEXT,
    symbol_id INTEGER
);
"""


def init_vec_table(conn: sqlite3.Connection) -> None:
    """Create the vec0 virtual table if it does not exist.

    Must be called after ``sqlite_vec.load(conn)``.
    The table stores embeddings keyed by ``symbol_id`` with a per-build
    ``config_hash`` metadata column for filtered KNN queries.
    """
    conn.execute(_VEC_SCHEMA.format(dim=_EMBEDDING_DIM))
    conn.commit()


def upsert_embeddings_vec(
    conn: sqlite3.Connection,
    rows: list[tuple[int, str, list[float]]],
) -> int:
    """Insert or replace embeddings into the vec0 vector table.

    Each row is ``(symbol_id, config_hash, embedding_vector)``.
    Uses INSERT OR REPLACE so re-indexing the same build is idempotent.
    Returns number of rows inserted.
    """
    import json

    # vec0 requires INSERT per-row (no batch executemany via virtual table).
    # We build a single transaction around the whole batch.
    count = 0
    for symbol_id, config_hash, vec in rows:
        conn.execute(
            "INSERT OR REPLACE INTO vec_symbols(rowid, embedding, config_hash, symbol_id) "
            "VALUES (?, ?, ?, ?)",
            (symbol_id, json.dumps(vec), config_hash, symbol_id),
        )
        count += 1
    return count


def search_similar_vec(
    conn: sqlite3.Connection,
    query_vec: list[float],
    config_hash: str,
    threshold: float = 0.6,
    limit: int = 30,
) -> list[sqlite3.Row]:
    """KNN search over the vec0 table filtered by *config_hash*.

    Returns rows with ``symbol_id`` and ``distance`` (cosine distance,
    range [0, 2], where 0 = identical).  Results are post-filtered by
    *threshold* so only sufficiently similar vectors are returned.
    """
    import json

    query_json = json.dumps(query_vec)
    rows = conn.execute(
        """SELECT symbol_id, distance
           FROM vec_symbols
           WHERE embedding MATCH ?
             AND config_hash = ?
             AND k = ?
           ORDER BY distance""",
        (query_json, config_hash, limit),
    ).fetchall()

    # Post-filter by threshold (vec0 does not natively support distance < N
    # in the WHERE clause — distance is available only after MATCH)
    return [r for r in rows if r["distance"] <= (1.0 - threshold)]


def search_similar_hybrid(
    conn: sqlite3.Connection,
    query_vec: list[float],
    config_hash: str,
    fts5_query: str,
    threshold: float = 0.6,
    limit: int = 20,
) -> list[sqlite3.Row]:
    """Hybrid search: combine FTS5 text relevance with vector similarity.

    1. Fetch candidate symbols via FTS5 (broad recall – up to 200 candidates).
    2. Re-rank candidates by cosine distance against *query_vec*.
    3. Return top *limit* results sorted by distance.

    This avoids a full-scan of vec_symbols while still leveraging semantic
    similarity for ranking.
    """
    import json

    from fw_context_mcp.indexer.db import search_symbols

    # Phase 1 — text recall
    text_candidates = search_symbols(conn, fts5_query, config_hash, limit=200)
    if not text_candidates:
        return []

    candidate_ids = [r["id"] for r in text_candidates]
    placeholders = ",".join("?" * len(candidate_ids))

    # Phase 2 — vector re-rank
    query_json = json.dumps(query_vec)
    rows = conn.execute(
        f"""SELECT vs.symbol_id, distance
            FROM vec_symbols vs
            WHERE vs.symbol_id IN ({placeholders})
              AND vs.config_hash = ?""",
        (*candidate_ids, config_hash),
    ).fetchall()

    # Build distance map, filter by threshold
    dist_map: dict[int, float] = {}
    for r in rows:
        d = r["distance"]
        if d <= (1.0 - threshold):
            dist_map[r["symbol_id"]] = d

    # Re-rank candidates by vector distance
    scored = [
        (dist_map[r["id"]], r)
        for r in text_candidates
        if r["id"] in dist_map
    ]
    scored.sort(key=lambda x: x[0])

    top = scored[:limit]
    # Embed distance into result dict
    return [
        dict(r, _vector_distance=d)
        for d, r in top
    ]


# ---------------------------------------------------------------------------
# Graph analytics — call-graph traversal via recursive CTE
# ---------------------------------------------------------------------------


def _resolve_target_usr(
    conn: sqlite3.Connection, config_hash: str, name: str
) -> str | None:
    """Look up the USR of a symbol by name.

    When multiple USRs exist for the same name (e.g. C++ inline functions
    with ``#*1C.#`` ABI tags), pick the one with the most incoming
    references — that is the variant actually called throughout the codebase.

    Tiebreaker: prefer symbols that have outgoing refs (they call other
    functions — i.e. they have a body) over symbols with no outgoing refs
    (framework struct fields, declaration-only symbols, etc.).
    """
    esc_name = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    suffix_pattern = f"%::{esc_name}"
    rows = conn.execute(
        """SELECT s.usr,
                  COUNT(r_in.rowid) AS ref_count,
                  COUNT(r_out.rowid) AS out_count
           FROM symbols s
           LEFT JOIN refs r_in ON r_in.to_usr = s.usr AND r_in.config_hash = s.config_hash
           LEFT JOIN refs r_out ON r_out.from_usr = s.usr AND r_out.config_hash = s.config_hash
           WHERE s.config_hash = ?
             AND (s.name = ? OR s.qualified_name = ? OR s.qualified_name LIKE ? ESCAPE '\\')
           GROUP BY s.usr
           ORDER BY s.is_definition DESC, ref_count DESC, out_count DESC
           LIMIT 1""",
        (config_hash, name, name, suffix_pattern),
    ).fetchone()
    return rows["usr"] if rows else None


def _get_alias_pairs(
    conn: sqlite3.Connection, config_hash: str
) -> list[tuple[str, str]]:
    """Return [(decl_usr, def_usr)] for weak-alias declarations → definitions.

    Detects the ``__attribute__((weak, alias(\"__func\")))`` pattern by finding
    declaration-only symbols that have a ``__``-prefixed sibling definition
    with the same parameter signature.
    """
    # Declarations that appear as callees but have no outgoing refs themselves.
    # Use NOT EXISTS instead of NOT IN — the refs table has NULL from_usr
    # (2760 file-scope references with no enclosing function), and NOT IN
    # returns NULL (falsy) when the subquery contains NULLs.
    rows = conn.execute(
        """SELECT s.usr, s.name, s.signature
           FROM symbols s
           WHERE s.config_hash = ?
             AND s.is_definition = 0
             AND s.kind IN ('function', 'method', 'constructor', 'destructor')
             AND EXISTS (SELECT 1 FROM refs r WHERE r.to_usr = s.usr AND r.config_hash = ?)
             AND NOT EXISTS (SELECT 1 FROM refs r WHERE r.from_usr = s.usr AND r.config_hash = ?)
        """,
        (config_hash, config_hash, config_hash),
    ).fetchall()

    if not rows:
        return []

    # Build index of __-prefixed definitions by (name, param_count)
    def_rows = conn.execute(
        """SELECT usr, name, signature FROM symbols
           WHERE config_hash = ? AND is_definition = 1 AND name LIKE '__%'
        """,
        (config_hash,),
    ).fetchall()

    def_index: dict[tuple[str, int], str] = {}
    for r in def_rows:
        pc = (r["signature"] or "").count(",") + 1 if r["signature"] else 0
        def_index[(r["name"], pc)] = r["usr"]

    pairs: list[tuple[str, str]] = []
    for r in rows:
        pc = (r["signature"] or "").count(",") + 1 if r["signature"] else 0
        for candidate in (f"__{r['name']}", f"_{r['name']}"):
            def_usr = def_index.get((candidate, pc))
            if def_usr:
                pairs.append((r["usr"], def_usr))
                break
    return pairs


def _bridge_weak_aliases(
    conn: sqlite3.Connection,
    config_hash: str,
    all_edges: dict[str, list[tuple[str, str]]],
) -> None:
    """Add synthetic edges from weak-alias declarations to their definitions.

    Embedded firmware uses ``__attribute__((weak, alias(\"__func\")))`` for
    user-overridable hooks (e.g. ``digitalWrite`` → ``__digitalWrite``).
    Libclang sees these as two different USRs with no connection.  We inject
    synthetic edges so the BFS can traverse past the declaration.
    """
    pairs = _get_alias_pairs(conn, config_hash)
    for decl_usr, def_usr in pairs:
        # Get the definition name for display in chains
        def_name = conn.execute(
            "SELECT name FROM symbols WHERE config_hash=? AND usr=? LIMIT 1",
            (config_hash, def_usr),
        ).fetchone()
        label = def_name["name"] if def_name else "?"
        all_edges.setdefault(decl_usr, []).append((def_usr, label))


def find_call_path(
    conn: sqlite3.Connection,
    config_hash: str,
    from_name: str,
    to_name: str,
    max_depth: int = 10,
) -> list[dict]:
    """Find call paths from *from_name* to *to_name* via BFS in the refs table.

    Uses Python BFS with cycle detection — avoids the exponential explosion
    of a recursive CTE over 1M+ reference edges.  Returns up to 5 shortest
    paths, each with ``depth`` (number of edges) and ``chain``
    (human-readable ``A → B → C`` string).
    """
    from_usr = _resolve_target_usr(conn, config_hash, from_name)
    to_usr = _resolve_target_usr(conn, config_hash, to_name)
    if not from_usr or not to_usr:
        return []

    # Pre-load adjacency: for each USR, its outgoing edges → [(to_usr, callee_name)]
    # Use LEFT JOIN — edges whose target is a declaration-only symbol
    # (is_definition=0, common for weak aliases like digitalWrite) are included.
    # COALESCE prefers the definition name, falls back to any name.
    all_edges: dict[str, list[tuple[str, str]]] = {}
    for row in conn.execute(
        """SELECT r.from_usr, r.to_usr,
                  COALESCE(
                      (SELECT name FROM symbols s
                       WHERE s.usr = r.to_usr AND s.config_hash = r.config_hash
                         AND s.is_definition = 1 LIMIT 1),
                      (SELECT name FROM symbols s
                       WHERE s.usr = r.to_usr AND s.config_hash = r.config_hash LIMIT 1),
                      '?'
                  ) AS callee_name
           FROM refs r
           WHERE r.config_hash = ?
             AND r.ref_kind IN ('call', 'indirect')
           GROUP BY r.from_usr, r.to_usr""",
        (config_hash,),
    ):
        all_edges.setdefault(row["from_usr"], []).append(
            (row["to_usr"], row["callee_name"])
        )

    # Also load symbol names for USRs (needed for caller names in base step).
    # Prefer definition names, fall back to declaration names.
    usr_names: dict[str, str] = {}
    for row in conn.execute(
        """SELECT usr, name, is_definition
           FROM symbols
           WHERE config_hash = ?
           ORDER BY is_definition DESC""",
        (config_hash,),
    ):
        if row["usr"] not in usr_names:
            usr_names[row["usr"]] = row["name"]

    from_name_resolved = usr_names.get(from_usr, from_name)

    # --- Weak-alias bridging ---
    # Declaration symbols with no outgoing edges (e.g. digitalWrite in .h)
    # never lead anywhere.  Detect the ``__attribute__((alias("__name")))``
    # pattern by looking for a definition whose name is ``__<declname>`` with
    # the same parameter signature.  Add a synthetic edge so the BFS can
    # continue from the declaration into the framework internals.
    _bridge_weak_aliases(conn, config_hash, all_edges)

    # BFS queue: (current_usr, depth, chain)
    from collections import deque
    queue: deque = deque()
    visited: set[str] = {from_usr}  # cycle prevention — never revisit a USR

    for to_usr_edge, callee_name in all_edges.get(from_usr, []):
        if to_usr_edge not in visited:
            chain = f"{from_name_resolved} → {callee_name}"
            if to_usr_edge == to_usr:
                return [{"depth": 1, "chain": chain, "target_usr": to_usr}]
            queue.append((to_usr_edge, 1, chain))
            visited.add(to_usr_edge)

    # BFS main loop
    found: list[dict] = []
    while queue and len(found) < 5:
        current, depth, chain = queue.popleft()
        if current == to_usr:
            found.append({"depth": depth, "chain": chain, "target_usr": current})
            continue
        if depth >= max_depth:
            continue
        for next_usr, next_name in all_edges.get(current, []):
            if next_usr not in visited:
                visited.add(next_usr)
                new_chain = f"{chain} → {next_name}"
                if next_usr == to_usr:
                    found.append({"depth": depth + 1, "chain": new_chain, "target_usr": next_usr})
                    if len(found) >= 5:
                        break
                else:
                    queue.append((next_usr, depth + 1, new_chain))

    return found


def find_all_callers_recursive(
    conn: sqlite3.Connection,
    config_hash: str,
    name: str,
    max_depth: int = 5,
    limit: int = 50,
) -> list[dict]:
    """Find all transitive callers of *name* (who calls it, directly or indirectly).

    Returns deduplicated results with ``depth`` (shortest distance to target)
    and the callee's ``name``, ``qualified_name``, ``kind``, ``file_path``.
    """
    target_usr = _resolve_target_usr(conn, config_hash, name)
    if not target_usr:
        return []

    # Build extended refs: real refs + synthetic weak-alias edges.
    # When someone calls decl_usr (alias), they also effectively call def_usr.
    alias_pairs = _get_alias_pairs(conn, config_hash)
    alias_values = ", ".join(
        f"('{d}', '{f}')" for d, f in alias_pairs
    )
    alias_cte = (
        f"""alias_pairs(decl_usr, def_usr) AS (VALUES {alias_values}),"""
        if alias_values else ""
    )
    alias_join = (
        """UNION ALL
        SELECT r.from_usr, ap.def_usr
        FROM refs r
        JOIN alias_pairs ap ON r.to_usr = ap.decl_usr
        WHERE r.config_hash = ?"""
        if alias_values else ""
    )

    query = f"""WITH {alias_cte}
        extended_refs(from_usr, to_usr) AS (
            SELECT from_usr, to_usr FROM refs
            WHERE config_hash = ? AND ref_kind IN ('call', 'indirect')
            {alias_join}
        ),
        callers(usr, depth) AS (
            SELECT from_usr, 1
            FROM extended_refs
            WHERE to_usr = ?
            UNION
            SELECT er.from_usr, c.depth + 1
            FROM extended_refs er
            JOIN callers c ON er.to_usr = c.usr
            WHERE c.depth < ?
        ),
        dedup AS (
            SELECT usr, MIN(depth) AS depth
            FROM callers
            GROUP BY usr
        )
        SELECT COALESCE(s_def.name, s_any.name, '?') AS name,
               COALESCE(s_def.qualified_name, s_any.qualified_name) AS qualified_name,
               COALESCE(s_def.kind, s_any.kind) AS kind,
               COALESCE(s_def.file_path, s_any.file_path) AS file_path,
               COALESCE(s_def.signature, s_any.signature) AS signature,
               d.depth
        FROM dedup d
        LEFT JOIN symbols s_def ON s_def.usr = d.usr AND s_def.config_hash = ?
                                   AND s_def.is_definition = 1
        LEFT JOIN symbols s_any ON s_any.usr = d.usr AND s_any.config_hash = ?
        WHERE COALESCE(s_def.name, s_any.name) IS NOT NULL
        ORDER BY d.depth, COALESCE(s_def.name, s_any.name)
        LIMIT ?"""

    params = [config_hash]
    if alias_values:
        params.append(config_hash)
    params.extend([target_usr, max_depth, config_hash, config_hash, limit])

    rows = conn.execute(query, params).fetchall()

    return [dict(r) for r in rows]


def find_callees_recursive(
    conn: sqlite3.Connection,
    config_hash: str,
    name: str,
    max_depth: int = 5,
    limit: int = 50,
) -> list[dict]:
    """Find all transitive callees of *name* (what it calls, directly or indirectly).

    Inverse of ``find_all_callers_recursive`` — walks edges from caller to callee.
    """
    source_usr = _resolve_target_usr(conn, config_hash, name)
    if not source_usr:
        return []

    # Build extended refs: real refs + synthetic weak-alias edges.
    # When decl_usr is an alias for def_usr, anything def_usr calls should
    # also be reachable from decl_usr.
    alias_pairs = _get_alias_pairs(conn, config_hash)
    alias_values = ", ".join(
        f"('{d}', '{f}')" for d, f in alias_pairs
    )
    alias_cte = (
        f"""alias_pairs(decl_usr, def_usr) AS (VALUES {alias_values}),"""
        if alias_values else ""
    )
    alias_join = (
        """UNION ALL
        SELECT ap.decl_usr, r.to_usr
        FROM refs r
        JOIN alias_pairs ap ON r.from_usr = ap.def_usr
        WHERE r.config_hash = ?"""
        if alias_values else ""
    )

    query = f"""WITH {alias_cte}
        extended_refs(from_usr, to_usr) AS (
            SELECT from_usr, to_usr FROM refs
            WHERE config_hash = ? AND ref_kind IN ('call', 'indirect')
            {alias_join}
        ),
        callees(usr, depth) AS (
            SELECT to_usr, 1
            FROM extended_refs
            WHERE from_usr = ?
            UNION
            SELECT er.to_usr, c.depth + 1
            FROM extended_refs er
            JOIN callees c ON er.from_usr = c.usr
            WHERE c.depth < ?
        ),
        dedup AS (
            SELECT usr, MIN(depth) AS depth
            FROM callees
            GROUP BY usr
        )
        SELECT COALESCE(s_def.name, s_any.name, '?') AS name,
               COALESCE(s_def.qualified_name, s_any.qualified_name) AS qualified_name,
               COALESCE(s_def.kind, s_any.kind) AS kind,
               COALESCE(s_def.file_path, s_any.file_path) AS file_path,
               COALESCE(s_def.signature, s_any.signature) AS signature,
               d.depth
        FROM dedup d
        LEFT JOIN symbols s_def ON s_def.usr = d.usr AND s_def.config_hash = ?
                                   AND s_def.is_definition = 1
        LEFT JOIN symbols s_any ON s_any.usr = d.usr AND s_any.config_hash = ?
        WHERE COALESCE(s_def.name, s_any.name) IS NOT NULL
        ORDER BY d.depth, COALESCE(s_def.name, s_any.name)
        LIMIT ?"""

    params = [config_hash]
    if alias_values:
        params.append(config_hash)
    params.extend([source_usr, max_depth, config_hash, config_hash, limit])

    rows = conn.execute(query, params).fetchall()

    return [dict(r) for r in rows]


def find_dead_code(
    conn: sqlite3.Connection,
    config_hash: str,
    limit: int = 100,
    exclude_paths: list[str] | None = None,
) -> list[dict]:
    """Find functions/methods that are defined but never called.

    A "dead" symbol is one whose USR never appears in ``refs.to_usr``.

    *exclude_paths* is a list of LIKE patterns for file paths to exclude
    (e.g. ``["mbed-os/%", "cmsis/%"]``).  When omitted, no paths are excluded.
    """
    if exclude_paths:
        path_clauses = " AND ".join(
            "s.file_path NOT LIKE ?" for _ in exclude_paths
        )
        params = [config_hash, config_hash] + list(exclude_paths) + [limit]
    else:
        path_clauses = ""
        params = [config_hash, config_hash, limit]

    rows = conn.execute(
        f"""SELECT s.name, s.qualified_name, s.kind, s.file_path,
                  s.signature, s.line
           FROM symbols s
           WHERE s.config_hash = ?
             AND s.is_definition = 1
             AND s.kind IN ('function', 'method', 'constructor', 'destructor')
             AND s.usr NOT IN (
                 SELECT DISTINCT to_usr FROM refs WHERE config_hash = ?
             )
             {('AND ' + path_clauses) if path_clauses else ''}
           ORDER BY s.kind, s.name
           LIMIT ?""",
        params,
    ).fetchall()

    return [dict(r) for r in rows]


def find_hotspots(
    conn: sqlite3.Connection,
    config_hash: str,
    limit: int = 20,
) -> list[dict]:
    """Find the most-called functions (hotspots) ranked by caller count.

    Only counts actual call edges (``ref_kind IN ('call', 'indirect')``) —
    plain references and member-access expressions are excluded so enum
    constants and fields don't appear as "hot" call targets.
    """
    rows = conn.execute(
        """SELECT s.name, s.qualified_name, s.kind, s.file_path,
                  s.signature, s.line,
                  COUNT(r.rowid) AS caller_count
           FROM refs r
           JOIN symbols s ON s.usr = r.to_usr AND s.config_hash = r.config_hash
           WHERE r.config_hash = ?
             AND s.is_definition = 1
             AND r.ref_kind IN ('call', 'indirect')
           GROUP BY s.usr
           ORDER BY caller_count DESC
           LIMIT ?""",
        (config_hash, limit),
    ).fetchall()

    return [dict(r) for r in rows]


def _build_kind_filter(ref_kind: str | list[str] | None) -> tuple[str, list[str]]:
    """Build a SQL kind-filter clause and parameter list for ref_kind.

    Returns ``(sql_fragment, params)`` — the fragment is empty when
    *ref_kind* is None, a single ``= ?`` for a string, or an ``IN (...)``
    clause for a list.
    """
    if ref_kind is None:
        return "", []
    if isinstance(ref_kind, list):
        placeholders = ", ".join("?" * len(ref_kind))
        return f"AND r.ref_kind IN ({placeholders})", list(ref_kind)
    return "AND r.ref_kind = ?", [ref_kind]


def find_refs(
    conn: sqlite3.Connection,
    config_hash: str,
    name: str,
    ref_kind: str | list[str] | None = None,
    limit: int = 50,
) -> list[sqlite3.Row]:
    """Find references to a symbol by name.

    Resolves the target name → its USR(s) via symbols, then joins refs.to_usr.
    Each result carries the referencing location and, when known, the enclosing
    caller symbol (joined from refs.from_usr → symbols.usr).

    Name resolution uses a three-tier match so partially-qualified names work:
    1. Exact ``name`` match (e.g. ``send`` matches bare name ``send``)
    2. Exact ``qualified_name`` match (``zbox::ZMODEM_DRIVER::send``)
    3. Suffix LIKE on ``qualified_name`` (``ZMODEM_DRIVER::send``
       matches ``zbox::ZMODEM_DRIVER::send``)

    For classes, structs, and enums the index stores references at member-level
    granularity (e.g. ``ZUART::get``, not ``ZUART`` itself).  When the resolved
    symbol is an aggregate type, the query uses USR prefix matching
    (``to_usr LIKE usr || '@%'``) so all member references are included.

    *ref_kind* can be a single value, a list (→ ``IN`` clause), or None
    (no filter).
    """
    # ── Resolve name → USR and kind ──────────────────────────────────────
    # Three-tier match: exact name, exact qualified_name, suffix LIKE
    # Escape % and _ for the LIKE pattern
    esc_name = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    suffix_pattern = f"%::{esc_name}"
    symbol = conn.execute(
        """SELECT usr, kind FROM symbols
           WHERE config_hash = ?
             AND (name = ? OR qualified_name = ? OR qualified_name LIKE ? ESCAPE '\\')
           ORDER BY is_definition DESC, qualified_name = ? DESC
           LIMIT 1""",
        (config_hash, name, name, suffix_pattern, name),
    ).fetchone()

    if not symbol:
        return []

    is_aggregate = symbol["kind"] in ("class", "struct", "enum")
    kind_filter, kind_params = _build_kind_filter(ref_kind)

    _SELECT = """SELECT r.from_file, r.from_line, r.ref_kind,
                        r.from_usr,
                        caller.name           AS caller_name,
                        caller.qualified_name AS caller_qname,
                        caller.kind           AS caller_kind,
                        caller.file_path      AS caller_file
                 FROM refs r
                 LEFT JOIN symbols caller
                   ON caller.config_hash = r.config_hash AND caller.usr = r.from_usr
                 WHERE r.config_hash = ?"""

    if is_aggregate:
        usr_prefix = symbol["usr"] + "@%"
        params: list = [config_hash, usr_prefix] + kind_params + [limit]
        return conn.execute(
            f"""{_SELECT}
                  AND r.to_usr LIKE ? {kind_filter}
                GROUP BY caller.qualified_name, r.from_file, r.from_line
                ORDER BY r.from_file, r.from_line
                LIMIT ?""",
            params,
        ).fetchall()

    # ── Exact USR match for functions, methods, variables, etc. ──────────
    # Same three-tier name resolution: exact name, exact qualified_name, suffix LIKE
    params = [config_hash, config_hash, name, name, suffix_pattern] + kind_params + [limit]
    return conn.execute(
        f"""{_SELECT}
              AND r.to_usr IN (
                  SELECT usr FROM symbols
                  WHERE config_hash = ?
                    AND (name = ? OR qualified_name = ? OR qualified_name LIKE ? ESCAPE '\\')
              ) {kind_filter}
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
    return ' OR '.join(expanded)


def search_symbols(
    conn: sqlite3.Connection,
    query: str,
    config_hash: str,
    limit: int = 20,
    kind: str | None = None,
    exclude_variables: bool = False,
) -> list[sqlite3.Row]:
    """FTS5 search over symbols for a given build config.

    Bare words are expanded to trailing-wildcard prefix queries so that
    ``modem init`` matches ``modem_parser_oob_init``.
    When *kind* is given, the filter is applied in SQL (before LIMIT) so the
    caller reliably gets up to *limit* matching rows — filtering after the fact
    in Python would silently under-return.
    When *exclude_variables* is True, local/file-scope variables are excluded
    from results.  Set True in topic-search tools (``search_code``) to prevent
    low-signal entries from cluttering the top results; leave False in recall
    phases (hybrid / embedding search) where vector re-ranking handles relevance.
    Note: file_path is read from the denormalized symbols.file_path column,
    not re-joined from files — the JOIN was removed to avoid the redundant
    round-trip and the shadowing hazard.
    """
    expanded = _expand_query(query)
    if kind:
        kind_filter = "AND s.kind = ?"
    elif exclude_variables:
        kind_filter = "AND s.kind != 'variable'"
    else:
        kind_filter = ""
    params: list = [expanded, config_hash]
    if kind:
        params.append(kind)
    params.append(limit)
    return conn.execute(
        f"""SELECT s.*
           FROM symbols_fts
           JOIN symbols s ON s.id = symbols_fts.rowid
           WHERE symbols_fts MATCH ? AND s.config_hash = ? {kind_filter}
           ORDER BY rank
           LIMIT ?""",
        params,
    ).fetchall()


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
    matching the ``symbols.file_path`` column.  Exact match first, then suffix
    match so both ``src/main.cpp`` and ``main.cpp`` work.

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
               WHERE config_hash = ? AND file_path LIKE ?
               ORDER BY kind, line""",
            (config_hash, f"%{file_path}"),
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
                entry: dict = {
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
            group["subgroups"] = sorted(
                group.pop("_subgroups").values(), key=lambda g: g["name"]
            )

    return {
        "file": file_path,
        "total_symbols": len(rows),
        "symbols": groups,
    }


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
