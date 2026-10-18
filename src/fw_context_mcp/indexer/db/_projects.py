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

from ._schema import _ensure_column

__all__ = [
    "ANALYZABLE_KINDS",
    "EMBEDDABLE_KINDS",
    "compute_analysis_coverage",
    "delete_build_data",
    "get_active_config",
    "get_all_builds_for_project",
    "get_all_projects",
    "get_build_stats",
    "get_builds_for_scope",
    "make_analysis_summary",
    "set_analyze_vendor",
    "symbols_without_embedding",
    "upsert_build_config",
    "upsert_project",
]

# Kinds the LLM analysis pipeline processes.  The two consumers import
# this tuple, thus the selection query and the coverage report always use
# the same kinds:
#   1. ``_llm_analysis._select_unanalyzed_symbols`` — selects the work.
#   2. ``compute_analysis_coverage`` — reports the coverage.
# If a kind is in the selection query but not in this tuple, the coverage
# report does not count the analysis work for that kind.
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

# Kinds of definition that get an embedding.  The two consumers import this
# tuple, thus the work and the check of the work always use the same kinds:
#   1. ``_embedding._build_embeddings`` — selects the symbols to embed.
#   2. ``symbols_without_embedding`` — finds the definitions that have none.
# A kind that only one of them reads gives a gap that nobody reports, or a
# report of a gap that no run can close.
EMBEDDABLE_KINDS: tuple[str, ...] = (
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
    still process.  ``make_analysis_summary`` uses that value.
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


def symbols_without_embedding(conn: sqlite3.Connection, config_hash: str) -> list[int]:
    """Return the ids of the definitions of the build that have no embedding.

    The definitions are the ones that ``_build_embeddings`` embeds: kinds of
    ``EMBEDDABLE_KINDS`` with ``is_definition = 1``.  A definition without a
    row of chunk 0 has no embedding.  The row of chunk 0 is also the row
    that ``_build_embeddings`` reads for its content hash.

    WHY a gap can occur: a re-parse of one file deletes the symbols of its
    translation unit, also the symbols of the headers that it includes, and
    inserts them again with new ids.  ``ON DELETE CASCADE`` removes their
    embeddings.  Measured on a project of 7 builds with 4 300 to 8 400
    definitions each: 36 ms for the 7 builds together.
    """
    placeholders = ", ".join("?" * len(EMBEDDABLE_KINDS))
    rows = conn.execute(
        f"""SELECT s.id FROM symbols s
            WHERE s.config_hash = ?
              AND s.is_definition = 1
              AND s.kind IN ({placeholders})
              AND NOT EXISTS (SELECT 1 FROM embeddings e
                              WHERE e.symbol_id = s.id AND e.chunk_index = 0)
            ORDER BY s.id""",
        (config_hash, *EMBEDDABLE_KINDS),
    ).fetchall()
    return [row[0] for row in rows]


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

    Thus ``complete`` is True exactly when ``total - analyzed - skipped``
    is 0 or less for each side that the pipeline processes.
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
    description: str | None = None,
    analyze_vendor: int = 0,
    variant: str | None = None,
    image: str | None = None,
    board: str | None = None,
    row_format: str | None = None,
    git_head: str | None = None,
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
        manifest_verification: ``"full"``, ``"partial"`` or ``"none"`` —
            ``"none"`` when manifest.json was not available during indexing,
            ``"partial"`` when it was and the embeddings are not complete.
        description: Human-readable build description (git branch + tag).
            ``None`` keeps whatever the row already holds.

            WHY None exists: this description says which branch the CONTENT
            of the index came from, and a run stamps the row twice — once
            before the translation units and once after.  The first write
            used to overwrite it with the current git context, so a run
            that FAILED left the new branch recorded over the old content.
            Anything comparing the two to notice a branch switch would then
            see agreement that is not there.  ``runner.run`` passes None on
            the first write for that reason; ``_postprocess`` passes the
            real value on the last one, when the content matches it.
        variant: Build variant name (``''`` for single-project builds).
            ``None`` keeps whatever the row already holds, and it is the
            default.  ``''`` is a value and not a way of saying "keep": it
            is what a single-project build stores.

            WHY None is the default: the UPDATE arm assigns this column from
            ``excluded``, thus a caller that omits it writes the default over
            the stored value.  ``cmd_analyze`` omitted this column and the
            two below, because it only updates ``analyze_vendor`` on an index
            that another run built (now `set_analyze_vendor`).  It erased the
            three, and
            ``get_active_config`` then matched no row for a build that names
            a variant — every tool called with an explicit variant failed on
            an index that is fully present.
        image: The image name, the program of a build of several programs
            (``''`` for a build of one program).
            ``None`` keeps the stored value, as with ``variant``.
        row_format: Which meaning the stored text of this build carries —
            pass ``CURRENT_ROW_FORMAT``.  ``None`` keeps whatever the row
            already holds, and it is the default.

            WHY None is the default: only the step that WROTE the text may
            say what the text means.  ``_run_postprocess`` is that step, and
            it runs after the last translation unit.  Two callers must not
            stamp: ``runner.run`` writes this row before it reads a single
            unit, thus a run that fails would leave the new format over the
            old rows; ``cmd_analyze`` called it to update only
            ``analyze_vendor`` on an index that another version built (now
            `set_analyze_vendor`).  A missed stamp costs one
            reindex that was not needed — a wrong stamp costs silence over
            an index that answers with dead code.
        board: Concrete board string per-(variant, image) — captures per-image
            board overrides.  ``None`` keeps the stored value, as with
            ``variant``.
        git_head: The text of the file HEAD of the tree that the content
            came from (``git_context.read_head``).  ``None`` keeps the
            stored value, for the reason that ``description`` gives.

            WHY the column is outside the fingerprint of the schema
            (``_ensure_column``, not ``_MIGRATION_ADD_COLUMNS``): a new
            column in the fingerprint reports each existing index as an
            index of an older schema, and asks for a full reindex of each
            project.  An index without the value compares the branch of
            ``description``, as before, until its next run
            (``git_context.head_moved``).

    Returns:
        None.
    """

    # Five columns take None for "keep the value that the row holds".  Read
    # them rather than express that in SQL: each column is NOT NULL, thus a
    # NULL cannot travel through the INSERT arm to a coalesce in the UPDATE
    # arm.  One row by primary key answers for all five, because the UPDATE
    # arm assigns every column from `excluded` and a caller that omits one
    # would otherwise write the default over the stored value.
    keep = {
        "description": description,
        "row_format": row_format,
        "variant": variant,
        "image": image,
        "board": board,
    }
    if any(value is None for value in keep.values()):
        old = conn.execute(
            """SELECT description, row_format, variant, image, board
               FROM build_configs WHERE config_hash = ?""",
            (config_hash,),
        ).fetchone()
        for column, value in keep.items():
            if value is None:
                # An absent row gives ''.  For `row_format` that reports the
                # build as an older format until the postprocess step stamps
                # it, which is the safe direction.
                keep[column] = str(old[column]) if old is not None else ""
    description = keep["description"]
    row_format = keep["row_format"]
    variant = keep["variant"]
    image = keep["image"]
    board = keep["board"]

    # Columns guaranteed by open_db() → _ensure_migrated_columns()
    conn.execute(
        """INSERT INTO build_configs(config_hash, project_id, compile_commands_path,
                                     embedding_dim, manifest_verification,
                                     description, first_indexed_at,
                                     analyze_vendor, variant, image, board,
                                     row_format)
           VALUES (?,?,?,?,?,?, datetime('now'), ?, ?, ?, ?, ?)
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
               board = excluded.board,
               row_format = excluded.row_format""",
        (config_hash, project_id, compile_commands_path, embedding_dim, manifest_verification, description, analyze_vendor, variant, image, board, row_format),
    )
    if git_head is not None:
        _ensure_column(conn, "build_configs", "git_head", "TEXT")
        conn.execute(
            "UPDATE build_configs SET git_head = ? WHERE config_hash = ?",
            (git_head, config_hash),
        )


def set_analyze_vendor(conn: sqlite3.Connection, config_hash: str, analyze_vendor: bool) -> None:
    """Record whether the LLM analysis of the build *config_hash* covers vendor code.

    WHY not `upsert_build_config`: its UPDATE arm sets ``created_at`` to
    the time of the call.  ``fw-context analyze`` writes no symbol, thus
    that would make an old index look new: the analysed build would become
    the newest build, and a compile_commands.json newer than the index run
    would no longer look newer.  This function changes the one column that
    the analysis changes.
    """
    conn.execute(
        "UPDATE build_configs SET analyze_vendor = ? WHERE config_hash = ?",
        (int(analyze_vendor), config_hash),
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

    Selection semantics:

    - ``variant="*"`` → all variants (all builds), newest first.
    - ``variant`` set, ``image=""`` → every image of that variant.
    - ``variant`` set, ``image`` set → that one (variant, image) build.
    - ``variant=""`` → every build of the project, newest first.

    These are the semantics of THIS query and not of the MCP protocol, which
    is narrower: ``resolve_build`` answers only for the exact pair of one
    build, because one query about code answers for one build.  It reads
    every build here to LIST what a project holds, and then matches the
    pair of the caller.

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

    WHY the counts come from subqueries and not from a join.  This used to
    join ``symbols`` and ``files`` on one ``config_hash`` and count each
    side with ``COUNT(DISTINCT …)``.  The join produces one row per PAIR:
    on a firmware index of 61 322 symbols and 1 922 files that is 118
    million rows for two numbers, and the query took 21 s.  The same
    answer through two pre-aggregated subqueries takes 4,6 ms — measured
    on that index, same result (61322 / 1922).  ``list_projects`` calls
    this for every indexed project, thus the cost was paid once per
    project on every call.

    ``get_all_builds_for_project`` below counts the same way; the two
    stayed different only because nothing forced them together.
    """
    return conn.execute(
        """SELECT p.project_id, p.name, p.root_path,
                  b.config_hash, b.created_at, b.compile_commands_path,
                  b.description, b.first_indexed_at,
                  COALESCE(s.sym_count, 0) AS symbol_count,
                  COALESCE(f.file_count, 0) AS file_count
           FROM projects p
           LEFT JOIN build_configs b ON b.project_id = p.project_id
               AND b.rowid = (
                   SELECT rowid FROM build_configs
                   WHERE project_id = p.project_id
                     AND (manifest_verification IS NULL OR manifest_verification != 'indexing')
                   ORDER BY created_at DESC, rowid DESC
                   LIMIT 1
               )
           LEFT JOIN (
               SELECT config_hash, COUNT(*) AS sym_count FROM symbols GROUP BY config_hash
           ) s ON s.config_hash = b.config_hash
           LEFT JOIN (
               SELECT config_hash, COUNT(*) AS file_count FROM files GROUP BY config_hash
           ) f ON f.config_hash = b.config_hash""",
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
