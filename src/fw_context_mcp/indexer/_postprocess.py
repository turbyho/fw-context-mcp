"""Post-processing pipelines that run AFTER libclang parsing is complete.

The indexer loop first extracts symbols from each translation unit via
libclang.  That phase is TU-parallel and focused on raw symbol + reference
extraction.  This module handles everything that must run *sequentially*
after the full symbol table is available:

**Why post-processing is separate from parsing**

1. **Global knowledge required.**  The inheritance graph, call graph,
   and cross-TU reference resolution need the complete symbol table —
   they cannot work on a single TU at a time.

2. **Dependency ordering.**  Steps have strict dependencies:
   FTS5 rebuilding must see the final symbol set, PageRank needs the
   complete call graph, and override detection needs the inheritance
   graph.  A data-driven pipeline (``_STEPS``) ensures correct ordering
   and skips optional phases based on user configuration.

3. **Idempotency for resilience.**  If a step fails mid-way, the next
   reindex run can pick up where it left off — each step checks whether
   its output already exists before recomputing.

**Pipeline phases (in order)**

1. **Purge missing files** — remove symbols from deleted source files
2. **FTS5 rebuild** — rebuild full-text search indexes for symbols,
   files, and macros
3. **Orphan cleanup** — remove dangling rows in related tables
4. **Project alignment** — mark which files belong to the project
   vs. The vendor SDK
5. **Manifest update** — write ``manifest.json`` for incremental
   reindex tracking
6. **Macro expansion** — resolve ``#define`` values via libclang
   preprocessing
7. **Dispatch edges** — create synthetic call-graph edges from
   event-loop registrations (``call_every``, ``k_work_submit``, etc.)
8. **Embeddings** — compute vector embeddings for semantic search
9. **LLM analysis** — generate natural-language symbol explanations
10. **Override graph** — build the virtual-method override map
11. **Cross-TU ref backfill** — resolve method calls whose target
    was defined in a different translation unit
12. **PageRank + hotspot cache** — compute call-graph centrality
    and pre-aggregate caller counts
13. **Finalize manifest** — stamp build config with verification status
14. **Cleanup old builds** — delete data from previous indexing runs
    (unless a reindex is paused)
15. **WAL checkpoint** — flush SQLite write-ahead log to disk

Two functions are also used before the pipeline during TU-parallel
parsing: ``_extract_param_types`` for override matching and
``_normalize_type_namespaces`` for cross-namespace override detection.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
import tomllib
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from pathlib import Path

from ..mcp.shared.pid_file import PidFile
from ..utils import SAFE_EXCEPT, is_fatal
from ._dispatch_bridges import _DISPATCH_ENTRY_POINTS
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
from .db._chunking import chunked
from .manifest import _is_generated_header

log = logging.getLogger(__name__)

# C++ type qualifiers that are NOT parameter names during signature parsing.
_CPP_TYPE_QUALIFIERS: frozenset[str] = frozenset({"const", "volatile", "constexpr", "noexcept"})


def _extract_param_types(signature: str) -> str:
    """Extract the parameter type list from a C/C++ function signature.

    Strips parameter names, default values, and whitespace to produce a
    normalized string suitable for override comparison.

    This is used during virtual-method override detection: a derived-class
    method ``Derived::write(const uint8_t *data, size_t len)`` overrides
    ``Base::write(const uint8_t *buf, size_t sz)`` when both normalize to
    the same type list ``"const uint8_t *,size_t"``.  Parameter names are
    irrelevant for override semantics — only types matter.

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
    # Find the outermost parentheses — use a depth counter rather than a
    # regex because C++ parameter lists can contain nested parentheses
    # (function pointers, lambdas, template args with > and <).
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
    # In C++, foo(void) and foo() are semantically identical for
    # override purposes — both mean "no parameters".  Normalize
    # both to empty so they match during comparison.
    if params_str == "void":
        return ""

    # Split by top-level commas, strip parameter names (keep only types)
    # Depth tracking handles nested angle brackets, parentheses, braces,
    # and square brackets so that commas inside template arguments
    # (e.g. map<int,string>) or function-pointer types are NOT treated
    # as parameter separators.
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
    # and any trailing default value.

    # The algorithm strips the last identifier token from each parameter
    # *unless* that token is a C++ type qualifier (const, volatile, etc.).
    # Pointer/reference markers (*, &) that precede the parameter name
    # are kept because they belong to the type.  This handles both
    # "int x" → "int" and "int *x" → "int *".
    normalized: list[str] = []
    for param in parts:
        param = param.strip()
        if not param:
            continue
        # Remove default value (everything after '=').
        # Must be done before name stripping because default values
        # can themselves contain identifier-like strings,
        # e.g. "int x = DEFAULT_X" — we first strip "= DEFAULT_X",
        # then remove "x" from the remaining "int x".
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


def _normalize_type_namespaces(param_types: str) -> str:
    """Strip namespace prefixes from all type tokens in a parameter list.

    ``const ble::ConnectionCompleteEvent &`` → ``const ConnectionCompleteEvent &``
    ``std::vector<int>`` → ``vector<int>``

    Used as a fallback in override detection when exact type-matching
    fails.  Cross-namespace overrides are common in firmware codebases
    where a derived class in namespace ``app`` overrides a base-class
    method whose signature uses types from a vendor namespace (``ble::``,
    ``mbed::``).  By stripping the namespace prefix from every type
    token, ``ble::ConnectionCompleteEvent`` and ``ConnectionCompleteEvent``
    (when the derived class imports the symbol) are treated as equivalent.
    """
    if not param_types:
        return ""
    parts = param_types.split(",")
    normalized: list[str] = []
    for part in parts:
        tokens = part.strip().split()
        stripped: list[str] = []
        for t in tokens:
            if "::" in t:
                t = t.rsplit("::", 1)[-1]
            stripped.append(t)
        normalized.append(" ".join(stripped))
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

    # Phase 1: collect all virtual/pure-virtual project methods with parent class info.
    # Single table scan groups by parent_usr for efficient batch processing in Phase 2.
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
        queue: deque[str] = deque([parent_usr])
        while queue:
            # Process in levels: batch-fetch edges for all nodes at this depth
            level = list(queue)
            queue.clear()
            visited.update(level)
            placeholders = ",".join("?" * len(level))
            rows = conn.execute(
                f"""SELECT derived_usr, base_usr FROM inheritance
                   WHERE config_hash = ? AND derived_usr IN ({placeholders})""",
                (config_hash, *level),
            ).fetchall()
            for r in rows:
                base = r["base_usr"]
                if base not in visited:
                    bases.append(base)
                    queue.append(base)
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
            # Phase 1: exact type match (fast path, handles 95 %+ of cases)
            if derived_params == base_params:
                override_rows.append((config_hash, vrow["usr"], bm["usr"]))
                continue
            # Phase 2: namespace-normalized match (handles cross-namespace overrides)
            derived_norm = _normalize_type_namespaces(derived_params)
            base_norm = _normalize_type_namespaces(base_params)
            if derived_norm == base_norm:
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

    PageRank gives each function a centrality score — how many callers
    (direct and indirect, weighted) depend on it.  This powers the
    ``find_hotspots`` tool: functions with high PageRank have the most
    architectural weight and are the best candidates for refactoring,
    testing, or optimization.

    Damping factor 0.85 is the standard value from the original PageRank
    paper, balancing the random-surfer probability with the link-following
    probability.  Max 50 iterations covers even large codebases (10k+
    functions) because the call graph is sparse — almost all call graphs
    converge within 20-30 iterations.  The convergence threshold of
    1e-6 is sufficient for a stable ordering; higher precision adds
    iterations without changing relative rankings.

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

    # Load edges: only `call` ref_kind to avoid inflating scores with
    # indirect/constructor edges.  JOIN on symbols ensures both endpoints
    # are indexed functions — filters out refs to variables and macros.
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

    # Build adjacency lists: outgoing (who-this-calls) for rank distribution,
    # incoming (who-calls-this) for rank accumulation.
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

    # Standard PageRank iteration: each function distributes its score
    # equally among outgoing call edges.  The |outgoing| for
    # undirected-graph nodes defaults to [1] so they don't divide by zero —
    # their score dissipates into the (1-damping)/n base term.
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

    # Normalize to 0.0–1.0 so scores are human-readable and comparable
    # across reindex runs.  Division by max_score preserves relative
    # ordering while bounding values in [0, 1].
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


def _build_hotspot_cache(conn, config_hash: str, *, force: bool = False,
                         write_lock_held: bool = False, db_dir: Path | None = None) -> None:
    """Pre-compute hotspot caller counts for instant ``find_hotspots`` queries.

    The ``find_hotspots`` tool must return a ranked list of functions by
    caller count in under a second.  Computing ``COUNT(*) ... GROUP BY``
    on the ``refs`` table at query time would require a full scan of
    millions of refs rows — tens of seconds on large codebases.  This
    cache stores pre-aggregated counts keyed by ``symbol_id`` so the
    tool does a simple ``ORDER BY caller_count DESC LIMIT N``.

    Counts both ``call`` and ``indirect`` reference kinds so that
    function-pointer callbacks and ISR vector registrations are included
    in the caller count.

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

    with write_lock(db_dir, timeout=5.0) if not write_lock_held and db_dir is not None else nullcontext():
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


def _step_purge_missing_files(conn: sqlite3.Connection, ctx: dict) -> None:
    """Purge ghost records of files that no longer exist on disk.

    Runs first in the pipeline so FTS, embeddings, overrides, and PageRank
    are not built on ghost nodes that are deleted moments later.

    Uses a ThreadPoolExecutor for parallel ``os.path.exists()`` checks
    because file-existence is I/O-bound — the GIL does not block it and
    checking thousands of files sequentially would add seconds of latency
    while the I/O scheduler is idle.

    The threshold guard prevents accidental mass-deletion: if >20% of
    indexed files are missing, the index is likely pointing at a
    disconnected mount or stale project root, not genuinely deleted files.
    """
    from ..utils import abs_path

    config_hash = ctx["config_hash"]
    project_root = ctx["project_root"]
    build_patterns = ctx.get("build_dir_patterns") or []

    rows = conn.execute(
        "SELECT id, path FROM files WHERE config_hash = ?",
        (config_hash,),
    ).fetchall()

    # Collect candidates: skip empty paths (header-less TU markers) and
    # build-output files (regenerated independently of source control).
    candidates: list[tuple[int, str, str]] = []  # (id, db_path, abs_path)
    for r in rows:
        db_path = r["path"]
        if not db_path:
            continue
        if any(pat in db_path for pat in build_patterns):
            continue
        abs_p = abs_path(project_root, db_path)
        # Absolute paths outside project_root: skip (system headers etc.)
        if os.path.isabs(db_path) and not abs_p.startswith(str(project_root)):
            continue
        candidates.append((r["id"], db_path, abs_p))

    if not candidates:
        return

    # Guard: abort when too many files are missing (offline mount etc.)
    threshold_pct = ctx.get("purge_max_missing_percent", 20)
    total = len(candidates)
    if total == 0:
        return

    missing: list[tuple[int, str]] = []
    with ThreadPoolExecutor(max_workers=min(8, len(candidates))) as ex:
        futures = {
            ex.submit(os.path.exists, abs_p): (file_id, db_path)
            for file_id, db_path, abs_p in candidates
        }
        for f in as_completed(futures):
            if not f.result():
                missing.append(futures[f])

    if not missing:
        return

    missing_pct = (len(missing) / total) * 100
    if missing_pct > threshold_pct:
        log.warning(
            "Ghost-file purge aborted: %d/%d files (%.1f%%) missing from disk "
            "(threshold %d%%). Possible offline mount or wrong project root.",
            len(missing), total, missing_pct, threshold_pct,
        )
        return

    from .db import purge_missing_files_batch

    removed = purge_missing_files_batch(
        conn, config_hash, missing,
        db_dir=ctx["db_dir"],
    )
    preview = ", ".join(p for _, p in missing[:5])
    if len(missing) > 5:
        preview += f", … (+{len(missing) - 5} more)"
    log.info("Purged %d ghost file(s), %d symbol(s): %s", len(missing), removed, preview)


def _step_rebuild_fts(conn: sqlite3.Connection, ctx: dict) -> None:
    """Rebuild FTS5 indexes for symbols, files, and macros."""
    rebuild_fts(conn)
    rebuild_files_fts(conn)
    rebuild_macros_fts(conn)


def _step_orphan_cleanup(conn: sqlite3.Connection, ctx: dict) -> None:
    """Remove orphaned files, embeddings, and LLM analysis rows."""
    from .db import clean_orphan_embeddings, clean_orphan_embeddings_vec, delete_orphan_files

    delete_orphan_files(conn, ctx["config_hash"], ctx["project_root"])
    clean_orphan_embeddings(conn)
    clean_orphan_embeddings_vec(conn)
    conn.execute(
        "DELETE FROM llm_analysis WHERE symbol_id NOT IN (SELECT id FROM symbols)"
    )


def _step_align_is_project(conn: sqlite3.Connection, ctx: dict) -> None:
    """Mark project files in the ``files`` table based on project/vendor patterns.

    Two-phase marking:

    1. **Explicit project patterns** (``project_patterns_list`` from config):
       files matching globs like ``src/%`` are unconditionally marked
       ``is_project = 1``.  This handles the typical directory structure
       where all source under ``src/`` and ``lib/`` is project code.

    2. **Exclusion-based remainder**: all files that do NOT match any
       vendor pattern are marked as project code.  Vendor patterns come
       from the detected build system (e.g. ``mbed-os/%`` for Mbed OS,
       ``zephyr/%`` for Zephyr).  Files with absolute paths (system
       headers) are excluded from this phase — they are neither project
       nor vendor.

    The two-phase approach ensures custom project directory layouts are
    supported while still auto-detecting project-vs-vendor boundaries
    from the build system.
    """
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
         vendor_patterns=ctx.get("vendor_patterns"),
         config_hash=config_hash,
         scope=ctx.get("scope"),
         reparsed_tus=ctx.get("reparsed_tus"),
     )
    # Keep whichever manifest is now authoritative so the coverage purge does
    # not have to re-read it.  A no-op run returns None and leaves the
    # on-disk manifest (already loaded into ctx) current.
    ctx["effective_manifest"] = updated_manifest or ctx.get("manifest")
    if updated_manifest is not None:
        _refresh_header_mtimes_from_manifest(
            conn,
            config_hash,
            ctx["project_root"],
            updated_manifest,
            hash_cache=ctx.get("header_hash_cache"),
        )


def _build_coverage_set(units: list, manifest: dict | None, project_root: Path) -> set[str] | None:
    """Return the file paths that belong to this build, or None if unknown.

    A file belongs to the build when it is a translation unit of the current
    ``compile_commands.json``, or a header that one of those TUs includes.

    The TU half comes from *units*, never from the manifest: when no TU was
    re-parsed the manifest is not rewritten, so a TU dropped from the build
    would still be listed there.  The header half can only come from the
    manifest — it is the sole record of what each TU includes — and entries
    are filtered to the current TUs so a dropped TU takes its headers with it
    (unless another TU includes them too).

    Returns ``None`` when the manifest cannot answer the header question: no
    manifest, no entries, or entries carrying no header lists (a preliminary
    manifest).  Purging on that basis would delete every header in the index.
    """
    if not manifest:
        return None
    entries = manifest.get("entries") or []
    if not entries:
        return None

    tu_paths: set[str] = set()
    for unit in units:
        try:
            tu_paths.add(str(unit.file.resolve().relative_to(project_root)))
        except ValueError:
            tu_paths.add(str(unit.file.resolve()))

    covered = set(tu_paths)
    saw_headers = False
    for entry in entries:
        if entry.get("file") not in tu_paths:
            continue
        # Built from the per-TU lists, never from the manifest's shared
        # headers map: an orphan left in the map by a re-parse that stopped
        # including a header would keep that file inside coverage and the
        # purge would never remove its rows.
        headers = entry.get("headers") or []
        if headers:
            saw_headers = True
        covered.update(headers)

    return covered if saw_headers else None


def _step_purge_files_outside_build(conn: sqlite3.Connection, ctx: dict) -> None:
    """Delete index rows for files that no longer belong to the build.

    WHY this step exists: ``config_hash`` identifies the compilation dialect,
    so dropping a source file from ``compile_commands.json`` no longer mints a
    new build.  Nothing else notices the file left — ``purge_missing`` only
    looks for files gone from DISK, and ``delete_orphan_files`` only removes
    rows with no symbols, no macros and empty content.  Without this step a
    removed TU keeps its symbols, macros, refs and file content forever.

    The same guard as ``purge_missing`` applies: refuse to act when the set
    looks implausibly large, which would mean the coverage data is wrong
    rather than the index being stale.
    """
    config_hash = ctx["config_hash"]
    covered = _build_coverage_set(
        ctx["units"], ctx.get("effective_manifest"), ctx["project_root"]
    )
    if covered is None:
        log.debug("coverage purge skipped: manifest carries no header lists")
        return
    # Assembly units are part of the build and of neither source above:
    # libclang never parsed them, so they are absent from *units*, and the
    # manifest lists only what libclang saw.  They were written and then
    # deleted again in the same run until this line existed.
    covered |= set(ctx.get("asm_paths") or ())

    # Vendor and SDK files are candidates too, not just project sources: a
    # framework upgrade replaces headers, and the ones it dropped must go with
    # it.  That only works because the manifest now records every file an
    # #include reached, extensionless C++ standard headers and .tcc template
    # bodies included.  While it carried an extension whitelist this step
    # deleted 29 real files and 1810 symbols on the ESP32 project.
    rows = conn.execute(
        "SELECT id, path FROM files WHERE config_hash = ? AND path != ''",
        (config_hash,),
    ).fetchall()
    stale = [(r["id"], r["path"]) for r in rows if r["path"] not in covered]
    if not stale:
        return

    threshold_pct = ctx.get("purge_max_missing_percent", 20)
    stale_pct = (len(stale) / len(rows)) * 100 if rows else 0
    if stale_pct > threshold_pct:
        log.warning(
            "Coverage purge aborted: %d/%d files (%.1f%%) are outside the build "
            "(threshold %d%%). The manifest's header lists are probably incomplete.",
            len(stale), len(rows), stale_pct, threshold_pct,
        )
        return

    from .db import purge_missing_files_batch

    removed = purge_missing_files_batch(conn, config_hash, stale, db_dir=ctx["db_dir"])
    preview = ", ".join(p for _, p in stale[:5])
    if len(stale) > 5:
        preview += f", … (+{len(stale) - 5} more)"
    log.info(
        "Purged %d file(s) no longer in the build, %d symbol(s): %s",
        len(stale), removed, preview,
    )


def _resolve_matching_usr(
    conn: sqlite3.Connection, config_hash: str, name: str
) -> str | None:
    """Look up a symbol's USR by name, preferring definitions.

    Used by dispatch-edge resolution when the pending-dispatch table
    stores a symbol by qualified name rather than USR (the USR may not
    have been known at registration time because it was from a different
    TU).  Three-tier matching handles the common name-ambiguity cases:

    1. Exact ``name`` match (e.g. ``"main"``)
    2. Exact ``qualified_name`` match (e.g. ``"app::main"``)
    3. Suffix LIKE match on qualified name (e.g. ``"%::EventHandler::process"``)

    When multiple USRs exist for the same name, tie-breaking uses three
    criteria in descending priority:

    1. **is_definition** — the definition is the "real" symbol; a
       declaration-only entry may exist from a forward-declaration header.
    2. **ref_count** — incoming references indicate the symbol is actively
       used, so it is more likely to be the intended target.
    3. **out_count** — outgoing references indicate the symbol has a body
       (there are calls inside it), so it is not a stub or declaration.
    """
    esc_name = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    suffix_pattern = f"%::{esc_name}"
    row = conn.execute(
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
    return row["usr"] if row else None


def _step_resolve_dispatches(conn: sqlite3.Connection, ctx: dict) -> None:
    """Resolve pending dispatch edges into ref_kind='dispatch' references.

    Reads the `_pending_dispatch` temp table populated during indexing,
    resolves the dispatch entry point and callback target USRs, and
    inserts synthetic dispatch edges into the refs table.

    **How dispatch bridging works:**  When the indexer encounters a
    registration call like ``event_queue.call_every(1000, &MyClass::tick)``,
    it records the callee qualified name (``EventQueue::call_every``) and
    the callback target in ``_pending_dispatch``.  This step matches
    ``call_every`` to a known *dispatch entry point*
    (``EventQueue::dispatch_forever``) and creates an edge from the
    entry point to ``MyClass::tick``.  The entry point acts as a bridge
    node — subsequent call-graph tools can walk from ``main`` through
    ``dispatch_forever`` to all registered callbacks.

    User-defined dispatch bridges (in ``config.toml``) override or extend
    the built-in map.  This allows support for custom RTOS dispatch
    primitives without modifying the source code.
    """
    config_hash = ctx["config_hash"]
    try:
        rows = conn.execute(
            "SELECT * FROM _pending_dispatch WHERE config_hash = ?",
            (config_hash,),
        ).fetchall()
    except sqlite3.OperationalError:
        return
    if not rows:
        return

    # ── Merge user-defined dispatch bridges from project TOML config ──
    _dispatch_map = dict(_DISPATCH_ENTRY_POINTS)
    project_root = ctx.get("project_root")
    if project_root is not None:
        try:
            _config_path = Path(project_root) / ".fw-context" / "config.toml"
            if _config_path.exists():
                _cfg = tomllib.loads(_config_path.read_text())
                _user_bridges = _cfg.get("call_graph", {}).get("dispatch_bridges", {})
                if isinstance(_user_bridges, dict):
                    _dispatch_map.update(_user_bridges)
        except (OSError, KeyError, ValueError) as e:
            log.warning("Failed to load dispatch_bridges from %s: %s", _config_path, e)
        except Exception:
            log.exception("Unexpected error loading dispatch_bridges from %s", _config_path)

    resolved = 0
    for r in rows:
        callee_qn = r["callee_qn"]
        entry_qn = _dispatch_map.get(callee_qn)
        if not entry_qn:
            continue
        entry_usr = _resolve_matching_usr(conn, config_hash, entry_qn)
        if not entry_usr:
            continue

        target_usr = r["target_usr"] or ""
        if not target_usr:
            target_qn = r["target_qn_partial"] or ""
            if target_qn:
                target_usr = _resolve_matching_usr(conn, config_hash, target_qn) or ""

        if not target_usr:
            continue

        conn.execute(
            """INSERT OR IGNORE INTO refs
               (config_hash, to_usr, from_file, from_line, from_usr, ref_kind)
               VALUES (?, ?, ?, ?, ?, 'dispatch')""",
            (
                config_hash,
                target_usr,
                r["file"],
                r["line"],
                entry_usr,
            ),
        )
        resolved += 1

    if resolved:
        log.info("Dispatch edges resolved: %d synthetic edges created", resolved)


def _step_expand_macros(conn: sqlite3.Connection, ctx: dict) -> None:
    """Resolve and store expanded macro values (libclang-powered)."""
    from .macros import resolve_and_update

    # Process all TUs — different TUs include different headers,
    # so each contributes a different set of expanded macro values.
    # Deduplicating by flags would miss macros only visible from
    # TUs that include project config headers.
    for unit in ctx["units"]:
        try:
            resolve_and_update(
                conn, ctx["config_hash"], unit.clang_args,
                unit.file.resolve(), cwd=ctx.get("project_root"),
            )
        except SAFE_EXCEPT as e:
            if is_fatal(e):
                raise
            pass  # libclang/SQLite fallback


def _step_build_embeddings(conn: sqlite3.Connection, ctx: dict) -> None:
    """Compute symbol embeddings via the configured LLM backend.

    Embeddings are high-dimensional vectors (1024D for mxbai-embed-large)
    computed from each symbol's signature and docstring.  They power the
    ``semantic_search`` tool — finding symbols by meaning rather than
    by exact keyword match.

    When ``force`` is True, existing embeddings for this config are deleted
    first.  This is necessary after incremental reindex because removed
    symbols would leave orphaned embedding rows that reference stale
    ``symbol_id`` values.
    """
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
    """Generate natural-language symbol explanations via the configured LLM.

    Each function/method/class definition gets an ``llm_analysis`` row
    with a summary, inputs, and outputs description.  This powers the
    ``explain_symbol`` tool and the pre-computed analysis fields in
    ``search_code`` results.

    The optional ``CacheClient`` avoids re-analyzing symbols that have
    not changed since the last index run — it checks a remote cache
    server (if configured) for existing analyses keyed by symbol USR
    and content hash.  This saves minutes of LLM calls on incremental
    reindexes where only a handful of files changed.
    """
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
    """Build the virtual-method override graph from the inheritance table.

    Resolves which derived-class methods override which base-class
    virtual methods.  Used by ``get_method_overrides`` and the
    ``find_callers`` tool (to find callers through base-class pointers).
    """
    config_hash = ctx["config_hash"]
    if ctx["force"]:
        conn.execute("DELETE FROM overrides WHERE config_hash = ?", (config_hash,))
        conn.commit()
    _build_overrides(conn, config_hash, ctx["db_dir"])
    conn.commit()


def _step_backfill_cross_tu_refs(conn: sqlite3.Connection, ctx: dict) -> None:
    """Backfill call references that per-TU symbol lookup could not resolve.

    Per-TU ``_qn_to_usr`` only contains symbols from the current
    translation unit.  Cross-TU method calls (e.g. a private method
    defined in another ``.cpp`` file) are resolved here using the
    complete symbols table, which is available after the TU loop.
    Must run BEFORE PageRank so the call graph is complete.
    """
    from .ops import backfill_cross_tu_refs

    config_hash = ctx["config_hash"]
    project_root = ctx["project_root"]
    added = backfill_cross_tu_refs(conn, config_hash, project_root)
    if added:
        log.info("Cross-TU ref backfill: %d references added", added)


def _step_pagerank_hotspot(conn: sqlite3.Connection, ctx: dict) -> None:
    """Compute PageRank scores and build the hotspot cache from the call graph.

    Runs after cross-TU backfill to ensure the call graph is complete.
    Two outputs are computed sequentially from the same call-graph data:
    PageRank scores (centrality) and the pre-aggregated hotspot cache
    (caller counts for instant ``find_hotspots`` queries).
    """
    config_hash = ctx["config_hash"]
    if ctx["force"]:
        conn.execute("UPDATE symbols SET pagerank = 0.0 WHERE config_hash = ?", (config_hash,))
        conn.execute("DELETE FROM hotspot_cache WHERE config_hash = ?", (config_hash,))
        conn.commit()
    _build_pagerank(conn, config_hash)
    conn.commit()
    _build_hotspot_cache(conn, config_hash, db_dir=ctx.get("db_dir"))
    conn.commit()


class IndexIntegrityError(Exception):
    """The finished index contradicts itself.

    Deliberately outside :data:`SAFE_EXCEPT`, so the post-process loop cannot
    swallow it as "a step failed, carry on".  An index that disagrees with
    itself answers queries wrongly, and a wrong answer is worse than a
    missing one — the run must fail and say why.
    """


_FTS_TABLES = ("symbols_fts", "files_fts", "macros_fts")


def fts_inconsistencies(conn: sqlite3.Connection) -> list[str]:
    """Public alias — the multi-build CLI owns its own FTS rebuild.

    That path defers the per-build rebuild and does one at the end, so it also
    has to run the check that was skipped inside each build.
    """
    return _fts_inconsistencies(conn)


def _fts_inconsistencies(conn: sqlite3.Connection) -> list[str]:
    """Return a message per FTS index that disagrees with its content table.

    WHY ``rank`` is passed as 1: these are external-content FTS5 tables, and
    the plain ``integrity-check`` command only validates the index's internal
    structure.  Measured against a deliberately broken index — a row deleted
    from ``symbols`` with the ``symbols_ad`` trigger dropped, leaving a
    dangling FTS entry that ``MATCH`` still finds — the bare form and
    ``rank = 0`` both reported "ok".  Only ``rank = 1``, which compares the
    index against the content table, detected it.  A check that cannot see
    the failure it exists for is worse than no check: it grants confidence.
    """
    problems: list[str] = []
    for table in _FTS_TABLES:
        try:
            conn.execute(
                f"INSERT INTO {table}({table}, rank) VALUES('integrity-check', 1)"  # noqa: S608
            )
        except sqlite3.OperationalError as exc:
            # No such table — the FTS index was never created in this DB.
            #
            # This catch is DELIBERATELY wider than that one case.
            # OperationalError is a subtype of DatabaseError and this except
            # comes first, so a locked database or a disk error also lands
            # here and reaches log.debug alone.  A narrower catch would need
            # to read the message text, which is not a stable interface.  The
            # cost is one silent skip; the check runs again on the next run.
            log.debug("FTS integrity check skipped for %s: %s", table, exc)
        except sqlite3.DatabaseError as exc:
            problems.append(f"{table} disagrees with its content table: {exc}")
    return problems


def _step_verify_integrity(conn: sqlite3.Connection, ctx: dict) -> None:
    """Fail the run when the finished index contradicts itself.

    Two checks, both cheap enough to always run:

    - ``PRAGMA foreign_key_check`` — a row pointing at a parent that no
      longer exists.  This is what a cleanup path that missed a table looks
      like from the outside, which is the defect class this whole series of
      changes was about.
    - FTS index versus content table.  A dangling FTS entry makes a deleted
      symbol searchable, so ``search_code`` reports code that is not there.

    Placed BEFORE ``finalize_manifest`` on purpose: raising here means the
    build is never stamped ``"full"``.  For a new ``config_hash`` it keeps the
    ``"indexing"`` marker, so ``get_active_config`` hides it and readers stay
    on the last complete build instead of being served a broken one.  It also
    runs before ``cleanup_old``, so the previous build survives for
    comparison.

    WHAT THIS DOES NOT CATCH: both checks test whether the index agrees with
    ITSELF, not whether it agrees with the source.  An index can be perfectly
    self-consistent and still be missing data.  Measured: on the ESP32 project, a
    coverage purge that deleted 145 files and 6698 symbols — the entire C++
    standard library — passed both checks, because the deletes were clean and
    fired the FTS triggers.  Completeness against the sources is a different
    question, answered by the manifest and by the per-file hashes, not here.

    TWO CONSEQUENCES OF THE DB-WIDE SCOPE, both deliberate and both a cost:

    - ``PRAGMA foreign_key_check`` reads the WHOLE database, not the current
      config_hash.  The pragma takes a table name at most, so it cannot be
      scoped to one build.  On a multi-build project a violation left by
      ANOTHER build therefore fails a build that is itself correct.  The
      error message says so; real scoping is an open question, not something
      this function can do.
    - When this raises, the pipeline stops here, so ``finalize_manifest`` is
      skipped — which is the intent — but ``cleanup_old`` and
      ``wal_checkpoint`` are skipped too.  The WAL stays without a
      checkpoint and ``PRAGMA user_version`` is never set, and the next
      reader sees that as ``reindex_needed`` because of the schema version.
    """
    problems: list[str] = []

    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        # Row shape: (child table, child rowid, parent table, fkid).
        preview = "; ".join(
            f"{r[0]} rowid={r[1]} -> missing {r[2]}" for r in violations[:5]
        )
        if len(violations) > 5:
            preview += f"; … (+{len(violations) - 5} more)"
        problems.append(f"{len(violations)} foreign key violation(s): {preview}")

    # The FTS half only means something once the index has been rebuilt.
    # A multi-build project defers that rebuild: the CLI passes
    # defer_fts=True for every (variant, image) run and rebuilds once at the
    # end, so checking here would compare the content tables against an index
    # that nothing has updated yet.  Measured on the Zephyr project (two
    # variants): every build failed verification and therefore never reached
    # finalize_manifest, leaving both stamped "indexing" — hidden from
    # readers.  The caller that owns the deferral runs this check after its
    # rebuild instead.
    if ctx.get("defer_fts"):
        log.debug("FTS integrity check deferred with the FTS rebuild")
    else:
        problems.extend(_fts_inconsistencies(conn))

    if problems:
        for p in problems:
            log.error("Index integrity: %s", p)
        raise IndexIntegrityError(
            f"index verification failed for config_hash="
            f"{ctx['config_hash'][:12]}: " + "; ".join(problems)
            + " (the foreign-key check reads the whole database, so the "
            "violation can belong to a different build of this project)"
        )


def _step_finalize_manifest(conn: sqlite3.Connection, ctx: dict) -> None:
    """Stamp the build config with manifest verification status.

    The ``manifest_verification`` field records how complete the index is:

    - ``"full"`` — manifest.json exists and no critical steps failed;
      all data (symbols, embeddings, FTS) is consistent.
    - ``"partial"`` — manifest exists but critical steps (FTS,
      embeddings) failed; the index is usable but some features are
      degraded.
    - ``"none"`` — no manifest.json was written (e.g. build system
      detection failed); the index cannot support incremental updates.
    """
    from .manifest import _manifest_path

    manifest_path = _manifest_path(ctx["db_dir"], ctx["config_hash"])
    manifest_verification: str
    if manifest_path.exists():
        failed = ctx.get("failed_critical", set())
        manifest_verification = "partial" if failed else "full"
    else:
        manifest_verification = "none"
    with transaction(conn):
        upsert_build_config(
            conn, ctx["config_hash"], ctx["project_id"],
            str(ctx["compile_commands"]),
            description=ctx["git_description"],
            manifest_verification=manifest_verification,
            analyze_vendor=int(ctx["analyze_vendor"]),
            variant=ctx.get("variant", ""),
            image=ctx.get("image", ""),
            board=ctx.get("board", ""),
        )


def _cleanup_old_for_pair(
    conn: sqlite3.Connection,
    project_id: str,
    db_dir: Path,
    variant: str,
    image: str,
    keep_hash: str,
) -> list[str]:
    """Delete older builds of one ``(variant, image)`` pair, keeping *keep_hash*.

    Returns the list of deleted ``config_hash`` values.  Also removes the
    debug artifacts ``<db_dir>/<project_id>/compile_commands.<hash>.json``
    so no orphaned files survive retention.
    """
    old_rows = conn.execute(
        """SELECT config_hash FROM build_configs
           WHERE project_id = ? AND variant = ? AND image = ? AND config_hash != ?
           ORDER BY created_at DESC, rowid DESC""",
        (project_id, variant, image, keep_hash),
    ).fetchall()
    deleted: list[str] = []
    for row in old_rows:
        old_ch = row["config_hash"]
        with transaction(conn):
            delete_build_data(conn, old_ch)
        deleted.append(old_ch)

    # Both on-disk artifacts of a retired build go with its rows.  The
    # manifest used to be left behind: nothing reads an abandoned build's
    # manifest since the reuse tier was removed, so it was pure accumulation —
    # one file per dialect change, 52 MB of it on the Mbed project, for the life of
    # the project.  It also made load(db_dir) ambiguous, since that form picks
    # the most recently modified manifest in the directory.
    from .manifest import _manifest_path

    cc_dir = db_dir / project_id
    for old_ch in deleted:
        for artifact in (
            _manifest_path(db_dir, old_ch),
            cc_dir / f"compile_commands.{old_ch}.json",
        ):
            try:
                artifact.unlink(missing_ok=True)
            except OSError as exc:
                log.debug("could not remove %s: %s", artifact.name, exc)
    return deleted


def cleanup_old_builds_multi(
    conn: sqlite3.Connection,
    project_id: str,
    db_dir: Path,
    touched_pairs: list[tuple[str, str]],
) -> int:
    """Run per-``(variant, image)`` retention after a multi-build run.

    Keeps the newest ``config_hash`` per touched ``(variant, image)`` pair and
    deletes older builds of those pairs ONLY.  Builds of untouched pairs are
    left alone — a narrowed ``--variant`` run must not delete other variants.

    Returns the number of deleted builds.
    """
    if PidFile.is_active_other(db_dir / "reindex.pause"):
        return 0
    deleted = 0
    for variant, image in dict.fromkeys(touched_pairs):
        row = conn.execute(
            """SELECT config_hash FROM build_configs
               WHERE project_id = ? AND variant = ? AND image = ?
               ORDER BY created_at DESC, rowid DESC LIMIT 1""",
            (project_id, variant, image),
        ).fetchone()
        if row is None:
            continue
        deleted += len(
            _cleanup_old_for_pair(conn, project_id, db_dir, variant, image, row["config_hash"])
        )
    return deleted


def _step_cleanup_old_builds(conn: sqlite3.Connection, ctx: dict) -> None:
    """Delete older builds of the current ``(variant, image)`` pair.

    Each reindex of the same (variant, image) produces a new ``config_hash`` —
    the old hash's data is deleted to reclaim disk space.  Retention is scoped
    to the pair: builds of OTHER variants/images are preserved (multi-build).

    The reindex-pause guard prevents deletion when ANOTHER process paused a
    background reindex mid-stream: that process's data is still the active
    index.  It must test ``is_active_other``, not ``is_active`` — ``fw-context
    index`` writes ``reindex.pause`` with its own PID for the whole run, so
    the plain liveness check is always true here and retention never ran at
    all: every reindex left its predecessor's build in the database forever.
    """
    config_hash = ctx["config_hash"]
    project_id = ctx["project_id"]
    db_dir = ctx["db_dir"]
    variant = ctx.get("variant", "")
    image = ctx.get("image", "")

    if PidFile.is_active_other(db_dir / "reindex.pause"):
        return

    _cleanup_old_for_pair(conn, project_id, db_dir, variant, image, config_hash)


def _step_wal_checkpoint(conn: sqlite3.Connection, ctx: dict) -> None:
    """Run a passive WAL checkpoint and stamp the schema version.

    SQLite WAL (Write-Ahead Log) accumulates changes during indexing.
    Without a checkpoint, the WAL file grows without bound — every
    ``INSERT/UPDATE/DELETE`` appends to it.  PASSIVE mode merges WAL
    pages into the main database file without blocking concurrent
    readers, so the server can serve queries during checkpoint.

    The ``user_version`` pragma stamps the schema version into the
    database header so the startup code can detect schema mismatches
    (stale indexes from older fw-context versions) and trigger a
    reindex automatically.
    """
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
#
# Ordering constraints (why steps are in this exact order):
#  • purge_missing MUST run before fts5 — FTS must not index ghost symbols.
#  • coverage_purge MUST run after manifest — it needs the current header
#    lists to know which files the build still includes.  It runs after fts5
#    rather than before, which is safe: its deletes fire the symbols_ad /
#    files_ad / macros_ad triggers, so the FTS indexes follow.
#  • fts5 MUST run before embeddings and llm_analysis — both query FTS.
#  • is_project MUST run before embeddings — embedding source selection
#    depends on project/vendor classification.
#  • llm_analysis MUST run before embeddings — the embedding content hash
#    includes the LLM summary, so summaries must exist before embeddings are
#    hashed.  Otherwise the first run stores a hash without summaries, the
#    analysis step fills them, and the NEXT run re-embeds every analyzed
#    symbol (a spurious full re-embed on every run that follows analysis).
#  • dispatch_edges MUST run before cross_tu_refs — dispatch edges add
#    new from_usr values that the backfill needs.
#  • cross_tu_refs MUST run before pagerank_hotspot — PageRank needs the
#    complete call graph.
#  • verify_integrity MUST run before finalize_manifest and cleanup_old.
#    Before finalize so a failing index is never stamped "full"; before
#    cleanup so the previous build is still there to fall back on.
#  • cleanup_old runs LAST with data mutation — it deletes old-build tables
#    that earlier steps might reference.
#  • wal_checkpoint runs LAST — flushes all accumulated changes to disk.
def _step_reconcile_generated(conn: sqlite3.Connection, ctx: dict) -> None:
    """Make ``files.generated`` agree with THIS run's build_dir_patterns.

    upsert_file() takes MAX so that the one caller who knows the patterns
    cannot be overwritten by the four who do not.  That is right inside a
    run and wrong across two: a row can only gain the flag, never lose it.

    Its docstring used to say the case could not arise, because a change to
    build_dir_patterns mints a new config_hash.  Measured, that is false:
    narrowing PlatformIO from ``.pio/`` to ``.pio/build/`` left the
    config_hash of the ESP32 project and of the STM32 project byte for byte identical, because the
    path pass drops in-project paths anyway.  57 rows would have kept a flag
    the manifest no longer gives them, and the structural check that compares
    the two would fail on every existing index until a --force.

    So the run reconciles instead of hoping.  This is the authoritative
    write: the patterns of the run decide, in both directions, and the step
    is idempotent.  It runs after the manifest is written, so the two
    describe the same boundary.
    """
    build_dir_patterns = ctx.get("build_dir_patterns")
    config_hash = ctx["config_hash"]

    rows = conn.execute(
        "SELECT path, generated FROM files WHERE config_hash = ?", (config_hash,)
    ).fetchall()

    to_set: list[str] = []
    to_clear: list[str] = []
    for row in rows:
        want = _is_generated_header(row["path"], build_dir_patterns)
        if want and not row["generated"]:
            to_set.append(row["path"])
        elif not want and row["generated"]:
            to_clear.append(row["path"])

    for value, paths in ((1, to_set), (0, to_clear)):
        for batch in chunked(paths):
            placeholders = ",".join("?" * len(batch))
            conn.execute(
                f"UPDATE files SET generated = ? "  # noqa: S608 — placeholders only
                f"WHERE config_hash = ? AND path IN ({placeholders})",
                (value, config_hash, *batch),
            )
    if to_set or to_clear:
        conn.commit()
        log.info(
            "files.generated reconciled: %d set, %d cleared",
            len(to_set), len(to_clear),
        )


_STEPS: list[tuple[str, Callable[..., None], Callable[..., bool] | None]] = [
    ("purge_missing",    _step_purge_missing_files, None),
    ("fts5",             _step_rebuild_fts,       None),
    ("orphans",          _step_orphan_cleanup,     None),
    ("is_project",       _step_align_is_project,   None),
    ("manifest",         _step_update_manifest,    None),
    ("generated_flag",   _step_reconcile_generated, None),
    ("coverage_purge",   _step_purge_files_outside_build, None),
    ("macros",           _step_expand_macros,      lambda c: c["index_macros_expanded"] and c["units"]),
    ("dispatch_edges",   _step_resolve_dispatches,  lambda c: c["index_refs"]),
    ("llm_analysis",     _step_llm_analysis,       lambda c: c["analyze_symbols"] and c["llm_config"] is not None and c["llm_config"].enabled),
    ("embeddings",       _step_build_embeddings,   lambda c: c["index_embeddings"] and c["llm_config"] is not None and c["llm_config"].enabled),
    ("overrides",        _step_build_overrides,    lambda c: c["analyze_overrides"]),
    ("cross_tu_refs",    _step_backfill_cross_tu_refs, lambda c: c["index_refs"]),
    ("pagerank_hotspot", _step_pagerank_hotspot,   lambda c: c["index_refs"]),
    ("verify_integrity", _step_verify_integrity,   None),
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
    asm_paths: set[str] | None = None,
    index_refs: bool,
    index_embeddings: bool,
    index_macros_expanded: bool,
    analyze_symbols: bool,
    analyze_overrides: bool,
    analyze_vendor: bool,
    llm_config=None,
    cache_server_config=None,
    force: bool = False,
    purge_max_missing_percent: int = 20,
    variant: str = "",
    image: str = "",
    board: str = "",
    scope: list[str] | None = None,
    defer_fts: bool = False,
    defer_cleanup: bool = False,
    header_hash_cache: dict[str, str] | None = None,
    reparsed_tus: set[str] | None = None,
) -> None:
    """Run all post-processing phases via a data-driven pipeline.

    Each step in ``_STEPS`` is executed in order.  Conditional steps
    are skipped when their guard returns ``False``.  Runtime errors in
    individual steps are logged (not fatal) so the remaining steps
    always execute.

    **Error classification:**  Steps are divided into *critical* and
    *non-critical*.  Critical steps (FTS5, embeddings) are core features
    — if they fail, the index is marked ``"partial"`` in the manifest
    so the server can report degraded functionality (e.g. "semantic
    search is unavailable") instead of serving a silently-broken index.
    Non-critical step failures are logged at WARNING level and do not
    affect the manifest status.
    """
    ctx = {
        "config_hash": config_hash,
        "project_root": project_root,
        "db_dir": db_dir,
        "units": units,
        "asm_paths": asm_paths or set(),
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
        "purge_max_missing_percent": purge_max_missing_percent,
        "variant": variant,
        "image": image,
        "board": board,
        "scope": scope,
        "defer_fts": defer_fts,
        "defer_cleanup": defer_cleanup,
        # Header hashes already computed by the runner's staleness pre-pass —
        # reused by the mtime refresh so no project header is read twice.
        "header_hash_cache": header_hash_cache,
        # TUs re-parsed in this run — only they may refresh their manifest entry.
        "reparsed_tus": reparsed_tus,
    }

    defer_skip: set[str] = set()
    if defer_fts:
        defer_skip.add("fts5")
    if defer_cleanup:
        defer_skip.add("cleanup_old")

    failed_critical: set[str] = set()
    critical_steps = {"fts5", "embeddings"}

    for step_name, step_fn, guard in _STEPS:
        if step_name in defer_skip:
            continue
        if guard is not None and not guard(ctx):
            continue
        t0 = time.monotonic()
        try:
            step_fn(conn, ctx)
            elapsed = time.monotonic() - t0
            log.info("Post-process: %s (%.1fs)", step_name, elapsed)
        except SAFE_EXCEPT as e:
            if is_fatal(e):
                raise
            if step_name in critical_steps:
                failed_critical.add(step_name)
                log.error("Postprocess CRITICAL step %s failed: %s", step_name, e)
            else:
                log.warning("Postprocess step %s failed: %s", step_name, e)

    ctx["failed_critical"] = failed_critical

