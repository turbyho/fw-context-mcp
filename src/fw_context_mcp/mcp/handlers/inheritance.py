"""Inheritance MCP tools — see mcp/server.py for registration."""

from __future__ import annotations

import logging
import sqlite3
from typing import Annotated

from pydantic import Field

from ...indexer.db import (
    get_class_members as query_class_members,
)
from ...indexer.db import (
    get_direct_bases,
    get_direct_bases_batch,
    get_direct_derived,
    get_direct_derived_batch,
    get_overrides_for_method,
)
from ...indexer.db import (
    get_template_instances as query_template_instances,
)
from ...utils import abs_path
from ._base import BaseHandler
from .source import _lookup_definition

log = logging.getLogger(__name__)

# ── moved from server.py ──
def get_inheritance_chain(
    class_name: Annotated[str, Field(description="Class or struct name to get inheritance information for. E.g. 'UART_DRIVER' or 'comm::MODEM'.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    transitive: Annotated[bool, Field(description="When True, walk the full inheritance tree both up (ancestors) and down (descendants). Default: False (direct bases and derived only).")] = False,
    max_depth: Annotated[int, Field(description="Maximum BFS depth for transitive walk (default 10).", ge=1, le=50)] = 10,
) -> dict:
    """Return the C++ inheritance chain for a class or struct —
    libclang-aware hierarchy. Resolves base/derived class relationships
    across all translation units, which single-file reading cannot do.

    Shows direct base classes (what this inherits from) and direct derived
    classes (what inherits from this), along with access level and virtual
    flag for each edge.

    When ``transitive=True``, walks the full hierarchy up to all ancestors
    and down to all descendants (bounded by ``max_depth``). Uses BFS with
    cycle detection to handle diamond inheritance.

    For class members use ``get_class_members``. For virtual method
    override chains use ``get_method_overrides``.

    Read-only. No side effects.

    Args:
        class_name: Class or struct name to get inheritance information for.
            E.g. ``'UART_DRIVER'`` or ``'comm::MODEM'``.
        project_root: Project root. Auto-detected if omitted.
        transitive: When True, walk the full inheritance tree both up
            (ancestors) and down (descendants). Default: False (direct
            bases and derived only).
        max_depth: Maximum BFS depth for transitive walk (default 10,
            clamped to 1–50).

    Returns:
        dict: {
            name, qualified_name, kind, file, line,
            bases: [{name, usr, access, is_virtual, file}],
            derived: [{name, usr, access, is_virtual, file}],
            all_bases: [...] (when transitive=True, ancestors sorted by depth),
            all_derived: [...] (when transitive=True, descendants sorted by depth)
        }
    """
    try:
        db = BaseHandler.resolve_db_context(project_root)
    except RuntimeError as e:
        return {"error": str(e)}
    with db.conn:
        config_hash = db.config_hash
        root = db.root
        row = _lookup_definition(db.conn, config_hash, class_name,
                                 preferred_kinds=("class", "struct"))
        if not row:
            return {"error": f"Symbol not found: {class_name}"}
        if row["kind"] not in ("class", "struct"):
            return {"error": f"'{class_name}' is a {row['kind']}, not a class or struct."}

        usr = row["usr"]
        result: dict = {
            "name": row["name"],
            "qualified_name": row["qualified_name"],
            "kind": row["kind"],
            "file": abs_path(root, row["file_path"]),
            "line": row["line"],
        }

        # ── Direct base classes (parents) ──
        bases = get_direct_bases(db.conn, config_hash, usr)
        result["bases"] = [
            {
                "name": b.get("base_name") or "<unknown>",
                "usr": b["base_usr"],
                "access": b["access"],
                "is_virtual": bool(b["is_virtual"]),
                "file": abs_path(root, b["base_file"]) if b.get("base_file") else None,
            }
            for b in bases
        ]

        # ── Direct derived classes (children) ──
        derived = get_direct_derived(db.conn, config_hash, usr)
        result["derived"] = [
            {
                "name": d.get("derived_name") or "<unknown>",
                "usr": d["derived_usr"],
                "access": d["access"],
                "is_virtual": bool(d["is_virtual"]),
                "file": abs_path(root, d["derived_file"]) if d.get("derived_file") else None,
            }
            for d in derived
        ]

        # ── Transitive walk (level-by-level BFS with batched queries) ──
        if transitive:
            # ── Walk up — ancestors ──
            all_bases: list[dict] = []
            visited_up: set[str] = {usr}
            # Current BFS level: list of (usr, access, is_virtual)
            current_level: list[tuple[str, str, bool]] = [
                (b["base_usr"], b["access"], bool(b["is_virtual"]))
                for b in bases
                if b["base_usr"] not in visited_up
            ]
            for depth in range(1, max_depth + 1):
                if not current_level:
                    break
                # Deduplicate within level (diamond inheritance)
                seen_level: set[str] = set()
                unique_level: list[tuple[str, str, bool]] = []
                for cur_usr, access, is_virtual in current_level:
                    if cur_usr not in seen_level and cur_usr not in visited_up:
                        seen_level.add(cur_usr)
                        visited_up.add(cur_usr)
                        unique_level.append((cur_usr, access, is_virtual))
                current_level = unique_level
                if not current_level:
                    break

                level_usrs = [u for u, _, _ in current_level]
                # Batch lookup symbols for all USRs at this depth
                placeholders = ",".join("?" * len(level_usrs))
                symbol_rows = db.conn.execute(
                    f"SELECT usr, name, kind, file_path FROM symbols WHERE config_hash=? AND usr IN ({placeholders})",
                    (config_hash, *level_usrs),
                ).fetchall()
                symbol_map: dict[str, sqlite3.Row] = {r["usr"]: r for r in symbol_rows}

                # Batch lookup bases for the next depth
                bases_batch = get_direct_bases_batch(db.conn, config_hash, level_usrs)

                # Build results and next level
                next_level: list[tuple[str, str, bool]] = []
                for cur_usr, access, is_virtual in current_level:
                    cur_row = symbol_map.get(cur_usr)
                    all_bases.append({
                        "name": cur_row["name"] if cur_row else "<unknown>",
                        "usr": cur_usr,
                        "access": access,
                        "is_virtual": is_virtual,
                        "depth": depth,
                        "file": abs_path(root, cur_row["file_path"]) if cur_row and cur_row["file_path"] else None,
                        "kind": cur_row["kind"] if cur_row else None,
                    })
                    for gb in bases_batch.get(cur_usr, []):
                        if gb["base_usr"] not in visited_up:
                            next_level.append((gb["base_usr"], gb["access"], bool(gb["is_virtual"])))
                current_level = next_level
            result["all_bases"] = all_bases

            # ── Walk down — descendants ──
            all_derived: list[dict] = []
            visited_down: set[str] = {usr}
            current_level = [
                (d["derived_usr"], d["access"], bool(d["is_virtual"]))
                for d in derived
                if d["derived_usr"] not in visited_down
            ]
            for depth in range(1, max_depth + 1):
                if not current_level:
                    break
                seen_level: set[str] = set()
                unique_level: list[tuple[str, str, bool]] = []
                for cur_usr, access, is_virtual in current_level:
                    if cur_usr not in seen_level and cur_usr not in visited_down:
                        seen_level.add(cur_usr)
                        visited_down.add(cur_usr)
                        unique_level.append((cur_usr, access, is_virtual))
                current_level = unique_level
                if not current_level:
                    break

                level_usrs = [u for u, _, _ in current_level]
                placeholders = ",".join("?" * len(level_usrs))
                symbol_rows = db.conn.execute(
                    f"SELECT usr, name, kind, file_path FROM symbols WHERE config_hash=? AND usr IN ({placeholders})",
                    (config_hash, *level_usrs),
                ).fetchall()
                symbol_map = {r["usr"]: r for r in symbol_rows}

                derived_batch = get_direct_derived_batch(db.conn, config_hash, level_usrs)

                next_level = []
                for cur_usr, access, is_virtual in current_level:
                    cur_row = symbol_map.get(cur_usr)
                    all_derived.append({
                        "name": cur_row["name"] if cur_row else "<unknown>",
                        "usr": cur_usr,
                        "access": access,
                        "is_virtual": is_virtual,
                        "depth": depth,
                        "file": abs_path(root, cur_row["file_path"]) if cur_row and cur_row["file_path"] else None,
                        "kind": cur_row["kind"] if cur_row else None,
                    })
                    for gd in derived_batch.get(cur_usr, []):
                        if gd["derived_usr"] not in visited_down:
                            next_level.append((gd["derived_usr"], gd["access"], bool(gd["is_virtual"])))
                current_level = next_level
            result["all_derived"] = all_derived

    return result

# ── moved from server.py ──
def get_class_members(
    class_name: Annotated[str, Field(description="Class or struct name. E.g. 'ModemManager' or 'zbox::ZMODEM'.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
) -> dict:
    """Return all methods, fields, and nested types of a C/C++ class/struct —
    libclang-powered member table. Groups members by kind (method,
    constructor, field, enum, etc.), distinguishing class members from
    free functions across the entire codebase.

    Members are grouped by kind (method, constructor, destructor, field, enum,
    typedef, class, struct). Each member includes its signature, virtual flags,
    and source line. Works for C structs too — they just won't have methods.

    For inheritance hierarchy use ``get_inheritance_chain``. For individual
    method details use ``get_symbol_context``.

    Read-only. No side effects.

    Args:
        class_name: Class or struct name. E.g. ``'ModemManager'`` or
            ``'comm::MODEM'``.
        project_root: Project root. Auto-detected if omitted.

    Returns:
        dict: {name, qualified_name, kind, file, line, members: {kind:
        [{name, qualified_name, signature, is_virtual, is_pure_virtual,
        line}]}, member_count}
    """
    try:
        db = BaseHandler.resolve_db_context(project_root)
    except RuntimeError as e:
        return {"error": str(e)}
    with db.conn:
        config_hash = db.config_hash
        root = db.root
        row = _lookup_definition(db.conn, config_hash, class_name,
                                 preferred_kinds=("class", "struct"))
        if not row:
            return {"error": f"Symbol not found: {class_name}"}
        if row["kind"] not in ("class", "struct"):
            return {"error": f"'{class_name}' is a {row['kind']}, not a class or struct."}

        usr = row["usr"]
        result: dict = {
            "name": row["name"],
            "qualified_name": row["qualified_name"],
            "kind": row["kind"],
            "file": abs_path(root, row["file_path"]),
            "line": row["line"],
        }

        # ── Members grouped by kind ──
        members = query_class_members(db.conn, config_hash, usr)
        grouped: dict[str, list[dict]] = {}
        for m in members:
            k = m["kind"]
            if k not in grouped:
                grouped[k] = []
            grouped[k].append({
                "name": m["name"],
                "qualified_name": m["qualified_name"],
                "signature": m["signature"] or "",
                "is_virtual": bool(m["is_virtual"]),
                "is_pure_virtual": bool(m["is_pure_virtual"]),
                "line": m["line"],
            })
        result["members"] = grouped
        result["member_count"] = len(members)

        return result

# ── moved from server.py ──
def get_template_instances(
    template_name: Annotated[str, Field(description="Template name to find instantiations for. E.g. 'Callback' or 'mbed::Callback'.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
    limit: Annotated[int, Field(description="Maximum results (default 50).")] = 50,
) -> list[dict]:
    """Find all template instantiations for a C/C++ class or function
    template — libclang template-aware lookup. Finds concrete
    instantiations spread across all translation units, each with its
    full type signature. Text-based search cannot resolve template
    specializations across translation units.

    Returns concrete instantiations of the template — each with its full type
    signature (e.g. ``Callback<void(int)>``).  The template declaration itself
    is also returned as the first result when found.

    Uses the ``template_usr`` column populated during indexing via libclang's
    ``cursor.specialized_template``.

    For finding the template declaration itself use ``lookup_symbol``.

    Read-only. No side effects.

    Args:
        template_name: Template name to find instantiations for.
            E.g. ``'Callback'`` or ``'mbed::Callback'``.
        project_root: Project root. Auto-detected if omitted.
        limit: Maximum results (default 50).

    Returns:
        list[dict] with one element wrapping the template declaration:
        {name, qualified_name, kind, file, line, is_definition,
        signature, instances (list of dicts, each with name,
        qualified_name, kind, file, line, signature, is_definition),
        instance_count (int)}
    """
    limit = max(0, min(limit, 200))  # clamp
    try:
        db = BaseHandler.resolve_db_context(project_root)
    except RuntimeError as e:
        return [{"error": str(e)}]
    with db.conn:
        config_hash = db.config_hash
        root = db.root
        row = _lookup_definition(db.conn, config_hash, template_name,
                                 preferred_kinds=None)
        if not row:
            return [{"error": f"Symbol not found: {template_name}"}]
        if not row["is_template"]:
            return [{"error": f"'{template_name}' is not a template (kind: {row['kind']}, is_template: false)."}]

        template_usr = row["usr"]
        instances = query_template_instances(db.conn, config_hash, template_usr, limit=limit)

        result: list[dict] = [
            {
                "name": row["name"],
                "qualified_name": row["qualified_name"],
                "kind": row["kind"],
                "file": abs_path(root, row["file_path"]),
                "line": row["line"],
                "is_definition": bool(row["is_definition"]),
                "signature": row["signature"],
                "instances": [
                    {
                        "name": i["name"],
                        "qualified_name": i["qualified_name"],
                        "kind": i["kind"],
                        "file": abs_path(root, i["file_path"]),
                        "line": i["line"],
                        "signature": i["signature"],
                        "is_definition": bool(i["is_definition"]),
                    }
                    for i in instances
                ],
                "instance_count": len(instances),
            }
        ]
        return result

# ── moved from server.py ──
def get_method_overrides(
    method_name: Annotated[str, Field(description="Method name to get override information for. Use qualified name for disambiguation, e.g. 'UART_DRIVER::write'.")],
    project_root: Annotated[str | None, Field(description="Project root. Auto-detected if omitted.")] = None,
) -> dict:
    """Return C++ virtual method override information — libclang-powered
    vtable analysis. Resolves virtual dispatch across class hierarchies:
    shows which base-class method this overrides, and which derived-class
    methods override this one. Text-based search cannot resolve virtual
    dispatch across translation units.

    Shows what base-class method this method overrides, and what derived-class
    methods override this one.  Built from the ``overrides`` table which is
    populated during ``fw-context index`` via post-processing of the inheritance
    graph and virtual method signatures.

    For class-level inheritance, use ``get_inheritance_chain``.  For symbol
    details, use ``get_symbol_context``.

    Read-only. No side effects.

    Args:
        method_name: Method name to get override information for. Use
            qualified name for disambiguation, e.g.
            ``'UART_DRIVER::write'``.
        project_root: Project root. Auto-detected if omitted.

    Returns:
        dict: {
            name, qualified_name, kind, file, line, signature,
            overrides: [{usr, name, qualified_name, kind, file, line}],
            overridden_by: [{usr, name, qualified_name, kind, file, line}]
        }
    """
    try:
        db = BaseHandler.resolve_db_context(project_root)
    except RuntimeError as e:
        return {"error": str(e)}
    with db.conn:
        config_hash = db.config_hash
        root = db.root
        row = _lookup_definition(db.conn, config_hash, method_name,
                                 preferred_kinds=("method", "destructor"))
        if not row:
            return {"error": f"Symbol not found: {method_name}"}
        if row["kind"] not in ("method", "destructor"):
            return {"error": f"'{method_name}' is a {row['kind']}, not a method."}

        result: dict = {
            "name": row["name"],
            "qualified_name": row["qualified_name"],
            "kind": row["kind"],
            "file": abs_path(root, row["file_path"]),
            "line": row["line"],
            "signature": row["signature"],
        }

        ov = get_overrides_for_method(db.conn, config_hash, row["usr"])
        result["overrides"] = [
            {
                "usr": o["base_usr"],
                "name": o["name"],
                "qualified_name": o["qualified_name"],
                "kind": o["kind"],
                "file": abs_path(root, o["file_path"]) if o.get("file_path") else None,
                "line": o["line"],
            }
            for o in ov["overrides"]
        ]
        result["overridden_by"] = [
            {
                "usr": o["derived_usr"],
                "name": o["name"],
                "qualified_name": o["qualified_name"],
                "kind": o["kind"],
                "file": abs_path(root, o["file_path"]) if o.get("file_path") else None,
                "line": o["line"],
            }
            for o in ov["overridden_by"]
        ]
    return result

