"""Call-graph analysis — call paths, recursive callers/callees, dead code, hotspots.

This module performs graph-traversal queries on the ``refs`` table to answer:

1. **Call paths** — "How does A reach B?" (bidirectional BFS).
2. **Transitive callers** — "Who calls X, directly or indirectly?" (recursive CTE).
3. **Transitive callees** — "What does X call, directly or indirectly?" (recursive CTE).
4. **Dead code** — "Which functions are defined but never called?" (anti-join).
5. **Hotspots** — "Which functions have the most callers?" (aggregation + cache).

── Query strategy overview ──

**BFS vs Recursive CTE for path finding**

``find_call_path()`` uses Python BFS with in-memory caches instead of
a SQL recursive CTE.  The refs table can contain 1M+ edges for a typical
firmware project.  A recursive CTE over that many edges risks exponential
expansion because SQLite cannot apply cycle detection to recursive joins.
Python BFS with a visited set (``seen`` dict) guarantees O(V+E) traversal
with explicit depth and node-expansion limits.

The bidirectional variant was added when unidirectional BFS hit the
depth limit on wide dispatch patterns.  Expanding forward from source
AND reverse from target simultaneously halves the effective search depth
(O(b^(d/2)) instead of O(b^d)), so large fan-out nodes like
``dispatch_forever`` (with thousands of event handlers) no longer
dominate the search.

**Recursive CTE for transitive queries**

``find_all_callers_recursive()`` and ``find_callees_recursive()`` use
SQL recursive CTEs.  These are correct here because:
- The search is one-sided (only outgoing edges OR only incoming edges).
- The depth limit is small (default 5).
- MIN(depth) GROUP BY deduplicates at each level, preventing exponential
  re-expansion of nodes reached through multiple paths.

**Weak-alias bridging**

C/C++ weak aliases (``__attribute__((weak, alias("__func")))``) create
symbols that appear as declarations in the index but whose bodies exist
under a different USR (the ``__``-prefixed definition).  Without alias
bridging, callers and callees of the alias declaration would appear empty.
The ``_get_alias_pairs()`` function detects these pairs and ``_build_alias_temp_table()``
injects synthetic edges into the query, so graph traversal passes through
aliases transparently.

**Global constructor bridging**

File-scope C++ global objects call constructors before ``main()``.
These constructor calls are recorded as ``ref_kind='implicit_construct'``
with ``from_usr=NULL``.  A synthetic ``<global ctors>`` node is injected
between ``main`` and all implicit constructors, making them reachable in
any call-path query that can reach ``main``.

── Edge types ──

- ``call`` — direct function call (``foo()``).
- ``indirect`` — function pointer assignment (``driver.onData = &handler``).
- ``implicit_construct`` — global/static object constructor.
- ``dispatch`` — synthetic edge through event loop / thread start.
"""

from __future__ import annotations

import logging
import sqlite3
from collections import deque

from fw_context_mcp.utils import escape_like as _escape_like

log = logging.getLogger(__name__)

__all__ = [
    "find_all_callers_recursive",
    "find_call_path",
    "find_callees_recursive",
    "find_dead_code",
    "find_hotspots",
    "get_vector_table",
]

# ---------------------------------------------------------------------------
# Graph analytics — call-graph traversal via recursive CTE
# ---------------------------------------------------------------------------


def _resolve_target_usr(conn: sqlite3.Connection, config_hash: str, name: str) -> str | None:
    """Look up the USR of a symbol by name, preferring the most-referenced variant.

    When multiple USRs exist for the same name — e.g. C++ inline functions
    with ``#*1C.#`` ABI tags in different TUs — the system must pick one.
    Picking arbitrarily would produce inconsistent call-graph results:
    some queries would traverse edges to one variant, others to another,
    and paths would break.

    Strategy: pick the variant with the most incoming references (``to_usr``
    count) and outgoing references (``from_usr`` count), preferring
    ``is_definition = 1``.  This chooses the variant that is actually called
    throughout the codebase, not an incidental declaration in a header.

    Why correlated subqueries with LIMIT 1 over LEFT JOIN + GROUP BY:
    SQLite's query planner short-circuits after the first matching row
    in a correlated subquery, avoiding a full scan of the refs table.
    A JOIN + GROUP BY would materialize all rows before aggregating.
    """
    esc_name = _escape_like(name)
    suffix_pattern = f"%::{esc_name}"
    plain_name = name.rsplit("::", 1)[-1] if "::" in name else name
    try:
        rows = conn.execute(
            """SELECT s.usr,
                      (SELECT COUNT(*) FROM refs r
                       WHERE r.to_usr = s.usr AND r.config_hash = s.config_hash) AS ref_count,
                      (SELECT COUNT(*) FROM refs r
                       WHERE r.from_usr = s.usr AND r.config_hash = s.config_hash) AS out_count
               FROM symbols s
               WHERE s.config_hash = ?
                 AND (s.name = ? OR s.qualified_name = ? OR s.qualified_name LIKE ? ESCAPE '\\'
                      OR s.name = ?)
               ORDER BY s.is_definition DESC, ref_count DESC, out_count DESC
               LIMIT 1""",
            (config_hash, name, name, suffix_pattern, plain_name),
        ).fetchone()
    except sqlite3.Error:
        log.exception("_resolve_target_usr failed for '%s'", name)
        return None
    return rows["usr"] if rows else None


# Cache for _get_alias_pairs — module-level dict keyed by id(conn).
# pysqlite3.dbapi2.Connection objects lack __dict__ for attribute
# storage, so we cannot attach _alias_cache to the connection directly.
# A module-level dict with connection ID keys works because:
#  - Connection objects are created per request and live for seconds.
#  - id(conn) is stable for the connection's lifetime.
#  - The 16-entry eviction limit prevents unbounded growth from
#    orphaned connections (Python GC does not notify us when a
#    connection is garbage-collected).
_alias_cache_global: dict[int, dict[str, list[tuple[str, str]]]] = {}


def _get_alias_pairs(conn: sqlite3.Connection, config_hash: str) -> list[tuple[str, str]]:
    """Find weak-alias declaration → definition pairs in the index.

    Detects the ``__attribute__((weak, alias("__func")))`` pattern by
    finding declaration-only symbols that have a ``__``-prefixed sibling
    definition with the same parameter count.

    Why parameter-count matching over exact signature comparison:
    alias declarations often omit parameter names or use abbreviated
    types in headers, while definitions have full signatures.  Parameter
    count is a robust heuristic that avoids false negatives from
    whitespace or qualifier differences.

    Why cached per-connection: alias pairs are invariant during a
    connection's lifetime — they are derived from the static symbol
    table.  Recomputing them for every call-graph query would waste
    2-4ms per query scanning the symbols table.

    Why separate per-connection cache instead of a global cache keyed
    by config_hash: different connections may access databases from
    different points in time (e.g., after a reindex).  A global cache
    would serve stale data.
    """
    per_conn_cache = _alias_cache_global.get(id(conn))
    if per_conn_cache is not None:
        cached = per_conn_cache.get(config_hash)
        if cached is not None:
            return cached
    try:
        rows = conn.execute(
            """SELECT s.usr, s.name, s.signature
               FROM symbols s
               WHERE s.config_hash = ?
                 AND s.is_definition = 0
                 AND s.kind IN ('function', 'method', 'constructor', 'destructor')
                 AND EXISTS (SELECT 1 FROM refs r WHERE r.to_usr = s.usr AND r.config_hash = ? LIMIT 1)
                 AND NOT EXISTS (SELECT 1 FROM refs r WHERE r.from_usr = s.usr AND r.config_hash = ? LIMIT 1)
            """,
            (config_hash, config_hash, config_hash),
        ).fetchall()
    except sqlite3.Error:
        log.exception("_get_alias_pairs failed")
        return []

    if not rows:
        return []

    # Build index of __-prefixed definitions by (name, param_count)
    try:
        def_rows = conn.execute(
            """SELECT usr, name, signature FROM symbols
               WHERE config_hash = ? AND is_definition = 1 AND name LIKE '__%'
            """,
            (config_hash,),
        ).fetchall()
    except sqlite3.Error:
        log.exception("_get_alias_pairs def_rows failed")
        return []

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
    conn_id = id(conn)
    if conn_id not in _alias_cache_global:
        if len(_alias_cache_global) > 16:  # evict stale entries from closed connections
            _alias_cache_global.clear()
        _alias_cache_global[conn_id] = {}
    _alias_cache_global[conn_id][config_hash] = pairs
    return pairs


def _build_alias_temp_table(
    conn: sqlite3.Connection, alias_pairs: list[tuple[str, str]]
) -> tuple[str, list[str]]:
    """Populate a temporary table with alias pairs and return a CTE fragment.

    Why a temp table over a VALUES clause: SQLite's
    ``SQLITE_LIMIT_COMPOUND_SELECT`` (default 500) caps the number of
    rows in a VALUES clause.  Large firmware projects can have 1000+
    weak-alias pairs (e.g., STM32 HAL with per-peripheral weak aliases).
    A temp table has no such limit.

    Returns ``(cte_fragment, [])`` where *cte_fragment* is a CTE
    definition string, or ``""`` when *alias_pairs* is empty.
    The empty-string case avoids injecting a CTE with zero rows,
    which would still add parsing overhead to the SQL statement.
    """
    if not alias_pairs:
        return "", []
    conn.execute(
        "CREATE TEMP TABLE IF NOT EXISTS _alias_pairs(decl_usr TEXT, def_usr TEXT)"
    )
    conn.execute("DELETE FROM _alias_pairs")
    conn.executemany("INSERT INTO _alias_pairs VALUES (?, ?)", alias_pairs)
    return (
        "alias_pairs(decl_usr, def_usr) AS (SELECT * FROM _alias_pairs),",
        [],
    )




def find_call_path(
    conn: sqlite3.Connection,
    config_hash: str,
    from_name: str,
    to_name: str,
    max_depth: int = 10,
    max_nodes: int = 5000,
) -> list[dict]:
    """Find call paths from *from_name* to *to_name* via bidirectional BFS.

    Uses Python BFS with cycle detection instead of a SQL recursive CTE.
    The refs table can contain 1M+ edges — a recursive CTE over that many
    rows risks exponential expansion and cannot be bounded by a node limit.

    **Bidirectional BFS** expands forward from *from_name* AND reverse from
    *to_name* simultaneously.  When the two search fronts meet, the path
    is reconstructed.  This halves the effective search depth from O(b^d)
    to O(b^(d/2)), making wide dispatch patterns tractable.

    **Multi-USR source expansion**: *from_name* may resolve to multiple
    USRs (e.g., a function declared in multiple TUs).  All variants are
    used as forward BFS seeds — edges from ANY variant are valid.  This
    avoids missing paths that pass through TU-specific USRs.

    **Lazy edge caching** avoids loading the entire refs table into memory.
    Edges for a USR are fetched on first access and cached per-call.
    For a typical 5-depth BFS, this loads ~500-2000 edges instead of the
    full 1M+ row refs table.

    **max_nodes** prevents runaway queries on pathological call graphs
    (e.g., a global dispatch loop with thousands of registered handlers).

    Args:
        conn: SQLite connection.
        config_hash: Build configuration hash.
        from_name: Source symbol name or qualified name.
        to_name: Target symbol name or qualified name.
        max_depth: Maximum call path depth (default 10).
        max_nodes: Maximum total BFS node expansions (default 5000).

    Returns:
        Up to 5 paths, each with ``depth`` (edge count), ``chain``
        (``A → B → C`` string), and ``target_usr``.
    """
    from_usr = _resolve_target_usr(conn, config_hash, from_name)
    to_usr = _resolve_target_usr(conn, config_hash, to_name)
    if not from_usr or not to_usr:
        return []

    # ── Resolve ALL USRs for from_name (there can be multiple due to TU variants) ──
    esc_name = _escape_like(from_name)
    suffix_pattern = f"%::{esc_name}"
    plain_name = from_name.rsplit("::", 1)[-1] if "::" in from_name else from_name
    try:
        from_usr_rows = conn.execute(
            """SELECT s.usr FROM symbols s
               WHERE s.config_hash = ?
                 AND (s.name = ? OR s.qualified_name = ? OR s.qualified_name LIKE ? ESCAPE '\\'
                      OR s.name = ?)
               ORDER BY s.is_definition DESC""",
            (config_hash, from_name, from_name, suffix_pattern, plain_name),
        ).fetchall()
    except sqlite3.Error:
        log.exception("Multi-USR resolution failed for '%s'", from_name)
        return []
    from_usr_set: set[str] = {r["usr"] for r in from_usr_rows}
    if not from_usr_set:
        return []

    # ── Lazy edge / name caches ──────────────────────────────────────
    # Avoid loading all 1M+ refs into memory.  Each USR's edges are
    # fetched on first access and cached per-call.  The _get_edges and
    # _get_in_edges helper functions encapsulate this caching pattern.
    # GROUP BY r.to_usr (or r.from_usr) deduplicates multiple refs to
    # the same target (e.g., a function called on multiple lines).
    _edge_cache: dict[str, list[tuple[str, str]]] = {}
    _in_edge_cache: dict[str, list[tuple[str, str]]] = {}
    _name_cache: dict[str, str] = {}
    # USR variants for from_name — edges from ANY variant are valid
    _from_variants: set[str] = from_usr_set

    _GLOBAL_CTORS_USR = "$$__global_ctors__"

    def _get_name(usr: str) -> str:
        """Resolve USR → name, preferring definitions.  Cached per call."""
        if usr == _GLOBAL_CTORS_USR:
            return "<global ctors>"
        if usr not in _name_cache:
            try:
                row = conn.execute(
                    """SELECT name FROM symbols
                       WHERE config_hash = ? AND usr = ?
                       ORDER BY is_definition DESC LIMIT 1""",
                    (config_hash, usr),
                ).fetchone()
                _name_cache[usr] = row["name"] if row else "?"
            except sqlite3.Error:
                log.exception("_get_name failed for USR '%s'", usr)
                _name_cache[usr] = "?"
        return _name_cache[usr]

    def _get_edges(usr: str) -> list[tuple[str, str]]:
        """Return [(to_usr, callee_name), ...] for *usr*.  Cached per call."""
        if usr not in _edge_cache:
            try:
                rows = conn.execute(
                    """SELECT r.to_usr,
                              COALESCE(
                                  (SELECT name FROM symbols s
                                   WHERE s.usr = r.to_usr AND s.config_hash = r.config_hash
                                     AND s.is_definition = 1 LIMIT 1),
                                  (SELECT name FROM symbols s
                                   WHERE s.usr = r.to_usr AND s.config_hash = r.config_hash LIMIT 1),
                                  '?'
                              ) AS callee_name
                       FROM refs r
                       WHERE r.config_hash = ? AND r.from_usr = ?
                         AND r.ref_kind IN ('call', 'indirect', 'implicit_construct', 'dispatch')
                       GROUP BY r.to_usr""",
                    (config_hash, usr),
                ).fetchall()
                _edge_cache[usr] = [(r["to_usr"], r["callee_name"]) for r in rows]
            except sqlite3.Error:
                log.exception("_get_edges failed for USR '%s'", usr)
                _edge_cache[usr] = []
        return _edge_cache[usr]

    def _get_in_edges(usr: str) -> list[tuple[str, str]]:
        """Return [(from_usr, caller_name), ...] for *usr* — who calls it.  Cached per call."""
        if usr not in _in_edge_cache:
            try:
                rows = conn.execute(
                    """SELECT r.from_usr,
                              COALESCE(
                                  (SELECT name FROM symbols s
                                   WHERE s.usr = r.from_usr AND s.config_hash = r.config_hash
                                     AND s.is_definition = 1 LIMIT 1),
                                  (SELECT name FROM symbols s
                                   WHERE s.usr = r.from_usr AND s.config_hash = r.config_hash LIMIT 1),
                                  '?'
                              ) AS caller_name
                       FROM refs r
                       WHERE r.config_hash = ? AND r.to_usr = ?
                         AND r.from_usr IS NOT NULL
                         AND r.ref_kind IN ('call', 'indirect', 'implicit_construct', 'dispatch')
                       GROUP BY r.from_usr""",
                    (config_hash, usr),
                ).fetchall()
                _in_edge_cache[usr] = [(r["from_usr"], r["caller_name"]) for r in rows]
            except sqlite3.Error:
                log.exception("_get_in_edges failed for USR '%s'", usr)
                _in_edge_cache[usr] = []
        return _in_edge_cache[usr]

    def _get_edges_multi(usrs: set[str]) -> list[tuple[str, str]]:
        """Return all outgoing edges from ANY of the given USRs."""
        edges: list[tuple[str, str]] = []
        seen: set[str] = set()
        for u in usrs:
            for to_usr, name in _get_edges(u):
                if to_usr not in seen:
                    seen.add(to_usr)
                    edges.append((to_usr, name))
        return edges

    # ── Weak-alias bridging ─────────────────────────────────────────
    # Inject synthetic edges from alias declarations → their definitions
    # into _edge_cache.  Without this, a call path that passes through
    # a weak alias (e.g., HAL_UART_Init → __HAL_UART_Init) would be
    # invisible — the declaration USR has no outgoing edges.
    try:
        alias_pairs = _get_alias_pairs(conn, config_hash)
        for decl_usr, def_usr in alias_pairs:
            label = _get_name(def_usr)
            _edge_cache.setdefault(decl_usr, []).append((def_usr, label))
    except sqlite3.Error:
        log.exception("Alias pair resolution failed")

    # ── Global constructor bridging ─────────────────────────────────
    # File-scope C++ global objects call constructors before main().
    # These are recorded as ref_kind='implicit_construct' with
    # from_usr=NULL.  Without bridging, call paths that span global
    # constructors (main → MyClass::MyClass → some_init) would break.
    # A synthetic <global ctors> node is inserted between main and
    # all implicit_construct targets, making them reachable via BFS.
    _main_usr = _resolve_target_usr(conn, config_hash, "main")
    if _main_usr:
        try:
            _ctors_rows = conn.execute(
                """SELECT r.to_usr FROM refs r
                   WHERE r.config_hash = ? AND r.ref_kind = 'implicit_construct' AND r.from_usr IS NULL""",
                (config_hash,),
            ).fetchall()
            if _ctors_rows:
                _edge_cache[_GLOBAL_CTORS_USR] = [
                    (r["to_usr"], _get_name(r["to_usr"])) for r in _ctors_rows
                ]
                _edge_cache.setdefault(_main_usr, []).append(
                    (_GLOBAL_CTORS_USR, "<global ctors>")
                )
        except sqlite3.Error:
            log.exception("Global ctors bridging failed")

    from_name_resolved = _get_name(from_usr)

    # ── Direct edge check (depth 1) — same as old code for fast path ──
    for to_usr_edge, callee_name in _get_edges_multi(_from_variants):
        if to_usr_edge == to_usr:
            return [{"depth": 1, "chain": f"{from_name_resolved} → {callee_name}", "target_usr": to_usr}]

    # ── Bidirectional BFS — expands forward from source AND reverse from target
    # simultaneously.  Halves the effective search depth (O(b^(d/2)) vs O(b^d))
    # so large fan-out nodes (e.g. dispatch_forever with thousands of event
    # handlers) no longer dominate the search. ──
    f_dist: dict[str, int] = {}     # usr → distance from source
    f_chain: dict[str, str] = {}    # usr → chain string from source
    r_dist: dict[str, int] = {}     # usr → distance to target (reverse)
    r_chain: dict[str, str] = {}    # usr → chain string to target (reverse)

    # Seed forward: all USR variants of from_name
    for u in _from_variants:
        if u not in f_dist:
            f_dist[u] = 0
            f_chain[u] = _get_name(u) if u != from_usr else from_name_resolved
    # Seed reverse: target
    to_name_resolved = _get_name(to_usr)
    r_dist[to_usr] = 0
    r_chain[to_usr] = to_name_resolved

    f_queue: deque[str] = deque(f_dist.keys())
    r_queue: deque[str] = deque([to_usr])
    found: list[dict] = []
    depth = 0
    nodes_expanded = 0

    while f_queue and r_queue and depth < max_depth and nodes_expanded < max_nodes:
        depth += 1
        # ── Expand forward (outgoing edges) ──
        for _ in range(len(f_queue)):
            u = f_queue.popleft()
            for v, v_name in _get_edges(u):
                if v in f_dist:
                    continue
                f_dist[v] = depth
                f_chain[v] = f"{f_chain[u]} → {v_name}"
                f_queue.append(v)
                nodes_expanded += 1
                if v in r_dist:
                    # Meeting point — reconstruct full path
                    total_depth = f_dist[v] + r_dist[v]
                    r_parts = r_chain[v].split(" → ")
                    tail = " → ".join(r_parts[1:])  # skip meeting node (already in f_chain)
                    chain = f_chain[v] if not tail else f"{f_chain[v]} → {tail}"
                    found.append({"depth": total_depth, "chain": chain, "target_usr": to_usr})
                    if len(found) >= 5:
                        return found

        # ── Expand reverse (incoming edges) ──
        for _ in range(len(r_queue)):
            u = r_queue.popleft()
            for v, v_name in _get_in_edges(u):
                if v in r_dist:
                    continue
                r_dist[v] = depth
                r_chain[v] = f"{v_name} → {r_chain[u]}"
                r_queue.append(v)
                nodes_expanded += 1
                if v in f_dist:
                    # Meeting point
                    total_depth = f_dist[v] + r_dist[v]
                    r_parts = r_chain[v].split(" → ")
                    tail = " → ".join(r_parts[1:])
                    chain = f_chain[v] if not tail else f"{f_chain[v]} → {tail}"
                    found.append({"depth": total_depth, "chain": chain, "target_usr": to_usr})
                    if len(found) >= 5:
                        return found

    return found



def find_all_callers_recursive(
    conn: sqlite3.Connection,
    config_hash: str,
    name: str,
    max_depth: int = 5,
    limit: int = 50,
) -> list[dict]:
    """Find all transitive callers of *name* via recursive CTE.

    Returns deduplicated callers at depth 1 (direct), depth 2
    (callers of callers), up to *max_depth*.  Each caller appears once
    at its shortest distance — ``MIN(depth) GROUP BY usr``.

    Why recursive CTE over Python BFS: the search radius is small
    (max 5), and SQLite's recursive CTE with a depth bound is efficient
    when the fan-in per node is moderate (<50 callers per function).
    For wide calls (>500 callers), the CTE with GROUP BY naturally
    deduplicates without materializing all intermediate results.

    Why extended_refs with alias UNION ALL: when A calls decl_usr
    (a weak alias) and decl_usr aliases def_usr, A also effectively
    calls def_usr.  The UNION ALL injects these synthetic edges so the
    recursive CTE can traverse past aliases.

    Why COALESCE(s_def.name, s_any.name): some callers may exist only
    as declarations (not definitions) in the index — e.g., an ISR
    declared in a header but defined in assembly.  Falling back to
    s_any ensures the caller still appears in results.
    """
    target_usr = _resolve_target_usr(conn, config_hash, name)
    if not target_usr:
        return []

    # Build extended refs: real refs + synthetic weak-alias edges.
    # When someone calls decl_usr (alias), they also effectively call def_usr.
    alias_pairs = _get_alias_pairs(conn, config_hash)
    alias_cte, alias_params = _build_alias_temp_table(conn, alias_pairs)
    alias_join = (
        """UNION ALL
        SELECT r.from_usr, ap.def_usr
        FROM refs r
        JOIN alias_pairs ap ON r.to_usr = ap.decl_usr
        WHERE r.config_hash = ?"""
        if alias_cte
        else ""
    )

    query = f"""WITH {alias_cte}
        extended_refs(from_usr, to_usr) AS (
            SELECT from_usr, to_usr FROM refs
            WHERE config_hash = ? AND ref_kind IN ('call', 'indirect', 'implicit_construct', 'dispatch')
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
    if alias_cte:
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
    """Find all transitive callees of *name* via recursive CTE.

    Inverse of ``find_all_callers_recursive`` — follows outgoing edges
    from caller to callee.  Same recursive CTE pattern with alias bridging,
    but the alias UNION ALL direction is reversed: when def_usr is the
    real definition that decl_usr aliases, anything def_usr calls should
    also appear as callees of decl_usr.

    Query structure: WITH alias_pairs → extended_refs (real + synthetic
    edges) → recursive callees CTE → dedup with MIN(depth) → join symbols
    for metadata.
    """
    source_usr = _resolve_target_usr(conn, config_hash, name)
    if not source_usr:
        return []

    # Build extended refs: real refs + synthetic weak-alias edges.
    # When decl_usr is an alias for def_usr, anything def_usr calls should
    # also be reachable from decl_usr.
    alias_pairs = _get_alias_pairs(conn, config_hash)
    alias_cte, alias_params = _build_alias_temp_table(conn, alias_pairs)
    alias_join = (
        """UNION ALL
        SELECT ap.decl_usr, r.to_usr
        FROM refs r
        JOIN alias_pairs ap ON r.from_usr = ap.def_usr
        WHERE r.config_hash = ?"""
        if alias_cte
        else ""
    )

    query = f"""WITH {alias_cte}
        extended_refs(from_usr, to_usr) AS (
            SELECT from_usr, to_usr FROM refs
            WHERE config_hash = ? AND ref_kind IN ('call', 'indirect', 'implicit_construct', 'dispatch')
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
    if alias_cte:
        params.append(config_hash)
    params.extend([source_usr, max_depth, config_hash, config_hash, limit])

    rows = conn.execute(query, params).fetchall()

    return [dict(r) for r in rows]


def find_dead_code(
    conn: sqlite3.Connection,
    config_hash: str,
    limit: int = 100,
    exclude_paths: list[str] | None = None,
    project_only: bool = False,
) -> list[dict]:
    """Find functions that are defined but never called, with two confidence levels.

    **Category 1 — "dead"**: the symbol's USR has no entry at all in
    ``refs.to_usr``.  This is a single-layer check, not a reachability
    analysis.  A function called only by another dead function will NOT
    appear here (it has a reference).  Use ``find_callees_recursive()``
    from known entry points for transitive dead-code analysis.

    **Category 2 — "possibly_dead"**: the symbol has ``ref_kind='indirect'``
    entries (function pointer assignments) but no resolved call site via
    the ``fp_assignments → indirect_call_sites`` join.  This means the
    function IS assigned to a function pointer somewhere, but the
    invocation path is unknown — it may be called through unindexed code,
    a type-erased API, or an interrupt vector table entry the indexer
    could not resolve.  Treat these as uncertain.

    Why two categories: flagging all indirect-only targets as "dead" would
    produce false positives for ISR handlers, callback registrations, and
    RTOS task entries — all of which are invoked through function pointers
    and would appear unreachable to a naive direct-call-only check.

    Why ``NOT IN (SELECT DISTINCT to_usr FROM refs)`` over LEFT JOIN +
    IS NULL: the NOT IN anti-join lets SQLite short-circuit on the first
    matching ref row per USR, while LEFT JOIN must scan all rows before
    filtering NULLs.
    """
    project_only_clause = "AND s.is_project = 1" if project_only else ""
    if exclude_paths:
        path_clause = "AND " + " AND ".join("s.file_path NOT LIKE ?" for _ in exclude_paths)
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
             {project_only_clause}
           ORDER BY s.kind, s.name
           LIMIT ?""",
        dead_params,
    ).fetchall()

    results: list[dict] = []
    dead_usr_set: set[str] = set()
    for r in dead_rows:
        dead_usr_set.add(r["usr"])
        results.append(
            {
                "name": r["name"],
                "qualified_name": r["qualified_name"],
                "kind": r["kind"],
                "file_path": r["file_path"],
                "signature": r["signature"],
                "line": r["line"],
                "status": "dead",
                "reason": "no references found — likely unused",
            }
        )

    # Category 2: possibly dead — indirect refs exist but no resolved call site.
    # A symbol is possibly dead when it has ref_kind='indirect' entries
    # (function pointer assignments) but is NOT found as a resolved target
    # via fp_assignments → indirect_call_sites linking.
    remaining_slots = limit - len(results)
    if remaining_slots > 0:
        possibly_params: list[object] = (
            [
                config_hash,
                config_hash,
                config_hash,
                config_hash,
            ]
            + exclude_params
            + [remaining_slots]
        )
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
                 {project_only_clause}
               ORDER BY s.kind, s.name
               LIMIT ?""",
            possibly_params,
        ).fetchall()
        for r in possibly_rows:
            if r["usr"] in dead_usr_set:
                continue
            results.append(
                {
                    "name": r["name"],
                    "qualified_name": r["qualified_name"],
                    "kind": r["kind"],
                    "file_path": r["file_path"],
                    "signature": r["signature"],
                    "line": r["line"],
                    "status": "possibly_dead",
                    "reason": "assigned as function pointer but call sites unresolved",
                    "indirect_refs": r["indirect_sites"] or "",
                }
            )

    return results


def find_hotspots(
    conn: sqlite3.Connection,
    config_hash: str,
    limit: int = 20,
    exclude_paths: list[str] | None = None,
    project_only: bool = False,
) -> list[dict]:
    """Find the most-called functions ranked by caller count.

    Only counts call edges (``ref_kind IN ('call', 'indirect')``) —
    plain references (``ref_kind='ref'``) and member accesses are excluded
    so enum constants and field declarations do not appear as hotspots.

    **Cache strategy**: when ``hotspot_cache`` is populated for this
    config, the function returns instantly from the pre-computed table.
    The cache is built during indexing via a COUNT(*) GROUP BY over refs,
    which takes 2-5 seconds for 1M+ edges.  Serving from cache eliminates
    this latency on every query.

    Falls back to a live COUNT+GROUP BY query when:
    - The cache table has zero rows for this config (first index).
    - The cache was not populated (refs indexed but hotspot_cache skipped).

    Why not always use the live query: 2-5 second latency on every
    ``find_hotspots()`` call is unacceptable for interactive use.
    The cache trades a small write cost at index time for zero-latency
    reads at query time.
    """
    proj_clause = "AND s.is_project = 1" if project_only else ""
    # Try cache first
    cached = conn.execute("SELECT COUNT(*) FROM hotspot_cache WHERE config_hash = ?", (config_hash,)).fetchone()
    if cached and cached[0] > 0:
        path_clauses = ""
        params: list = [config_hash]
        if exclude_paths:
            path_clauses = "AND " + " AND ".join("s.file_path NOT LIKE ?" for _ in exclude_paths)
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
                 {proj_clause}
               ORDER BY h.caller_count DESC
               LIMIT ?""",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    # Live query fallback
    path_clauses = ""
    p: list = [config_hash]
    if exclude_paths:
        path_clauses = "AND " + " AND ".join("s.file_path NOT LIKE ?" for _ in exclude_paths)
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
             {proj_clause}
           GROUP BY s.usr
           ORDER BY caller_count DESC
           LIMIT ?""",
        p,
    ).fetchall()

    return [dict(r) for r in rows]


def get_vector_table(
    conn: sqlite3.Connection,
    config_hash: str,
    unhandled_only: bool = False,
) -> list[dict]:
    """Read the interrupt vector table, one row for each slot.

    A slot is a position in the table, and the hardware reaches it
    directly.  Nothing calls the handler, so the table is the only edge a
    reader has, and the indexer writes one ``refs`` row for each slot with
    ``ref_kind='vector'`` and ``slot_index`` set.

    Each row gets a ``status`` from where its target is defined:

    * ``"c"`` — the target is defined outside assembly.  Code services
      this interrupt.
    * ``"assembly"`` — the target is a strong assembly definition.
      Assembly services this interrupt.
    * ``"unhandled"`` — the target is an alias of another symbol, and
      nothing overrode it.  A CMSIS startup file writes each unserviced
      interrupt as an alias of ``Default_Handler``, which is an infinite
      loop.  The row then holds ``aliases`` with the name, file and line
      of what it really reaches.

    Weakness alone cannot say this, which is why the alias edge exists.
    The same startup declares ``Reset_Handler`` weak WITH a body and
    ``NMI_Handler`` weak with nothing but an alias.
    * ``"linker"`` — the linker script gives the target its address, and no
      compiled file defines it.  Slot 0 holds the initial stack pointer and
      looks like this.  When the index read the script, ``file`` and
      ``line`` name the assignment in it; otherwise the row carries the
      name alone.  NOT code — a reader must not follow this row expecting
      a function.

    ``overridden`` holds the weak definition that a ``"c"`` row replaced,
    when one is in the index.  This is the CMSIS pattern: the startup file
    defines each handler weakly, and the project defines the same name
    again.  The linker keeps the strong one.

    Args:
        conn: An open index database connection.
        config_hash: The build configuration to read.
        unhandled_only: When True, return only the ``"unhandled"`` rows.

    Returns:
        A list of dicts sorted by slot.  Each dict has: slot, name, file,
        line, status, and table_file and table_line for the position in
        the table itself.  A ``"c"`` row can also have overridden, a dict
        with file and line.
    """
    rows = conn.execute(
        """SELECT r.slot_index AS slot,
                  r.from_file  AS table_file,
                  r.from_line  AS table_line,
                  s.name       AS name,
                  s.file_path  AS file,
                  s.line       AS line,
                  s.kind       AS kind,
                  s.is_weak    AS is_weak,
                  s.usr        AS usr
           FROM refs r
           JOIN symbols s ON s.usr = r.to_usr AND s.config_hash = r.config_hash
           WHERE r.config_hash = ? AND r.ref_kind = 'vector'
             AND r.slot_index IS NOT NULL
           ORDER BY r.from_file, r.slot_index""",
        (config_hash,),
    ).fetchall()

    out: list[dict] = []
    for row in rows:
        entry = dict(row)
        entry_usr = str(entry.pop("usr"))
        from_assembly = entry_usr.startswith("asm:")
        weak = bool(entry.pop("is_weak"))
        kind = entry.pop("kind")

        # `linker` covers both shapes of the same fact: the slot reaches a
        # name the linker script gives an address to.
        #
        # `kind == "undefined"` is the older shape — the assembly reader saw
        # the name in a vector slot and nothing in the build defined it.
        # The `ld:` namespace is the newer one: the linker-script pass now
        # reads that script and stores a real definition with a file and a
        # line, so slot 0 of a CMSIS table went from `undefined` to a
        # `varglobal` in `.link_script.ld`.
        #
        # It must NOT fall through to "c".  That status means "a definition
        # outside assembly, code runs", and slot 0 holds the initial stack
        # pointer, not code.  Measured on zbox-ecb-fw: with the fall-through
        # the tool reported `__StackTop` as code that runs.
        if kind == "undefined" or entry_usr.startswith("ld:"):
            entry["status"] = "linker"
        elif not from_assembly:
            entry["status"] = "c"
        else:
            # An alias edge, not weakness, says the target is only
            # another name for something else.  The startup declares
            # Reset_Handler weak WITH a body and NMI_Handler weak with
            # nothing but an alias, so weakness would report the reset
            # vector as unserviced.
            reaches = conn.execute(
                """SELECT t.name, t.file_path AS file, t.line FROM refs r
                   JOIN symbols t ON t.usr = r.to_usr
                                 AND t.config_hash = r.config_hash
                   WHERE r.config_hash = ? AND r.ref_kind = 'alias'
                     AND r.from_usr = ?
                   LIMIT 1""",
                (config_hash, entry_usr),
            ).fetchone()
            if reaches is not None:
                entry["status"] = "unhandled"
                entry["aliases"] = dict(reaches)
            else:
                entry["status"] = "assembly"
        del weak

        if entry["status"] == "c":
            # The weak definition this one replaced, when the index holds
            # it.  A reader asking "who really services this" wants both:
            # the code that runs, and the stub it took the place of.
            replaced = conn.execute(
                """SELECT file_path AS file, line FROM symbols
                   WHERE config_hash = ? AND name = ? AND is_weak = 1
                   LIMIT 1""",
                (config_hash, entry["name"]),
            ).fetchone()
            if replaced is not None:
                entry["overridden"] = dict(replaced)

        if unhandled_only and entry["status"] != "unhandled":
            continue
        out.append(entry)
    return out
