"""Schema definitions, migrations, and data backfills for fw-context-mcp index.

Note: SQL DEFAULT (datetime('now')) captures per-row INSERT timestamps.
Rows inserted in the same transaction may share identical timestamps —
this is expected and harmless.  Use first_indexed_at for the earliest event.
"""

from __future__ import annotations

import hashlib
import logging
import re
import sqlite3

log = logging.getLogger(__name__)

__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "_ensure_column",
    "drop_fts_triggers",
]


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, type_def: str) -> None:
    """Idempotently add a column to a table if it doesn't exist.

    Used as a belt-and-suspenders guard in functions that write to columns
    added by schema migrations — handles the case where the migration ran
    for one project's database but not another.

    The ALTER TABLE DDL auto-commits in SQLite's default autocommit mode
    (``isolation_level=''``), so no explicit ``COMMIT`` is needed here.
    """
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {type_def}")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e):
                raise


def _ensure_migrated_columns(conn: sqlite3.Connection) -> None:
    """Apply all ALTER TABLE ADD COLUMN migrations idempotently.

    Runs UNCONDITIONALLY on every ``open_db()`` call, not just when the
    schema version is outdated.  Handles edge cases where the version
    was stamped but individual migrations were skipped or interrupted.

    Fast path: checks all expected columns via ``PRAGMA table_info``
    (read-only, no write lock).  On a fully migrated DB this returns
    in sub-milliseconds.  Only falls through to the write transaction
    when columns are genuinely missing.
    """
    # Fast path — read-only, no write lock.
    for table, expected in _MIGRATED_COLUMNS.items():
        actual = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if not expected.issubset(actual):
            break
    else:
        return  # all columns present

    # Slow path — some columns missing, apply migrations.
    for stmt in _MIGRATION_ADD_COLUMNS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e) and "no such table" not in str(e):
                raise

def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """Return True if the named table exists in the database."""
    result = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return result is not None


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
        schema_sql,
        re.DOTALL | re.IGNORECASE,
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
                "PRIMARY",
                "UNIQUE",
                "FOREIGN",
                "CHECK",
                "CONSTRAINT",
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
            stmt.strip(),
            re.IGNORECASE,
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

    tables = _parse_expected_columns(schema_sql, migration_statements)
    canonical = "".join(f"{table}:{','.join(sorted(cols))};" for table, cols in sorted(tables.items()))
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
    "ALTER TABLE symbols ADD COLUMN source TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE llm_analysis ADD COLUMN content_hash TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE build_configs ADD COLUMN embedding_dim INTEGER",
    "ALTER TABLE files ADD COLUMN content TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE files ADD COLUMN content_hash TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE files ADD COLUMN source_hash TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE files ADD COLUMN flags_hash TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE build_configs ADD COLUMN manifest_verification TEXT NOT NULL DEFAULT 'none'",
    "ALTER TABLE build_configs ADD COLUMN description TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE build_configs ADD COLUMN first_indexed_at TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE build_configs ADD COLUMN analyze_vendor INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE files ADD COLUMN is_project INTEGER NOT NULL DEFAULT 0",
    # Schema version bump — DO NOT REMOVE. When adding new migration steps after this
    # column, also add a NEW ALTER TABLE … ADD COLUMN _schema_bump_… line. The hash
    # of _MIGRATION_ADD_COLUMNS drives CURRENT_SCHEMA_VERSION.
    "ALTER TABLE build_configs ADD COLUMN _schema_bump_emb_chunk_idx INTEGER NOT NULL DEFAULT 0"  # schema version bump marker,
]

# Pre-computed {table: {columns}} from _MIGRATION_ADD_COLUMNS.
# Used by _ensure_migrated_columns() for a fast read-only check that
# skips the write transaction when all columns already exist.
_MIGRATED_COLUMNS: dict[str, set[str]] = {}
for _stmt in _MIGRATION_ADD_COLUMNS:
    _m = re.match(r"ALTER TABLE (\w+) ADD COLUMN (\w+)", _stmt, re.IGNORECASE)
    if _m:
        _MIGRATED_COLUMNS.setdefault(_m.group(1), set()).add(_m.group(2))

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
    compile_commands_path   TEXT NOT NULL,
    embedding_dim           INTEGER,
    manifest_verification   TEXT NOT NULL DEFAULT 'none',
    description             TEXT NOT NULL DEFAULT '',
    first_indexed_at        TEXT NOT NULL DEFAULT '',
    analyze_vendor          INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS files (
    id           INTEGER PRIMARY KEY,
    config_hash  TEXT NOT NULL REFERENCES build_configs(config_hash),
    path         TEXT NOT NULL,
    language     TEXT NOT NULL,     -- 'c' | 'cpp'
    generated    INTEGER NOT NULL DEFAULT 0,
    is_project   INTEGER NOT NULL DEFAULT 0,
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
    is_project     INTEGER NOT NULL DEFAULT 0,
    pagerank       REAL    NOT NULL DEFAULT 0.0,
    source         TEXT    NOT NULL DEFAULT '',
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
CREATE INDEX IF NOT EXISTS idx_files_is_project    ON files(is_project);
CREATE INDEX IF NOT EXISTS idx_files_config_mtime  ON files(config_hash, path, mtime);
CREATE INDEX IF NOT EXISTS idx_symbols_def_kind   ON symbols(config_hash, is_definition, kind);

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
    source,
    content='symbols',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS symbols_ai AFTER INSERT ON symbols BEGIN
    INSERT INTO symbols_fts(rowid, name, qualified_name, signature, docstring, file_path, name_tokens, summary, inputs, outputs, source)
    VALUES (new.id, new.name, new.qualified_name, new.signature, new.docstring, new.file_path, new.name_tokens, new.summary, new.inputs, new.outputs, new.source);
END;

CREATE TRIGGER IF NOT EXISTS symbols_ad AFTER DELETE ON symbols BEGIN
    INSERT INTO symbols_fts(symbols_fts, rowid, name, qualified_name, signature, docstring, file_path, name_tokens, summary, inputs, outputs, source)
    VALUES ('delete', old.id, old.name, old.qualified_name, old.signature, old.docstring, old.file_path, old.name_tokens, old.summary, old.inputs, old.outputs, old.source);
END;

CREATE TRIGGER IF NOT EXISTS symbols_au AFTER UPDATE ON symbols BEGIN
    INSERT INTO symbols_fts(symbols_fts, rowid, name, qualified_name, signature, docstring, file_path, name_tokens, summary, inputs, outputs, source)
    VALUES ('delete', old.id, old.name, old.qualified_name, old.signature, old.docstring, old.file_path, old.name_tokens, old.summary, old.inputs, old.outputs, old.source);
    INSERT INTO symbols_fts(rowid, name, qualified_name, signature, docstring, file_path, name_tokens, summary, inputs, outputs, source)
    VALUES (new.id, new.name, new.qualified_name, new.signature, new.docstring, new.file_path, new.name_tokens, new.summary, new.inputs, new.outputs, new.source);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
    path,
    content,
    content='files',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS files_ai AFTER INSERT ON files BEGIN
    INSERT INTO files_fts(rowid, path, content)
    VALUES (new.id, new.path, new.content);
END;

CREATE TRIGGER IF NOT EXISTS files_ad AFTER DELETE ON files BEGIN
    INSERT INTO files_fts(files_fts, rowid, path, content)
    VALUES ('delete', old.id, old.path, old.content);
END;

CREATE TRIGGER IF NOT EXISTS files_au AFTER UPDATE ON files BEGIN
    INSERT INTO files_fts(files_fts, rowid, path, content)
    VALUES ('delete', old.id, old.path, old.content);
    INSERT INTO files_fts(rowid, path, content)
    VALUES (new.id, new.path, new.content);
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

-- Indirect function-pointer call sites (Phase 2). -- CRITICAL_TABLE
-- Records locations where a function pointer field or variable is invoked --
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

-- Function pointer assignments (Phase 3). -- CRITICAL_TABLE
-- Records both sides of "field = &function" so Phase 3 can link
-- fp_assignments.lhs_usr = indirect_call_sites.target_usr to answer
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
-- Stored as BLOB: variable-dimension float32 values packed with struct.pack('f', ...).
-- Common: mxbai-embed-large → 1024 floats (4096 bytes), qwen3-embedding → 4096 floats (16384 bytes).
-- ON DELETE CASCADE: when a symbol row is deleted, all of its embedding chunks are removed.
CREATE TABLE IF NOT EXISTS embeddings (
    symbol_id    INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
    chunk_index  INTEGER NOT NULL DEFAULT 0,
    embedding    BLOB    NOT NULL,
    model        TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (symbol_id, chunk_index)
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

-- C++ inheritance hierarchy edges. -- CRITICAL_TABLE
-- derived_usr → base_usr: class Derived : public Base { ... }
-- access: "public", "protected", "private"
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

-- Virtual method override tracking. -- CRITICAL_TABLE
-- derived_usr → base_usr: DerivedClass::method overrides BaseClass::method.
-- Built as a post-processing step after inheritance chains are indexed.
CREATE TABLE IF NOT EXISTS overrides (
    id           INTEGER PRIMARY KEY,
    config_hash  TEXT    NOT NULL REFERENCES build_configs(config_hash),
    derived_usr  TEXT    NOT NULL,
    base_usr     TEXT    NOT NULL,
    UNIQUE(config_hash, derived_usr, base_usr)
);
CREATE INDEX IF NOT EXISTS idx_overrides_derived ON overrides(config_hash, derived_usr);
CREATE INDEX IF NOT EXISTS idx_overrides_base    ON overrides(config_hash, base_usr);

-- Preprocessor macro definitions (#define). -- CRITICAL_TABLE
-- Collected via CXCursor_MacroDefinition during TU parsing.
-- expanded_value is populated by the clang -dM -E driver (opt-in).
CREATE TABLE IF NOT EXISTS macros (
    id               INTEGER PRIMARY KEY,
    config_hash      TEXT    NOT NULL REFERENCES build_configs(config_hash),
    file_id          INTEGER NOT NULL REFERENCES files(id),
    name             TEXT    NOT NULL,
    value            TEXT    NOT NULL DEFAULT '',
    expanded_value   TEXT    NOT NULL DEFAULT '',
    line             INTEGER NOT NULL,
    is_function_like INTEGER NOT NULL DEFAULT 0,
    UNIQUE(config_hash, file_id, line)
);
CREATE INDEX IF NOT EXISTS idx_macros_name ON macros(name, config_hash);
CREATE INDEX IF NOT EXISTS idx_macros_file ON macros(file_id);

CREATE VIRTUAL TABLE IF NOT EXISTS macros_fts USING fts5(
    name,
    value,
    expanded_value,
    content='macros',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS macros_ai AFTER INSERT ON macros BEGIN
    INSERT INTO macros_fts(rowid, name, value, expanded_value)
    VALUES (new.id, new.name, new.value, new.expanded_value);
END;

CREATE TRIGGER IF NOT EXISTS macros_ad AFTER DELETE ON macros BEGIN
    INSERT INTO macros_fts(macros_fts, rowid, name, value, expanded_value)
    VALUES ('delete', old.id, old.name, old.value, old.expanded_value);
END;

CREATE TRIGGER IF NOT EXISTS macros_au AFTER UPDATE ON macros BEGIN
    INSERT INTO macros_fts(macros_fts, rowid, name, value, expanded_value)
    VALUES ('delete', old.id, old.name, old.value, old.expanded_value);
    INSERT INTO macros_fts(rowid, name, value, expanded_value)
    VALUES (new.id, new.name, new.value, new.expanded_value);
END;

-- Pre-computed hotspot cache — caller counts for instant find_hotspots queries. -- CRITICAL_TABLE
CREATE TABLE IF NOT EXISTS hotspot_cache (
    id           INTEGER PRIMARY KEY,
    config_hash  TEXT    NOT NULL REFERENCES build_configs(config_hash),
    symbol_id    INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
    caller_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE(config_hash, symbol_id)
);
CREATE INDEX IF NOT EXISTS idx_hotspot_cache_config ON hotspot_cache(config_hash);
"""

# ── Self-healing critical-tables block ───────────────────────────────────────────────────
# Executed unconditionally on every connection open (CREATE TABLE IF NOT EXISTS
# is idempotent, taking microseconds when the table already exists).  This
# guarantees that tables survive a crashed migration or partial index.
#
# _CRITICAL_TABLES is auto-generated from _SCHEMA.  Tables annotated with
# ``-- CRITICAL_TABLE`` in the comment immediately preceding their CREATE
# statement are automatically included.  No manual synchronization needed.


def _build_critical_tables() -> str:
    """Extract CRITICAL_TABLE-annotated CREATE statements from _SCHEMA.

    Every CREATE TABLE / CREATE INDEX whose preceding comment line
    contains ``CRITICAL_TABLE`` is included in the self-healing block.
    Captures the full multi-line statement body through the matching
    ``);`` terminator.
    """
    lines = _SCHEMA.split("\n")
    parts: list[str] = []
    capture = False
    paren_depth = 0
    for line in lines:
        stripped = line.strip()
        if "CRITICAL_TABLE" in stripped and stripped.startswith("--"):
            capture = True
            continue
        if capture:
            if stripped.upper().startswith("CREATE TABLE"):
                parts.append(line)
                paren_depth = line.count("(") - line.count(")")
            elif stripped.upper().startswith("CREATE INDEX"):
                parts.append(line)
            elif paren_depth > 0:
                parts.append(line)
                paren_depth += line.count("(") - line.count(")")
                if paren_depth == 0:
                    capture = False
            elif stripped == "" or stripped.startswith("--"):
                # Blank lines and comments between CRITICAL_TABLE marker
                # and the actual CREATE statement — keep capturing.
                pass
            else:
                capture = False
    result = "\n".join(parts).rstrip("\n") + "\n"
    return result


_CRITICAL_TABLES = _build_critical_tables()


CURRENT_SCHEMA_VERSION = _derive_schema_version(_SCHEMA, _MIGRATION_ADD_COLUMNS)


def _run_data_migrations(conn: sqlite3.Connection) -> None:
    """Run idempotent data backfills (inside SAVEPOINT) and FTS5 rebuild.

    Data backfills (file_path, name_tokens, summary/inputs/outputs) run
    inside a SAVEPOINT so any failure leaves the DB unchanged.
    The FTS5 rebuild and embeddings migration run outside the SAVEPOINT
    because ``executescript()`` implicitly issues COMMIT.
    Caller must have the schema already created (via executescript(_SCHEMA)
    and _ensure_migrated_columns) before calling this function.
    """
    from fw_context_mcp.indexer.db import split_tokens

    conn.execute("SAVEPOINT data_migration")
    try:
        # file_path backfill
        if conn.execute(
            "SELECT COUNT(*) FROM symbols WHERE file_path = ''"
        ).fetchone()[0] > 0:
            conn.execute(
                """UPDATE symbols SET file_path = COALESCE(
                    (SELECT f.path FROM files f WHERE f.id = symbols.file_id), ''
                ) WHERE file_path = ''"""
            )
            conn.execute("RELEASE data_migration")
            conn.execute("SAVEPOINT data_migration")

        # name_tokens backfill
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
            conn.execute("RELEASE data_migration")
            conn.execute("SAVEPOINT data_migration")

        # summary/inputs/outputs backfill from llm_analysis
        if conn.execute(
            "SELECT COUNT(*) FROM symbols WHERE summary = ''"
        ).fetchone()[0] > 0:
            conn.execute(
                """UPDATE symbols SET
                    summary = COALESCE(
                        (SELECT a.summary FROM llm_analysis a
                         WHERE a.symbol_id = symbols.id), ''),
                    inputs = COALESCE(
                        (SELECT a.inputs FROM llm_analysis a
                         WHERE a.symbol_id = symbols.id), ''),
                    outputs = COALESCE(
                        (SELECT a.outputs FROM llm_analysis a
                         WHERE a.symbol_id = symbols.id), '')
                WHERE id IN (SELECT symbol_id FROM llm_analysis)"""
            )
            conn.execute("RELEASE data_migration")
            conn.execute("SAVEPOINT data_migration")

        # ── Deduplicate refs before creating unique index ──
        dup_count = conn.execute(
            """SELECT COUNT(*) FROM refs WHERE rowid NOT IN (
                SELECT MIN(rowid) FROM refs
                GROUP BY config_hash, to_usr, from_file, from_line, from_usr, ref_kind
            )"""
        ).fetchone()[0]
        if dup_count > 0:
            log.info("Deduplicating %d duplicate ref rows", dup_count)
            conn.execute(
                """DELETE FROM refs WHERE rowid NOT IN (
                    SELECT MIN(rowid) FROM refs
                    GROUP BY config_hash, to_usr, from_file, from_line, from_usr, ref_kind
                )"""
            )
            conn.execute("RELEASE data_migration")
            conn.execute("SAVEPOINT data_migration")

        # ── Create unique index on refs (idempotent; dedup ran first) ──
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_refs_unique "
            "ON refs(config_hash, to_usr, from_file, from_line, from_usr, ref_kind)"
        )

        conn.execute("RELEASE data_migration")
    except sqlite3.Error:
        try:
            conn.execute("ROLLBACK TO data_migration")
        except sqlite3.OperationalError:
            pass  # SAVEPOINT may have already been released or never created
        raise

    # ── FTS5 rebuild (after SAVEPOINT — executescript() issues COMMIT) ──
    from fw_context_mcp.indexer.db import rebuild_fts

    rebuild_fts(conn)

    # ── embeddings composite PK migration ──
    if _table_exists(conn, "embeddings"):
        emb_cols = [
            r[1]
            for r in conn.execute(
                "PRAGMA table_info(embeddings)"  # NOTE: sub-ms per call, negligible overhead
            ).fetchall()
        ]
        if "chunk_index" not in emb_cols:
            log.info(
                "Migrating embeddings table to composite PK "
                "(symbol_id, chunk_index)..."
            )
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS embeddings_v2 (
                        symbol_id    INTEGER NOT NULL
                            REFERENCES symbols(id) ON DELETE CASCADE,
                        chunk_index  INTEGER NOT NULL DEFAULT 0,
                        embedding    BLOB    NOT NULL,
                        model        TEXT    NOT NULL,
                        updated_at   TEXT    NOT NULL
                            DEFAULT (datetime('now')),
                        PRIMARY KEY (symbol_id, chunk_index)
                    )"""
                )
                conn.execute(
                    """INSERT OR IGNORE INTO embeddings_v2
                        (symbol_id, chunk_index, embedding, model, updated_at)
                    SELECT symbol_id, 0, embedding, model, updated_at
                    FROM embeddings"""
                )
                conn.execute("DROP TABLE IF EXISTS embeddings")
                conn.execute(
                    "ALTER TABLE embeddings_v2 RENAME TO embeddings"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_embeddings_symbol ON embeddings(symbol_id)"
                )
                conn.commit()
            except sqlite3.Error:
                conn.execute("ROLLBACK")
                raise


def drop_fts_triggers(conn: sqlite3.Connection) -> None:
    """Drop FTS5 content-sync triggers for symbols and files tables.

    Call before bulk indexing to eliminate the per-row FTS index overhead.
    After all TUs are processed, call ``rebuild_fts()`` and
    ``rebuild_files_fts()`` to recreate the FTS tables and triggers
    in one pass.
    """
    conn.execute("DROP TRIGGER IF EXISTS symbols_ai")
    conn.execute("DROP TRIGGER IF EXISTS symbols_ad")
    conn.execute("DROP TRIGGER IF EXISTS symbols_au")
    conn.execute("DROP TRIGGER IF EXISTS files_ai")
    conn.execute("DROP TRIGGER IF EXISTS files_ad")
    conn.execute("DROP TRIGGER IF EXISTS files_au")
    conn.execute("DROP TRIGGER IF EXISTS macros_ai")
    conn.execute("DROP TRIGGER IF EXISTS macros_ad")
    conn.execute("DROP TRIGGER IF EXISTS macros_au")
