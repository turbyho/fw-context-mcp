"""Call-graph tool suite for MCP server.

Provides the complete call-graph analysis API exposed through MCP tools:

* **Direct reference lookup** — ``find_callers``, ``find_references``
* **Indirect (function pointer) resolution** — ``find_indirect_call_sites``,
  ``find_indirect_targets``
* **Transitive graph traversal** — ``find_all_callers_recursive``,
  ``find_callees_recursive``, ``find_call_path``
* **Structural analysis** — ``find_dead_code``, ``find_hotspots``,
  ``find_wrapper_callers``, ``trace_data_flow``

Design decisions
----------------

**Single-connection executor pattern:** All SQL queries run inside a closure
passed to ``db.executor.execute_sync(query_fn, config_hash)``. The executor
pins a single database connection and serializes access through a lock,
ensuring thread safety without connection pooling overhead. The public handler
functions do not access the database directly — they close over the executor
and delegate.

**Three-tier symbol resolution:** Every reference lookup (callers, references,
indirect targets) resolves the target symbol in three ordered steps:

1. Exact name match in the ``symbols`` table.
2. Exact qualified name match.
3. Suffix LIKE match (``%::name``).

This mirrors libclang's name resolution heuristics and prevents false
matches on similarly-named but unrelated symbols.

**Virtual callers propagation:** libclang resolves ``this->method()`` to the
nearest override in the C++ class hierarchy. When the most-derived override
is indexed but callers are recorded against the nearest (base) override,
``_resolve_virtual_callers`` propagates caller information from peer overrides.

**Macro fallback:** When ``_lookup_definition`` returns None, the reference
functions fall back to the macro table. Raw FTS5 results are filtered to
exclude matches inside C/C++ comments (FTS5 indexes comment content but
only active-code references are meaningful).

**Function pointer phases:** The ``refs`` table stores Phase 1 assignments
(``driver.onData = &handler``). Phase 3 call sites (``driver.onData(buf, len)``)
are stored in the ``indirect_calls`` table. ``find_indirect_targets`` links
them via the field's USR, producing a complete assignment-to-invocation trace.
"""

from __future__ import annotations

import logging
import time
from typing import Annotated

from pydantic import Field

from ...indexer import db as index_db
from ...indexer.db import (
    count_fp_assignments,
    count_indirect_call_sites,
    count_refs,
    find_macro_refs,
    find_refs,
    get_active_config,
    lookup_macro,
)
from ...indexer.db import (
    find_indirect_call_sites as query_indirect_call_sites,
)
from ...indexer.db import (
    find_indirect_targets as query_indirect_targets,
)
from ...utils import abs_path
from ...utils import escape_like as _escape_like
from ._base import BaseHandler, DbContext
from .source import _lookup_definition

log = logging.getLogger(__name__)

def _resolve_virtual_callers(conn, config_hash: str, usr: str, root, ref_kind: list[str] | None,
                            limit: int = 50) -> list[dict] | None:
    """Return callers of sibling override methods when *usr* has no direct callers.

    When libclang resolves ``this->end_download()`` to the nearest override
    (e.g. ``DownloadManagerSD::end_download``), the most-derived override
    (e.g. ``DownloadManagerFlash::end_download``) shows 0 callers in ``refs``.
    This propagates callers from peer overrides to reveal the real call sites.

    Uses two resolution strategies:

    1. **Base-method lookup** — queries the ``overrides`` table to find all
       derived classes that override the same base method. Includes the base
       method itself because libclang may record callers against it.
    2. **Parent-class sibling lookup** — when no entry exists in ``overrides``,
       falls back to finding same-name virtual methods within the same parent
       class. This handles cases where the overrides table was not populated
       (e.g., partial indexing).

    Returns None when the symbol is not a virtual method with overrides,
    or an empty list when overrides exist but also have no callers.
    """
    # Verify the symbol exists and is a method or destructor — virtual
    # dispatch only applies to methods, not free functions or fields.
    sym_row = conn.execute(
        """SELECT s.kind, s.parent_usr
           FROM symbols s
           WHERE s.config_hash = ? AND s.usr = ?
           LIMIT 1""",
        (config_hash, usr),
    ).fetchone()
    if not sym_row:
        return None
    if sym_row["kind"] not in ("method", "destructor"):
        return None

    # Strategy 1: resolve through the overrides table (Phase 1 indexing).
    # The overrides table maps derived_usr → base_usr for every virtual
    # method pair. When the base method has callers, propagate them.
    base = conn.execute(
        """SELECT base_usr FROM overrides
           WHERE derived_usr = ? AND config_hash = ?""",
        (usr, config_hash),
    ).fetchone()

    # Collect all peer overrides that share the same base method.
    # Include the base method itself — callers may be recorded directly
    # against it when libclang resolves the call to the base-level override.
    if base:
        base_usr = base["base_usr"]
        overrides = conn.execute(
            """SELECT derived_usr FROM overrides
               WHERE base_usr = ? AND config_hash = ?
                 AND derived_usr != ?""",
            (base_usr, config_hash, usr),
        ).fetchall()
        all_derived: list[str] = [r["derived_usr"] for r in overrides]
        all_derived.append(base_usr)
    else:
        # Strategy 2: the overrides table has no entry for this symbol.
        # Walk the parent class to find same-name virtual methods.
        # This handles partial indexes where overrides were not computed
        # or the symbol is flagged virtual but not tracked in overrides.
        parent_usr = sym_row["parent_usr"]
        if not parent_usr:
            return None
        siblings = conn.execute(
            """SELECT usr FROM symbols
               WHERE config_hash = ? AND parent_usr = ? AND name = (
                 SELECT name FROM symbols WHERE config_hash = ? AND usr = ?
               )
                 AND usr != ?
                 AND kind IN ('method', 'destructor')
                 AND (is_virtual OR is_pure_virtual)""",
            (config_hash, parent_usr, config_hash, usr, usr),
        ).fetchall()
        all_derived = [r["usr"] for r in siblings]
        if not all_derived:
            return None

    if not all_derived:
        return None

    # Build a UNION query across all peer overrides. A single IN clause
    # is used rather than per-usr UNION ALL because the target set is
    # built programmatically — SQLite handles the IN list efficiently
    # when the placeholder count is small (typically 2–10 values).
    # DISTINCT deduplicates callers that reference multiple peers.
    placeholders = ",".join("?" * len(all_derived))
    kind_clause = ""
    kind_params: list = []
    if ref_kind:
        rk_placeholders = ",".join("?" * len(ref_kind))
        kind_clause = f"AND r.ref_kind IN ({rk_placeholders})"
        kind_params = list(ref_kind)

    caller_rows = conn.execute(
        f"""SELECT DISTINCT r.from_file, r.from_line, r.ref_kind,
                    caller.name AS caller_name,
                    caller.qualified_name AS caller_qname,
                    caller.kind AS caller_kind
             FROM refs r
             LEFT JOIN symbols caller
               ON caller.config_hash = r.config_hash AND caller.usr = r.from_usr
             WHERE r.config_hash = ?
               AND r.to_usr IN ({placeholders})
               {kind_clause}
             LIMIT ?""",
        (config_hash, *all_derived, *kind_params, limit),
    ).fetchall()

    return [
        {
            "file": abs_path(root, r["from_file"]),
            "line": r["from_line"],
            "ref_kind": r["ref_kind"],
            "caller": r["caller_qname"] or r["caller_name"] or "<file scope>",
            "caller_kind": r["caller_kind"],
        }
        for r in caller_rows
    ]


# ── moved from server.py ──
def _references_result(name: str, project_root: str | None, ref_kind: str | list[str] | None, limit: int, *, caller_mode: bool = False, variant: str | None = None, image: str | None = None) -> list[dict]:
    """Shared logic for ``find_callers`` and ``find_references``.

    Resolves the project, opens the DB, looks up the symbol, checks that refs
    are indexed, and returns formatted reference results.

    The two callers differ only in the ``ref_kind`` filter:
    ``find_callers`` passes ``["call", "indirect", "implicit_construct"]``
    while ``find_references`` passes ``None`` (all kinds including reads).

    Args:
        name: Symbol name to find references for.
        project_root: Project root directory.
        ref_kind: Reference kind filter (``["call", "indirect"]`` for callers,
            ``None`` for all kinds).
        limit: Maximum results.
        caller_mode: If True, use "callers" in info messages instead of "references".

    Returns:
        list of dicts, each with: file, line, ref_kind, caller, caller_kind.
    """
    try:
        db = BaseHandler.resolve_db_context(project_root, variant=variant, image=image)
    except RuntimeError as e:
        return [{"error": str(e)}]
    root = db.root

    def _query(conn, config_hash):
        # Runs under the executor lock on the single shared connection;
        # must not open its own connection.  Timeout is enforced by
        # _wrap_tool (300 s + interrupt), not here.
        cfg_data = get_active_config(conn, db.project_id)
        if not cfg_data:
            return [{"error": "No build config indexed."}]
        symbol = _lookup_definition(conn, config_hash, name, preferred_kinds=None)
        if symbol is None:
            # ── Macro fallback ──────────────────────────────────────────
            # The symbol is not a libclang-indexed function/method/type.
            # Fall back to the macros table. FTS5 indexes the entire file
            # content including comments, so matched snippets must be
            # post-filtered to exclude references in C/C++ comments.
            # Only active-code references are meaningful to the caller.
            macros = lookup_macro(conn, config_hash, name, exact=True, limit=1)
            if macros:
                macro = macros[0]
                ref_rows = find_macro_refs(conn, config_hash, name, limit=limit)
                # Post-filter: the FTS5 match snippet is checked for
                # surrounding comment markers. Inline (//) and block (/* */)
                # comments are detected via regex. This is heuristic —
                # false negatives are possible when the comment spans
                # multiple lines and the snippet cuts mid-comment.
                import re as _re
                _comment_refs: list = []
                _non_comment_refs: list = []
                for r in ref_rows:
                    _snip = r["_match_snippet"] or ""
                    if _re.search(r'//[^\n]*<b>', _snip):
                        _comment_refs.append(r)
                    elif _re.search(r'/\*.*<b>', _snip, _re.DOTALL):
                        _comment_refs.append(r)
                    else:
                        _non_comment_refs.append(r)
                macro_def = {
                    "kind": "macro",
                    "name": macro["name"],
                    "file": abs_path(root, macro["file_path"]),
                    "line": macro["line"],
                    "value": macro["value"],
                    **({"expanded_value": macro["expanded_value"]} if macro["expanded_value"] else {}),
                }
                if not _non_comment_refs:
                    label = "callers" if caller_mode else "references"
                    extra: dict = {}
                    if _comment_refs:
                        extra["warning"] = (
                            "All macro uses are in comments — "
                            "no active code references found."
                        )
                    return [{"info": f"No {label} (macro) found for '{name}'.",
                             **macro_def, **extra}]
                macro_refs: list[dict] = [
                    {
                        "file": abs_path(root, r["file_path"]),
                        "ref_kind": "macro_use",
                        "_match_snippet": r["_match_snippet"],
                    }
                    for r in _non_comment_refs
                ]
                macro_refs.insert(0, macro_def)
                return macro_refs
            return [{"error": f"Symbol not found: {name}"}]
        if count_refs(conn, config_hash) == 0:
            return [{"info": (
                "No references indexed. Refs are on by default — "
                "they may have been disabled with [index] index_refs = false. "
                "Re-run 'fw-context index' to rebuild with refs enabled."
            )}]
        clamped_limit = max(0, min(limit, 200))
        rows = find_refs(conn, config_hash, name, ref_kind=ref_kind, limit=clamped_limit)
        if not rows:
            # When zero refs are recorded for this specific symbol, try
            # virtual dispatch resolution. C++ virtual method calls
            # through base-class pointers are often recorded against
            # the nearest (base) override, not the most-derived one.
            virtual_result = _resolve_virtual_callers(
                conn, config_hash, symbol["usr"], root, ref_kind=ref_kind, limit=clamped_limit,
            )
            if virtual_result is not None:
                if not virtual_result:
                    label = "callers" if caller_mode else "references"
                    return [{"info": f"No {label} found for '{name}'."}]
                return virtual_result
            label = "callers" if caller_mode else "references"
            return [{"info": f"No {label} found for '{name}'."}]
        result: list[dict] = [
            {
                "file": abs_path(root, r["from_file"]),
                "line": r["from_line"],
                "ref_kind": r["ref_kind"],
                "caller": r["caller_qname"] or r["caller_name"] or "<file scope>",
                "caller_kind": r["caller_kind"],
            }
            for r in rows
        ]
        return result

    return db.execute_scoped(_query)

# ── moved from server.py ──
def find_callers(
    name: Annotated[str, Field(description="Symbol name to find callers of. Returns direct call sites and indirect calls via function pointers.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    limit: Annotated[int, Field(description="Maximum results.")] = 50,
    variant: Annotated[str | None, Field(description="Build variant name (multi-project). Omit to use default_variant or fail-closed. Use '*' for all variants.")] = None,
    image: Annotated[str | None, Field(description="Sysbuild image name within the variant (multi-project). Omit for all images of the variant.")] = None,
) -> list[dict]:
    """Find who calls a C/C++ function — direct calls AND indirect via
    function pointers, callbacks, interrupt vector registrations, and
    struct init lists. libclang-powered: detects function-pointer
    assignments and ISR vector registrations that text-based search
    cannot see.

    Falls back to macro lookup when the symbol is not found as a
    function/method: returns the macro definition (kind="macro") and
    files that use it (ref_kind="macro_use").

    Use when you need a quick, flat list of immediate callers. For the full
    transitive call tree (who calls this indirectly through other functions),
    use ``find_all_callers_recursive``.  For all references including reads
    and member accesses, use ``find_references``.  For a path between two
    specific symbols, use ``find_call_path``.

    Read-only. No side effects. Requires the reference index
    (``fw-context index`` — refs are on by default).  Only direct call
    sites are returned; callers more than one hop away are not included.

    Indirect edges (``ref_kind: "indirect"``) are detected when a function
    pointer references a function through:

    - **Call arguments**: ``callback(&Class::method, this)``,
      ``EventQueue::call_every(ms, obj, &handler)``
    - **Assignments**: ``driver.onData = &handleData``,
      ``global_cb = &handler``
    - **Variable initializers**: ``static void (*fp)(int) = &handler``
    - **Struct/array init lists**: ``{.on_data = &handler}``,
      ``{&fn_a, &fn_b}``

    Args:
        name: Symbol name to find callers of. Uses the same three-tier
            resolution as ``find_references`` (exact name, exact qualified,
            suffix LIKE).
        project_root: Project root directory. Auto-detected if omitted.
        limit: Maximum results (default 50).
        variant: Build variant (multi-project). Omit for the default
            variant, ``"*"`` for all.
        image: Sysbuild image in the variant. Omit for all images.

    Returns:
        list of dicts, each with: file, line, ref_kind (``"call"``,
        ``"indirect"``, ``"implicit_construct"``, or ``"macro_use"``),
        caller (enclosing function name), caller_kind (``"function"``,
        ``"method"``, …). Macro fallback includes a leading dict with
        ``kind="macro"``, ``value``, and ``expanded_value``.

        Never empty: one dict with ``error`` (symbol not resolved) or
        ``info`` (no references of this kind).  Check both keys first.
    """
    return _references_result(name, project_root, ref_kind=["call", "indirect", "implicit_construct"], limit=limit, caller_mode=True, variant=variant, image=image)

# ── moved from server.py ──
def find_references(
    name: Annotated[str, Field(description="Symbol name to find all references of — calls, reads, member accesses.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    limit: Annotated[int, Field(description="Maximum results.")] = 50,
    variant: Annotated[str | None, Field(description="Build variant name (multi-project). Omit to use default_variant or fail-closed. Use '*' for all variants.")] = None,
    image: Annotated[str | None, Field(description="Sysbuild image name within the variant (multi-project). Omit for all images of the variant.")] = None,
) -> list[dict]:
    """Find ALL references to a C/C++ symbol — calls, reads, member accesses,
    function pointer registrations, template references, and macro
    usages. libclang-powered: detects function-pointer registrations
    (interrupt vector table writes, callback attachments, ISR handler
    assignments) that text-based search cannot see.

    Falls back to macro lookup when the symbol is not found as a
    function/method: returns the macro definition (kind="macro") and
    files that reference it (ref_kind="macro_use").

    Read-only. No side effects. Returns every reference in the indexed codebase,
    including call sites, variable reads, struct member accesses, indirect
    function-pointer references, and macro usages. Requires the reference
    index (``fw-context index`` — refs on by default).

    For direct callers only use ``find_callers``. For transitive callers use
    ``find_all_callers_recursive``. For call paths between two symbols use
    ``find_call_path``.

    Args:
        name: Symbol name to find all references of.
        project_root: Project root directory. Auto-detected if omitted.
        limit: Maximum results (default 50, max 200).
        variant: Build variant (multi-project). Omit for the default
            variant, ``"*"`` for all.
        image: Sysbuild image in the variant. Omit for all images.

    Returns:
        list of dicts, each with: file, line, ref_kind, caller, caller_kind.
        ``ref_kind`` is one of: ``"call"``, ``"ref"``, ``"member"``,
        ``"indirect"`` (function-pointer reference in arguments, assignments,
        initializers, or init lists), ``"implicit_construct"`` (implicit
        constructor call from global/static object or member-field
        initialization), ``"macro_use"`` (macro usage
        in file). Macro fallback includes a leading dict with
        ``kind="macro"``, ``value``, and ``expanded_value``.

        Never empty: one dict with ``error`` (symbol not resolved) or
        ``info`` (no references).  Check both keys first.
    """
    return _references_result(name, project_root, ref_kind=None, limit=limit, variant=variant, image=image)

# ── moved from server.py ──
def find_indirect_call_sites(
    name: Annotated[str, Field(description="Name of the function pointer field or variable to find call sites of. E.g. 'onData' finds all calls through Driver::onData.")],
    project_root: Annotated[str | None, Field(description="Project root directory. Auto-detected if omitted.")] = None,
    limit: Annotated[int, Field(description="Maximum results (default 50).")] = 50,
    variant: Annotated[str | None, Field(description="Build variant name (multi-project). Omit to use default_variant or fail-closed. Use '*' for all variants.")] = None,
    image: Annotated[str | None, Field(description="Sysbuild image name within the variant (multi-project). Omit for all images of the variant.")] = None,
) -> list[dict]:
    """Find indirect call sites where a C/C++ function pointer field or
    variable is invoked. libclang-powered: resolves calls through
    function pointers (e.g. ``driver.onData(buf, len)``), which
    text-based search cannot detect.

    Returns locations where a function pointer is called through a field
    access (``driver.onData(buf, len)``) or variable dereference
    (``stored_callback(42)``).

    Read-only. No side effects. Use this to answer "where is this function
    pointer invoked?" as opposed to ``find_callers`` which answers "who
    calls this function?" and ``find_references`` which answers "where is
    this symbol read or assigned?"

    For the reverse query — which functions are assigned to a given field
    or parameter — use ``find_indirect_targets``.

    Requires the reference index (``fw-context index`` — refs on by default).

    Args:
        name: Name of the function pointer field or variable.
            E.g. ``"onData"`` finds every call through a field named
            ``onData``.  Uses three-tier resolution: exact name, exact
            qualified, suffix LIKE.
        project_root: Project root directory. Auto-detected if omitted.
        limit: Maximum results (default 50, max 200).
        variant: Build variant (multi-project). Omit for the default
            variant, ``"*"`` for all.
        image: Sysbuild image in the variant. Omit for all images.

    Returns:
        list of dicts, each with: file, line, expr_text (the callee
        expression, e.g. ``"driver.onData"``), target_usr, target_name,
        fn_ptr_type (the function pointer type signature), caller
        (enclosing function name), caller_kind.

        Never empty: one dict with ``error`` (cannot resolve) or ``info``
        (no results) replaces the results.  Check both keys first.
    """
    try:
        db = BaseHandler.resolve_db_context(project_root, variant=variant, image=image)
    except RuntimeError as e:
        return [{"error": str(e)}]
    root = db.root

    def _query(conn, config_hash):
        # Runs under the executor lock on the single shared connection;
        # must not open its own connection.  Timeout is enforced by
        # _wrap_tool (300 s + interrupt), not here.
        # Check that Phase 2 indirect call sites exist at all in this index.
        # Older indexes predating Phase 2 will have zero call sites,
        # and reporting "not found" for every query would be misleading.
        if count_indirect_call_sites(conn, config_hash) == 0:
            return [{"info": (
                "No indirect call sites indexed. Re-run 'fw-context index' "
                "to populate the table (added in Phase 2)."
            )}]

        clamped_limit = max(0, min(limit, 200))
        rows = query_indirect_call_sites(conn, config_hash, name, limit=clamped_limit)
        if not rows:
            return [{"info": f"No indirect call sites found for '{name}'."}]

        return [
            {
                "file": abs_path(root, r["from_file"]),
                "line": r["from_line"],
                "expr_text": r["expr_text"],
                "target_usr": r["target_usr"],
                "target_name": r["target_name"],
                "fn_ptr_type": r["fn_ptr_type"],
                "caller": r["caller_qname"] or r["caller_name"] or "<file scope>",
                "caller_kind": r["caller_kind"],
            }
            for r in rows
        ]

    return db.execute_scoped(_query)

# ── moved from server.py ──
def find_indirect_targets(
    name: Annotated[str, Field(description="Name of the function pointer field, variable, or parameter. "
        "E.g. 'onData' — returns functions assigned to Driver::onData.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    limit: Annotated[int, Field(description="Maximum results (default 50, max 200).")] = 50,
    variant: Annotated[str | None, Field(description="Build variant name (multi-project). Omit to use default_variant or fail-closed. Use '*' for all variants.")] = None,
    image: Annotated[str | None, Field(description="Sysbuild image name within the variant (multi-project). Omit for all images of the variant.")] = None,
) -> list[dict]:
    """Find functions assigned to a C/C++ function pointer field or
    variable. libclang-powered: links assignment sites to call sites
    via the field's unique symbol reference, which text-based search
    cannot resolve.

    Links assignment sites (``driver.onData = &handler``) to call
    sites (``driver.onData(buf, len)``) via the field's USR.

    Returns each function that could be invoked through the named function
    pointer, showing both the assignment location and the call site(s).
    When a function is assigned but no call site is found, ``call_file``
    and ``call_line`` are ``null`` — the assignment exists but the
    invocation may be in unindexed code.

    For the reverse query — where is this field or parameter called — use
    ``find_indirect_call_sites``.

    Read-only. No side effects. Requires the reference index
    (``fw-context index`` — refs on by default).

    Args:
        name: Name of the function pointer field, variable, or parameter.
            E.g. ``"onData"`` finds every function assigned to a field
            named ``onData``.  Uses three-tier resolution.
        project_root: Project root directory. Auto-detected if omitted.
        limit: Maximum results (default 50, max 200).
        variant: Build variant (multi-project). Omit for the default
            variant, ``"*"`` for all.
        image: Sysbuild image in the variant. Omit for all images.

    Returns:
        list of dicts, each with: rhs_name (assigned function),
        rhs_qname, fn_ptr_type, method (assignment/call_arg/var_init/
        init_list), assign_file, assign_line, assign_caller,
        call_file, call_line, call_expr_text.

        An entry can carry ``_note`` (str) when fw-context cannot resolve
        the direct call site — the callee is template-obscured, or the call
        site comes from the type-based fallback.  Read that note before you
        act on ``call_file`` and ``call_line``.

        Never empty: one dict with ``error`` (cannot resolve) or ``info``
        (no results) replaces the results.  Check both keys first.
    """
    try:
        db = BaseHandler.resolve_db_context(project_root, variant=variant, image=image)
    except RuntimeError as e:
        return [{"error": str(e)}]
    root = db.root

    def _query(conn, config_hash):
        # Runs under the executor lock on the single shared connection;
        # must not open its own connection.  Timeout is enforced by
        # _wrap_tool (300 s + interrupt), not here.
        # Check that Phase 3 function-pointer assignments exist.
        # Older indexes predating Phase 3 will have zero entries.
        if count_fp_assignments(conn, config_hash) == 0:
            return [{"info": (
                "No function pointer assignments indexed. Re-run "
                "'fw-context index' to populate the table (added in Phase 3)."
            )}]

        clamped_limit = max(0, min(limit, 200))
        rows = query_indirect_targets(conn, config_hash, name, limit=clamped_limit)
        if not rows:
            return [{"info": f"No functions assigned to '{name}'."}]

        results: list[dict] = []
        for r in rows:
            call_expr_text: str | None = str(r["call_expr_text"]) if r["call_expr_text"] else None
            entry: dict = {
                "rhs_name": str(r["rhs_name"] or ""),
                "rhs_qname": str(r["rhs_qname"] or r["rhs_name"] or ""),
                "fn_ptr_type": str(r["fn_ptr_type"] or ""),
                "method": str(r["method"] or ""),
                "assign_file": abs_path(root, str(r["assign_file"] or "")),
                "assign_line": r["assign_line"],
                "assign_caller": str(r["assign_caller"] or "<file scope>"),
                "call_file": abs_path(root, str(r["call_file"])) if r["call_file"] else None,
                "call_line": r["call_line"],
                "call_expr_text": call_expr_text,
            }
            lhs_name = str(r["lhs_name"] or "")
            if lhs_name and not r["lhs_usr"]:
                # Template-obscured callback: libclang could not resolve the
                # concrete callee type (e.g., _timeout.attach(&handler)).
                # The call-site resolver uses a type-based fallback — when
                # the receiver class's method signature matches a known
                # handler type, the call site is attributed to that method.
                if not r["call_file"]:
                    lhs_display = lhs_name.strip()
                    entry["_note"] = (
                        f"Direct call site not resolved — the callee "
                        f"({lhs_display}) is template-obscured. "
                        "The function pointer is likely stored and invoked "
                        "asynchronously (e.g. Timeout/Ticker callback). "
                        "Use find_indirect_call_sites on the receiver class "
                        "to find the actual invocation site."
                    )
                else:
                    entry["_note"] = (
                        "Call site resolved via type-based fallback "
                        "(template-obscured callback → receiver class handler)."
                    )
            results.append(entry)
        return results

    return db.execute_scoped(_query)

# ── moved from server.py ──
def _refs_guard(project_root: str | None, variant: str | None = None, image: str | None = None) -> tuple[DbContext, None] | tuple[None, list[dict]]:
    """Shared guard for graph tools: resolve project, get executor, check refs exist.

    Every graph-traversal tool (find_call_path, find_all_callers_recursive,
    find_callees_recursive, find_dead_code, find_wrapper_callers,
    trace_data_flow, find_hotspots) must verify two preconditions:
    1. The project DB context can be resolved.
    2. At least one reference is indexed — an empty refs table means
       the graph tools cannot return meaningful results.

    Returns:
        ``(db_context, None)`` on success — callers run their queries via
        ``db.executor.execute_sync(query_fn, db.config_hash)``.
        ``(None, error_list)`` on failure — caller propagates the error.
    """

    try:
        db = BaseHandler.resolve_db_context(project_root, variant=variant, image=image)
    except RuntimeError as e:
        return None, [{"error": str(e)}]

    if db.executor.execute_sync(count_refs, db.config_hash) == 0:
        return None, [{"info": (
            "No references indexed. Refs are on by default — "
            "they may have been disabled with [index] index_refs = false. "
            "Re-run 'fw-context index' to rebuild."
        )}]

    return db, None

# ── moved from server.py ──
def find_call_path(
    from_name: Annotated[str, Field(description="Starting symbol for path search.")],
    to_name: Annotated[str, Field(description="Target symbol to find path to.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    max_depth: Annotated[int, Field(description="Maximum BFS depth for path search (default 10).")] = 10,
    variant: Annotated[str | None, Field(description="Build variant name (multi-project). Omit to use default_variant or fail-closed. Use '*' for all variants.")] = None,
    image: Annotated[str | None, Field(description="Sysbuild image name within the variant (multi-project). Omit for all images of the variant.")] = None,
) -> list[dict]:
    """Find call paths between two C/C++ functions via BFS in the libclang
    call graph, including function-pointer edges, ISR vector
    registrations, implicit constructors, and synthetic dispatch edges
    (event loops, thread starts).  libclang-powered: follows
    function-pointer edges and ISR vector registrations that text-based
    search cannot resolve.

    Use to answer "how does A reach B?" — e.g. tracing how a high-level
    event handler eventually calls a low-level driver.  Returns up to 5
    shortest paths, each with ``depth`` (edge count) and ``chain``
    (e.g. ``"main → app_run → modem_init"``).

    **Edge types traversed:** The BFS includes ``call``, ``indirect``
    (function pointers / ISRs), ``implicit_construct`` (global/static
    object constructors), and ``dispatch`` (synthetic edges through event
    loops like ``EventQueue::dispatch_forever`` and thread starts like
    ``Thread::start``).

    **Limitations:**

    - **Dispatch bridges:** callbacks registered through
      ``EventQueue::call_every``, ``k_work_submit``, or ``xTimerStart``
      reach their dispatch entry point (``dispatch_forever``,
      ``z_work_q_main``) through a built-in map for mbed-os, Zephyr, and
      FreeRTOS.  Add other RTOS patterns in
      ``[call_graph.dispatch_bridges]`` (``.fw-context/config.toml``); a
      bridge whose entry symbol is not in the index is skipped silently.
    - **Ambiguous fallback names:** for a call that libclang cannot
      resolve (template-obscured ``_timeout.attach(...)``), a source-line
      regex matches the method name.  When several methods share that
      unqualified name and neither the receiver field type nor the caller
      class disambiguates, fw-context creates NO edge — conservative, to
      avoid false paths.
    - **Global constructors:** file-scope ``implicit_construct`` edges
      hang off a synthetic ``<global ctors>`` node between ``main`` and
      every global constructor.  Any query that can reach ``main`` uses
      it, not only a query that starts at ``main``.

    **On an empty result** that you expected to hold a path: look for async
    dispatch (``search_bodies("call_every")``, ``search_bodies("attach")``),
    trace the intermediate symbols with ``find_callers``, raise
    ``max_depth``, and check the function-pointer wiring with
    ``find_indirect_call_sites`` / ``find_indirect_targets``.

    For one-sided exploration use ``find_all_callers_recursive`` (who reaches
    this?) or ``find_callees_recursive`` (what does this reach?).
    For exact call-graph verification use ``find_callers`` or
    ``find_references``.

    Read-only. No side effects. Requires both symbols to be in the index
    and refs enabled (``fw-context index`` — refs on by default).

    Args:
        from_name: Starting symbol for path search.
        to_name: Target symbol to find path to.
        project_root: Project root. Auto-detected if omitted.
        max_depth: Maximum BFS depth for path search (default 10, max 50).
        variant: Build variant (multi-project). Omit for the default
            variant, ``"*"`` for all.
        image: Sysbuild image in the variant. Omit for all images.

    Returns:
        list of dicts, each with: depth (edge count, int), chain (str —
        e.g. ``"main → app_run → modem_init"``). When no path exists
        within the depth limit, the list holds one ``info`` dict.

        Never empty: one dict with ``error`` (cannot resolve) or ``info``
        (no results) replaces the results.  Check both keys first.
    """
    db, err = _refs_guard(project_root, variant=variant, image=image)
    if err:
        return err
    assert db is not None  # narrowed: err is None only on success

    def _query(conn, config_hash):
        # Runs under the executor lock on the single shared connection;
        # must not open its own connection.  Timeout is enforced by
        # _wrap_tool (300 s + interrupt), not here.
        # Validate both symbols exist before running the BFS —
        # a failed BFS on nonexistent symbols wastes time and
        # produces unhelpful "no path" errors.
        if _lookup_definition(conn, config_hash, from_name, preferred_kinds=None) is None:
            return [{"error": f"Symbol not found: {from_name}"}]
        if _lookup_definition(conn, config_hash, to_name, preferred_kinds=None) is None:
            return [{"error": f"Symbol not found: {to_name}"}]
        rows = index_db.find_call_path(conn, config_hash, from_name, to_name, max_depth=max_depth)
        if not rows:
            return [{"info": f"No path found from '{from_name}' to '{to_name}' within depth {max_depth}."}]
        return rows

    return db.execute_scoped(_query)

# ── moved from server.py ──
def find_all_callers_recursive(
    name: Annotated[str, Field(description="Symbol name to find transitive callers of.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    max_depth: Annotated[int, Field(description="Maximum BFS depth for transitive search (default 5).")] = 5,
    limit: Annotated[int, Field(description="Maximum results (default 50).")] = 50,
    variant: Annotated[str | None, Field(description="Build variant name (multi-project). Omit to use default_variant or fail-closed. Use '*' for all variants.")] = None,
    image: Annotated[str | None, Field(description="Sysbuild image name within the variant (multi-project). Omit for all images of the variant.")] = None,
) -> list[dict]:
    """Find all transitive C/C++ callers — who calls *name*, directly or
    indirectly, through the libclang call graph including
    function-pointer edges, implicit constructors, and synthetic
    dispatch edges. libclang-powered: follows function-pointer
    assignments and ISR vector registrations across the full call tree.

    Use for impact analysis: "if I change this function, how far does the
    ripple go?"  Returns callers at depth 1 (direct), depth 2 (callers of
    callers), up to ``max_depth`` (default 5).  Results are deduplicated —
    each caller appears once at its shortest distance to the target.

    **Edge types traversed:** Includes ``call``, ``indirect`` (function
    pointers / ISRs), ``implicit_construct`` (constructors reachable through
    file-scope global objects), and ``dispatch`` (synthetic edges through
    event loops and thread starts).

    **Limitation — ambiguous name resolution:** When a source-line fallback
    cannot disambiguate which method is called (e.g. ``attach()`` matching
    both ``Timeout::attach`` and ``SerialBase::attach``), the edge is
    conservatively omitted to avoid false callers.  If you suspect a
    missing caller, verify with ``search_bodies("target_name")`` and
    ``find_indirect_targets``.

    For a flat, single-level caller list use ``find_callers`` (faster).
    For the reverse direction use ``find_callees_recursive``.

    Read-only. No side effects. Requires the reference index
    (``fw-context index`` — refs on by default). BFS from the target
    outward; performance scales with call-graph fan-out.

    Args:
        name: Symbol name to find transitive callers of.
        project_root: Project root. Auto-detected if omitted.
        max_depth: Maximum BFS depth for transitive search (default 5).
        limit: Maximum results (default 50).
        variant: Build variant (multi-project). Omit for the default
            variant, ``"*"`` for all.
        image: Sysbuild image in the variant. Omit for all images.

    Returns:
        list of dicts, each with: caller (str — caller name),
        caller_qualified_name (str), depth (int — distance from target),
        file (str), line (int), ref_kind (``"call"`` or ``"indirect"``).

        Never empty: one dict with ``error`` (cannot resolve) or ``info``
        (no results) replaces the results.  Check both keys first.
    """
    db, err = _refs_guard(project_root, variant=variant, image=image)
    if err:
        return err
    assert db is not None  # narrowed: err is None only on success

    def _query(conn, config_hash):
        # Runs under the executor lock on the single shared connection;
        # must not open its own connection.  Timeout is enforced by
        # _wrap_tool (300 s + interrupt), not here.
        if _lookup_definition(conn, config_hash, name, preferred_kinds=None) is None:
            return [{"error": f"Symbol not found: {name}"}]
        rows = index_db.find_all_callers_recursive(conn, config_hash, name, max_depth=max_depth, limit=limit)
        if not rows:
            return [{"info": f"No callers found for '{name}'."}]
        return rows

    return db.execute_scoped(_query)

# ── moved from server.py ──
def find_callees_recursive(
    name: Annotated[str, Field(description="Symbol name to find transitive callees of.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    max_depth: Annotated[int, Field(description="Maximum BFS depth for transitive search (default 5).")] = 5,
    limit: Annotated[int, Field(description="Maximum results (default 50).")] = 50,
    variant: Annotated[str | None, Field(description="Build variant name (multi-project). Omit to use default_variant or fail-closed. Use '*' for all variants.")] = None,
    image: Annotated[str | None, Field(description="Sysbuild image name within the variant (multi-project). Omit for all images of the variant.")] = None,
) -> list[dict]:
    """Find all transitive C/C++ callees — what *name* calls, directly or
    indirectly, through the libclang call graph including
    function-pointer edges, implicit constructors, and synthetic
    dispatch edges. libclang-powered: follows function-pointer
    calls and indirect invocations across the full dependency tree.

    Use for dependency analysis: "what does this function depend on to do
    its job?"  Returns callees at depth 1 (direct), depth 2 (callees of
    callees), up to ``max_depth`` (default 5).  Results are deduplicated
    by shortest distance.

    **Edge types traversed:** Includes ``call``, ``indirect`` (function
    pointers / ISRs), ``implicit_construct`` (constructors reachable through
    file-scope global objects), and ``dispatch`` (synthetic edges through
    event loops and thread starts).

    **Limitation — ambiguous name resolution:** When a source-line fallback
    cannot disambiguate which method is called, the edge is conservatively
    omitted to avoid false callees.  If you suspect a missing callee,
    verify with ``search_bodies("target_name")``.

    For direct callees only, ``get_symbol_context`` gives a faster flat
    list along with the function body and callers. For the reverse
    direction use ``find_all_callers_recursive``.

    Read-only. No side effects. Requires the reference index
    (``fw-context index`` — refs on by default).

    Args:
        name: Symbol name to find transitive callees of.
        project_root: Project root. Auto-detected if omitted.
        max_depth: Maximum BFS depth for transitive search (default 5).
        limit: Maximum results (default 50).
        variant: Build variant (multi-project). Omit for the default
            variant, ``"*"`` for all.
        image: Sysbuild image in the variant. Omit for all images.

    Returns:
        list of dicts, each with: callee (str — callee name),
        callee_qualified_name (str), depth (int — distance from source),
        file (str), line (int), ref_kind (``"call"`` or ``"indirect"``).

        Never empty: one dict with ``error`` (cannot resolve) or ``info``
        (no results) replaces the results.  Check both keys first.
    """
    db, err = _refs_guard(project_root, variant=variant, image=image)
    if err:
        return err
    assert db is not None  # narrowed: err is None only on success

    def _query(conn, config_hash):
        # Runs under the executor lock on the single shared connection;
        # must not open its own connection.  Timeout is enforced by
        # _wrap_tool (300 s + interrupt), not here.
        if _lookup_definition(conn, config_hash, name, preferred_kinds=None) is None:
            return [{"error": f"Symbol not found: {name}"}]
        rows = index_db.find_callees_recursive(conn, config_hash, name, max_depth=max_depth, limit=limit)
        if not rows:
            return [{"info": f"No callees found for '{name}'."}]
        return rows

    return db.execute_scoped(_query)

# ── moved from server.py ──
def find_dead_code(
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    limit: Annotated[int, Field(description="Maximum results (default 100).")] = 100,
    exclude_paths: Annotated[list[str] | None, Field(description="Additional LIKE patterns to exclude. Merged with defaults from config. E.g. ['lib/%'].")] = None,
    project_only: Annotated[bool, Field(description="When True (default), auto-excludes SDK/vendor paths based on the detected build system and applies project config exclude_paths. Set False to see all results.")] = True,
    variant: Annotated[str | None, Field(description="Build variant name (multi-project). Omit to use default_variant or fail-closed. Use '*' for all variants.")] = None,
    image: Annotated[str | None, Field(description="Sysbuild image name within the variant (multi-project). Omit for all images of the variant.")] = None,
) -> list[dict]:
    """Find C/C++ functions that are defined but never called —
    libclang-powered dead code detection across the entire indexed
    codebase. Distinguishes called from uncalled symbols globally, not
    just within a single file — text-based search cannot determine
    whether a function is actually reachable.

    **What "dead" means:** zero references in the index — no call, no
    function-pointer assignment, no indirect call site.  This is a
    single-layer reference check, NOT a reachability analysis from the
    entry points (main, ISR, exported symbols): a function that only a
    second dead function calls still has a reference, thus this tool does
    not mark it.  For transitive reachability, trace from your entry points
    with ``find_callees_recursive``.

    The ``status`` field splits the results:

    * ``"dead"`` — no reference at all.  Likely unused.
    * ``"possibly_dead"`` — assigned to a function pointer (Phase 1
      ``ref_kind="indirect"``), but no call site through that pointer
      resolved (Phase 3).  Unindexed code or a type-erased API can still
      call it.  Treat it as uncertain, and check each hit with
      ``find_indirect_targets`` before you delete anything.

    fw-context detects a constructor call through global/static object and
    member-field initialization as an ``implicit_construct`` reference.
    Known false positives remain: constructors from factories, ISRs,
    virtual method overrides, and weak-aliased symbols.  Always verify
    before you delete.

    ``project_only=True`` (default) excludes the SDK and vendor paths
    through the ``is_project`` column, which follows the ``vendor_paths``
    and ``project_paths`` config.  Set ``project_only=False`` to see the
    vendor results too.

    Read-only. No side effects. Requires the reference index
    (``fw-context index`` — refs on by default).

    Args:
        project_root: Project root. Auto-detected if omitted.
        limit: Maximum results (default 100).
        exclude_paths: Additional LIKE patterns to exclude (user-supplied
            tool parameter, not config). E.g. ``['lib/%']``.
        project_only: When True (default), filters to ``is_project = 1``
            symbols. Set False to see all results.
        variant: Build variant (multi-project). Omit for the default
            variant, ``"*"`` for all.
        image: Sysbuild image in the variant. Omit for all images.

    Returns:
        list of dicts, each with: name, qualified_name, kind, file, line,
        status (``"dead"`` or ``"possibly_dead"``), and reason (str —
        explains why the function is classified as dead or possibly dead).

        Never empty: one dict with ``info`` replaces an empty result.
        Check that key first.
    """
    limit = max(0, min(limit, 200))  # clamp
    db, err = _refs_guard(project_root, variant=variant, image=image)
    if err:
        return err
    assert db is not None  # narrowed: err is None only on success

    def _query(conn, config_hash):
        # Runs under the executor lock on the single shared connection;
        # must not open its own connection.  Timeout is enforced by
        # _wrap_tool (300 s + interrupt), not here.
        rows = index_db.find_dead_code(
            conn, config_hash, limit=limit,
            exclude_paths=exclude_paths,
            project_only=project_only,
        )
        if not rows:
            return [{"info": "No dead or possibly-dead functions found — every defined function has at least one caller."}]
        return rows

    return db.execute_scoped(_query)

# ── moved from server.py ──
def find_wrapper_callers(
    class_name: Annotated[str, Field(description="Driver class name to find wrappers for. E.g. 'UART_DRIVER' or 'hal::UART_DRIVER'.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    limit: Annotated[int, Field(description="Maximum wrapper method results (default 50).")] = 50,
    variant: Annotated[str | None, Field(description="Build variant name (multi-project). Omit to use default_variant or fail-closed. Use '*' for all variants.")] = None,
    image: Annotated[str | None, Field(description="Sysbuild image name within the variant (multi-project). Omit for all images of the variant.")] = None,
) -> list[dict]:
    """Find C/C++ wrapper classes that call methods of a driver class —
    libclang-powered adapter pattern detection. Traces method ownership
    across class boundaries to reveal the wrapper/adapter architecture
    (e.g. ``UART`` wraps ``UART_DRIVER``). Text-based search cannot
    distinguish which class owns each method call.

    Returns wrapper methods grouped by wrapper class, showing which driver
    methods each wrapper calls.  Useful for understanding the adapter/wrapper
    architecture (e.g. ``UART`` wraps ``UART_DRIVER``).

    For the reverse perspective — finding who calls a specific driver method
    — use ``find_callers``. For class member listing use
    ``get_class_members``.

    Read-only. No side effects. Requires the reference index
    (``fw-context index`` — refs on by default).

    Args:
        class_name: Driver class name to find wrappers for.
            E.g. ``'UART_DRIVER'`` or ``'hal::UART_DRIVER'``.
        project_root: Project root. Auto-detected if omitted.
        limit: Maximum wrapper method results (default 50).
        variant: Build variant (multi-project). Omit for the default
            variant, ``"*"`` for all.
        image: Sysbuild image in the variant. Omit for all images.

    Returns:
        list of dicts, each with: wrapper_class (str — ``"(global)"`` for a
        free function), method_count (int),
        methods (list of dicts — each with method, qualified_name, kind,
        file (str — absolute path of the file that holds the body of that
        method), and calls (list of dicts — ``driver_method`` (str) and
        ``line`` (int) of each call into the driver))).

        The path sits on the method, not on the class, because one wrapper
        class often spans several files.

        Never empty: one dict with ``error`` (cannot resolve) or ``info``
        (no results) replaces the results.  Check both keys first.
    """
    limit = max(0, min(limit, 50))  # clamp
    db, err = _refs_guard(project_root, variant=variant, image=image)
    if err:
        return err
    assert db is not None  # narrowed: err is None only on success

    def _query(conn, config_hash):
        # Runs under the executor lock on the single shared connection;
        # must not open its own connection.  Timeout is enforced by
        # _wrap_tool (300 s + interrupt), not here.
        # Resolve driver class — check it exists in the index
        if _lookup_definition(conn, config_hash, class_name, preferred_kinds=None) is None:
            return [{"error": f"Symbol not found: {class_name}"}]

        # Find all methods of the class. Three-tier resolution:
        # 1. Exact prefix LIKE (class_name::%) — matches when the
        #    class was indexed with its full qualified name.
        # 2. Escaped LIKE with leading wildcard (%class_name::%) —
        #    catches cases where the class is nested in a namespace.
        #    ESCAPE clause prevents SQLite from treating '::' specially.
        # 3. Short-name fallback — when the class has a fully-qualified
        #    name (Namespace::Class), strip the namespace and retry
        #    with just the short name as prefix.
        driver_methods = conn.execute(
            """SELECT s.usr, s.name, s.qualified_name
               FROM symbols s
               WHERE s.config_hash = ?
                 AND s.kind = 'method'
                 AND s.qualified_name LIKE ?
               ORDER BY s.name
               LIMIT ?""",
            (config_hash, f"{class_name}::%", max(limit * 10, 500)),
        ).fetchall()

        if not driver_methods:
            esc_name = _escape_like(class_name)
            driver_methods = conn.execute(
                """SELECT s.usr, s.name, s.qualified_name
                   FROM symbols s
                   WHERE s.config_hash = ?
                     AND s.kind = 'method'
                     AND s.qualified_name LIKE ? ESCAPE '\\'
                   ORDER BY s.name
                   LIMIT ?""",
                (config_hash, f"%{esc_name}::%", max(limit * 10, 500)),
            ).fetchall()

        if not driver_methods and "::" in class_name:
            short_name = class_name.rsplit("::", 1)[-1]
            driver_methods = conn.execute(
                """SELECT s.usr, s.name, s.qualified_name
                   FROM symbols s
                   WHERE s.config_hash = ?
                     AND s.kind = 'method'
                     AND s.qualified_name LIKE ? ESCAPE '\\'
                   ORDER BY s.name
                   LIMIT ?""",
                (config_hash, f"{short_name}::%", max(limit * 10, 500)),
            ).fetchall()

        if not driver_methods:
            return [{"info": f"No methods found for class '{class_name}'."}]

        driver_usr_map = {r["usr"]: r for r in driver_methods}

        # Find all callers of those methods
        placeholders = ",".join("?" * len(driver_usr_map))
        rows = conn.execute(
            f"""SELECT r.from_usr, r.to_usr, r.from_file, r.from_line, r.ref_kind,
                       caller.name AS caller_name,
                       caller.qualified_name AS caller_qname,
                       caller.kind AS caller_kind
                FROM refs r
                LEFT JOIN symbols caller
                  ON caller.config_hash = r.config_hash AND caller.usr = r.from_usr
                WHERE r.config_hash = ?
                  AND r.to_usr IN ({placeholders})
                  AND r.ref_kind IN ('call', 'indirect')
                ORDER BY caller.qualified_name, r.from_line
                LIMIT ?""",
            (config_hash, *driver_usr_map.keys(), limit),
        ).fetchall()

        if not rows:
            return [{"info": f"No callers found for methods of '{class_name}'."}]

        # Aggregate callers by wrapper class. The qualified name of each
        # caller method is split at the last "::" to extract the enclosing
        # class name. Free functions (no "::" in qualified name) are
        # grouped under "(global)".
        # Methods are deduplicated by qualified name within each class.
        # Each method tracks the list of driver methods it calls.
        wrapped: dict[str, dict] = {}
        for r in rows:
            caller_qn = r["caller_qname"] or r["caller_name"] or "?"
            # Extract class from qualified name: "zbox::ZMODEM::start" → "zbox::ZMODEM"
            if "::" in caller_qn:
                wrapper_class = caller_qn.rsplit("::", 1)[0]
            else:
                wrapper_class = "(global)"
            if wrapper_class not in wrapped:
                wrapped[wrapper_class] = {"class": wrapper_class, "methods": {}}
            cm = wrapped[wrapper_class]["methods"]
            if caller_qn not in cm:
                cm[caller_qn] = {
                    "method": r["caller_name"],
                    "qualified_name": caller_qn,
                    "kind": r["caller_kind"],
                    # The file that holds the call, thus the file that holds
                    # the body of this wrapper method.  A per-method path is
                    # necessary: one wrapper class can span many files (18%
                    # of the classes in zbox-ecb-fw do, up to 49 files), thus
                    # a single path on the class would cover only one of
                    # them, and the staleness check would miss the rest.
                    "file": abs_path(db.root, r["from_file"]),
                    "calls": [],
                }
            target = driver_usr_map.get(r["to_usr"])
            if target:
                cm[caller_qn]["calls"].append({
                    "driver_method": target["name"],
                    "line": r["from_line"],
                })

        # Flatten for output
        result = []
        for wc in sorted(wrapped.keys()):
            entry = wrapped[wc]
            result.append({
                "wrapper_class": wc,
                "method_count": len(entry["methods"]),
                "methods": sorted(entry["methods"].values(), key=lambda m: m["qualified_name"]),
            })
        return result

    return db.execute_scoped(_query)

# ── moved from server.py ──
def trace_data_flow(
    type_name: Annotated[str, Field(description="Type name to trace. E.g. 'SensorData' or 'Config::SensorData'.")],
    to_symbol: Annotated[str, Field(description="Target symbol name. E.g. 'uart_send' or 'UART_DRIVER::send'.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    max_depth: Annotated[int, Field(description="Maximum call path depth (default 8).")] = 8,
    limit: Annotated[int, Field(description="Maximum source functions to trace (default 15).")] = 15,
    timeout_ms: Annotated[int, Field(description="Maximum total execution time in "
        "milliseconds (default 30000).")] = 30000,
    variant: Annotated[str | None, Field(description="Build variant name (multi-project). Omit to use default_variant or fail-closed. Use '*' for all variants.")] = None,
    image: Annotated[str | None, Field(description="Sysbuild image name within the variant (multi-project). Omit for all images of the variant.")] = None,
) -> list[dict]:
    """Trace how C/C++ data of a given type flows to a target function via
    libclang call paths. libclang-powered: finds functions by type
    signature and maps call paths through the full call graph, which
    text-based search cannot trace across translation units.

    Finds functions whose signature mentions *type_name*, then looks for call
    paths from those functions to *to_symbol*.  Returns a data flow map —
    useful for understanding how a data structure travels through the system
    to its destination.

    Works best for synchronous driver stacks (e.g. sensor read → I2C write).
    Cannot follow async flows (message queues, interrupts, RS485 callbacks).
    For exact call-graph queries use the ``find_*`` family;
    verify specific paths with ``find_call_path``.

    Read-only. No side effects. Requires the reference index
    (``fw-context index`` — refs on by default).

    Args:
        type_name: Type name to trace. E.g. ``'SensorData'`` or
            ``'Config::SensorData'``.
        to_symbol: Target symbol name. E.g. ``'uart_send'`` or
            ``'UART_DRIVER::send'``.
        project_root: Project root. Auto-detected if omitted.
        max_depth: Maximum call path depth (default 8).
        limit: Maximum source functions to trace (default 15).
        timeout_ms: Maximum total execution time in milliseconds
            (default 30000). Clamped to 1000–300000.
        variant: Build variant (multi-project). Omit for the default
            variant, ``"*"`` for all.
        image: Sysbuild image in the variant. Omit for all images.

    Returns:
        list of dicts with a leading ``_summary`` entry:
        {_summary (str), _type (str), _target (str)}, followed by source
        entries each with: source_name, source_qualified_name, source_kind,
        source_file, source_line, caller_count, reachable (bool), and
        paths (list of call path dicts — empty when unreachable).

        A source entry with ``timed_out: True`` means that the path search
        stopped at the time limit for that source.  Its ``reachable: False``
        thus means "not proved reachable", not "proved unreachable".

        Never empty: one dict with ``info`` replaces an empty result.
        Check that key first.
    """
    max_depth = max(1, min(max_depth, 20))  # clamp
    limit = max(0, min(limit, 15))  # clamp
    timeout_ms = max(1000, min(timeout_ms, 300000))  # clamp 1s–5min
    db, err = _refs_guard(project_root, variant=variant, image=image)
    if err:
        return err
    assert db is not None  # narrowed: err is None only on success
    root = db.root

    def _query(conn, config_hash):
        # Runs under the executor lock on the single shared connection;
        # must not open its own connection.  Timeout is enforced by
        # _wrap_tool (300 s + interrupt), not here.
        t0 = time.perf_counter()
        # Resolve the target symbol's USR — needed for call-path lookups.
        target = conn.execute(
            """SELECT usr, name FROM symbols
               WHERE config_hash = ? AND (name = ? OR qualified_name = ?)
               ORDER BY is_definition DESC LIMIT 1""",
            (config_hash, to_symbol, to_symbol),
        ).fetchone()
        if not target:
            return [{"info": f"Target symbol '{to_symbol}' not found."}]

        # Find functions mentioning type_name in their signature (ranked by
        # caller count so the most "active" data handlers are shown first)
        sources = conn.execute(
            """SELECT s.name, s.qualified_name, s.kind, s.file_path, s.line,
                      s.signature, s.usr,
                      (SELECT COUNT(*) FROM refs r
                       WHERE r.to_usr = s.usr AND r.config_hash = s.config_hash
                         AND r.ref_kind IN ('call', 'indirect')) AS caller_count
               FROM symbols s
               WHERE s.config_hash = ?
                 AND s.is_definition = 1
                 AND s.signature LIKE ? ESCAPE '\\'
               ORDER BY caller_count DESC
               LIMIT ?""",
            (config_hash, f"%{_escape_like(type_name)}%", limit),
        ).fetchall()

        if not sources:
            return [{"info": f"No functions found with '{type_name}' in their signature."}]

        # For each source function, attempt call-path BFS to the target.
        # A per-iteration timeout guard checks elapsed time against
        # timeout_ms. When the budget is exhausted, remaining sources
        # are marked as timed_out rather than silently dropped.
        # This allows the caller to distinguish between truly unreachable
        # sources and sources that were not checked due to time limits.
        results = []
        for src in sources:
            elapsed_ms = (time.perf_counter() - t0) * 1000
            if elapsed_ms >= timeout_ms:
                results.append({
                    "source_name": src["name"],
                    "source_qualified_name": src["qualified_name"],
                    "source_kind": src["kind"],
                    "source_file": abs_path(root, src["file_path"]),
                    "source_line": src["line"],
                    "caller_count": src["caller_count"],
                    "reachable": False,
                    "timed_out": True,
                })
                continue
            paths = index_db.find_call_path(
                conn, config_hash, src["qualified_name"], to_symbol, max_depth=max_depth,
            )
            entry = {
                "source_name": src["name"],
                "source_qualified_name": src["qualified_name"],
                "source_kind": src["kind"],
                "source_file": abs_path(root, src["file_path"]),
                "source_line": src["line"],
                "caller_count": src["caller_count"],
            }
            if paths:
                entry["reachable"] = True
                entry["paths"] = paths[:3]
            else:
                entry["reachable"] = False
            results.append(entry)

        num_reachable = sum(1 for r in results if r.get("reachable"))
        num_timed_out = sum(1 for r in results if r.get("timed_out"))
        summary = f"{num_reachable}/{len(results)} source functions reach '{to_symbol}' within depth {max_depth}"
        if num_timed_out:
            summary += f" ({num_timed_out} timed out)"

        return [
            {
                "_summary": summary,
                "_type": type_name,
                "_target": to_symbol,
            },
            *results,
        ]

    return db.execute_scoped(_query)

# ── moved from server.py ──
def find_hotspots(
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    limit: Annotated[int, Field(description="Number of top-called functions to return (default 20).")] = 20,
    project_only: Annotated[bool, Field(description="When True (default), auto-excludes SDK/vendor paths so hotspots reflect project code.")] = True,
    exclude_paths: Annotated[list[str] | None, Field(description="Additional LIKE patterns to exclude. Merged with defaults. E.g. ['lib/%'].")] = None,
    variant: Annotated[str | None, Field(description="Build variant name (multi-project). Omit to use default_variant or fail-closed. Use '*' for all variants.")] = None,
    image: Annotated[str | None, Field(description="Sysbuild image name within the variant (multi-project). Omit for all images of the variant.")] = None,
) -> list[dict]:
    """Find the most-called C/C++ functions ranked by caller count —
    libclang call-graph hotspot detection. Identifies functions with
    the most architectural weight — good targets for refactoring,
    optimization, or extra testing. Text-based search cannot aggregate
    caller statistics across the full call graph.

    Use for high-level impact assessment: changing a hotspot affects many
    call sites. The result tells you which functions carry the most
    "architectural weight" across the entire codebase.

    By default, SDK/vendor paths are auto-excluded so hotspots reflect
    project code. Use ``project_only=False`` to see all results including
    vendor code.

    For the callers of a specific hotspot, follow up with ``find_callers``
    or ``find_all_callers_recursive``.

    Read-only. No side effects. Requires the reference index
    (``fw-context index`` — refs on by default).

    Args:
        project_root: Project root. Auto-detected if omitted.
        limit: Number of top-called functions to return (default 20).
        project_only: When True (default), filters to ``is_project = 1``
            symbols so hotspots reflect project code.
        exclude_paths: Additional LIKE patterns to exclude (user-supplied
            tool parameter). E.g. ``['lib/%']``.
        variant: Build variant (multi-project). Omit for the default
            variant, ``"*"`` for all.
        image: Sysbuild image in the variant. Omit for all images.

    Returns:
        list of dicts, each with: name, qualified_name, kind, file, line,
        caller_count (int — total number of call sites), signature.

        Never empty: one dict with ``info`` replaces an empty result.
        Check that key first.
    """
    limit = max(0, min(limit, 50))  # clamp
    db, err = _refs_guard(project_root, variant=variant, image=image)
    if err:
        return err
    assert db is not None  # narrowed: err is None only on success

    def _query(conn, config_hash):
        # Runs under the executor lock on the single shared connection;
        # must not open its own connection.  Timeout is enforced by
        # _wrap_tool (300 s + interrupt), not here.
        rows = index_db.find_hotspots(
            conn, config_hash, limit=limit,
            exclude_paths=exclude_paths,
            project_only=project_only,
        )
        if not rows and project_only:
            return [{"info": "No project hotspots found. Try project_only=False to include vendor code."}]
        if not rows:
            return [{"info": "No references indexed — enable index_refs and re-index."}]
        return rows

    return db.execute_scoped(_query)



def get_vector_table(
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    unhandled_only: Annotated[bool, Field(description="Return only the slots that reach the default handler.")] = False,
    limit: Annotated[int, Field(description="Maximum slots (default 300).")] = 300,
    variant: Annotated[str | None, Field(description="Build variant name (multi-project). Omit to use default_variant or fail-closed. Use '*' for all variants.")] = None,
    image: Annotated[str | None, Field(description="Sysbuild image name within the variant (multi-project). Omit for all images of the variant.")] = None,
) -> list[dict]:
    """Read the interrupt vector table, and say what services each interrupt.

    The vector table is how an interrupt reaches code.  Nothing CALLS a
    handler — the hardware reads a slot and jumps — so a handler has no
    caller, and every other tool shows it as unreferenced.  This tool
    reads the table itself, from the assembly the build compiles.

    Use it to answer "which interrupts does this firmware service", to
    find the handler for one interrupt, or to find the interrupts that
    reach the trap loop.

    **The slot number is the position in the table.**  What that position
    means belongs to the architecture, not to the index.  On Cortex-M
    slots 0 to 15 are the system exceptions and slot 16 + n is external
    interrupt n, so ``TIM2_IRQHandler`` in slot 44 is ``TIM2_IRQn = 28``.
    On other architectures the same position means something else.

    The ``status`` field says what services the interrupt:

    * ``"c"`` — a definition outside assembly.  Code runs.  When the
      index also holds the weak definition that this one replaced, the
      row has ``overridden`` with its file and line.
    * ``"assembly"`` — a strong assembly definition.  Assembly services
      the interrupt.
    * ``"unhandled"`` — a weak assembly definition that nothing
      overrode.  A CMSIS startup file makes this an alias of
      ``Default_Handler``, which is an infinite loop.  If the interrupt
      fires, the device stops.
    * ``"linker"`` — the linker script gives the address and no compiled
      file defines the name.  Slot 0 holds the initial stack pointer, not
      a handler, and looks like this.  When the index read the script,
      ``file`` and ``line`` name the assignment in it — on zbox-ecb-fw,
      `__StackTop` at `.link_script.ld:148`.  Do not read this row as
      code: there is no function to follow.

    A ``"c"`` row with ``overridden`` is the CMSIS pattern: the startup
    file defines each handler weakly, the project defines the same name
    again, and the linker keeps the strong one.

    Only tables written as address words are read — ``.word`` and
    ``.long`` in a vector section, which is what a CMSIS startup file
    writes.  Two limits follow, and both give fewer slots, never wrong
    ones:

    * **Zephyr gives the exceptions only.**  Its ``vector_table.S`` holds
      slots 0 to 15, and ``gen_isr_tables.py`` writes the table of
      external interrupts into C.  Measured on an nRF54L application:
      11 slots from the assembly.
    * **Some architectures write no address table.**  arm64, Xtensa and
      MIPS build theirs from branch instructions.  A RISC-V target has no
      table at all: ``mtvec`` holds one trap handler, and software reads
      the cause.  Measured on the RISC-V image of the same project: 27
      assembly symbols and 0 slots.

    For an interrupt this tool cannot show, ``find_references`` on the
    handler name still gives every reference the index holds.

    Read-only. No side effects. Requires an index of the assembly
    (``fw-context index``).

    Args:
        project_root: Project root. Auto-detected if omitted.
        unhandled_only: When True, return only the ``"unhandled"`` slots.
        limit: Maximum slots (default 300, max 1000).
        variant: Build variant (multi-project). Omit for the default
            variant, ``"*"`` for all.
        image: Sysbuild image in the variant. Omit for all images.

    Returns:
        list of dicts sorted by slot, each with: slot (int), name, file,
        line, status (``"c"``, ``"assembly"``, ``"unhandled"`` or
        ``"linker"``), table_file and table_line (where the slot is
        written).  A ``"c"`` row can also hold overridden, a dict with
        file and line.

        Never empty: one dict with ``error`` (no index) or ``info`` (no
        vector table in this build).  Check both keys first.
    """
    limit = max(0, min(limit, 1000))
    db, err = _refs_guard(project_root, variant=variant, image=image)
    if err:
        return err
    assert db is not None  # narrowed: err is None only on success

    def _query(conn, config_hash):
        # Runs under the executor lock on the single shared connection;
        # must not open its own connection.  Timeout is enforced by
        # _wrap_tool (300 s + interrupt), not here.
        rows = index_db.get_vector_table(
            conn, config_hash, unhandled_only=unhandled_only,
        )
        if not rows:
            return [{"info": (
                "No vector table in this build. The assembly holds no table "
                "of address words, or the build has no assembly at all. A "
                "RISC-V target has no such table: mtvec holds one trap "
                "handler and software reads the cause. arm64, Xtensa and "
                "MIPS build theirs from branch instructions, which this "
                "tool does not read. Use find_references on a handler name "
                "instead."
            )}]
        return rows[:limit]

    return db.execute_scoped(_query)
