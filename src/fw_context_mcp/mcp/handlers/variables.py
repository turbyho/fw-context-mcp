"""Variable MCP tools — see mcp/server.py for registration."""

from __future__ import annotations

import logging
import sqlite3
from typing import Annotated

from pydantic import Field

from ...utils import abs_path
from ..shared.context import _resolve_handler_context
from ..shared.stale import _stale_files

log = logging.getLogger(__name__)

_VALID_KINDS = frozenset({"varglobal", "varlocal", "variable"})
_VAR_KINDS = ("varglobal", "varlocal", "variable")


def find_variables(
    name: Annotated[str, Field(description="Variable name or prefix to search. "
        "Uses LIKE match (e.g. 'g_' finds g_debug_level, g_state).")],
    project_root: Annotated[str | None, Field(description="Project root. "
        "Auto-detected if omitted.")] = None,
    kind: Annotated[str | None, Field(description="Filter by kind: "
        "'varglobal', 'varlocal', or None for both.")] = None,
    limit: Annotated[int, Field(description="Maximum results "
        "(default 20, max 100).")] = 20,
) -> list[dict]:
    """Find C/C++ variables by name or prefix and trace who reads or
    writes them through the call graph.  libclang-powered: splits
    variables into global (``varglobal`` — file/namespace/class-scope)
    and local (``varlocal`` — inside a function body).

    Each result includes a type signature (``bool timeSet``,
    ``const IPAddress modbus_ip``), the enclosing function for locals
    (``"<file scope>"`` for globals), and a ``references`` list showing
    every function that reads or writes the variable — the same
    ``ref_kind`` values as ``find_references`` (``"call"``, ``"ref"``,
    ``"member"``).

    Use when you need to understand shared state, find who modifies a
    global variable, trace side effects, or distinguish important globals
    from loop counters.  For general symbol search use ``search_code`` or
    ``lookup_symbol``.  For all references to a specific variable
    (including reads in expressions), use ``find_references``.

    Legacy indexes with ``kind="variable"`` (pre-split) are detected and
    included in results — reindex to fully benefit from the split.

    Read-only. No side effects.

    Args:
        name: Variable name or prefix to search. Uses LIKE match
            (e.g. ``g_`` finds ``g_debug_level``, ``g_state``).
        project_root: Project root directory. Auto-detected if omitted.
        kind: Optional kind filter — ``"varglobal"``, ``"varlocal"``,
            or ``None`` (both). Default ``None``.
        limit: Maximum results (default 20, max 100).

    Returns:
        list of dicts, each with: name (str), qualified_name (str),
        kind (str — ``"varglobal"`` or ``"varlocal"``), file (str),
        line (int), signature (str — e.g. ``"const IPAddress modbus_ip"``),
        enclosing_function (str — function name for varlocal,
        ``"<file scope>"`` for varglobal), enclosing_class (str — class
        name for static members, empty otherwise),
        references (list[dict] — ``function``, ``file``, ``line``,
        ``ref_kind``).
    """
    if not name.strip():
        return [{"error": "Variable name must be non-empty."}]
    if kind is not None and kind not in _VALID_KINDS:
        return [{"error": f"Invalid kind: {kind!r}. Expected 'varglobal', 'varlocal', or None."}]
    limit = min(limit, 100)

    ctx, err = _resolve_handler_context(project_root)
    if err:
        return err

    root = ctx.root

    def _do_find(c: sqlite3.Connection, config_hash: str) -> list[dict]:
        if kind:
            kind_filter = "AND s.kind = ?"
        else:
            kind_filter = f"AND s.kind IN ({','.join('?' * len(_VAR_KINDS))})"

        esc_name = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params: list = [config_hash, f"%{esc_name}%", f"%{esc_name}%"]
        if kind:
            params.append(kind)
        else:
            params.extend(_VAR_KINDS)
        params.append(limit)

        rows = c.execute(
            f"""SELECT s.* FROM symbols s
                WHERE s.config_hash = ?
                  AND (s.name LIKE ? ESCAPE '\\' OR s.qualified_name LIKE ? ESCAPE '\\')
                  {kind_filter}
                  AND s.is_definition = 1
                ORDER BY CASE s.kind WHEN 'varglobal' THEN 0 ELSE 1 END,
                         s.name
                LIMIT ?""",
            params,
        ).fetchall()

        if not rows:
            return []

        rows_list = [dict(r) for r in rows]

        # Batch parent lookups
        parent_usrs = {r["parent_usr"] for r in rows_list if r.get("parent_usr")}
        parents: dict[str, dict] = {}
        if parent_usrs:
            placeholders = ",".join("?" * len(parent_usrs))
            parent_rows = c.execute(
                f"SELECT usr, name, qualified_name, kind FROM symbols "
                f"WHERE usr IN ({placeholders})",
                tuple(parent_usrs),
            ).fetchall()
            parents = {r["usr"]: dict(r) for r in parent_rows}

        # Batch refs lookups
        var_usrs = {r["usr"] for r in rows_list if r.get("usr")}
        refs_map: dict[str, list[dict]] = {}
        if var_usrs:
            placeholders = ",".join("?" * len(var_usrs))
            ref_rows = c.execute(
                f"""SELECT r.to_usr, r.from_file, r.from_line, r.ref_kind,
                           r.from_usr, c.name AS caller_name,
                           c.qualified_name AS caller_qname, c.kind AS caller_kind
                    FROM refs r
                    LEFT JOIN symbols c ON c.config_hash = r.config_hash
                        AND c.usr = r.from_usr AND c.is_definition = 1
                    WHERE r.to_usr IN ({placeholders})
                    ORDER BY r.from_file, r.from_line
                    LIMIT 100""",
                tuple(var_usrs),
            ).fetchall()
            for ref in ref_rows:
                rdict = dict(ref)
                usr = rdict["to_usr"]
                if usr not in refs_map:
                    refs_map[usr] = []
                if len(refs_map[usr]) < 30:
                    refs_map[usr].append({
                        "function": rdict.get("caller_qname") or rdict.get("caller_name") or "<unknown>",
                        "file": abs_path(root, rdict["from_file"]),
                        "line": rdict["from_line"],
                        "ref_kind": rdict["ref_kind"],
                    })

        results = []
        for row in rows_list:
            parent_usr = row.get("parent_usr") or ""
            enclosing_function = "<file scope>"
            enclosing_class = ""

            if parent_usr:
                parent = parents.get(parent_usr)
                if parent:
                    pkind = parent["kind"]
                    if pkind in ("function", "method", "constructor", "destructor"):
                        enclosing_function = parent["qualified_name"] or parent["name"]
                    elif pkind in ("class", "struct", "union"):
                        enclosing_class = parent["qualified_name"] or parent["name"]

            var_usr = row["usr"]
            references = refs_map.get(var_usr, [])
            results.append({
                "name": row["name"],
                "qualified_name": row["qualified_name"] or row["name"],
                "kind": row["kind"],
                "file": abs_path(root, row["file_path"]),
                "line": row["line"],
                "signature": row["signature"] or "",
                "enclosing_function": enclosing_function,
                "enclosing_class": enclosing_class,
                "references": references,
            })

        return results

    try:
        with ctx.conn:
            results = _do_find(ctx.conn, ctx.config_hash)
            file_paths = [abs_path(ctx.root, r["file"]) for r in results if "file" in r]
            if file_paths:
                stale = _stale_files(ctx.conn, ctx.config_hash, file_paths, ctx.root)
                if stale:
                    from fw_context_mcp.mcp.background import _ensure_daemon_running
                    _ensure_daemon_running(ctx.root)
                    return [{"warning": (
                        f"Results may be stale — {len(stale)} file(s) changed. "
                        "Background reindex in progress. Run 'fw-context index' to force full update."
                    )}] + results
            return results
    finally:
        pass  # connection managed by connection.py cache
