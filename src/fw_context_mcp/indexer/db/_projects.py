"""Project and build config management for the fw-context-mcp index."""

from __future__ import annotations

import sqlite3

__all__ = [
    "delete_build_data",
    "get_active_config",
    "get_all_builds_for_project",
    "get_all_projects",
    "get_build_stats",
    "upsert_build_config",
    "upsert_project",
]



def delete_build_data(conn: sqlite3.Connection, config_hash: str) -> None:
    """Delete all data for a given *config_hash*.

    ``embeddings`` are handled by ON DELETE CASCADE —
    they do not need explicit DELETE statements.

    Safe to call after a successful reindex — old config_hash data is
    no longer needed and its presence bloats the database.
    """
    conn.execute("DELETE FROM symbols WHERE config_hash = ?", (config_hash,))
    conn.execute("DELETE FROM macros WHERE config_hash = ?", (config_hash,))
    conn.execute("DELETE FROM refs WHERE config_hash = ?", (config_hash,))
    conn.execute("DELETE FROM fp_assignments WHERE config_hash = ?", (config_hash,))
    conn.execute("DELETE FROM indirect_call_sites WHERE config_hash = ?", (config_hash,))
    conn.execute("DELETE FROM inheritance WHERE config_hash = ?", (config_hash,))
    conn.execute("DELETE FROM overrides WHERE config_hash = ?", (config_hash,))
    conn.execute("DELETE FROM hotspot_cache WHERE config_hash = ?", (config_hash,))
    conn.execute("DELETE FROM files WHERE config_hash = ?", (config_hash,))
    conn.execute("DELETE FROM build_configs WHERE config_hash = ?", (config_hash,))
    # vec0 virtual table — not covered by ON DELETE CASCADE.
    # The table only exists when the sqlite-vec extension is loaded.
    try:
        conn.execute("DELETE FROM vec_symbols WHERE config_hash = ?", (config_hash,))
    except sqlite3.OperationalError:
        pass  # sqlite-vec not loaded — vec_symbols table doesn't exist


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
    embedding_dim: int | None = None,
    manifest_verification: str = "none",
    description: str = "",
    analyze_vendor: int = 0,
) -> None:
    """Insert or update a build configuration record.

    Uses ``ON CONFLICT`` so re-indexing the same config refreshes
    ``created_at``, ``description``, and ``compile_commands_path``.
    ``first_indexed_at`` is set only on the first insert and preserved
    on subsequent updates.

    Args:
        conn: Open database connection.
        config_hash: Content-addressable hash of the compile_commands.json.
        project_id: Foreign key to ``projects``.
        compile_commands_path: Absolute path to ``compile_commands.json``.
        embedding_dim: Embedding vector dimension detected from the model.
            ``None`` when embeddings are disabled or not yet generated.
        manifest_verification: ``"full"`` or ``"none"`` —
            indicates whether manifest.json was available during indexing.
        description: Human-readable build description (git branch + tag).
            Updated on every index to reflect current git context.

    Returns:
        None.
    """

    # Columns guaranteed by open_db() → _ensure_migrated_columns()
    conn.execute(
        """INSERT INTO build_configs(config_hash, project_id, compile_commands_path,
                                     embedding_dim, manifest_verification,
                                     description, first_indexed_at,
                                     analyze_vendor)
           VALUES (?,?,?,?,?,?, datetime('now'), ?)
           ON CONFLICT(config_hash) DO UPDATE SET
               created_at = datetime('now'),
               description = excluded.description,
               compile_commands_path = excluded.compile_commands_path,
               embedding_dim = coalesce(excluded.embedding_dim, build_configs.embedding_dim),
               manifest_verification = excluded.manifest_verification,
               first_indexed_at = CASE WHEN build_configs.first_indexed_at = ''
                                       THEN datetime('now')
                                       ELSE build_configs.first_indexed_at
                                  END,
               analyze_vendor = excluded.analyze_vendor""",
        (config_hash, project_id, compile_commands_path, embedding_dim, manifest_verification, description, analyze_vendor),
    )


def get_active_config(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row | None:
    """Return the most recently indexed build_config for a project.

    Builds with ``manifest_verification = 'indexing'`` are excluded —
    they are still being indexed and their data is incomplete.
    MCP queries fall back to the previous completed build.
    """
    return conn.execute(
        """SELECT * FROM build_configs WHERE project_id=?
           AND (manifest_verification IS NULL OR manifest_verification != 'indexing')
           ORDER BY created_at DESC, rowid DESC LIMIT 1""",
        (project_id,),
    ).fetchone()


def get_all_projects(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return all projects with their latest *completed* build_config stats.

    Builds with ``manifest_verification = 'indexing'`` are excluded so that
    an in-progress index never appears as the latest project state.
    """
    return conn.execute(
        """SELECT p.project_id, p.name, p.root_path,
                  b.config_hash, b.created_at, b.compile_commands_path,
                  b.description, b.first_indexed_at,
                  COUNT(DISTINCT s.id) AS symbol_count,
                  COUNT(DISTINCT f.id) AS file_count
           FROM projects p
           LEFT JOIN build_configs b ON b.project_id = p.project_id
               AND b.rowid = (
                   SELECT rowid FROM build_configs
                   WHERE project_id = p.project_id
                     AND (manifest_verification IS NULL OR manifest_verification != 'indexing')
                   ORDER BY created_at DESC, rowid DESC
                   LIMIT 1
               )
           LEFT JOIN symbols s ON s.config_hash = b.config_hash
           LEFT JOIN files f ON f.config_hash = b.config_hash
           GROUP BY p.project_id""",
    ).fetchall()


def get_all_builds_for_project(conn: sqlite3.Connection, project_id: str) -> list[sqlite3.Row]:
    """Return all build configs for a project with per-build stats.

    Ordered by ``created_at`` descending (most recent first).  Each row
    includes aggregated symbol, file, and reference counts for that build.
    """
    return conn.execute(
        """SELECT b.config_hash, b.created_at, b.first_indexed_at,
                  b.compile_commands_path, b.embedding_dim,
                  b.manifest_verification, b.description,
                  COALESCE(s.sym_count, 0) AS symbol_count,
                  COALESCE(f.file_count, 0) AS file_count,
                  COALESCE(r.ref_count, 0) AS reference_count
           FROM build_configs b
           LEFT JOIN (
               SELECT config_hash, COUNT(*) AS sym_count FROM symbols GROUP BY config_hash
           ) s ON s.config_hash = b.config_hash
           LEFT JOIN (
               SELECT config_hash, COUNT(*) AS file_count FROM files GROUP BY config_hash
           ) f ON f.config_hash = b.config_hash
           LEFT JOIN (
               SELECT config_hash, COUNT(*) AS ref_count FROM refs GROUP BY config_hash
           ) r ON r.config_hash = b.config_hash
           WHERE b.project_id = ?
           ORDER BY b.created_at DESC, b.rowid DESC""",
        (project_id,),
    ).fetchall()


def get_build_stats(conn: sqlite3.Connection, config_hash: str) -> dict:
    """Return detailed statistics for a single build config.

    Returns a dict with symbol counts by kind, file/ref/macro counts,
    LLM analysis and embedding coverage, override and hotspot counts.
    When *config_hash* is not found, returns ``{"error": "..."}``.
    """
    config = conn.execute(
        "SELECT * FROM build_configs WHERE config_hash = ?", (config_hash,)
    ).fetchone()
    if config is None:
        return {"error": f"config_hash not found: {config_hash[:12]}..."}

    # Symbol counts by kind
    by_kind_rows = conn.execute(
        "SELECT kind, COUNT(*) AS cnt FROM symbols WHERE config_hash = ? GROUP BY kind ORDER BY cnt DESC",
        (config_hash,),
    ).fetchall()

    sym_defs = conn.execute(
        "SELECT COUNT(*) FROM symbols WHERE config_hash = ? AND is_definition = 1",
        (config_hash,),
    ).fetchone()[0]

    sym_total = sum(r["cnt"] for r in by_kind_rows)
    files = conn.execute("SELECT COUNT(*) FROM files WHERE config_hash = ?", (config_hash,)).fetchone()[0]
    refs = conn.execute("SELECT COUNT(*) FROM refs WHERE config_hash = ?", (config_hash,)).fetchone()[0]
    macros = conn.execute("SELECT COUNT(*) FROM macros WHERE config_hash = ?", (config_hash,)).fetchone()[0]
    overrides = conn.execute("SELECT COUNT(*) FROM overrides WHERE config_hash = ?", (config_hash,)).fetchone()[0]
    hotspot = conn.execute("SELECT COUNT(*) FROM hotspot_cache WHERE config_hash = ?", (config_hash,)).fetchone()[0]
    pagerank = conn.execute(
        "SELECT COUNT(*) FROM symbols WHERE config_hash = ? AND pagerank > 0",
        (config_hash,),
    ).fetchone()[0]

    analyzed = conn.execute(
        """SELECT COUNT(*) FROM llm_analysis a
           JOIN symbols s ON s.id = a.symbol_id
           WHERE s.config_hash = ?
             AND a.model NOT LIKE 'skip:%'""",
        (config_hash,),
    ).fetchone()[0]

    embeddings = conn.execute(
        """SELECT COUNT(DISTINCT e.symbol_id) FROM embeddings e
           JOIN symbols s ON s.id = e.symbol_id
           WHERE s.config_hash = ?""",
        (config_hash,),
    ).fetchone()[0]

    indirect_sites = conn.execute(
        "SELECT COUNT(*) FROM indirect_call_sites WHERE config_hash = ?", (config_hash,)
    ).fetchone()[0]
    fp_assignments = conn.execute(
        "SELECT COUNT(*) FROM fp_assignments WHERE config_hash = ?", (config_hash,)
    ).fetchone()[0]

    result: dict = dict(config)
    result["by_kind"] = {r["kind"]: r["cnt"] for r in by_kind_rows}
    result["symbol_total"] = sym_total
    result["symbol_definitions"] = sym_defs
    result["analyzed_symbols"] = analyzed
    result["unanalyzed_definitions"] = max(0, sym_defs - analyzed)
    result["file_count"] = files
    result["reference_count"] = refs
    result["macro_count"] = macros
    result["override_count"] = overrides
    result["hotspot_cache_entries"] = hotspot
    result["pagerank_coverage"] = pagerank
    result["embedding_count"] = embeddings
    result["indirect_call_sites"] = indirect_sites
    result["fp_assignments"] = fp_assignments

    return result
