"""Project and build config management for the fw-context-mcp index.

Manages ``projects`` and ``build_configs`` tables.  Each project gets a
deterministic UUID4 (hashed from root path), and each build gets a
content-addressable hash of the compile_commands.json.

WHY deterministic project ID: the project ID must be stable across
machines and reindexes so that MCP tool calls from different directories
resolve to the same project.  Hash-based derivation from the resolved
root path ensures consistency without storing state globally.

WHY content-addressable build hash: the compile_commands.json is hashed
to produce the ``config_hash``.  When the same file is indexed twice
(with no changes), the hash matches and the existing build is updated
in-place (idempotent).  When compiler flags change, a new build is
created — old builds remain queryable until explicitly deleted.
"""

from __future__ import annotations

import sqlite3

__all__ = [
    "ANALYZABLE_KINDS",
    "compute_analysis_coverage",
    "count_pending_analysis",
    "delete_build_data",
    "get_active_config",
    "get_all_builds_for_project",
    "get_all_projects",
    "get_build_stats",
    "get_builds_for_scope",
    "make_analysis_summary",
    "upsert_build_config",
    "upsert_project",
]

# Kinds the LLM analysis pipeline processes.  All three consumers import
# this tuple, thus the selection query, the coverage report, and the
# staleness check can never drift apart again:
#   1. ``_llm_analysis._select_unanalyzed_symbols`` — selects the work.
#   2. ``compute_analysis_coverage`` — reports the coverage.
#   3. ``count_pending_analysis`` — the staleness signal that
#      ``background._fast_staleness_check`` uses.
# A kind that is in the selection query but not here made the daemon miss
# the analysis work for that kind.
ANALYZABLE_KINDS: tuple[str, ...] = (
    "function",
    "method",
    "constructor",
    "destructor",
    "class",
    "struct",
    "union",
    "typedef",
    "enum",
    "varglobal",
)


def compute_analysis_coverage(conn: sqlite3.Connection, config_hash: str) -> dict:
    """Return LLM-analysis coverage split by project vs vendor symbols.

    Counts definition symbols (``is_definition=1``) of the kinds the
    analysis pipeline processes (``ANALYZABLE_KINDS``), excluding
    anonymous/unnamed symbols.  The split uses the ``is_project`` column so
    callers can tell intentionally-skipped vendor symbols apart from project
    symbols that genuinely still need analysis.

    Each side reports three counts, and ``llm_analysis`` holds at most one
    row per symbol (``symbol_id`` is the primary key), thus the counts never
    overlap:

    * ``analyzed`` — symbols with a real analysis row.
    * ``skipped`` — symbols with a ``skip:*`` sentinel row.  The pipeline
      tried these symbols and cannot analyze them (the body is larger than
      the model context, the model gave an unparseable answer, or the body
      was not readable at all).
    * ``total`` — all symbols of the analyzable kinds.

    ``total - analyzed - skipped`` gives the symbols that the pipeline can
    still process.  Use ``count_pending_analysis`` for that value — do not
    compute it again in the caller.
    """
    placeholders = ", ".join("?" * len(ANALYZABLE_KINDS))
    rows = conn.execute(
        f"""SELECT s.is_project,
                   SUM(CASE WHEN a.symbol_id IS NOT NULL
                                AND a.model NOT LIKE 'skip:%'
                            THEN 1 ELSE 0 END) AS analyzed,
                   SUM(CASE WHEN a.model LIKE 'skip:%'
                            THEN 1 ELSE 0 END) AS skipped,
                   COUNT(*) AS total
            FROM symbols s
            LEFT JOIN llm_analysis a ON a.symbol_id = s.id
            WHERE s.config_hash = ?
              AND s.is_definition = 1
              AND s.kind IN ({placeholders})
              AND s.name NOT LIKE '%(anonymous%'
              AND s.name NOT LIKE '%(unnamed%'
            GROUP BY s.is_project""",
        (config_hash, *ANALYZABLE_KINDS),
    ).fetchall()

    coverage = {
        "project": {"analyzed": 0, "skipped": 0, "total": 0},
        "vendor": {"analyzed": 0, "skipped": 0, "total": 0},
    }
    for row in rows:
        key = "project" if row["is_project"] else "vendor"
        coverage[key]["analyzed"] = row["analyzed"] or 0
        coverage[key]["skipped"] = row["skipped"] or 0
        coverage[key]["total"] = row["total"] or 0
    return coverage


def count_pending_analysis(
    conn: sqlite3.Connection,
    config_hash: str,
    *,
    analyze_vendor: bool,
) -> int:
    """Count the symbols that the LLM-analysis pipeline can still process.

    This is the staleness signal: a value larger than 0 means that a
    background reindex has analysis work to do.  It is the counterpart of
    ``compute_analysis_coverage``, and it uses the same query, thus the
    coverage report and the staleness check can never disagree.

    A symbol with a ``skip:*`` sentinel counts as done.  The pipeline
    already tried it and cannot analyze it, thus a pending count that
    included these symbols would start a background reindex again after
    every run.

    When *analyze_vendor* is False, only project symbols count.  Pass the
    value from the CONFIG, not the value stored in ``build_configs`` — this
    count must predict what the next background reindex does.
    """
    coverage = compute_analysis_coverage(conn, config_hash)
    sides = ("project", "vendor") if analyze_vendor else ("project",)
    return sum(
        max(0, coverage[side]["total"] - coverage[side]["analyzed"] - coverage[side]["skipped"])
        for side in sides
    )


def make_analysis_summary(
    coverage: dict,
    analyze_vendor: bool,
    model: str | None,
) -> dict:
    """Shape ``compute_analysis_coverage`` output into the ``analysis`` status.

    ``complete`` is True when the pipeline has no more work: every project
    symbol is analyzed or skipped and, when vendor analysis is enabled,
    every vendor symbol too.  Two states never block completeness, because
    the two are expected, not deficient:

    * Vendor symbols that ``analyze_vendor=False`` excludes.
    * Symbols with a ``skip:*`` sentinel, which the pipeline cannot
      analyze.  ``skipped`` keeps these symbols visible in the counts.

    Thus ``complete`` agrees with ``count_pending_analysis``:
    ``complete`` is True exactly when the pending count is 0.
    """
    project_pending = (
        coverage["project"]["total"]
        - coverage["project"]["analyzed"]
        - coverage["project"]["skipped"]
    )
    vendor_pending = (
        coverage["vendor"]["total"]
        - coverage["vendor"]["analyzed"]
        - coverage["vendor"]["skipped"]
    )
    return {
        "model": model,
        "analyze_vendor": analyze_vendor,
        "project": coverage["project"],
        "vendor": coverage["vendor"],
        "complete": project_pending <= 0 and (vendor_pending <= 0 or not analyze_vendor),
    }


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
    conn.execute("DELETE FROM memory_regions WHERE config_hash = ?", (config_hash,))
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
    variant: str = "",
    image: str = "",
    board: str = "",
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
        variant: Build variant name (``''`` for single-project builds).
        image: Sysbuild image name (``''`` for non-sysbuild builds).
        board: Concrete board string per-(variant, image) — captures per-image
            board overrides (e.g. FLPR ``cpuflpr`` vs ``cpuapp``).

    Returns:
        None.
    """

    # Columns guaranteed by open_db() → _ensure_migrated_columns()
    conn.execute(
        """INSERT INTO build_configs(config_hash, project_id, compile_commands_path,
                                     embedding_dim, manifest_verification,
                                     description, first_indexed_at,
                                     analyze_vendor, variant, image, board)
           VALUES (?,?,?,?,?,?, datetime('now'), ?, ?, ?, ?)
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
               analyze_vendor = excluded.analyze_vendor,
               variant = excluded.variant,
               image = excluded.image,
               board = excluded.board""",
        (config_hash, project_id, compile_commands_path, embedding_dim, manifest_verification, description, analyze_vendor, variant, image, board),
    )


def get_active_config(
    conn: sqlite3.Connection,
    project_id: str,
    variant: str = "",
    image: str = "",
) -> sqlite3.Row | None:
    """Return the most recently indexed build_config for a project.

    Builds with ``manifest_verification = 'indexing'`` are excluded —
    they are still being indexed and their data is incomplete.
    MCP queries fall back to the previous completed build.

    *variant*/*image* optionally narrow the lookup to one build — when
    both are empty, the newest build overall is returned (single-project
    default, unchanged).
    """
    if variant and image:
        return conn.execute(
            """SELECT * FROM build_configs WHERE project_id=?
               AND variant=? AND image=?
               AND (manifest_verification IS NULL OR manifest_verification != 'indexing')
               ORDER BY created_at DESC, rowid DESC LIMIT 1""",
            (project_id, variant, image),
        ).fetchone()
    if variant:
        return conn.execute(
            """SELECT * FROM build_configs WHERE project_id=?
               AND variant=?
               AND (manifest_verification IS NULL OR manifest_verification != 'indexing')
               ORDER BY created_at DESC, rowid DESC LIMIT 1""",
            (project_id, variant),
        ).fetchone()
    return conn.execute(
        """SELECT * FROM build_configs WHERE project_id=?
           AND (manifest_verification IS NULL OR manifest_verification != 'indexing')
           ORDER BY created_at DESC, rowid DESC LIMIT 1""",
        (project_id,),
    ).fetchone()


def get_builds_for_scope(
    conn: sqlite3.Connection,
    project_id: str,
    variant: str = "",
    image: str = "",
) -> list[sqlite3.Row]:
    """Return completed build_configs rows matching a ``(variant, image)`` scope.

    Selection semantics (mirrors the MCP ``variant``/``image`` protocol):

    - ``variant="*"`` → all variants (all builds), newest first.
    - ``variant`` set, ``image=""`` → every image of that variant.
    - ``variant`` set, ``image`` set → that one (variant, image) build.
    - both empty → the newest single build (single-project default).

    Completed builds only (``manifest_verification != 'indexing'``).
    """
    where = "project_id = ? AND (manifest_verification IS NULL OR manifest_verification != 'indexing')"
    params: list[object] = [project_id]
    if variant and variant != "*":
        where += " AND variant = ?"
        params.append(variant)
        if image:
            where += " AND image = ?"
            params.append(image)
    return conn.execute(
        f"SELECT * FROM build_configs WHERE {where} "
        "ORDER BY created_at DESC, rowid DESC",
        params,
    ).fetchall()


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
                  b.variant, b.image, b.board,
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

    coverage = compute_analysis_coverage(conn, config_hash)
    # Exclude the skip:* sentinels (skip:toolarge, skip:unparseable) —
    # they are not model names and must never surface as analysis.model.
    model_row = conn.execute(
        """SELECT a.model FROM llm_analysis a
           JOIN symbols s ON s.id = a.symbol_id
           WHERE s.config_hash = ?
             AND a.model NOT LIKE 'skip:%' LIMIT 1""",
        (config_hash,),
    ).fetchone()
    model = model_row["model"] if model_row else None

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
    result["analysis"] = make_analysis_summary(
        coverage,
        bool(result.get("analyze_vendor")),
        model,
    )
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
