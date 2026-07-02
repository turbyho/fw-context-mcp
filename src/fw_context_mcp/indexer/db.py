"""SQLite storage layer for fw-context-mcp index."""

from __future__ import annotations

__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "DatabaseCorruptionError",
    "drop_fts_triggers",
    "rebuild_fts",
    "count_fp_assignments",
    "count_indirect_call_sites",
    "count_llm_analysis",
    "count_refs",
    "delete_fp_assignments_for_file",
    "delete_inheritance_for_file",
    "delete_indirect_call_sites_for_file",
    "delete_refs_for_file",
    "delete_symbols_for_file",
    "find_all_callers_recursive",
    "find_callees_recursive",
    "find_call_path",
    "find_dead_code",
    "find_hotspots",
    "find_indirect_call_sites",
    "find_indirect_targets",
    "find_refs",
    "get_active_config",
    "get_all_projects",
    "get_class_members",
    "get_db_schema_version",
    "get_direct_bases",
    "get_direct_derived",
    "get_embeddings",
    "get_file_map",
    "get_file_mtime_indexed",
    "get_file_mtimes",
    "get_llm_analysis_for_symbol",
    "get_overrides_for_method",
    "get_template_instances",
    "init_vec_table",
    "insert_fp_assignments_batch",
    "insert_indirect_call_sites_batch",
    "insert_inheritance_batch",
    "insert_overrides_batch",
    "insert_refs_batch",
    "insert_symbols_batch",
    "open_db",
    "search_similar_hybrid",
    "search_similar_vec",
    "search_symbols",
    "split_tokens",
    "transaction",
    "write_lock",
    "WriteLockTimeout",
    "upsert_build_config",
    "upsert_embeddings",
    "upsert_embeddings_vec",
    "upsert_file",
    "upsert_llm_analysis_batch",
    "upsert_project",
]

import fcntl
import logging
import os
import re
import struct
import sys

# Replace stdlib sqlite3 with pysqlite3 when available.
# The stdlib sqlite3 on macOS (pyenv without --enable-loadable-sqlite-extensions)
# lacks enable_load_extension(), which is required by sqlite-vec.
# pysqlite3 provides a build of SQLite with extension support and pre-built
# wheels for macOS, Linux, and Windows.
try:
    import pysqlite3

    sys.modules["sqlite3"] = pysqlite3
except ImportError:
    pass
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger(__name__)


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
    # Noise words that pollute FTS5 — strip before tokenizing
    _NOISE_WORDS = frozenset(("at", "unnamed"))

    def _tokenize(s: str) -> list[str]:
        # Strip anonymous struct/enum/union markers — these inject noise tokens
        # like "mbed", "include", "enum" that match thousands of irrelevant symbols.
        s = re.sub(r"\(unnamed\s+(struct|enum|union)\s+at\s+[^)]+\)", "", s)
        s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s)      # camelCase split
        s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", s)   # HTTPResponse → HTTP Response
        parts = re.split(r"[^a-zA-Z0-9]+", s)               # split on non-alnum
        return [p.lower() for p in parts if len(p) > 1 and p.lower() not in _NOISE_WORDS]

    tokens: list[str] = []
    seen: set[str] = set()
    for src in (name, qualified_name):
        if src:
            for tok in _tokenize(src):
                if tok not in seen:
                    seen.add(tok)
                    tokens.append(tok)
    return " ".join(tokens)


def _backfill_is_project(conn: sqlite3.Connection, db_path: Path) -> None:
    """Backfill ``symbols.is_project`` for existing indexes.

    Detects project source directories from the project-root directory
    structure and marks symbols whose ``file_path`` falls underneath.

    This is a best-effort migration — any missed symbols are corrected
    on the next ``fw-context index`` run which sets is_project during
    normal indexing.
    """
    project_root = db_path.parent.parent
    _COMMON_SRC = ["src", "lib", "app", "drivers", "include", "modules"]
    patterns: list[str] = []
    params: list[str] = []
    for name in _COMMON_SRC:
        if (project_root / name).is_dir():
            patterns.append("file_path LIKE ?")
            params.append(f"{name}/%")
    if not patterns:
        return
    clause = " OR ".join(patterns)
    conn.execute(f"UPDATE symbols SET is_project = 1 WHERE {clause}", params)


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
        alt_match = re.match(
            r"ALTER TABLE (\w+) ADD COLUMN (\w+)",
            stmt.strip(), re.IGNORECASE,
        )
        if alt_match:
            table, col = alt_match.group(1), alt_match.group(2)
            tables.setdefault(table, set()).add(col)

    return tables


def _derive_schema_version(
    schema_sql: str,
    migration_statements: list[str],
) -> int:
    """Return a stable fingerprint of the full DB schema.

    Derived from the actual ``_SCHEMA`` and ``ALTER TABLE`` migrations
    in ``open_db()``.  Adding an ALTER TABLE to ``_MIGRATION_ADD_COLUMNS``
    automatically changes the fingerprint — no manual constant to bump.

    However, complex inline migrations in ``open_db()`` (backfill FTS5
    columns, name_tokens backfill, summary/inputs/outputs backfill) also
    have their ALTER TABLE statements listed in ``_MIGRATION_ADD_COLUMNS``
    to keep the fingerprint accurate.  Adding or removing such a migration
    requires manually updating ``_MIGRATION_ADD_COLUMNS``.
    """
    import hashlib

    tables = _parse_expected_columns(schema_sql, migration_statements)
    canonical = "".join(
        f"{table}:{','.join(sorted(cols))};"
        for table, cols in sorted(tables.items())
    )
    return int.from_bytes(hashlib.sha256(canonical.encode()).digest()[:4], "big") & 0x7FFFFFFF


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
    "ALTER TABLE symbols ADD COLUMN summary TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE symbols ADD COLUMN inputs TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE symbols ADD COLUMN outputs TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE symbols ADD COLUMN is_virtual INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE symbols ADD COLUMN is_pure_virtual INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE symbols ADD COLUMN parent_usr TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE symbols ADD COLUMN is_template INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE symbols ADD COLUMN template_usr TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE symbols ADD COLUMN is_project INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE symbols ADD COLUMN pagerank REAL NOT NULL DEFAULT 0.0",
    "ALTER TABLE llm_analysis ADD COLUMN content_hash TEXT NOT NULL DEFAULT ''",
]

_SCHEMA = """
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
    is_virtual     INTEGER NOT NULL DEFAULT 0,
    is_pure_virtual INTEGER NOT NULL DEFAULT 0,
    parent_usr     TEXT    NOT NULL DEFAULT '',
    is_template    INTEGER NOT NULL DEFAULT 0,
    template_usr   TEXT    NOT NULL DEFAULT '',
    pagerank       REAL    NOT NULL DEFAULT 0.0,
    UNIQUE(config_hash, usr)
);

CREATE INDEX IF NOT EXISTS idx_symbols_name        ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_qname       ON symbols(qualified_name);
CREATE INDEX IF NOT EXISTS idx_symbols_kind        ON symbols(kind);
CREATE INDEX IF NOT EXISTS idx_symbols_file        ON symbols(file_id);
CREATE INDEX IF NOT EXISTS idx_symbols_parent     ON symbols(config_hash, parent_usr);
CREATE INDEX IF NOT EXISTS idx_symbols_template  ON symbols(config_hash, template_usr);
CREATE INDEX IF NOT EXISTS idx_symbols_filepath  ON symbols(config_hash, file_path);
CREATE INDEX IF NOT EXISTS idx_files_config        ON files(config_hash);

CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts USING fts5(
    name,
    qualified_name,
    signature,
    docstring,
    file_path,
    name_tokens,
    summary,
    inputs,
    outputs,
    content='symbols',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS symbols_ai AFTER INSERT ON symbols BEGIN
    INSERT INTO symbols_fts(rowid, name, qualified_name, signature, docstring, file_path, name_tokens, summary, inputs, outputs)
    VALUES (new.id, new.name, new.qualified_name, new.signature, new.docstring, new.file_path, new.name_tokens, new.summary, new.inputs, new.outputs);
END;

CREATE TRIGGER IF NOT EXISTS symbols_ad AFTER DELETE ON symbols BEGIN
    INSERT INTO symbols_fts(symbols_fts, rowid, name, qualified_name, signature, docstring, file_path, name_tokens, summary, inputs, outputs)
    VALUES ('delete', old.id, old.name, old.qualified_name, old.signature, old.docstring, old.file_path, old.name_tokens, old.summary, old.inputs, old.outputs);
END;

CREATE TRIGGER IF NOT EXISTS symbols_au AFTER UPDATE ON symbols BEGIN
    INSERT INTO symbols_fts(symbols_fts, rowid, name, qualified_name, signature, docstring, file_path, name_tokens, summary, inputs, outputs)
    VALUES ('delete', old.id, old.name, old.qualified_name, old.signature, old.docstring, old.file_path, old.name_tokens, old.summary, old.inputs, old.outputs);
    INSERT INTO symbols_fts(rowid, name, qualified_name, signature, docstring, file_path, name_tokens, summary, inputs, outputs)
    VALUES (new.id, new.name, new.qualified_name, new.signature, new.docstring, new.file_path, new.name_tokens, new.summary, new.inputs, new.outputs);
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

-- Indirect function-pointer call sites (Phase 2).
-- Records locations where a function pointer field or variable is invoked --
-- NOTE: keep in sync with _CRITICAL_TABLES below (defensive re-creation block). --
-- unlike refs, there is no resolved target function (the callee is a
-- FIELD_DECL or VAR_DECL, not a FUNCTION_DECL).
CREATE TABLE IF NOT EXISTS indirect_call_sites (
    id           INTEGER PRIMARY KEY,
    config_hash  TEXT    NOT NULL,
    from_file    TEXT    NOT NULL,
    from_line    INTEGER NOT NULL,
    from_usr     TEXT,
    expr_text    TEXT    NOT NULL DEFAULT '',
    target_usr   TEXT    NOT NULL,
    target_name  TEXT    NOT NULL DEFAULT '',
    fn_ptr_type  TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_ics_config_target ON indirect_call_sites(config_hash, target_usr);
CREATE INDEX IF NOT EXISTS idx_ics_config_file   ON indirect_call_sites(config_hash, from_file);
CREATE INDEX IF NOT EXISTS idx_ics_config_usr    ON indirect_call_sites(config_hash, from_usr);

-- Function pointer assignments (Phase 3).
-- Records both sides of "field = &function" so Phase 3 can link
-- fp_assignments.lhs_usr = indirect_call_sites.target_usr to answer
-- NOTE: keep in sync with _CRITICAL_TABLES below.
-- "which functions can be called through this field?"
CREATE TABLE IF NOT EXISTS fp_assignments (
    id           INTEGER PRIMARY KEY,
    config_hash  TEXT    NOT NULL,
    from_file    TEXT    NOT NULL,
    from_line    INTEGER NOT NULL,
    lhs_usr      TEXT    NOT NULL,
    lhs_name     TEXT    NOT NULL DEFAULT '',
    rhs_usr      TEXT    NOT NULL,
    rhs_name     TEXT    NOT NULL DEFAULT '',
    fn_ptr_type  TEXT    NOT NULL DEFAULT '',
    method       TEXT    NOT NULL,
    from_usr     TEXT
);
CREATE INDEX IF NOT EXISTS idx_fpa_config_lhs ON fp_assignments(config_hash, lhs_usr);
CREATE INDEX IF NOT EXISTS idx_fpa_config_rhs ON fp_assignments(config_hash, rhs_usr);
CREATE INDEX IF NOT EXISTS idx_fpa_config_file ON fp_assignments(config_hash, from_file);

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

-- LLM analysis for structured symbol descriptions (opt-in via [llm] analyze_symbols = true).
-- Pre-computed by Ollama during indexing: summary, inputs, outputs for each symbol.
-- ON DELETE CASCADE: when a symbol row is deleted, its analysis is automatically removed.
CREATE TABLE IF NOT EXISTS llm_analysis (
    symbol_id    INTEGER PRIMARY KEY REFERENCES symbols(id) ON DELETE CASCADE,
    summary      TEXT    NOT NULL DEFAULT '',
    inputs       TEXT    NOT NULL DEFAULT '',
    outputs      TEXT    NOT NULL DEFAULT '',
    model        TEXT    NOT NULL,
    analyzed_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    content_hash TEXT    NOT NULL DEFAULT ''
);

-- Per-file LLM analysis (opt-in via [llm] analyze_files = true).
-- Pre-computed by Ollama during indexing: a 2-3 sentence summary of what
-- the file is responsible for, based on the symbols it contains.
-- ON DELETE CASCADE: when a file row is deleted, its analysis is removed.
CREATE TABLE IF NOT EXISTS file_analysis (
    file_id      INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    config_hash  TEXT    NOT NULL DEFAULT '',
    summary      TEXT    NOT NULL DEFAULT '',
    model        TEXT    NOT NULL,
    analyzed_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- C++ inheritance hierarchy edges.
-- derived_usr → base_usr: class Derived : public Base { ... }
-- access: "public", "protected", "private"
-- NOTE: keep in sync with _CRITICAL_TABLES below.
CREATE TABLE IF NOT EXISTS inheritance (
    id           INTEGER PRIMARY KEY,
    config_hash  TEXT    NOT NULL REFERENCES build_configs(config_hash),
    derived_usr  TEXT    NOT NULL,
    base_usr     TEXT    NOT NULL,
    access       TEXT    NOT NULL DEFAULT 'public',
    is_virtual   INTEGER NOT NULL DEFAULT 0,
    UNIQUE(config_hash, derived_usr, base_usr)
);
CREATE INDEX IF NOT EXISTS idx_inheritance_derived ON inheritance(config_hash, derived_usr);
CREATE INDEX IF NOT EXISTS idx_inheritance_base    ON inheritance(config_hash, base_usr);

-- Virtual method override tracking.
-- derived_usr → base_usr: DerivedClass::method overrides BaseClass::method.
-- Built as a post-processing step after inheritance chains are indexed.
-- NOTE: keep in sync with _CRITICAL_TABLES below.
CREATE TABLE IF NOT EXISTS overrides (
    id           INTEGER PRIMARY KEY,
    config_hash  TEXT    NOT NULL REFERENCES build_configs(config_hash),
    derived_usr  TEXT    NOT NULL,
    base_usr     TEXT    NOT NULL,
    UNIQUE(config_hash, derived_usr, base_usr)
);
CREATE INDEX IF NOT EXISTS idx_overrides_derived ON overrides(config_hash, derived_usr);
CREATE INDEX IF NOT EXISTS idx_overrides_base    ON overrides(config_hash, base_usr);

-- Pre-computed hotspot cache — caller counts for instant find_hotspots queries.
CREATE TABLE IF NOT EXISTS hotspot_cache (
    id           INTEGER PRIMARY KEY,
    config_hash  TEXT    NOT NULL REFERENCES build_configs(config_hash),
    symbol_id    INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
    caller_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE(config_hash, symbol_id)
);
CREATE INDEX IF NOT EXISTS idx_hotspot_cache_config ON hotspot_cache(config_hash);
"""

CURRENT_SCHEMA_VERSION = _derive_schema_version(_SCHEMA, _MIGRATION_ADD_COLUMNS)


class WriteLockTimeout(Exception):
    """Raised when the write lock cannot be acquired within the timeout."""


@contextmanager
def write_lock(db_dir: Path, timeout: float = 60.0) -> Generator[None, None, None]:
    """Acquire an exclusive write lock for the index directory.

    Serializes all write operations (symbol storage, LLM analysis, embeddings)
    across processes.  Uses ``fcntl.flock`` — the kernel releases the lock
    automatically on process exit, so a crash never leaves a stale lock.

    Blocks for up to *timeout* seconds; raises ``WriteLockTimeout`` when the
    lock cannot be acquired in time.  Callers should catch and propagate
    the error gracefully — never retry indefinitely.

    Args:
        db_dir: Directory containing the index database (lock file is
            ``<db_dir>/write.lock``).
        timeout: Maximum time to wait for the lock, in seconds (default 60).

    Raises:
        WriteLockTimeout: Lock could not be acquired within *timeout*.
    """
    import time as _time

    lock_file = db_dir / "write.lock"
    fd = os.open(lock_file, os.O_CREAT | os.O_RDWR)
    deadline = _time.monotonic() + timeout
    acquired = False
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if _time.monotonic() > deadline:
                    raise WriteLockTimeout(
                        f"Could not acquire write lock for {db_dir} within {timeout:.0f}s"
                    ) from None
                _time.sleep(0.5)
        yield
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def open_db(path: Path, *, skip_integrity_check: bool = False) -> sqlite3.Connection:
    """Open SQLite database at *path*, enabling WAL mode and loading extensions.

    Creates the parent directory if missing.  Configures WAL journal mode,
    foreign keys, and a 30 s busy timeout.  Loads the ``sqlite-vec`` extension
    (best-effort — silently skipped when unavailable).

    Runs the full schema and migrations in sequence:

    1. Executes ``_SCHEMA`` (CREATE TABLE IF NOT EXISTS, indexes, triggers).
    2. Applies add-column migrations from ``_MIGRATION_ADD_COLUMNS``
       (idempotent — skips duplicate column errors).
    3. Backfills ``symbols.file_path`` from ``files.path`` for rows with
       empty ``file_path`` (migration for old indexes).
    4. Backfills ``symbols.name_tokens`` via Python ``split_tokens()``
       (SQLite cannot call Python functions).
    5. Rebuilds ``symbols_fts`` FTS5 virtual table when it lacks the
       ``name_tokens`` column (older schema).
    6. Backfills ``symbols.summary/inputs/outputs`` from ``llm_analysis``
       table, then rebuilds FTS5 again when it lacks those columns.
    7. Initialises the ``vec_symbols`` vec0 table (best-effort).
    8. Runs ``PRAGMA integrity_check`` (unless *skip_integrity_check*
       is ``True``) — raises ``DatabaseCorruptionError`` on failure
       (detected before any tool reads the data).

    On ``OperationalError('locked')`` the connection is retried once after a
    short sleep.  Other ``OperationalError`` and ``DatabaseError`` are
    converted to ``DatabaseCorruptionError``.

    Args:
        path: Filesystem path to the SQLite database file.
        skip_integrity_check: When ``True``, skip the ``PRAGMA integrity_check``
            scan.  Use for auxiliary connections (worker threads, pooled
            connections) where the check was already performed on the primary
            connection.  On large databases (5+ GB) the scan takes 15–30 s
            and saturates disk I/O.

    Returns:
        Open ``sqlite3.Connection`` with ``row_factory = sqlite3.Row``.

    Raises:
        DatabaseCorruptionError: When ``PRAGMA integrity_check`` fails or the
            database is otherwise unreadable.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")  # 10s — wait on lock, don't fail
    conn.execute("PRAGMA foreign_keys = ON")

    # WAL mode — persistent on the DB file once set, but needs to be applied
    # on first-open.  Run outside executescript() to avoid an unnecessary
    # write transaction when the DB is already in WAL mode.
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.DatabaseError as e:
        conn.close()
        raise DatabaseCorruptionError(str(path), str(e)) from e

    # Performance pragmas — read-heavy workload, WAL mode safe
    conn.execute("PRAGMA cache_size = -64000")       # 64 MB page cache
    conn.execute("PRAGMA mmap_size = 268435456")     # 256 MB memory-mapped I/O
    conn.execute("PRAGMA synchronous = 1")           # NORMAL — safe with WAL

    # Load sqlite-vec extension for vector search (graceful when missing)
    try:
        conn.enable_load_extension(True)
        import sqlite_vec

        sqlite_vec.load(conn)
    except (ImportError, Exception):
        pass

    # Only run the (expensive) schema/migration block when the on-disk schema
    # is outdated.  executescript() implies a write transaction — skipping it
    # when the schema is current means read-only queries never acquire a
    # write lock, even while a background reindex is writing.
    try:
        current_schema_ver = conn.execute("PRAGMA user_version").fetchone()[0]
    except sqlite3.DatabaseError as e:
        conn.close()
        raise DatabaseCorruptionError(str(path), str(e)) from e

    if current_schema_ver < CURRENT_SCHEMA_VERSION:
        try:
            conn.executescript(_SCHEMA)

            # Simple add-column migrations — idempotent, run from
            # _MIGRATION_ADD_COLUMNS so the schema fingerprint
            # (CURRENT_SCHEMA_VERSION) stays in sync automatically.
            for stmt in _MIGRATION_ADD_COLUMNS:
                try:
                    conn.execute(stmt)
                    conn.commit()
                except sqlite3.OperationalError as e:
                    # Only skip "duplicate column" — re-raise disk-full etc.
                    if "duplicate column" not in str(e):
                        raise

            # Migration: is_project backfill — column added by
            # _MIGRATION_ADD_COLUMNS loop.  Backfill for existing indexes
            # where all rows were inserted with DEFAULT 0.
            if conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE is_project = 0"
            ).fetchone()[0] > 0:
                _backfill_is_project(conn, path)
                conn.commit()

            # Migration: file_path backfill — column added by
            # _MIGRATION_ADD_COLUMNS loop.  Backfill empties left over from
            # old indexes or the DEFAULT ''.
            if conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE file_path = ''"
            ).fetchone()[0] > 0:
                conn.execute("""
                    UPDATE symbols SET file_path = COALESCE(
                        (SELECT f.path FROM files f WHERE f.id = symbols.file_id), ''
                    ) WHERE file_path = ''
                """)
                conn.commit()

            # Migration: name_tokens backfill — column added by
            # _MIGRATION_ADD_COLUMNS loop.  Backfill using Python split_tokens
            # (SQLite can't call Python functions).  Use a generator to avoid
            # materialising all rows in memory at once.
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
                # Use IF NOT EXISTS for race-free idempotency —
                # another process may have created the table between our
                # DROP above and the CREATE below.
                conn.executescript("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts USING fts5(
                        name, qualified_name, signature, docstring, file_path, name_tokens,
                        content='symbols', content_rowid='id'
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
                """)
                try:
                    conn.execute("""
                        INSERT INTO symbols_fts(rowid, name, qualified_name, signature, docstring, file_path, name_tokens)
                        SELECT id, name, qualified_name, COALESCE(signature,''), COALESCE(docstring,''),
                               COALESCE(file_path,''), COALESCE(name_tokens,'')
                        FROM symbols
                    """)
                    conn.commit()
                except sqlite3.OperationalError:
                    pass  # another process already backfilled

            # Migration: summary/inputs/outputs — backfill from llm_analysis table.
            if conn.execute(
                "SELECT COUNT(*) FROM symbols WHERE summary = ''"
            ).fetchone()[0] > 0:
                conn.execute(
                    """UPDATE symbols SET
                           summary = COALESCE((SELECT a.summary FROM llm_analysis a WHERE a.symbol_id = symbols.id), ''),
                           inputs = COALESCE((SELECT a.inputs FROM llm_analysis a WHERE a.symbol_id = symbols.id), ''),
                           outputs = COALESCE((SELECT a.outputs FROM llm_analysis a WHERE a.symbol_id = symbols.id), '')
                       WHERE id IN (SELECT symbol_id FROM llm_analysis)"""
                )
                conn.commit()

            # Migration: FTS5 rebuild — add summary/inputs/outputs columns.
            fts_cols2 = [r[1] for r in conn.execute("PRAGMA table_info(symbols_fts)").fetchall()]
            if "summary" not in fts_cols2:
                conn.executescript("""
                    DROP TRIGGER IF EXISTS symbols_ai;
                    DROP TRIGGER IF EXISTS symbols_ad;
                    DROP TRIGGER IF EXISTS symbols_au;
                    DROP TABLE IF EXISTS symbols_fts;
                """)
                conn.executescript("""
                    CREATE VIRTUAL TABLE symbols_fts USING fts5(
                        name, qualified_name, signature, docstring, file_path, name_tokens,
                        summary, inputs, outputs,
                        content='symbols', content_rowid='id'
                    );
                    CREATE TRIGGER symbols_ai AFTER INSERT ON symbols BEGIN
                        INSERT INTO symbols_fts(rowid, name, qualified_name, signature, docstring, file_path, name_tokens, summary, inputs, outputs)
                        VALUES (new.id, new.name, new.qualified_name, new.signature, new.docstring, new.file_path, new.name_tokens, new.summary, new.inputs, new.outputs);
                    END;
                    CREATE TRIGGER symbols_ad AFTER DELETE ON symbols BEGIN
                        INSERT INTO symbols_fts(symbols_fts, rowid, name, qualified_name, signature, docstring, file_path, name_tokens, summary, inputs, outputs)
                        VALUES ('delete', old.id, old.name, old.qualified_name, old.signature, old.docstring, old.file_path, old.name_tokens, old.summary, old.inputs, old.outputs);
                    END;
                    CREATE TRIGGER symbols_au AFTER UPDATE ON symbols BEGIN
                        INSERT INTO symbols_fts(symbols_fts, rowid, name, qualified_name, signature, docstring, file_path, name_tokens, summary, inputs, outputs)
                        VALUES ('delete', old.id, old.name, old.qualified_name, old.signature, old.docstring, old.file_path, old.name_tokens, old.summary, old.inputs, old.outputs);
                        INSERT INTO symbols_fts(rowid, name, qualified_name, signature, docstring, file_path, name_tokens, summary, inputs, outputs)
                        VALUES (new.id, new.name, new.qualified_name, new.signature, new.docstring, new.file_path, new.name_tokens, new.summary, new.inputs, new.outputs);
                    END;
                """)
                conn.execute("""
                    INSERT INTO symbols_fts(rowid, name, qualified_name, signature, docstring, file_path, name_tokens, summary, inputs, outputs)
                    SELECT id, name, qualified_name, COALESCE(signature,''), COALESCE(docstring,''),
                           COALESCE(file_path,''), COALESCE(name_tokens,''),
                           COALESCE(summary,''), COALESCE(inputs,''), COALESCE(outputs,'')
                    FROM symbols
                """)
                conn.commit()

        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower():
                conn.close()
                import time as _time
                _time.sleep(1)
                conn = sqlite3.connect(str(path))
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout = 10000")
                conn.execute("PRAGMA foreign_keys = ON")
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("PRAGMA cache_size = -64000")
                conn.execute("PRAGMA mmap_size = 268435456")
                conn.execute("PRAGMA synchronous = 1")
                conn.executescript(_SCHEMA)
                for stmt in _MIGRATION_ADD_COLUMNS:
                    try:
                        conn.execute(stmt)
                        conn.commit()
                    except sqlite3.OperationalError as e2:
                        if "duplicate column" not in str(e2):
                            raise
            elif "no such column" in str(e):
                # Old database — _SCHEMA contains CREATE INDEX statements
                # that reference columns (e.g. parent_usr) which don't exist
                # yet.  Also possible: the DB predates newer tables entirely
                # (inheritance, overrides) — executescript
                # may have stopped before creating them.
                # Strategy: apply add-column migrations (skip tables that
                # don't exist yet), then retry _SCHEMA to create remaining
                # tables and indexes.
                for stmt in _MIGRATION_ADD_COLUMNS:
                    try:
                        conn.execute(stmt)
                        conn.commit()
                    except sqlite3.OperationalError as e2:
                        if "duplicate column" not in str(e2) and "no such table" not in str(e2):
                            raise
                try:
                    conn.executescript(_SCHEMA)
                except sqlite3.OperationalError as e3:
                    conn.close()
                    raise DatabaseCorruptionError(
                        str(path),
                        f"Schema migration failed after column recovery — "
                        f"the database may be in an inconsistent state: {e3}",
                    ) from e3
            else:
                conn.close()
                raise DatabaseCorruptionError(str(path), str(e)) from e
        except sqlite3.DatabaseError as e:
            conn.close()
            raise DatabaseCorruptionError(str(path), str(e)) from e

        # Stamp the database with the current schema version so we can detect
        # stale indexes — PRAGMA user_version is the standard SQLite mechanism.
        # PRAGMA does not support bound parameters, so we use an f-string.
        # CURRENT_SCHEMA_VERSION is an integer constant — no injection risk.
        conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")

    # ── Defensive table creation ──────────────────────────────────────────
    # Critical tables that were added after the initial _SCHEMA definition.
    # These run unconditionally (outside the version gate) so that a database
    # whose migration was interrupted — leaving user_version stamped but
    # tables missing — self-heals automatically.  CREATE TABLE IF NOT EXISTS
    # is idempotent and takes microseconds when the table already exists.
    #
    # IMPORTANT: every table definition below MUST be kept in sync with the
    # corresponding CREATE TABLE statement in _SCHEMA above.  When you change
    # one, change the other.  The two copies exist because _CRITICAL_TABLES
    # runs unconditionally (self-healing) while _SCHEMA runs only on fresh
    # databases and schema-version migrations.
    _CRITICAL_TABLES = """
    CREATE TABLE IF NOT EXISTS indirect_call_sites (
        id           INTEGER PRIMARY KEY,
        config_hash  TEXT    NOT NULL,
        from_file    TEXT    NOT NULL,
        from_line    INTEGER NOT NULL,
        from_usr     TEXT,
        expr_text    TEXT    NOT NULL DEFAULT '',
        target_usr   TEXT    NOT NULL,
        target_name  TEXT    NOT NULL DEFAULT '',
        fn_ptr_type  TEXT    NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS idx_ics_config_target ON indirect_call_sites(config_hash, target_usr);
    CREATE INDEX IF NOT EXISTS idx_ics_config_file   ON indirect_call_sites(config_hash, from_file);
    CREATE INDEX IF NOT EXISTS idx_ics_config_usr    ON indirect_call_sites(config_hash, from_usr);

    CREATE TABLE IF NOT EXISTS fp_assignments (
        id           INTEGER PRIMARY KEY,
        config_hash  TEXT    NOT NULL,
        from_file    TEXT    NOT NULL,
        from_line    INTEGER NOT NULL,
        lhs_usr      TEXT    NOT NULL,
        lhs_name     TEXT    NOT NULL DEFAULT '',
        rhs_usr      TEXT    NOT NULL,
        rhs_name     TEXT    NOT NULL DEFAULT '',
        fn_ptr_type  TEXT    NOT NULL DEFAULT '',
        method       TEXT    NOT NULL,
        from_usr     TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_fpa_config_lhs ON fp_assignments(config_hash, lhs_usr);
    CREATE INDEX IF NOT EXISTS idx_fpa_config_rhs ON fp_assignments(config_hash, rhs_usr);
    CREATE INDEX IF NOT EXISTS idx_fpa_config_file ON fp_assignments(config_hash, from_file);

    CREATE TABLE IF NOT EXISTS inheritance (
        id           INTEGER PRIMARY KEY,
        config_hash  TEXT    NOT NULL REFERENCES build_configs(config_hash),
        derived_usr  TEXT    NOT NULL,
        base_usr     TEXT    NOT NULL,
        access       TEXT    NOT NULL DEFAULT 'public',
        is_virtual   INTEGER NOT NULL DEFAULT 0,
        UNIQUE(config_hash, derived_usr, base_usr)
    );
    CREATE INDEX IF NOT EXISTS idx_inheritance_derived ON inheritance(config_hash, derived_usr);
    CREATE INDEX IF NOT EXISTS idx_inheritance_base    ON inheritance(config_hash, base_usr);

    CREATE TABLE IF NOT EXISTS overrides (
        id           INTEGER PRIMARY KEY,
        config_hash  TEXT    NOT NULL REFERENCES build_configs(config_hash),
        derived_usr  TEXT    NOT NULL,
        base_usr     TEXT    NOT NULL,
        UNIQUE(config_hash, derived_usr, base_usr)
    );
    CREATE INDEX IF NOT EXISTS idx_overrides_derived ON overrides(config_hash, derived_usr);
    CREATE INDEX IF NOT EXISTS idx_overrides_base    ON overrides(config_hash, base_usr);

    CREATE TABLE IF NOT EXISTS hotspot_cache (
        id           INTEGER PRIMARY KEY,
        config_hash  TEXT    NOT NULL REFERENCES build_configs(config_hash),
        symbol_id    INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
        caller_count INTEGER NOT NULL DEFAULT 0,
        UNIQUE(config_hash, symbol_id)
    );
    CREATE INDEX IF NOT EXISTS idx_hotspot_cache_config ON hotspot_cache(config_hash);
    """
    conn.executescript(_CRITICAL_TABLES)

    # Migrate vec0 table when sqlite-vec is available (idempotent CREATE IF NOT EXISTS)
    try:
        init_vec_table(conn)
    except Exception as e:
        log.warning(
            "sqlite-vec vector table initialization failed — "
            "semantic search will use legacy BLOB fallback: %s",
            e,
        )

    # Integrity check — detect corruption early, before any tool uses the DB.
    # Skip for auxiliary connections (worker threads, pooled connections)
    # where the check already ran on the primary connection.
    #
    # GOTCHA — on 5+ GB databases a full scan takes 15–30 s and saturates
    # disk I/O.  Never call this during MCP server startup (even in a
    # background thread — the I/O contention delays first queries).
    # server.py pre-marks _integrity_checked to skip it; the DB was already
    # verified during ``fw-context index``.  See server.py:324-328.
    if not skip_integrity_check:
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


def drop_fts_triggers(conn: sqlite3.Connection) -> None:
    """Drop the three FTS5 content-sync triggers (ai, ad, au).

    Call before bulk indexing to eliminate the per-row FTS index overhead.
    After all TUs are processed, call ``rebuild_fts()`` to recreate the FTS
    table and triggers in one pass.
    """
    conn.execute("DROP TRIGGER IF EXISTS symbols_ai")
    conn.execute("DROP TRIGGER IF EXISTS symbols_ad")
    conn.execute("DROP TRIGGER IF EXISTS symbols_au")


def rebuild_fts(conn: sqlite3.Connection) -> None:
    """Rebuild the FTS5 virtual table from the current ``symbols`` content.

    Drops and recreates ``symbols_fts``, populates it with a single
    ``INSERT ... SELECT`` from ``symbols``, then reinstates the three
    content-sync triggers (ai, ad, au).  Call after bulk indexing to
    restore FTS5 search capability in one pass instead of paying per-row
    trigger overhead for every symbol INSERT/DELETE/UPDATE.

    Idempotent — safe to call on an already-healthy FTS table.
    """
    conn.executescript("""
        DROP TRIGGER IF EXISTS symbols_ai;
        DROP TRIGGER IF EXISTS symbols_ad;
        DROP TRIGGER IF EXISTS symbols_au;
        DROP TABLE IF EXISTS symbols_fts;

        CREATE VIRTUAL TABLE symbols_fts USING fts5(
            name, qualified_name, signature, docstring, file_path, name_tokens,
            summary, inputs, outputs,
            content='symbols', content_rowid='id'
        );

        INSERT INTO symbols_fts(rowid, name, qualified_name, signature, docstring,
                                file_path, name_tokens, summary, inputs, outputs)
        SELECT id, name, qualified_name, COALESCE(signature,''), COALESCE(docstring,''),
               COALESCE(file_path,''), COALESCE(name_tokens,''),
               COALESCE(summary,''), COALESCE(inputs,''), COALESCE(outputs,'')
        FROM symbols;

        CREATE TRIGGER symbols_ai AFTER INSERT ON symbols BEGIN
            INSERT INTO symbols_fts(rowid, name, qualified_name, signature, docstring,
                                    file_path, name_tokens, summary, inputs, outputs)
            VALUES (new.id, new.name, new.qualified_name, new.signature, new.docstring,
                    new.file_path, new.name_tokens, new.summary, new.inputs, new.outputs);
        END;

        CREATE TRIGGER symbols_ad AFTER DELETE ON symbols BEGIN
            INSERT INTO symbols_fts(symbols_fts, rowid, name, qualified_name, signature,
                                    docstring, file_path, name_tokens, summary, inputs, outputs)
            VALUES ('delete', old.id, old.name, old.qualified_name, old.signature,
                    old.docstring, old.file_path, old.name_tokens, old.summary, old.inputs, old.outputs);
        END;

        CREATE TRIGGER symbols_au AFTER UPDATE ON symbols BEGIN
            INSERT INTO symbols_fts(symbols_fts, rowid, name, qualified_name, signature,
                                    docstring, file_path, name_tokens, summary, inputs, outputs)
            VALUES ('delete', old.id, old.name, old.qualified_name, old.signature,
                    old.docstring, old.file_path, old.name_tokens, old.summary, old.inputs, old.outputs);
            INSERT INTO symbols_fts(rowid, name, qualified_name, signature, docstring,
                                    file_path, name_tokens, summary, inputs, outputs)
            VALUES (new.id, new.name, new.qualified_name, new.signature, new.docstring,
                    new.file_path, new.name_tokens, new.summary, new.inputs, new.outputs);
        END;
    """)


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

    The checkpoint is best-effort: if it fails (e.g. another connection holds
    a read transaction), the commit is NOT rolled back — data is already safe.
    """
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if checkpoint:
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass  # best-effort — data was already committed


def upsert_project(conn: sqlite3.Connection, project_id: str, name: str, root_path: str) -> None:
    """Insert or replace a project record.

    Args:
        conn: Open database connection.
        project_id: Unique project identifier (derived from project root path).
        name: Human-readable project name.
        root_path: Absolute filesystem path to the project root.

    Returns:
        None.
    """
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
    """Insert or update a build configuration record.

    Uses ``ON CONFLICT`` so re-indexing the same config refreshes
    ``created_at`` and ``compile_commands_path``.

    Args:
        conn: Open database connection.
        config_hash: Content-addressable hash of the compile_commands.json.
        project_id: Foreign key to ``projects``.
        compile_commands_path: Absolute path to ``compile_commands.json``.

    Returns:
        None.
    """
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

    Returns:
        int: The ``files.id`` of the inserted or updated row.
    """
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
               signature, docstring, enum_value, is_virtual, is_pure_virtual,
               parent_usr, is_template, template_usr, is_project, pagerank)

    Returns count of rows inserted or upgraded to definition.
    """
    cur = conn.executemany(
        """INSERT INTO symbols
           (config_hash, file_id, file_path, name_tokens, usr, name, qualified_name, kind,
            line, col, end_line, is_definition, signature, docstring, enum_value,
            is_virtual, is_pure_virtual, parent_usr, is_template, template_usr, is_project, pagerank)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
               enum_value    = excluded.enum_value,
               is_virtual    = excluded.is_virtual,
               is_pure_virtual = excluded.is_pure_virtual,
               parent_usr    = excluded.parent_usr,
               is_template   = excluded.is_template,
               template_usr  = excluded.template_usr,
               is_project    = excluded.is_project,
               pagerank      = excluded.pagerank
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
# Indirect call sites (Phase 2) — function pointer invocation tracking
# ---------------------------------------------------------------------------


def insert_indirect_call_sites_batch(conn: sqlite3.Connection, rows: list[tuple]) -> int:
    """Insert indirect call site rows.

    Each row: (config_hash, from_file, from_line, from_usr, expr_text,
    target_usr, target_name, fn_ptr_type).
    Returns number of rows inserted.
    """
    cur = conn.executemany(
        """INSERT INTO indirect_call_sites
           (config_hash, from_file, from_line, from_usr, expr_text, target_usr, target_name, fn_ptr_type)
           VALUES (?,?,?,?,?,?,?,?)""",
        rows,
    )
    return cur.rowcount


def delete_indirect_call_sites_for_file(conn: sqlite3.Connection, config_hash: str, from_file: str) -> None:
    """Delete all indirect call sites originating in a given file (for incremental reindex)."""
    conn.execute(
        "DELETE FROM indirect_call_sites WHERE config_hash=? AND from_file=?",
        (config_hash, from_file),
    )


def find_indirect_call_sites(
    conn: sqlite3.Connection,
    config_hash: str,
    name: str,
    limit: int = 50,
) -> list[sqlite3.Row]:
    """Find indirect call sites for a function pointer field or variable by name.

    Three-tier name resolution (same pattern as ``find_refs``): exact name,
    exact qualified_name, suffix LIKE.
    """
    esc_name = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    suffix_pattern = f"%::{esc_name}"
    return conn.execute(
        """SELECT ics.*,
                  caller.name            AS caller_name,
                  caller.qualified_name  AS caller_qname,
                  caller.kind            AS caller_kind
           FROM indirect_call_sites ics
           LEFT JOIN symbols caller
             ON caller.config_hash = ics.config_hash AND caller.usr = ics.from_usr
           WHERE ics.config_hash = ?
             AND (
                 -- Match via indexed symbols (covers FIELD_DECL, VAR_DECL)
                 ics.target_usr IN (
                     SELECT usr FROM symbols
                     WHERE config_hash = ?
                       AND (name = ? OR qualified_name = ? OR qualified_name LIKE ? ESCAPE '\\')
                 )
                 OR
                 -- Fallback: direct name match (covers PARM_DECL which are
                 -- not in the symbols table)
                 ics.target_name = ?
             )
           ORDER BY ics.from_file, ics.from_line
           LIMIT ?""",
        (config_hash, config_hash, name, name, suffix_pattern, name, limit),
    ).fetchall()


def count_indirect_call_sites(conn: sqlite3.Connection, config_hash: str) -> int:
    """Return total indirect call site count (0 when not indexed)."""
    return conn.execute(
        "SELECT COUNT(*) FROM indirect_call_sites WHERE config_hash=?",
        (config_hash,),
    ).fetchone()[0]


# ---------------------------------------------------------------------------
# Function pointer assignment helpers — Phase 3 linking
# ---------------------------------------------------------------------------


def insert_fp_assignments_batch(conn: sqlite3.Connection, rows: list[tuple]) -> int:
    """Insert function pointer assignment records.

    Each row: (config_hash, from_file, from_line, lhs_usr, lhs_name,
    rhs_usr, rhs_name, fn_ptr_type, method, from_usr).
    Uses OR IGNORE to handle re-indexing of the same file.
    """
    cur = conn.executemany(
        """INSERT OR IGNORE INTO fp_assignments
           (config_hash, from_file, from_line, lhs_usr, lhs_name,
            rhs_usr, rhs_name, fn_ptr_type, method, from_usr)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    return cur.rowcount


def delete_fp_assignments_for_file(
    conn: sqlite3.Connection, config_hash: str, from_file: str,
) -> None:
    """Delete fp_assignments rows for *from_file* under *config_hash*.

    Called before re-indexing a TU to remove stale entries."""
    conn.execute(
        "DELETE FROM fp_assignments WHERE config_hash = ? AND from_file = ?",
        (config_hash, from_file),
    )


def find_indirect_targets(
    conn: sqlite3.Connection,
    config_hash: str,
    name: str,
    limit: int = 50,
) -> list[sqlite3.Row]:
    """Find functions assigned to a function pointer field/variable/parameter.

    Joins ``fp_assignments`` with ``indirect_call_sites`` on
    ``lhs_usr = target_usr`` to link assignment sites to call sites.
    Three-tier name resolution: exact name, exact qualified, suffix LIKE.
    """
    esc_name = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    suffix_pattern = f"%::{esc_name}"
    return conn.execute(
        """SELECT fpa.rhs_usr, fpa.rhs_name, fpa.fn_ptr_type, fpa.method,
                  fpa.from_file AS assign_file, fpa.from_line AS assign_line,
                  fpa.from_usr AS assign_from_usr,
                  ics.from_file AS call_file, ics.from_line AS call_line,
                  ics.expr_text AS call_expr_text,
                  rhs_sym.qualified_name AS rhs_qname,
                  caller_sym.name AS assign_caller
           FROM fp_assignments fpa
           LEFT JOIN indirect_call_sites ics
             ON ics.target_usr = fpa.lhs_usr
            AND ics.config_hash = fpa.config_hash
           LEFT JOIN symbols rhs_sym
             ON rhs_sym.usr = fpa.rhs_usr
            AND rhs_sym.config_hash = fpa.config_hash
           LEFT JOIN symbols caller_sym
             ON caller_sym.usr = fpa.from_usr
            AND caller_sym.config_hash = fpa.config_hash
           WHERE fpa.config_hash = ?
             AND (
                 -- Match via indexed symbols (covers FIELD_DECL, VAR_DECL)
                 fpa.lhs_usr IN (
                     SELECT usr FROM symbols
                     WHERE config_hash = ?
                       AND (name = ? OR qualified_name = ? OR qualified_name LIKE ? ESCAPE ?)
                 )
                 OR
                 -- Fallback: direct name match (covers PARM_DECL which are
                 -- not in the symbols table)
                 fpa.lhs_name = ?
             )
           ORDER BY fpa.from_file, fpa.from_line
           LIMIT ?""",
        (config_hash, config_hash, name, name, suffix_pattern, "\\", name, limit),
    ).fetchall()


def count_fp_assignments(conn: sqlite3.Connection, config_hash: str) -> int:
    """Return total fp_assignment count (0 when not indexed)."""
    return conn.execute(
        "SELECT COUNT(*) FROM fp_assignments WHERE config_hash=?",
        (config_hash,),
    ).fetchone()[0]


# ---------------------------------------------------------------------------
# Inheritance helpers — C++ class hierarchy tracking
# ---------------------------------------------------------------------------


def insert_inheritance_batch(conn: sqlite3.Connection, rows: list[tuple]) -> int:
    """Insert inheritance edges.

    Each row: (config_hash, derived_usr, base_usr, access, is_virtual).
    Uses ON CONFLICT DO UPDATE so re-indexing the same derived class
    updates the access/virtual flags without duplication.

    Returns number of rows inserted or updated.
    """
    cur = conn.executemany(
        """INSERT INTO inheritance (config_hash, derived_usr, base_usr, access, is_virtual)
           VALUES (?,?,?,?,?)
           ON CONFLICT(config_hash, derived_usr, base_usr) DO UPDATE SET
               access = excluded.access,
               is_virtual = excluded.is_virtual""",
        rows,
    )
    return cur.rowcount


def delete_inheritance_for_file(conn: sqlite3.Connection, config_hash: str, file_id: int) -> None:
    """Delete inheritance records for classes defined in the given file.

    Called before re-indexing a TU to remove stale edges for classes
    whose definitions are in the file being re-parsed.
    """
    conn.execute(
        """DELETE FROM inheritance WHERE config_hash = ? AND derived_usr IN (
            SELECT usr FROM symbols WHERE config_hash = ? AND file_id = ?
        )""",
        (config_hash, config_hash, file_id),
    )


def get_direct_bases(conn: sqlite3.Connection, config_hash: str, usr: str) -> list[dict]:
    """Return direct base classes of *usr* (the class this one inherits from).

    Each result: {base_usr, derived_usr, access, is_virtual,
                   base_name, base_kind, base_file}
    """
    rows = conn.execute(
        """SELECT i.derived_usr, i.base_usr, i.access, i.is_virtual,
                  s.name AS base_name, s.kind AS base_kind,
                  s.file_path AS base_file
           FROM inheritance i
           LEFT JOIN symbols s ON s.usr = i.base_usr AND s.config_hash = i.config_hash
           WHERE i.config_hash = ? AND i.derived_usr = ?
           ORDER BY s.name""",
        (config_hash, usr),
    ).fetchall()
    return [dict(r) for r in rows]


def get_direct_derived(conn: sqlite3.Connection, config_hash: str, usr: str) -> list[dict]:
    """Return direct derived classes of *usr* (classes that inherit from this one).

    Each result: {base_usr, derived_usr, access, is_virtual,
                   derived_name, derived_kind, derived_file}
    """
    rows = conn.execute(
        """SELECT i.derived_usr, i.base_usr, i.access, i.is_virtual,
                  s.name AS derived_name, s.kind AS derived_kind,
                  s.file_path AS derived_file
           FROM inheritance i
           LEFT JOIN symbols s ON s.usr = i.derived_usr AND s.config_hash = i.config_hash
           WHERE i.config_hash = ? AND i.base_usr = ?
           ORDER BY s.name""",
        (config_hash, usr),
    ).fetchall()
    return [dict(r) for r in rows]


def insert_overrides_batch(
    conn: sqlite3.Connection,
    rows: list[tuple[str, str, str]],
) -> int:
    """Insert or replace override relationships.

    Each row: (config_hash, derived_usr, base_usr).
    Deletes existing overrides for the same derived_usr before inserting,
    so re-analysis is idempotent. Returns number of rows inserted.
    """
    conn.executemany(
        """DELETE FROM overrides WHERE config_hash = ? AND derived_usr = ?""",
        [(r[0], r[1]) for r in rows],
    )
    cur = conn.executemany(
        """INSERT INTO overrides (config_hash, derived_usr, base_usr)
           VALUES (?, ?, ?)""",
        rows,
    )
    return cur.rowcount


def get_overrides_for_method(
    conn: sqlite3.Connection,
    config_hash: str,
    usr: str,
) -> dict[str, list[dict]]:
    """Return what *usr* overrides and what overrides *usr*.

    Returns:
        dict with ``overrides`` (base methods this method overrides) and
        ``overridden_by`` (derived methods that override this one).
    """
    overrides_rows = conn.execute(
        """SELECT o.base_usr, s.name, s.qualified_name, s.kind, s.file_path, s.line
           FROM overrides o
           JOIN symbols s ON s.usr = o.base_usr AND s.config_hash = ?
           WHERE o.config_hash = ? AND o.derived_usr = ?
           ORDER BY s.qualified_name""",
        (config_hash, config_hash, usr),
    ).fetchall()

    overridden_by_rows = conn.execute(
        """SELECT o.derived_usr, s.name, s.qualified_name, s.kind, s.file_path, s.line
           FROM overrides o
           JOIN symbols s ON s.usr = o.derived_usr AND s.config_hash = ?
           WHERE o.config_hash = ? AND o.base_usr = ?
           ORDER BY s.qualified_name""",
        (config_hash, config_hash, usr),
    ).fetchall()

    return {
        "overrides": [dict(r) for r in overrides_rows],
        "overridden_by": [dict(r) for r in overridden_by_rows],
    }


def get_class_members(
    conn: sqlite3.Connection,
    config_hash: str,
    parent_usr: str,
) -> list[sqlite3.Row]:
    """Return all symbols (methods, fields, nested types) belonging to a class/struct.

    Args:
        conn: Open database connection.
        config_hash: Build config hash.
        parent_usr: USR of the parent class/struct.

    Returns:
        List of sqlite3.Row objects with symbol fields, ordered by kind then name.
        Empty list if no members found (e.g. index predates parent_usr support).
    """
    return conn.execute(
        """SELECT name, qualified_name, kind, file_path, line, col AS column,
                  signature, is_definition, is_virtual, is_pure_virtual
           FROM symbols
           WHERE config_hash = ? AND parent_usr = ?
           ORDER BY kind, name""",
        (config_hash, parent_usr),
    ).fetchall()


def get_template_instances(
    conn: sqlite3.Connection,
    config_hash: str,
    template_usr: str,
    limit: int = 50,
) -> list[sqlite3.Row]:
    """Return all instantiated symbols for a given template USR.

    Each row is a full symbol row for an instantiation whose ``template_usr``
    matches the given template.
    """
    return conn.execute(
        """SELECT name, qualified_name, kind, file_path, line, col AS column,
                  signature, is_definition, parent_usr
           FROM symbols
           WHERE config_hash = ? AND template_usr = ?
           ORDER BY file_path, line
           LIMIT ?""",
        (config_hash, template_usr, limit),
    ).fetchall()


# ---------------------------------------------------------------------------
# Embedding helpers — pack/unpack float vectors as BLOBs for the embeddings
# table.  1024 float32 values = 4096 bytes per embedding with mxbai-embed-large.
# ---------------------------------------------------------------------------

_EMBEDDING_DIM = 1024


def _vec_to_blob(vec: list[float]) -> bytes:
    """Pack a float vector into a BLOB for storage.

    Uses ``struct.pack("f" * N, *vec)`` where each float32 is 4 bytes.
    The embedding dimension is ``_EMBEDDING_DIM`` (1024 for mxbai-embed-large).

    Args:
        vec: Float vector (list of float32 values).

    Returns:
        bytes: BLOB of packed float32 values.
    """
    return struct.pack("f" * len(vec), *vec)


def _blob_to_vec(blob: bytes) -> list[float]:
    """Unpack a BLOB back into a float vector.

    Inverse of ``_vec_to_blob``.  Each 4-byte chunk is unpacked as a
    float32 via ``struct.unpack("f" * (len(blob) // 4), blob)``.

    Args:
        blob: BLOB of packed float32 values (must be a multiple of 4 bytes).

    Returns:
        list[float]: The reconstructed float vector.
    """
    return list(struct.unpack("f" * (len(blob) // 4), blob))


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
    Uses DELETE + INSERT because ``vec0`` virtual tables do not reliably
    support ``INSERT OR REPLACE`` conflict resolution.
    Returns number of rows inserted.
    """
    import json

    count = 0
    for symbol_id, config_hash, vec in rows:
        conn.execute("DELETE FROM vec_symbols WHERE rowid = ?", (symbol_id,))
        conn.execute(
            "INSERT INTO vec_symbols(rowid, embedding, config_hash, symbol_id) "
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
) -> list[dict]:
    """Hybrid search: combine FTS5 text relevance with vector similarity.

    1. Fetch candidate symbols via FTS5 (broad recall – up to 200 candidates).
    2. Re-rank candidates by cosine distance against *query_vec*.
    3. Return top *limit* results sorted by distance.

    This avoids a full-scan of vec_symbols while still leveraging semantic
    similarity for ranking.
    """

    from fw_context_mcp.indexer.db import search_symbols

    # Phase 1 — text recall (includes declarations + definitions).
    # The direct KNN path filters is_definition=1 but the hybrid path
    # intentionally doesn't — RRF fusion prefers definitions anyway,
    # and including declarations in the recall set catches cases
    # where a definition was renamed but the old declaration persists.
    text_candidates = search_symbols(conn, fts5_query, config_hash, limit=200)
    if not text_candidates:
        return []

    candidate_ids = [r["id"] for r in text_candidates]
    placeholders = ",".join("?" * len(candidate_ids))

    # Phase 2 — vector re-rank
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


def _escape_usr_values(alias_pairs: list[tuple[str, str]]) -> str:
    """Return SQL VALUES string with single-quote-escaped USRs.

    USRs from libclang (e.g. ``c:@F@main#I#``) do not contain single quotes,
    but we escape defensively to prevent SQL syntax errors or injection.
    """
    _q = "'"  # single quote
    return ", ".join(
        f"({_q}{d.replace(_q, _q * 2)}{_q}, {_q}{f.replace(_q, _q * 2)}{_q})"
        for d, f in alias_pairs
    )


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
    alias_values = _escape_usr_values(alias_pairs)
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

    params: list[object] = [config_hash]
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
    alias_values = _escape_usr_values(alias_pairs)
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

    params: list[object] = [config_hash]
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

    Returns two categories of results, each with a ``status`` field:

    * ``"dead"`` — the symbol's USR has no entry at all in ``refs.to_usr``
      (neither direct calls nor indirect function pointer assignments).
    * ``"possibly_dead"`` — the symbol has at least one indirect reference
      (``ref_kind = 'indirect'``, a function pointer assignment) but the
      assignment is not linked to any call site via ``fp_assignments``.
      This means the function IS assigned to a function pointer somewhere,
      but the invocation site is unknown — it may be called through
      unindexed code or a type-erased API.  LLM should treat this as
      uncertain, not as confirmed dead code.

    Each result dict includes: name, qualified_name, kind, file_path,
    signature, line, status, reason.

    *exclude_paths* is a list of LIKE patterns for file paths to exclude
    (e.g. ``["mbed-os/%", "cmsis/%"]``).  When omitted, no paths are excluded.
    """
    if exclude_paths:
        path_clause = "AND " + " AND ".join(
            "s.file_path NOT LIKE ?" for _ in exclude_paths
        )
        exclude_params = list(exclude_paths)
    else:
        path_clause = ""
        exclude_params = []

    # Category 1: truly dead — no refs at all
    dead_params: list[object] = [config_hash, config_hash] + exclude_params + [limit]
    dead_rows = conn.execute(
        f"""SELECT s.name, s.qualified_name, s.kind, s.file_path,
                  s.signature, s.line, s.usr
           FROM symbols s
           WHERE s.config_hash = ?
             AND s.is_definition = 1
             AND s.kind IN ('function', 'method', 'constructor', 'destructor')
             AND s.usr NOT IN (
                 SELECT DISTINCT to_usr FROM refs WHERE config_hash = ?
             )
             {path_clause}
           ORDER BY s.kind, s.name
           LIMIT ?""",
        dead_params,
    ).fetchall()

    results: list[dict] = []
    dead_usr_set: set[str] = set()
    for r in dead_rows:
        dead_usr_set.add(r["usr"])
        results.append({
            "name": r["name"],
            "qualified_name": r["qualified_name"],
            "kind": r["kind"],
            "file_path": r["file_path"],
            "signature": r["signature"],
            "line": r["line"],
            "status": "dead",
            "reason": "no references found — likely unused",
        })

    # Category 2: possibly dead — indirect refs exist but no resolved call site.
    # A symbol is possibly dead when it has ref_kind='indirect' entries
    # (function pointer assignments) but is NOT found as a resolved target
    # via fp_assignments → indirect_call_sites linking.
    remaining_slots = limit - len(results)
    if remaining_slots > 0:
        possibly_params: list[object] = [
            config_hash, config_hash, config_hash, config_hash,
        ] + exclude_params + [remaining_slots]
        possibly_rows = conn.execute(
            f"""SELECT s.name, s.qualified_name, s.kind, s.file_path,
                      s.signature, s.line, s.usr,
                      (SELECT GROUP_CONCAT(site, ', ')
                       FROM (SELECT DISTINCT r2.from_file || ':' || r2.from_line AS site
                             FROM refs r2
                             WHERE r2.to_usr = s.usr AND r2.config_hash = s.config_hash
                               AND r2.ref_kind = 'indirect'
                             LIMIT 3)) AS indirect_sites
               FROM symbols s
               WHERE s.config_hash = ?
                 AND s.is_definition = 1
                 AND s.kind IN ('function', 'method', 'constructor', 'destructor')
                 -- has indirect refs
                 AND s.usr IN (
                     SELECT DISTINCT to_usr FROM refs
                     WHERE config_hash = ? AND ref_kind = 'indirect'
                 )
                 -- but NOT in fp_assignments that link to a call site
                 AND s.usr NOT IN (
                     SELECT fpa.rhs_usr FROM fp_assignments fpa
                     JOIN indirect_call_sites ics
                       ON ics.target_usr = fpa.lhs_usr
                      AND ics.config_hash = fpa.config_hash
                     WHERE fpa.config_hash = ?
                 )
                 -- exclude truly dead (already covered)
                 AND s.usr NOT IN (
                     SELECT to_usr FROM refs WHERE config_hash = ? AND ref_kind = 'call'
                 )
                 {path_clause}
               ORDER BY s.kind, s.name
               LIMIT ?""",
            possibly_params,
        ).fetchall()
        for r in possibly_rows:
            if r["usr"] in dead_usr_set:
                continue
            results.append({
                "name": r["name"],
                "qualified_name": r["qualified_name"],
                "kind": r["kind"],
                "file_path": r["file_path"],
                "signature": r["signature"],
                "line": r["line"],
                "status": "possibly_dead",
                "reason": "assigned as function pointer but call sites unresolved",
                "indirect_refs": r["indirect_sites"] or "",
            })

    return results


def find_hotspots(
    conn: sqlite3.Connection,
    config_hash: str,
    limit: int = 20,
    exclude_paths: list[str] | None = None,
) -> list[dict]:
    """Find the most-called functions (hotspots) ranked by caller count.

    Only counts actual call edges (``ref_kind IN ('call', 'indirect')``) —
    plain references and member-access expressions are excluded so enum
    constants and fields don't appear as "hot" call targets.

    When ``hotspot_cache`` is populated for this config, returns instantly
    from the pre-computed cache.  Falls back to a live COUNT+GROUP BY
    query when the cache is missing or stale.

    When *exclude_paths* is given, symbols whose ``file_path`` matches any
    of the LIKE patterns are excluded.
    """
    # Try cache first
    cached = conn.execute(
        "SELECT COUNT(*) FROM hotspot_cache WHERE config_hash = ?", (config_hash,)
    ).fetchone()
    if cached and cached[0] > 0:
        path_clauses = ""
        params: list = [config_hash]
        if exclude_paths:
            path_clauses = "AND " + " AND ".join(
                "s.file_path NOT LIKE ?" for _ in exclude_paths
            )
            params.extend(exclude_paths)
        params.append(limit)
        rows = conn.execute(
            f"""SELECT s.name, s.qualified_name, s.kind, s.file_path,
                      s.signature, s.line,
                      h.caller_count
               FROM hotspot_cache h
               JOIN symbols s ON s.id = h.symbol_id AND s.config_hash = h.config_hash
               WHERE h.config_hash = ?
                 {path_clauses}
               ORDER BY h.caller_count DESC
               LIMIT ?""",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    # Live query fallback
    path_clauses = ""
    p: list = [config_hash]
    if exclude_paths:
        path_clauses = "AND " + " AND ".join(
            "s.file_path NOT LIKE ?" for _ in exclude_paths
        )
        p.extend(exclude_paths)
    p.append(limit)

    rows = conn.execute(
        f"""SELECT s.name, s.qualified_name, s.kind, s.file_path,
                  s.signature, s.line,
                  COUNT(r.rowid) AS caller_count
           FROM refs r
           JOIN symbols s ON s.usr = r.to_usr AND s.config_hash = r.config_hash
           WHERE r.config_hash = ?
             AND s.is_definition = 1
             AND r.ref_kind IN ('call', 'indirect')
             {path_clauses}
           GROUP BY s.usr
           ORDER BY caller_count DESC
           LIMIT ?""",
        p,
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
            ORDER BY bm25(symbols_fts, 1.2, 0.75, 10.0, 1.0, 3.0, 2.0, 1.0, 5.0, 1.0, 1.0, 1.0)
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
            group["subgroups"] = sorted(
                group.pop("_subgroups").values(), key=lambda g: g["name"]
            )

    return {
        "file": file_path,
        "total_symbols": len(rows),
        "symbols": groups,
    }


# ---------------------------------------------------------------------------
# LLM analysis helpers — structured symbol descriptions from Ollama
# ---------------------------------------------------------------------------

def upsert_llm_analysis_batch(
    conn: sqlite3.Connection,
    rows: list[tuple[int, str, str, str, str, str]],
) -> int:
    """Insert or replace LLM analysis rows.

    Each row: (symbol_id, summary, inputs, outputs, model, content_hash).
    Uses INSERT OR REPLACE so re-analysis is idempotent.
    Cleans orphaned rows and syncs denormalized columns to symbols.
    Returns number of rows inserted.
    """
    conn.execute(
        """DELETE FROM llm_analysis WHERE symbol_id NOT IN (
            SELECT id FROM symbols
        )"""
    )
    cur = conn.executemany(
        """INSERT OR REPLACE INTO llm_analysis(symbol_id, summary, inputs, outputs, model, content_hash, analyzed_at)
           VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
        rows,
    )
    # Sync denormalized columns on symbols table for FTS5 indexing
    conn.executemany(
        """UPDATE symbols SET summary = ?, inputs = ?, outputs = ? WHERE id = ?""",
        [(r[1], r[2], r[3], r[0]) for r in rows],
    )
    return cur.rowcount


def get_llm_analysis_for_symbol(
    conn: sqlite3.Connection,
    symbol_id: int,
) -> dict | None:
    """Return the LLM analysis row for a single symbol, or None."""
    row = conn.execute(
        """SELECT summary, inputs, outputs, model, analyzed_at
           FROM llm_analysis
           WHERE symbol_id = ?""",
        (symbol_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "summary": row["summary"],
        "inputs": row["inputs"],
        "outputs": row["outputs"],
        "model": row["model"],
        "analyzed_at": row["analyzed_at"],
    }


def count_llm_analysis(
    conn: sqlite3.Connection,
    config_hash: str,
) -> int:
    """Return how many symbols in a config have pre-computed LLM analysis."""
    return conn.execute(
        """SELECT COUNT(*) FROM llm_analysis a
           JOIN symbols s ON s.id = a.symbol_id
           WHERE s.config_hash = ?""",
        (config_hash,),
    ).fetchone()[0]


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
