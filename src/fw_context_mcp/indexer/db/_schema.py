"""Database schema definitions, migrations, and data backfills for fw-context-mcp.

This module defines the complete SQLite schema for the project index.
It handles three concerns that are usually split across separate modules:

1. **DDL**: CREATE TABLE/INDEX/TRIGGER statements in ``_SCHEMA``.
2. **Migration**: Column additions in ``_MIGRATION_ADD_COLUMNS`` and
   complex backfills in ``_run_data_migrations()``.
3. **Self-healing**: Critical-table re-creation and migration re-application
   on every connection open — the database must survive interrupted builds
   and partial schema upgrades.

── Schema versioning strategy ──

The schema version (``CURRENT_SCHEMA_VERSION``) is NOT a hand-maintained
integer.  It is a SHA-256 fingerprint of every CREATE TABLE column name
and every ALTER TABLE ADD COLUMN statement.  When a developer adds a new
column to ``_MIGRATION_ADD_COLUMNS``, the fingerprint changes automatically.
This eliminates the "forgot to bump the version constant" defect class.

── Table design rationale ──

Each table and index exists for a specific purpose.  The comments below
each CREATE statement explain:

- **Why the table exists** — the problem it solves for callers.
- **Why these column types** — TEXT over BLOB, INTEGER booleans, etc.
- **Why these indexes** — the query patterns each index accelerates.
- **UNIQUE constraints** — what constitutes a duplicate row and why.

── Migration strategy ──

Column additions are idempotent ALTER TABLE ADD COLUMN statements.
Complex data backfills (FTS5 rebuild, deduplication, composite PK upgrade)
run unconditionally on every ``open_db()`` call.  This guarantees that
a database last opened with an old fw-context version is automatically
brought to the current schema on first use.

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
    """Add a column to a table if it does not exist (idempotent).

    Used as a guard in functions that write to migrated columns.  The
    ALTER TABLE DDL auto-commits when ``isolation_level=''`` (the default)
    so no explicit COMMIT is needed.

    Why this exists: a migration may have run for project A's database
    but not project B's.  Rather than track per-project migration state,
    each write-path checks whether the column exists and adds it if not.
    This is cheaper than a full schema-version check on every write.
    """
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {type_def}")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e):
                raise


def _ensure_migrated_columns(conn: sqlite3.Connection) -> None:
    """Apply all ALTER TABLE ADD COLUMN migrations (idempotent).

    Runs on every ``open_db()`` call, not only when the schema version is
    out of date.  This handles cases where the version was stamped but
    individual migrations were skipped or interrupted (power loss, killed
    process, partial upgrade).

    Why a fast path: on a fully migrated database this returns in
    sub-milliseconds because PRAGMA table_info is read-only and cached
    by SQLite.  The write transaction is only entered when columns are
    actually missing — avoiding contention on the write lock.
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
    """Check whether a table exists in the database.

    Why a separate function: several migration paths need to check for
    optional tables (embeddings, llm_analysis) that may not exist in
    indexes built before those features were added.  Using sqlite_master
    directly each time would duplicate the query pattern.
    """
    result = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return result is not None


def _parse_expected_columns(schema_sql: str, migration_statements: list[str]) -> dict[str, set[str]]:
    """Extract table→column mapping from CREATE and ALTER TABLE statements.

    Parses ``_SCHEMA`` for ``CREATE TABLE ... (col1, col2, ...)`` and the
    migration list for ``ALTER TABLE t ADD COLUMN c``.

    Why regex-based parsing instead of SQLite introspection: the function
    must derive the schema fingerprint BEFORE the database exists — it runs
    against the source strings themselves.  SQLite introspection would
    require an actual database file to query.

    Why virtual tables are excluded: they are rebuilt on every connection
    open, so their columns are not part of the structural fingerprint.
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
    """Compute a stable fingerprint of the full database schema.

    Derived from the actual ``_SCHEMA`` and ALTER TABLE migrations
    — NOT a hand-maintained integer.  When a developer adds an
    ALTER TABLE to ``_MIGRATION_ADD_COLUMNS``, the fingerprint
    changes automatically.

    Why SHA-256 instead of a sequential integer: eliminates the human
    error of forgetting to bump the version.  Two developers adding
    migrations to different branches will produce different fingerprints,
    which triggers a full reindex instead of silent schema drift.

    The result is masked to 31 bits (``& 0x7FFFFFFF``) to fit in a
    signed 32-bit SQLite INTEGER without overflow.
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
    # Incremental re-embedding: content-addressable hash of the fields that
    # feed an embedding description.  Lets _build_embeddings skip unchanged
    # symbols instead of re-embedding the whole index on every `fw-context index`.
    "ALTER TABLE embeddings ADD COLUMN content_hash TEXT NOT NULL DEFAULT ''",
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
-- ── projects: multi-project registry ────────────────────────────────────
-- Each row is one indexed project.  Separate from build_configs so a single
-- project root can be re-indexed under different configs without duplicating
-- the project identity.  TEXT PRIMARY KEY (UUID4) avoids autoincrement
-- conflicts when merging databases.
CREATE TABLE IF NOT EXISTS projects (
    project_id   TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    root_path    TEXT NOT NULL
);

-- ── build_configs: one row per indexing run ─────────────────────────────
-- config_hash is a content-addressable fingerprint of the build configuration
-- (compile_commands.json path + compiler flags).  This lets the indexer
-- detect when the build config changed without comparing every file.
-- embedding_dim stores the dimension of the embedding model used during
-- indexing — needed to validate embeddings at query time.
-- manifest_verification tracks whether manifest.json exists (for header-
-- change detection).  first_indexed_at records the earliest index date
-- for staleness warnings.
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

-- ── files: source files tracked during indexing ─────────────────────────
-- UNIQUE(config_hash, path) means a file can appear in exactly one config.
-- mtime is a REAL (Julian day fraction) so file-change detection uses
-- sub-second precision and supports comparison across OS boundaries.
-- language stores 'c' or 'cpp' — needed for libclang TU creation and
-- syntax-aware search filtering.
-- generated = 1 marks auto-generated files (e.g., protobuf output) that
-- should be excluded from dead-code analysis.
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

-- ── symbols: the core table — one row per clang Cursor ─────────────────
-- USR (Unified Symbol Resolution) is clang's stable identifier.  The
-- UNIQUE(config_hash, usr) constraint prevents the same symbol from
-- being inserted twice across different TUs.
-- TEXT columns over BLOB: SQLite TEXT is natively searchable via LIKE
-- and FTS5, while BLOB requires deserialization for every query.
-- INTEGER booleans (is_definition, is_virtual, is_template): SQLite has
-- no native BOOLEAN type; INTEGER NOT NULL DEFAULT 0 is idiomatic and
-- allows efficient WHERE clause filtering.
-- parent_usr links methods/fields to their containing class, enabling
-- class-member queries and inheritance traversal.
-- template_usr links specializations to the primary template, enabling
-- template instantiation discovery.
-- is_project distinguishes application code from vendor SDK — a per-symbol
-- flag rather than per-file because files may mix project and vendor symbols
-- (e.g., headers included by both).
-- pagerank is pre-computed call-graph centrality — stored on the symbol
-- so ranking queries do not need runtime graph traversal.
-- source contains the filtered function body text — stored eagerly so
-- search_bodies queries hit the FTS5 index, not files on disk.
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

-- ── Index rationale ─────────────────────────────────────────────────────
-- Each index exists because a specific query pattern would otherwise
-- require a full table scan over thousands of symbols.

-- idx_symbols_name: lookup_symbol() exact-name matching — the most frequent
-- query in the system.  Covers both bare names and prefix LIKE.
CREATE INDEX IF NOT EXISTS idx_symbols_name        ON symbols(name);

-- idx_symbols_qname: three-tier name resolution where qualified_name is
-- the second tier (exact match) and third tier (suffix LIKE).  Without
-- this index, every find_refs() call scans all symbols.
CREATE INDEX IF NOT EXISTS idx_symbols_qname       ON symbols(qualified_name);

-- idx_symbols_kind: filtered queries (search_code with kind='function',
-- find_dead_code with kind IN ('function','method',...)).  Also accelerates
-- get_file_map which groups symbols by kind.
CREATE INDEX IF NOT EXISTS idx_symbols_kind        ON symbols(kind);

-- idx_symbols_file: file-level queries — "give me all symbols in main.cpp".
-- Used by get_file_map and per-file reindex lookups.
CREATE INDEX IF NOT EXISTS idx_symbols_file        ON symbols(file_id);

-- idx_symbols_parent: class member queries — get_class_members and
-- inheritance traversal.  config_hash is the leading column because
-- every query is scoped to one build config.
CREATE INDEX IF NOT EXISTS idx_symbols_parent     ON symbols(config_hash, parent_usr);

-- idx_symbols_template: find_template_instances — resolves
-- specializations by template_usr.  Without this, every specialization
-- lookup scans the entire symbols table.
CREATE INDEX IF NOT EXISTS idx_symbols_template  ON symbols(config_hash, template_usr);

-- idx_symbols_filepath: file-path-based filtering (exclude vendor paths
-- in project_only queries).  Also supports search_content which filters
-- at the file level before joining with FTS5 results.
CREATE INDEX IF NOT EXISTS idx_symbols_filepath  ON symbols(config_hash, file_path);

-- idx_symbols_def_kind: find_dead_code and find_hotspots — both filter
-- on is_definition + kind.  A compound index avoids an intersection of
-- two separate indexes.  config_hash leads because all queries are
-- scoped to one build.
CREATE INDEX IF NOT EXISTS idx_symbols_def_kind   ON symbols(config_hash, is_definition, kind);

-- idx_files_config: joins between files and other tables on config_hash.
-- Every file-related query is config-scoped.
CREATE INDEX IF NOT EXISTS idx_files_config        ON files(config_hash);

-- idx_files_is_project: project_only file filtering for search_content
-- and per-file project/vendor classification.
CREATE INDEX IF NOT EXISTS idx_files_is_project    ON files(is_project);

-- idx_files_config_mtime: incremental indexing — finds files whose mtime
-- changed since the last index.  config_hash + path is the unique key,
-- and including mtime in the index makes change detection a covered query.
CREATE INDEX IF NOT EXISTS idx_files_config_mtime  ON files(config_hash, path, mtime);

-- ── symbols_fts: full-text search over symbol metadata ─────────────────
-- External content FTS5 table (content='symbols') — the FTS5 index stores
-- only the tokenized text, while the actual data lives in symbols.  This
-- avoids duplicating gigabytes of source text.
-- Triggers keep the FTS index in sync: insert → insert, delete → delete,
-- update → delete-old + insert-new.  These are dropped during bulk indexing
-- (via drop_fts_triggers) and recreated afterward for performance.
-- name_tokens is included so CamelCase/snake_case token searches work
-- without regex — "FileSystem" is indexed as "file system".
-- summary/inputs/outputs enable semantic LLM-generated descriptions to
-- participate in keyword search.
-- source enables search_bodies — the only FTS5 column that points at
-- function body text rather than metadata.
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

-- ── FTS5 content-sync triggers ──────────────────────────────────────────
-- Each trigger pattern follows the FTS5 external-content protocol:
-- INSERT → push the new row into the FTS index.
-- DELETE → push a 'delete' token with the old rowid (FTS5 removes it).
-- UPDATE → push a delete for the old row, then an insert for the new row.
-- This is idempotent because FTS5 merges token lists per rowid.
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

-- ── files_fts: full-text search over ifdef-filtered file content ───────
-- External content FTS5 pointing at files.content.  Enables search_content
-- queries that find patterns in full file text (not limited to function
-- bodies).  Separate from symbols_fts because file-level and symbol-level
-- searches have different result granularity and ranking requirements.
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

-- ── refs: cross-reference / call-graph edges ────────────────────────────
-- Each row records one reference from source code to a symbol.  to_usr
-- points at the referenced definition; from_usr is the enclosing function
-- that contains the reference (NULL when at file scope — e.g. a global
-- variable initialization).
-- Why from_usr can be NULL: implicit constructor calls from global/static
-- objects lack an enclosing function.
-- Why no UNIQUE constraint initially: duplicates can occur when the same
-- TU is indexed twice.  A UNIQUE index is created during data migration
-- AFTER deduplication.
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

-- ── indirect_call_sites: function pointer invocation tracking ───────────
-- Records every call through a function pointer (driver.onData(buf, len)).
-- Unlike refs, the callee is a FIELD_DECL or VAR_DECL — the actual function
-- called is resolved later by joining with fp_assignments (Phase 3).
-- Why a separate table: the callee has a different USR domain (field/variable)
-- than direct calls (function/method).  Mixing them in refs would require
-- nullable fields and complicate every query.
-- expr_text stores the source-code expression (e.g. "driver.onData") so
-- callers can see HOW the pointer was invoked.
-- fn_ptr_type stores the function pointer type string — used by Phase 3c
-- fallback when exact USR matching fails.
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

-- ── fp_assignments: function pointer assignment records ─────────────────
-- Records both sides of "field = &function" so the linking phase can
-- connect fp_assignments.lhs_usr = indirect_call_sites.target_usr.
-- Why lhs and rhs columns: the LHS is the field/variable receiving the
-- pointer; the RHS is the function being assigned.  Both are needed for
-- the Phase 3 resolution: "which functions can be called through this field?"
-- method stores the assignment type: assignment, call_arg, var_init, init_list.
-- This distinction matters because call_arg assignments (callback(&fn, obj))
-- are resolved differently than direct assignments (field = &fn).
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

-- ── embeddings: semantic search vectors ─────────────────────────────────
-- Stored as BLOB of float32 values packed with struct.pack('f', ...).
-- Why BLOB over TEXT: binary packing is 4 bytes per float vs ~10 bytes
-- for textual representation — 4x space savings for 1024-dim vectors.
-- Why ON DELETE CASCADE: when a symbol row is removed, its embeddings
-- are automatically cleaned up without needing a separate DELETE.
-- Why composite PK (symbol_id, chunk_index): supports multi-chunk
-- embeddings for models with output dimensions beyond SQLite's BLOB
-- size limits.  chunk_index = 0 for single-chunk embeddings.
-- Why content_hash: enables incremental re-embedding — a symbol is
-- re-embedded only when its source content changed.  Without this,
-- every `fw-context index` would regenerate all embeddings.
CREATE TABLE IF NOT EXISTS embeddings (
    symbol_id    INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
    chunk_index  INTEGER NOT NULL DEFAULT 0,
    embedding    BLOB    NOT NULL,
    model        TEXT    NOT NULL,
    content_hash TEXT    NOT NULL DEFAULT '',
    updated_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (symbol_id, chunk_index)
);
CREATE INDEX IF NOT EXISTS idx_embeddings_symbol ON embeddings(symbol_id);

-- ── llm_analysis: pre-computed LLM symbol descriptions ──────────────────
-- Stores structured analysis (summary, inputs, outputs) generated by
-- an LLM during `fw-context index --analyze`.  1:1 with the symbols
-- table (symbol_id is PRIMARY KEY).
-- Why denormalized into symbols columns too: summary, inputs, and outputs
-- are also stored as columns on the symbols table for direct FTS5 search.
-- The llm_analysis table is the authoritative source; symbols columns
-- are a read-optimized copy updated during migration.
-- Why ON DELETE CASCADE: when a symbol is removed, its analysis must
-- also be removed — no orphan analysis rows.
CREATE TABLE IF NOT EXISTS llm_analysis (
    symbol_id    INTEGER PRIMARY KEY REFERENCES symbols(id) ON DELETE CASCADE,
    summary      TEXT    NOT NULL DEFAULT '',
    inputs       TEXT    NOT NULL DEFAULT '',
    outputs      TEXT    NOT NULL DEFAULT '',
    model        TEXT    NOT NULL,
    analyzed_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    content_hash TEXT    NOT NULL DEFAULT ''
);


-- ── inheritance: C++ class hierarchy edges ──────────────────────────────
-- Each row is one base class of a derived class.  Multiple inheritance
-- produces multiple rows for the same derived_usr.
-- Why UNIQUE(config_hash, derived_usr, base_usr): the same inheritance
-- edge can be observed in multiple TUs — deduplication at INSERT time
-- is cheaper than post-processing.
-- Why access column: method override queries need to know visibility
-- (a private base class's public methods are not accessible).
-- Why is_virtual: virtual base classes in diamond inheritance are
-- only traversed once during BFS — ignoring virtual bases would create
-- duplicate ancestor paths.
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

-- ── overrides: virtual method dispatch tracking ─────────────────────────
-- Each row records that derived_usr overrides base_usr.  Built as a
-- post-processing step after inheritance chains are indexed — the indexer
-- walks each class hierarchy and matches methods by name + signature.
-- Why a separate table from inheritance: inheritance is a class-level
-- relationship; overrides is a method-level relationship.  Mixing them
-- would require nullable columns and make both queries slower.
-- UNIQUE prevents the same override being recorded from multiple TUs.
CREATE TABLE IF NOT EXISTS overrides (
    id           INTEGER PRIMARY KEY,
    config_hash  TEXT    NOT NULL REFERENCES build_configs(config_hash),
    derived_usr  TEXT    NOT NULL,
    base_usr     TEXT    NOT NULL,
    UNIQUE(config_hash, derived_usr, base_usr)
);
CREATE INDEX IF NOT EXISTS idx_overrides_derived ON overrides(config_hash, derived_usr);
CREATE INDEX IF NOT EXISTS idx_overrides_base    ON overrides(config_hash, base_usr);

-- ── macros: preprocessor macro definitions ──────────────────────────────
-- Collected via CXCursor_MacroDefinition during TU parsing.
-- Why UNIQUE(config_hash, file_id, line): a macro at a given line in a
-- given file is unique.  The name alone is not unique — the same name
-- can be #define'd differently in different files or #ifdef branches.
-- Why expanded_value: populated by clang -dM -E — gives the preprocessor-
-- resolved value, which is different from the raw #define body (e.g.,
-- concatenation and stringification are applied).
-- Why is_function_like: distinguishes object-like from function-like
-- macros — needed because function-like macros participate in search
-- differently (they accept arguments).
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

-- ── macros_fts: full-text search over macro definitions ─────────────────
-- Separate from symbols_fts because macros have a different content model
-- (name + value + expanded_value vs name + signature + docstring + source).
-- External content FTS5 with content='macros' — non-duplicating storage.
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

-- ── hotspot_cache: pre-computed caller-count rankings ───────────────────
-- Stores caller_count for each symbol so find_hotspots() returns instantly.
-- Why a cache table instead of a live query: the live query requires a
-- COUNT(*) GROUP BY over the entire refs table (~1M+ rows).  Pre-computing
-- at index time eliminates the 2-5 second latency on every hotspot query.
-- Why UNIQUE(config_hash, symbol_id): one caller count per symbol per build.
-- Why ON DELETE CASCADE: when a symbol is removed, its cache entry is
-- automatically cleaned up.
CREATE TABLE IF NOT EXISTS hotspot_cache (
    id           INTEGER PRIMARY KEY,
    config_hash  TEXT    NOT NULL REFERENCES build_configs(config_hash),
    symbol_id    INTEGER NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
    caller_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE(config_hash, symbol_id)
);
CREATE INDEX IF NOT EXISTS idx_hotspot_cache_config ON hotspot_cache(config_hash);
"""

# ── Self-healing critical-tables block ───────────────────────────────────
# Executed on every connection open.  CREATE TABLE IF NOT EXISTS is
# idempotent — it takes microseconds when the table already exists.
# This guarantees that tables survive a crashed migration or partial
# index where some tables were created and others were not.
#
# Why auto-generated from _SCHEMA annotations: tables annotated with
# ``-- CRITICAL_TABLE`` in the comment immediately preceding their
# CREATE statement are automatically included.  This eliminates the
# need to manually synchronize two lists of table names.
#
# A table is critical if its loss would cause query failures rather
# than degraded results.  Tables like embeddings and hotspot_cache
# are NOT critical — they can be re-built from the core tables.


def _build_critical_tables() -> str:
    """Extract CRITICAL_TABLE-annotated CREATE statements from _SCHEMA.

    Every CREATE TABLE / CREATE INDEX whose preceding comment line
    contains ``CRITICAL_TABLE`` is included in the self-healing block.

    Why comment-annotation instead of a separate list: automatic
    extraction ensures the self-healing block is always up to date.
    When a developer adds a table, they only need to add the
    ``-- CRITICAL_TABLE`` comment — the extraction code finds it.
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
    """Run idempotent data backfills under SAVEPOINT and FTS5 rebuild.

    Data backfills (file_path, name_tokens, llm_analysis denormalization)
    run inside a SAVEPOINT so any failure rolls back to the pre-migration
    state.  The FTS5 rebuild and embeddings migration run outside the
    SAVEPOINT because ``executescript()`` implicitly issues COMMIT.

    Why idempotent: this function runs on every ``open_db()`` call, not
    just on first use.  Each step checks whether it has work to do (e.g.,
    ``WHERE file_path = ''``) before executing.  This is cheaper than
    maintaining a separate migration-tracking table.

    Why SAVEPOINT over BEGIN/COMMIT: the caller holds a connection-level
    transaction.  SAVEPOINT nests within it, so a backfill failure only
    rolls back its own work, not the caller's transaction.

    Why FTS5 rebuild after SAVEPOINT: the rebuild function uses
    executescript() which issues an implicit COMMIT — SQLite does not
    allow this inside an active SAVEPOINT.
    """
    from fw_context_mcp.indexer.db import split_tokens

    conn.execute("SAVEPOINT data_migration")
    try:
        # ── file_path backfill ──────────────────────────────────────
        # Symbols indexed before file_path was added have empty strings.
        # Backfill from files.path via the file_id foreign key.
        # Why denormalized: file_path on every symbol avoids a JOIN to
        # files on every query — the most common query patterns
        # (search by name, filter by path) need it thousands of times.
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

        # ── name_tokens backfill ─────────────────────────────────────
        # name_tokens stores CamelCase/snake_case split tokens for FTS5
        # search.  Old indexes lack this column.  Backfill using the
        # split_tokens() function which decomposes identifiers into
        # searchable word tokens (e.g., "FileSystemInit" → "file system init").
        # Why stored: computing tokens at query time is too slow for FTS5
        # matching — FTS5 needs pre-tokenized text.
        # Why RELEASE + new SAVEPOINT between steps: long-running UPDATE
        # within a single SAVEPOINT can bloat the rollback journal.
        # Releasing periodically keeps memory usage bounded.
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

        # ── LLM analysis denormalization ──────────────────────────────
        # summary/inputs/outputs are stored on both llm_analysis (authoritative)
        # and symbols (read-optimized copy).  This backfill copies existing
        # LLM analysis into the symbols columns so FTS5 search works
        # immediately after migration — no re-analysis needed.
        # Why denormalized: including these fields in symbols_fts means
        # search_code queries can match LLM-generated descriptions without
        # joining llm_analysis.
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


        conn.execute("RELEASE data_migration")
    except sqlite3.Error:
        try:
            conn.execute("ROLLBACK TO data_migration")
        except sqlite3.OperationalError:
            pass  # SAVEPOINT may have already been released or never created
        raise

    # ── FTS5 rebuild (post-SAVEPOINT — executescript() issues COMMIT) ──
    # FTS5 tables can get out of sync when triggers were dropped during
    # bulk indexing but not recreated due to a crash.  Rebuilding here
    # guarantees a fresh, consistent FTS index on every open.
    conn.execute("DROP TABLE IF EXISTS file_analysis")
    from fw_context_mcp.indexer.db import rebuild_fts

    rebuild_fts(conn)

    # ── Deduplicate refs + create unique index ─────────────────────
    # Duplicate refs can accumulate when the same TU is indexed multiple
    # times (e.g., reindex after a header change).  The UNIQUE index
    # prevents future duplicates; this step cleans up existing ones.
    # Why MIN(rowid): keeps the first-inserted row — deterministic
    # and avoids choosing arbitrarily between identical duplicates.
    idx_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_refs_unique'"
    ).fetchone()
    if not idx_exists:
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
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_refs_unique "
            "ON refs(config_hash, to_usr, from_file, from_line, from_usr, ref_kind)"
        )

    # ── Embeddings composite PK migration ───────────────────────────
    # Original schema had a single-column PK (symbol_id).  The composite
    # PK (symbol_id, chunk_index) was added to support multi-chunk
    # embeddings for large models.  This migration recreates the table
    # with the new schema, preserving all existing data.
    # Why NOT ALTER TABLE: SQLite cannot alter PRIMARY KEY constraints.
    # The only way is CREATE new → INSERT data → DROP old → RENAME.
    # Why BEGIN IMMEDIATE: prevents other connections from starting a
    # transaction while the table is being rebuilt.
    if _table_exists(conn, "embeddings"):
        emb_cols = [
            r[1]
            for r in conn.execute(
                "PRAGMA table_info(embeddings)"
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
    """Drop all FTS5 content-sync triggers for symbols, files, and macros.

    Called before bulk indexing to eliminate per-row FTS index overhead.
    When indexing 50,000+ symbols, each INSERT/UPDATE fires an FTS trigger
    that tokenizes and indexes the new text — this adds 30-50% to total
    indexing time.  Dropping the triggers defers FTS population until
    all TUs are processed, then ``rebuild_fts()`` populates everything
    in a single pass.

    The triggers are recreated by `rebuild_fts()` which calls
    ``CREATE TRIGGER IF NOT EXISTS`` — idempotent and safe to call
    after a crash.
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
