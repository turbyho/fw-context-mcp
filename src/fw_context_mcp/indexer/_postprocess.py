"""Post-processing phases extracted from runner.py.

Handles parameter type extraction, method override graph, PageRank,
hotspot cache, and the main post-processing orchestrator
(``_run_postprocess``).
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path

from ..mcp.shared.pid_file import PidFile
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
    write_lock,
)

log = logging.getLogger(__name__)

# C++ type qualifiers that are NOT parameter names during signature parsing.
_CPP_TYPE_QUALIFIERS: frozenset[str] = frozenset({"const", "volatile", "constexpr", "noexcept"})


def _extract_param_types(signature: str) -> str:
    """Extract the parameter type list from a C/C++ function signature.

    Strips parameter names, default values, and whitespace to produce a
    normalized string suitable for override comparison.

    Examples:
        "int read(char *buf, size_t len)" → "char *,size_t"
        "void write(const uint8_t *data, size_t len)" → "const uint8_t *,size_t"
        "void reset()" → ""
        "void set(int)" → "int"

    **Limitations:**

    - Does **not** parse string literals or comments — a comma inside
      ``printf("hello, world")`` is treated as a parameter separator.
    - Does **not** resolve template aliases — ``std::vector<int>`` and
      ``VectorInt`` (where ``VectorInt = std::vector<int>``) are seen as
      different types.
    - **False negatives** (missing override edges) are possible; **false
      positives** (incorrect override edges) are extremely unlikely
      because both the derived AND base method signatures would need to
      contain the same ambiguous pattern.
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
# SECTION: Post-processing pipeline steps
# ═══════════════════════════════════════════════════════════════


def _step_rebuild_fts(conn: sqlite3.Connection, ctx: dict) -> None:
    """Rebuild FTS5 indexes for symbols, files, and macros."""
    rebuild_fts(conn)
    rebuild_files_fts(conn)
    rebuild_macros_fts(conn)


def _step_orphan_cleanup(conn: sqlite3.Connection, ctx: dict) -> None:
    """Remove orphaned files, embeddings, and LLM analysis rows."""
    from .db import clean_orphan_embeddings, clean_orphan_embeddings_vec, delete_orphan_files

    delete_orphan_files(conn, ctx["config_hash"])
    clean_orphan_embeddings(conn)
    clean_orphan_embeddings_vec(conn)
    conn.execute(
        "DELETE FROM llm_analysis WHERE symbol_id NOT IN (SELECT id FROM symbols)"
    )


def _step_align_is_project(conn: sqlite3.Connection, ctx: dict) -> None:
    """Mark project files in the ``files`` table based on project/vendor patterns."""
    config_hash = ctx["config_hash"]
    project_patterns_list: list[str] = ctx["project_patterns_list"]
    vendor_patterns: list[str] = ctx["vendor_patterns"]

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


def _step_update_manifest(conn: sqlite3.Connection, ctx: dict) -> None:
    """Update manifest.json and refresh header mtimes from it."""
    config_hash = ctx["config_hash"]
    updated_manifest = _update_manifest_after_index(
        manifest=ctx["manifest"],
        units=ctx["units"],
        project_root=ctx["project_root"],
        db_dir=ctx["db_dir"],
        compile_commands=ctx["compile_commands"],
        updated_count=ctx["updated"],
        tu_headers=ctx["tu_headers"] if ctx["tu_headers"] else None,
        build_dir_patterns=ctx["build_dir_patterns"],
        config_hash=config_hash,
    )
    if updated_manifest is not None:
        _refresh_header_mtimes_from_manifest(conn, config_hash, ctx["project_root"], updated_manifest)


def _step_expand_macros(conn: sqlite3.Connection, ctx: dict) -> None:
    """Resolve and store expanded macro values (libclang-powered)."""
    from .macros import resolve_and_update

    seen_flags: set[tuple] = set()
    for unit in ctx["units"]:
        flag_key = tuple(sorted(unit.clang_args))
        if flag_key in seen_flags:
            continue
        seen_flags.add(flag_key)
        try:
            resolve_and_update(
                conn, ctx["config_hash"], unit.clang_args, unit.file.resolve(),
            )
        except SAFE_EXCEPT as e:
            if is_fatal(e):
                raise
            pass  # libclang/SQLite fallback


def _step_build_embeddings(conn: sqlite3.Connection, ctx: dict) -> None:
    """Compute symbol embeddings via the configured LLM backend."""
    config_hash = ctx["config_hash"]
    llm_config = ctx["llm_config"]
    if ctx["force"]:
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
    _build_embeddings(conn, config_hash, llm_config, ctx["db_dir"])
    conn.commit()


def _step_llm_analysis(conn: sqlite3.Connection, ctx: dict) -> None:
    """Generate natural-language symbol explanations via the configured LLM."""
    from ..cache_client import CacheClient

    config_hash = ctx["config_hash"]
    llm_config = ctx["llm_config"]
    cache_server_config = ctx.get("cache_server_config")

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
            if is_fatal(e):
                raise
            pass
    if ctx["force"]:
        conn.execute(
            "DELETE FROM llm_analysis WHERE symbol_id IN (SELECT id FROM symbols WHERE config_hash = ?)",
            (config_hash,),
        )
        conn.commit()
    _build_llm_analysis(
        conn, config_hash, llm_config, ctx["db_dir"],
        project_root=ctx["project_root"],
        project_only=not ctx["analyze_vendor"],
        cache_client=cc,
        retry_unparseable=True,
    )
    if cc:
        cc.close()
    conn.commit()


def _step_build_overrides(conn: sqlite3.Connection, ctx: dict) -> None:
    """Build the virtual-method override graph from the inheritance table."""
    config_hash = ctx["config_hash"]
    if ctx["force"]:
        conn.execute("DELETE FROM overrides WHERE config_hash = ?", (config_hash,))
        conn.commit()
    _build_overrides(conn, config_hash, ctx["db_dir"])
    conn.commit()


def _step_pagerank_hotspot(conn: sqlite3.Connection, ctx: dict) -> None:
    """Compute PageRank scores and build the hotspot cache from the call graph."""
    config_hash = ctx["config_hash"]
    if ctx["force"]:
        conn.execute("UPDATE symbols SET pagerank = 0.0 WHERE config_hash = ?", (config_hash,))
        conn.execute("DELETE FROM hotspot_cache WHERE config_hash = ?", (config_hash,))
        conn.commit()
    _build_pagerank(conn, config_hash)
    conn.commit()
    _build_hotspot_cache(conn, config_hash)
    conn.commit()


def _step_finalize_manifest(conn: sqlite3.Connection, ctx: dict) -> None:
    """Stamp the build config with manifest verification status."""
    manifest_path = ctx["db_dir"] / "manifest.json"
    manifest_verification: str = "full" if manifest_path.exists() else "none"
    with transaction(conn):
        upsert_build_config(
            conn, ctx["config_hash"], ctx["project_id"],
            str(ctx["compile_commands"]),
            description=ctx["git_description"],
            manifest_verification=manifest_verification,
            analyze_vendor=int(ctx["analyze_vendor"]),
        )


def _step_cleanup_old_builds(conn: sqlite3.Connection, ctx: dict) -> None:
    """Delete data from previous builds, unless a reindex is paused."""
    config_hash = ctx["config_hash"]
    project_id = ctx["project_id"]
    db_dir = ctx["db_dir"]

    old_hashes = conn.execute(
        """SELECT config_hash FROM build_configs
           WHERE project_id = ? AND config_hash != ?
           ORDER BY created_at DESC""",
        (project_id, config_hash),
    ).fetchall()
    if not old_hashes:
        return

    if PidFile.is_active(db_dir / "reindex.pause"):
        return

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


def _step_wal_checkpoint(conn: sqlite3.Connection, ctx: dict) -> None:
    """Run a passive WAL checkpoint and stamp the schema version."""
    try:
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    except SAFE_EXCEPT as e:
        if is_fatal(e):
            raise
        pass
    conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")


# ── Pipeline definition ─────────────────────────────────────────────

# Each entry: (step_name, step_fn, condition | None)
# condition(ctx) → bool — when None, the step always runs.
_STEPS: list[tuple[str, Callable[..., None], Callable[..., bool] | None]] = [
    ("fts5",             _step_rebuild_fts,       None),
    ("orphans",          _step_orphan_cleanup,     None),
    ("is_project",       _step_align_is_project,   None),
    ("manifest",         _step_update_manifest,    None),
    ("macros",           _step_expand_macros,      lambda c: c["index_macros_expanded"] and c["units"]),
    ("embeddings",       _step_build_embeddings,   lambda c: c["index_embeddings"] and c["llm_config"] is not None and c["llm_config"].enabled),
    ("llm_analysis",     _step_llm_analysis,       lambda c: c["analyze_symbols"] and c["llm_config"] is not None and c["llm_config"].enabled),
    ("overrides",        _step_build_overrides,    lambda c: c["analyze_overrides"]),
    ("pagerank_hotspot", _step_pagerank_hotspot,   lambda c: c["index_refs"]),
    ("finalize_manifest", _step_finalize_manifest,  None),
    ("cleanup_old",      _step_cleanup_old_builds,  None),
    ("wal_checkpoint",   _step_wal_checkpoint,      None),
]


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
    """Run all post-processing phases via a data-driven pipeline.

    Each step in ``_STEPS`` is executed in order.  Conditional steps
    are skipped when their guard returns ``False``.  Runtime errors in
    individual steps are logged (not fatal) so the remaining steps
    always execute.
    """
    ctx = {
        "config_hash": config_hash,
        "project_root": project_root,
        "db_dir": db_dir,
        "units": units,
        "tu_headers": tu_headers,
        "manifest": manifest,
        "compile_commands": compile_commands,
        "updated": updated,
        "build_dir_patterns": build_dir_patterns,
        "vendor_patterns": vendor_patterns,
        "project_patterns_list": project_patterns_list,
        "project_id": project_id,
        "git_description": git_description,
        "index_refs": index_refs,
        "index_embeddings": index_embeddings,
        "index_macros_expanded": index_macros_expanded,
        "analyze_symbols": analyze_symbols,
        "analyze_overrides": analyze_overrides,
        "analyze_vendor": analyze_vendor,
        "llm_config": llm_config,
        "cache_server_config": cache_server_config,
        "force": force,
    }

    for step_name, step_fn, guard in _STEPS:
        if guard is not None and not guard(ctx):
            continue
        t0 = time.monotonic()
        try:
            step_fn(conn, ctx)
            elapsed = time.monotonic() - t0
            if elapsed > 0.5:
                log.debug("Postprocess step %s: %.1fs", step_name, elapsed)
        except SAFE_EXCEPT as e:
            if is_fatal(e):
                raise
            log.warning("Postprocess step %s failed: %s", step_name, e)

