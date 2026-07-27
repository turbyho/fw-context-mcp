"""Variable MCP tools — see mcp/server.py for registration."""

from __future__ import annotations

import logging
from typing import Annotated

from pydantic import Field

from ...indexer.db import get_active_config
from ...utils import abs_path
from ..shared.context import _open_db_safe, _resolve_context

log = logging.getLogger(__name__)

_VALID_KINDS = frozenset({"varglobal", "varlocal"})
_VAR_KINDS = ("varglobal", "varlocal", "variable")


def _format_variable_result(conn, config_hash: str, row: dict, root: str) -> dict:
    """Enrich a variable row with enclosing_function, enclosing_class, and references."""
    parent_usr = row["parent_usr"] or ""
    enclosing_function = "<file scope>"
    enclosing_class = ""

    if parent_usr:
        parent_row = conn.execute(
            "SELECT name, qualified_name, kind FROM symbols "
            "WHERE config_hash = ? AND usr = ? LIMIT 1",
            (config_hash, parent_usr),
        ).fetchone()
        if parent_row:
            pkind = parent_row["kind"]
            if pkind in ("function", "method", "constructor", "destructor"):
                enclosing_function = parent_row["qualified_name"] or parent_row["name"]
            elif pkind in ("class", "struct", "union"):
                enclosing_class = parent_row["qualified_name"] or parent_row["name"]

    var_usr = row["usr"]
    ref_rows = conn.execute(
        """SELECT r.from_file, r.from_line, r.ref_kind, r.from_usr,
                  c.name AS caller_name, c.qualified_name AS caller_qname,
                  c.kind AS caller_kind
           FROM refs r
           LEFT JOIN symbols c ON c.config_hash = r.config_hash
               AND c.usr = r.from_usr AND c.is_definition = 1
           WHERE r.config_hash = ? AND r.to_usr = ?
           ORDER BY r.from_file, r.from_line
           LIMIT 30""",
        (config_hash, var_usr),
    ).fetchall()

    references = [
        {
            "function": r["caller_qname"] or r["caller_name"] or "<unknown>",
            "file": abs_path(root, r["from_file"]),
            "line": r["from_line"],
            "ref_kind": r["ref_kind"],
        }
        for r in ref_rows
    ]

    return {
        "name": row["name"],
        "qualified_name": row["qualified_name"] or row["name"],
        "kind": row["kind"],
        "file": abs_path(root, row["file_path"]),
        "line": row["line"],
        "signature": row["signature"] or "",
        "enclosing_function": enclosing_function,
        "enclosing_class": enclosing_class,
        "references": references,
    }


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
    if kind is not None and kind not in _VALID_KINDS:
        raise ValueError(
            f"Invalid kind: {kind!r}. Expected 'varglobal', 'varlocal', or None."
        )
    limit = min(limit, 100)

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

            rows = conn.execute(
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

            return [
                _format_variable_result(conn, config_hash, dict(r), str(root))
                for r in rows
            ]
    finally:
        conn.close()
