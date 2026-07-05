"""Callgraph MCP tools — see mcp/server.py for registration."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Annotated

from pydantic import Field

from ...config import derive_project_id
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
from ...utils import abs_path, resolve_project_root
from ..shared.context import _db_path, _open_db_safe, _resolve_context
from ..shared.filtering import _merge_excludes
from .source import _lookup_definition

log = logging.getLogger(__name__)

# ── moved from server.py ──
def _references_result(name: str, project_root: str | None, ref_kind: str | list[str] | None, limit: int, *, caller_mode: bool = False) -> list[dict]:
    """Shared logic for ``find_callers`` and ``find_references``.

    Resolves the project, opens the DB, looks up the symbol, checks that refs
    are indexed, and returns formatted reference results.

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
    db_path, cfg, project_id, root = _resolve_context(project_root)
    if not db_path.exists():
        return [{"error": f"No index found for {root}. Run 'fw-context index' first."}]
    conn, err = _open_db_safe(db_path)
    if err:
        return [err]
    assert conn is not None
    try:
        with conn:
            cfg_data = get_active_config(conn, project_id)
            if not cfg_data:
                return [{"error": "No build config indexed."}]
            config_hash = cfg_data["config_hash"]
            symbol = _lookup_definition(conn, config_hash, name)
            if symbol is None:
                # Macro fallback: check if name is a macro definition
                macros = lookup_macro(conn, config_hash, name, exact=True, limit=1)
                if macros:
                    macro = macros[0]
                    # Use find_macro_refs for file-level usage tracking
                    ref_rows = find_macro_refs(conn, config_hash, name, limit=limit)
                    macro_def = {
                        "kind": "macro",
                        "name": macro["name"],
                        "file": abs_path(root, macro["file_path"]),
                        "line": macro["line"],
                        "value": macro["value"],
                        **({"expanded_value": macro["expanded_value"]} if macro["expanded_value"] else {}),
                    }
                    if not ref_rows:
                        label = "callers" if caller_mode else "references"
                        return [{"info": f"No {label} (macro) found for '{name}'.", **macro_def}]
                    result: list[dict] = [
                        {
                            "file": abs_path(root, r["file_path"]),
                            "ref_kind": "macro_use",
                            "_match_snippet": r["_match_snippet"],
                        }
                        for r in ref_rows
                    ]
                    result.insert(0, macro_def)
                    return result
                return [{"error": f"Symbol not found: {name}"}]
            if count_refs(conn, config_hash) == 0:
                return [{"info": (
                    "No references indexed. Refs are on by default — "
                    "they may have been disabled with [index] index_refs = false. "
                    "Re-run 'fw-context index' to rebuild with refs enabled."
                )}]
            limit = min(limit, 200)
            rows = find_refs(conn, config_hash, name, ref_kind=ref_kind, limit=limit)
            if not rows:
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
    finally:
        conn.close()

# ── moved from server.py ──
def find_callers(
    name: Annotated[str, Field(description="Symbol name to find callers of. Returns direct call sites and indirect calls via function pointers.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    limit: Annotated[int, Field(description="Maximum results.")] = 50,
) -> list[dict]:
    """USE INSTEAD OF grep, ctx_callgraph, or ctx_compose. Find who calls a
    C/C++ function — direct calls AND indirect via function pointers,
    callbacks, NVIC_SetVector, and struct init lists. grep cannot detect
    function pointer assignments or ISR vector registrations.

    Falls back to macro lookup when the symbol is not found as a
    function/method: returns the macro definition (kind="macro") and
    files that use it (ref_kind="macro_use").

    Use when you need a quick, flat list of immediate callers. For the full
    transitive call tree (who calls this indirectly through other functions),
    use ``find_all_callers_recursive``.  For all references including reads
    and member accesses, use ``find_references``.  For a path between two
    specific symbols, use ``find_call_path``.

    Requires the reference index (``fw-context index`` — refs are on by
    default).  Only direct call sites are returned; callers more than one
    hop away are not included.

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

    Returns:
        list of dicts, each with: file, line, ref_kind (``"call"``,
        ``"indirect"``, ``"implicit_construct"``, or ``"macro_use"``),
        caller (enclosing function name), caller_kind (``"function"``,
        ``"method"``, …). Macro fallback includes a leading dict with
        ``kind="macro"``, ``value``, and ``expanded_value``.
    """
    return _references_result(name, project_root, ref_kind=["call", "indirect", "implicit_construct"], limit=limit, caller_mode=True)

# ── moved from server.py ──
def find_references(
    name: Annotated[str, Field(description="Symbol name to find all references of — calls, reads, member accesses.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    limit: Annotated[int, Field(description="Maximum results.")] = 50,
) -> list[dict]:
    """USE INSTEAD OF grep, ctx_search, or ctx_callgraph. Find ALL references
    to a C/C++ symbol — calls, reads, member accesses, function pointer
    registrations, template references, and macro usages. grep and
    ctx_callgraph cannot see function-pointer registrations: NVIC_SetVector,
    mbed-os Timeout::attach, Ticker::attach, SerialBase::RxIrq,
    InterruptIn::fall/rise — all detected.

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

    Returns:
        list of dicts, each with: file, line, ref_kind, caller, caller_kind.
        ``ref_kind`` is one of: ``"call"``, ``"ref"``, ``"member"``,
        ``"indirect"`` (function-pointer reference in arguments, assignments,
        initializers, or init lists), ``"implicit_construct"`` (implicit
        constructor call from global/static object or member-field
        initialization), ``"template_ref"``, ``"macro_use"`` (macro usage
        in file). Macro fallback includes a leading dict with
        ``kind="macro"``, ``value``, and ``expanded_value``.
    """
    return _references_result(name, project_root, ref_kind=None, limit=limit)

# ── moved from server.py ──
def find_indirect_call_sites(
    name: Annotated[str, Field(description="Name of the function pointer field or variable to find call sites of. E.g. 'onData' finds all calls through Driver::onData.")],
    project_root: Annotated[str | None, Field(description="Project root directory. Auto-detected if omitted.")] = None,
    limit: Annotated[int, Field(description="Maximum results (default 50).")] = 50,
) -> list[dict]:
    """USE INSTEAD OF grep or ctx_callgraph. Find indirect call sites where a
    C/C++ function pointer field or variable is invoked — invisible to grep.

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

    Returns:
        list of dicts, each with: file, line, expr_text (the callee
        expression, e.g. ``"driver.onData"``), target_usr, target_name,
        fn_ptr_type (the function pointer type signature), caller
        (enclosing function name), caller_kind.
    """
    db_path, cfg, project_id, root = _resolve_context(project_root)
    if not db_path.exists():
        return [{"error": f"No index found for {root}. Run 'fw-context index' first."}]
    conn, err = _open_db_safe(db_path)
    if err:
        return [err]
    assert conn is not None
    try:
        with conn:
            cfg_data = get_active_config(conn, project_id)
            if not cfg_data:
                return [{"error": "No build config indexed."}]
            config_hash = cfg_data["config_hash"]

            if count_indirect_call_sites(conn, config_hash) == 0:
                return [{"info": (
                    "No indirect call sites indexed. Re-run 'fw-context index' "
                    "to populate the table (added in Phase 2)."
                )}]

            limit = min(limit, 200)
            rows = query_indirect_call_sites(conn, config_hash, name, limit=limit)
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
    finally:
        conn.close()

# ── moved from server.py ──
def find_indirect_targets(
    name: Annotated[str, Field(description="Name of the function pointer field, variable, or parameter. "
        "E.g. 'onData' — returns functions assigned to Driver::onData.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    limit: Annotated[int, Field(description="Maximum results (default 50, max 200).")] = 50,
) -> list[dict]:
    """USE INSTEAD OF grep or ctx_callgraph. Find functions assigned to a
    C/C++ function pointer field or variable — invisible to grep.

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

    Returns:
        list of dicts, each with: rhs_name (assigned function),
        rhs_qname, fn_ptr_type, method (assignment/call_arg/var_init/
        init_list), assign_file, assign_line, assign_caller,
        call_file, call_line, call_expr_text.
    """
    db_path, cfg, project_id, root = _resolve_context(project_root)
    if not db_path.exists():
        return [{"error": f"No index found for {root}. Run 'fw-context index' first."}]
    conn, err = _open_db_safe(db_path)
    if err:
        return [err]
    assert conn is not None
    try:
        with conn:
            cfg_data = get_active_config(conn, project_id)
            if not cfg_data:
                return [{"error": "No build config indexed."}]
            config_hash = cfg_data["config_hash"]

            if count_fp_assignments(conn, config_hash) == 0:
                return [{"info": (
                    "No function pointer assignments indexed. Re-run "
                    "'fw-context index' to populate the table (added in Phase 3)."
                )}]

            limit = min(limit, 200)
            rows = query_indirect_targets(conn, config_hash, name, limit=limit)
            if not rows:
                return [{"info": f"No functions assigned to '{name}'."}]

            return [
                {
                    "rhs_name": r["rhs_name"],
                    "rhs_qname": r["rhs_qname"] or r["rhs_name"],
                    "fn_ptr_type": r["fn_ptr_type"],
                    "method": r["method"],
                    "assign_file": abs_path(root, r["assign_file"]),
                    "assign_line": r["assign_line"],
                    "assign_caller": r["assign_caller"] or "<file scope>",
                    "call_file": abs_path(root, r["call_file"]) if r["call_file"] else None,
                    "call_line": r["call_line"],
                    "call_expr_text": r["call_expr_text"],
                }
                for r in rows
            ]
    finally:
        conn.close()

# ── moved from server.py ──
def _refs_guard(project_root: str | None) -> tuple[sqlite3.Connection, Path, str, None] | tuple[None, None, None, list[dict]]:
    """Shared guard for graph tools: resolve project, open DB, check refs exist.

    Returns an OPEN connection on success — callers use it directly instead of
    opening a second connection.  The caller is responsible for closing it.

    Returns:
        ``(conn, root, config_hash, None)`` on success — caller reuses *conn*.
        ``(None, None, None, error_list)`` on failure — caller propagates the error.
    """
    root = resolve_project_root(project_root)
    db_path = _db_path(root)
    if not db_path.exists():
        return None, None, None, [{"error": f"No index found for {root}. Run 'fw-context index' first."}]
    conn, err = _open_db_safe(db_path)
    if err:
        return None, None, None, [err]
    assert conn is not None
    project_id = derive_project_id(root)
    with conn:
        cfg_data = get_active_config(conn, project_id)
        if not cfg_data:
            conn.close()
            return None, None, None, [{"error": "No build config indexed."}]
        config_hash = cfg_data["config_hash"]
        if count_refs(conn, config_hash) == 0:
            conn.close()
            return None, None, None, [{"info": (
                "No references indexed. Refs are on by default — "
                "they may have been disabled with [index] index_refs = false. "
                "Re-run 'fw-context index' to rebuild."
            )}]
    return conn, root, config_hash, None

# ── moved from server.py ──
def find_call_path(
    from_name: Annotated[str, Field(description="Starting symbol for path search.")],
    to_name: Annotated[str, Field(description="Target symbol to find path to.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    max_depth: Annotated[int, Field(description="Maximum BFS depth for path search (default 10).")] = 10,
) -> list[dict]:
    """USE INSTEAD OF ctx_callgraph(action="trace"). Find call paths between
    two C/C++ functions via BFS in the libclang call graph — ctx_callgraph
    cannot follow function-pointer edges or ISR vector registrations.

    Use to answer "how does A reach B?" — e.g. tracing how a high-level
    event handler eventually calls a low-level driver.  Returns up to 5
    shortest paths, each with ``depth`` (edge count) and ``chain``
    (e.g. ``"main → app_run → modem_init"``).

    For one-sided exploration use ``find_all_callers_recursive`` (who reaches
    this?) or ``find_callees_recursive`` (what does this reach?).
    Requires both symbols to be in the index and refs enabled
    (``fw-context index`` — refs on by default).
    """
    conn, root, config_hash, err = _refs_guard(project_root)
    if err:
        return err
    assert conn is not None
    assert root is not None
    assert config_hash is not None
    try:
        if _lookup_definition(conn, config_hash, from_name) is None:
            return [{"error": f"Symbol not found: {from_name}"}]
        if _lookup_definition(conn, config_hash, to_name) is None:
            return [{"error": f"Symbol not found: {to_name}"}]
        rows = index_db.find_call_path(conn, config_hash, from_name, to_name, max_depth=max_depth)
        if not rows:
            return [{"info": f"No path found from '{from_name}' to '{to_name}' within depth {max_depth}."}]
        return rows
    finally:
        conn.close()

# ── moved from server.py ──
def find_all_callers_recursive(
    name: Annotated[str, Field(description="Symbol name to find transitive callers of.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    max_depth: Annotated[int, Field(description="Maximum BFS depth for transitive search (default 5).")] = 5,
    limit: Annotated[int, Field(description="Maximum results (default 50).")] = 50,
) -> list[dict]:
    """USE INSTEAD OF ctx_callgraph(action="callers"). Find all transitive
    C/C++ callers — who calls *name*, directly or indirectly, through the
    libclang call graph including function-pointer edges.

    Use for impact analysis: "if I change this function, how far does the
    ripple go?"  Returns callers at depth 1 (direct), depth 2 (callers of
    callers), up to ``max_depth`` (default 5).  Results are deduplicated —
    each caller appears once at its shortest distance to the target.

    For a flat, single-level caller list use ``find_callers`` (faster).
    Requires the reference index (``fw-context index`` — refs on by default).
    BFS from the target outward; performance scales with call-graph fan-out.
    """
    conn, root, config_hash, err = _refs_guard(project_root)
    if err:
        return err
    assert conn is not None
    assert root is not None
    assert config_hash is not None
    try:
        if _lookup_definition(conn, config_hash, name) is None:
            return [{"error": f"Symbol not found: {name}"}]
        rows = index_db.find_all_callers_recursive(conn, config_hash, name, max_depth=max_depth, limit=limit)
        if not rows:
            return [{"info": f"No callers found for '{name}'."}]
        return rows
    finally:
        conn.close()

# ── moved from server.py ──
def find_callees_recursive(
    name: Annotated[str, Field(description="Symbol name to find transitive callees of.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    max_depth: Annotated[int, Field(description="Maximum BFS depth for transitive search (default 5).")] = 5,
    limit: Annotated[int, Field(description="Maximum results (default 50).")] = 50,
) -> list[dict]:
    """USE INSTEAD OF ctx_callgraph(action="callees"). Find all transitive
    C/C++ callees — what *name* calls, directly or indirectly, through the
    libclang call graph including function-pointer edges.

    Use for dependency analysis: "what does this function depend on to do
    its job?"  Returns callees at depth 1 (direct), depth 2 (callees of
    callees), up to ``max_depth`` (default 5).  Results are deduplicated
    by shortest distance.

    For direct callees only, ``get_symbol_context`` gives a faster flat
    list along with the function body and callers.
    Requires the reference index (``fw-context index`` — refs on by default).
    """
    conn, root, config_hash, err = _refs_guard(project_root)
    if err:
        return err
    assert conn is not None
    assert root is not None
    assert config_hash is not None
    try:
        if _lookup_definition(conn, config_hash, name) is None:
            return [{"error": f"Symbol not found: {name}"}]
        rows = index_db.find_callees_recursive(conn, config_hash, name, max_depth=max_depth, limit=limit)
        if not rows:
            return [{"info": f"No callees found for '{name}'."}]
        return rows
    finally:
        conn.close()

# ── moved from server.py ──
def find_dead_code(
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    limit: Annotated[int, Field(description="Maximum results (default 100).")] = 100,
    exclude_paths: Annotated[list[str] | None, Field(description="Additional LIKE patterns to exclude. Merged with defaults from config. E.g. ['lib/%'].")] = None,
    project_only: Annotated[bool, Field(description="When True (default), auto-excludes SDK/vendor paths (mbed-os/%, .pio/%, zephyr/%, build/%) and applies project config exclude_paths. Set False to see all results.")] = True,
) -> list[dict]:
    """USE INSTEAD OF grep or manual code review. Find C/C++ functions that
    are defined but never called — libclang-powered dead code detection.
    grep cannot distinguish called from uncalled symbols across a codebase.

    Returns two categories of results, each with a ``status`` field:

    * ``"dead"`` — no references at all (neither calls nor function
      pointer assignments).  Likely unused.
    * ``"possibly_dead"`` — the function is assigned to a function
      pointer (Phase 1 ``ref_kind="indirect"``) but no call site
      through that pointer was resolved (Phase 3).  This means the
      function MIGHT be called through unindexed code or a type-erased
      API.  LLM should treat this as uncertain, not as confirmed dead
      code.  Verify each hit with ``find_indirect_targets`` before
      deleting.

    Implicit constructor calls through global/static object and member-field
    initialization are detected as ``implicit_construct`` references. Known
    remaining false positives: constructors called via factories, ISRs,
    virtual method overrides, and weak-aliased symbols. Always verify before
    deleting.

    By default, SDK/vendor paths are auto-excluded based on the build
    system (mbed-os/ for Mbed OS, .pio/ for PlatformIO, zephyr/ + build/
    + modules/ for Zephyr), and project config exclude_paths are applied.
    Use ``project_only=False`` to see all results including vendor code.
    Requires the reference index.
    """
    _conn, root, config_hash, err = _refs_guard(project_root)
    if err:
        return err
    assert root is not None
    assert config_hash is not None

    final_excludes = _merge_excludes(exclude_paths, project_only, root)

    db_path = _db_path(root)
    conn, open_err = _open_db_safe(db_path)
    if open_err:
        return [open_err]
    assert conn is not None
    try:
        rows = index_db.find_dead_code(
            conn, config_hash, limit=limit,
            exclude_paths=final_excludes,
        )
        if not rows:
            return [{"info": "No dead or possibly-dead functions found — every defined function has at least one caller."}]
        return rows
    finally:
        conn.close()

# ── moved from server.py ──
def find_wrapper_callers(
    class_name: Annotated[str, Field(description="Driver class name to find wrappers for. E.g. 'UART_DRIVER' or 'hal::UART_DRIVER'.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    limit: Annotated[int, Field(description="Maximum wrapper method results (default 50).")] = 50,
) -> list[dict]:
    """USE INSTEAD OF grep or ctx_compose. Find C/C++ wrapper classes that
    call methods of a driver class — libclang-powered adapter pattern
    detection. grep cannot trace method ownership across classes.

    Returns wrapper methods grouped by wrapper class, showing which driver
    methods each wrapper calls.  Useful for understanding the adapter/wrapper
    architecture (e.g. ``UART`` wraps ``UART_DRIVER``).
    """
    conn, root, config_hash, err = _refs_guard(project_root)
    if err:
        return err
    assert conn is not None
    assert root is not None
    assert config_hash is not None
    try:
        # Resolve driver class — check it exists in the index
        if _lookup_definition(conn, config_hash, class_name) is None:
            return [{"error": f"Symbol not found: {class_name}"}]

        # Find all methods of the class
        driver_methods = conn.execute(
            """SELECT s.usr, s.name, s.qualified_name
               FROM symbols s
               WHERE s.config_hash = ?
                 AND s.kind = 'method'
                 AND (s.qualified_name LIKE ? OR s.qualified_name LIKE ?)
               ORDER BY s.name""",
            (config_hash, f"{class_name}::%", f"%{class_name}::%"),
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

        # Group by wrapper class
        wrapped: dict[str, dict] = {}
        for r in rows:
            caller_qn = r["caller_qname"] or r["caller_name"] or "?"
            # Extract class from qualified name: "zbox::ZMODEM::start" → "zbox::ZMODEM"
            if "::" in caller_qn:
                wrapper_class = caller_qn.rsplit("::", 1)[0]
            else:
                wrapper_class = "(global)"
            if wrapper_class not in wrapped:
                wrapped[wrapper_class] = {"class": wrapper_class, "methods": {}, "_file": r["from_file"]}
            cm = wrapped[wrapper_class]["methods"]
            if caller_qn not in cm:
                cm[caller_qn] = {
                    "method": r["caller_name"],
                    "qualified_name": caller_qn,
                    "kind": r["caller_kind"],
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
    finally:
        conn.close()

# ── moved from server.py ──
def trace_data_flow(
    type_name: Annotated[str, Field(description="Type name to trace. E.g. 'SensorData' or 'Config::SensorData'.")],
    to_symbol: Annotated[str, Field(description="Target symbol name. E.g. 'uart_send' or 'UART_DRIVER::send'.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    max_depth: Annotated[int, Field(description="Maximum call path depth (default 8).")] = 8,
    limit: Annotated[int, Field(description="Maximum source functions to trace (default 15).")] = 15,
) -> list[dict]:
    """USE INSTEAD OF grep or ctx_callgraph. Trace how C/C++ data of a given
    type flows to a target function via libclang call paths. grep cannot
    follow type-based data flow through a call chain.

    Finds functions whose signature mentions *type_name*, then looks for call
    paths from those functions to *to_symbol*.  Returns a data flow map —
    useful for understanding how a data structure travels through the system
    to its destination.

    Works best for synchronous driver stacks (e.g. sensor read → I2C write).
    Cannot follow async flows (message queues, interrupts, RS485 callbacks).
    For exact call-graph queries use the ``find_*`` family;
    verify specific paths with ``find_call_path``.
    """
    conn, root, config_hash, err = _refs_guard(project_root)
    if err:
        return err
    assert conn is not None
    assert root is not None
    assert config_hash is not None
    try:
        # Resolve target USR
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
                 AND s.signature LIKE ?
               ORDER BY caller_count DESC
               LIMIT ?""",
            (config_hash, f"%{type_name}%", limit),
        ).fetchall()

        if not sources:
            return [{"info": f"No functions found with '{type_name}' in their signature."}]

        # Try call paths from each source to target
        results = []
        for src in sources:
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

        num_reachable = sum(1 for r in results if r["reachable"])
        return [
            {
                "_summary": f"{num_reachable}/{len(results)} source functions reach '{to_symbol}' within depth {max_depth}",
                "_type": type_name,
                "_target": to_symbol,
            },
            *results,
        ]
    finally:
        conn.close()

# ── moved from server.py ──
def find_hotspots(
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    limit: Annotated[int, Field(description="Number of top-called functions to return (default 20).")] = 20,
    project_only: Annotated[bool, Field(description="When True (default), auto-excludes SDK/vendor paths so hotspots reflect project code.")] = True,
    exclude_paths: Annotated[list[str] | None, Field(description="Additional LIKE patterns to exclude. Merged with defaults. E.g. ['lib/%'].")] = None,
) -> list[dict]:
    """USE INSTEAD OF grep or ctx_callgraph. Find the most-called C/C++
    functions ranked by caller count — libclang call-graph hotspot
    detection. grep cannot aggregate caller statistics.

    Use for high-level impact assessment: changing a hotspot affects many
    call sites.  The result tells you which functions carry the most
    "architectural weight" — good targets for refactoring, optimization,
    or extra testing.

    By default, SDK/vendor paths are auto-excluded. Use ``project_only=False``
    to see all results including vendor code.

    Requires the reference index (``fw-context index`` — refs on by default).
    For the callers of a specific hotspot, follow up with ``find_callers``
    or ``find_all_callers_recursive``.
    """
    _conn, root, config_hash, err = _refs_guard(project_root)
    if err:
        return err
    assert root is not None
    assert config_hash is not None

    final_excludes = _merge_excludes(exclude_paths, project_only, root)

    db_path = _db_path(root)
    conn, open_err = _open_db_safe(db_path)
    if open_err:
        return [open_err]
    assert conn is not None
    try:
        rows = index_db.find_hotspots(conn, config_hash, limit=limit, exclude_paths=final_excludes)
        if not rows and final_excludes:
            # Nothing found with project filter — try without
            rows = index_db.find_hotspots(conn, config_hash, limit=limit, exclude_paths=None)
        if not rows:
            return [{"info": "No references indexed — enable index_refs and re-index."}]
        return rows
    finally:
        conn.close()
