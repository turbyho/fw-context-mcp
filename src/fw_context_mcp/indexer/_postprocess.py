"""Post-processing phases extracted from runner.py.

Handles parameter type extraction, method override graph, PageRank,
hotspot cache, and the main post-processing orchestrator
(``_run_postprocess``).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from pathlib import Path

from ..utils import SAFE_EXCEPT, is_fatal
from ._embedding import _build_embeddings
from ._llm_analysis import _build_llm_analysis
from ._manifest_updater import _refresh_header_mtimes_from_manifest, _update_manifest_after_index
from .db import (
    CURRENT_SCHEMA_VERSION,
    delete_build_data,
    rebuild_files_fts,
    rebuild_fts,
    rebuild_macros_fts,
    transaction,
    upsert_build_config,
)

log = logging.getLogger(__name__)


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
            except SAFE_EXCEPT as e:
                if is_fatal(e): raise
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
            except SAFE_EXCEPT as e:
                if is_fatal(e): raise
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
    except SAFE_EXCEPT as e:
        if is_fatal(e): raise
        pass  # libclang/SQLite fallback
    conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")

