"""Index runner: parse compile_commands.json, extract symbols, store to SQLite.

Uses ``indexer/ops.py`` for the shared "parse TU → store symbols" loop so
that runner, reindex_file, and auto-reindex all use the same code path.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import threading
import time
from contextlib import nullcontext
from pathlib import Path

from ..config.settings import DESCRIPTION_VERSION, derive_project_id
from ..llm.ollama import call_ollama
from ..utils import MTIME_TOLERANCE_S, compute_source_hash, read_file_lines
from .compile_commands import _SOURCE_EXTS, validate_include_files
from .compile_commands import parse as parse_compile_commands
from .config_hash import compute_flags_hash, compute_tu_content_hash
from .db import (
    CURRENT_SCHEMA_VERSION,
    delete_build_data,
    drop_fts_triggers,
    get_file_hashes,
    open_db,
    rebuild_files_fts,
    rebuild_fts,
    rebuild_macros_fts,
    transaction,
    upsert_build_config,
    upsert_file,
    upsert_project,
    write_lock,
)
from .ops import _build_filtered_file_content, _normalize_file_path, store_symbols_for_unit
from ._manifest_updater import _refresh_header_mtimes_from_manifest, _update_manifest_after_index
from ._embedding import _build_embeddings, _chunk_body, _cleanup_orphaned_cc_artifacts, _embed_model_key, _fmt_dur, _truncate_body
from ._llm_analysis import _build_llm_analysis, _enrich_batch, _fetch_callees, _fetch_referencers, _read_body
from ._unit_processor import _check_and_parse_unit, _get_manifest_entry_hash_for_unit, _process_unit, _reassign_symbols_for_file

log = logging.getLogger(__name__)



# ═══════════════════════════════════════════════════════════════
# SECTION: Embedding building (→ llm_analysis.py)
# ═══════════════════════════════════════════════════════════════





def _extract_param_types(signature: str) -> str:
    """Extract the parameter type list from a C/C++ function signature.

    Strips parameter names, default values, and whitespace to produce a
    normalized string suitable for override comparison.

    Examples:
        "int read(char *buf, size_t len)" → "char *,size_t"
        "void write(const uint8_t *data, size_t len)" → "const uint8_t *,size_t"
        "void reset()" → ""
        "void set(int)" → "int"
    """
    # Find the outermost parentheses
    paren_start = signature.find("(")
    if paren_start == -1:
        return ""
    paren_depth = 0
    paren_end = paren_start
    for i in range(paren_start, len(signature)):
        if signature[i] == "(":
            paren_depth += 1
        elif signature[i] == ")":
            paren_depth -= 1
            if paren_depth == 0:
                paren_end = i
                break
    params_str = signature[paren_start + 1 : paren_end].strip()
    if not params_str:
        return ""
    # In C++, foo(void) and foo() are semantically identical — normalize both to empty
    if params_str == "void":
        return ""

    # Split by top-level commas, strip parameter names (keep only types)
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in params_str:
        if ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            if ch in "<({[":
                depth += 1
            elif ch in ">)}]":
                depth -= 1
            current.append(ch)
    if current:
        parts.append("".join(current).strip())

    # For each parameter, extract the type by removing the parameter name
    # and any trailing default value
    normalized: list[str] = []
    for param in parts:
        param = param.strip()
        if not param:
            continue
        # Remove default value (everything after '=')
        eq_idx = param.find("=")
        if eq_idx != -1:
            param = param[:eq_idx].strip()
        # Remove parameter name from the end.
        # The parameter name is the last identifier — it may be preceded by
        # pointer/reference markers (*, &, &&) that belong to the type.
        tokens = param.split()
        if len(tokens) >= 2:
            last = tokens[-1]
            # Strip leading pointer/reference markers from the last token
            stripped = last.lstrip("*&")
            # If what remains is a pure identifier (alphanumeric + underscores),
            # it's the parameter name — remove it, keeping any pointer/ref prefix
            # on the type.  But C++ type qualifiers (const, volatile, etc.) are
            # NOT parameter names — keep them.
            _CPP_TYPE_QUALIFIERS = frozenset({"const", "volatile", "constexpr", "noexcept"})
            if stripped and stripped.replace("_", "").isalnum() and stripped not in _CPP_TYPE_QUALIFIERS:
                ptr_prefix = last[: len(last) - len(stripped)]
                if ptr_prefix:
                    # Pointer/ref on the name token (e.g. "*buf") — move markers
                    # to the type by keeping them as a separate token
                    tokens[-1] = ptr_prefix
                else:
                    # Pure name — drop the last token
                    tokens = tokens[:-1]
        param = " ".join(tokens).strip()
        normalized.append(param)

    return ",".join(normalized)



# ═══════════════════════════════════════════════════════════════
# SECTION: Post-processing pipeline (→ postprocessor.py)
# ═══════════════════════════════════════════════════════════════

def _build_overrides(
    conn, config_hash: str, db_dir: Path, *, write_lock_held: bool = False, force: bool = False
) -> None:
    """Build the method override graph by matching virtual methods to their
    base-class counterparts through the inheritance chain.

    Pure post-processing — walks the inheritance graph already stored in
    the ``inheritance`` table and matches methods by name.  Parameter-type
    comparison provides a basic guard against accidental name collisions
    (overloads, not overrides).

    Set *force* to True to recompute even when overrides already exist
    (e.g. after incremental reindex).
    """
    from .db import insert_overrides_batch

    # Idempotency: if overrides were already built for this config, skip
    if not force:
        row = conn.execute("SELECT COUNT(*) FROM overrides WHERE config_hash = ?", (config_hash,)).fetchone()
        if row and row[0] > 0:
            log.info("Override graph already built (%d relationships) — nothing to do", row[0])
            return
    else:
        # Start from a clean slate — old overrides may reference removed
        # virtual methods or changed inheritance chains.
        conn.execute("DELETE FROM overrides WHERE config_hash = ?", (config_hash,))

    total = 0

    # Phase 1: collect all virtual/pure-virtual project methods with parent class info
    with transaction(conn):
        virtual_rows = conn.execute(
            """SELECT s.usr, s.name, s.qualified_name, s.signature,
                      s.parent_usr, s.kind
               FROM symbols s
               WHERE s.config_hash = ?
                 AND s.is_definition = 1
                 AND (s.is_virtual = 1 OR s.is_pure_virtual = 1)
                 AND s.kind IN ('method', 'destructor')
                 AND s.parent_usr != ''
               ORDER BY s.parent_usr, s.name""",
            (config_hash,),
        ).fetchall()

    if not virtual_rows:
        log.info("No virtual methods found — skipping override analysis")
        return

    # Phase 2: for each virtual method, walk the inheritance chain up and
    # find base-class methods with the same name.
    # Build parent→bases lookup cache for efficiency.
    parent_to_bases: dict[str, list[str]] = {}

    def _get_bases_recursive(parent_usr: str, visited: set | None = None) -> list[str]:
        """BFS up the inheritance chain — return all ancestor USRs."""
        if visited is None:
            visited = set()
        if parent_usr in parent_to_bases:
            return parent_to_bases[parent_usr]
        bases: list[str] = []
        queue = [parent_usr]
        while queue:
            cur = queue.pop(0)
            if cur in visited:
                continue
            visited.add(cur)
            rows = conn.execute(
                """SELECT base_usr FROM inheritance
                   WHERE config_hash = ? AND derived_usr = ?""",
                (config_hash, cur),
            ).fetchall()
            for r in rows:
                if r["base_usr"] not in visited:
                    bases.append(r["base_usr"])
                    queue.append(r["base_usr"])
        parent_to_bases[parent_usr] = bases
        return bases

    # Phase 3: resolve overrides
    override_rows: list[tuple[str, str, str]] = []
    skipped_no_base = 0
    skipped_no_match = 0

    for vrow in virtual_rows:
        base_usrs = _get_bases_recursive(vrow["parent_usr"])
        if not base_usrs:
            skipped_no_base += 1
            continue

        # Find virtual methods with the same name in any base class.
        # Only virtual/pure-virtual base methods can be overridden — non-virtual
        # methods with the same signature are *hidden*, not overridden.
        placeholders = ",".join("?" * len(base_usrs))
        base_methods = conn.execute(
            f"""SELECT usr, signature, parent_usr, qualified_name
                FROM symbols
                WHERE config_hash = ?
                  AND name = ?
                  AND kind IN ('method', 'destructor')
                  AND (is_virtual OR is_pure_virtual)
                  AND parent_usr IN ({placeholders})
                ORDER BY qualified_name""",
            (config_hash, vrow["name"], *base_usrs),
        ).fetchall()

        if not base_methods:
            skipped_no_match += 1
            continue

        # Compare parameter types to filter out accidental name collisions
        derived_params = _extract_param_types(vrow["signature"] or "")
        for bm in base_methods:
            base_params = _extract_param_types(bm["signature"] or "")
            if derived_params == base_params:
                override_rows.append((config_hash, vrow["usr"], bm["usr"]))

    if override_rows:
        with write_lock(db_dir, timeout=5.0) if not write_lock_held else nullcontext():
            with transaction(conn):
                insert_overrides_batch(conn, override_rows)
                total += len(override_rows)

    log.info(
        "Overrides stored: %d relationships (%d virtual, %d no-base, %d no-match)",
        total,
        len(virtual_rows),
        skipped_no_base,
        skipped_no_match,
    )


def _build_pagerank(conn, config_hash: str, *, write_lock_held: bool = False, force: bool = False) -> None:
    """Compute PageRank scores for function/method symbols from the call graph.

    Iterates until convergence (max 50 iterations, damping factor 0.85).
    Scores are normalized to 0.0–1.0 and stored in ``symbols.pagerank``.

    Idempotent — skips when pagerank already exists for this config.
    Set *force* to True to recompute even when pagerank data already exists
    (e.g. after incremental reindex).
    Requires the reference index (``fw-context index`` — refs on by default).
    """
    # Check already computed
    if not force:
        row = conn.execute(
            "SELECT COUNT(*) FROM symbols WHERE config_hash = ? AND pagerank > 0",
            (config_hash,),
        ).fetchone()
        if row and row[0] > 0:
            log.info("PageRank already computed — nothing to do")
            return

    edges = conn.execute(
        """SELECT DISTINCT r.from_usr, r.to_usr
           FROM refs r
           JOIN symbols fs ON fs.usr = r.from_usr AND fs.config_hash = r.config_hash
           JOIN symbols ts ON ts.usr = r.to_usr AND ts.config_hash = r.config_hash
           WHERE r.config_hash = ?
             AND r.ref_kind = 'call'
             AND fs.kind IN ('function', 'method', 'constructor', 'destructor')
             AND ts.kind IN ('function', 'method', 'constructor', 'destructor')
             AND r.from_usr != ''
             AND r.to_usr != ''
        """,
        (config_hash,),
    ).fetchall()

    outgoing: dict[str, list[str]] = {}
    incoming: dict[str, list[str]] = {}
    all_nodes: set[str] = set()

    for e in edges:
        frm, to = e["from_usr"], e["to_usr"]
        outgoing.setdefault(frm, []).append(to)
        incoming.setdefault(to, []).append(frm)
        all_nodes.add(frm)
        all_nodes.add(to)

    n = len(all_nodes)
    if n == 0:
        log.info("No call graph edges — skipping PageRank")
        return

    damping = 0.85
    scores: dict[str, float] = {node: 1.0 / n for node in all_nodes}

    for iteration in range(50):
        new_scores: dict[str, float] = {}
        for node in all_nodes:
            rank = (1 - damping) / n
            for caller in incoming.get(node, []):
                out_count = len(outgoing.get(caller, [1]))
                rank += damping * scores[caller] / out_count
            new_scores[node] = rank
        diff = sum(abs(new_scores[node] - scores[node]) for node in all_nodes)
        scores = new_scores
        if diff < 1e-6:
            log.info("PageRank converged after %d iterations", iteration + 1)
            break

    # Normalize to 0.0–1.0
    max_score = max(scores.values()) if scores else 1.0
    if max_score > 0:
        for node in scores:
            scores[node] /= max_score

    with transaction(conn):
        conn.executemany(
            "UPDATE symbols SET pagerank = ? WHERE config_hash = ? AND usr = ?",
            [(scores[usr], config_hash, usr) for usr in scores],
        )

    log.info("PageRank stored: %d nodes", n)


def _build_hotspot_cache(conn, config_hash: str, *, force: bool = False) -> None:
    """Pre-compute hotspot caller counts for instant ``find_hotspots`` queries.

    Idempotent — skips when cache already exists for this config.
    Set *force* to True to recompute even when cache data already exists
    (e.g. after incremental reindex).
    Requires the reference index.
    """
    if not force:
        row = conn.execute("SELECT COUNT(*) FROM hotspot_cache WHERE config_hash = ?", (config_hash,)).fetchone()
        if row and row[0] > 0:
            log.info("Hotspot cache already built — nothing to do")
            return

    with transaction(conn):
        if force:
            conn.execute("DELETE FROM hotspot_cache WHERE config_hash = ?", (config_hash,))
        conn.execute(
            """INSERT INTO hotspot_cache (config_hash, symbol_id, caller_count)
               SELECT r.config_hash, s.id, COUNT(r.rowid)
               FROM refs r
               JOIN symbols s ON s.usr = r.to_usr AND s.config_hash = r.config_hash
               WHERE r.config_hash = ?
                 AND s.is_definition = 1
                 AND r.ref_kind IN ('call', 'indirect')
               GROUP BY s.usr
            """,
            (config_hash,),
        )

    cnt = conn.execute("SELECT COUNT(*) FROM hotspot_cache WHERE config_hash = ?", (config_hash,)).fetchone()[0]
    log.info("Hotspot cache stored: %d entries", cnt)


# ── Content-hash helpers ────────────────────────────────────────────



# ═══════════════════════════════════════════════════════════════
# SECTION: Manifest management
# ═══════════════════════════════════════════════════════════════





def _run_postprocess(
    conn,
    config_hash: str,
    project_root: Path,
    db_dir: Path,
    units: list,
    tu_headers: dict,
    manifest: dict | None,
    compile_commands: Path,
    updated: int,
    build_dir_patterns: list[str] | None,
    vendor_patterns: list[str],
    project_patterns_list: list[str],
    project_id: str,
    git_description: str,
    *,
    index_refs: bool,
    index_embeddings: bool,
    index_macros_expanded: bool,
    analyze_symbols: bool,
    analyze_overrides: bool,
    analyze_vendor: bool,
    llm_config=None,
    cache_server_config=None,
    force: bool = False,
) -> None:
    """Post-processing phases: FTS5 rebuild, macros, embeddings, LLM, overrides, pagerank."""

    # ── FTS5 rebuild ──
    rebuild_fts(conn)
    rebuild_files_fts(conn)
    rebuild_macros_fts(conn)

    # ── Orphan cleanup ──
    from .db import delete_orphan_files

    delete_orphan_files(conn, config_hash)

    from .db import clean_orphan_embeddings, clean_orphan_embeddings_vec

    clean_orphan_embeddings(conn)
    clean_orphan_embeddings_vec(conn)

    # Clean orphaned LLM analysis (was O(n) per batch — now once, see _llm.py)
    conn.execute(
        "DELETE FROM llm_analysis WHERE symbol_id NOT IN (SELECT id FROM symbols)"
    )

    # ── is_project alignment ──
    for pp in project_patterns_list:
        fn_pp = pp.replace("%", "*")
        conn.execute(
            """UPDATE files SET is_project = 1
               WHERE config_hash = ? AND is_project = 0 AND path GLOB ?""",
            (config_hash, fn_pp),
        )
    external_guard = "AND path NOT LIKE '/%'"
    if vendor_patterns:
        sql_vendor_patterns = [p.replace("_", "\\_") for p in vendor_patterns]
        not_like_clauses = " AND ".join(
            ["path NOT LIKE ? ESCAPE '\\'"] * len(vendor_patterns)
        )
        conn.execute(
            f"""UPDATE files SET is_project = 1
                WHERE config_hash = ? AND is_project = 0
                  AND ({not_like_clauses}) {external_guard}""",
            [config_hash] + sql_vendor_patterns,
        )
    else:
        conn.execute(
            f"""UPDATE files SET is_project = 1
               WHERE config_hash = ? AND is_project = 0
                 {external_guard}""",
            [config_hash],
        )
    conn.commit()

    # ── Manifest update ──
    updated_manifest = _update_manifest_after_index(
        manifest=manifest,
        units=units,
        project_root=project_root,
        db_dir=db_dir,
        compile_commands=compile_commands,
        updated_count=updated,
        tu_headers=tu_headers if tu_headers else None,
        build_dir_patterns=build_dir_patterns,
        config_hash=config_hash,
    )
    if updated_manifest is not None:
        _refresh_header_mtimes_from_manifest(conn, config_hash, project_root, updated_manifest)

    # ── Macro expansion ──
    if index_macros_expanded and units:
        from .macros import resolve_and_update

        seen_flags: set[tuple] = set()
        for unit in units:
            flag_key = tuple(sorted(unit.clang_args))
            if flag_key in seen_flags:
                continue
            seen_flags.add(flag_key)
            try:
                resolve_and_update(
                    conn, config_hash, unit.clang_args, unit.file.resolve(),
                )
            except (ValueError, TypeError, RuntimeError, AttributeError, sqlite3.Error):
                pass  # libclang/SQLite fallback

    # ── Embeddings ──
    if index_embeddings and llm_config is not None and llm_config.enabled:
        if force:
            conn.execute(
                "DELETE FROM embeddings WHERE symbol_id IN (SELECT id FROM symbols WHERE config_hash = ?)",
                (config_hash,),
            )
            try:
                conn.execute(
                    "DELETE FROM vec_symbols WHERE symbol_id IN (SELECT id FROM symbols WHERE config_hash = ?)",
                    (config_hash,),
                )
            except sqlite3.OperationalError:
                pass
            conn.commit()
        _build_embeddings(conn, config_hash, llm_config, db_dir)
        conn.commit()

    # ── LLM analysis ──
    if analyze_symbols and llm_config is not None and llm_config.enabled:
        from ..cache_client import CacheClient

        cc = None
        if cache_server_config is not None and cache_server_config.url:
            try:
                cc = CacheClient(
                    url=cache_server_config.url,
                    token=cache_server_config.token,
                    force=cache_server_config.force,
                    batch_size=cache_server_config.batch_size,
                )
            except (ValueError, TypeError, RuntimeError, AttributeError, sqlite3.Error):
                pass  # libclang/SQLite fallback
        if force:
            conn.execute(
                "DELETE FROM llm_analysis WHERE symbol_id IN (SELECT id FROM symbols WHERE config_hash = ?)",
                (config_hash,),
            )
            conn.commit()
        _build_llm_analysis(
            conn, config_hash, llm_config, db_dir,
            project_only=not analyze_vendor,
            cache_client=cc,
            retry_unparseable=True,
        )
        if cc:
            cc.close()
        conn.commit()

    # ── Override graph ──
    if analyze_overrides:
        if force:
            conn.execute("DELETE FROM overrides WHERE config_hash = ?", (config_hash,))
            conn.commit()
        _build_overrides(conn, config_hash, db_dir)
        conn.commit()

    # ── PageRank + hotspot ──
    if index_refs:
        if force:
            conn.execute("UPDATE symbols SET pagerank = 0.0 WHERE config_hash = ?", (config_hash,))
            conn.execute("DELETE FROM hotspot_cache WHERE config_hash = ?", (config_hash,))
            conn.commit()
        _build_pagerank(conn, config_hash)
        conn.commit()
        _build_hotspot_cache(conn, config_hash)
        conn.commit()

    # ── Manifest verification upgrade ──
    manifest_path = db_dir / "manifest.json"
    manifest_verification: str = "full" if manifest_path.exists() else "none"
    with transaction(conn):
        upsert_build_config(
            conn, config_hash, project_id, str(compile_commands),
            description=git_description, manifest_verification=manifest_verification,
            analyze_vendor=int(analyze_vendor),
        )

    # ── Cleanup old build data ──
    old_hashes = conn.execute(
        """SELECT config_hash FROM build_configs
           WHERE project_id = ? AND config_hash != ?
           ORDER BY created_at DESC""",
        (project_id, config_hash),
    ).fetchall()
    if old_hashes:
        pause_file = db_dir / "reindex.pause"
        skip_cleanup = False
        if pause_file.exists():
            try:
                pause_pid = int(pause_file.read_text(encoding="utf-8").strip())
                try:
                    os.kill(pause_pid, 0)
                    skip_cleanup = True
                except OSError:
                    pause_file.unlink(missing_ok=True)
            except (OSError, ValueError):
                pause_file.unlink(missing_ok=True)
        if not skip_cleanup:
            for row in old_hashes:
                old_ch = row["config_hash"]
                with transaction(conn):
                    delete_build_data(conn, old_ch)
            cc_dir = Path.home() / ".fw-context" / "index" / project_id
            if cc_dir.exists():
                for row in old_hashes:
                    old_ch = row["config_hash"]
                    try:
                        (cc_dir / f"compile_commands.{old_ch}.json").unlink(missing_ok=True)
                    except OSError:
                        pass

    # ── WAL checkpoint + schema stamp ──
    try:
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    except (ValueError, TypeError, RuntimeError, AttributeError, sqlite3.Error):
        pass  # libclang/SQLite fallback
    conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")


def run(
    compile_commands: Path,
    db_path: Path,
    vendor_paths: list[str] | None = None,
    project_paths: list[str] | None = None,
    project_name: str | None = None,
    index_refs: bool = False,
    index_embeddings: bool = False,
    analyze_symbols: bool = False,
    analyze_overrides: bool = True,
    project_root: Path | None = None,
    project_id: str | None = None,
    llm_config=None,
    cache_server_config=None,
    parallel: bool = True,
    force: bool = False,
    index_macros_expanded: bool = True,
    config_header: str = "",
    build_dir_patterns: list[str] | None = None,
    analyze_vendor: bool = False,
) -> str:
    """Index a project: parse translation units, extract symbols, and store to SQLite.

    This is the main entry point for indexing a firmware project.  It reads
    ``compile_commands.json``, parses every translation unit with libclang,
    deduplicates symbols across files, and persists the result to the SQLite
    database at ``db_path``.  Optionally builds embeddings, LLM-based symbol
    analysis, file-level summaries, and the method override graph.

    All files from all included headers are indexed unconditionally.
    ``is_project`` is computed per-symbol from *vendor_paths* (auto-detected
    SDK dirs + user-configured) and *project_paths* (user-configured overrides).

    Args:
        compile_commands: Path to ``compile_commands.json`` (generated by
            ``bear``, ``compiledb``, or ``CMAKE_EXPORT_COMPILE_COMMANDS``).
        db_path: Path to the SQLite database file that will store the index.
        vendor_paths: Additional vendor/SDK directory patterns (additive to
            auto-detection).  Paths matching these get ``is_project=0``.
        project_paths: Manual project directory patterns that override
            auto-detection.  Paths matching these get ``is_project=1``.
            Use absolute paths for directories outside the project root.
        project_name: Human-readable name for the project (defaults to the
            directory name of ``project_root``).
        index_refs: When True, extract call-graph references (call, ref,
            member, and indirect edges) during AST traversal.
        index_embeddings: When True, generate vector embeddings for all
            definition symbols using Ollama after indexing.
        analyze_symbols: When True, generate structured LLM analysis
            (summary, inputs, outputs) for project symbols via Ollama.
        analyze_overrides: When True (default), build the method override
            graph by matching virtual methods across the inheritance chain.
            Runs entirely on the local index — no LLM needed.
        project_root: Root directory of the project.  Used to resolve relative
            paths and derive defaults.  Defaults to the parent of
            ``compile_commands``.
        project_id: Unique project identifier (auto-derived from
            ``project_root`` when not provided).
        llm_config: Configuration dataclass for Ollama connection (URL,
            model names, enabled flag).  Required when any ``index_*`` or
            ``analyze_*`` option is enabled.
        parallel: Deprecated — kept for backward compatibility.  All
            indexing is now sequential with per-TU write locks so manual
            operations (reindex_file) can interleave via the pause marker.

    Returns:
        The ``config_hash`` string — a content-addressable fingerprint of the
        ``compile_commands.json`` used for staleness detection.
    """
    if project_root is None:
        project_root = compile_commands.parent.resolve()
    else:
        project_root = project_root.resolve()

    # Prepare vendor/project patterns for is_project computation.
    # Normalize patterns without % wildcard to match subdirectories.
    from .sdk_detect import _build_sdk_excludes, _normalize_patterns

    vendor_patterns = list(_build_sdk_excludes(project_root))
    if vendor_paths:
        vendor_patterns.extend(_normalize_patterns(vendor_paths))
    project_patterns_list = _normalize_patterns(list(project_paths)) if project_paths else []

    if project_id is None:
        project_id = derive_project_id(project_root)
    name = project_name or project_root.name
    # Collect git context for build description (branch + last tag).
    from .git_context import get_git_description

    git_description = get_git_description(project_root)
    # Clean up compile_commands.<hash>.json artifacts left by a previous
    # interrupted run before computing a new config_hash.
    _cleanup_orphaned_cc_artifacts(db_path, project_id)
    # Parse compile_commands.json to discover translation units.  Must
    # happen before config_hash computation so the manifest can be built
    # from the actual TU list.
    units = list(parse_compile_commands(compile_commands))
    units = [u for u in units if u.file.suffix.lower() in _SOURCE_EXTS]
    log.info("TUs to index: %d", len(units))

    # Determine config_hash from manifest.json.  The manifest captures the
    # full structural build identity (files, directories, compiler flags) —
    # more comprehensive than hashing compile_commands.json alone.
    #
    # When a manifest exists, compute the expected structural hash from the
    # current units and compare.  If they match, reuse the stored hash so
    # _update_manifest_after_index() can do an incremental header update.
    # If they differ (compile_commands.json changed), rebuild the preliminary
    # manifest.  When no manifest exists yet (first index), build one.
    # Inject user-configured config header when the build system doesn't emit
    # -include flags (custom builds, legacy Makefiles, etc.).
    if config_header:
        ch = project_root / config_header
        if not ch.exists():
            raise RuntimeError(
                f"Configured config_header not found: {ch}\nCheck [index] config_header in .fw-context/config.toml"
            )
        ch_abs = ch.resolve()
        for unit in units:
            unit.clang_args.extend(("-include", str(ch_abs)))

    from .manifest import build_preliminary, compute_structural_hash
    from .manifest import load as load_manifest

    manifest = load_manifest(db_path.parent)
    expected_hash = compute_structural_hash(
        compile_commands,
        project_root,
        units,
        build_dir_patterns,
        project_id=project_id,
    )
    if manifest is not None and manifest.get("config_hash") == expected_hash:
        config_hash = manifest["config_hash"]
    else:
        config_hash = build_preliminary(
            compile_commands,
            db_path.parent,
            project_root,
            units,
            build_dir_patterns,
            project_id=project_id,
        )
        # Reload manifest from disk — build_preliminary may have overwritten
        # manifest.json.  The in-memory manifest must reflect what _update_manifest_after_index
        # will find on disk (degraded or fresh), so the early-return guard can detect
        # preliminary (empty source_hash) entries and fall through to regeneration.
        manifest = load_manifest(db_path.parent)

    # Heartbeat for background reindex watchdog.  When the subprocess is
    # stuck (deadlock / hung syscall), this daemon thread stops writing
    # and the watchdog kills the process.  Only active when the heartbeat
    # log path is passed via env var (background reindex).
    _hb_log = os.environ.get("FW_CONTEXT_HEARTBEAT_LOG")
    if _hb_log:
        _hb_stop = threading.Event()

        def _heartbeat() -> None:
            while not _hb_stop.wait(30.0):
                try:
                    with open(_hb_log, "a") as f:
                        f.write(f"{time.strftime('%H:%M:%S')} heartbeat\n")
                except (ValueError, TypeError, RuntimeError, AttributeError, sqlite3.Error):
                    pass  # libclang/SQLite fallback

        _hb_thread = threading.Thread(target=_heartbeat, daemon=True)
        _hb_thread.start()

    log.info("", extra={"phase": f"Indexing {name} ({config_hash[:12]})"})
    log.info("project=%s project_id=%s config_hash=%s", name, project_id, config_hash[:12])

    conn = open_db(db_path)
    # Determine initial manifest verification for this config.
    # New config_hash → 'indexing' (hidden from MCP queries until complete).
    # Existing config → preserve the previous value (in-place update).
    old_row = conn.execute(
        "SELECT manifest_verification FROM build_configs WHERE config_hash=?",
        (config_hash,),
    ).fetchone()
    if old_row is None:
        initial_manifest_verification = "indexing"
    else:
        initial_manifest_verification = old_row["manifest_verification"]

    # Resolve analyze_vendor: CLI flag takes precedence, config falls back.
    # Only meaningful when analyze_symbols is True — otherwise analysis doesn't
    # run and the flag is irrelevant (storing True would mislead get_active_build).
    if analyze_symbols:
        _analyze_vendor = analyze_vendor or (llm_config is not None and llm_config.analyze_vendor)
    else:
        _analyze_vendor = False

    # Serialize with any concurrent writer (background reindex, daemon)
    with write_lock(db_path.parent, timeout=120.0):
        with transaction(conn):
            upsert_project(conn, project_id, name, str(project_root))
            upsert_build_config(
                conn,
                config_hash,
                project_id,
                str(compile_commands),
                description=git_description,
                manifest_verification=initial_manifest_verification,
                analyze_vendor=int(_analyze_vendor),
            )

    # Validate that all -include/-imacros referenced files exist BEFORE
    # starting any libclang parsing.  A missing build-generated config
    # header (e.g. BUILD/.../mbed_config.h) would otherwise cause libclang
    # to fail for every TU that includes the SDK, producing a partial index
    # and wasting ~seconds per TU on doomed parse attempts.
    for unit in units:
        validate_include_files(unit.clang_args)

    existing_files = get_file_hashes(conn, config_hash)

    # Pre-build lookup dict for O(1) manifest entry access during Tier 2 checks.
    # *manifest* was loaded above (before config_hash computation) — reuse it.
    manifest_lookup: dict[str, dict] = {}
    if manifest is not None:
        for e in manifest.get("entries", []):
            manifest_lookup[e.get("file", "")] = e

    # Drop FTS5 content-sync triggers before bulk indexing — each symbol
    # INSERT/DELETE/UPDATE would otherwise pay per-row FTS index overhead
    # (~2× write I/O).  The FTS table is rebuilt from scratch in one pass
    # after all TUs are stored.
    drop_fts_triggers(conn)

    total_syms = 0
    total_refs = 0
    skipped = 0
    unchanged = 0
    reused = 0
    updated = 0
    acc_parse = 0.0
    acc_lock = 0.0
    acc_write = 0.0
    content_filled = 0
    # Collect headers during tokenization for incremental manifest update.
    # Maps file_path → list of {path, hash, generated} header dicts.
    tu_headers: dict[str, list[dict]] = {}
    t0 = time.monotonic()

    log.info("", extra={"phase": f"Parsing ({len(units)} TUs)"})

    def _wait_if_paused() -> None:
        """If a manual operation requested pause, wait until it finishes.

        The MCP server writes ``<pid>`` to ``reindex.pause`` before a manual
        ``reindex_file`` or ``reset_index``.  This function blocks until the
        pause is lifted or the requesting process dies (stale marker cleanup).

        When the current process wrote the marker itself (e.g. ``fw-context
        index --force`` was invoked from the CLI while a background reindex
        is running), the marker is skipped so the foreground process does not
        pause itself.
        """
        pause_file = db_path.parent / "reindex.pause"
        our_pid = os.getpid()
        # 1s polling — exits immediately when no pause file exists
        deadline = time.monotonic() + 300  # 5-min timeout
        while True:
            if time.monotonic() > deadline:
                log.warning("_wait_if_paused: timeout after 300s — resuming")
                return
            if not pause_file.exists():
                return
            try:
                content = pause_file.read_text(encoding="utf-8").strip()
                requester_pid = int(content)
            except (OSError, ValueError):
                try:
                    pause_file.unlink(missing_ok=True)
                except OSError:
                    pass
                return
            # Never pause on our own marker — this process created it
            # to signal the background reindex, not to block itself.
            if requester_pid == our_pid:
                return
            try:
                os.kill(requester_pid, 0)
            except OSError:
                # Process dead — clean up stale marker
                try:
                    pause_file.unlink(missing_ok=True)
                except OSError:
                    pass
                return
            time.sleep(1.0)  # Wait, then check again

    # Single sequential loop with per-TU write lock for responsiveness.
    # The lock is acquired and released for each translation unit so that
    # manual operations (reindex_file, reset_index) can interleave via
    # the pause marker mechanism instead of blocking for 60+ seconds.
    #
    # Libclang parsing runs OUTSIDE the write lock — a single TU can take
    # seconds to minutes on large codebases (mbed-os, Zephyr).  Holding the
    # lock during parsing starves other indexers (bg reindex, concurrent
    # ``fw-context index --force``) and causes WriteLockTimeout errors.
    for i, unit in enumerate(units):
        _wait_if_paused()  # Check pause marker before each TU
        fname = unit.file.name
        processed = i + 1

        # ── Phase 1: staleness check + libclang parse (no lock) ──
        check_status, parsed_data, parse_timing, hashes = _check_and_parse_unit(
            unit,
            config_hash,
            project_root,
            vendor_patterns,
            index_refs,
            existing_files,
            force=force,
            manifest=manifest_lookup,
        )

        if check_status in ("unchanged", "reuse"):
            is_reuse = check_status == "reuse"
            file_path_str = _normalize_file_path(str(unit.file.resolve()), project_root)
            rec = existing_files.get(file_path_str)
            file_id = rec.file_id if rec else None
            try:
                current_mtime = unit.file.stat().st_mtime if unit.file.exists() else 0.0
            except OSError:
                current_mtime = 0.0

            # When "reuse" migration produces 0 symbols (no old data for this
            # TU), we must fall through to Phase 2 for a real libclang parse
            # instead of skipping the TU permanently.
            fallthrough = False

            with write_lock(db_path.parent, timeout=120.0):
                with transaction(conn, checkpoint=False):
                    if hashes is not None:
                        # Tier 2 / Tier 2b: content-hash match — update or create file record
                        source_hash, flags_hash, manifest_entry_hash = hashes
                        content_hash_val = compute_tu_content_hash(source_hash, flags_hash, manifest_entry_hash)
                        if file_id is not None:
                            conn.execute(
                                """UPDATE files SET mtime=?, content_hash=?, source_hash=?,
                                   flags_hash=?
                                   WHERE id=?""",
                                (current_mtime, content_hash_val, source_hash, flags_hash, file_id),
                            )
                        else:
                            # New config_hash — create file record for this TU
                            file_id = upsert_file(
                                conn,
                                config_hash,
                                file_path_str,
                                unit.language,
                                mtime=current_mtime,
                                content_hash=content_hash_val,
                                source_hash=source_hash,
                                flags_hash=flags_hash,
                            )
                    elif file_id is not None:
                        # Tier 1: mtime match — just refresh stored mtime
                        conn.execute(
                            "UPDATE files SET mtime=? WHERE id=?",
                            (current_mtime, file_id),
                        )
                    # For "reuse": copy symbols + refs from old config_hash
                    if is_reuse and file_id is not None:
                        syms_copied = _reassign_symbols_for_file(
                            conn, config_hash, file_id, file_path_str,
                        )
                        if syms_copied > 0:
                            total_syms += syms_copied
                            reused += 1
                        else:
                            # No old data to migrate — clear the file record
                            # so orphan cleanup handles it, then fall through
                            # to Phase 2 for a real libclang parse.
                            log.info(
                                "[%d/%d] %s: reuse produced 0 symbols — re-parsing",
                                processed, len(units), fname,
                            )
                            conn.execute("UPDATE files SET content = '' WHERE id = ?", (file_id,))
                            fallthrough = True
                    elif not is_reuse:
                        unchanged += 1

                    if not fallthrough:
                        # Fill ifdef-filtered file content via tokenization
                        fc, headers = _build_filtered_file_content(
                            conn, unit, config_hash, project_root, build_dir_patterns=build_dir_patterns
                        )
                        content_filled += fc
                if not fallthrough and headers:
                    try:
                        tu_key = str(unit.file.resolve().relative_to(project_root))
                    except ValueError:
                        tu_key = str(unit.file.resolve())
                    tu_headers[tu_key] = headers
            if fallthrough:
                # Fall through to Phase 2 — _process_unit will re-parse with libclang
                pass
            else:
                if is_reuse:
                    terse = "reused (manifest)"
                elif hashes is not None:
                    terse = "unchanged (content)"
                else:
                    terse = "unchanged"
                log.info("[%d/%d] %s: %s", processed, len(units), fname, terse)
                continue

        if check_status == "skipped":
            skipped += 1
            log.info("[%d/%d] %s: skipped", processed, len(units), fname)
            continue

        # ── Phase 2: DB store (inside lock) ──
        with write_lock(db_path.parent, timeout=120.0):
            status, syms, refs, timing, tu_headers_list = _process_unit(
                unit,
                config_hash,
                project_root,
                vendor_patterns,
                project_patterns_list,
                index_refs,
                db_path,
                existing_files,
                conn=conn,
                force=force,
                pre_parsed=parsed_data,
                parse_timing=parse_timing,
                hashes=hashes,
                build_dir_patterns=build_dir_patterns,
            )
            if status == "updated":
                updated += 1
                total_syms += syms
                total_refs += refs
                acc_parse += timing[0]
                acc_lock += timing[1]
                acc_write += timing[2]
                if tu_headers_list:
                    try:
                        tu_key = str(unit.file.resolve().relative_to(project_root))
                    except ValueError:
                        tu_key = str(unit.file.resolve())
                    tu_headers[tu_key] = tu_headers_list
                log.info(
                    "[%d/%d] %s: %d syms, %d refs, %.1fs",
                    processed,
                    len(units),
                    fname,
                    syms,
                    refs,
                    sum(timing),
                )
            else:
                skipped += 1
                log.info("[%d/%d] %s: skipped", processed, len(units), fname)


    # ── Post-processing ──
    _run_postprocess(
        conn=conn,
        config_hash=config_hash,
        project_root=project_root,
        db_dir=db_path.parent,
        units=units,
        tu_headers=tu_headers,
        manifest=manifest,
        compile_commands=compile_commands,
        updated=updated,
        build_dir_patterns=build_dir_patterns,
        vendor_patterns=vendor_patterns,
        project_patterns_list=project_patterns_list,
        project_id=project_id,
        git_description=git_description,
        index_refs=index_refs,
        index_embeddings=index_embeddings,
        index_macros_expanded=index_macros_expanded,
        analyze_symbols=analyze_symbols,
        analyze_overrides=analyze_overrides,
        analyze_vendor=_analyze_vendor,
        llm_config=llm_config,
        cache_server_config=cache_server_config,
        force=force,
    )

    elapsed = time.monotonic() - t0
    log.info("", extra={"phase": f"Done — {total_syms} symbols, {total_refs} refs, {_fmt_dur(elapsed)}"})
    log.info("%d updated, %d unchanged, %d reused, %d skipped  config_hash=%s", updated, unchanged, reused, skipped, config_hash[:12])
    return config_hash
